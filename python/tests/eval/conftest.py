"""Builders for small hand-made `qt.zoo_predictions` / `qt.features` tables."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from queuetruth.schemas import SCHEMAS

S = 1_000_000_000
DAY0 = 20_460  # 2026-01-07 as days since epoch
MIDNIGHT0 = DAY0 * 86_400 * S

_DEFAULTS: dict[str, Any] = {
    "model_id": "m",
    "qty": 1,
    "e_fill_qty": None,
    "model_ts_first_fill": None,
    "truth_filled": None,
    "truth_ts_first_fill": None,
    "truth_filled_qty": None,
    "censored": False,
    "queue_ahead_at_live": 0,
    "level_qty_at_live": 10,
    "visible_level_qty_at_live": 10,
    "out_of_view_at_live": False,
    "instrument_id": 1,
    "session_date": DAY0,
    "ts_live": MIDNIGHT0 + 36_000 * S,
    "trade_vol_60s": 0,
}


def make_predictions(rows: list[dict[str, Any]]) -> pa.Table:
    """`qt.zoo_predictions` table; unspecified columns take defaults, `e_fill_qty` defaults to `p_fill * qty`."""
    full = []
    for r in rows:
        d = {**_DEFAULTS, **r}
        if d["e_fill_qty"] is None:
            d["e_fill_qty"] = float(d["p_fill"]) * d["qty"]
        if d["truth_filled"] is not None and d["truth_filled_qty"] is None:
            d["truth_filled_qty"] = d["qty"] if d["truth_filled"] else 0
        full.append(d)
    schema = SCHEMAS["qt.zoo_predictions"]
    cols = []
    for f in schema:
        vals = [d[f.name] for d in full]
        if pa.types.is_date(f.type):
            cols.append(pa.array(vals, pa.int32()).cast(f.type))
        elif pa.types.is_timestamp(f.type):
            cols.append(pa.array(vals, pa.int64()).cast(f.type))
        else:
            cols.append(pa.array(vals, f.type))
    return pa.Table.from_arrays(cols, schema=schema)


@pytest.fixture
def predictions() -> Callable[[list[dict[str, Any]]], pa.Table]:
    """The `make_predictions` builder."""
    return make_predictions


def planned_from_censored(table: pa.Table) -> pa.Table:
    """A `planned_life` table that encodes each shadow's `censored` flags as an administrative end: the end sits at
    the last horizon before the shadow's first censored horizon (0 if that is the first), no end when no row is
    censored; no planned cancel. `evaluate_zoo` then sees exactly the eligibility the flags state (tests only:
    `evaluate_zoo` itself never infers eligibility from the flag, E38 OC-7)."""
    from queuetruth.eval import planned_life_table

    rows = table.select(["shadow_id", "horizon_ns", "censored", "ts_live"]).to_pylist()
    horizons = sorted({int(r["horizon_ns"]) for r in rows})
    live: dict[str, int] = {}
    first_cens: dict[str, int] = {}
    for r in rows:
        sid = str(r["shadow_id"])
        ts = r["ts_live"]
        live.setdefault(sid, int(ts.value if hasattr(ts, "value") else ts))
        if r["censored"]:
            first_cens[sid] = min(first_cens.get(sid, 2**62), int(r["horizon_ns"]))
    ids = sorted(live)
    admin: list[int | None] = []
    for sid in ids:
        fc = first_cens.get(sid)
        if fc is None:
            admin.append(None)
        else:
            before = [h for h in horizons if h < fc]
            admin.append(live[sid] + (before[-1] if before else 0))
    return planned_life_table(ids, [None] * len(ids), admin)


@pytest.fixture
def planned() -> Callable[[pa.Table], pa.Table]:
    """The `planned_from_censored` builder."""
    return planned_from_censored
