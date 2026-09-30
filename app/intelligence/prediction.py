"""Prediction engine: ETA, late-delivery risk and *route-specific* carrier reliability.

ML predicts outcomes; the optimiser (optimizer.py) selects actions.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import brier_score_loss, mean_absolute_error, precision_score, recall_score, roc_auc_score

from app.core.geo import region
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.data import calibration
from app.intelligence.history import CAT_IDX, feature_vector, generate_history, history_source

log = get_logger("prediction")


@dataclass
class Prediction:
    predicted_days: float
    late_risk: float          # P(actual > carrier promise)
    reliability: float        # route-specific on-time probability (empirical-Bayes)
    sla_risk: float | None = None   # P(actual > customer SLA), if an SLA was given
    p90_days: float = 0.0


@dataclass
class ReliabilityTable:
    """Beta-Binomial shrinkage: route (state->state) -> region pair -> carrier prior."""
    k_route: int = 20
    stats: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))  # key -> [late, n]

    def fit(self, keys_late: list[tuple[tuple, int]]) -> None:
        for key, late in keys_late:
            for level in self._levels(key):
                self.stats[level][0] += late
                self.stats[level][1] += 1

    @staticmethod
    def _levels(key: tuple):
        c, o, d = key
        return [(c, o, d), (c, region(o), region(d)), (c,)]

    def reliability(self, carrier: str, o: str, d: str) -> float:
        prior_late, prior_n = self.stats.get((carrier,), [0, 0])
        rate = 1 - prior_late / max(prior_n, 1)
        for level in reversed(self._levels((carrier, o, d))[:-1]):     # region pair, then exact route
            late, n = self.stats.get(level, [0, 0])
            rate = (rate * self.k_route + (n - late)) / (self.k_route + n)
        return float(rate)


class PredictionEngine:
    def __init__(self, seed: int = 7, n_history: int = 24000) -> None:
        self.seed, self.n_history = seed, n_history
        self.eta_model: HistGradientBoostingRegressor | None = None
        self.risk_model: HistGradientBoostingClassifier | None = None
        self.table = ReliabilityTable()
        self.residuals = np.array([0.0])
        self.report: dict = {}
        self._lock = threading.Lock()
        self.risk_threshold = 0.2
        self.late_ratio = np.array([1.5])
        self.ontime_ratio = np.array([0.9])

    # ---------------------------------------------------------------- training
    def fit(self) -> dict:
        data = generate_history(self.n_history, self.seed)
        X, yd, yl = data["X"], data["y_days"], data["y_late"]
        n = len(X)
        cut = int(n * 0.8)                      # chronological-free random split is fine: rows are iid draws
        Xtr, Xte, ydtr, ydte, yltr, ylte = X[:cut], X[cut:], yd[:cut], yd[cut:], yl[:cut], yl[cut:]
        mask = [i in CAT_IDX for i in range(X.shape[1])]
        self.eta_model = HistGradientBoostingRegressor(loss="absolute_error", categorical_features=mask, max_iter=250,
                                                       learning_rate=0.08, random_state=self.seed).fit(Xtr, ydtr)
        self.risk_model = HistGradientBoostingClassifier(categorical_features=mask, max_iter=200, learning_rate=0.06,
                                                         random_state=self.seed).fit(Xtr, yltr)
        pred_te = self.eta_model.predict(Xte)
        self.residuals = ydte - pred_te
        prob = self.risk_model.predict_proba(Xte)[:, 1]
        # Empirical shapes used to turn a late-probability into P(actual > customer SLA).
        promised_tr = Xtr[:, -1]
        late_mask = yltr == 1
        self.late_ratio = np.sort(ydtr[late_mask] / promised_tr[late_mask])
        self.ontime_ratio = np.sort(ydtr[~late_mask] / promised_tr[~late_mask])
        # Threshold is chosen for F1 (a fixed 0.5 is meaningless when base late-rate is ~14%).
        best_thr, best_f1 = 0.5, -1.0
        for thr in np.linspace(0.05, 0.6, 56):
            pr, rc = precision_score(ylte, prob >= thr, zero_division=0), recall_score(ylte, prob >= thr, zero_division=0)
            f1 = 0 if pr + rc == 0 else 2 * pr * rc / (pr + rc)
            if f1 > best_f1:
                best_thr, best_f1 = float(thr), f1
        top = np.argsort(-prob)[: max(1, len(prob) // 10)]
        self.risk_threshold = best_thr
        self.report = {
            "n_train": int(cut), "n_test": int(n - cut),
            "eta_mae_days": round(float(mean_absolute_error(ydte, pred_te)), 3),
            "carrier_promise_mae_days": round(float(mean_absolute_error(ydte, Xte[:, -1])), 3),
            "late_auc": round(float(roc_auc_score(ylte, prob)), 3),
            "late_brier": round(float(brier_score_loss(ylte, prob)), 4),
            "base_late_rate": round(float(yl.mean()), 3),
            "risk_threshold_f1": round(best_thr, 2),
            "late_precision": round(float(precision_score(ylte, prob >= best_thr, zero_division=0)), 3),
            "late_recall": round(float(recall_score(ylte, prob >= best_thr, zero_division=0)), 3),
            "top_decile_late_rate": round(float(ylte[top].mean()), 3),
            "top_decile_lift": round(float(ylte[top].mean() / max(ylte.mean(), 1e-9)), 2),
            "history_source": history_source(), "calibration": calibration.token(),
            "late_definition": "late vs the carrier's own promise (not the Olist customer estimate)",
        }
        # Reliability table is built from the *training* rows only (no leakage into the report).
        rel = ReliabilityTable()
        rel.fit([(k, int(v)) for k, v in zip(data["keys"][:cut], yl[:cut])])
        self.table = rel
        log.info("prediction models trained", extra={"ctx": self.report})
        return self.report

    # --------------------------------------------------------------- inference
    def _sla_risk(self, late_risk: float, promised: float, sla_days: float) -> float:
        """P(actual > SLA) = P(late)*P(excess beyond SLA | late) + P(on-time)*P(on-time arrival > SLA)."""
        r = sla_days / max(promised, 0.5)
        p_late_beyond = float(np.mean(self.late_ratio > r))
        p_ontime_beyond = float(np.mean(self.ontime_ratio > r))
        return late_risk * p_late_beyond + (1 - late_risk) * p_ontime_beyond

    def predict_many(self, rows: list[tuple]) -> list[Prediction]:
        """Vectorised inference. rows = (carrier, origin, dest, service, month, weight, promised, sla_days|None)."""
        assert self.eta_model and self.risk_model, "engine not fitted"
        if not rows:
            return []
        X = np.array([feature_vector(*r[:7]) for r in rows], dtype=float)
        with metrics.timer("prediction_batch"):
            days = self.eta_model.predict(X)
            risk = self.risk_model.predict_proba(X)[:, 1]
        q90 = float(np.quantile(self.residuals, 0.9))
        out = []
        for r, d, k in zip(rows, days, risk):
            rel = self.table.reliability(r[0], r[1], r[2])
            lr = float(np.clip(0.7 * k + 0.3 * (1 - rel), 0.0, 1.0))
            sla = r[7] if len(r) > 7 else None
            sr = None if sla is None else self._sla_risk(lr, r[6], sla)
            out.append(Prediction(round(max(float(d), 0.5), 2), round(lr, 4), round(rel, 4),
                                  None if sr is None else round(sr, 4), round(float(d) + q90, 2)))
        return out

    def predict(self, carrier_id: str, origin: str, dest: str, service: str, month: int, weight: float,
                promised: float, sla_days: float | None = None) -> Prediction:
        assert self.eta_model and self.risk_model, "engine not fitted"
        x = np.array([feature_vector(carrier_id, origin, dest, service, month, weight, promised)], dtype=float)
        with metrics.timer("prediction"):
            days = float(self.eta_model.predict(x)[0])
            risk = float(self.risk_model.predict_proba(x)[0, 1])
        rel_route = self.table.reliability(carrier_id, origin, dest)
        # Blend the model's risk with the empirical route reliability for stability on sparse routes.
        late_risk = float(np.clip(0.7 * risk + 0.3 * (1 - rel_route), 0.0, 1.0))
        sla_risk = None
        if sla_days is not None:
            sla_risk = self._sla_risk(late_risk, promised, sla_days)
        p90 = float(days + np.quantile(self.residuals, 0.9))
        return Prediction(round(max(days, 0.5), 2), round(late_risk, 4), round(rel_route, 4),
                          None if sla_risk is None else round(sla_risk, 4), round(p90, 2))

