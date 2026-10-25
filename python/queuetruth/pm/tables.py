"""Arrow schemas of the prediction-market kit's own tables (Python-only; not registered in
`queuetruth.schemas.arrow.SCHEMAS`, which must mirror the Rust core). Same conventions as SPEC 3.4: schema metadata
`qt.schema`, `qt.schema_version`, `qt.engine_version`; timestamps `timestamp[ns, UTC]`; prices at `price_exp = -4`;
sizes at the instrument's `size_exp`.

```
qt.pm_queue_samples   one row per official queue position (Kalshi GET /portfolio/orders/.../queue_position)
  order_key: utf8 [null] | venue_order_id: utf8 | instrument_id: uint32 [null] | queue_position: uint64
  ts_local_recv: ts | ts_local_send: ts [null] | location: utf8

qt.pm_own_entries     one row per own order (leg): where it joined its level in the published book, and left it
  order_key: utf8 (`<order key>#up<k>`: the re-entry after a size increase) | instrument_id: uint32 | side: uint8
  | price: int64 | seq: uint64 (published batch sequence) | inside: bool (joined during that batch, else just before
  it) | ts_event: ts | how: utf8 (exact | aligned | unaligned | restated | outside) | qty: uint64 | end_seq: uint64
  [null] | end_inside: bool [null] (where the stretch's resting size went to 0 or rose; null while it rests at the end)

qt.pm_chain_fills     one row per decoded OrderFilled (Polymarket)
  tx: utf8 | block: uint64 | log_index: uint32 | ts_block: ts [null] | ts_match: ts [null] (WebSocket time of the
  transaction) | exchange: utf8 | version: utf8 | role: utf8 (maker | taker | operator_fill) | match_idx: int32 [null]
  | maker_pos: int32 [null] (position in the operator's makerOrders array) | order_hash: utf8 | maker: utf8
  | taker: utf8 | side: utf8 (BUY | SELL of the order) | token_id: utf8 | outcome: utf8 [null] (yes | no)
  | instrument_id: uint32 [null] | canon_side: uint8 [null] (B | A on the YES ladder) | price: int64 [null]
  | price_exact: bool | size: uint64 (outcome tokens, 6 decimals) | collateral: uint64 | fee: uint64
  | fee_asset: utf8 | own: bool

qt.pm_score_fills     one row per fill of a `pm score` scenario (or of the live arm)
  scenario: utf8 (live | pessimistic | model | optimistic) | order_key: utf8 | instrument_id: uint32 | ts: ts
  | side: uint8 | price: int64 | qty: uint64 | liquidity: utf8 | reason: utf8 | mid_before: float64 [null]
  (price units) | fee: float64 (dollars, positive = cost)

qt.pm_score_orders    one row per scored order and scenario
  scenario: utf8 | order_key: utf8 | instrument_id: uint32 | side: uint8 | price: int64 | qty: uint64
  | ts_live: ts [null] | ts_end: ts [null] | end: utf8 | filled_qty: uint64 | ts_first_fill: ts [null]
  | trading_pnl: float64 [null] (dollars, `sum s (M - p) q` before fees; null without a mark) | fees: float64

qt.pm_filled_orders   one row per order hash that received a fill (the "filled-orders-only L3")
  order_hash: utf8 | maker: utf8 | instrument_id: uint32 [null] | canon_side: uint8 [null] | price: int64 [null]
  | n_fills: uint32 | filled: uint64 | first_block: uint64 | first_log_index: uint32 | last_block: uint64
  | ts_first_match: ts [null] | own: bool | price_consistent: bool

qt.pm_chain_queue     one row per own order with an on-chain maker fill: the queue ahead of it at the start of the
                      published batch that holds its first fill's print, from the operator's fill order (Polymarket)
  order_key: utf8 | order_hash: utf8 | instrument_id: uint32 | side: uint8 | price: int64 | seq: uint64
  | ts_event: ts (that batch) | tx: utf8 | block: uint64 | log_index: uint32 (the first fill) | ahead_others: uint64
  | ahead_own: uint64 (size of other traders' / the user's other orders filled at the level before it in the
  batch's transactions) | makers_ahead: uint32
```
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from queuetruth.schemas.arrow import ARROW_SCHEMA_VERSION, ENGINE_VERSION

TS = pa.timestamp("ns", tz="UTC")


def _schema(name: str, fields: list[tuple[str, pa.DataType, bool]]) -> pa.Schema:
    meta = {
        b"qt.schema": name.encode(),
        b"qt.schema_version": ARROW_SCHEMA_VERSION.encode(),
        b"qt.engine_version": ENGINE_VERSION.encode(),
    }
    return pa.schema([pa.field(n, t, nullable=null) for n, t, null in fields], metadata=meta)


N = True
PM_SCHEMAS: dict[str, pa.Schema] = {
    "qt.pm_queue_samples": _schema(
        "qt.pm_queue_samples",
        [
            ("order_key", pa.utf8(), N),
            ("venue_order_id", pa.utf8(), False),
            ("instrument_id", pa.uint32(), N),
            ("queue_position", pa.uint64(), False),
            ("ts_local_recv", TS, False),
            ("ts_local_send", TS, N),
            ("location", pa.utf8(), False),
        ],
    ),
    "qt.pm_own_entries": _schema(
        "qt.pm_own_entries",
        [
            ("order_key", pa.utf8(), False),
            ("instrument_id", pa.uint32(), False),
            ("side", pa.uint8(), False),
            ("price", pa.int64(), False),
            ("seq", pa.uint64(), False),
            ("inside", pa.bool_(), False),
            ("ts_event", TS, False),
            ("how", pa.utf8(), False),
            ("qty", pa.uint64(), False),
            ("end_seq", pa.uint64(), N),
            ("end_inside", pa.bool_(), N),
        ],
    ),
    "qt.pm_chain_fills": _schema(
        "qt.pm_chain_fills",
        [
            ("tx", pa.utf8(), False),
            ("block", pa.uint64(), False),
            ("log_index", pa.uint32(), False),
            ("ts_block", TS, N),
            ("ts_match", TS, N),
            ("exchange", pa.utf8(), False),
            ("version", pa.utf8(), False),
            ("role", pa.utf8(), False),
            ("match_idx", pa.int32(), N),
            ("maker_pos", pa.int32(), N),
            ("order_hash", pa.utf8(), False),
            ("maker", pa.utf8(), False),
            ("taker", pa.utf8(), False),
            ("side", pa.utf8(), False),
            ("token_id", pa.utf8(), False),
            ("outcome", pa.utf8(), N),
            ("instrument_id", pa.uint32(), N),
            ("canon_side", pa.uint8(), N),
            ("price", pa.int64(), N),
            ("price_exact", pa.bool_(), False),
            ("size", pa.uint64(), False),
            ("collateral", pa.uint64(), False),
            ("fee", pa.uint64(), False),
            ("fee_asset", pa.utf8(), False),
            ("own", pa.bool_(), False),
        ],
    ),
    "qt.pm_score_fills": _schema(
        "qt.pm_score_fills",
        [
            ("scenario", pa.utf8(), False),
            ("order_key", pa.utf8(), False),
            ("instrument_id", pa.uint32(), False),
            ("ts", TS, False),
            ("side", pa.uint8(), False),
            ("price", pa.int64(), False),
            ("qty", pa.uint64(), False),
            ("liquidity", pa.utf8(), False),
            ("reason", pa.utf8(), False),
            ("mid_before", pa.float64(), N),
            ("fee", pa.float64(), False),
        ],
    ),
    "qt.pm_score_orders": _schema(
        "qt.pm_score_orders",
        [
            ("scenario", pa.utf8(), False),
            ("order_key", pa.utf8(), False),
            ("instrument_id", pa.uint32(), False),
            ("side", pa.uint8(), False),
            ("price", pa.int64(), False),
            ("qty", pa.uint64(), False),
            ("ts_live", TS, N),
            ("ts_end", TS, N),
            ("end", pa.utf8(), False),
            ("filled_qty", pa.uint64(), False),
            ("ts_first_fill", TS, N),
            ("trading_pnl", pa.float64(), N),
            ("fees", pa.float64(), False),
        ],
    ),
    "qt.pm_chain_queue": _schema(
        "qt.pm_chain_queue",
        [
            ("order_key", pa.utf8(), False),
            ("order_hash", pa.utf8(), False),
            ("instrument_id", pa.uint32(), False),
            ("side", pa.uint8(), False),
            ("price", pa.int64(), False),
            ("seq", pa.uint64(), False),
            ("ts_event", TS, False),
            ("tx", pa.utf8(), False),
            ("block", pa.uint64(), False),
            ("log_index", pa.uint32(), False),
            ("ahead_others", pa.uint64(), False),
            ("ahead_own", pa.uint64(), False),
            ("makers_ahead", pa.uint32(), False),
        ],
    ),
    "qt.pm_filled_orders": _schema(
        "qt.pm_filled_orders",
        [
            ("order_hash", pa.utf8(), False),
            ("maker", pa.utf8(), False),
            ("instrument_id", pa.uint32(), N),
            ("canon_side", pa.uint8(), N),
            ("price", pa.int64(), N),
            ("n_fills", pa.uint32(), False),
            ("filled", pa.uint64(), False),
            ("first_block", pa.uint64(), False),
            ("first_log_index", pa.uint32(), False),
            ("last_block", pa.uint64(), False),
            ("ts_first_match", TS, N),
            ("own", pa.bool_(), False),
            ("price_consistent", pa.bool_(), False),
        ],
    ),
}


def table(name: str, rows: list[dict[str, Any]]) -> pa.Table:
    """A table of `PM_SCHEMAS[name]` from row dicts (timestamps as integer ns)."""
    schema = PM_SCHEMAS[name]
    arrays = []
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_timestamp(f.type):
            arrays.append(pa.array(vals, type=pa.int64()).cast(f.type))
        else:
            arrays.append(pa.array(vals, type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


__all__ = ["PM_SCHEMAS", "table"]
