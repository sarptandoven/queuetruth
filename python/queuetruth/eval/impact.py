"""Impact and queue-share warnings for the independent-shadow assumption (docs/concepts/impact-warning.md).

Shadows never touch the book (SPEC 1.2): the replay treats every shadow as if its presence changed nothing. This
module measures, per shadow, how doubtful that is and writes `qt.shadow_impact` (one row per shadow) plus a
`qt.shadow_impact_summary` document with counts at documented thresholds and one warning line for zoo and score
outputs when the doubtful share is material. It never changes a fill.

Per shadow (dataset fixed-point units; `Q = qty`, the posted size):

- Session: the `qt.instruments` row of the instrument with `midnight_utc_ns <= ts_live < midnight_utc_ns + 1 day`
  (the latest such row); regular session `[midnight + open_s, midnight + close_s]`. Without one: the UTC day of
  `ts_live`, session = the whole day, tick size unknown.
- `status`: `rejected` for shadows that never rested (replay `end_state` `rejected_*`, zoo `exact_end_state`
  `rejected*`); `out_of_session` when live outside the regular session (`ts_live < open` or `>= close`, or the replay's
  `out_of_session`); else `live`. Only `live` shadows are assessed and flagged. Rejected shadows carry no figures;
  out-of-session shadows carry queue and volume shares for information but no impact (the daily figures describe the
  regular session).
- `daily_volume V`: the venue's traded volume of the instrument in the session (every `kind`). `sigma_daily`: square
  root of the realized variance of previous-tick log trade prices on the grid `open, open + rv_interval_ns, ...,
  close` over non-`cross` trades in the session, the price at grid points before the first such trade being the
  opening cross print (when one prints in `[open, first trade]`) or else the first trade, so the opening interval
  counts; null with fewer than `min_returns` returns (returns ending before the first trade are not counted) or with
  a non-positive price in the session (log returns undefined, e.g. calendar spreads). A `daily` table
  (`instrument_id`, `session_date`, `daily_volume`, `sigma_daily`, each figure nullable) overrides either figure per
  instrument-day; `volume_source` is `user` when it supplied `V`.
- `impact_frac = Y * sigma_daily * sqrt(Q / V)` for `Q / V >= linear_below`, continued linearly below it;
  `impact_bps = 1e4 * impact_frac`, `impact_ticks = impact_frac * ref_price / tick_size` with `ref_price` the
  shadow's price, else the last non-cross trade at or before `ts_live`.
- `queue_share_at_live = Q / (level_qty_at_live + Q)` (`level_qty_at_live` excludes the shadow; zoo rows without an
  exact level use `visible_level_qty_at_live`). `queue_share_max`: the largest `leaves / (level_qty_after + leaves)`
  over the shadow's `qt.queue_trajectory` rows and the at-live share (looks ahead: information only, never flagged).
- Life `(ts_live, ts_end]`. Replay: the full fill, else the trajectory `end` row, else the earlier of the effective
  cancel and the planned end, else the session close (`session_close`). Zoo: the earliest of the planned cancel, the
  administrative end and `ts_live + horizon`, `horizon` = the largest prediction horizon `<= h_max_ns` (the horizon
  `filled_qty = truth_filled_qty` is read at). `life_volume` = the instrument's non-cross volume in the life.
  `volume_share = filled_qty / (filled_qty + life_volume)`; with `fills_in_tape` (a self-excluded twin's executions
  are the shadow's fills and are already in the trades) `min(filled_qty / life_volume, 1)`. Null without a filled
  quantity (zoo runs on L2 datasets) or when the denominator is 0.

Flags (`ImpactConfig`, inclusive), live shadows only: `flag_impact` = `impact_ticks >= impact_ticks` with
`volume_source = user` (venue volume only is an upper bound, counted in the summary as `n_impact_venue_bound`;
`flag_venue_volume_impact` flags it anyway); `flag_queue` = `queue_share_at_live >= queue_share` and `qty >=
queue_min_qty` (default the instrument's round lot: 100 shares on US equity venues, else `lot_size`); `flag_volume`
= `volume_share >= volume_share`; `material` = any. A run is material when `n_material > 0` and `n_material /
n_assessed >= warn_fraction`: if every flagged shadow's outcome flipped, a fill rate over the assessed shadows would
move by at most that share.

Rows are sorted by `shadow_id`; every figure is a pure function of the inputs. Table metadata: the SPEC 3.4 keys plus
`qt.shadow_impact.source` (`replay` or `zoo`), `qt.shadow_impact.volume_source` (`venue_session`, `user`, `mixed`),
and for zoo inputs `qt.shadow_impact.h_max_ns` and `qt.shadow_impact.horizon_ns`.

The callers' hooks (`zoo_spec_warnings`, `score_warnings`, `score_zoo_warnings`, `replay_warnings`, `zoo_warnings`)
are fail-soft: an input they cannot assess gives one `impact: not assessed (<code>: <message>)` line instead of
failing the run.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import math
import sys
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import ValidationError

from queuetruth.errors import QtError
from queuetruth.eval import _cols as C
from queuetruth.schemas import SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.schemas.models import (
    FileEntry,
    ImpactConfig,
    ImpactDay,
    ImpactDistribution,
    ImpactSensitivity,
    ShadowImpactSummary,
)

NS = 1_000_000_000
DAY_NS = 86_400 * NS

TABLE = "qt.shadow_impact"
SUMMARY = ShadowImpactSummary.SCHEMA
#: Arrow schema of `qt.shadow_impact` (`queuetruth.schemas.SCHEMAS[TABLE]`).
SCHEMA: pa.Schema = SCHEMAS[TABLE]
META_SOURCE = "qt.shadow_impact.source"
META_VOLUME_SOURCE = "qt.shadow_impact.volume_source"
META_H_MAX = "qt.shadow_impact.h_max_ns"
META_HORIZON = "qt.shadow_impact.horizon_ns"
#: Venues (`venue` or `source_venue` of `qt.instruments`) whose round lot is 100 shares.
US_EQUITY_VENUES = frozenset({"nasdaq", "iex"})
US_ROUND_LOT_SHARES = 100
REJECTED_END_STATES = frozenset({"rejected_would_cross", "rejected_no_touch"})

#: Thresholds at which the summary also counts assessed shadows (sensitivity of the defaults). `impact_ticks` counts
#: every evaluable estimate, whatever its volume source.
SENSITIVITY: dict[str, tuple[float, ...]] = {
    "impact_ticks": (0.25, 0.5, 1.0, 2.0, 5.0),
    "queue_share": (0.01, 0.05, 0.10, 0.25, 0.50),
    "volume_share": (0.01, 0.05, 0.10, 0.25),
}
#: Exceptions the callers' hooks turn into one `impact: not assessed` line.
SOFT_ERRORS: tuple[type[BaseException], ...] = (
    QtError,
    OSError,
    pa.ArrowException,
    ValueError,
    TypeError,
    KeyError,
    MemoryError,
)

_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar("qt_impact_suppressed", default=False)

__all__ = [
    "SCHEMA",
    "SENSITIVITY",
    "TABLE",
    "ImpactConfig",
    "ShadowImpactSummary",
    "dataset_instruments",
    "dataset_trades",
    "impact_frac",
    "main",
    "realized_sigma",
    "replay_warnings",
    "score_warnings",
    "score_zoo_warnings",
    "shadow_impact",
    "summarize",
    "suppressed",
    "warning_line",
    "write",
    "zoo_shadow_impact",
    "zoo_spec_warnings",
    "zoo_warnings",
]


# ---- inputs -----------------------------------------------------------------------------------------------------


@dataclass
class _Orders:
    """Normalized per-shadow inputs (one entry per shadow, any order)."""

    sid: list[str]
    iid: np.ndarray
    qty: np.ndarray
    filled: np.ndarray
    filled_ok: np.ndarray
    ts_live: np.ndarray
    ts_end: np.ndarray  # -1 = not known yet (session close)
    end_source: list[str]
    price: np.ndarray
    price_ok: np.ndarray
    level: np.ndarray
    level_ok: np.ndarray
    qmax: np.ndarray  # NaN = no trajectory
    rejected: np.ndarray
    oos: np.ndarray  # out of session according to the input
    in_tape: np.ndarray


def _config(config: ImpactConfig | dict[str, Any] | None) -> ImpactConfig:
    if config is None:
        return ImpactConfig()
    if isinstance(config, ImpactConfig):
        return config
    try:
        return ImpactConfig.model_validate(config)
    except ValidationError as e:
        raise QtError("QT-CONFIG-001", f"impact config: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}") from e


def _unique(sid: list[str], what: str) -> None:
    seen: set[str] = set()
    for s in sid:
        if s in seen:
            raise QtError("QT-INPUT-003", f"{what}: duplicate shadow_id {s!r}")
        seen.add(s)


def _no_nulls(table: pa.Table, names: list[str], what: str) -> None:
    for name in names:
        if table[name].null_count:
            raise QtError("QT-INPUT-002", f"{what}: column {name} has {table[name].null_count} null value(s)")


def _in_tape(sid: list[str], fills_in_tape: bool | Collection[str]) -> np.ndarray:
    if isinstance(fills_in_tape, bool):
        return np.full(len(sid), fills_in_tape, dtype=bool)
    keep = {str(s) for s in fills_in_tape}
    return np.array([s in keep for s in sid], dtype=bool)


@dataclass
class _Trades:
    """Trades of one instrument sorted by (ts_event, sequence), with the continuous (non-cross) subset."""

    ts: np.ndarray
    size: np.ndarray
    price: np.ndarray
    cross: np.ndarray
    cts: np.ndarray  # continuous trades only
    csize_cum: np.ndarray  # len(cts) + 1, cumulative sizes
    cprice: np.ndarray


def _trades_by_instrument(trades: pa.Table) -> dict[int, _Trades]:
    C.require(trades, ["ts_event", "instrument_id", "price", "size"], "trades")
    keys = [("instrument_id", "ascending"), ("ts_event", "ascending")]
    if C.has(trades, "sequence"):
        keys.append(("sequence", "ascending"))  # sorted natively: uint64 sequences above 2^63 stay ordered
    t = trades.take(pc.sort_indices(trades.select([k for k, _ in keys]), sort_keys=keys))
    iid, _ = C.ints(t, "instrument_id")
    ts, _ = C.ints(t, "ts_event")
    size, _ = C.ints(t, "size")
    price, _ = C.ints(t, "price")
    cross = (C.strings(t, "kind") == "cross") if C.has(t, "kind") else np.zeros(len(ts), dtype=bool)
    out: dict[int, _Trades] = {}
    for k in np.unique(iid):
        lo, hi = np.searchsorted(iid, k, "left"), np.searchsorted(iid, k, "right")
        tt, s, p, x = ts[lo:hi], size[lo:hi], price[lo:hi], cross[lo:hi]
        keep = ~x
        out[int(k)] = _Trades(
            ts=tt,
            size=s,
            price=p,
            cross=x,
            cts=tt[keep],
            csize_cum=np.concatenate([[0], np.cumsum(s[keep], dtype=np.int64)]),
            cprice=p[keep],
        )
    return out


@dataclass(frozen=True)
class _Session:
    session_date: int
    open_ns: int
    close_ns: int
    tick: int | None
    known: bool  # from an instrument row (False: the UTC day)


def _days(sd: Any) -> int:
    return int(sd.toordinal() - 719_163) if hasattr(sd, "toordinal") else int(sd)


def _round_lot(row: dict[str, Any]) -> int:
    """100 shares on US equity venues (in the instrument's size units), else `lot_size` (at least 1)."""
    venues = {str(row.get("venue") or "").lower(), str(row.get("source_venue") or "").lower()}
    if venues & US_EQUITY_VENUES:
        exp = int(row.get("size_exp") or 0)
        return max(1, round(US_ROUND_LOT_SHARES / 10.0**exp))
    lot = row.get("lot_size")
    return max(1, int(lot)) if lot is not None else 1


def _sessions(instruments: pa.Table | None) -> tuple[dict[int, list[tuple[int, _Session]]], dict[int, int]]:
    """(instrument_id -> [(midnight_utc_ns, session)] sorted by midnight, instrument_id -> round lot)."""
    out: dict[int, list[tuple[int, _Session]]] = {}
    lots: dict[int, int] = {}
    if instruments is None:
        return out, lots
    C.require(instruments, ["instrument_id", "session_date", "midnight_utc_ns"], "instruments")
    for r in instruments.to_pylist():
        mid = int(r["midnight_utc_ns"])
        open_s, close_s, tick = r.get("open_s"), r.get("close_s"), r.get("tick_size")
        sess = _Session(
            session_date=_days(r["session_date"]),
            open_ns=mid + int(open_s if open_s is not None else 0) * NS,
            close_ns=mid + int(close_s if close_s is not None else 86_400) * NS,
            tick=int(tick) if tick is not None and int(tick) > 0 else None,
            known=True,
        )
        iid = int(r["instrument_id"])
        out.setdefault(iid, []).append((mid, sess))
        lots[iid] = _round_lot(r)
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out, lots


def _session_of(sessions: dict[int, list[tuple[int, _Session]]], iid: int, ts: int) -> _Session:
    """The instrument row whose local day contains `ts` (`midnight <= ts < midnight + 1 day`, latest), else the UTC
    day of `ts`."""
    v = sessions.get(iid)
    if v:
        mids = [m for m, _ in v]
        i = int(np.searchsorted(mids, ts, "right")) - 1
        if i >= 0 and ts < v[i][0] + DAY_NS:
            return v[i][1]
    d = ts // DAY_NS
    return _Session(int(d), int(d * DAY_NS), int((d + 1) * DAY_NS), None, False)


def realized_sigma(
    ts: np.ndarray,
    price: np.ndarray,
    open_ns: int,
    close_ns: int,
    interval_ns: int,
    seed_price: int | None = None,
) -> tuple[float | None, int]:
    """(sqrt of the realized variance, number of returns) of previous-tick log prices on the grid `open, open +
    interval, ..., close` from the trades (`ts` ascending) inside `[open, close]`. Grid points before the first trade
    take `seed_price` (the opening cross) or else the first trade's price, so the opening interval counts; returns
    that end before the first trade are not counted. `(None, 0)` when a price in the window (or the seed) is not
    positive; `(0.0, 0)` without trades in the window."""
    if close_ns <= open_ns:
        return 0.0, 0
    lo, hi = np.searchsorted(ts, open_ns, "left"), np.searchsorted(ts, close_ns, "right")
    t, p = ts[lo:hi], price[lo:hi]
    if len(t) == 0:
        return 0.0, 0
    if np.any(p <= 0) or (seed_price is not None and seed_price <= 0):
        return None, 0
    grid = np.append(np.arange(open_ns, close_ns, interval_ns, dtype=np.int64), np.int64(close_ns))
    idx = np.searchsorted(t, grid, "right") - 1
    first = float(seed_price) if seed_price is not None else float(p[0])
    px = np.where(idx >= 0, p[np.maximum(idx, 0)].astype(float), first)
    r = np.diff(np.log(px))
    counted = idx[1:] >= 0
    return float(math.sqrt(float(np.sum(r * r)))), int(np.sum(counted))


def _from_results(results: pa.Table, trajectory: pa.Table | None, fills_in_tape: bool | Collection[str]) -> _Orders:
    C.require(results, ["shadow_id", "instrument_id", "qty", "filled_qty", "ts_live"], "shadow_results")
    _no_nulls(results, ["shadow_id", "instrument_id", "qty", "filled_qty", "ts_live"], "shadow_results")
    sid = [str(s) for s in results["shadow_id"].to_pylist()]
    _unique(sid, "shadow_results")
    n = len(sid)
    qty, _ = C.ints(results, "qty")
    filled, _ = C.ints(results, "filled_qty")
    ts_live, _ = C.ints(results, "ts_live")

    def opt(name: str) -> tuple[np.ndarray, np.ndarray]:
        if C.has(results, name):
            return C.ints(results, name)
        return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=bool)

    full, full_ok = opt("ts_full_fill")
    canc, canc_ok = opt("ts_cancel_effective")
    plan, plan_ok = opt("ts_planned_end")
    price, price_ok = opt("price")
    level, level_ok = opt("level_qty_at_live")
    rejected = (
        np.array([s in REJECTED_END_STATES for s in C.strings(results, "end_state")], dtype=bool)
        if C.has(results, "end_state")
        else np.zeros(n, dtype=bool)
    )
    oos = C.bools(results, "out_of_session")[0] if C.has(results, "out_of_session") else np.zeros(n, dtype=bool)
    traj_end: dict[str, int] = {}
    qmax = np.full(n, np.nan)
    if trajectory is not None and trajectory.num_rows:
        C.require(trajectory, ["shadow_id", "ts_event", "kind", "level_qty_after", "filled_qty"], "queue_trajectory")
        pos = {s: i for i, s in enumerate(sid)}
        tsid = C.strings(trajectory, "shadow_id")
        tkind = C.strings(trajectory, "kind")
        tts, _ = C.ints(trajectory, "ts_event")
        tlev, _ = C.ints(trajectory, "level_qty_after")
        tfil, _ = C.ints(trajectory, "filled_qty")
        rows = np.array([pos.get(s, -1) for s in tsid], dtype=np.int64)
        known = rows >= 0
        leaves = np.where(known, qty[np.maximum(rows, 0)] - tfil, 0)
        good = known & (leaves > 0)
        share = np.where(good, leaves / np.maximum(tlev + leaves, 1), np.nan)
        qm = np.full(n, -np.inf)
        np.maximum.at(qm, rows[good], share[good])
        qmax = np.where(np.isfinite(qm), qm, np.nan)
        for s, k, t in zip(tsid, tkind, tts, strict=True):
            if k == "end" and s in pos:
                traj_end[s] = max(traj_end.get(s, int(t)), int(t))
    ts_end = np.full(n, -1, dtype=np.int64)
    src: list[str] = []
    for i, s in enumerate(sid):
        e = "session_close"
        if rejected[i]:
            ts_end[i], e = ts_live[i], "rejected"
        elif full_ok[i]:
            ts_end[i], e = full[i], "full_fill"
        elif s in traj_end:
            ts_end[i], e = traj_end[s], "trajectory_end"
        elif canc_ok[i] and (not plan_ok[i] or canc[i] <= plan[i]):
            ts_end[i], e = canc[i], "cancel"
        elif plan_ok[i]:
            ts_end[i], e = plan[i], "planned_end"
        src.append(e)
    ts_end = np.where(ts_end >= 0, np.maximum(ts_end, ts_live), ts_end)
    return _Orders(
        sid=sid,
        iid=C.ints(results, "instrument_id")[0],
        qty=qty,
        filled=filled,
        filled_ok=np.ones(n, dtype=bool),
        ts_live=ts_live,
        ts_end=ts_end,
        end_source=src,
        price=price,
        price_ok=price_ok,
        level=level,
        level_ok=level_ok,
        qmax=qmax,
        rejected=rejected,
        oos=oos,
        in_tape=_in_tape(sid, fills_in_tape),
    )


def _from_zoo(predictions: pa.Table, zoo_shadows: pa.Table, h_max_ns: int | None) -> tuple[_Orders, int]:
    """Normalized zoo shadows and the largest horizon used as a life."""
    C.require(
        predictions,
        ["shadow_id", "instrument_id", "qty", "horizon_ns", "ts_live", "level_qty_at_live", "truth_filled_qty"],
        "zoo_predictions",
    )
    C.require(zoo_shadows, ["shadow_id", "ts_cancel_planned", "ts_admin_end"], "zoo_shadows")
    _no_nulls(predictions, ["shadow_id", "instrument_id", "qty", "horizon_ns", "ts_live"], "zoo_predictions")
    _no_nulls(zoo_shadows, ["shadow_id"], "zoo_shadows")
    psid = C.strings(predictions, "shadow_id")
    hz, _ = C.ints(predictions, "horizon_ns")
    cap = int(hz.max()) if h_max_ns is None and len(hz) else h_max_ns
    # per shadow: the row with the largest horizon <= cap (its truth is the filled quantity over the life)
    best: dict[str, int] = {}
    for i in np.flatnonzero(hz <= (cap if cap is not None else 0)):
        s = str(psid[i])
        j = best.get(s)
        if j is None or hz[i] > hz[j]:
            best[s] = int(i)
    missing = sorted({str(s) for s in psid} - set(best))
    if missing:
        raise QtError(
            "QT-CONFIG-001",
            f"h_max_ns {cap} is below every prediction horizon of shadow {missing[0]!r} ({len(missing)} shadows)",
        )
    sid = sorted(best)
    p = predictions.take(pa.array([best[s] for s in sid], type=pa.int64()))
    zsid = [str(s) for s in zoo_shadows["shadow_id"].to_pylist()]
    _unique(zsid, "zoo_shadows")
    zpos = {s: i for i, s in enumerate(zsid)}
    absent = [s for s in sid if s not in zpos]
    if absent:
        raise QtError("QT-INPUT-002", f"zoo_shadows: no row for shadow {absent[0]!r} ({len(absent)} missing)")
    z = zoo_shadows.take(pa.array([zpos[s] for s in sid], type=pa.int64()))
    n = len(sid)
    qty, _ = C.ints(p, "qty")
    ts_live, _ = C.ints(p, "ts_live")
    horizon, _ = C.ints(p, "horizon_ns")
    filled, filled_ok = C.ints(p, "truth_filled_qty")
    canc, canc_ok = C.ints(z, "ts_cancel_planned")
    adm, adm_ok = C.ints(z, "ts_admin_end")
    level, level_ok = C.ints(p, "level_qty_at_live")
    if C.has(p, "visible_level_qty_at_live"):
        vis, vis_ok = C.ints(p, "visible_level_qty_at_live")
        level = np.where(level_ok, level, vis)
        level_ok = level_ok | vis_ok
    rejected = (
        np.array([str(s or "").startswith("rejected") for s in C.strings(z, "exact_end_state")], dtype=bool)
        if C.has(z, "exact_end_state")
        else np.zeros(n, dtype=bool)
    )
    ts_end = ts_live + horizon
    src = ["horizon"] * n
    for i in range(n):
        if rejected[i]:
            ts_end[i], src[i] = ts_live[i], "rejected"
            continue
        if canc_ok[i] and canc[i] < ts_end[i]:
            ts_end[i], src[i] = canc[i], "cancel"
        if adm_ok[i] and adm[i] < ts_end[i]:
            ts_end[i], src[i] = adm[i], "admin_end"
    orders = _Orders(
        sid=sid,
        iid=C.ints(p, "instrument_id")[0],
        qty=qty,
        filled=filled,
        filled_ok=filled_ok,
        ts_live=ts_live,
        ts_end=np.maximum(ts_end, ts_live),
        end_source=src,
        price=np.zeros(n, dtype=np.int64),
        price_ok=np.zeros(n, dtype=bool),
        level=level,
        level_ok=level_ok,
        qmax=np.full(n, np.nan),
        rejected=rejected,
        oos=np.zeros(n, dtype=bool),
        in_tape=np.zeros(n, dtype=bool),
    )
    return orders, int(horizon.max()) if n else 0


# ---- computation ------------------------------------------------------------------------------------------------


def _daily_overrides(daily: pa.Table | None) -> dict[tuple[int, int], tuple[int | None, float | None]]:
    out: dict[tuple[int, int], tuple[int | None, float | None]] = {}
    if daily is None:
        return out
    C.require(daily, ["instrument_id", "session_date"], "daily")
    checks: list[tuple[str, Callable[[pa.DataType], bool], bool]] = [
        ("instrument_id", pa.types.is_integer, False),
        ("session_date", lambda t: pa.types.is_date(t) or pa.types.is_integer(t), False),
        ("daily_volume", pa.types.is_integer, True),
        ("sigma_daily", lambda t: pa.types.is_floating(t) or pa.types.is_integer(t), True),
    ]
    for name, ok, nullable in checks:
        if not C.has(daily, name):
            continue
        col = daily[name]
        if not ok(col.type):
            raise QtError("QT-INPUT-002", f"daily: column {name} has type {col.type}", "dates as date32, ids as ints")
        if not nullable and col.null_count:
            raise QtError("QT-INPUT-002", f"daily: column {name} has {col.null_count} null value(s)")
    for r in daily.to_pylist():
        key = (int(r["instrument_id"]), _days(r["session_date"]))
        if key in out:
            raise QtError("QT-INPUT-003", f"daily: duplicate row for instrument {key[0]} session_date {key[1]}")
        v, s = r.get("daily_volume"), r.get("sigma_daily")
        if v is not None and int(v) < 0:
            raise QtError("QT-INPUT-002", f"daily: negative daily_volume for instrument {key[0]}")
        if s is not None and (not math.isfinite(float(s)) or float(s) < 0):
            raise QtError("QT-INPUT-002", f"daily: sigma_daily must be finite and >= 0 for instrument {key[0]}")
        out[key] = (None if v is None else int(v), None if s is None else float(s))
    return out


def impact_frac(q: np.ndarray, sigma: np.ndarray, cfg: ImpactConfig) -> np.ndarray:
    """`Y * sigma * sqrt(q)` for `q >= linear_below`, linear in `q` below it (continuous at `linear_below`)."""
    q = np.asarray(q, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    lb = cfg.linear_below
    root = np.sqrt(np.maximum(q, 0.0))
    if lb > 0:
        root = np.where(q < lb, math.sqrt(lb) * q / lb, root)
    return cfg.coefficient * sigma * root


def _day_figures(tr: _Trades | None, sess: _Session, cfg: ImpactConfig) -> tuple[int | None, float | None]:
    """(venue session volume, realized sigma or None) of one instrument-day."""
    if tr is None:
        return None, None
    lo, hi = np.searchsorted(tr.ts, sess.open_ns, "left"), np.searchsorted(tr.ts, sess.close_ns, "right")
    v = int(np.sum(tr.size[lo:hi]))
    seed = None
    c0 = int(np.searchsorted(tr.cts, sess.open_ns, "left"))
    if c0 < len(tr.cts) and tr.cts[c0] <= sess.close_ns:
        crosses = np.flatnonzero(tr.cross[lo:hi] & (tr.ts[lo:hi] <= tr.cts[c0]))
        if len(crosses):
            seed = int(tr.price[lo + int(crosses[0])])
    sg, nr = realized_sigma(tr.cts, tr.cprice, sess.open_ns, sess.close_ns, cfg.rv_interval_ns, seed)
    return v, (sg if sg is not None and nr >= cfg.min_returns else None)


def _compute(
    o: _Orders,
    trades: pa.Table,
    instruments: pa.Table | None,
    daily: pa.Table | None,
    cfg: ImpactConfig,
    meta: dict[str, str],
) -> pa.Table:
    by = _trades_by_instrument(trades)
    sessions, lots = _sessions(instruments)
    overrides = _daily_overrides(daily)
    n = len(o.sid)
    day_cache: dict[tuple[int, int], tuple[int | None, float | None, bool]] = {}
    sdate = np.zeros(n, dtype=np.int64)
    tick = np.zeros(n, dtype=np.int64)
    tick_ok = np.zeros(n, dtype=bool)
    vol = np.zeros(n, dtype=np.int64)
    vol_ok = np.zeros(n, dtype=bool)
    user = np.zeros(n, dtype=bool)
    sig = np.full(n, np.nan)
    ref = np.zeros(n, dtype=np.int64)
    ref_src: list[str] = []
    status: list[str] = []
    qmin = np.zeros(n, dtype=np.int64)
    ts_end = o.ts_end.copy()
    life = np.zeros(n, dtype=np.int64)
    for i in range(n):
        iid, t0 = int(o.iid[i]), int(o.ts_live[i])
        sess = _session_of(sessions, iid, t0)
        sdate[i] = sess.session_date
        qmin[i] = cfg.queue_min_qty if cfg.queue_min_qty is not None else lots.get(iid, 1)
        if sess.tick is not None:
            tick[i], tick_ok[i] = sess.tick, True
        tr = by.get(iid)
        key = (iid, sess.session_date)
        if key not in day_cache:
            v, s = _day_figures(tr, sess, cfg)
            ov = overrides.get(key)
            if ov is not None:
                v = ov[0] if ov[0] is not None else v
                s = ov[1] if ov[1] is not None else s
            day_cache[key] = (v, s, ov is not None and ov[0] is not None)
        v, s, user[i] = day_cache[key]
        if v is not None and v > 0:
            vol[i], vol_ok[i] = v, True
        if s is not None:
            sig[i] = s
        if o.price_ok[i]:
            ref[i] = o.price[i]
            ref_src.append("order")
        elif tr is not None and (j := int(np.searchsorted(tr.cts, t0, "right")) - 1) >= 0:
            ref[i] = tr.cprice[j]
            ref_src.append("last_trade")
        else:
            ref_src.append("none")
        if o.rejected[i]:
            status.append("rejected")
        elif o.oos[i] or (sess.known and not sess.open_ns <= t0 < sess.close_ns):
            status.append("out_of_session")
        else:
            status.append("live")
        if ts_end[i] < 0:
            ts_end[i] = max(sess.close_ns, t0)
        if tr is not None and status[i] != "rejected":
            a = int(np.searchsorted(tr.cts, t0, "right"))
            b = int(np.searchsorted(tr.cts, int(ts_end[i]), "right"))
            life[i] = int(tr.csize_cum[b] - tr.csize_cum[a])
    live = np.array([s == "live" for s in status], dtype=bool)
    rested = np.array([s != "rejected" for s in status], dtype=bool)
    qty = o.qty.astype(float)
    ref_ok = np.array([r != "none" for r in ref_src], dtype=bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        part = np.where(vol_ok & live, qty / np.where(vol_ok, vol, 1), np.nan)
        est = impact_frac(np.nan_to_num(part), np.nan_to_num(sig), cfg)
        frac = np.where(~np.isnan(part) & ~np.isnan(sig), est, np.nan)
        ticks = np.where(tick_ok & ref_ok & ~np.isnan(frac), frac * ref / np.where(tick_ok, tick, 1), np.nan)
        qden = o.level + o.qty
        qlive = np.where(rested & o.level_ok & (qden > 0), qty / np.where(qden > 0, qden, 1), np.nan)
        qmax = np.where(np.isnan(o.qmax), qlive, np.fmax(o.qmax, qlive))
        filled = o.filled.astype(float)
        plain = np.where(filled + life > 0, filled / np.where(filled + life > 0, filled + life, 1), np.nan)
        tape = np.where(life > 0, np.minimum(filled / np.where(life > 0, life, 1), 1.0), np.nan)
        vshare = np.where(rested & o.filled_ok, np.where(o.in_tape, tape, plain), np.nan)
    f_imp, f_q, f_v = _flags(live, ticks, user, qlive, o.qty, qmin, vshare, cfg)
    order = sorted(range(n), key=lambda i: o.sid[i])
    ix = np.array(order, dtype=np.int64)

    def take(a: np.ndarray) -> np.ndarray:
        return a[ix] if len(ix) else a[:0]

    def fcol(a: np.ndarray) -> pa.Array:
        x = take(a)
        return pa.array(x, type=pa.float64(), mask=np.isnan(x))

    def icol(a: np.ndarray, ok: np.ndarray | None, ty: pa.DataType) -> pa.Array:
        m = None if ok is None else ~take(ok)
        return pa.array(take(a), type=pa.int64(), mask=m).cast(ty)

    def dcol(vals: list[str], col: str) -> pa.DictionaryArray:
        return C.dict_array([vals[i] for i in order], TABLE, col)

    def bcol(a: np.ndarray) -> pa.Array:
        return pa.array(take(a), type=pa.bool_())

    cols: dict[str, pa.Array] = {
        "shadow_id": pa.array([o.sid[i] for i in order], type=pa.utf8()),
        "instrument_id": icol(o.iid, None, pa.uint32()),
        "session_date": pa.array(take(sdate), type=pa.int32()).cast(pa.date32()),
        "status": dcol(status, "status"),
        "qty": icol(o.qty, None, pa.uint64()),
        "filled_qty": icol(o.filled, o.filled_ok, pa.uint64()),
        "fills_in_tape": bcol(o.in_tape),
        "ts_live": icol(o.ts_live, None, pa.int64()).cast(SCHEMA.field("ts_live").type),
        "ts_end": icol(ts_end, None, pa.int64()).cast(SCHEMA.field("ts_end").type),
        "end_source": dcol(o.end_source, "end_source"),
        "ref_price": icol(ref, ref_ok, pa.int64()),
        "ref_price_source": dcol(ref_src, "ref_price_source"),
        "tick_size": icol(tick, tick_ok, pa.int64()),
        "daily_volume": icol(vol, vol_ok, pa.uint64()),
        "volume_source": dcol(["user" if u else "venue_session" for u in user], "volume_source"),
        "sigma_daily": fcol(sig),
        "daily_participation": fcol(part),
        "impact_frac": fcol(frac),
        "impact_bps": fcol(frac * 1e4),
        "impact_ticks": fcol(ticks),
        "level_qty_at_live": icol(o.level, o.level_ok, pa.uint64()),
        "queue_share_at_live": fcol(qlive),
        "queue_share_max": fcol(qmax),
        "queue_min_qty": icol(qmin, None, pa.uint64()),
        "life_volume": icol(life, rested, pa.uint64()),
        "volume_share": fcol(vshare),
        "flag_impact": bcol(f_imp),
        "flag_queue": bcol(f_q),
        "flag_volume": bcol(f_v),
        "material": bcol(f_imp | f_q | f_v),
    }
    srcs = {bool(u) for u in user}
    vs = "mixed" if len(srcs) > 1 else ("user" if srcs == {True} else "venue_session")
    table = C.build(TABLE, cols)
    extra = {META_VOLUME_SOURCE: vs, **meta}
    return table.replace_schema_metadata(
        {**(SCHEMA.metadata or {}), **{k.encode(): v.encode() for k, v in sorted(extra.items())}}
    )


def _flags(
    live: np.ndarray,
    ticks: np.ndarray,
    user: np.ndarray,
    qlive: np.ndarray,
    qty: np.ndarray,
    qmin: np.ndarray,
    vshare: np.ndarray,
    cfg: ImpactConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    over = np.nan_to_num(ticks, nan=-1.0) >= cfg.impact_ticks
    f_imp = live & over & (user | cfg.flag_venue_volume_impact)
    f_q = live & (np.nan_to_num(qlive, nan=-1.0) >= cfg.queue_share) & (qty >= qmin)
    f_v = live & (np.nan_to_num(vshare, nan=-1.0) >= cfg.volume_share)
    return f_imp, f_q, f_v


def shadow_impact(
    results: pa.Table,
    trades: pa.Table,
    instruments: pa.Table | None = None,
    *,
    trajectory: pa.Table | None = None,
    daily: pa.Table | None = None,
    config: ImpactConfig | dict[str, Any] | None = None,
    fills_in_tape: bool | Collection[str] = False,
) -> pa.Table:
    """`qt.shadow_impact` of a replay: `results` = `qt.shadow_results`, `trades` = `qt.trades` of the same run (or
    `dataset_trades`), `instruments` = `qt.instruments`, optional `qt.queue_trajectory`. `fills_in_tape`: true (or
    the shadow ids) when the shadows replaced self-excluded twins whose executions are in `trades`."""
    cfg = _config(config)
    return _compute(
        _from_results(results, trajectory, fills_in_tape), trades, instruments, daily, cfg, {META_SOURCE: "replay"}
    )


def zoo_shadow_impact(
    predictions: pa.Table,
    zoo_shadows: pa.Table,
    trades: pa.Table,
    instruments: pa.Table | None = None,
    *,
    h_max_ns: int | None = None,
    daily: pa.Table | None = None,
    config: ImpactConfig | dict[str, Any] | None = None,
) -> pa.Table:
    """`qt.shadow_impact` of a zoo run (`qt.zoo_predictions` + `qt.zoo_shadows`); the life is the largest prediction
    horizon `<= h_max_ns` (None = the largest horizon), cut by the planned cancel and the administrative end."""
    cfg = _config(config)
    if h_max_ns is not None and int(h_max_ns) < 0:
        raise QtError("QT-CONFIG-001", "h_max_ns must be >= 0")
    orders, horizon = _from_zoo(predictions, zoo_shadows, None if h_max_ns is None else int(h_max_ns))
    meta = {META_SOURCE: "zoo", META_HORIZON: str(horizon)}
    if h_max_ns is not None:
        meta[META_H_MAX] = str(int(h_max_ns))
    return _compute(orders, trades, instruments, daily, cfg, meta)


# ---- summary and warning ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Counts:
    """What a warning line says, for shadows or for orders (`unit`)."""

    unit: str
    n_assessed: int
    n_material: int
    n_queue: int
    n_volume: int
    n_impact: int
    n_impact_bound: int
    n_eval_impact: int
    n_eval_volume: int
    volume_source: str


def _table_flags(table: pa.Table, cfg: ImpactConfig) -> dict[str, np.ndarray]:
    C.require(table, list(SCHEMA.names), TABLE)
    live = C.strings(table, "status") == "live"
    ticks = C.floats(table, "impact_ticks")
    user = C.strings(table, "volume_source") == "user"
    qlive = C.floats(table, "queue_share_at_live")
    qty, _ = C.ints(table, "qty")
    qmin, _ = C.ints(table, "queue_min_qty")
    vshare = C.floats(table, "volume_share")
    f_i, f_q, f_v = _flags(live, ticks, user, qlive, qty, qmin, vshare, cfg)
    over = np.nan_to_num(ticks, nan=-1.0) >= cfg.impact_ticks
    return {
        "live": live,
        "ticks": ticks,
        "qlive": qlive,
        "vshare": vshare,
        "qty": qty,
        "qmin": qmin,
        "impact": f_i,
        "queue": f_q,
        "volume": f_v,
        "material": f_i | f_q | f_v,
        "bound": live & over & ~user & ~f_i,
        "queue_small": live & (np.nan_to_num(qlive, nan=-1.0) >= cfg.queue_share) & (qty < qmin),
        "eval_impact": live & ~np.isnan(ticks),
        "eval_queue": live & ~np.isnan(qlive),
        "eval_volume": live & ~np.isnan(vshare),
    }


def _volume_source(table: pa.Table) -> str:
    meta = table.schema.metadata or {}
    return meta.get(META_VOLUME_SOURCE.encode(), b"venue_session").decode()


def _counts(table: pa.Table, cfg: ImpactConfig, groups: Sequence[str] | None = None, unit: str = "shadows") -> _Counts:
    """Counts per shadow, or per group (an order key per row: a group is assessed, flagged or evaluable when any of
    its rows is)."""
    f = _table_flags(table, cfg)
    if groups is not None:
        keys = sorted(set(groups))
        pos = {k: i for i, k in enumerate(keys)}
        g = np.array([pos[k] for k in groups], dtype=np.int64)

        def red(a: np.ndarray) -> np.ndarray:
            out = np.zeros(len(keys), dtype=bool)
            np.logical_or.at(out, g, a)
            return out

        f = {k: red(v) for k, v in f.items() if v.dtype == bool}
    return _Counts(
        unit=unit,
        n_assessed=int(np.sum(f["live"])),
        n_material=int(np.sum(f["material"])),
        n_queue=int(np.sum(f["queue"])),
        n_volume=int(np.sum(f["volume"])),
        n_impact=int(np.sum(f["impact"])),
        n_impact_bound=int(np.sum(f["bound"])),
        n_eval_impact=int(np.sum(f["eval_impact"])),
        n_eval_volume=int(np.sum(f["eval_volume"])),
        volume_source=_volume_source(table),
    )


def _is_material(c: _Counts, cfg: ImpactConfig) -> bool:
    return c.n_material > 0 and c.n_assessed > 0 and c.n_material / c.n_assessed >= cfg.warn_fraction


def _line(c: _Counts, cfg: ImpactConfig, tail: str = "") -> str:
    pct = 100.0 * c.n_material / c.n_assessed if c.n_assessed else 0.0
    t = f">= {cfg.impact_ticks:g} tick"
    if c.n_eval_impact == 0:
        imp = "own impact: not evaluable"
    elif cfg.flag_venue_volume_impact:
        imp = f"own impact {t}: {c.n_impact}"
    else:
        parts = []
        if c.volume_source in ("user", "mixed"):
            parts.append(f"own impact {t}: {c.n_impact}")
        if c.volume_source in ("venue_session", "mixed"):
            parts.append(f"impact {t} on venue volume only (upper bound, not flagged): {c.n_impact_bound}")
        imp = "; ".join(parts)
    if 0 < c.n_eval_impact < c.n_assessed:
        imp += f" (not evaluable for {c.n_assessed - c.n_eval_impact})"
    vol = (
        "volume share: not evaluable"
        if c.n_eval_volume == 0
        else f">= {cfg.volume_share:.0%} of traded volume over life: {c.n_volume}"
    )
    size = "round lot or more" if cfg.queue_min_qty is None else f"qty >= {cfg.queue_min_qty}"
    return (
        f"impact: {c.n_material} of {c.n_assessed} {c.unit} ({pct:.1f}%) may not be independent of the book "
        f"(>= {cfg.queue_share:.0%} of the displayed queue at live, {size}: {c.n_queue}; {vol}; {imp}); "
        f"a fill rate over them could move by up to {pct:.1f} pts{tail}"
    )


def warning_line(s: ShadowImpactSummary, tail: str = "") -> str | None:
    """The one-line warning of a material summary (None otherwise); `tail` is appended (e.g. where to look)."""
    if not s.material:
        return None
    c = _Counts(
        unit="shadows",
        n_assessed=s.n_assessed,
        n_material=s.n_material,
        n_queue=s.n_flag_queue,
        n_volume=s.n_flag_volume,
        n_impact=s.n_flag_impact,
        n_impact_bound=s.n_impact_venue_bound,
        n_eval_impact=s.n_evaluable_impact,
        n_eval_volume=s.n_evaluable_volume,
        volume_source=s.volume_source,
    )
    return _line(c, s.config, tail)


def _dist(x: np.ndarray) -> ImpactDistribution:
    x = x[~np.isnan(x)]
    if len(x) == 0:
        return ImpactDistribution(n=0, p50=None, p90=None, p99=None, max=None)
    q = np.quantile(x, [0.5, 0.9, 0.99])
    return ImpactDistribution(n=len(x), p50=float(q[0]), p90=float(q[1]), p99=float(q[2]), max=float(np.max(x)))


def _meta_int(table: pa.Table, key: str) -> int | None:
    v = (table.schema.metadata or {}).get(key.encode())
    return None if v is None else int(v.decode())


def summarize(
    table: pa.Table,
    config: ImpactConfig | dict[str, Any] | None = None,
    *,
    dataset: str | None = None,
    inputs: list[FileEntry] | None = None,
) -> ShadowImpactSummary:
    """`qt.shadow_impact_summary` of a `qt.shadow_impact` table (thresholds of `config`, which should be the one the
    table was computed with; flags are recomputed from the figures so the two always agree)."""
    cfg = _config(config)
    f = _table_flags(table, cfg)
    live = f["live"]
    status = C.strings(table, "status")
    n = table.num_rows
    n_live = int(np.sum(live))
    mat = f["material"]
    n_mat = int(np.sum(mat))
    share = n_mat / n_live if n_live else None

    def sens(x: np.ndarray, extra: np.ndarray, name: str) -> list[ImpactSensitivity]:
        v = np.nan_to_num(x, nan=-1.0)
        return [
            ImpactSensitivity(threshold=t, n_flagged=int(np.sum(live & extra & (v >= t)))) for t in SENSITIVITY[name]
        ]

    lev, lev_ok = C.ints(table, "level_qty_at_live")
    iid, _ = C.ints(table, "instrument_id")
    sd, _ = C.ints(table, "session_date")
    dv, dv_ok = C.ints(table, "daily_volume")
    sg = C.floats(table, "sigma_daily")
    vsrc = C.strings(table, "volume_source")
    by_inst: dict[str, int] = {}
    days: dict[tuple[int, int], list[Any]] = {}
    for i in range(n):
        if mat[i]:
            by_inst[str(int(iid[i]))] = by_inst.get(str(int(iid[i])), 0) + 1
        d = days.setdefault(
            (int(iid[i]), int(sd[i])),
            [int(dv[i]) if dv_ok[i] else None, None if np.isnan(sg[i]) else float(sg[i]), str(vsrc[i]), 0, 0],
        )
        d[3] += 1
        d[4] += int(mat[i])
    epoch = np.datetime64("1970-01-01", "D")
    in_tape, _ = C.bools(table, "fills_in_tape")
    summ = ShadowImpactSummary(
        source=(table.schema.metadata or {}).get(META_SOURCE.encode(), b"replay").decode(),  # type: ignore[arg-type]
        config=cfg,
        volume_source=_volume_source(table),  # type: ignore[arg-type]
        h_max_ns=_meta_int(table, META_H_MAX),
        horizon_ns=_meta_int(table, META_HORIZON),
        fills_in_tape=bool(np.any(in_tape)),
        dataset=dataset,
        inputs=inputs or [],
        files=[],
        n_shadows=n,
        n_rejected=int(np.sum(status == "rejected")),
        n_out_of_session=int(np.sum(status == "out_of_session")),
        n_assessed=n_live,
        n_evaluable_impact=int(np.sum(f["eval_impact"])),
        n_evaluable_queue=int(np.sum(f["eval_queue"])),
        n_evaluable_volume=int(np.sum(f["eval_volume"])),
        n_flag_impact=int(np.sum(f["impact"])),
        n_impact_venue_bound=int(np.sum(f["bound"])),
        n_flag_queue=int(np.sum(f["queue"])),
        n_queue_below_min_qty=int(np.sum(f["queue_small"])),
        n_flag_volume=int(np.sum(f["volume"])),
        n_alone_at_level=int(np.sum(live & lev_ok & (lev == 0))),
        n_material=n_mat,
        material_share=share,
        material=_is_material(_counts(table, cfg), cfg),
        distributions={
            "impact_bps": _dist(np.where(live, C.floats(table, "impact_bps"), np.nan)),
            "impact_ticks": _dist(np.where(live, f["ticks"], np.nan)),
            "daily_participation": _dist(np.where(live, C.floats(table, "daily_participation"), np.nan)),
            "queue_share_at_live": _dist(np.where(live, f["qlive"], np.nan)),
            "queue_share_max": _dist(np.where(live, C.floats(table, "queue_share_max"), np.nan)),
            "volume_share": _dist(np.where(live, f["vshare"], np.nan)),
        },
        sensitivity={
            "impact_ticks": sens(f["ticks"], np.ones(n, dtype=bool), "impact_ticks"),
            "queue_share": sens(f["qlive"], f["qty"] >= f["qmin"], "queue_share"),
            "volume_share": sens(f["vshare"], np.ones(n, dtype=bool), "volume_share"),
        },
        by_instrument=dict(sorted(by_inst.items(), key=lambda kv: int(kv[0]))),
        days=[
            ImpactDay(
                instrument_id=k[0],
                session_date=str(epoch + np.timedelta64(k[1], "D")),
                daily_volume=v[0],
                sigma_daily=v[1],
                volume_source=v[2],
                n_shadows=v[3],
                n_material=v[4],
            )
            for k, v in sorted(days.items())
        ],
        warning=None,
    )
    return summ.model_copy(update={"warning": warning_line(summ)})


# ---- dataset access ---------------------------------------------------------------------------------------------


def _part_keys(path: Path) -> tuple[int | None, str | None]:
    iid: int | None = None
    date: str | None = None
    for part in path.parts:
        if part.startswith("instrument_id="):
            with contextlib.suppress(ValueError):
                iid = int(part.split("=", 1)[1])
        elif part.startswith("date="):
            date = part.split("=", 1)[1]
    return iid, date


def _read_parts(
    root: Path, columns: list[str], keys: set[tuple[int, str]] | None, filters: list[Any] | None = None
) -> list[pa.Table]:
    """Parquet files under `root`, pruned to the `keys` instrument-days by their `instrument_id=` / `date=` path
    components (files without them are read and filtered on `instrument_id`)."""
    iids = None if keys is None else {k[0] for k in keys}
    dates = None if keys is None else {k[1] for k in keys}
    out = []
    for p in sorted(root.rglob("*.parquet")):
        iid, date = _part_keys(p.relative_to(root))
        if keys is not None:
            if iid is not None and date is not None and (iid, date) not in keys:
                continue
            if iid is not None and date is None and iid not in iids:  # type: ignore[operator]
                continue
            if date is not None and iid is None and date not in dates:  # type: ignore[operator]
                continue
        flt = list(filters or [])
        if iids is not None and iid is None:
            flt.append(("instrument_id", "in", sorted(iids)))
        out.append(pq.read_table(p, columns=columns, partitioning=None, filters=flt or None))
    return out


#: Columns of `dataset_trades` (an empty selection has exactly these).
TRADE_COLUMNS = pa.schema(
    [
        ("ts_event", pa.timestamp("ns", tz="UTC")),
        ("instrument_id", pa.uint32()),
        ("sequence", pa.uint64()),
        ("price", pa.int64()),
        ("size", pa.uint64()),
        ("kind", pa.utf8()),
    ]
)


def _concat(parts: list[pa.Table]) -> pa.Table:
    return pa.concat_tables(parts) if parts else TRADE_COLUMNS.empty_table()


def dataset_instruments(dataset: str | Path) -> pa.Table | None:
    """`instruments.parquet` of a dataset directory, or None when it has none."""
    p = Path(dataset) / "instruments.parquet"
    return pq.read_table(p) if p.exists() else None


def dataset_trades(dataset: str | Path, keys: Iterable[tuple[int, str]] | None = None) -> pa.Table:
    """The venue's trades of a dataset directory (SPEC 3.3) as `ts_event`, `instrument_id`, `sequence`, `price`,
    `size`, `kind`, limited to the `keys` (instrument_id, `YYYY-MM-DD`) instrument-days when given: `trades_source/`
    when present (its `kind`, so cross prints are known), else the `T` records of `mbo/` (kind `displayed`: cross
    prints are not told apart), else the `trade` rows of `l2/` (`trade_qty` as size); the first source directory that
    exists is used, and an empty selection is an empty table. `QT-INPUT-001` without any."""
    root = Path(dataset)
    if not root.is_dir():
        raise QtError("QT-INPUT-001", f"dataset: {root} is not a directory")
    ks = None if keys is None else set(keys)
    cols = ["ts_event", "instrument_id", "sequence", "price", "size"]
    if (root / "trades_source").is_dir():
        return _concat(
            [
                t.set_column(5, "kind", t["kind"].cast(pa.utf8()))
                for t in _read_parts(root / "trades_source", [*cols, "kind"], ks)
            ]
        )
    if (root / "mbo").is_dir():
        parts = []
        for t in _read_parts(root / "mbo", [*cols, "action"], ks, [("action", "=", ord("T"))]):
            t = t.drop_columns(["action"])
            parts.append(t.append_column("kind", pa.array(["displayed"] * t.num_rows, type=pa.utf8())))
        return _concat(parts)
    if (root / "l2").is_dir():
        parts = []
        l2cols = ["ts_event", "instrument_id", "sequence", "kind", "price", "trade_qty"]
        for t in _read_parts(root / "l2", l2cols, ks):
            t = t.filter(pc.and_(pc.equal(t["kind"].cast(pa.utf8()), "trade"), pc.greater(t["trade_qty"], 0)))
            parts.append(
                pa.table(
                    {
                        "ts_event": t["ts_event"],
                        "instrument_id": t["instrument_id"],
                        "sequence": t["sequence"],
                        "price": t["price"],
                        "size": t["trade_qty"],
                        "kind": pa.array(["displayed"] * t.num_rows, type=pa.utf8()),
                    }
                )
            )
        return _concat(parts)
    raise QtError("QT-INPUT-001", f"dataset: {root} has no trades_source/, mbo/ or l2/ partitions")


def _date_str(days: int) -> str:
    return str(np.datetime64("1970-01-01", "D") + np.timedelta64(int(days), "D"))


def _replay_keys(results: pa.Table, instruments: pa.Table | None) -> set[tuple[int, str]]:
    sessions, _ = _sessions(instruments)
    iid, _ = C.ints(results, "instrument_id")
    ts, _ = C.ints(results, "ts_live")
    return {
        (int(i), _date_str(_session_of(sessions, int(i), int(t)).session_date)) for i, t in zip(iid, ts, strict=True)
    }


def _zoo_keys(predictions: pa.Table) -> set[tuple[int, str]]:
    iid, _ = C.ints(predictions, "instrument_id")
    sd, _ = C.ints(predictions, "session_date")
    return {(int(i), _date_str(int(d))) for i, d in set(zip(iid.tolist(), sd.tolist(), strict=True))}


# ---- the callers' one-liners (fail-soft) ------------------------------------------------------------------------


@contextlib.contextmanager
def suppressed() -> Iterator[None]:
    """Within the block `zoo_spec_warnings` returns `[]` (a caller that runs a zoo internally and assesses the
    shadows itself, as `score` does)."""
    tok = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(tok)


def _soft(fn: Callable[[], list[str]]) -> list[str]:
    try:
        return fn()
    except SOFT_ERRORS as e:
        code = e.code if isinstance(e, QtError) else ("QT-INPUT-001" if isinstance(e, OSError) else "QT-INPUT-002")
        text = e.message if isinstance(e, QtError) else str(e)
        first = text.splitlines()[0] if text.strip() else type(e).__name__
        return [f"impact: not assessed ({code}: {first[:200]})"]


def replay_warnings(
    results: pa.Table | None,
    trades: pa.Table | None,
    instruments: pa.Table | None = None,
    *,
    trajectory: pa.Table | None = None,
    config: ImpactConfig | dict[str, Any] | None = None,
    fills_in_tape: bool | Collection[str] = False,
) -> list[str]:
    """`[warning line]` for a replay's shadows when material, else `[]` (no results or no trades: `[]`). Fail-soft."""
    if results is None or trades is None or results.num_rows == 0:
        return []

    def run() -> list[str]:
        cfg = _config(config)
        t = shadow_impact(results, trades, instruments, trajectory=trajectory, config=cfg, fills_in_tape=fills_in_tape)
        w = summarize(t, cfg).warning
        return [w] if w else []

    return _soft(run)


def _zoo_inputs(out: Path) -> tuple[pa.Table, pa.Table]:
    pp, sp = out / "zoo_predictions.parquet", out / "zoo_shadows.parquet"
    if not pp.exists() or not sp.exists():
        raise QtError("QT-INPUT-001", f"zoo output: {out} lacks zoo_predictions.parquet or zoo_shadows.parquet")
    return pq.read_table(pp), pq.read_table(sp)


def zoo_warnings(
    out_dir: str | Path,
    dataset: str | Path,
    *,
    h_max_ns: int | None = None,
    config: ImpactConfig | dict[str, Any] | None = None,
) -> list[str]:
    """`[warning line]` for a zoo output directory on its dataset when material, else `[]`. Zoo runs on L2 datasets
    (no exact filled quantity) are assessed on impact and queue share only. Fail-soft."""
    out = Path(out_dir)

    def run() -> list[str]:
        preds, zs = _zoo_inputs(out)
        if preds.num_rows == 0:
            return []
        cfg = _config(config)
        inst = dataset_instruments(dataset)
        t = zoo_shadow_impact(preds, zs, dataset_trades(dataset, _zoo_keys(preds)), inst, h_max_ns=h_max_ns, config=cfg)
        # No path in the text: the warning lands in summary.json, which must not depend on where the run was written
        # (SPEC 7.2 determinism; certificates re-run in another directory and compare).
        w = warning_line(summarize(t, cfg), "; per shadow: python -m queuetruth.eval.impact zoo --zoo <zoo output dir>")
        return [w] if w else []

    return _soft(run)


def zoo_spec_warnings(out_dir: str | Path, spec: Any) -> list[str]:
    """The `zoo` command's hook (`api.run_zoo`): `zoo_warnings` on the spec's dataset path and `h_max_ns`; `[]`
    inside `suppressed()` or without a dataset path."""
    if _SUPPRESSED.get():
        return []
    ds = getattr(getattr(spec, "dataset", None), "path", None)
    if not isinstance(ds, str):
        return []
    return zoo_warnings(out_dir, ds, h_max_ns=getattr(spec, "h_max_ns", None))


def _leg_keys(schedule: Sequence[dict[str, Any]]) -> dict[str, str]:
    return {str(r["shadow_id"]): str(r.get("order_key") or r["shadow_id"]) for r in schedule}


def _order_line(table: pa.Table, cfg: ImpactConfig, legs: dict[str, str]) -> list[str]:
    sids = [str(s) for s in table["shadow_id"].to_pylist()]
    groups = [legs.get(s, s) for s in sids]
    c = _counts(table, cfg, groups, unit="orders")
    if not _is_material(c, cfg):
        return []
    mat = _table_flags(table, cfg)["material"]
    flagged = sorted({g for g, m in zip(groups, mat, strict=True) if m})
    more = f" (+{len(flagged) - 5} more)" if len(flagged) > 5 else ""
    return [_line(c, cfg, f"; orders: {', '.join(flagged[:5])}{more}")]


def score_warnings(
    results: pa.Table | None,
    trades: pa.Table | None,
    instruments: pa.Table | None,
    *,
    schedule: Sequence[dict[str, Any]],
    twin_keys: Collection[str] = (),
    config: ImpactConfig | dict[str, Any] | None = None,
) -> list[str]:
    """`score`'s exact-arm hook: one line counting live orders (an order is flagged when any of its legs is) and
    naming the first flagged order keys. Legs of orders in `twin_keys` replaced a self-excluded twin, so their fills
    are already in `trades`. Fail-soft."""
    if results is None or trades is None or results.num_rows == 0:
        return []

    def run() -> list[str]:
        cfg = _config(config)
        legs = _leg_keys(schedule)
        tk = set(twin_keys)
        in_tape = {s for s, k in legs.items() if k in tk}
        return _order_line(shadow_impact(results, trades, instruments, config=cfg, fills_in_tape=in_tape), cfg, legs)

    return _soft(run)


def score_zoo_warnings(
    predictions: pa.Table | None,
    zoo_shadows: pa.Table | None,
    trades: pa.Table | None,
    instruments: pa.Table | None,
    *,
    h_max_ns: int | None,
    schedule: Sequence[dict[str, Any]],
    config: ImpactConfig | dict[str, Any] | None = None,
) -> list[str]:
    """`score`'s hook on L2 datasets (no exact arm): impact and queue share at live from the zoo arm's shadows
    (volume share needs an exact filled quantity). Fail-soft."""
    if predictions is None or zoo_shadows is None or trades is None or predictions.num_rows == 0:
        return []

    def run() -> list[str]:
        cfg = _config(config)
        t = zoo_shadow_impact(predictions, zoo_shadows, trades, instruments, h_max_ns=h_max_ns, config=cfg)
        return _order_line(t, cfg, _leg_keys(schedule))

    return _soft(run)


# ---- files and the module entry point ---------------------------------------------------------------------------


def write(out: str | Path, table: pa.Table, summary: ShadowImpactSummary) -> ShadowImpactSummary:
    """Writes `shadow_impact.parquet` (SPEC 0.1 Parquet settings, `schemas.io.write_table`) and
    `shadow_impact_summary.json` (sorted keys, no NaN, with the table's `files` entry) under `out`; returns the
    summary as written."""
    o = Path(out)
    o.mkdir(parents=True, exist_ok=True)
    entry = FileEntry.model_validate(write_table(table, o / "shadow_impact.parquet"))
    summ = summary.model_copy(update={"files": [entry]})
    doc = summ.model_dump(mode="json")
    (o / "shadow_impact_summary.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return summ


def _read(path: Path, inputs: list[FileEntry]) -> pa.Table:
    t = pq.read_table(path)
    inputs.append(FileEntry(name=path.name, rows=t.num_rows, bytes=path.stat().st_size, sha256=sha256_file(path)))
    return t


def _find(d: Path, name: str, inputs: list[FileEntry]) -> pa.Table | None:
    p = d / name
    return _read(p, inputs) if p.exists() else None


def _run_summary(d: Path) -> dict[str, Any]:
    p = d / "summary.json"
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise QtError("QT-INPUT-002", f"{p}: not JSON ({e.msg})") from e
    return doc if isinstance(doc, dict) else {}


def _dataset_of(arg: str | None, run: dict[str, Any], what: str) -> Path:
    path = arg or ((run.get("spec") or {}).get("dataset") or {}).get("path")
    if not path:
        raise QtError("QT-INPUT-001", f"{what}: no dataset (none in summary.json spec)", "pass --dataset DIR")
    p = Path(path)
    if not p.is_dir():
        raise QtError("QT-INPUT-001", f"{what}: dataset {p} is not a directory", "pass --dataset DIR")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m queuetruth.eval.impact {replay,zoo} ...`: writes `qt.shadow_impact` and its summary, prints the
    summary JSON on stdout (the warning line, if any, on stderr). Exit 0, or 2 with a `qt.error` document. The dataset
    and (zoo) `h_max_ns` default to the run's `summary.json` spec."""
    ap = argparse.ArgumentParser(prog="python -m queuetruth.eval.impact", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="shadows of a replay output directory")
    r.add_argument("--replay", required=True, help="replay output dir (shadow_results.parquet, trades.parquet)")
    r.add_argument("--dataset", help="dataset dir (default: the replay summary's spec)")
    r.add_argument(
        "--twins",
        action="store_true",
        help="the shadows replaced self-excluded twins (their fills are in trades); default: every shadow twin-linked "
        "in the replay summary",
    )
    z = sub.add_parser("zoo", help="shadows of a zoo output directory")
    z.add_argument("--zoo", required=True, help="zoo output dir (zoo_predictions.parquet, zoo_shadows.parquet)")
    z.add_argument("--dataset", help="the zoo run's dataset dir (default: the zoo summary's spec)")
    z.add_argument("--h-max-ns", type=int, help="life cap (default: the zoo summary's spec.h_max_ns)")
    for p in (r, z):
        p.add_argument("--out", required=True)
        p.add_argument("--daily", help="Parquet with instrument_id, session_date, daily_volume, sigma_daily")
        p.add_argument("--config", help="JSON object of ImpactConfig fields")
    a = ap.parse_args(argv)
    inputs: list[FileEntry] = []
    try:
        cfg = _config(json.loads(a.config) if a.config else None)
        daily = _read(Path(a.daily), inputs) if a.daily else None
        if a.cmd == "replay":
            d = Path(a.replay)
            run = _run_summary(d)
            ds = _dataset_of(a.dataset, run, "replay")
            res = _find(d, "shadow_results.parquet", inputs)
            if res is None:
                raise QtError("QT-INPUT-001", f"replay: {d} has no shadow_results.parquet")
            inst = dataset_instruments(ds)
            trades = _find(d, "trades.parquet", inputs)
            if trades is None:
                trades = dataset_trades(ds, _replay_keys(res, inst))
            n_live = int((run.get("shadows") or {}).get("n") or 0)
            linked = int((run.get("shadow_diagnostics") or {}).get("twins_linked") or 0)
            table = shadow_impact(
                res,
                trades,
                inst,
                trajectory=_find(d, "queue_trajectory.parquet", inputs),
                daily=daily,
                config=cfg,
                fills_in_tape=bool(a.twins or (n_live > 0 and linked >= n_live)),
            )
        else:
            d = Path(a.zoo)
            run = _run_summary(d)
            ds = _dataset_of(a.dataset, run, "zoo")
            h_max = a.h_max_ns if a.h_max_ns is not None else (run.get("spec") or {}).get("h_max_ns")
            pp, sp = _find(d, "zoo_predictions.parquet", inputs), _find(d, "zoo_shadows.parquet", inputs)
            if pp is None or sp is None:
                raise QtError("QT-INPUT-001", f"zoo: {d} lacks zoo_predictions.parquet or zoo_shadows.parquet")
            table = zoo_shadow_impact(
                pp,
                sp,
                dataset_trades(ds, _zoo_keys(pp)),
                dataset_instruments(ds),
                h_max_ns=None if h_max is None else int(h_max),
                daily=daily,
                config=cfg,
            )
        summ = write(a.out, table, summarize(table, cfg, dataset=str(ds), inputs=inputs))
    except (QtError, json.JSONDecodeError, OSError, pa.ArrowException, ValueError, TypeError) as e:
        err = (
            e
            if isinstance(e, QtError)
            else QtError("QT-INPUT-001" if isinstance(e, OSError) else "QT-INPUT-002", str(e))
        )
        print(json.dumps(err.to_document(), sort_keys=True))
        return err.exit_code
    print(json.dumps(summ.model_dump(mode="json"), indent=2, sort_keys=True))
    if summ.warning:
        print(f"warning: {summ.warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
