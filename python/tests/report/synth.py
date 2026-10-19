"""Synthetic report inputs (test helper module) conforming to the frozen schemas (SPEC 3.4, 3.10): scorecard, zoo,
calibration, learned model and replay output directories generated from a seeded numpy RNG. No other component is
needed."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.report.fmt import esc
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.models import (
    CalibrationResult,
    DataQuality,
    DatasetManifest,
    HazardModelCard,
    ReplaySummary,
    ScorecardSummary,
    WhyTrace,
    ZooSummary,
)

DAY = 18260  # 2019-12-30
MIDNIGHT = DAY * 86_400 * 1_000_000_000 + 5 * 3_600_000_000_000  # US/Eastern midnight in UTC
OPEN = MIDNIGHT + 34_200 * 1_000_000_000
HORIZONS = [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000]
H_MAX = 300_000_000_000
MODELS = [
    "front_of_queue[mode=faithful]",
    "hbt_power3[exch=partial;mode=faithful;n=2]",
    "hbt_risk_adverse[exch=partial;mode=faithful]",
    "nt_prob_touch[mode=faithful;p=1]",
    "databento_mc[k=-0.8;mode=faithful;particles=100]",
    "trade_through_only[mode=faithful]",
]
SHA0 = "0" * 64


def table(name: str, cols: dict[str, Any]) -> pa.Table:
    """Arrow table with the frozen schema `name` from Python column lists."""
    sch = SCHEMAS[name]
    return pa.Table.from_arrays([pa.array(cols[f.name], type=f.type) for f in sch], schema=sch)


def write(root: Path, fname: str, name: str, cols: dict[str, Any]) -> dict[str, Any]:
    """Writes a frozen-schema Parquet table and returns its Files entry."""
    return write_table(table(name, cols), root / fname, SCHEMAS[name])


def write_doc(path: Path, doc: Any) -> None:
    """Writes a Pydantic document as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc.model_dump_json(indent=2) + "\n", encoding="utf-8")


def dq(verdict: str = "warnings") -> DataQuality:
    """A data-quality document with one non-zero counter."""
    return DataQuality(
        records=200_000,
        maybe_bad_book_records=0,
        bad_ts_recv_records=0,
        snapshot_records=120,
        pre_snapshot_rows_skipped=0,
        unknown_order_refs=3 if verdict != "clean" else 0,
        duplicate_adds=0,
        unexpected_modifies=0,
        sequence_gaps=0,
        sequence_regressions=0,
        out_of_order_ts_event=0,
        crossed_books_after_last=0,
        hidden_prints=0,
        off_price_executions=0,
        feed_gaps=[
            {
                "instrument_id": 1,
                "ts_start": OPEN + 10**12,
                "ts_end": OPEN + 10**12 + 4 * 10**9,
                "reason": "sequence gap",
            }
        ]
        if verdict != "clean"
        else [],
        source_specific={"unterminated_events": 0},
        verdict=verdict,
        reasons=["unknown_order_refs > 0"] if verdict != "clean" else [],
    )


# ---- scorecard ---------------------------------------------------------------------------------------------------


def make_score_dir(
    root: Path,
    n: int = 400,
    seed: int = 1,
    with_why: bool = True,
    dataset: str = "data/synth",
    live: str = "live/synth",
) -> dict[str, Any]:
    """A `score` output directory; returns facts used by tests."""
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)
    keys = [f"binance:acct:{i:05d}" for i in range(n)]
    inst = (np.arange(n) % 2 + 1).tolist()
    day2 = np.arange(n) % 3 == 0
    sdate = np.where(day2, DAY + 1, DAY).tolist()
    ts_entry = (OPEN + np.where(day2, 86_400 * 10**9, 0) + np.sort(rng.integers(0, 6 * 3600 * 10**9, n))).tolist()
    qa = rng.integers(0, 5000, n)
    live_f = rng.random(n) < 0.45
    flip_up = rng.random(n) < 0.12
    flip_dn = rng.random(n) < 0.04
    sim_f = (live_f & ~flip_dn) | (~live_f & flip_up)
    cls = np.where(
        live_f & sim_f, "both", np.where(~live_f & ~sim_f, "neither", np.where(live_f, "live_only", "sim_only"))
    )
    qty = rng.integers(1, 10, n) * 100
    ttf_live = np.where(live_f, rng.lognormal(21, 1.5, n).astype(np.int64), 0)
    ttf_sim = np.where(
        sim_f,
        (ttf_live * rng.lognormal(-0.3, 0.4, n)).astype(np.int64)
        + np.where(live_f, 0, rng.lognormal(21, 1.5, n).astype(np.int64)),
        0,
    )
    dec = np.minimum(qa // 500, 9)
    orders = {
        "order_key": keys,
        "instrument_id": inst,
        "session_date": sdate,
        "side": np.where(np.arange(n) % 2, "sell", "buy").tolist(),
        "limit_px": (100_000_000_000 + rng.integers(-50, 50, n) * 10_000_000).tolist(),
        "qty": qty.tolist(),
        "ts_entry": ts_entry,
        "live_filled": live_f.tolist(),
        "live_filled_qty": np.where(live_f, qty, 0).tolist(),
        "live_ts_first_fill": [int(t + d) if f else None for t, d, f in zip(ts_entry, ttf_live, live_f, strict=True)],
        "sim_filled": sim_f.tolist(),
        "sim_filled_qty": np.where(sim_f, qty, 0).tolist(),
        "sim_ts_first_fill": [int(t + d) if f else None for t, d, f in zip(ts_entry, ttf_sim, sim_f, strict=True)],
        "class": cls.tolist(),
        "live_end": np.where(live_f, "filled", "cancelled").tolist(),
        "queue_ahead_at_entry": qa.tolist(),
        "queue_ahead_source": ["exact"] * n,
        "queue_decile": dec.tolist(),
        "liquidity_bucket": (np.arange(n) % 3).tolist(),
        "ttf_live_ns": [int(d) if f else None for d, f in zip(ttf_live, live_f, strict=True)],
        "ttf_sim_ns": [int(d) if f else None for d, f in zip(ttf_sim, sim_f, strict=True)],
        "overlap_same_px": (rng.random(n) < 0.01).tolist(),
        "sim_model_id": ["exact"] * n,
        "trade_vol_60s": rng.integers(0, 10**6, n).tolist(),
        "legs": [1] * n,
    }
    files = [write(root, "orders.parquet", "qt.score_orders", orders)]
    f_rows: dict[str, list] = {k: [] for k in ("order_key", "arm", "fill_seq", "ts", "qty", "px", "liquidity")}
    mk: dict[str, list] = {
        k: [] for k in ("order_key", "arm", "fill_seq", "horizon_ns", "ref", "mo_ticks", "mo_ccy", "mm_ticks")
    }
    pos: dict[str, list] = {k: [] for k in ("arm", "instrument_id", "ts", "position")}
    mh = [50_000_000, 100_000_000, 10**9, 10**10, 15 * 10**9, 6 * 10**10, 3 * 10**11]
    for arm, filled, ttf in (("live", live_f, ttf_live), ("sim", sim_f, ttf_sim)):
        cum = {1: 0, 2: 0}
        for i in np.argsort(np.array(ts_entry) + ttf, kind="stable"):
            if not filled[i]:
                continue
            t = int(ts_entry[i] + ttf[i])
            f_rows["order_key"].append(keys[i])
            f_rows["arm"].append(arm)
            f_rows["fill_seq"].append(1)
            f_rows["ts"].append(t)
            f_rows["qty"].append(int(qty[i]))
            f_rows["px"].append(orders["limit_px"][i])
            f_rows["liquidity"].append("taker" if arm == "sim" and i % 9 == 0 else "maker")
            for h in mh:
                v = float(rng.normal(-0.2 - 0.1 * math.log10(h / 1e7) - (0.3 if arm == "sim" else 0), 1.0))
                mk["order_key"].append(keys[i])
                mk["arm"].append(arm)
                mk["fill_seq"].append(1)
                mk["horizon_ns"].append(h)
                mk["ref"].append("mid")
                mk["mo_ticks"].append(v)
                mk["mo_ccy"].append(v * 0.01 * int(qty[i]))
                mk["mm_ticks"].append(v * 0.5)
            sgn = 1 if orders["side"][i] == "buy" else -1
            cum[inst[i]] += sgn * int(qty[i])
            pos["arm"].append(arm)
            pos["instrument_id"].append(inst[i])
            pos["ts"].append(t)
            pos["position"].append(cum[inst[i]])
    files.append(write(root, "fills.parquet", "qt.score_fills", f_rows))
    files.append(write(root, "markouts.parquet", "qt.score_markouts", mk))
    files.append(write(root, "position.parquet", "qt.score_position", pos))
    tests = {
        "test_id": ["mcnemar_overall", "fill_rate_bias", "markout_diff_1s"]
        + [f"mcnemar_decile_{d}" for d in range(10)],
        "family": ["primary"] * 3 + ["secondary"] * 10,
        "stratum_kind": ["all"] * 3 + ["queue_decile"] * 10,
        "stratum": ["all"] * 3 + [str(d) for d in range(10)],
        "statistic": ["p_exact", "bootstrap", "mean"] + ["p_exact"] * 10,
        "value": rng.random(13).tolist(),
        "p_value": rng.random(13).tolist(),
        "p_adj": rng.random(13).tolist(),
        "adj_method": ["holm"] * 3 + ["bh"] * 10,
        "ci_lo": [None] * 13,
        "ci_hi": [None] * 13,
        "n": [n] * 13,
        "note": [None] * 13,
    }
    b_ = int((cls == "live_only").sum())
    c_ = int((cls == "sim_only").sum())
    extra_tests = [
        (
            "discordance_clustered",
            "primary",
            "all",
            "all",
            "fill_rate_bias",
            (c_ - b_) / n,
            2.5e-4,
            None,
            "holm",
            (c_ - b_) / n - 0.02,
            (c_ - b_) / n + 0.02,
            n,
            "obuchowski; clusters=7; discordant clusters=6; design_effect=1.84; n_effective=217.4",
        ),
        (
            "mcnemar",
            "secondary",
            "all",
            "all",
            "chi2_cc",
            12.0,
            3e-6,
            1e-5,
            "bh",
            None,
            None,
            b_ + c_,
            "p = exact; assumes independent orders",
        ),
    ]
    for h in (10**9, 10**10):
        for arm in ("live", "sim"):
            extra_tests.append(
                (
                    "markout_mean_taker",
                    "secondary",
                    arm,
                    f"mid:{h}",
                    "mean_ticks",
                    -1.1 if arm == "sim" else -0.9,
                    None,
                    None,
                    "none",
                    -1.6,
                    -0.5,
                    7,
                    "taker fills",
                )
            )
    for t in extra_tests:
        for k, v in zip(
            (
                "test_id",
                "family",
                "stratum_kind",
                "stratum",
                "statistic",
                "value",
                "p_value",
                "p_adj",
                "adj_method",
                "ci_lo",
                "ci_hi",
                "n",
                "note",
            ),
            t,
            strict=True,
        ):
            tests[k].append(v)
    files.append(write(root, "tests.parquet", "qt.score_tests", tests))
    b = int((cls == "live_only").sum())
    c = int((cls == "sim_only").sum())
    both = int((cls == "both").sum())
    neither = int((cls == "neither").sum())
    bias = (c - b) / n

    def stratum_rows(key: str, vals: np.ndarray, k: int) -> list[dict[str, Any]]:
        out = []
        for v in range(k):
            m = vals == v
            out.append(
                {
                    key: v,
                    "both": int((m & (cls == "both")).sum()),
                    "neither": int((m & (cls == "neither")).sum()),
                    "live_only": int((m & (cls == "live_only")).sum()),
                    "sim_only": int((m & (cls == "sim_only")).sum()),
                    "p_exact": float(rng.random()),
                    "p_adj": float(rng.random()),
                }
            )
        return out

    summary = ScorecardSummary(
        run_id="a1b2c3d4e5f60718",
        spec={
            "live": {"path": live},
            "sim": {"kind": "exact"},
            "dataset": {"path": dataset},
            "entry": "exchange_ack",
            "markout_horizons_ns": mh,
        },
        live_source=live,
        sim_source={"kind": "exact"},
        n_orders=n,
        n_matched=n,
        n_unmatched_live=0,
        n_unmatched_sim=0,
        n_overlap_same_px=int(np.sum(orders["overlap_same_px"])),
        two_by_two={"both": both, "neither": neither, "live_only": b, "sim_only": c},
        bias={
            "fill_rate_bias": bias,
            "ci_lo": bias - 0.02,
            "ci_hi": bias + 0.02,
            "relative_overfill": (both + c) / max(both + b, 1) - 1,
            "qty_bias": bias * 0.9,
            "qty_bias_ci_lo": bias * 0.9 - 0.02,
            "qty_bias_ci_hi": bias * 0.9 + 0.02,
            "direction": "sim_overfills",
        },
        mcnemar={
            "b": b,
            "c": c,
            "chi2_cc": (abs(b - c) - 1) ** 2 / max(b + c, 1),
            "p_chi2": 1e-5,
            "p_exact": 3e-6,
            "odds_ratio": c / max(b, 1),
            "or_ci_lo": 1.5,
            "or_ci_hi": 4.0,
        },
        by_queue_decile=stratum_rows("decile", dec, 10),
        by_liquidity=stratum_rows("bucket", np.arange(n) % 3, 3),
        ttf={
            "n_joint": both,
            "median_log_ratio": -0.3,
            "ci_lo": -0.42,
            "ci_hi": -0.18,
            "wilcoxon_p": 1e-4,
            "paired_perm_p": 0.002,
            "logrank_p": 0.01,
            "gray_p": 0.03,
            "cif_fill": {
                "live": {str(h): 0.1 + 0.08 * i for i, h in enumerate(HORIZONS)},
                "sim": {str(h): 0.14 + 0.1 * i for i, h in enumerate(HORIZONS)},
            },
        },
        markouts=[
            {
                "horizon_ns": h,
                "ref": "mid",
                "arm": arm,
                "n": 100,
                "mean_ticks": -0.1 * i - (0.2 if arm == "sim" else 0),
                "ci_lo": -0.1 * i - 0.3,
                "ci_hi": -0.1 * i + 0.1,
            }
            for arm in ("live", "sim")
            for i, h in enumerate(mh)
        ],
        markout_diff=[
            {
                "horizon_ns": h,
                "ref": "mid",
                "n": both,
                "mean_diff_ticks": -0.2,
                "ci_lo": -0.35,
                "ci_hi": -0.05,
                "p_adj": 0.01,
            }
            for h in mh
        ],
        sim_only_vs_live_only_markout=[
            {
                "horizon_ns": h,
                "sim_only_mean": -0.6 - 0.1 * i,
                "live_only_mean": -0.1,
                "diff": -0.5 - 0.1 * i,
                "ci_lo": -0.9,
                "ci_hi": -0.2,
            }
            for i, h in enumerate(mh)
        ],
        equivalence={
            "fill_rate_margin": 0.02,
            "tost_p": 0.9,
            "fill_rate_equivalent": False,
            "markout_margin_ticks": 0.25,
            "markout_horizon_ns": 10**9,
            "markout_tost_p": 0.4,
            "markout_equivalent": False,
        },
        fdr={"method": "bh", "q": 0.05, "n_tests": 13, "n_rejected": 4},
        verdict={
            "headline": f"sim overfills live by {100 * bias:+.1f} pp fill rate (95% CI [{100 * bias - 2:.1f}, "
            f"{100 * bias + 2:.1f}]); McNemar p = 3e-06 (cluster-robust p = 0.00025, 7 clusters); {n} orders",
            "signed_bias": bias,
            "gates_passed": False,
            "failed_gates": ["max_abs_fill_rate_bias"],
        },
        data_quality=dq("warnings"),
        files=files,
        warnings=[
            "ci_block=30min",
            "clusters: 7 (30min blocks), with discordance 6, design effect 1.84, effective n 217.4; primary "
            "discordance test obuchowski",
        ],
    )
    write_doc(root / "summary.json", summary)
    first_sim = min((i for i in range(n) if cls[i] == "sim_only"), key=lambda i: (ts_entry[i], keys[i]))
    sim_only_key = keys[first_sim]
    if with_why:
        ev = []
        t0 = ts_entry[first_sim]
        ahead = 600
        for j in range(12):
            size = 100
            is_ahead = j % 3 != 2
            if is_ahead:
                ahead = max(ahead - size, 0)
            ev.append(
                {
                    "ts_event": t0 + (j + 1) * 50_000_000,
                    "sequence": 1000 + j,
                    "action": "C" if j % 2 else "T",
                    "order_id": 5000 + j,
                    "side": "B",
                    "price": 100_000_000_000,
                    "size": size,
                    "effect": "remove" if j % 2 else "none",
                    "is_ahead": is_ahead,
                    "queue_ahead_after": ahead,
                    "rank_after": ahead // 100,
                    "level_qty_after": ahead + 300,
                    "fill_qty": 0,
                    "note": None,
                }
            )
        tr = WhyTrace(
            subject={"order_key": sim_only_key},
            instrument_id=1,
            side="B",
            price=100_000_000_000,
            qty=100,
            ts_live=t0,
            outcome={"filled_qty": 0, "end_state": "cancelled"},
            events=ev,
            truncated=False,
        )
        write_doc(root / "why-trace.json", tr)
    return {"n": n, "b": b, "c": c, "both": both, "neither": neither, "sim_only_key": sim_only_key, "files": files}


# ---- zoo ---------------------------------------------------------------------------------------------------------


def make_zoo_dir(
    root: Path, n: int = 200, seed: int = 2, with_errors: bool = True, with_traj: bool = True
) -> dict[str, Any]:
    """A `zoo` output directory with predictions for MODELS on n shadows."""
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)
    sids = [f"s{i:05d}" for i in range(n)]
    inst = np.arange(n) % 2 + 1
    qa = rng.integers(0, 4000, n)
    ts_live = OPEN + np.sort(rng.integers(0, 5 * 3600 * 10**9, n))
    truth_ttf = (rng.exponential(2e10, n) * (1 + qa / 1000)).astype(np.int64)
    truth_f = truth_ttf < H_MAX
    cens_t = np.where(rng.random(n) < 0.1, rng.integers(10**9, H_MAX, n), np.iinfo(np.int64).max)
    big = np.iinfo(np.int64).max
    cancel_rel = np.where(np.arange(n) % 5 == 4, rng.integers(5 * 10**9, 60 * 10**9, n), big)
    raw_ttf = truth_ttf.copy()
    truth_ttf = np.where(truth_ttf < cancel_rel, truth_ttf, big)  # own cancel competes with the fill
    truth_f = truth_ttf < H_MAX
    hs = [*HORIZONS, H_MAX]
    cols: dict[str, list] = {f.name: [] for f in SCHEMAS["qt.zoo_predictions"]}
    for mi, m in enumerate(MODELS):
        speed = [0.2, 0.7, 1.0, 0.5, 0.9, 3.0][mi]
        model_ttf = (raw_ttf * speed * rng.lognormal(0, 0.3, n)).astype(np.int64)
        prob = m.startswith(("nt_prob", "databento"))
        for i in range(n):
            for h in hs:
                tf = bool(truth_ttf[i] <= h)
                cen = (not tf) and h > cens_t[i]
                if prob:
                    p = float(1 - math.exp(-h / max(model_ttf[i], 1)))
                else:
                    p = 1.0 if model_ttf[i] <= h else 0.0
                cols["model_id"].append(m)
                cols["shadow_id"].append(sids[i])
                cols["qty"].append(100)
                cols["horizon_ns"].append(h)
                cols["p_fill"].append(p)
                cols["e_fill_qty"].append(100 * p)
                cols["model_ts_first_fill"].append(int(ts_live[i] + model_ttf[i]) if model_ttf[i] <= H_MAX else None)
                cols["truth_filled"].append(tf)
                cols["truth_ts_first_fill"].append(int(ts_live[i] + truth_ttf[i]) if truth_f[i] else None)
                cols["truth_filled_qty"].append(100 if tf else 0)
                cols["censored"].append(bool(cen))
                cols["queue_ahead_at_live"].append(int(qa[i]))
                cols["level_qty_at_live"].append(int(qa[i] + 200))
                cols["visible_level_qty_at_live"].append(int(qa[i] + 200))
                cols["out_of_view_at_live"].append(False)
                cols["instrument_id"].append(int(inst[i]))
                cols["session_date"].append(DAY)
                cols["ts_live"].append(int(ts_live[i]))
                cols["trade_vol_60s"].append(int(rng.integers(0, 10**5)))
    files = [write(root, "zoo_predictions.parquet", "qt.zoo_predictions", cols)]
    p = np.array(cols["p_fill"])
    y = np.array(cols["truth_filled"], dtype=float)
    cen = np.array(cols["censored"])
    mids = np.array(cols["model_id"])
    hz = np.array(cols["horizon_ns"])
    lb = []
    er: dict[str, list] = {f.name: [] for f in SCHEMAS["qt.zoo_errors"]}
    dec = np.minimum(np.array(cols["queue_ahead_at_live"]) // 400, 9)
    for m in MODELS:
        e = {"model_id": m, "brier": {}, "logloss": {}, "fill_rate_bias": {}}
        for h in HORIZONS:
            sel = (mids == m) & (hz == h) & ~cen
            br = float(np.mean((p[sel] - y[sel]) ** 2))
            fb = float(p[sel].mean() - y[sel].mean())
            e["brier"][str(h)] = br
            pc_ = np.clip(p[sel], 1e-6, 1 - 1e-6)
            e["logloss"][str(h)] = float(-np.mean(y[sel] * np.log(pc_) + (1 - y[sel]) * np.log(1 - pc_)))
            e["fill_rate_bias"][str(h)] = {"value": fb, "ci_lo": fb - 0.03, "ci_hi": fb + 0.03}
            for metric, v in (("brier", br), ("fill_rate_bias", fb)):
                er["model_id"].append(m)
                er["metric"].append(metric)
                er["horizon_ns"].append(h)
                er["stratum_kind"].append("all")
                er["stratum"].append("all")
                er["value"].append(v)
                er["ci_lo"].append(v - 0.01)
                er["ci_hi"].append(v + 0.01)
                er["n"].append(int(sel.sum()))
            for d in range(10):
                sd = sel & (dec == d)
                if not sd.any():
                    continue
                v = float(p[sd].mean() - y[sd].mean())
                er["model_id"].append(m)
                er["metric"].append("fill_rate_bias")
                er["horizon_ns"].append(h)
                er["stratum_kind"].append("queue_decile")
                er["stratum"].append(str(d))
                er["value"].append(v)
                er["ci_lo"].append(v - 0.05)
                er["ci_hi"].append(v + 0.05)
                er["n"].append(int(sd.sum()))
        lb.append(
            {
                **e,
                "overfill_bias": {"value": 0.05, "ci_lo": 0.01, "ci_hi": 0.09},
                "qty_bias": 0.04,
                "ttf_log_ratio_median": -0.2,
                "ttf_iad": 0.05,
                "qtrack_mae": 0.1 if with_traj else None,
                "mcnemar": {"b": 10, "c": 30, "p_exact": 0.002},
            }
        )
    # zoo_fills (A5 refinement: crossed_at_live is taker, a later crossed is maker) and zoo_shadows (A2 planned life)
    zf: dict[str, list] = {f.name: [] for f in SCHEMAS["qt.zoo_fills"]}
    for m in MODELS:
        if m.startswith(("nt_prob", "databento")):
            continue
        sel = (mids == m) & (np.array(cols["horizon_ns"]) == H_MAX)
        for k in np.flatnonzero(sel):
            i = (int(k) // len(hs)) % n
            t_fill = cols["model_ts_first_fill"][k]
            if t_fill is None:
                continue
            reason = "crossed_at_live" if i % 7 == 3 else ("crossed" if i % 11 == 5 else "queue")
            zf["model_id"].append(m)
            zf["shadow_id"].append(sids[i])
            zf["fill_seq"].append(1)
            zf["ts_event"].append(int(ts_live[i]) if reason == "crossed_at_live" else int(t_fill))
            zf["fill_qty"].append(100.0)
            zf["cum_qty"].append(100.0)
            zf["queue_ahead_est_before"].append(0.0)
            zf["reason"].append(reason)
            zf["quirk"].append("none")
    files.append(write(root, "zoo_fills.parquet", "qt.zoo_fills", zf))
    admin = np.where(cens_t < big, ts_live + cens_t, ts_live + 6 * 3600 * 10**9)
    ts_type = pa.timestamp("ns", tz="UTC")
    zs = pa.table(
        {
            "shadow_id": pa.array(sids, pa.utf8()),
            "ts_cancel_planned": pa.array(
                [int(ts_live[i] + cancel_rel[i]) if cancel_rel[i] < big else None for i in range(n)], ts_type
            ),
            "ts_admin_end": pa.array([int(v) for v in admin], ts_type),
            "instrument_id": pa.array([int(v) for v in inst], pa.uint32()),
            "session_date": pa.array([DAY] * n, pa.date32()),
            "ts_live": pa.array([int(v) for v in ts_live], ts_type),
            "session_close": pa.array([int(ts_live[i] + 6 * 3600 * 10**9) for i in range(n)], ts_type),
            "exact_end_state": pa.array(["filled" if truth_f[i] else "open_at_end" for i in range(n)], pa.utf8()),
            "never_cancel": pa.array([bool(cancel_rel[i] >= H_MAX) for i in range(n)], pa.bool_()),
        }
    ).replace_schema_metadata({"qt.schema": "qt.zoo_shadows", "qt.schema_version": "1", "qt.engine_version": "0.1.0"})
    files.append(write_table(zs, root / "zoo_shadows.parquet"))
    traj_table = None
    if with_traj:
        tr: dict[str, list] = {f.name: [] for f in SCHEMAS["qt.zoo_trajectory"]}
        for i in range(min(n, 40)):
            for m in MODELS[:3]:
                for j in range(8):
                    tr["model_id"].append(m)
                    tr["shadow_id"].append(sids[i])
                    tr["ts_event"].append(int(ts_live[i] + j * 10**9))
                    tr["queue_ahead_est"].append(float(max(qa[i] - 300 * j, 0)))
                    tr["queue_ahead_exact"].append(int(max(qa[i] - 400 * j, 0)))
        files.append(write(root, "zoo_trajectory.parquet", "qt.zoo_trajectory", tr))
        traj_table = table("qt.zoo_trajectory", tr)
    if with_errors:  # the real evaluation (queuetruth.eval, audit rulings A2 and A10)
        from queuetruth.eval import evaluate_zoo

        errs, lb = evaluate_zoo(
            table("qt.zoo_predictions", cols),
            h_max_ns=H_MAX,
            trajectory=traj_table,
            bootstrap_b=40,
            seed=0,
            planned_life=zs.select(["shadow_id", "ts_cancel_planned", "ts_admin_end"]),
        )
        files.append(write_table(errs, root / "zoo_errors.parquet", SCHEMAS["qt.zoo_errors"]))
    z = ZooSummary(
        run_id="0f1e2d3c4b5a6978",
        spec={
            "dataset": {"path": "data/synth"},
            "shadows": {"path": "shadows.parquet"},
            "mode": "faithful",
            "exchange": "partial",
            "horizons_ns": HORIZONS,
            "h_max_ns": H_MAX,
            "particles": 100,
            "l2": {
                "depth_levels": 10,
                "cadence_ns": 0,
                "trade_position": "before",
                "include_counts": True,
                "emit_hidden_trades": True,
            },
        },
        n_shadows=n,
        models=MODELS,
        leaderboard=lb,
        files=files,
        warnings=[],
    )
    write_doc(root / "summary.json", z)
    return {"n": n, "sids": sids, "ts_live": ts_live, "qa": qa}


# ---- calibration -------------------------------------------------------------------------------------------------


def profile_fit(ev: dict[str, Any]) -> tuple[float, float, float]:
    """(n_hat, profile CI lo, hi) of power3 allocation events on a fine grid (as `qt_calib` publishes them)."""
    from queuetruth.report.compute import alloc_profile
    from queuetruth.report.sections_models import _crossing

    prof = alloc_profile("power3", ev["front"], ev["back"], ev["ahead_cancel"], ev["behind_cancel"], (-3.0, 4.0, 7001))
    lmax = max(v for _, v in prof)
    lo, hi = _crossing([(t, v - lmax) for t, v in prof], -3.841459 / 2)
    t_hat = max(prof, key=lambda tv: tv[1])[0]
    assert lo is not None and hi is not None
    return math.exp(t_hat), math.exp(lo), math.exp(hi)


def make_cal_dir(root: Path, seed: int = 3, n_true: float = 2.0, with_fit_json: bool = True) -> dict[str, Any]:
    """A `calibrate alloc` output directory (power3) with allocation events drawn from n_true."""
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)
    m = 4000
    front = rng.uniform(1, 500, m)
    back = rng.uniform(1, 500, m)
    pi = 1.0 - (front / (front + back)) ** n_true
    tot = rng.integers(1, 20, m)
    behind = rng.binomial(tot, pi).astype(float)
    ev = {
        "shadow_id": [f"s{i % 50:05d}" for i in range(m)],
        "instrument_id": [1] * m,
        "session_date": [DAY] * m,
        "ts_event": (OPEN + np.arange(m) * 10**8).tolist(),
        "front": front.tolist(),
        "back": back.tolist(),
        "ahead_cancel": (tot - behind).tolist(),
        "behind_cancel": behind.tolist(),
        "l2_prev": [None] * m,
        "l2_new": [None] * m,
        "l2_trades": [None] * m,
    }
    files = [write(root, "alloc_events.parquet", "qt.alloc_events", ev)]
    est, ci_lo, ci_hi = profile_fit(ev)  # the published estimate / profile CI of these (train-day) events
    hs = HORIZONS
    snippet = (
        "# queuetruth 0.1.0 | calibration 1111222233334444 | power3 fitted on 2019-12-30; held-out 2019-12-31\n"
        "from hftbacktest import BacktestAsset\nasset = (\n    BacktestAsset()\n"
        "    .power_prob_queue_model3(2.01)\n    .partial_fill_exchange()\n)\n"
    )
    common = dict(
        run_id="1111222233334444",
        spec={"kind": "alloc", "family": "power3", "grid": {"alloc": {"lo": -3.0, "hi": 4.0, "n": 141}}},
        kind="alloc",
        family="power3",
        mode="faithful",
        param_name="n",
        estimate=round(est, 4),
        ci_profile=(ci_lo, ci_hi),
        ci_bootstrap=(1.8, 2.25),
        loglik=-12345.6,
        n_events=m,
        n_shadows=50,
        train={"dates": ["2019-12-30"]},
        test={"dates": ["2019-12-31"]},
        stratified=[
            {"stratum_kind": "instrument", "stratum": "1", "estimate": 2.0, "ci_lo": 1.8, "ci_hi": 2.2, "n": m}
        ],
        data_sources=[
            {"dataset_id": "ab" * 32, "venue": "cme", "source_venue": "cme", "validation_status": "synthetic_only"}
        ],
        warnings=[],
    )
    ho = {
        "brier": {str(h): 0.1 + 0.01 * i for i, h in enumerate(hs)},
        "logloss": {str(h): 0.3 + 0.01 * i for i, h in enumerate(hs)},
        "fill_rate_bias": {str(h): 0.01 for h in hs},
    }
    base = {
        "brier": {str(h): 0.12 + 0.012 * i for i, h in enumerate(hs)},
        "logloss": {str(h): 0.33 + 0.01 * i for i, h in enumerate(hs)},
        "fill_rate_bias": {str(h): 0.05 for h in hs},
    }
    cal = CalibrationResult(
        **common,
        held_out={"hbt_power3[exch=partial;mode=faithful;n=2.01]": ho},
        baseline_held_out={"hbt_power3[exch=partial;mode=faithful;n=2]": base},
        exports={"hftbacktest": {"python": snippet, "notes": ["hbt #316/#312 partial-fill accounting"]}},
    )
    write_doc(root / "calibration.json", cal)
    if with_fit_json:
        fit = CalibrationResult(**common, held_out={}, baseline_held_out={}, exports={})
        write_doc(root / "fit.json", fit)
    return {"files": files, "snippet": snippet}


# ---- learned model -----------------------------------------------------------------------------------------------


def make_hazard_dir(root: Path, seed: int = 4) -> None:
    """A `fit-hazard` output directory (model card + predictions)."""
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)
    grid = [10**8, 10**9, 10**10, 6 * 10**10]
    n = 300
    cols: dict[str, list] = {f.name: [] for f in SCHEMAS["qt.hazard_predictions"]}
    for i in range(n):
        base = rng.random()
        for j, h in enumerate(grid):
            cif = min(base * (j + 1) / len(grid), 1.0)
            cens = bool(rng.random() < 0.05)
            cols["order_ref"].append(f"s{i:05d}")
            cols["landmark_ns"].append(0)
            cols["horizon_ns"].append(h)
            cols["cif_fill_raw"].append(cif)
            cols["cif_fill"].append(cif)
            cols["cif_cancel"].append(0.1)
            cols["survival"].append(max(1 - cif - 0.1, 0))
            cols["y_fill"].append(None if cens else bool(rng.random() < cif))
            cols["censored"].append(cens)
            cols["split"].append(["train", "calib", "test"][i % 3])
    files = [write(root, "predictions.parquet", "qt.hazard_predictions", cols)]
    hm = {str(h): 0.1 + 0.01 * j for j, h in enumerate(grid)}
    card = HazardModelCard(
        model_id="learned_hazard",
        format_version=1,
        spec={"features": {"path": "features"}, "seed": 0},
        grid_ns=grid,
        causes=["fill", "cancel"],
        feature_names=["dist_ticks", "level_qty"],
        privileged=False,
        split={"scheme": "blocks", "train": [0, 1, 2], "calib": [3], "test": [4]},
        n_orders={"train": 100, "calib": 100, "test": 100},
        n_rows_train=400,
        scores_test={
            "rcll": 1.23,
            "ipcw_logloss": hm,
            "brier": hm,
            "bss_vs_climatology": {
                str(h): {"value": 0.1 + 0.02 * j, "ci_lo": 0.02, "ci_hi": 0.2 + 0.02 * j} for j, h in enumerate(grid)
            },
            "ibs": 0.11,
            "calib_slope": {str(h): 1.0 for h in grid},
            "calib_intercept": {str(h): 0.0 for h in grid},
            "spiegelhalter_z": {str(h): 0.5 for h in grid},
            "hl_p": {str(h): 0.4 for h in grid},
            "c_index_uno": 0.71,
            "auc": {str(h): 0.7 for h in grid},
        },
        zoo_comparison={
            MODELS[1]: {"brier": {str(h): 0.12 + 0.01 * j for j, h in enumerate(grid)}},
            MODELS[2]: {"brier": {str(h): 0.15 + 0.01 * j for j, h in enumerate(grid)}},
        },
        recalibration="isotonic",
        files=files,
        warnings=[],
    )
    write_doc(root / "model.json", card)


# ---- replay ------------------------------------------------------------------------------------------------------


def make_replay_dir(
    root: Path, sids: list[str], ts_live: np.ndarray, qa: np.ndarray, status: str = "synthetic_only"
) -> None:
    """A `replay` output directory with a summary, data quality, queue trajectories and the dataset manifest."""
    root.mkdir(parents=True, exist_ok=True)
    tr: dict[str, list] = {f.name: [] for f in SCHEMAS["qt.queue_trajectory"]}
    for i, s in enumerate(sids):
        ahead = int(qa[i])
        for j in range(6):
            kind = "live" if j == 0 else ("end" if j == 5 else "event")
            after = max(ahead - 700, 0) if kind == "event" else ahead
            tr["shadow_id"].append(s)
            tr["ts_event"].append(int(ts_live[i] + j * 2 * 10**9))
            tr["sequence"].append(10_000 + j)
            tr["kind"].append(kind)
            tr["queue_ahead_before"].append(ahead)
            tr["queue_ahead_after"].append(after)
            tr["rank_before"].append(ahead // 100)
            tr["rank_after"].append(after // 100)
            tr["level_qty_before"].append(ahead + 300)
            tr["level_qty_after"].append(after + 300)
            tr["cancel_ahead_qty"].append(ahead - after if kind == "event" else 0)
            tr["cancel_behind_qty"].append(50 if kind == "event" else 0)
            tr["add_behind_qty"].append(100 if kind == "event" else 0)
            tr["traded_at_px_qty"].append(0)
            tr["trade_through_qty"].append(0)
            tr["filled_qty"].append(100 if kind == "end" and after == 0 else 0)
            ahead = after
    files = [write(root, "queue_trajectory.parquet", "qt.queue_trajectory", tr)]
    q = dq("warnings")
    write_doc(root / "data_quality.json", q)
    rep = ReplaySummary(
        run_id="9999888877776666",
        engine_version="0.1.0",
        spec={"dataset": {"path": "data/synth"}, "rank_oracle": True},
        stats={
            "records": 200_000,
            "events": 150_000,
            "trades": 9000,
            "instruments": 2,
            "partitions": 4,
            "snapshot_records": 120,
        },
        invariants={
            "cancels_exceeding_resting": 0,
            "fills_exceeding_resting": 0,
            "fills_unknown_order": 0,
            "unknown_order_refs": 3,
            "duplicate_adds": 0,
            "unexpected_modifies": 0,
            "crossed_books_after_last": 0,
            "negative_spreads": 0,
        },
        rank_oracle={
            "executions_checked": 9000,
            "executions_unknown_order": 0,
            "price_priority_violations": 0,
            "fifo_rank_histogram": [8500, 300, 100, 50, 30, 20],
            "fifo_rank_histogram_sweep_adjusted": [8900, 60, 20, 10, 6, 4],
            "rank0_share": 0.944,
            "rank0_share_sweep_adjusted": 0.989,
            "sweeps": 800,
            "sweeps_multi_fill": 120,
            "sweep_gap_ns": 2_000_000,
            "first_60s_after_open": {
                "checked": 100,
                "rank_violations": 3,
                "rank_violations_sweep_adjusted": 0,
                "rank0_share": 0.97,
                "rank0_share_sweep_adjusted": 1.0,
            },
            "rank0_share_by_bucket": {
                "09:30": {
                    "checked": 100,
                    "rank0": 97,
                    "rank0_sweep_adjusted": 100,
                    "rank0_share": 0.97,
                    "rank0_share_sweep_adjusted": 1.0,
                }
            },
        },
        parity={
            "trades_compared": 9000,
            "trades_mismatches": 0,
            "trades_missing_derived": 0,
            "trades_extra_derived": 0,
            "bbo_compared": 0,
            "bbo_mismatches": 0,
            "first_mismatches": [],
            "pass": True,
        },
        shadows={
            "n": len(sids),
            "n_live": len(sids),
            "n_filled": 10,
            "n_partial_only": 0,
            "n_cancelled": 5,
            "n_censored": 1,
            "n_rejected": 0,
            "fills_rows": 10,
            "trajectory_rows": 6 * len(sids),
        },
        data_quality=q,
        files=files,
        warnings=[],
    )
    write_doc(root / "summary.json", rep)
    man = DatasetManifest(
        dataset_id="cd" * 32,
        kind="mbo",
        source_format="testkit",
        venue="cme",
        source_venue="cme",
        inputs=[],
        partitions=[],
        price_exp_by_instrument={"1": -9, "2": -9},
        created_by="queuetruth-testkit 0.1.0",
        validation_status=status,
    )
    write_doc(root / "manifest.json", man)


@dataclass
class Bundle:
    """Paths of a full synthetic run set."""

    root: Path
    score: Path
    zoo: Path
    cal: Path
    hazard: Path
    replay: Path
    facts: dict[str, Any]

    @property
    def inputs(self) -> list[str]:
        return [str(p) for p in (self.score, self.zoo, self.cal, self.hazard, self.replay)]


def make_bundle(root: Path) -> Bundle:
    """Every report input kind under `root`."""
    sc = make_score_dir(root / "score")
    zf = make_zoo_dir(root / "zoo")
    make_cal_dir(root / "cal")
    make_hazard_dir(root / "hazard")
    make_replay_dir(root / "replay", zf["sids"], zf["ts_live"], zf["qa"])
    return Bundle(
        root, root / "score", root / "zoo", root / "cal", root / "hazard", root / "replay", {"score": sc, "zoo": zf}
    )


def json_load(p: Path) -> Any:
    """Reads JSON."""
    return json.loads(p.read_text(encoding="utf-8"))


def sha256(p: Path) -> str:
    """sha256 of a file."""
    return sha256_file(p)


__all__ = ["esc", "json"]
