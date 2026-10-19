"""Exchange-local, DST-safe time-of-day for the time-of-day strata of `climatology_aj`, `mmw_bucket` and the hazard
model's climatology (review F8).

`ts mod 1 day` is the UTC clock: the same local time lands one hour apart on either side of a DST change, so a split
spanning the change (train October, test November) matches the test opening half-hour to another stratum.
`session_clock_ns` returns, per row, nanoseconds in `[0, 1 day)` since a daily origin that is fixed in local time:

1. `midnight_utc_ns` given and valid for every row (the instrument-day's local midnight in UTC, `qt.instruments`):
   `ts - midnight_utc_ns`, the exchange-local clock (as the scorer's `local_ns`).
2. else `tod_sin` / `tod_cos` (SPEC 5.5.2: `frac = clamp((t - midnight - open_s) / (close_s - open_s), 0, 1)`,
   angle `2 pi frac`, computed by the features crate from the dataset's instrument-day): per (instrument, session
   date), the session open in UTC and the session length are solved from the rows strictly inside the session
   (`t = open_utc + frac * length`, least squares, residual < 1 s); `ts - open_utc` mod 1 day is time since the
   session open, a local clock shifted by the instrument's constant `open_s` (half-hour buckets start at the open).
   An instrument-day with a single interior forecast time uses the instrument's median session length; one with
   none uses the instrument's median open offset from UTC midnight (DST-unsafe, rows outside the session only).
3. else (or an instrument without any solvable day): `ts mod 1 day` (UTC clock).

The second value names the rule used ("midnight_utc_ns", "tod_sin/tod_cos", "utc", or a mix).
"""

from __future__ import annotations

import math

import numpy as np

DAY_NS = 86_400_000_000_000
_INTERIOR_EPS = 1e-9
_DISTINCT_FRAC = 1e-6
_MAX_RESIDUAL_NS = 1e9


def _frac(tod_sin: np.ndarray, tod_cos: np.ndarray) -> np.ndarray:
    """Session fraction in [0, 1) from the angle (NaN where a feature is missing)."""
    ang = np.arctan2(tod_sin, tod_cos)
    return np.mod(ang / (2.0 * math.pi), 1.0)


def _solve(t_rel: np.ndarray, frac: np.ndarray) -> tuple[float, float] | None:
    """(open, length) of `t_rel = open + frac * length` by least squares, or None (degenerate or inconsistent)."""
    if t_rel.size < 2 or np.ptp(frac) < _DISTINCT_FRAC:
        return None
    design = np.column_stack([np.ones_like(frac), frac])
    coef, *_ = np.linalg.lstsq(design, t_rel, rcond=None)
    a, length = float(coef[0]), float(coef[1])
    if not (math.isfinite(a) and math.isfinite(length)) or length <= 0:
        return None
    if np.max(np.abs(t_rel - (a + frac * length))) > _MAX_RESIDUAL_NS:
        return None
    return a, length


def session_clock_ns(
    ts: np.ndarray,
    instrument_id: np.ndarray,
    session_date: np.ndarray,
    *,
    midnight_utc_ns: tuple[np.ndarray, np.ndarray] | None = None,
    tod_sin: np.ndarray | None = None,
    tod_cos: np.ndarray | None = None,
) -> tuple[np.ndarray, str]:
    """Per row: ns since the DST-invariant daily origin (module docstring) and the rule used. `session_date` in days
    since the epoch; `midnight_utc_ns` as (values, validity)."""
    t = np.asarray(ts, dtype=np.int64)
    n = t.size
    if midnight_utc_ns is not None:
        mv, ok = midnight_utc_ns
        if n and bool(np.all(ok)):
            return np.mod(t - np.asarray(mv, dtype=np.int64), DAY_NS), "midnight_utc_ns"
    if tod_sin is None or tod_cos is None or n == 0:
        return np.mod(t, DAY_NS), "utc"
    inst = np.asarray(instrument_id, dtype=np.int64)
    day = np.asarray(session_date, dtype=np.int64)
    frac = _frac(np.asarray(tod_sin, dtype=np.float64), np.asarray(tod_cos, dtype=np.float64))
    interior = np.isfinite(frac) & (frac > _INTERIOR_EPS) & (frac < 1.0 - _INTERIOR_EPS)
    keys = np.stack([inst, day], axis=1)
    groups, g_inv = np.unique(keys, axis=0, return_inverse=True)
    g_inv = np.asarray(g_inv).reshape(-1)
    n_g = groups.shape[0]
    open_utc: list[int | None] = [None] * n_g
    length: dict[int, list[float]] = {}
    offset: dict[int, list[int]] = {}
    for g in range(n_g):
        m = (g_inv == g) & interior
        if not m.any():
            continue
        t0 = int(t[m].min())
        sol = _solve((t[m] - t0).astype(np.float64), frac[m])
        if sol is None:
            continue
        o = t0 + round(sol[0])
        open_utc[g] = o
        length.setdefault(int(groups[g, 0]), []).append(sol[1])
        offset.setdefault(int(groups[g, 0]), []).append(o - int(groups[g, 1]) * DAY_NS)
    rules = set()
    for g in range(n_g):
        if open_utc[g] is not None:
            rules.add("tod_sin/tod_cos")
            continue
        i_id = int(groups[g, 0])
        m = (g_inv == g) & interior
        if m.any() and i_id in length:
            lg = float(np.median(length[i_id]))
            open_utc[g] = int(np.median(t[m] - np.round(frac[m] * lg).astype(np.int64)))
            rules.add("tod_sin/tod_cos")
        elif i_id in offset:
            open_utc[g] = int(groups[g, 1]) * DAY_NS + int(np.median(offset[i_id]))
            rules.add("tod_sin/tod_cos (instrument median open offset)")
    origin = np.array([0 if o is None else o for o in open_utc], dtype=np.int64)[g_inv]
    solved = np.array([o is not None for o in open_utc], dtype=bool)[g_inv]
    if not solved.all():
        rules.add("utc")
    clock = np.where(solved, np.mod(t - origin, DAY_NS), np.mod(t, DAY_NS))
    return clock.astype(np.int64), " + ".join(sorted(rules)) if rules else "utc"


__all__ = ["DAY_NS", "session_clock_ns"]
