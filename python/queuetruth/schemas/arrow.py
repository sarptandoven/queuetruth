"""pyarrow mirrors of the Arrow table schemas of SPEC 3.4 and 3.8.

`SCHEMAS[name]` must equal `qt_schema::schema(name)` exactly (field names, types, nullability, order and schema
metadata). Dictionary columns are `dictionary<int8, utf8>`; their allowed values are listed in `DICT_VALUES`.
`qt.features` holds only the fixed columns; `features_schema(names)` inserts the float64 feature columns
(`full=True`: also the 1.1 column `midnight_utc_ns` after `cause`).

Schema 1.1 (SPEC 3.11) is additive. Every table carries `qt.schema_version = "1.1"`. Columns added in 1.1 are
appended after the v0.1 columns and are optional per file: a 1.1 table file holds either the v0.1 columns
(`SCHEMAS[name]`, unchanged so that existing writers and readers keep working) or all columns
(`FULL_SCHEMAS[name]` = `qt_schema::full_schema(name)`); `ADDED_COLUMNS[name]` names the 1.1 columns, `conform`
accepts both layouts and `upgrade` adds the 1.1 columns to a v0.1 table (derived where the v0.1 columns imply them,
else null). Dictionary values added in 1.1 (`qt.live_orders.tif` `gtd`, `qt.live_orders.validation_status`
`simulated`, `qt.live_events.source` `sim`) are appended, so v0.1 dictionary indices are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

from queuetruth._version import __version__

#: JSON documents' integer `schema_version` (the major version).
SCHEMA_VERSION = 1
#: `qt.schema_version` of every Arrow table (SPEC 3.11).
ARROW_SCHEMA_VERSION = "1.1"
#: Release that added a column (`since` in `schemas/arrow/*.json`).
V1_1 = "1.1"
ENGINE_VERSION = __version__

TS = pa.timestamp("ns", tz="UTC")
DICT = pa.dictionary(pa.int8(), pa.utf8())

_TYPES: dict[str, pa.DataType] = {
    "u8": pa.uint8(),
    "u16": pa.uint16(),
    "u32": pa.uint32(),
    "u64": pa.uint64(),
    "i8": pa.int8(),
    "i32": pa.int32(),
    "i64": pa.int64(),
    "f64": pa.float64(),
    "bool": pa.bool_(),
    "str": pa.utf8(),
    "lstr": pa.large_utf8(),
    "ts": TS,
    "date": pa.date32(),
    "dict": DICT,
    "list_str": pa.list_(pa.utf8()),
}

# (column, type key, nullable, dictionary values or None, release that added it or None = v0.1); column order is
# normative and 1.1 columns follow every v0.1 column of their table.
_Col = tuple[str, str, bool, tuple[str, ...] | None, str | None]


def _c(name: str, ty: str, nullable: bool = False, since: str | None = None) -> _Col:
    return (name, ty, nullable, None, since)


def _d(name: str, values: str, nullable: bool = False, since: str | None = None) -> _Col:
    return (name, "dict", nullable, tuple(values.split(",")), since)


N = True  # nullable marker

TABLES: dict[str, list[_Col]] = {
    "qt.instruments": [
        _c("instrument_id", "u32"),
        _c("session_date", "date"),
        _c("symbol", "str"),
        _d("venue", "nasdaq,cme,coinbase,generic"),
        _c("source_venue", "str"),
        _c("price_exp", "i8"),
        _c("tick_size", "i64"),
        _c("lot_size", "u64"),
        _c("size_exp", "i8"),
        _c("contract_multiplier", "f64"),
        _c("currency", "str"),
        _c("session_tz", "str"),
        _c("midnight_utc_ns", "i64"),
        _c("open_s", "u32"),
        _c("close_s", "u32"),
        _c("source", "str"),
    ],
    "qt.mbo": [
        _c("ts_event", "ts"),
        _c("ts_recv", "ts"),
        _c("instrument_id", "u32"),
        _c("action", "u8"),
        _c("side", "u8"),
        _c("price", "i64"),
        _c("size", "u64"),
        _c("order_id", "u64"),
        _c("flags", "u8"),
        _c("sequence", "u64"),
        _c("channel_id", "u16"),
        _c("publisher_id", "u16"),
    ],
    "qt.bbo": [
        _c("ts_event", "ts"),
        _c("instrument_id", "u32"),
        _c("sequence", "u64"),
        _c("bid_px", "i64", N),
        _c("bid_sz", "u64"),
        _c("bid_ct", "u32"),
        _c("ask_px", "i64", N),
        _c("ask_sz", "u64"),
        _c("ask_ct", "u32"),
    ],
    "qt.depth": [
        _c("ts_event", "ts"),
        _c("instrument_id", "u32"),
        _c("sequence", "u64"),
        _c("side", "u8"),
        _c("level", "u8"),
        _c("price", "i64", N),
        _c("size", "u64"),
        _c("count", "u32"),
    ],
    "qt.trades": [
        _c("ts_event", "ts"),
        _c("instrument_id", "u32"),
        _c("sequence", "u64"),
        _c("price", "i64"),
        _c("size", "u64"),
        _c("aggressor_side", "u8"),
        _d("kind", "displayed,hidden,cross"),
        _c("maker_order_id", "u64", N),
    ],
    "qt.rank_trace": [
        _c("ts_event", "ts"),
        _c("instrument_id", "u32"),
        _c("sequence", "u64"),
        _c("order_id", "u64"),
        _c("side", "u8"),
        _c("price", "i64"),
        _c("rank", "u32"),
        _c("rank_sweep_adjusted", "u32"),
        _c("level_depth", "u32"),
        _c("sweep_id", "u64"),
        _d("exec_kind", "at_price,off_price"),
    ],
    "qt.real_orders": [
        _c("instrument_id", "u32"),
        _c("session_date", "date"),
        _c("order_id", "u64"),
        _c("side", "u8"),
        _c("price", "i64"),
        _c("size_at_add", "u64"),
        _c("ts_add", "ts"),
        _c("sequence_add", "u64"),
        _c("queue_ahead_at_add", "u64"),
        _c("rank_at_add", "u32"),
        _c("level_qty_at_add", "u64"),
        _c("ts_first_fill", "ts", N),
        _c("ts_last_fill", "ts", N),
        _c("filled_qty", "u64"),
        _c("n_fills", "u32"),
        _c("n_reduces", "u32"),
        _c("ts_end", "ts", N),
        _d("end_reason", "filled,cancelled,replaced,cleared,open_at_end"),
        _c("in_snapshot", "bool"),
    ],
    "qt.shadow_spec": [
        _c("shadow_id", "str"),
        _c("instrument_id", "u32"),
        _c("side", "u8"),
        _d("price_mode", "abs,touch"),
        _c("price", "i64", N),
        _c("offset_ticks", "i32"),
        _c("qty", "u64"),
        _c("ts_submit", "ts"),
        _c("latency_ns", "i64"),
        _c("ts_cancel", "ts", N),
        _c("group", "str", N),
        _d("source", "sampled,live_log,real_order_mirror,user"),
        _c("order_key", "str", N),
    ],
    "qt.shadow_amend": [
        _c("shadow_id", "str"),
        _c("ts_send", "ts"),
        _c("new_qty", "u64"),
    ],
    "qt.shadow_results": [
        _c("shadow_id", "str"),
        _c("instrument_id", "u32"),
        _c("side", "u8"),
        _c("price", "i64", N),
        _c("qty", "u64"),
        _c("ts_submit", "ts"),
        _c("ts_live", "ts"),
        _c("ts_activated_event", "ts", N),
        _c("ts_cancel_effective", "ts", N),
        _c("queue_ahead_at_live", "u64", N),
        _c("rank_at_live", "u32", N),
        _c("level_qty_at_live", "u64", N),
        _c("ts_first_fill", "ts", N),
        _c("ts_last_fill", "ts", N),
        _c("ts_full_fill", "ts", N),
        _c("filled_qty", "u64"),
        _c("n_fills", "u32"),
        _c("queue_ahead_at_first_fill", "u64", N),
        _d("first_fill_reason", "queue_depleted,trade_through,crossed,crossed_at_live,never"),
        _d(
            "end_state",
            "filled,cancelled,amended_to_filled,censored_session_end,censored_clear,censored_gap,"
            "open_at_end,rejected_would_cross,rejected_no_touch",
        ),
        _c("latency_applied_ns", "i64"),
        _c("ambiguous_race", "bool"),
        _c("out_of_session", "bool"),
        _c("dq_flags", "u32"),
        _c("ts_planned_end", "ts", N, since=V1_1),
        _d("planned_end_kind", "cancel,session_close,none", since=V1_1),
    ],
    "qt.shadow_fills": [
        _c("shadow_id", "str"),
        _c("fill_seq", "u32"),
        _c("ts_event", "ts"),
        _c("sequence", "u64"),
        _c("fill_qty", "u64"),
        _c("cum_qty", "u64"),
        _c("leaves_qty", "u64"),
        _c("fill_px", "i64"),
        _d("reason", "queue_depleted,trade_through,crossed,crossed_at_live"),
        _c("queue_ahead_before", "u64"),
        _c("traded_at_px_event", "u64"),
        _c("trade_through_event", "u64"),
        _c("behind_volume_executed", "u64"),
        _d("liquidity", "maker,taker", since=V1_1),
    ],
    "qt.queue_trajectory": [
        _c("shadow_id", "str"),
        _c("ts_event", "ts"),
        _c("sequence", "u64"),
        _d("kind", "live,event,end"),
        _c("queue_ahead_before", "u64"),
        _c("queue_ahead_after", "u64"),
        _c("rank_before", "u32"),
        _c("rank_after", "u32"),
        _c("level_qty_before", "u64"),
        _c("level_qty_after", "u64"),
        _c("cancel_ahead_qty", "u64"),
        _c("cancel_behind_qty", "u64"),
        _c("add_behind_qty", "u64"),
        _c("traded_at_px_qty", "u64"),
        _c("trade_through_qty", "u64"),
        _c("filled_qty", "u64"),
    ],
    "qt.l2": [
        _c("ts_event", "ts"),
        _c("ts_recv", "ts"),
        _c("instrument_id", "u32"),
        _c("event_id", "u64"),
        _c("sequence", "u64"),
        _d("kind", "depth,trade,snapshot,clear"),
        _c("side", "u8"),
        _c("price", "i64", N),
        _c("prev_qty", "u64"),
        _c("new_qty", "u64"),
        _c("prev_ct", "u32", N),
        _c("new_ct", "u32", N),
        _c("trade_qty", "u64"),
        _c("aggressor", "u8"),
        _c("level", "u8"),
        _c("flags", "u8"),
    ],
    "qt.zoo_predictions": [
        _c("model_id", "str"),
        _c("shadow_id", "str"),
        _c("qty", "u64"),
        _c("horizon_ns", "i64"),
        _c("p_fill", "f64"),
        _c("e_fill_qty", "f64"),
        _c("model_ts_first_fill", "ts", N),
        _c("truth_filled", "bool", N),
        _c("truth_ts_first_fill", "ts", N),
        _c("truth_filled_qty", "u64", N),
        _c("censored", "bool"),
        _c("queue_ahead_at_live", "u64", N),
        _c("level_qty_at_live", "u64", N),
        _c("visible_level_qty_at_live", "u64"),
        _c("out_of_view_at_live", "bool"),
        _c("instrument_id", "u32"),
        _c("session_date", "date"),
        _c("ts_live", "ts"),
        _c("trade_vol_60s", "u64"),
        _d("liquidity", "maker,taker", N, since=V1_1),
        # `hbt_312_abort`: the faithful hbt pair was aborted at its #312 InvalidOrderStatus event at or before this
        # row's deadline (qt ends only that pair where hbt ends the run, E8); not a prediction, metrics skip it
        _d("status", "hbt_312_abort", N, since=V1_1),
    ],
    "qt.zoo_fills": [
        _c("model_id", "str"),
        _c("shadow_id", "str"),
        _c("fill_seq", "u32"),
        _c("ts_event", "ts"),
        _c("fill_qty", "f64"),
        _c("cum_qty", "f64"),
        _c("queue_ahead_est_before", "f64"),
        _d("reason", "queue,trade_through,crossed,crossed_at_live,touch_draw"),
        _d("quirk", "none,hbt_312,hbt_nan_reset"),
        _d("liquidity", "maker,taker", since=V1_1),
    ],
    "qt.zoo_trajectory": [
        _c("model_id", "str"),
        _c("shadow_id", "str"),
        _c("ts_event", "ts"),
        _c("queue_ahead_est", "f64"),
        _c("queue_ahead_exact", "u64"),
    ],
    "qt.zoo_shadows": [
        _c("shadow_id", "str"),
        _c("ts_cancel_planned", "ts", N),
        _c("ts_admin_end", "ts", N),
        _c("instrument_id", "u32"),
        _c("session_date", "date"),
        _c("ts_live", "ts"),
        _c("session_close", "ts"),
        _c("exact_end_state", "str", N),
        _c("never_cancel", "bool"),
    ],
    "qt.zoo_errors": [
        _c("model_id", "str"),
        _c("metric", "str"),
        _c("horizon_ns", "i64", N),
        _d("stratum_kind", "all,queue_decile,liquidity_bucket,instrument"),
        _c("stratum", "str"),
        _c("value", "f64"),
        _c("ci_lo", "f64", N),
        _c("ci_hi", "f64", N),
        _c("n", "u64"),
    ],
    "qt.alloc_events": [
        _c("shadow_id", "str"),
        _c("instrument_id", "u32"),
        _c("session_date", "date"),
        _c("ts_event", "ts"),
        _c("front", "f64"),
        _c("back", "f64"),
        _c("ahead_cancel", "f64"),
        _c("behind_cancel", "f64"),
        _c("l2_prev", "f64", N),
        _c("l2_new", "f64", N),
        _c("l2_trades", "f64", N),
    ],
    "qt.touch_records": [
        _c("shadow_id", "str"),
        _c("ts_event", "ts"),
        _c("touch_index", "u32"),
        _c("nt_front_before", "f64"),
        _c("nt_excess", "f64"),
        _c("truth_filled_now", "bool"),
        _c("truth_first_fill_here", "bool"),
    ],
    "qt.features": [
        _c("order_ref", "str"),
        _d("population", "shadows,real_orders"),
        _c("instrument_id", "u32"),
        _c("session_date", "date"),
        _c("ts_landmark", "ts"),
        _c("landmark_ns", "i64"),
        _c("side_sign", "i8"),
        _c("event_ns", "i64"),
        _d("cause", "fill,cancel,censor"),
        # 1.1: local midnight of the order's instrument-day in UTC ns (`qt.instruments`): the exact exchange-local
        # time of day of `ts_landmark` (null in files written before 1.1).
        _c("midnight_utc_ns", "i64", N, since=V1_1),
    ],
    "qt.live_orders": [
        _c("order_key", "str"),
        _c("venue", "str"),
        _c("account", "str"),
        _c("instrument", "str"),
        _c("instrument_id", "u32", N),
        _c("venue_order_id", "str", N),
        _c("client_order_id", "str", N),
        _c("client_order_id_chain", "list_str", N),
        _d("side", "buy,sell"),
        _d("order_type", "limit,market,stop,stop_limit,other"),
        _d("tif", "gtc,ioc,fok,post_only,gtx,day,other,gtd"),
        _c("post_only", "bool"),
        _c("reduce_only", "bool", N),
        _c("orig_qty", "u64"),
        _c("qty_str", "str"),
        _c("limit_px", "i64", N),
        _c("px_str", "str", N),
        _c("price_exp", "i8"),
        _c("size_exp", "i8"),
        _c("strategy_tag", "str", N),
        _d("validation_status", "real,fixture_only,simulated"),
        _c("raw", "lstr", N),
        _c("replaces_order_key", "str", N, since=V1_1),
    ],
    "qt.live_events": [
        _c("order_key", "str"),
        _c("event_seq", "u32"),
        _d(
            "event_type",
            "submit_sent,ack,reject,post_only_reject,amend_sent,amend_ack,amend_reject,cancel_sent,cancel_ack,"
            "cancel_reject,fill,fill_bust,fill_correct,expire,restate,done",
        ),
        _c("qty", "u64", N),
        _c("px", "i64", N),
        _c("leaves_qty", "u64", N),
        _c("cum_qty", "u64", N),
        _c("fill_id", "str", N),
        _c("ref_fill_id", "str", N),
        _d("liquidity", "maker,taker,unknown", N),
        _c("fee", "f64", N),
        _c("fee_ccy", "str", N),
        _c("amend_keeps_priority", "bool", N),
        _c("ts_local_send", "ts", N),
        _c("ts_exchange", "ts", N),
        _c("ts_local_recv", "ts", N),
        _d("ts_exchange_kind", "match_time,record_time,gateway_time,unknown"),
        _d("ts_precision", "s,ms,us,ns"),
        _d("source", "ws,rest,fix_dropcopy,flex,csv,synthetic,sim"),
        _c("reason", "str", N),
        _c("derived", "bool"),
        _c("raw", "lstr", N),
    ],
    "qt.score_orders": [
        _c("order_key", "str"),
        _c("instrument_id", "u32", N),
        _c("session_date", "date"),
        _d("side", "buy,sell"),
        _c("limit_px", "i64"),
        _c("qty", "u64"),
        _c("ts_entry", "ts"),
        _c("live_filled", "bool"),
        _c("live_filled_qty", "u64"),
        _c("live_ts_first_fill", "ts", N),
        _c("sim_filled", "bool"),
        _c("sim_filled_qty", "u64"),
        _c("sim_ts_first_fill", "ts", N),
        _d("class", "both,neither,live_only,sim_only"),
        _d("live_end", "filled,cancelled,expired,rejected,open"),
        _c("queue_ahead_at_entry", "u64", N),
        _d("queue_ahead_source", "exact,l2_visible,none"),
        _c("queue_decile", "u8", N),
        _c("liquidity_bucket", "u8", N),
        _c("ttf_live_ns", "i64", N),
        _c("ttf_sim_ns", "i64", N),
        _c("overlap_same_px", "bool"),
        _c("sim_model_id", "str"),
        _c("trade_vol_60s", "u64", N),
        _c("legs", "u32"),
        _c("price_exp", "i8", N, since=V1_1),
        _c("size_exp", "i8", N, since=V1_1),
    ],
    "qt.score_fills": [
        _c("order_key", "str"),
        _d("arm", "live,sim"),
        _c("fill_seq", "u32"),
        _c("ts", "ts"),
        _c("qty", "u64"),
        _c("px", "i64", N),
        _d("liquidity", "maker,taker,unknown"),
        _d("side", "buy,sell", since=V1_1),
        _c("instrument_id", "u32", N, since=V1_1),
        _c("fee", "f64", N, since=V1_1),
        _c("fee_ccy", "str", N, since=V1_1),
        _c("price_exp", "i8", N, since=V1_1),
        _c("size_exp", "i8", N, since=V1_1),
    ],
    "qt.score_markouts": [
        _c("order_key", "str"),
        _d("arm", "live,sim"),
        _c("fill_seq", "u32"),
        _c("horizon_ns", "i64"),
        _d("ref", "mid,microprice"),
        _c("mo_ticks", "f64", N),
        _c("mo_ccy", "f64", N),
        _c("mm_ticks", "f64", N),
        _c("mo_bps", "f64", N, since=V1_1),
        _c("mo_net_ccy", "f64", N, since=V1_1),
    ],
    "qt.score_position": [
        _d("arm", "live,sim"),
        _c("instrument_id", "u32"),
        _c("ts", "ts"),
        _c("position", "i64"),
    ],
    "qt.score_tests": [
        _c("test_id", "str"),
        _d("family", "primary,secondary"),
        _c("stratum_kind", "str"),
        _c("stratum", "str"),
        _c("statistic", "str"),
        _c("value", "f64"),
        _c("p_value", "f64", N),
        _c("p_adj", "f64", N),
        _d("adj_method", "none,holm,bh,by"),
        _c("ci_lo", "f64", N),
        _c("ci_hi", "f64", N),
        _c("n", "u64"),
        _c("note", "str", N),
    ],
    "qt.hazard_predictions": [
        _c("order_ref", "str"),
        _c("landmark_ns", "i64"),
        _c("horizon_ns", "i64"),
        _c("cif_fill_raw", "f64"),
        _c("cif_fill", "f64"),
        _c("cif_cancel", "f64"),
        _c("survival", "f64"),
        _c("y_fill", "bool", N),
        _c("censored", "bool"),
        _d("split", "train,calib,test"),
    ],
}

#: Index of the first outcome column of `qt.features`; feature columns are inserted before it.
FEATURES_INSERT_AT = 7


def _metadata(name: str) -> dict[bytes, bytes]:
    return {
        b"qt.schema": name.encode(),
        b"qt.schema_version": ARROW_SCHEMA_VERSION.encode(),
        b"qt.engine_version": ENGINE_VERSION.encode(),
    }


def _build(name: str, cols: list[_Col]) -> pa.Schema:
    fields = [pa.field(c[0], _TYPES[c[1]], nullable=c[2]) for c in cols]
    return pa.schema(fields, metadata=_metadata(name))


def _check_order() -> None:
    for name, cols in TABLES.items():
        seen_new = False
        for c in cols:
            if c[4] is not None:
                seen_new = True
            elif seen_new:
                raise AssertionError(f"{name}.{c[0]}: v0.1 column after a 1.1 column")


_check_order()

#: v0.1 column set of every table (what unchanged writers produce); metadata says 1.1.
SCHEMAS: dict[str, pa.Schema] = {name: _build(name, [c for c in cols if c[4] is None]) for name, cols in TABLES.items()}
#: Every column including the 1.1 additions.
FULL_SCHEMAS: dict[str, pa.Schema] = {name: _build(name, cols) for name, cols in TABLES.items()}
#: Columns added in 1.1, per table (empty for tables without additions).
ADDED_COLUMNS: dict[str, tuple[str, ...]] = {
    name: tuple(c[0] for c in cols if c[4] is not None) for name, cols in TABLES.items()
}

DICT_VALUES: dict[str, dict[str, tuple[str, ...]]] = {
    name: {c[0]: c[3] for c in cols if c[3] is not None} for name, cols in TABLES.items()
}


def schema_names() -> list[str]:
    """Table names in definition order (same order as `qt_schema::schema_names()`)."""
    return list(TABLES)


#: Optional schema-level metadata keys a writer may add to a table file, per table (documented in the dumps as
#: `optional_metadata`; same text as `qt_schema::tables::OPTIONAL_METADATA`). Readers must not require them.
OPTIONAL_METADATA: dict[str, dict[str, str]] = {
    "qt.zoo_trajectory": {
        "qt.zoo_trajectory_scope": "which shadows the table holds: `all` (write_trajectory) or `traced:<n>` "
        "(the n shadows the report's why traces pick)"
    },
    "qt.live_orders": {
        "qt.live.instrument_scales": "JSON array of per-instrument scales {venue, instrument, instrument_id, "
        "price_exp, size_exp, scale_source, n_orders} (queuetruth.live.instrument_scales)"
    },
}


def full_schema(name: str) -> pa.Schema:
    """Every column of a table including the 1.1 additions (`qt_schema::full_schema`)."""
    return FULL_SCHEMAS[name]


def conform(table: pa.Table, name: str) -> pa.Table:
    """`table` cast to the v0.1 or the full 1.1 layout of `name`, or to the v0.1 columns plus the first k 1.1
    columns (a file written before a later 1.1 column was added, e.g. `qt.zoo_predictions` with `liquidity` but no
    `status`), whichever its column names match; anything else raises `ValueError`."""
    names = table.schema.names
    full = FULL_SCHEMAS[name]
    for k in range(len(SCHEMAS[name]), len(full) + 1):
        if names == full.names[:k]:
            return table.cast(pa.schema(list(full)[:k], metadata=full.metadata))
    raise ValueError(
        f"{name}: columns {names} match neither the v0.1 nor the 1.1 layout "
        f"(1.1 adds {list(ADDED_COLUMNS[name])} after the v0.1 columns)"
    )


def _liquidity_from_reason(table: pa.Table, taker: tuple[str, ...]) -> pa.Array:
    reason = table.column("reason").to_pylist()
    values = DICT_VALUES_LIQUIDITY
    idx = [None if r is None else (1 if r in taker else 0) for r in reason]
    return pa.DictionaryArray.from_arrays(pa.array(idx, type=pa.int8()), pa.array(list(values), type=pa.utf8()))


#: Values of every 1.1 `liquidity` dictionary column.
DICT_VALUES_LIQUIDITY = ("maker", "taker")
#: Fill reasons that are taker fills (A5 as refined, E48: a resting order crossed later fills as a maker at its
#: own price; only `crossed_at_live` walks the book as a taker): `qt_replay::fill_liquidity`, `qt_zoo::fill_liquidity`.
TAKER_REASONS = ("crossed_at_live",)


def upgrade(table: pa.Table, name: str) -> pa.Table:
    """A table in the v0.1 or the 1.1 layout -> the full 1.1 layout. Added columns: `liquidity` of
    `qt.shadow_fills` / `qt.zoo_fills` derived from `reason` (crossed_at_live = taker, else maker), other
    nullable columns null; a non-nullable added column that cannot be derived (`qt.shadow_results.planned_end_kind`,
    `qt.score_fills.side`) raises `ValueError`."""
    t = conform(table, name)
    full = FULL_SCHEMAS[name]
    if t.schema.names == full.names:
        return t.cast(full)
    for col in ADDED_COLUMNS[name]:
        if col in t.schema.names:
            continue
        f = full.field(col)
        if col == "liquidity" and name in ("qt.shadow_fills", "qt.zoo_fills"):
            arr: pa.Array = _liquidity_from_reason(t, TAKER_REASONS)
        elif f.nullable:
            arr = pa.nulls(t.num_rows, type=f.type)
        else:
            raise ValueError(f"{name}.{col}: not derivable from the v0.1 columns; the writer must provide it")
        t = t.append_column(f, arr)
    return t.cast(full)


def features_schema(feature_names: list[str], *, full: bool = False) -> pa.Schema:
    """`qt.features` with one nullable float64 column per feature name, inserted after `side_sign`: the v0.1 layout,
    or with `full` the 1.1 layout that appends `midnight_utc_ns` (`qt_schema::features_schema` /
    `features_full_schema`; the features crate writes the full layout)."""
    every = TABLES["qt.features"]
    base = every if full else [c for c in every if c[4] is None]
    seen = {c[0] for c in every}
    for f in feature_names:
        if f in seen:
            raise ValueError(f"duplicate or reserved feature column {f!r}")
        seen.add(f)
    cols = base[:FEATURES_INSERT_AT] + [_c(f, "f64", N) for f in feature_names] + base[FEATURES_INSERT_AT:]
    return _build("qt.features", cols)


def type_name(t: pa.DataType) -> str:
    """Canonical type string used in `schemas/arrow/*.json` (same rendering as `qt_schema::schema_json`)."""
    if pa.types.is_timestamp(t):
        return f"timestamp[{t.unit}, tz={t.tz}]" if t.tz else f"timestamp[{t.unit}]"
    if pa.types.is_dictionary(t):
        return f"dictionary<{type_name(t.index_type)}, {type_name(t.value_type)}>"
    if pa.types.is_large_list(t):
        return f"large_list<{type_name(t.value_type)}>"
    if pa.types.is_list(t):
        return f"list<{type_name(t.value_type)}>"
    simple = {
        pa.uint8(): "uint8",
        pa.uint16(): "uint16",
        pa.uint32(): "uint32",
        pa.uint64(): "uint64",
        pa.int8(): "int8",
        pa.int16(): "int16",
        pa.int32(): "int32",
        pa.int64(): "int64",
        pa.float64(): "float64",
        pa.bool_(): "bool",
        pa.utf8(): "utf8",
        pa.large_utf8(): "large_utf8",
        pa.date32(): "date32",
    }
    for k, v in simple.items():
        if t == k:
            return v
    raise ValueError(f"unsupported arrow type {t}")


def schema_dump(name: str, schema: pa.Schema | None = None) -> dict[str, Any]:
    """JSON dump of a table schema: name, metadata (sorted keys), fields (full 1.1 layout) with dictionary values and
    `since` on the columns added after v0.1."""
    sch = FULL_SCHEMAS[name] if schema is None else schema
    values = DICT_VALUES.get(name, {})
    since = {c[0]: c[4] for c in TABLES.get(name, []) if c[4] is not None}
    meta = {k.decode(): v.decode() for k, v in sorted((sch.metadata or {}).items())}
    fields: list[dict[str, Any]] = []
    for f in sch:
        entry: dict[str, Any] = {"name": f.name, "type": type_name(f.type), "nullable": f.nullable}
        if f.name in values:
            entry["values"] = list(values[f.name])
        if f.name in since:
            entry["since"] = since[f.name]
        fields.append(entry)
    out: dict[str, Any] = {"name": name, "metadata": meta}
    if name in OPTIONAL_METADATA:
        out["optional_metadata"] = dict(OPTIONAL_METADATA[name])
    out["fields"] = fields
    return out


def schema_json(name: str) -> str:
    """Pretty JSON text of `schema_dump(name)` with a trailing newline (byte-identical to the Rust dump)."""
    return json.dumps(schema_dump(name), indent=2, ensure_ascii=False) + "\n"
