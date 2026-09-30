# Olist Flow — Intelligent Logistics Optimization & Control Tower

> A real-time logistics decision engine that selects, monitors, and re-optimizes the best fulfillment and carrier plan for every order.

**Machine learning predicts outcomes. Optimization selects actions.**
Olist Flow does not list carriers and prices (that is a comparison tool). For every order it answers the harder question:
*"Given the current state of the network, what is the best logistics decision — and why?"* — and it re-decides when conditions change.

## Why this exists (evidence from the Olist data)

| Finding (`HVIA_Olist_Business_Discovery.ipynb`) | What Olist Flow does about it |
|---|---|
| 64% of shipments cross state lines, at ~75% higher freight (measured, see below) | Optimises **origin + carrier + service level** together (multi-node inventory), not just the carrier |
| Late deliveries: review score 4.31 → 2.27 | Predicts per-route late risk, treats the customer SLA as a constraint, escalates and re-plans **before** the customer complains |
| Lateness bursts (Black Friday 2017 **and** Feb–Mar 2018) | `peak_season` policy + capacity-aware batch MILP + what-if stress tests, with the observed months as presets |

## Real data integration

The nine Olist CSVs are distilled by `scripts/build_calibration.py` into a small, checksummed artifact (`app/data/artifacts/`, ~330 KB) that the service loads at start. The raw files (~120 MB) are **not** needed at runtime and are not committed. Without the artifact the platform falls back to its original synthetic physics and says so (`/v1/data/evidence` → `"active": false`).

```bash
pip install -r requirements-data.txt
python scripts/build_calibration.py --data-dir path/to/olist_csvs     # ~15 s → artifact + docs/DATA_CARD.md
```

| Real measurement (95,195 delivered single-seller orders, 2016-09-15 → 2018-08-29) | Value |
|---|---|
| Orders crossing a state line | **64.0%** |
| Freight premium, cross-state vs same-state | **+74.9%** per order (+76.8% per item) |
| Late vs the customer promise (calendar-day definition) | **6.9%**, median 7 days late when late |
| Review score, on-time → late | **4.31 → 2.27** |
| Promised delivery vs median actual | 23.22 d vs 10.25 d (the promise is **2.26×** the median) |
| Seller handling / carrier transit (median) | 2.21 d / 7.11 d (p90 18.96 d) |

**How it is used:** real origin/destination shares; a 20,000-order bootstrap of real orders (joint state pair, month, weight, volume, value, category, promise) feeds model training, the Digital Twin and demo replay; real per-route carrier-transit medians, route late-risk and zip-to-zip distances shape every simulated carrier; observed monthly lateness replaces the hand-made peak penalty; carrier tariffs are scaled per destination region to the observed Olist freight; the review penalty is measured. *Operations → Seed demo data* now replays **real Olist orders** through the real decide → book → track pipeline (`source=synthetic` keeps the old generator). The **Data & Evidence** tab shows all of this with its provenance.

**What the real data changed (honest before/after, Digital Twin, 1,000 orders):** the old synthetic physics was optimistic. Real Olist routes are much slower than the simulator assumed (the flow-weighted median route needed a **2.569×** transit correction), so the Balanced baseline's average delivery moved from 1.94 d to 4.78 d, and "always cheapest" now costs **+3.72 d** instead of +1.14 d. Two findings were invisible before: lateness is *bursty* (Nov-2017 and Feb–Mar 2018, not just Black Friday) and the customer promise is padded 2.26×.

### Real-data benchmark (what real orders can and cannot tell us)

Carrier-agnostic models trained on real orders before 2018-06-01 (76,908) and scored on 2018-06-01 → 2018-08-29 (18,287), a chronological hold-out:

| Metric | Model | Baseline |
|---|---|---|
| ETA mean absolute error | **3.49 d** | 12.62 d (Olist's own estimate) · 4.97 d (constant median) |
| Late-risk ROC-AUC | 0.655 | 0.478 (route history) · 0.50 (chance) |
| Brier score | 0.0366 | 0.0369 (constant) |
| Late rate in the riskiest decile | 7.7% (2.1× lift) | 3.7% overall |

ETA is the strong, reliable result: Olist's estimate is 12.07 days too conservative on the hold-out. **Late-risk is weak on real orders**: AUC 0.655, and the Brier score barely beats a constant. Route history is *below chance* (0.478) because the Feb–Mar 2018 burst that dominates the training window does not repeat. Olist has no carrier id, so carrier-level accuracy cannot be measured from this data at all.

## Quick start (no Docker)

```bash
pip install -r requirements.txt
python -m app.main            # http://localhost:8000   (models train in ~1 s on start)
python -m pytest -q           # 65 tests
```

Open <http://localhost:8000> and sign in (development accounts, password `olistflow-dev`):
`admin` · `ops` (Operations Manager) · `analyst` · `seller` (scoped to seller `S001`).
Then *Operations → Seed demo data* and open **Control Tower**, or walk through **Demo Story**.
API docs: <http://localhost:8000/docs>. In production set `OLISTFLOW_ENV=production` and a real `OLISTFLOW_JWT_SECRET` (the server refuses to start otherwise); see `.env.example`.

## Architecture

```
                 ┌──────────────────────── Control Tower UI (static, CSP-safe) ────────────────────────┐
                 │  Tower · Order Decision · Demo Story · What-If · Shipments · Operations · Data · Audit │
                 └───────────────────────────────────────┬─────────────────────────────────────────────┘
                                                         │ REST (JWT · RBAC · rate limit · request-id)
 ┌───────────────────────────────────────────────────────▼─────────────────────────────────────────────┐
 │ FlowEngine (services/engine.py)    Predict → Decide → Execute → Observe → Learn                     │
 │  decide · book (idempotent, fallback chain, retry queue) · tick/monitor · reoptimize · disrupt      │
 ├────────────────────────────┬───────────────────────────────┬────────────────────────────────────────┤
 │ Intelligence layer         │ Integration layer             │ Platform                               │
 │  Coverage Engine           │  CarrierAdapter (unified)     │  SQLite store (swap → PostgreSQL)      │
 │  Prediction Engine         │   quote·coverage·book·cancel· │  Event bus (in-proc; Kafka/Rabbit seam)│
 │   ETA · late risk ·        │   label·track                 │  Hash-chained audit log                │
 │   route reliability        │  Resilience: timeout · retry  │  Feedback loop (online learning)       │
 │  Optimization Engine       │   · circuit breaker · queue   │  Metrics (Prometheus text) · JSON logs │
 │   weighted + Pareto + MILP │  7 simulated carriers with    │  Digital Twin / What-If simulator      │
 │  Explainability · Exceptions│  route-specific performance  │                                        │
 └────────────────────────────┴───────────────────────────────┴────────────────────────────────────────┘
```

Layout: `app/integration` (adapters, resilience, simulated world) · `app/intelligence` (coverage, prediction, optimizer, explain, exceptions) · `app/services` (engine, store, events, feedback, twin, analytics, seed) · `app/data` (real-data ETL, calibration, evidence) · `app/api` (routes, security) · `app/ui` (dashboard) · `tests/`.

## What each part does

- **Coverage Engine** removes infeasible plans *before* optimisation (pickup area, service area, weight, restricted goods, capacity, SLA, carrier health) and keeps every rejection reason → *"7 carriers connected → 6 serve this route → 5 meet SLA → 51 feasible plans"*.
- **Prediction Engine** (gradient boosting): ETA regression, late-risk classifier, and **route-specific** reliability (empirical-Bayes shrinkage `carrier × state→state → region pair → carrier`). Customer-SLA risk is derived from the empirical shape of delays. It learns online from delivered shipments.
- **Optimization Engine** — configurable multi-objective scoring over normalised criteria (cost, ETA, late risk, reliability, capacity) with 5 profiles (Economy, Express, Reliability, Peak Season, Balanced), custom weights, seller priority, an epsilon-constraint on late risk, and Pareto-front tagging. For peak load, a **MILP** (HiGHS via SciPy) assigns a whole batch under shared carrier/origin capacity.
- **Explainable decisions** — head-to-head deltas ("39% cheaper than X, 6 pts lower late risk than Y"), SLA check, capacity, Pareto status, and the list of excluded plans with reasons. Nothing says "AI chose B".
- **Re-optimisation** — a capacity/API/latency disruption re-plans every parcel still with the seller; the replacement is booked *before* the old plan is cancelled, and a cancellation the old carrier can't accept is queued and retried. After pickup, the system escalates instead (you cannot re-plan a parcel in a truck).
- **Exception management** — Normal / Warning / High risk / Critical from: pickup missed, stuck at hub, tracking stalled, ETA exceeded, delivery failed, carrier API down, capacity exceeded, high delay probability → alert operations, notify seller, update customer ETA, recommend intervention, re-optimise.
- **Digital Twin** — synthetic orders through the *same* coverage/prediction/optimiser code, against carrier ground truth: policy what-ifs, carrier outage, 2× volume, Black-Friday peak, a new fulfilment partner in Rio.
- **Enterprise concerns** — see the section below.

## Measured results

Reproduce with `python scripts/measure.py` (calibrated physics; `--physics synthetic` for the original simulator).

> **Read this first.** These metrics are for the *simulated carriers* operating on *real route physics*. They show the pipeline works and how policies trade off. They are **not** real-world carrier accuracy: Olist has no carrier id, and the only fully real accuracy numbers are in the benchmark above.

**Prediction (simulated carriers, hold-out 4,800 shipments)**

| Metric | Model | Baseline |
|---|---|---|
| ETA mean absolute error | **1.377 days** | 1.616 days (carrier's own promise) |
| Late-risk ROC-AUC (vs the carrier's own promise) | 0.743 | 0.50 |
| Late rate in the riskiest decile | 43.1% (**3.1× lift**) | 14.2% overall |

**Latency** (199 real-shaped orders through the full pipeline, 7 carriers × up to 3 origins): decision p50 **12.7 ms** / p95 17.6 ms; a 1,000-order digital-twin run ≈ 2.5 s.

**Policy trade-offs** (Digital Twin, 1,000 orders, vs the Balanced June baseline: R$14.58 avg freight, 8.2% expected late, 4.78 d, estimated review 4.143)

| Scenario | Avg freight | Late rate | Avg delivery | Unserved |
|---|---|---|---|---|
| Naive policy: always the cheapest carrier | -41.7% | +8.4 pts | +3.72 d | 0 |
| Cost priority (Economy mode) | -40.3% | +5.8 pts | +3.30 d | 0 |
| Reliability priority | +5.1% | -0.1 pts | +0.17 d | 0 |
| Top carrier RapidoSul unavailable | -11.2% | +1.8 pts | +1.19 d | 0 |
| 2x order volume | -9.0% | +12.0 pts | +1.49 d | 211 |
| Black-Friday peak (Nov, 2.5x volume, Peak Season mode) | -9.5% | +28.8 pts | +1.32 d | 689 |
| Feb-Mar 2018 disruption replay (real lateness spike, Reliability mode) | +10.4% | +3.1 pts | +0.28 d | 0 |

These are the trade-offs a manager sees *before* changing policy. Scenario rows with a month (Black Friday, Feb–Mar 2018) include the month effect as well as the policy. Avg freight falls in the volume scenarios because scarce fast-carrier capacity forces work onto cheaper, slower carriers: read it together with the late rate and unserved columns. The Twin's freight is for the *chosen* carrier and is not a savings claim against the real Olist average (R$22.47).

## Enterprise quality

| Concern | Implementation |
|---|---|
| Reliability | Per-call timeout, retry with backoff + jitter (transient errors only), circuit breaker (closed/open/half-open), fallback chain across ranked plans, persistent retry queue for bookings and cancellations |
| Idempotency | `Idempotency-Key` required for booking; replays return the original shipment; one active shipment per order (DB unique index); per-order lock; **compensating carrier cancel** if another instance wins a race. Tested with 12 concurrent submits |
| Events | `ShipmentCreated, PickedUp, ArrivedAtHub, OutForDelivery, Delivered, DeliveryFailed, ExceptionRaised, ActionTriggered, PlanReoptimized…`; persisted + fan-out; `Transport` protocol is the Kafka/RabbitMQ seam |
| Observability | JSON logs with correlation id, Prometheus-format `/metrics` (API latency, carrier calls/failures, breaker opens, optimiser latency, booking failures, re-optimisations), `/health/live`, `/health/ready` |
| Security | JWT (HS256, exp, required claims), RBAC (Admin, Operations Manager, Seller, Analyst — sellers see only their own orders/shipments), scrypt password hashing, token-bucket rate limiting (stricter on login), CSP + security headers, secrets from env, refusal to boot in production with the dev secret, no user enumeration by timing |
| Audit | Every decision (policy, weights, selected plan, cost, ETA, risk), booking, re-plan, policy change, disruption and login is recorded in a **hash-chained** log; `/v1/audit/verify` detects any edit |
| Feedback loop | Predicted vs actual ETA/risk per delivered shipment → online reliability update, calibration gap, per-carrier bias (Audit & Models tab) |

KPIs are business KPIs first (freight cost per order, late rate, on-time %, median delivery time, estimated review score, SLA breaches, savings) with technical KPIs beside them (optimisation latency, booking failures, ETA MAE, precision/recall/lift).

## Demo script (2 minutes)

*Demo Story* tab (all steps call the real API): a São Paulo → Rio order with a 4-day SLA → coverage funnel → cheapest vs recommended vs fastest → **why** this plan → booking → *the chosen carrier's capacity becomes unavailable* → automatic re-optimisation to the next best feasible plan. Then open the Control Tower and the What-If tab ("what if cost mattered more?").

## Honest limitations

- **Olist has no carrier identifier, so carrier behaviour is still simulated.** The real data now constrains the *market* those carriers operate in (routes, distances, transit times, route risk, monthly lateness, freight level, parcel mix, customer promises), but carrier prices, speeds, reliability and capacity remain illustrative. The metrics under *Measured results* describe simulated carriers on real route physics, not real-world carrier accuracy; the only fully real accuracy figures are in the benchmark, and there late-risk is weak.
- **The data is historical and partly unrepresentative.** It ends in Oct 2018; the customer promise in it is padded 2.26×; Feb–Mar 2018 was a network-wide disruption, so month penalties for those months are stress scenarios, not a seasonal law. Multi-seller orders (1,278) and undelivered orders are excluded (every drop is counted in `docs/DATA_CARD.md`).
- **The raw CSVs are not bundled** (licence and size); rebuild the artifact with `scripts/build_calibration.py`. The artifact is checksummed and its integrity is shown in the UI, but it is not signed.
- **Carriers are simulated** behind the production adapter contract (`integration/base.py`). A real carrier = one new adapter class; nothing else changes. Carrier capacities and prices are illustrative.
- SQLite is single-writer and the event bus is in-process: fine for one node and a demo; move to PostgreSQL and Kafka/RabbitMQ (interfaces are in place) for multi-instance deployment. The rate limiter is per-process. Login has rate limiting but no account lockout/MFA.
- The digital twin assigns orders sequentially under live capacity (a greedy policy); exact batch optimality is provided by the MILP endpoint (`POST /v1/batch/optimize`).
- Not built: geo-services/maps integration, order/payment system adapters, HTTPS termination (put it behind a reverse proxy), automatic policy tuning.

## Roadmap mapping

Phase 1 (orders, coverage, quotes, weighted optimisation, booking, tracking, dashboard), Phase 2 (ETA/late-risk models, route reliability, alerts), Phase 3 (multi-origin optimisation, capacity constraints, Pareto/MILP, real-time re-optimisation) and the Phase 4 what-if/digital-twin/control-tower items are implemented. Auto policy tuning is the remaining Phase 4 item.

---
**ملخص بالعربي:** منصة بتاخد الـorder، تستبعد الخيارات غير الممكنة، تتوقع الـETA وخطر التأخير لكل route، وبعدين الـoptimizer يختار أفضل (مخزن + شركة شحن + مستوى خدمة) حسب سياسة قابلة للتعديل، ويشرح ليه اختارها، وينفّذ الحجز بشكل آمن ضد التكرار، ولو الظروف اتغيرت (شركة وقعت/كابستي خلصت) بيعيد التخطيط تلقائيًا. دلوقتي المشروع متدرّب ومعايَر على **95 ألف طلب حقيقي من Olist** (المسارات، زمن النقل، التأخير الشهري، سعر الشحن، الأوزان)، وفيه تاب "Data & Evidence" بيعرض الأرقام ومصدرها. أهم حاجة بنقولها بصراحة: داتا Olist مفيهاش اسم شركة الشحن، فشركات الشحن لسه محاكاة فوق فيزياء مسارات حقيقية، ودقة توقع التأخير على الداتا الحقيقية ضعيفة، بينما دقة توقع وقت التوصيل ممتازة مقارنة بتقدير Olist نفسه.
