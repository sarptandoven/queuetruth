# The learned fill model

The zoo models are hand-written rules. The learned model asks a different question: given only what an L2 feed shows
at some moment, what is the probability that a resting order fills (or is cancelled) within each horizon? It is
trained on L2 features synthesized from order-level data, with labels from the exact replay, so it can then be
applied where you only have L2.

## Population and labels

`queuetruth features` builds one row per (order, landmark):

- **Orders.** Either shadows (`--shadows`; life starts at `ts_live`) or real resting orders from the MBO data
  (`--population real-orders`; life starts at the real add; orders inside a snapshot are excluded because their
  queue position is unknown).
- **Landmarks.** Times after the start of the order's life at which it is still resting and unfilled: 0, 1 s and 5 s
  by default (`--landmarks`). Later landmarks let the model use what happened since entry.
- **Outcome.** Time from the landmark to the first of three causes, capped at `h_max` (60 s):
  - `fill`: the exact first fill;
  - `cancel`: for real orders, the real cancel or replace; for shadows, a policy cancel at the first event where the
    same-side best has moved `cancel_away_ticks` (2) or more ticks better than the order's price;
  - `censor`: anything else, including reaching `h_max`.

## Features

Every feature uses only the L2 view and L2 trades up to the landmark. A test deletes every input record after the
landmark and checks that the features do not change (O-FEAT-1). `s = +1` for bids, `-1` for asks.

| Group | Features |
|---|---|
| Position | `dist_ticks = s (best_same - price) / tick`, `spread_ticks`, `level_qty`, `level_ct`, `own_qty_lots`, `side_sign` |
| Queue trackers | `f_hbt_risk_adverse`, `f_hbt_power3_n1`, `f_hbt_power3_n3`, `f_hbt_log2` (zoo estimates of queue ahead run on the same L2 rows since arrival), `queue_share_ra = f_hbt_risk_adverse / (level_qty + own_qty)` |
| Since entry | `elapsed_ns`, `traded_at_px_since_entry`, `unexplained_dec_since_entry = sum over batches of max(0, prev - new - trades)` |
| Book shape | `imb_l1 = s (Qb1 - Qa1) / (Qb1 + Qa1)`, `imb_l3`, `wmid_offset_ticks = s (wmid - mid) / tick` with `wmid = (Qb1 Pa1 + Qa1 Pb1) / (Qb1 + Qa1)`, `same_sz_l1..l3`, `opp_sz_l1..l3` |
| Flow | `ofi_1s`, `ofi_10s` (Cont-Kukanov-Stoikov order-flow imbalance, signed, scaled by mean top-of-book size), `trade_vol_px_10s`, `trade_vol_side_10s`, `trade_vol_all_60s`, `dec_rate_px_10s`, `vol_ticks_60s`, `bbo_changes_10s` |
| Time and scale | `tod_sin`, `tod_cos` (position within the session), `log_mid_over_tick` |
| Privileged (exact, optional) | `p_f_exact` (exact queue ahead), `p_rank_exact`, `p_level_qty_exact` |

## Model

Discrete-time competing risks on a grid `tau_1..tau_J` (default 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60 s). Each
(order, landmark) row is expanded into one row per interval it survives into, with target 0 (still resting), 1
(filled in this interval) or 2 (cancelled in this interval); a row censored inside an interval stops without a
target, and a row censored at the end of an interval stays in that interval's risk set. A gradient-boosted
classifier (scikit-learn `HistGradientBoostingClassifier`, 3 classes, fixed seed) on those rows gives per-interval
hazards `h_f(j | x)` and `h_c(j | x)`:

```text
S(tau_j)        = prod_{l <= j} (1 - h_f(l) - h_c(l))
CIF_fill(tau_j) = sum_{l <= j} h_f(l) S(tau_{l-1})
```

`CIF_fill(tau)` is the probability of a fill within `tau`, accounting for the chance of being cancelled first. It is
monotone in `tau` by construction (O-MOD-2).

**Recalibration.** On a separate calibration split, an isotonic regression per horizon maps raw `CIF_fill` to the
observed fill frequency, with inverse-probability-of-censoring weights `1 / max(G(min(e, tau)-), 0.05)` (`G` is the
Kaplan-Meier survival of censoring). The result is forced monotone across horizons and clipped to [0, 1]; the cancel
incidence is scaled down only where fill plus cancel would exceed 1.

## Splits

Splits are by order (all landmarks of an order stay together) and by time (`--split auto`):

- 5 or more days: the first 60% of days train, the next 20% calibrate, the rest test.
- 2 to 4 days: the last day tests; within earlier days, the last of `--n-blocks` (5) equal blocks calibrates.
- 1 day: blocks within the day (train, then calibrate, then test).

Rows whose outcome window reaches into the next split are purged, and calibration and test rows within the embargo
(`--embargo`, 60 s) of their split's start are dropped. A check asserts that no overlap remains (O-MOD-4).

## Scoring

On the test split, proper scoring rules first:

| Score | Definition |
|---|---|
| `rcll` (primary) | Mean of `-ln P(observed outcome)` from the raw hazards: `h_c(K) S(tau_{K-1})` for an event of cause `c` in interval `K`, `S(tau_m)` if censored at `e` with `m` the last grid point at or before `e`, `S(tau_{K-1})` if censored inside `K`. |
| `ipcw_logloss(tau)` (primary) | `-mean[ 1{e <= tau, fill} ln F_fill(tau) / G(e) + 1{e <= tau, cancel} ln F_cancel(tau) / G(e) + 1{e > tau} ln S(tau) / G(tau) ]` |
| IPCW Brier, `ibs` | `BS(tau) = mean w (1{e <= tau, fill} - F_fill(tau))^2` with `w = 1/G(e)` for an event by `tau`, `1/G(tau)` if still resting, 0 if censored before `tau`; `ibs` integrates over the grid. |
| `bss_vs_climatology` | `1 - BS_model / BS_clim`, where climatology is the training-window Aalen-Johansen fill curve by instrument x side x distance bucket x 30-minute bucket. CI by day-block bootstrap of test orders. |
| Calibration | slope, intercept, Spiegelhalter z, Hosmer-Lemeshow p (10 bins; v0.1 uses 8 degrees of freedom), reliability diagrams with Wilson intervals. |
| Discrimination | Uno's C at 10 s and AUC per horizon, labelled improper and never used for model selection. |

Model selection uses only `rcll` and `ipcw_logloss`. When zoo predictions for the same shadows are supplied
(`fit-hazard` spec field `zoo_predictions`), their Brier score on the same test rows is reported next to the learned
model's (`zoo_comparison`).

## Two models: L2-only and privileged

`fit-hazard` trains on L2 features only. `fit-hazard --privileged` adds the exact queue columns. The privileged model is
not usable in production (an L2 backtester does not know exact queue ahead); it is an upper bound. The gap between the
two measures what L2 data cannot tell you about fills on that venue.

## Using it

`fit-hazard` writes `model.json` (the model card, `qt.hazard_model_card`: spec, splits, scores, warnings), `model.pkl`
and `predictions.parquet`. Unpickling runs code, so loading needs an authenticity check you supply:

```python
from queuetruth.model.hazard import HazardModel, pickle_sha256

card = qt.fit_hazard(spec)                 # the card fit_hazard returned
digest = pickle_sha256(card)               # sha256 of model.pkl: keep it where the model directory's writer cannot change it

model = HazardModel.load("model-dir", sha256=digest)
preds = model.predict(features)            # a qt.hazard_predictions table
```

`HazardModel.load(path, *, sha256=None, trusted=False)` refuses to unpickle without `sha256` (`QT-CONFIG-001`)
unless you pass `trusted=True` for a directory only you can write. A pickle whose bytes differ from the digest, or
from the `model.json` beside it, is refused with `QT-INPUT-002`; the `model.json` check is integrity only, because
whoever can write the pickle can also write the card. The file is read once, and the bytes hashed are the bytes
unpickled. Never load a model directory you received from someone else with `trusted=True`.
