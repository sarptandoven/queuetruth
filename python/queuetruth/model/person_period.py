"""Person-period expansion (SPEC 5.5.3).

Grid `tau_1 < ... < tau_J` (`tau_0 = 0`). A row `(e, c)` gets a target-0 row for every interval it survives and, if
it ends with a fill or cancel inside the grid, one event row (target 1 = fill, 2 = cancel) for the interval
`tau_{j-1} < e <= tau_j` (`e = 0` falls in interval 1). A censoring contributes target-0 rows for every interval with
`tau_j <= e` and nothing after: a censoring exactly at `tau_j` (e.g. the `h_max` cap) is event-free through `tau_j`.
"""

from __future__ import annotations

import math

import numpy as np
import pyarrow as pa

from queuetruth.model._common import CENSOR, check_grid, outcome_arrays


def interval_counts(event_ns: np.ndarray, cause: np.ndarray, grid_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per row: `n_survived` (target-0 rows) and `event_interval` (1-based interval of the event row, 0 if none)."""
    j_count = len(grid_ns)
    is_cens = cause == CENSOR
    # Events: first j with e <= tau_j (searchsorted left); censorings: number of tau_j <= e.
    k_event = np.searchsorted(grid_ns, event_ns, side="left")  # 0-based index of the event interval
    n_cens = np.searchsorted(grid_ns, event_ns, side="right")
    in_grid = k_event < j_count
    n_survived = np.where(is_cens, np.minimum(n_cens, j_count), np.where(in_grid, k_event, j_count))
    event_interval = np.where((~is_cens) & in_grid, k_event + 1, 0)
    return n_survived.astype(np.int64), event_interval.astype(np.int64)


def expand_indices(
    event_ns: np.ndarray, cause: np.ndarray, grid_ns: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`(row_index, interval_idx 1-based, target)` of every person-period row, in row then interval order."""
    n_surv, ev_int = interval_counts(event_ns, cause, grid_ns)
    per_row = n_surv + (ev_int > 0)
    total = int(per_row.sum())
    row = np.repeat(np.arange(len(event_ns), dtype=np.int64), per_row)
    starts = np.cumsum(per_row) - per_row
    interval = np.arange(total, dtype=np.int64) - np.repeat(starts, per_row) + 1
    target = np.zeros(total, dtype=np.int8)
    last = starts + per_row - 1
    has_event = ev_int > 0
    target[last[has_event]] = cause[has_event]
    return row, interval, target


def log_tau_end(grid_ns: np.ndarray) -> np.ndarray:
    """`ln(tau_j in seconds)` for j = 1..J."""
    return np.array([math.log(int(t) / 1e9) for t in grid_ns], dtype=np.float64)


def expand_person_period(features: pa.Table, grid_ns: list[int]) -> pa.Table:
    """Person-period rows (SPEC 5.5.3): every input column, then `row_id` (input row index), `interval_idx` (1..J),
    `log_tau_end` (ln of `tau_j` in s) and `target` (0 none, 1 fill, 2 cancel)."""
    grid = check_grid(grid_ns)
    e, c = outcome_arrays(features)
    row, interval, target = expand_indices(e, c, grid)
    out = features.take(pa.array(row, type=pa.int64()))
    lte = log_tau_end(grid)[interval - 1]
    out = out.append_column("row_id", pa.array(row, type=pa.int64()))
    out = out.append_column("interval_idx", pa.array(interval.astype(np.int32), type=pa.int32()))
    out = out.append_column("log_tau_end", pa.array(lte, type=pa.float64()))
    return out.append_column("target", pa.array(target, type=pa.int8()))
