"""Feedback loop: Predict -> Decide -> Execute -> Observe -> Learn.

Every delivered shipment is compared with what the models predicted. Outcomes update the
route-reliability table online (so the *next* decision already benefits) and feed drift KPIs.
"""
from __future__ import annotations

import time

import numpy as np

from app.intelligence.prediction import PredictionEngine
from app.services.store import Store


class FeedbackLoop:
    def __init__(self, store: Store, predictor: PredictionEngine) -> None:
        self.store, self.predictor = store, predictor

    def record(self, shipment: dict, actual_days: float) -> dict:
        late = int(actual_days > shipment["promised_days"])
        self.store.execute(
            "INSERT INTO feedback(shipment_id,carrier_id,origin_state,dest_state,predicted_days,actual_days,promised_days,"
            "predicted_risk,was_late,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (shipment["shipment_id"], shipment["carrier_id"], shipment["seller_state"], shipment["dest_state"],
             shipment["predicted_days"], actual_days, shipment["promised_days"], shipment["late_risk"], late, time.time()))
        # Online learning: update the empirical route reliability immediately.
        self.predictor.table.fit([((shipment["carrier_id"], shipment["seller_state"], shipment["dest_state"]), late)])
        return {"late": bool(late), "eta_error_days": round(actual_days - shipment["predicted_days"], 2)}

    def report(self) -> dict:
        rows = self.store.all("SELECT * FROM feedback")
        if not rows:
            return {"samples": 0}
        pred = np.array([r["predicted_days"] for r in rows])
        act = np.array([r["actual_days"] for r in rows])
        prom = np.array([r["promised_days"] for r in rows])
        risk = np.array([r["predicted_risk"] for r in rows])
        late = np.array([r["was_late"] for r in rows])
        by_carrier = {}
        for cid in sorted({r["carrier_id"] for r in rows}):
            m = np.array([r["carrier_id"] == cid for r in rows])
            by_carrier[cid] = {"n": int(m.sum()), "eta_bias_days": round(float((act[m] - pred[m]).mean()), 2),
                               "late_rate": round(float(late[m].mean()), 3), "avg_predicted_risk": round(float(risk[m].mean()), 3)}
        return {
            "samples": len(rows),
            "eta_mae_days": round(float(np.abs(act - pred).mean()), 3),
            "carrier_promise_mae_days": round(float(np.abs(act - prom).mean()), 3),
            "predicted_late_rate": round(float(risk.mean()), 3),
            "actual_late_rate": round(float(late.mean()), 3),
            "calibration_gap_pts": round(float((risk.mean() - late.mean()) * 100), 2),
            "brier": round(float(np.mean((risk - late) ** 2)), 4),
            "by_carrier": by_carrier,
        }
