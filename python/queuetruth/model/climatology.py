"""Climatology reference for Brier skill (SPEC 5.5.7): Aalen-Johansen `CIF_fill` of the train split by stratum.

Stratum = instrument x side x distance bucket {<= -1, 0, 1, >= 2} (rounded `dist_ticks`; null is its own bucket)
x 30-minute time-of-day bucket of `ts_landmark` on the exchange-local, DST-safe session clock of
`queuetruth.eval._tod.session_clock_ns` (an optional `midnight_utc_ns` column, else the `tod_sin` / `tod_cos` features
of the same row, else the UTC clock; review F8). A stratum with fewer than 50 distinct train orders falls back to
instrument x side, and an instrument x side absent from train falls back to all train rows.
The Aalen-Johansen estimator is `queuetruth.stats.aalen_johansen` (cause 1 = fill, 2 = cancel, 0 = censor).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa

from queuetruth.model._common import HALF_HOUR_NS, float_matrix, int64_column

MIN_STRATUM_ORDERS = 50
DIST_NULL = 9


def time_of_day_ns(table: pa.Table, ts_column: str) -> tuple[np.ndarray, str]:
    """Per row: DST-safe exchange-local time of day of `ts_column` in ns and the rule used (module docstring)."""
    from queuetruth.eval._tod import session_clock_ns

    names = table.column_names
    mid = None
    if "midnight_utc_ns" in names:
        col = table.column("midnight_utc_ns").combine_chunks()
        ok = np.asarray(col.is_valid().to_numpy(zero_copy_only=False), dtype=bool)
        vals = np.asarray(col.cast(pa.int64()).fill_null(0).to_numpy(zero_copy_only=False), dtype=np.int64)
        mid = (vals, ok)
    tods = float_matrix(table, ["tod_sin", "tod_cos"]) if {"tod_sin", "tod_cos"} <= set(names) else None
    return session_clock_ns(
        int64_column(table, ts_column),
        int64_column(table, "instrument_id"),
        int64_column(table, "session_date"),
        midnight_utc_ns=mid,
        tod_sin=None if tods is None else tods[:, 0],
        tod_cos=None if tods is None else tods[:, 1],
    )


def strata_keys(table: pa.Table) -> tuple[np.ndarray, np.ndarray]:
    """Per row: `(fine key, coarse key)` as int64 (instrument x side x dist x tod, and instrument x side)."""
    inst = int64_column(table, "instrument_id")
    side = int64_column(table, "side_sign")
    if "dist_ticks" in table.column_names:
        d = float_matrix(table, ["dist_ticks"])[:, 0]
        k = np.round(np.nan_to_num(d, nan=0.0))
        dist = np.where(np.isnan(d), DIST_NULL, np.clip(k, -1, 2) + 1).astype(np.int64)
    else:
        dist = np.full(table.num_rows, DIST_NULL, dtype=np.int64)
    tod = time_of_day_ns(table, "ts_landmark")[0] // HALF_HOUR_NS
    coarse = inst * 4 + (side + 1)
    fine = (coarse * 16 + dist) * 64 + tod
    return fine, coarse


def aj_cif_fill(event_ns: np.ndarray, cause: np.ndarray, grid_ns: np.ndarray) -> np.ndarray:
    """Aalen-Johansen fill CIF at each grid point (0 before the first event)."""
    from queuetruth.stats import aalen_johansen

    curves = aalen_johansen(np.asarray(event_ns, dtype=np.float64), np.asarray(cause, dtype=np.int64), causes=(1, 2))
    sf = curves[1]
    t = np.asarray(sf.time, dtype=np.float64)
    v = np.asarray(sf.value, dtype=np.float64)
    idx = np.searchsorted(t, grid_ns.astype(np.float64), side="right") - 1
    return np.where(idx >= 0, v[np.maximum(idx, 0)], 0.0)


@dataclass
class Climatology:
    """Fitted climatology: CIF_fill on the grid per fine stratum, per instrument x side, and overall."""

    grid_ns: np.ndarray
    fine: dict[int, np.ndarray] = field(default_factory=dict)
    coarse: dict[int, np.ndarray] = field(default_factory=dict)
    overall: np.ndarray | None = None

    @classmethod
    def fit(
        cls, table: pa.Table, event_ns: np.ndarray, cause: np.ndarray, order_index: np.ndarray, grid_ns: np.ndarray
    ) -> Climatology:
        """Fits on the train rows given (`order_index` identifies orders for the 50-order rule)."""
        clim = cls(np.asarray(grid_ns, dtype=np.int64))
        if table.num_rows == 0:
            return clim
        fine, coarse = strata_keys(table)
        clim.overall = aj_cif_fill(event_ns, cause, clim.grid_ns)
        for key in np.unique(coarse):
            m = coarse == key
            clim.coarse[int(key)] = aj_cif_fill(event_ns[m], cause[m], clim.grid_ns)
        for key in np.unique(fine):
            m = fine == key
            if np.unique(order_index[m]).size >= MIN_STRATUM_ORDERS:
                clim.fine[int(key)] = aj_cif_fill(event_ns[m], cause[m], clim.grid_ns)
        return clim

    def predict(self, table: pa.Table) -> np.ndarray:
        """CIF_fill at the grid per row, shape (rows, J)."""
        j = len(self.grid_ns)
        out = np.zeros((table.num_rows, j), dtype=np.float64)
        if table.num_rows == 0:
            return out
        if self.overall is None:
            out[:] = np.nan
            return out
        fine, coarse = strata_keys(table)
        for i in range(table.num_rows):
            f = self.fine.get(int(fine[i]))
            if f is None:
                f = self.coarse.get(int(coarse[i]), self.overall)
            out[i] = f
        return out


__all__ = ["MIN_STRATUM_ORDERS", "Climatology", "aj_cif_fill", "strata_keys", "time_of_day_ns"]
