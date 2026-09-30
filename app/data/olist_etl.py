"""Olist ETL: raw CSVs -> clean order-level frame -> calibration artifact (build time only; needs pandas).

Cleaning rules (every drop is counted in the quality report, nothing is silently discarded):
  * only `delivered` orders with a customer-delivery timestamp
  * single-seller orders only (the origin of a multi-seller order is ambiguous)
  * delivery must not precede purchase; a carrier hand-off timestamp outside [purchase, delivery] is ignored
  * parcel weight/volume = sum over item rows (one row per unit); missing/zero values are filled with the category
    median, then the global median, and the fill rate is reported
  * one review per order (the latest answered)
  * "late" uses the calendar-day definition (delivered date > estimated date), because the estimate is a date at 00:00
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.core.geo import STATES, haversine_km, region
from app.data import calibration as cal

ETL_VERSION = "1.0"
FILES = {
    "orders": "olist_orders_dataset.csv", "items": "olist_order_items_dataset.csv", "products": "olist_products_dataset.csv",
    "sellers": "olist_sellers_dataset.csv", "customers": "olist_customers_dataset.csv", "reviews": "olist_order_reviews_dataset.csv",
    "payments": "olist_order_payments_dataset.csv", "geo": "olist_geolocation_dataset.csv",
    "translation": "product_category_name_translation.csv",
}
BRAZIL_LAT, BRAZIL_LNG = (-34.0, 6.0), (-74.0, -34.0)
SAMPLE_SIZE = 20000
MIN_ROUTE_N = 5


class DataError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_raw(data_dir: str | Path) -> dict[str, pd.DataFrame]:
    d = Path(data_dir)
    missing = [n for n in FILES.values() if not (d / n).exists()]
    if missing:
        raise DataError(f"missing files in {d}: {missing}")
    ts = ["order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
          "order_delivered_customer_date", "order_estimated_delivery_date"]
    raw = {k: pd.read_csv(d / v) for k, v in FILES.items() if k != "orders"}
    raw["orders"] = pd.read_csv(d / FILES["orders"], parse_dates=ts)
    raw["reviews"]["review_answer_timestamp"] = pd.to_datetime(raw["reviews"]["review_answer_timestamp"], errors="coerce")
    return raw


def zip_centroids(geo: pd.DataFrame) -> pd.DataFrame:
    """Median lat/lng per zip prefix, ignoring coordinates outside Brazil (the raw file contains stray points)."""
    g = geo[geo.geolocation_lat.between(*BRAZIL_LAT) & geo.geolocation_lng.between(*BRAZIL_LNG)]
    return g.groupby("geolocation_zip_code_prefix")[["geolocation_lat", "geolocation_lng"]].median()


def _hav(lat1, lng1, lat2, lng2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def build_orders(raw: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, dict]:
    o, it, p, s, c = raw["orders"], raw["items"], raw["products"], raw["sellers"], raw["customers"]
    q: dict = {"orders_raw": int(len(o)), "status_counts": {k: int(v) for k, v in o.order_status.value_counts().items()}}

    tr = raw["translation"].set_index("product_category_name").product_category_name_english
    p = p.assign(category=p.product_category_name.map(tr).fillna(p.product_category_name).fillna("unknown"),
                 w=p.product_weight_g.where(p.product_weight_g > 0),
                 vol=(p.product_length_cm * p.product_height_cm * p.product_width_cm).where(lambda x: x > 0))
    q["products_missing_weight"] = int(p.w.isna().sum())
    q["products_missing_volume"] = int(p.vol.isna().sum())
    p["w"] = p.w.fillna(p.groupby("category").w.transform("median")).fillna(p.w.median())
    p["vol"] = p.vol.fillna(p.groupby("category").vol.transform("median")).fillna(p.vol.median())

    it = it.merge(p[["product_id", "category", "w", "vol"]], on="product_id", how="left") \
           .merge(s[["seller_id", "seller_state", "seller_zip_code_prefix"]], on="seller_id", how="left")
    g = it.groupby("order_id").agg(n_items=("order_item_id", "size"), n_sellers=("seller_id", "nunique"),
                                   seller_id=("seller_id", "first"), seller_state=("seller_state", "first"), seller_zip=("seller_zip_code_prefix", "first"),
                                   price=("price", "sum"), freight=("freight_value", "sum"), weight_g=("w", "sum"),
                                   volume_cm3=("vol", "sum"), category=("category", "first")).reset_index()
    q["orders_with_items"] = int(len(g))
    q["multi_seller_dropped"] = int((g.n_sellers > 1).sum())

    d = o.merge(g[g.n_sellers == 1], on="order_id").merge(
        c[["customer_id", "customer_state", "customer_zip_code_prefix"]], on="customer_id")
    q["not_delivered_dropped"] = int((d.order_status != "delivered").sum() + (d.order_delivered_customer_date.isna() & (d.order_status == "delivered")).sum())
    d = d[(d.order_status == "delivered") & d.order_delivered_customer_date.notna()].copy()

    day = 86400.0
    d["total_days"] = (d.order_delivered_customer_date - d.order_purchase_timestamp).dt.total_seconds() / day
    neg = d.total_days <= 0
    q["non_positive_duration_dropped"] = int(neg.sum())
    d = d[~neg].copy()
    bad_state = ~(d.seller_state.isin(STATES) & d.customer_state.isin(STATES))
    q["unknown_state_dropped"] = int(bad_state.sum())
    d = d[~bad_state].copy()

    d["promised_days"] = (d.order_estimated_delivery_date - d.order_purchase_timestamp).dt.total_seconds() / day
    d["late"] = (d.order_delivered_customer_date.dt.normalize() > d.order_estimated_delivery_date.dt.normalize()).astype(int)
    hand = (d.order_delivered_carrier_date - d.order_purchase_timestamp).dt.total_seconds() / day
    trans = (d.order_delivered_customer_date - d.order_delivered_carrier_date).dt.total_seconds() / day
    ok = d.order_delivered_carrier_date.notna() & (hand >= 0) & (trans > 0)
    q["carrier_timestamp_unusable"] = int((~ok).sum())
    d["handling_days"], d["transit_days"] = hand.where(ok), trans.where(ok)
    d["month"] = d.order_purchase_timestamp.dt.month
    d["ym"] = d.order_purchase_timestamp.dt.strftime("%Y-%m")
    d["weight_kg"] = d.weight_g / 1000.0

    # geography: real zip-prefix distance
    zc = zip_centroids(raw["geo"])
    q["zip_prefixes_geocoded"] = int(len(zc))
    a = zc.reindex(d.seller_zip.values).to_numpy()
    b = zc.reindex(d.customer_zip_code_prefix.values).to_numpy()
    d["km"] = _hav(a[:, 0], a[:, 1], b[:, 0], b[:, 1])
    q["orders_without_zip_distance"] = int(d.km.isna().sum())

    # review: latest answered per order
    rv = raw["reviews"].sort_values("review_answer_timestamp").drop_duplicates("order_id", keep="last")[["order_id", "review_score"]]
    q["orders_with_review"] = int(d.order_id.isin(rv.order_id).sum())
    d = d.merge(rv, on="order_id", how="left")

    # payments reconcile against items (price + freight)
    pay = raw["payments"].groupby("order_id").payment_value.sum().rename("paid")
    d = d.merge(pay, on="order_id", how="left")
    diff = (d.paid - (d.price + d.freight)).abs()
    q["payment_reconciles_pct"] = round(100 * float((diff < 1.0).mean()), 2)
    q["orders_analysed"] = int(len(d))
    return d.reset_index(drop=True), q


# ------------------------------------------------------------------------------------------ calibration
def _route_table(d: pd.DataFrame, keys: list[str]) -> dict:
    out = {}
    for k, g in d.groupby(keys):
        key = ">".join(k)
        out[key] = {
            "n": int(len(g)), "km": _r(g.km.median(), 1), "transit_p50": _r(g.transit_days.median()),
            "transit_p90": _r(g.transit_days.quantile(0.9)), "total_p50": _r(g.total_days.median()),
            "promised_p50": _r(g.promised_days.median()), "late": _r(g.late.mean(), 4), "freight_p50": _r(g.freight.median()),
            "freight_mean": _r(g.freight.mean()),
        }
    return out


def _r(v, nd: int = 2) -> Optional[float]:
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), nd)


def _weights(series: pd.Series) -> dict:
    vc = (series.value_counts(normalize=True) * 100).round(3)
    return {k: float(v) for k, v in vc.items() if v > 0}


def derive(d: pd.DataFrame, quality: dict, file_meta: dict) -> dict:
    d = d.assign(region_o=d.seller_state.map(region), region_d=d.customer_state.map(region))
    n = len(d)
    late_rate = float(d.late.mean())
    cross = d.seller_state != d.customer_state
    item_frt = d.freight / d.n_items
    lo, hi = float(d.weight_kg.quantile(0.005)), float(d.weight_kg.quantile(0.995))
    w = d.weight_kg.clip(lo, hi)
    mu, sigma = float(np.log(w).mean()), float(np.log(w).std())

    # month-of-year lateness above the network mean (observed, includes Nov-2017 and Feb/Mar-2018 stress)
    m = d.groupby("month").agg(n=("late", "size"), late=("late", "mean"), total_p50=("total_days", "median"))
    monthly = {str(k): {"n": int(r.n), "late": round(float(r.late), 4), "total_p50": round(float(r.total_p50), 2)} for k, r in m.iterrows()}
    month_penalty = {str(k): round(float(min(0.15, max(0.0, r.late - late_rate))), 4) if r.n >= 500 else 0.0 for k, r in m.iterrows()}
    tl = d.groupby("ym").agg(n=("late", "size"), late=("late", "mean"), total_p50=("total_days", "median"))
    timeline = [{"ym": k, "n": int(r.n), "late": round(float(r.late), 4), "total_p50": round(float(r.total_p50), 2)}
                for k, r in tl.iterrows() if r.n >= 200]

    rv = d.dropna(subset=["review_score"])
    review = {"on_time": float(rv[rv.late == 0].review_score.mean()), "late": float(rv[rv.late == 1].review_score.mean()),
              "n_on_time": int((rv.late == 0).sum()), "n_late": int((rv.late == 1).sum())}

    states = {}
    for st, g in d.groupby("customer_state"):
        states[st] = {"orders": int(len(g)), "late": _r(g.late.mean(), 4), "total_p50": _r(g.total_days.median()),
                      "transit_p50": _r(g.transit_days.median()), "freight_mean": _r(g.freight.mean())}
    cats = d.groupby("category").agg(n=("late", "size"), late=("late", "mean"), freight=("freight", "mean"), weight_p50=("weight_kg", "median"))
    cats = cats.sort_values("n", ascending=False).head(15)
    categories = {k: {"n": int(r.n), "share": round(100 * r.n / n, 2), "late": round(float(r.late), 4),
                      "freight_mean": round(float(r.freight), 2), "weight_p50": round(float(r.weight_p50), 3)} for k, r in cats.iterrows()}

    network = {
        "late_rate": round(late_rate, 4), "transit_p50": _r(d.transit_days.median()), "transit_p90": _r(d.transit_days.quantile(0.9)),
        "total_p50": _r(d.total_days.median()), "total_p90": _r(d.total_days.quantile(0.9)), "promised_p50": _r(d.promised_days.median()),
        "handling_days_p50": _r(d.handling_days.median()),
        "sla_padding_ratio": _r(d.promised_days.median() / d.total_days.median()),
        "delivered_10d_early_share": _r(float(((d.promised_days - d.total_days) >= 10).mean()), 4),
        "p90_total_within_promise_share": _r(float((d.total_days.quantile(0.9) <= d.promised_days).mean()), 4),
    }
    headline = {
        "orders_analysed": n, "date_from": str(d.order_purchase_timestamp.min().date()), "date_to": str(d.order_purchase_timestamp.max().date()),
        "sellers": int(d.seller_id.nunique()) if "seller_id" in d else None,
        "cross_state_share": round(float(cross.mean()), 4),
        "freight_premium_order_pct": round(100 * float(d[cross].freight.mean() / d[~cross].freight.mean() - 1), 1),
        "freight_premium_item_pct": round(100 * float(item_frt[cross].mean() / item_frt[~cross].mean() - 1), 1),
        "avg_freight": round(float(d.freight.mean()), 2), "avg_order_value": round(float(d.price.mean()), 2),
        "freight_share_of_value_pct": round(100 * float(d.freight.sum() / d.price.sum()), 1),
        "late_rate": round(late_rate, 4), "late_days_median": _r(float((d.order_delivered_customer_date - d.order_estimated_delivery_date)
                                                                    .dt.days[d.late == 1].median())),
        "avg_review": round(float(rv.review_score.mean()), 2),
    }
    return {
        "meta": {"etl_version": ETL_VERSION, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source_files": file_meta, "quality": quality, "late_definition": "delivered date > estimated date (calendar day)",
                 "min_route_n": MIN_ROUTE_N},
        "headline": headline, "network": network,
        "customer_weights": _weights(d.customer_state), "seller_weights": _weights(d.seller_state),
        "routes": _route_table(d, ["seller_state", "customer_state"]), "region_pairs": _route_table(d, ["region_o", "region_d"]),
        "monthly": monthly, "month_penalty": month_penalty, "timeline": timeline, "review": review,
        "states": states, "categories": categories,
        "weight_kg": {"mu": round(mu, 4), "sigma": round(sigma, 4), "p50": _r(d.weight_kg.median(), 3), "p90": _r(d.weight_kg.quantile(0.9), 3)},
        "freight_scale": {},
    }


def sample_arrays(d: pd.DataFrame, n: int = SAMPLE_SIZE, seed: int = 42) -> dict:
    s = d.sample(min(n, len(d)), random_state=seed)
    states = sorted(STATES)
    cats = sorted(d.category.unique())
    si, ci = {k: i for i, k in enumerate(states)}, {k: i for i, k in enumerate(cats)}
    return {"states": states, "categories": cats, "n_source_orders": int(len(d)), "seed": seed,
            "seller_state": [si[x] for x in s.seller_state], "customer_state": [si[x] for x in s.customer_state],
            "month": [int(x) for x in s.month], "weight_kg": [round(float(x), 3) for x in s.weight_kg.clip(0.05, 100)],
            "volume_cm3": [int(round(x)) for x in s.volume_cm3.clip(100, 2_000_000)], "price": [round(float(x), 2) for x in s.price],
            "freight": [round(float(x), 2) for x in s.freight], "category": [ci[x] for x in s.category],
            "promised_days": [round(float(x), 1) for x in s.promised_days]}


def fit_freight_scale(artifact: dict, d: pd.DataFrame, n: int = 4000, seed: int = 3) -> dict:
    """Scale simulated carrier tariffs so that, per destination region, the median simulated price equals the real
    median freight of comparable orders (same route, weight and volume). Computed against the *uncalibrated* tariff."""
    from app.integration import world
    c = cal.Calibration(artifact)
    s = d.sample(min(n, len(d)), random_state=seed)
    ratios: dict[str, list[float]] = {}
    with cal.use(c):
        for r in s.itertuples():
            costs = []
            for p in world.PROFILES:
                if region(r.seller_state) not in p.origin_regions or region(r.customer_state) not in p.regions or r.weight_kg > p.max_weight_kg:
                    continue
                svc = world.ServiceLevel.STANDARD if world.ServiceLevel.STANDARD in p.services else p.services[0]
                costs.append(world.freight_cost(p, r.seller_state, r.customer_state, float(r.weight_kg), float(r.volume_cm3), svc, calibrated=False))
            if costs and r.freight > 0:
                ratios.setdefault(region(r.customer_state), []).append(r.freight / float(np.mean(costs)))
    return {k: round(float(np.median(v)), 4) for k, v in ratios.items() if len(v) >= 30}


def diagnostics(artifact: dict) -> dict:
    """Self-checks stored with the artifact: does the physics calibration fit inside its sanity bounds, and how far
    was the *uncalibrated* simulator from the real network? (A large factor means the old simulator was optimistic.)"""
    from app.integration import world
    c = cal.Calibration(artifact)
    facs, ns = [], []
    with cal.use(c):
        for k, r in artifact["routes"].items():
            o, d = k.split(">")
            serving = [q for q in world.PROFILES if region(o) in q.origin_regions and region(d) in q.regions] or world.PROFILES
            ref = float(np.mean([world._raw_transit(q, o, d, world.ServiceLevel.STANDARD if world.ServiceLevel.STANDARD in q.services
                                                     else q.services[0]) for q in serving]))
            real = c.route_value(o, d, "transit_p50", region)
            if real is not None:
                facs.append(real / ref)
                ns.append(r["n"])
    f, n = np.array(facs), np.array(ns, dtype=float)
    lo, hi = world.TIME_FACTOR_BOUNDS
    order = np.argsort(f)
    cum = np.cumsum(n[order]) / n.sum()
    return {"time_factor_flow_median": round(float(f[order][np.searchsorted(cum, 0.5)]), 3),
            "time_factor_clipped_flow_share": round(float(n[(f < lo) | (f > hi)].sum() / n.sum()), 4),
            "time_factor_bounds": [lo, hi], "routes_calibrated": int(len(f))}


# ------------------------------------------------------------------------------------------ real-data benchmark
def benchmark(d: pd.DataFrame, split: str = "2018-06-01", seed: int = 7) -> dict:
    """Carrier-agnostic ETA + late-risk models trained on *real* orders, evaluated on a chronological hold-out.
    Baselines: Olist's own delivery estimate (ETA), and historical route late-rate (risk)."""
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.metrics import brier_score_loss, mean_absolute_error, roc_auc_score

    x = d.copy()
    x["km"] = x.km.fillna(pd.Series([haversine_km(a, b) for a, b in zip(x.seller_state, x.customer_state)], index=x.index))
    x["o"] = x.seller_state.astype("category").cat.codes
    x["dst"] = x.customer_state.astype("category").cat.codes
    x["cat"] = x.category.astype("category").cat.codes
    feats = ["o", "dst", "cat", "month", "weight_kg", "volume_cm3", "price", "freight", "n_items", "promised_days", "km"]
    mask = [f in ("o", "dst", "cat") for f in feats]
    cut = pd.Timestamp(split)
    tr, te = x[x.order_purchase_timestamp < cut], x[x.order_purchase_timestamp >= cut]
    if len(te) < 500 or len(tr) < 5000:
        raise DataError(f"chronological split {split} leaves too little data (train={len(tr)}, test={len(te)})")
    clip = float(tr.total_days.quantile(0.995))
    reg = HistGradientBoostingRegressor(loss="absolute_error", categorical_features=mask, max_iter=250, learning_rate=0.06, random_state=seed)
    reg.fit(tr[feats], tr.total_days.clip(upper=clip))
    clf = HistGradientBoostingClassifier(categorical_features=mask, max_iter=200, learning_rate=0.05, random_state=seed)
    clf.fit(tr[feats], tr.late)
    eta, prob = reg.predict(te[feats]), clf.predict_proba(te[feats])[:, 1]

    g = tr.groupby(["seller_state", "customer_state"]).late.agg(["sum", "count"])
    base = float(tr.late.mean())
    lk = te.join(g, on=["seller_state", "customer_state"])
    route_p = ((lk["sum"].fillna(0) + 20 * base) / (lk["count"].fillna(0) + 20)).to_numpy()
    top = np.argsort(-prob)[: max(1, len(prob) // 10)]
    y = te.late.to_numpy()
    return {
        "split": {"train_until": split, "n_train": int(len(tr)), "n_test": int(len(te)), "type": "chronological",
                  "test_from": str(te.order_purchase_timestamp.min().date()), "test_to": str(te.order_purchase_timestamp.max().date())},
        "eta": {"mae_days_model": round(float(mean_absolute_error(te.total_days, eta)), 2),
                "mae_days_olist_estimate": round(float(mean_absolute_error(te.total_days, te.promised_days)), 2),
                "mae_days_train_median": round(float(mean_absolute_error(te.total_days, np.full(len(te), tr.total_days.median()))), 2),
                "olist_estimate_bias_days": round(float((te.promised_days - te.total_days).mean()), 2)},
        "late": {"base_rate_train": round(base, 4), "base_rate_test": round(float(y.mean()), 4),
                 "auc_model": round(float(roc_auc_score(y, prob)), 3), "auc_route_history": round(float(roc_auc_score(y, route_p)), 3),
                 "brier_model": round(float(brier_score_loss(y, prob)), 4), "brier_constant": round(float(brier_score_loss(y, np.full(len(y), base))), 4),
                 "top_decile_late_rate": round(float(y[top].mean()), 4), "top_decile_lift": round(float(y[top].mean() / max(y.mean(), 1e-9)), 2)},
        "features": feats,
        "note": "Real Olist orders, carrier-agnostic (the dataset has no carrier id). Measures how predictable marketplace delivery outcomes are; "
                "carrier-level accuracy cannot be measured from this data.",
    }


# ------------------------------------------------------------------------------------------ orchestration
def build_artifact(data_dir: str | Path, out_dir: str | Path, run_benchmark: bool = True) -> dict:
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    raw = load_raw(data_dir)
    file_meta = {k: {"file": v, "rows": int(len(raw[k])), "sha256": _sha256(data_dir / v)} for k, v in FILES.items()}
    d, quality = build_orders(raw)
    if len(d) < 10000:
        raise DataError(f"only {len(d)} usable orders; this does not look like the Olist dataset")
    art = derive(d, quality, file_meta)
    art["freight_scale"] = fit_freight_scale(art, d)
    art["diagnostics"] = diagnostics(art)
    if run_benchmark:
        art["benchmark"] = benchmark(d)
    art["meta"]["checksum"] = cal.checksum(art)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "calibration.json").write_text(json.dumps(art, indent=1, sort_keys=True), encoding="utf-8")
    with gzip.open(out_dir / cal.SAMPLE_NAME, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(sample_arrays(d), f, separators=(",", ":"))
    return art
