"""Synthetic `qt.features` tables for the hazard-model tests.

Each order rests at the touch (or 1 tick away) from `t_0`; its fill hazard (per second) is
`base_fill * exp(beta_imb * imb_l1 + beta_queue * (1 - queue_share_ra) - 0.7 * dist_ticks)`, its cancel hazard a
constant `cancel_rate`, and it is censored at the session end or by an independent exponential censoring. Rows are
written for every landmark at which the order still rests; `event_ns` is capped at `h_max_ns` (cause `censor` at
the cap). Features other than `elapsed_ns` are the same at every landmark (the process is memoryless).
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pyarrow as pa

from queuetruth.schemas.arrow import features_schema

NS = 1_000_000_000
OPEN_UTC_S = 14 * 3600 + 30 * 60
CLOSE_UTC_S = 21 * 3600
FEATURES = [
    "dist_ticks",
    "spread_ticks",
    "level_qty",
    "own_qty_lots",
    "queue_share_ra",
    "elapsed_ns",
    "imb_l1",
    "noise_a",
    "noise_b",
    "tod_sin",
    "tod_cos",
]
EPOCH = dt.date(1970, 1, 1)


def make_features(
    seed: int = 0,
    *,
    dates: tuple[str, ...] = ("2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"),
    orders_per_day: int = 600,
    landmarks_ns: tuple[int, ...] = (0, 1 * NS, 5 * NS),
    h_max_ns: int = 60 * NS,
    base_fill: float = 0.04,
    beta_imb: float = 1.6,
    beta_queue: float = 2.2,
    cancel_rate: float = 0.03,
    random_censor_rate: float = 0.002,
    privileged: bool = False,
) -> pa.Table:
    """A `qt.features` table (population shadows) with the process of the module docstring."""
    rng = np.random.default_rng(seed)
    cols: dict[str, list] = {
        k: []
        for k in (
            "order_ref",
            "instrument_id",
            "session_date",
            "ts_landmark",
            "landmark_ns",
            "side_sign",
            "event_ns",
            "cause",
        )
    }
    feats: dict[str, list] = {k: [] for k in [*FEATURES, *(["p_f_exact"] if privileged else [])]}
    for d in dates:
        day = (dt.date.fromisoformat(d) - EPOCH).days
        midnight = day * 86_400 * NS
        open_ns, close_ns = midnight + OPEN_UTC_S * NS, midnight + CLOSE_UTC_S * NS
        n = orders_per_day
        t0 = np.sort(rng.integers(open_ns, close_ns - 1, size=n))
        inst = rng.integers(1, 3, size=n)
        side = rng.choice([-1, 1], size=n)
        imb = rng.uniform(-1, 1, size=n)
        q = rng.beta(2.0, 2.0, size=n)
        dist = rng.choice([0, 0, 0, 0, 0, 0, 0, 1, 1, -1], size=n).astype(float)
        spread = rng.choice([1.0, 1.0, 2.0], size=n)
        lvl = np.round(rng.lognormal(6.0, 0.8, size=n))
        own = rng.integers(1, 4, size=n).astype(float)
        na, nb = rng.normal(size=n), rng.normal(size=n)
        lam_f = base_fill * np.exp(beta_imb * imb + beta_queue * (1.0 - q) - 0.7 * dist)
        t_fill = rng.exponential(1.0 / lam_f)
        t_cancel = rng.exponential(1.0 / cancel_rate, size=n)
        t_cens = np.minimum((close_ns - t0) / NS, rng.exponential(1.0 / random_censor_rate, size=n))
        life = np.minimum(np.minimum(t_fill, t_cancel), t_cens)
        cause = np.where(life == t_fill, "fill", np.where(life == t_cancel, "cancel", "censor"))
        frac = np.clip((t0 - open_ns) / (close_ns - open_ns), 0, 1)
        for i in range(n):
            for lm in landmarks_ns:
                rem_ns = round(life[i] * NS) - lm
                if rem_ns <= 0:
                    break
                if rem_ns >= h_max_ns:
                    e_ns, c = h_max_ns, "censor"
                else:
                    e_ns, c = rem_ns, str(cause[i])
                cols["order_ref"].append(f"s{d}-{i:05d}")
                cols["instrument_id"].append(int(inst[i]))
                cols["session_date"].append(day)
                cols["ts_landmark"].append(int(t0[i]) + lm)
                cols["landmark_ns"].append(lm)
                cols["side_sign"].append(int(side[i]))
                cols["event_ns"].append(e_ns)
                cols["cause"].append(c)
                f = feats
                f["dist_ticks"].append(dist[i])
                f["spread_ticks"].append(spread[i])
                f["level_qty"].append(lvl[i])
                f["own_qty_lots"].append(own[i])
                f["queue_share_ra"].append(q[i])
                f["elapsed_ns"].append(float(lm))
                f["imb_l1"].append(imb[i])
                f["noise_a"].append(na[i])
                f["noise_b"].append(nb[i])
                f["tod_sin"].append(math.sin(2 * math.pi * frac[i]))
                f["tod_cos"].append(math.cos(2 * math.pi * frac[i]))
                if privileged:
                    f["p_f_exact"].append(q[i] * lvl[i])
    names = list(feats)
    schema = features_schema(names)
    data = {
        "order_ref": pa.array(cols["order_ref"], pa.utf8()),
        "population": pa.array(["shadows"] * len(cols["order_ref"])).dictionary_encode(),
        "instrument_id": pa.array(cols["instrument_id"], pa.uint32()),
        "session_date": pa.array(cols["session_date"], pa.int32()).cast(pa.date32()),
        "ts_landmark": pa.array(cols["ts_landmark"], pa.int64()).cast(pa.timestamp("ns", tz="UTC")),
        "landmark_ns": pa.array(cols["landmark_ns"], pa.int64()),
        "side_sign": pa.array(cols["side_sign"], pa.int8()),
        "event_ns": pa.array(cols["event_ns"], pa.int64()),
        "cause": pa.array(cols["cause"]).dictionary_encode(),
    }
    for k in names:
        data[k] = pa.array(feats[k], pa.float64())
    return pa.Table.from_arrays([data[f.name].cast(f.type) for f in schema], schema=schema)


def tiny_features(rows: list[tuple[str, str, int, int, int, str]], feature: list[float] | None = None) -> pa.Table:
    """Hand-built `qt.features` rows `(order_ref, date, ts_landmark_ns, landmark_ns, event_ns, cause)` with one
    feature column `x` (default 0.0)."""
    schema = features_schema(["x"])
    n = len(rows)
    days = [(dt.date.fromisoformat(r[1]) - EPOCH).days for r in rows]
    data = {
        "order_ref": pa.array([r[0] for r in rows], pa.utf8()),
        "population": pa.array(["shadows"] * n).dictionary_encode(),
        "instrument_id": pa.array([1] * n, pa.uint32()),
        "session_date": pa.array(days, pa.int32()).cast(pa.date32()),
        "ts_landmark": pa.array([r[2] for r in rows], pa.int64()).cast(pa.timestamp("ns", tz="UTC")),
        "landmark_ns": pa.array([r[3] for r in rows], pa.int64()),
        "side_sign": pa.array([1] * n, pa.int8()),
        "x": pa.array(feature if feature is not None else [0.0] * n, pa.float64()),
        "event_ns": pa.array([r[4] for r in rows], pa.int64()),
        "cause": pa.array([r[5] for r in rows]).dictionary_encode(),
    }
    return pa.Table.from_arrays([data[f.name].cast(f.type) for f in schema], schema=schema)
