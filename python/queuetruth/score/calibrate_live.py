"""Calibration of zoo parameters against the user's OWN live fills (`calibrate-live`; for L2-only
venues without exact MBO truth).

Inputs: an L2 tape (or MBO) dataset and a canonical live log. The live orders are scheduled as in the scorer
(`to_schedule`, instruction-sized legs, evaluation window W = [entry, min(cancel instruction, next leg, entry + H,
end of data)]; audit rulings A8, A9). The live event is the order's first fill in W of either liquidity: the zoo's
`p_fill` counts a model fill of any kind (a cross is a fill there too), so a taker-filled order is filled, never
"resting unfilled until the end of W" (Python review F9; orders first filled as taker are counted in `warnings`).
One zoo run evaluates every grid value of the family on every order's first leg: `hbt_<f>[n=e^theta]` (theta = ln n
on the grid), `databento_mc[k]`, `nt_prob_touch[p]` / `nt_queue_l2_prob_touch[p]`. On an MBO dataset the live
orders' own records are removed first (ruling A1, matched per instrument-day within `match_window_ns`); on an
L2-only dataset the user's own resting orders are removed from the visible depth (`score.l2_own`, trader review
E2) with the scorer's helper, else a refusing `trust: own_orders_in_depth` line is written. Live prices and
quantities are converted to the dataset's units first (`score.units`, review F4) and the schedule comes from the
runner's `build_schedule` (keep-priority venues as in the scorer).

Likelihood (discrete-time, censored; the C2 form of SPEC 5.4): horizons `tau_1 < ... < tau_J` = the spec horizons
below H plus H. Live truth per order: `K_i` = interval of the first fill in W (`delta_i = 1`), else the last
interval fully observed before the end of W (`delta_i = 0`; orders with no fully observed interval carry no
information and are skipped). Model survival `S_ij = 1 - p_fill(tau_j)` smoothed as `(S N + 0.5) / (N + 1)` with
`N` = particles for `databento_mc`, 1000 for the analytic NT touch families and 1 for deterministic models;
`h_ij = 1 - S_ij / S_i,j-1`; `l_i = sum_{j<K} ln(1 - h_ij) + delta ln h_iK + (1 - delta) ln(1 - h_iK)`.
The estimate maximizes the train sum over the grid (no refinement: every grid value is one zoo model). Profile CI:
the contiguous grid range with `2 (l_max - l) <= 3.841459` (an open side, at the grid edge, is null). Bootstrap CI:
train orders resampled by day blocks (30-minute blocks with fewer than 5 dates), argmax refit, percentiles.

Split (hftbacktest #332 protocol, chronological): explicit `train`/`test` dates when given; otherwise the first half
of the session dates (at least one) train and the rest test; with a single date, the first half of the orders by
entry time train and the second half test (echoed as blocks 0/2 and 1/2). Held-out (test orders): per horizon Brier,
log loss and fill-rate bias of the fitted and the default model (`n = 2`, `k = -0.8`, `p = 1`), and at H the
order-level 2x2 (model filled = `p_fill(H) >= 0.5`), exact and cluster-robust McNemar and the fill-rate bias with
its block-bootstrap CI (`held_out_discordance`, schema 1.1, and `live_calibration_test.json`). Eligibility at a
horizon is outcome-independent (audit A2, Python review F1): an order whose window W was cut by its own cancel or
next leg before `tau` stays in with label 0 (it cannot fill after its cancel, and the model's shadow is cancelled at
the same time); only a window cut by the END OF DATA before `tau` leaves the order out at that horizon.

Fail-safe (trader review D1, E1): window dates missing from the dataset, an empty train or test window, no train
order with an observed interval and no filled train order are `QT-DATA-001` errors. Grid-edge, plateau, flat and
non-identified likelihoods are reported as `trust:` lines (`queuetruth.export.trust.grid_lines`, the definitions of
`qt_calib::trust`); a bootstrap interval of zero width (every replicate chose one grid value) is suppressed with a
`trust: ci_grid_resolution` line. The held-out comparison with the default is recorded per horizon as
`trust: beats_default` and summarized as `trust: verdict`; `queuetruth export` refuses a refused fit.
The result is a `qt.calibration_result` with `kind = live` (`param_name` n/k/p in natural units,
`spec.calibration_source = "live"`), accepted by `export hftbacktest|nautilus`.
"""

from __future__ import annotations

import math
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import numpy as np

from queuetruth import stats
from queuetruth.errors import QtError
from queuetruth.schemas.arrow import ENGINE_VERSION
from queuetruth.schemas.io import write_table
from queuetruth.schemas.models import CalibrationResult
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.schemas.specs import (
    GridRange,
    LiveCalibrationSpec,
    WindowSpec,
    ZooSpec,
)
from queuetruth.score._dataset import DatasetView
from queuetruth.score._io import Row, make_table, read_live_log, ref_path, resolve_ref, rows, write_json
from queuetruth.score.outcomes import log_outcomes
from queuetruth.score.runner import DefaultRunner, ScoreRunner, find_table
from queuetruth.score.schedule import (
    cancel_instruction,
    in_window,
    instruction_legs,
    legs_by_order,
    window_end,
)
from queuetruth.score.scorecard import keep_priority_venues, own_free_l2, report_reasons, schedule_with_report
from queuetruth.score.selfexclude import (
    align_to_twins,
    filtered_dataset,
    match_twins,
    twin_entry_points,
)
from queuetruth.score.statistics import block_labels, fin
from queuetruth.score.units import UnitMap, dataset_exps, live_scales

CHI2_95 = 3.841459
DAY_NS = 86_400_000_000_000
Family = Literal["power", "power2", "power3", "databento", "nt_prob_touch", "nt_queue_l2_prob_touch"]
DEFAULTS = {
    "power": 2.0,
    "power2": 2.0,
    "power3": 2.0,
    "databento": -0.8,
    "nt_prob_touch": 1.0,
    "nt_queue_l2_prob_touch": 1.0,
}
PARAM = {
    "power": "n",
    "power2": "n",
    "power3": "n",
    "databento": "k",
    "nt_prob_touch": "p",
    "nt_queue_l2_prob_touch": "p",
}
KIND = {
    "power": "alloc",
    "power2": "alloc",
    "power3": "alloc",
    "databento": "survival",
    "nt_prob_touch": "nt_touch",
    "nt_queue_l2_prob_touch": "nt_touch",
}


def _days(iso: str) -> int:
    return int(np.datetime64(iso, "D").astype("int64"))


def _default_grid(family: str) -> GridRange:
    if family.startswith("power"):
        return GridRange(lo=-3.0, hi=4.0, n=29)
    if family == "databento":
        return GridRange(lo=-0.98, hi=0.9, n=25)
    return GridRange(lo=0.02, hi=1.0, n=50)


def _fmt(x: float) -> str:
    return np.format_float_positional(float(x), unique=True, trim="-")


def grid_values(family: str, grid: GridRange) -> list[tuple[float, float]]:
    """(theta on the grid, parameter in natural units) pairs."""
    th = np.linspace(grid.lo, grid.hi, grid.n) if grid.n > 1 else np.array([grid.lo])
    return [(float(t), float(math.exp(t)) if family.startswith("power") else float(t)) for t in th]


def model_id(family: str, value: float) -> str:
    """Zoo model id for a family parameter value (mode/exchange/particles come from the ZooSpec)."""
    if family.startswith("power"):
        return f"hbt_{family}[n={_fmt(value)}]"
    if family == "databento":
        return f"databento_mc[k={_fmt(value)}]"
    return f"{family}[p={_fmt(value)}]"


def _param_of(mid: str, family: str) -> float | None:
    m = re.search(r"[\[;]" + PARAM[family] + r"=([^;\]]+)", mid)
    return float(m.group(1)) if m else None


def _smooth_n(family: str, particles: int) -> int:
    if family == "databento":
        return particles
    if family.startswith("nt_"):
        return 1000
    return 1


def interval_loglik(p_by_h: np.ndarray, k: int, delta: int, n_smooth: int) -> float:
    """C2 log-likelihood of one order: `p_by_h` = model P(fill by tau_j), j = 1..J; truth interval `k` (1-based)."""
    s = (np.clip(1.0 - p_by_h, 0.0, 1.0) * n_smooth + 0.5) / (n_smooth + 1)
    prev = np.concatenate([[1.0], s[:-1]])
    h = np.clip(1.0 - s / prev, 1e-12, 1 - 1e-12)
    ll = float(np.sum(np.log(1.0 - h[: k - 1])))
    ll += math.log(h[k - 1]) if delta else math.log(1.0 - h[k - 1])
    return ll


def _truth_interval(t_fill: int | None, t_censor: int, taus: list[int]) -> tuple[int, int] | None:
    """(K 1-based, delta) or None when nothing is observed."""
    if t_fill is not None:
        for j, tau in enumerate(taus, start=1):
            if t_fill <= tau:
                return j, 1
        return None
    k = max((j for j, tau in enumerate(taus, start=1) if tau <= t_censor), default=0)
    return (k, 0) if k else None


def _profile_ci(theta: list[float], ll: np.ndarray, i_max: int) -> tuple[float | None, float | None]:
    ok = 2 * (ll[i_max] - ll) <= CHI2_95
    lo = hi = i_max
    while lo - 1 >= 0 and ok[lo - 1]:
        lo -= 1
    while hi + 1 < len(theta) and ok[hi + 1]:
        hi += 1
    return (None if lo == 0 else theta[lo]), (None if hi == len(theta) - 1 else theta[hi])


def calibrate_live(
    spec: LiveCalibrationSpec | dict[str, Any],
    *,
    runner: ScoreRunner | None = None,
    self_exclude: bool | None = None,
    match_window_ns: int | None = None,
) -> CalibrationResult:
    """Fits the family parameter to live outcomes; writes `calibration.json` (+ `live_calibration_test.json`,
    `live_orders_truth.parquet`-free) to `spec.out` when set. Self-exclusion follows the spec's `self_exclude` and
    `match_window_ns` (schema 1.1; defaults true and 1 ms); the keyword arguments, when given, override them and
    are recorded in the spec (echo and `run_id`)."""
    s = LiveCalibrationSpec.model_validate(spec) if isinstance(spec, dict) else spec
    over = {k: v for k, v in (("self_exclude", self_exclude), ("match_window_ns", match_window_ns)) if v is not None}
    if over:
        s = LiveCalibrationSpec.model_validate({**s.model_dump(mode="json"), **over})
    self_exclude, match_window_ns = s.self_exclude, s.match_window_ns
    rn: ScoreRunner = runner or DefaultRunner()
    fam = s.family
    grid = s.grid or _default_grid(fam)
    live_dir = ref_path(s.live, "live")
    ds = DatasetView(ref_path(s.dataset, "dataset"))
    warnings = [f"live: {w}" for w in rn.validate(str(live_dir))]
    warnings.append("fitted against live outcomes (calibrate-live), not exact queue truth")
    orders, events = read_live_log(live_dir)
    # Units (review F4): live prices and quantities in the dataset's fixed-point units, as the scorer does.
    raw_exps = live_scales(live_dir, orders)
    units = UnitMap(dataset_exps(rows(ds.instruments)) if ds.instruments is not None else {})
    units.add_orders(orders, source=raw_exps)
    keep = keep_priority_venues(orders, str(ds.manifest.get("venue") or "") or None)
    orders, events = units.orders(orders), units.events(events)
    warnings += units.notes("live log")
    use_lat = s.entry == "send_plus_latency" and s.latency is not None
    sched_t, amend_t, report = schedule_with_report(rn, live_dir, s.entry, 0 if use_lat else None, keep)
    raw_schedule = [dict(r) for r in rows(sched_t)]
    raw_amends = units.amends([dict(r) for r in rows(amend_t)], raw_schedule)
    raw_schedule = units.schedule(raw_schedule)
    if report is not None:
        warnings += report_reasons(report)[1]
    live_oc, w = log_outcomes(orders, events)
    warnings += [f"live: {x}" for x in w]
    schedule, _amends, _tot = instruction_legs(raw_schedule, raw_amends, events, live_oc, s.entry)
    legs = legs_by_order(schedule)
    if not legs:
        raise QtError("QT-INPUT-002", f"{live_dir}: no schedulable live orders", "need acked resting limit orders")
    ev_by: dict[str, list[Row]] = {}
    for e in events:
        ev_by.setdefault(str(e["order_key"]), []).append(e)
    info = {str(o["order_key"]): o for o in orders}

    data_dates = sorted({sd for (_iid, sd) in ds.ts_max})
    for name, win in (("train", s.train), ("test", s.test)):
        missing = [d for d in (win.dates or []) if data_dates and _days(d) not in set(data_dates)]
        if missing:
            raise QtError(
                "QT-DATA-001",
                f"calibrate-live: {name} window date(s) {', '.join(missing)} not in the dataset; available: "
                + ", ".join(str(np.datetime64(int(d), "D")) for d in data_dates),
            )
    taus = sorted({int(h) for h in s.horizons_ns if int(h) < s.window_ns} | {int(s.window_ns)})
    truth: dict[str, dict[str, Any]] = {}
    n_taker_first = 0
    for key, lg in sorted(legs.items()):
        first = lg[0]
        t0, iid = int(first["ts_submit"]), int(first["instrument_id"])
        sdate, midnight = ds.day(iid, t0)
        w_end, cut = window_end(t0, cancel_instruction(ev_by.get(key, [])), ds.end_of_data(iid, sdate), s.window_ns)
        if len(lg) > 1 and int(lg[1]["ts_submit"]) < w_end:
            w_end, cut = int(lg[1]["ts_submit"]), "leg"
        maker, taker = in_window(live_oc.get(key), t0, w_end)
        ff = min((f.ts for f in (*maker, *taker)), default=None)
        if ff is not None and not any(f.ts == ff for f in maker):
            n_taker_first += 1
        truth[key] = {
            "sid": str(first["shadow_id"]),
            "t0": t0,
            "instrument_id": iid,
            "session_date": sdate,
            "local_ns": t0 - midnight,
            "ts_entry": t0,
            "t_fill": None if ff is None else ff - t0,
            "t_censor": w_end - t0,
            "cut": cut,
            "filled": ff is not None,
            "interval": _truth_interval(None if ff is None else ff - t0, w_end - t0, taus),
        }

    # ---- split ---------------------------------------------------------------------------------------------------
    keys = sorted(truth, key=lambda k: (truth[k]["t0"], k))
    dates = sorted({truth[k]["session_date"] for k in keys})
    iso = {d: str(np.datetime64(int(d), "D")) for d in dates}
    if s.train.dates or s.test.dates or s.train.blocks or s.test.blocks:

        def in_win(k: str, win: WindowSpec) -> bool:
            tk = truth[k]
            if win.dates is not None and iso[tk["session_date"]] not in set(win.dates):
                return False
            if win.blocks is not None:
                b = ds.block_of(tk["instrument_id"], tk["t0"], int(win.blocks.n_blocks))
                return b in set(win.blocks.index)
            return True

        def given(win: WindowSpec) -> bool:
            return win.dates is not None or win.blocks is not None

        train = [k for k in keys if in_win(k, s.train)] if given(s.train) else []
        if given(s.test):
            test = [k for k in keys if in_win(k, s.test) and k not in set(train)]
        else:
            test = [k for k in keys if k not in set(train)]
        if not given(s.train):
            train = [k for k in keys if k not in set(test)]
        tr_echo: dict[str, Any] = s.train.model_dump(mode="json", exclude_none=True) or {
            "dates": sorted({iso[truth[k]["session_date"]] for k in train})
        }
        te_echo: dict[str, Any] = s.test.model_dump(mode="json", exclude_none=True) or {
            "dates": sorted({iso[truth[k]["session_date"]] for k in test})
        }
    elif len(dates) >= 2:
        cut_i = max(1, len(dates) // 2)
        tr = set(dates[:cut_i])
        train = [k for k in keys if truth[k]["session_date"] in tr]
        test = [k for k in keys if truth[k]["session_date"] not in tr]
        tr_echo = {"dates": [iso[d] for d in dates[:cut_i]]}
        te_echo = {"dates": [iso[d] for d in dates[cut_i:]]}
    else:
        half = max(1, len(keys) // 2)
        train, test = keys[:half], keys[half:]
        tr_echo = {"blocks": {"index": [0], "n_blocks": 2}}
        te_echo = {"blocks": {"index": [1], "n_blocks": 2}}
        warnings.append("single session date: chronological split of orders (first half train)")
    if n_taker_first:
        warnings.append(
            f"taker_fills: {n_taker_first} order(s) first filled as taker in W count as filled (the zoo's p_fill "
            "counts model crosses as fills too)"
        )
    if not train:
        raise QtError("QT-DATA-001", "calibrate-live: the train window holds no schedulable live order")
    if not test:
        raise QtError(
            "QT-DATA-001",
            "calibrate-live: the test window holds no schedulable live order; the fit could not be checked out of "
            "sample",
            "give a test window with orders (--test DATE), or a log with at least two orders",
        )

    # ---- zoo run over the grid -------------------------------------------------------------------------------
    grid_pts = grid_values(fam, grid)
    default_v = DEFAULTS[fam]
    mids = [model_id(fam, v) for _, v in grid_pts]
    base_mid = model_id(fam, default_v)
    all_mids = mids + ([base_mid] if base_mid not in mids else [])
    with tempfile.TemporaryDirectory(prefix="qt-calive-") as tmp:
        work = Path(tmp)
        first_legs = [lg[0] for _, lg in sorted(legs.items())]
        sched_path = work / "schedule.parquet"
        write_table(make_table("qt.shadow_spec", first_legs), sched_path)
        root = ds.root
        if ds.kind != "l2" and self_exclude:
            tw = match_twins(
                ds.root,
                [
                    {
                        "order_key": str(r.get("order_key") or r["shadow_id"]),
                        "instrument_id": int(r["instrument_id"]),
                        "side": int(r["side"]),
                        "price": int(r.get("price") or 0),
                        "qty": int(r["qty"]),
                        "ts_entry": int(r["ts_submit"]),
                        "venue_order_id": info.get(str(r.get("order_key")), {}).get("venue_order_id"),
                        "session_date": ds.day(int(r["instrument_id"]), int(r["ts_submit"]))[0],
                    }
                    for r in first_legs
                ],
                match_window_ns,
            )
            warnings.append(tw.summary())
            collide = tw.collision_warning()
            if collide is not None:
                warnings.append(collide)
            if tw.all_ids():
                root = filtered_dataset(ds.root, work / "dataset_self_excluded", tw.ids, by_day=tw.days)
                if s.entry == "exchange_ack":
                    moved, n_moved = align_to_twins(first_legs, tw, twin_entry_points(ds.root, tw.ids))
                    if n_moved:
                        write_table(make_table("qt.shadow_spec", moved), sched_path)
                        warnings.append(f"self_exclusion: {n_moved} order(s) activated in their twin's queue place")
        elif ds.kind == "l2":
            # Trader review E2: on an L2-only tape the user's own resting size is in the visible depth; without its
            # removal the twin queues behind its own order and the fit absorbs a fake underfill.
            opts = SimpleNamespace(self_exclude=self_exclude, match_window_ns=match_window_ns)
            root, why_not, w = own_free_l2(ds.root, orders, events, opts, work)  # type: ignore[arg-type]
            warnings += w
            if why_not is not None:
                warnings.append(
                    f"trust: own_orders_in_depth: the user's own resting orders were not removed from the L2 depth "
                    f"({why_not}); the twins queue behind their own orders and the fit absorbs a fake underfill"
                )
        zs = ZooSpec.model_validate(
            {
                "dataset": {"path": str(root)},
                "shadows": {"path": str(sched_path)},
                "out": str(work / "zoo"),
                "models": all_mids,
                "mode": s.mode,
                "exchange": s.exchange,
                "horizons_ns": [t for t in taus if t < s.window_ns],
                "h_max_ns": int(s.window_ns),
                "l2": s.l2.model_dump(),
                "particles": s.particles,
                "seed": s.seed,
                "latency": s.latency.model_dump() if use_lat and s.latency is not None else {},
                "write_fills": False,
                "threads": s.threads,
                "bootstrap_b": 1,
            }
        )
        rn.run_zoo(zs)
        pr = find_table(work / "zoo", "qt.zoo_predictions")
    if pr is None:
        raise QtError("QT-ENGINE-001", "zoo run wrote no qt.zoo_predictions")
    pred: dict[tuple[float, str], dict[int, float]] = {}
    for r in rows(pr):
        v = _param_of(str(r["model_id"]), fam)
        if v is None:
            continue
        pred.setdefault((v, str(r["shadow_id"])), {})[int(r["horizon_ns"])] = float(r["p_fill"])

    def pvec(v: float, sid: str) -> np.ndarray | None:
        d = pred.get((float(_fmt(v)), sid))
        if d is None:
            return None
        return np.array([d.get(t, np.nan) for t in taus])

    nsm = _smooth_n(fam, s.particles)
    usable = [k for k in train if truth[k]["interval"] is not None]
    L = np.zeros((len(usable), len(grid_pts)))
    for i, k in enumerate(usable):
        kk, delta = truth[k]["interval"]
        for g, (_, v) in enumerate(grid_pts):
            p = pvec(v, truth[k]["sid"])
            L[i, g] = interval_loglik(np.nan_to_num(p, nan=0.0), kk, delta, nsm) if p is not None else 0.0
    if not usable:
        raise QtError("QT-DATA-001", "calibrate-live: no train order with an observed interval")
    n_filled_train = sum(1 for k in usable if truth[k]["interval"][1] == 1)
    if n_filled_train == 0:
        raise QtError(
            "QT-DATA-001",
            f"calibrate-live: none of the {len(usable)} train order(s) filled inside its window; the fill model "
            "cannot be fitted (every grid value that never fills would win)",
        )
    ll = L.sum(axis=0)
    i_max = int(np.argmax(ll))
    thetas = [t for t, _ in grid_pts]
    nat = [v for _, v in grid_pts]
    plo, phi = _profile_ci(thetas, ll, i_max)
    to_nat = (lambda t: math.exp(t)) if fam.startswith("power") else (lambda t: t)
    blocks, kind = block_labels([truth[k] for k in usable], s.bootstrap.block)
    _, reps = stats.block_replicates(
        blocks,
        lambda idx: float(nat[int(np.argmax(L[idx].sum(axis=0)))]),
        int(s.bootstrap.b),
        stats.bootstrap_seed(s.seed, "calibrate_live"),
    )
    blo, bhi = stats.percentile_ci(reps)
    if kind == "30min" and s.bootstrap.block == "auto":
        warnings.append("ci_block=30min")
    est = nat[i_max]
    from queuetruth.export import trust as qtrust

    warnings += qtrust.grid_lines(thetas, [float(x) for x in ll], PARAM[fam], to_nat)
    if blo is not None and bhi is not None and math.isfinite(blo) and blo == bhi:
        warnings.append(
            f"trust: ci_grid_resolution: every bootstrap replicate chose {PARAM[fam]} = {qtrust.g6(blo)}; a "
            "zero-width interval reflects the grid step, not precision (suppressed)"
        )
        blo = bhi = None

    # ---- held-out ----------------------------------------------------------------------------------------------
    def held(v: float) -> dict[str, dict[str, float | None]]:
        out: dict[str, dict[str, float | None]] = {"brier": {}, "logloss": {}, "fill_rate_bias": {}}
        for j, tau in enumerate(taus):
            ps, ys = [], []
            for k in test:
                t = truth[k]
                y = t["t_fill"] is not None and t["t_fill"] <= tau
                # A2: only an administrative cut (end of data) before tau makes the label undefined; a cancel or
                # leg cut is the order's own instruction, after which neither arm can fill (label 0).
                if not y and t["cut"] == "end_of_data" and t["t_censor"] < tau:
                    continue
                p = pvec(v, t["sid"])
                if p is None or not np.isfinite(p[j]):
                    continue
                ps.append(float(p[j]))
                ys.append(1.0 if y else 0.0)
            key = str(tau)
            if ps:
                pa_, ya = np.array(ps), np.array(ys)
                pc = np.clip(pa_, 1e-6, 1 - 1e-6)
                out["brier"][key] = float(np.mean((pa_ - ya) ** 2))
                out["logloss"][key] = float(-np.mean(ya * np.log(pc) + (1 - ya) * np.log(1 - pc)))
                out["fill_rate_bias"][key] = float(np.mean(pa_) - np.mean(ya))
            else:
                out["brier"][key] = out["logloss"][key] = out["fill_rate_bias"][key] = None
        return out

    fitted_mid = model_id(fam, est)
    held_out = {fitted_mid: held(est)} if test else {}
    baseline = {base_mid: held(default_v)} if test else {}
    disc = _discordance(test, truth, lambda k: pvec(est, truth[k]["sid"]), s)
    from queuetruth.export import export_snippets

    dumped = s.model_dump(mode="json")
    doc = {
        "run_id": run_id(dumped, ENGINE_VERSION, resolve_ref),
        "spec": {**spec_echo(dumped), "calibration_source": "live"},
        "kind": "live",
        "family": fam,
        "mode": s.mode,
        "param_name": PARAM[fam],
        "estimate": est,
        "ci_profile": None
        if plo is None and phi is None
        else [None if plo is None else to_nat(plo), None if phi is None else to_nat(phi)],
        "ci_bootstrap": [None if blo is None else fin(blo), None if bhi is None else fin(bhi)],
        "loglik": float(ll[i_max]),
        "n_events": n_filled_train,
        "n_shadows": len(usable),
        "train": tr_echo,
        "test": te_echo,
        "held_out": held_out,
        "baseline_held_out": baseline,
        "stratified": [],
        "held_out_discordance": disc,
        "latency": None,
        "exports": {},
        "data_sources": [ds.data_source()],
        "warnings": warnings,
    }
    cal = qtrust.annotate(CalibrationResult.model_validate(doc))
    cal = cal.model_copy(update={"exports": export_snippets(cal)})
    if s.out:
        out = Path(s.out)
        out.mkdir(parents=True, exist_ok=True)
        write_json(cal.model_dump(mode="json"), out / "calibration.json")
        write_json(
            {
                "schema": "qt.live_calibration_test",
                "schema_version": 1,
                "model_id": fitted_mid,
                "grid": [{"theta": t, "value": v, "loglik": float(x)} for (t, v), x in zip(grid_pts, ll, strict=True)],
                **disc,
            },
            out / "live_calibration_test.json",
        )
    return cal


def _discordance(
    test: list[str], truth: dict[str, dict[str, Any]], pfun: Any, s: LiveCalibrationSpec
) -> dict[str, Any]:
    keys = [k for k in test if pfun(k) is not None]
    live = np.array([bool(truth[k]["filled"]) for k in keys], dtype=bool)
    sim = np.array([bool(np.nan_to_num(pfun(k)[-1]) >= 0.5) for k in keys], dtype=bool)
    out: dict[str, Any] = {
        "n": len(keys),
        "b": int(np.sum(live & ~sim)),
        "c": int(np.sum(sim & ~live)),
        "fill_rate_bias": None,
        "ci_lo": None,
        "ci_hi": None,
        "p_exact": None,
        "p_clustered": None,
        "clustered_method": None,
        "n_clusters": 0,
    }
    if not keys:
        return out
    m = stats.mcnemar(out["b"], out["c"])
    blocks, _ = block_labels([truth[k] for k in keys], s.bootstrap.block)
    cl = stats.clustered_mcnemar(
        live, sim, blocks, b=int(s.bootstrap.b), seed=stats.bootstrap_seed(s.seed, "calibrate_live_test")
    )
    out.update(
        fill_rate_bias=fin(cl.estimate),
        ci_lo=fin(cl.ci_lo),
        ci_hi=fin(cl.ci_hi),
        p_exact=fin(m.p_exact),
        p_clustered=fin(cl.p_primary),
        clustered_method=cl.primary_method,
        n_clusters=cl.n_clusters,
    )
    return out
