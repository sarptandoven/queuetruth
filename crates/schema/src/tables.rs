//! Arrow table schemas of SPEC 3.4 and 3.8 (column order normative) and their JSON dumps (`schemas/arrow/*.json`).
//!
//! Schema 1.1 (SPEC 3.11) is additive: every table carries `qt.schema_version = "1.1"`; columns added in 1.1 follow
//! the v0.1 columns and are optional per file. `schema(name)` is the v0.1 column set (what unchanged writers
//! produce), `full_schema(name)` every column, `added_columns(name)` the 1.1 ones; readers accept both layouts.

use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock};

use arrow_schema::{DataType, Field, Schema, SchemaRef, TimeUnit};

use crate::{ARROW_SCHEMA_VERSION, ENGINE_VERSION};

/// Column type keys (mirrors `queuetruth.schemas.arrow._TYPES`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Ty {
    U8,
    U16,
    U32,
    U64,
    I8,
    I32,
    I64,
    F64,
    Bool,
    Str,
    LStr,
    Ts,
    Date,
    Dict,
    ListStr,
}

/// One column: name, type, nullability, the allowed dictionary values and the release that added it.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Col {
    pub(crate) name: &'static str,
    pub(crate) ty: Ty,
    pub(crate) nullable: bool,
    pub(crate) values: Option<&'static [&'static str]>,
    /// `None` = v0.1; `Some("1.1")` = added in schema 1.1 (after every v0.1 column of the table).
    pub(crate) since: Option<&'static str>,
}

/// Release tag of the 1.1 columns.
const V1_1: Option<&str> = Some("1.1");

const fn c(name: &'static str, ty: Ty, nullable: bool) -> Col {
    Col {
        name,
        ty,
        nullable,
        values: None,
        since: None,
    }
}

const fn d(name: &'static str, nullable: bool, values: &'static [&'static str]) -> Col {
    Col {
        name,
        ty: Ty::Dict,
        nullable,
        values: Some(values),
        since: None,
    }
}

/// A column added in schema 1.1.
const fn added(col: Col) -> Col {
    Col { since: V1_1, ..col }
}

const INSTRUMENTS: &[Col] = &[
    c("instrument_id", Ty::U32, false),
    c("session_date", Ty::Date, false),
    c("symbol", Ty::Str, false),
    d("venue", false, &["nasdaq", "cme", "coinbase", "generic"]),
    c("source_venue", Ty::Str, false),
    c("price_exp", Ty::I8, false),
    c("tick_size", Ty::I64, false),
    c("lot_size", Ty::U64, false),
    c("size_exp", Ty::I8, false),
    c("contract_multiplier", Ty::F64, false),
    c("currency", Ty::Str, false),
    c("session_tz", Ty::Str, false),
    c("midnight_utc_ns", Ty::I64, false),
    c("open_s", Ty::U32, false),
    c("close_s", Ty::U32, false),
    c("source", Ty::Str, false),
];

const MBO: &[Col] = &[
    c("ts_event", Ty::Ts, false),
    c("ts_recv", Ty::Ts, false),
    c("instrument_id", Ty::U32, false),
    c("action", Ty::U8, false),
    c("side", Ty::U8, false),
    c("price", Ty::I64, false),
    c("size", Ty::U64, false),
    c("order_id", Ty::U64, false),
    c("flags", Ty::U8, false),
    c("sequence", Ty::U64, false),
    c("channel_id", Ty::U16, false),
    c("publisher_id", Ty::U16, false),
];

const BBO: &[Col] = &[
    c("ts_event", Ty::Ts, false),
    c("instrument_id", Ty::U32, false),
    c("sequence", Ty::U64, false),
    c("bid_px", Ty::I64, true),
    c("bid_sz", Ty::U64, false),
    c("bid_ct", Ty::U32, false),
    c("ask_px", Ty::I64, true),
    c("ask_sz", Ty::U64, false),
    c("ask_ct", Ty::U32, false),
];

const DEPTH: &[Col] = &[
    c("ts_event", Ty::Ts, false),
    c("instrument_id", Ty::U32, false),
    c("sequence", Ty::U64, false),
    c("side", Ty::U8, false),
    c("level", Ty::U8, false),
    c("price", Ty::I64, true),
    c("size", Ty::U64, false),
    c("count", Ty::U32, false),
];

const TRADES: &[Col] = &[
    c("ts_event", Ty::Ts, false),
    c("instrument_id", Ty::U32, false),
    c("sequence", Ty::U64, false),
    c("price", Ty::I64, false),
    c("size", Ty::U64, false),
    c("aggressor_side", Ty::U8, false),
    d("kind", false, &["displayed", "hidden", "cross"]),
    c("maker_order_id", Ty::U64, true),
];

const RANK_TRACE: &[Col] = &[
    c("ts_event", Ty::Ts, false),
    c("instrument_id", Ty::U32, false),
    c("sequence", Ty::U64, false),
    c("order_id", Ty::U64, false),
    c("side", Ty::U8, false),
    c("price", Ty::I64, false),
    c("rank", Ty::U32, false),
    c("rank_sweep_adjusted", Ty::U32, false),
    c("level_depth", Ty::U32, false),
    c("sweep_id", Ty::U64, false),
    d("exec_kind", false, &["at_price", "off_price"]),
];

const REAL_ORDERS: &[Col] = &[
    c("instrument_id", Ty::U32, false),
    c("session_date", Ty::Date, false),
    c("order_id", Ty::U64, false),
    c("side", Ty::U8, false),
    c("price", Ty::I64, false),
    c("size_at_add", Ty::U64, false),
    c("ts_add", Ty::Ts, false),
    c("sequence_add", Ty::U64, false),
    c("queue_ahead_at_add", Ty::U64, false),
    c("rank_at_add", Ty::U32, false),
    c("level_qty_at_add", Ty::U64, false),
    c("ts_first_fill", Ty::Ts, true),
    c("ts_last_fill", Ty::Ts, true),
    c("filled_qty", Ty::U64, false),
    c("n_fills", Ty::U32, false),
    c("n_reduces", Ty::U32, false),
    c("ts_end", Ty::Ts, true),
    d(
        "end_reason",
        false,
        &["filled", "cancelled", "replaced", "cleared", "open_at_end"],
    ),
    c("in_snapshot", Ty::Bool, false),
];

const SHADOW_SPEC: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("instrument_id", Ty::U32, false),
    c("side", Ty::U8, false),
    d("price_mode", false, &["abs", "touch"]),
    c("price", Ty::I64, true),
    c("offset_ticks", Ty::I32, false),
    c("qty", Ty::U64, false),
    c("ts_submit", Ty::Ts, false),
    c("latency_ns", Ty::I64, false),
    c("ts_cancel", Ty::Ts, true),
    c("group", Ty::Str, true),
    d("source", false, &["sampled", "live_log", "real_order_mirror", "user"]),
    c("order_key", Ty::Str, true),
];

const SHADOW_AMEND: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("ts_send", Ty::Ts, false),
    c("new_qty", Ty::U64, false),
];

const SHADOW_RESULTS: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("instrument_id", Ty::U32, false),
    c("side", Ty::U8, false),
    c("price", Ty::I64, true),
    c("qty", Ty::U64, false),
    c("ts_submit", Ty::Ts, false),
    c("ts_live", Ty::Ts, false),
    c("ts_activated_event", Ty::Ts, true),
    c("ts_cancel_effective", Ty::Ts, true),
    c("queue_ahead_at_live", Ty::U64, true),
    c("rank_at_live", Ty::U32, true),
    c("level_qty_at_live", Ty::U64, true),
    c("ts_first_fill", Ty::Ts, true),
    c("ts_last_fill", Ty::Ts, true),
    c("ts_full_fill", Ty::Ts, true),
    c("filled_qty", Ty::U64, false),
    c("n_fills", Ty::U32, false),
    c("queue_ahead_at_first_fill", Ty::U64, true),
    d(
        "first_fill_reason",
        false,
        &["queue_depleted", "trade_through", "crossed", "crossed_at_live", "never"],
    ),
    d(
        "end_state",
        false,
        &[
            "filled",
            "cancelled",
            "amended_to_filled",
            "censored_session_end",
            "censored_clear",
            "censored_gap",
            "open_at_end",
            "rejected_would_cross",
            "rejected_no_touch",
        ],
    ),
    c("latency_applied_ns", Ty::I64, false),
    c("ambiguous_race", Ty::Bool, false),
    c("out_of_session", Ty::Bool, false),
    c("dq_flags", Ty::U32, false),
    added(c("ts_planned_end", Ty::Ts, true)),
    added(d("planned_end_kind", false, &["cancel", "session_close", "none"])),
];

const SHADOW_FILLS: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("fill_seq", Ty::U32, false),
    c("ts_event", Ty::Ts, false),
    c("sequence", Ty::U64, false),
    c("fill_qty", Ty::U64, false),
    c("cum_qty", Ty::U64, false),
    c("leaves_qty", Ty::U64, false),
    c("fill_px", Ty::I64, false),
    d(
        "reason",
        false,
        &["queue_depleted", "trade_through", "crossed", "crossed_at_live"],
    ),
    c("queue_ahead_before", Ty::U64, false),
    c("traded_at_px_event", Ty::U64, false),
    c("trade_through_event", Ty::U64, false),
    c("behind_volume_executed", Ty::U64, false),
    added(d("liquidity", false, LIQUIDITY)),
];

/// Values of every 1.1 `liquidity` column.
const LIQUIDITY: &[&str] = &["maker", "taker"];

const QUEUE_TRAJECTORY: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("ts_event", Ty::Ts, false),
    c("sequence", Ty::U64, false),
    d("kind", false, &["live", "event", "end"]),
    c("queue_ahead_before", Ty::U64, false),
    c("queue_ahead_after", Ty::U64, false),
    c("rank_before", Ty::U32, false),
    c("rank_after", Ty::U32, false),
    c("level_qty_before", Ty::U64, false),
    c("level_qty_after", Ty::U64, false),
    c("cancel_ahead_qty", Ty::U64, false),
    c("cancel_behind_qty", Ty::U64, false),
    c("add_behind_qty", Ty::U64, false),
    c("traded_at_px_qty", Ty::U64, false),
    c("trade_through_qty", Ty::U64, false),
    c("filled_qty", Ty::U64, false),
];

const L2: &[Col] = &[
    c("ts_event", Ty::Ts, false),
    c("ts_recv", Ty::Ts, false),
    c("instrument_id", Ty::U32, false),
    c("event_id", Ty::U64, false),
    c("sequence", Ty::U64, false),
    d("kind", false, &["depth", "trade", "snapshot", "clear"]),
    c("side", Ty::U8, false),
    c("price", Ty::I64, true),
    c("prev_qty", Ty::U64, false),
    c("new_qty", Ty::U64, false),
    c("prev_ct", Ty::U32, true),
    c("new_ct", Ty::U32, true),
    c("trade_qty", Ty::U64, false),
    c("aggressor", Ty::U8, false),
    c("level", Ty::U8, false),
    c("flags", Ty::U8, false),
];

const ZOO_PREDICTIONS: &[Col] = &[
    c("model_id", Ty::Str, false),
    c("shadow_id", Ty::Str, false),
    c("qty", Ty::U64, false),
    c("horizon_ns", Ty::I64, false),
    c("p_fill", Ty::F64, false),
    c("e_fill_qty", Ty::F64, false),
    c("model_ts_first_fill", Ty::Ts, true),
    c("truth_filled", Ty::Bool, true),
    c("truth_ts_first_fill", Ty::Ts, true),
    c("truth_filled_qty", Ty::U64, true),
    c("censored", Ty::Bool, false),
    c("queue_ahead_at_live", Ty::U64, true),
    c("level_qty_at_live", Ty::U64, true),
    c("visible_level_qty_at_live", Ty::U64, false),
    c("out_of_view_at_live", Ty::Bool, false),
    c("instrument_id", Ty::U32, false),
    c("session_date", Ty::Date, false),
    c("ts_live", Ty::Ts, false),
    c("trade_vol_60s", Ty::U64, false),
    added(d("liquidity", true, LIQUIDITY)),
    added(d("status", true, ZOO_PREDICTION_STATUS)),
];

/// Values of `qt.zoo_predictions.status` (null = an ordinary prediction): `hbt_312_abort` = the faithful hbt pair
/// was aborted at its #312 `InvalidOrderStatus` event at or before this row's deadline (qt ends only
/// that pair where hbt ends the run, see E8); such rows are not predictions and metrics skip them.
pub const ZOO_PREDICTION_STATUS: &[&str] = &["hbt_312_abort"];

const ZOO_FILLS: &[Col] = &[
    c("model_id", Ty::Str, false),
    c("shadow_id", Ty::Str, false),
    c("fill_seq", Ty::U32, false),
    c("ts_event", Ty::Ts, false),
    c("fill_qty", Ty::F64, false),
    c("cum_qty", Ty::F64, false),
    c("queue_ahead_est_before", Ty::F64, false),
    d(
        "reason",
        false,
        &["queue", "trade_through", "crossed", "crossed_at_live", "touch_draw"],
    ),
    d("quirk", false, &["none", "hbt_312", "hbt_nan_reset"]),
    added(d("liquidity", false, LIQUIDITY)),
];

const ZOO_TRAJECTORY: &[Col] = &[
    c("model_id", Ty::Str, false),
    c("shadow_id", Ty::Str, false),
    c("ts_event", Ty::Ts, false),
    c("queue_ahead_est", Ty::F64, false),
    c("queue_ahead_exact", Ty::U64, false),
];

/// Registered in 1.1 (3.11 #5); the table itself is new, so its columns are its v0.1 layout.
const ZOO_SHADOWS: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("ts_cancel_planned", Ty::Ts, true),
    c("ts_admin_end", Ty::Ts, true),
    c("instrument_id", Ty::U32, false),
    c("session_date", Ty::Date, false),
    c("ts_live", Ty::Ts, false),
    c("session_close", Ty::Ts, false),
    c("exact_end_state", Ty::Str, true),
    c("never_cancel", Ty::Bool, false),
];

const ZOO_ERRORS: &[Col] = &[
    c("model_id", Ty::Str, false),
    c("metric", Ty::Str, false),
    c("horizon_ns", Ty::I64, true),
    d(
        "stratum_kind",
        false,
        &["all", "queue_decile", "liquidity_bucket", "instrument"],
    ),
    c("stratum", Ty::Str, false),
    c("value", Ty::F64, false),
    c("ci_lo", Ty::F64, true),
    c("ci_hi", Ty::F64, true),
    c("n", Ty::U64, false),
];

const ALLOC_EVENTS: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("instrument_id", Ty::U32, false),
    c("session_date", Ty::Date, false),
    c("ts_event", Ty::Ts, false),
    c("front", Ty::F64, false),
    c("back", Ty::F64, false),
    c("ahead_cancel", Ty::F64, false),
    c("behind_cancel", Ty::F64, false),
    c("l2_prev", Ty::F64, true),
    c("l2_new", Ty::F64, true),
    c("l2_trades", Ty::F64, true),
];

const TOUCH_RECORDS: &[Col] = &[
    c("shadow_id", Ty::Str, false),
    c("ts_event", Ty::Ts, false),
    c("touch_index", Ty::U32, false),
    c("nt_front_before", Ty::F64, false),
    c("nt_excess", Ty::F64, false),
    c("truth_filled_now", Ty::Bool, false),
    c("truth_first_fill_here", Ty::Bool, false),
];

const FEATURES: &[Col] = &[
    c("order_ref", Ty::Str, false),
    d("population", false, &["shadows", "real_orders"]),
    c("instrument_id", Ty::U32, false),
    c("session_date", Ty::Date, false),
    c("ts_landmark", Ty::Ts, false),
    c("landmark_ns", Ty::I64, false),
    c("side_sign", Ty::I8, false),
    c("event_ns", Ty::I64, false),
    d("cause", false, &["fill", "cancel", "censor"]),
    // 1.1: local midnight of the order's instrument-day in UTC ns (`qt.instruments`), for the exact
    // exchange-local time of day of `ts_landmark` (null in files written before 1.1).
    added(c("midnight_utc_ns", Ty::I64, true)),
];

const LIVE_ORDERS: &[Col] = &[
    c("order_key", Ty::Str, false),
    c("venue", Ty::Str, false),
    c("account", Ty::Str, false),
    c("instrument", Ty::Str, false),
    c("instrument_id", Ty::U32, true),
    c("venue_order_id", Ty::Str, true),
    c("client_order_id", Ty::Str, true),
    c("client_order_id_chain", Ty::ListStr, true),
    d("side", false, &["buy", "sell"]),
    d("order_type", false, &["limit", "market", "stop", "stop_limit", "other"]),
    d(
        "tif",
        false,
        &["gtc", "ioc", "fok", "post_only", "gtx", "day", "other", "gtd"],
    ),
    c("post_only", Ty::Bool, false),
    c("reduce_only", Ty::Bool, true),
    c("orig_qty", Ty::U64, false),
    c("qty_str", Ty::Str, false),
    c("limit_px", Ty::I64, true),
    c("px_str", Ty::Str, true),
    c("price_exp", Ty::I8, false),
    c("size_exp", Ty::I8, false),
    c("strategy_tag", Ty::Str, true),
    d("validation_status", false, &["real", "fixture_only", "simulated"]),
    c("raw", Ty::LStr, true),
    added(c("replaces_order_key", Ty::Str, true)),
];

const LIVE_EVENTS: &[Col] = &[
    c("order_key", Ty::Str, false),
    c("event_seq", Ty::U32, false),
    d(
        "event_type",
        false,
        &[
            "submit_sent",
            "ack",
            "reject",
            "post_only_reject",
            "amend_sent",
            "amend_ack",
            "amend_reject",
            "cancel_sent",
            "cancel_ack",
            "cancel_reject",
            "fill",
            "fill_bust",
            "fill_correct",
            "expire",
            "restate",
            "done",
        ],
    ),
    c("qty", Ty::U64, true),
    c("px", Ty::I64, true),
    c("leaves_qty", Ty::U64, true),
    c("cum_qty", Ty::U64, true),
    c("fill_id", Ty::Str, true),
    c("ref_fill_id", Ty::Str, true),
    d("liquidity", true, &["maker", "taker", "unknown"]),
    c("fee", Ty::F64, true),
    c("fee_ccy", Ty::Str, true),
    c("amend_keeps_priority", Ty::Bool, true),
    c("ts_local_send", Ty::Ts, true),
    c("ts_exchange", Ty::Ts, true),
    c("ts_local_recv", Ty::Ts, true),
    d(
        "ts_exchange_kind",
        false,
        &["match_time", "record_time", "gateway_time", "unknown"],
    ),
    d("ts_precision", false, &["s", "ms", "us", "ns"]),
    d(
        "source",
        false,
        &["ws", "rest", "fix_dropcopy", "flex", "csv", "synthetic", "sim"],
    ),
    c("reason", Ty::Str, true),
    c("derived", Ty::Bool, false),
    c("raw", Ty::LStr, true),
];

const SCORE_ORDERS: &[Col] = &[
    c("order_key", Ty::Str, false),
    c("instrument_id", Ty::U32, true),
    c("session_date", Ty::Date, false),
    d("side", false, &["buy", "sell"]),
    c("limit_px", Ty::I64, false),
    c("qty", Ty::U64, false),
    c("ts_entry", Ty::Ts, false),
    c("live_filled", Ty::Bool, false),
    c("live_filled_qty", Ty::U64, false),
    c("live_ts_first_fill", Ty::Ts, true),
    c("sim_filled", Ty::Bool, false),
    c("sim_filled_qty", Ty::U64, false),
    c("sim_ts_first_fill", Ty::Ts, true),
    d("class", false, &["both", "neither", "live_only", "sim_only"]),
    d(
        "live_end",
        false,
        &["filled", "cancelled", "expired", "rejected", "open"],
    ),
    c("queue_ahead_at_entry", Ty::U64, true),
    d("queue_ahead_source", false, &["exact", "l2_visible", "none"]),
    c("queue_decile", Ty::U8, true),
    c("liquidity_bucket", Ty::U8, true),
    c("ttf_live_ns", Ty::I64, true),
    c("ttf_sim_ns", Ty::I64, true),
    c("overlap_same_px", Ty::Bool, false),
    c("sim_model_id", Ty::Str, false),
    c("trade_vol_60s", Ty::U64, true),
    c("legs", Ty::U32, false),
    added(c("price_exp", Ty::I8, true)),
    added(c("size_exp", Ty::I8, true)),
];

const SCORE_FILLS: &[Col] = &[
    c("order_key", Ty::Str, false),
    d("arm", false, &["live", "sim"]),
    c("fill_seq", Ty::U32, false),
    c("ts", Ty::Ts, false),
    c("qty", Ty::U64, false),
    c("px", Ty::I64, true),
    d("liquidity", false, &["maker", "taker", "unknown"]),
    added(d("side", false, &["buy", "sell"])),
    added(c("instrument_id", Ty::U32, true)),
    added(c("fee", Ty::F64, true)),
    added(c("fee_ccy", Ty::Str, true)),
    added(c("price_exp", Ty::I8, true)),
    added(c("size_exp", Ty::I8, true)),
];

const SCORE_MARKOUTS: &[Col] = &[
    c("order_key", Ty::Str, false),
    d("arm", false, &["live", "sim"]),
    c("fill_seq", Ty::U32, false),
    c("horizon_ns", Ty::I64, false),
    d("ref", false, &["mid", "microprice"]),
    c("mo_ticks", Ty::F64, true),
    c("mo_ccy", Ty::F64, true),
    c("mm_ticks", Ty::F64, true),
    added(c("mo_bps", Ty::F64, true)),
    added(c("mo_net_ccy", Ty::F64, true)),
];

const SCORE_POSITION: &[Col] = &[
    d("arm", false, &["live", "sim"]),
    c("instrument_id", Ty::U32, false),
    c("ts", Ty::Ts, false),
    c("position", Ty::I64, false),
];

const SCORE_TESTS: &[Col] = &[
    c("test_id", Ty::Str, false),
    d("family", false, &["primary", "secondary"]),
    c("stratum_kind", Ty::Str, false),
    c("stratum", Ty::Str, false),
    c("statistic", Ty::Str, false),
    c("value", Ty::F64, false),
    c("p_value", Ty::F64, true),
    c("p_adj", Ty::F64, true),
    d("adj_method", false, &["none", "holm", "bh", "by"]),
    c("ci_lo", Ty::F64, true),
    c("ci_hi", Ty::F64, true),
    c("n", Ty::U64, false),
    c("note", Ty::Str, true),
];

const HAZARD_PREDICTIONS: &[Col] = &[
    c("order_ref", Ty::Str, false),
    c("landmark_ns", Ty::I64, false),
    c("horizon_ns", Ty::I64, false),
    c("cif_fill_raw", Ty::F64, false),
    c("cif_fill", Ty::F64, false),
    c("cif_cancel", Ty::F64, false),
    c("survival", Ty::F64, false),
    c("y_fill", Ty::Bool, true),
    c("censored", Ty::Bool, false),
    d("split", false, &["train", "calib", "test"]),
];

/// Every table in definition order.
const TABLES: &[(&str, &[Col])] = &[
    ("qt.instruments", INSTRUMENTS),
    ("qt.mbo", MBO),
    ("qt.bbo", BBO),
    ("qt.depth", DEPTH),
    ("qt.trades", TRADES),
    ("qt.rank_trace", RANK_TRACE),
    ("qt.real_orders", REAL_ORDERS),
    ("qt.shadow_spec", SHADOW_SPEC),
    ("qt.shadow_amend", SHADOW_AMEND),
    ("qt.shadow_results", SHADOW_RESULTS),
    ("qt.shadow_fills", SHADOW_FILLS),
    ("qt.queue_trajectory", QUEUE_TRAJECTORY),
    ("qt.l2", L2),
    ("qt.zoo_predictions", ZOO_PREDICTIONS),
    ("qt.zoo_fills", ZOO_FILLS),
    ("qt.zoo_trajectory", ZOO_TRAJECTORY),
    ("qt.zoo_shadows", ZOO_SHADOWS),
    ("qt.zoo_errors", ZOO_ERRORS),
    ("qt.alloc_events", ALLOC_EVENTS),
    ("qt.touch_records", TOUCH_RECORDS),
    ("qt.features", FEATURES),
    ("qt.live_orders", LIVE_ORDERS),
    ("qt.live_events", LIVE_EVENTS),
    ("qt.score_orders", SCORE_ORDERS),
    ("qt.score_fills", SCORE_FILLS),
    ("qt.score_markouts", SCORE_MARKOUTS),
    ("qt.score_position", SCORE_POSITION),
    ("qt.score_tests", SCORE_TESTS),
    ("qt.hazard_predictions", HAZARD_PREDICTIONS),
];

/// Index in `qt.features` before which the dynamic float64 feature columns are inserted (after `side_sign`).
pub const FEATURES_INSERT_AT: usize = 7;

/// The `timestamp[ns, tz="UTC"]` type.
pub fn ts_type() -> DataType {
    DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()))
}

/// The `dictionary<int8, utf8>` type.
pub fn dict_type() -> DataType {
    DataType::Dictionary(Box::new(DataType::Int8), Box::new(DataType::Utf8))
}

fn data_type(t: Ty) -> DataType {
    match t {
        Ty::U8 => DataType::UInt8,
        Ty::U16 => DataType::UInt16,
        Ty::U32 => DataType::UInt32,
        Ty::U64 => DataType::UInt64,
        Ty::I8 => DataType::Int8,
        Ty::I32 => DataType::Int32,
        Ty::I64 => DataType::Int64,
        Ty::F64 => DataType::Float64,
        Ty::Bool => DataType::Boolean,
        Ty::Str => DataType::Utf8,
        Ty::LStr => DataType::LargeUtf8,
        Ty::Ts => ts_type(),
        Ty::Date => DataType::Date32,
        Ty::Dict => dict_type(),
        Ty::ListStr => DataType::List(Arc::new(Field::new_list_field(DataType::Utf8, true))),
    }
}

/// Schema-level metadata of every table (SPEC 0.1).
pub fn table_metadata(name: &str) -> std::collections::HashMap<String, String> {
    [
        ("qt.schema".to_owned(), name.to_owned()),
        ("qt.schema_version".to_owned(), ARROW_SCHEMA_VERSION.to_owned()),
        ("qt.engine_version".to_owned(), ENGINE_VERSION.to_owned()),
    ]
    .into_iter()
    .collect()
}

fn build(name: &str, cols: &[Col]) -> SchemaRef {
    let fields: Vec<Field> = cols
        .iter()
        .map(|c| Field::new(c.name, data_type(c.ty), c.nullable))
        .collect();
    Arc::new(Schema::new_with_metadata(fields, table_metadata(name)))
}

fn v0_1(cols: &[Col]) -> Vec<Col> {
    cols.iter().copied().filter(|c| c.since.is_none()).collect()
}

/// (v0.1 layout, full 1.1 layout) per table.
fn registry() -> &'static BTreeMap<&'static str, (SchemaRef, SchemaRef)> {
    static REG: OnceLock<BTreeMap<&'static str, (SchemaRef, SchemaRef)>> = OnceLock::new();
    REG.get_or_init(|| {
        TABLES
            .iter()
            .map(|(n, cols)| (*n, (build(n, &v0_1(cols)), build(n, cols))))
            .collect()
    })
}

/// Every column of a table, 1.1 additions included.
pub(crate) fn columns(name: &str) -> Option<&'static [Col]> {
    TABLES.iter().find(|(n, _)| *n == name).map(|(_, c)| *c)
}

/// The Arrow schema of a table (`"qt.mbo"`, ...) in its v0.1 column layout (metadata `qt.schema_version = "1.1"`);
/// `qt.features` holds only its fixed columns. Unchanged writers keep producing this layout.
pub fn schema(name: &str) -> Option<SchemaRef> {
    registry().get(name).map(|(core, _)| core.clone())
}

/// The full 1.1 layout of a table: the v0.1 columns followed by the columns added in 1.1 (SPEC 3.11); equal to
/// `schema(name)` for tables without additions.
pub fn full_schema(name: &str) -> Option<SchemaRef> {
    registry().get(name).map(|(_, full)| full.clone())
}

/// Names of the columns a table gained in 1.1, in column order (empty when none; `None` for an unknown table).
pub fn added_columns(name: &str) -> Option<Vec<&'static str>> {
    columns(name).map(|cols| cols.iter().filter(|c| c.since.is_some()).map(|c| c.name).collect())
}

/// Every table name in definition order.
pub fn schema_names() -> &'static [&'static str] {
    static NAMES: OnceLock<Vec<&'static str>> = OnceLock::new();
    NAMES.get_or_init(|| TABLES.iter().map(|(n, _)| *n).collect())
}

/// Allowed values of a dictionary column, in canonical order.
pub fn dict_values(table: &str, column: &str) -> Option<&'static [&'static str]> {
    columns(table)?.iter().find(|c| c.name == column)?.values
}

/// `qt.features` in its v0.1 layout with one nullable float64 column per feature, inserted after `side_sign`
/// (what `features_schema` writers before 1.1 produced).
pub fn features_schema(feature_names: &[&str]) -> crate::QtResult<SchemaRef> {
    features_layout(feature_names, false)
}

/// `qt.features` in its full 1.1 layout: `features_schema` followed by the 1.1 columns (`midnight_utc_ns`).
pub fn features_full_schema(feature_names: &[&str]) -> crate::QtResult<SchemaRef> {
    features_layout(feature_names, true)
}

fn features_layout(feature_names: &[&str], full: bool) -> crate::QtResult<SchemaRef> {
    let all = columns("qt.features").unwrap_or(&[]);
    let base: Vec<Col> = if full { all.to_vec() } else { v0_1(all) };
    let mut seen: std::collections::BTreeSet<&str> = all.iter().map(|c| c.name).collect();
    let mut fields: Vec<Field> = Vec::with_capacity(base.len() + feature_names.len());
    for c in &base[..FEATURES_INSERT_AT] {
        fields.push(Field::new(c.name, data_type(c.ty), c.nullable));
    }
    for f in feature_names {
        if !seen.insert(f) {
            return Err(crate::QtError::Config(format!(
                "features: duplicate or reserved feature column {f:?}"
            )));
        }
        fields.push(Field::new(*f, DataType::Float64, true));
    }
    for c in &base[FEATURES_INSERT_AT..] {
        fields.push(Field::new(c.name, data_type(c.ty), c.nullable));
    }
    Ok(Arc::new(Schema::new_with_metadata(
        fields,
        table_metadata("qt.features"),
    )))
}

/// The feature column names of a `qt.features` schema and whether it is the full 1.1 layout (ends with the 1.1
/// columns), or `None` when it has fewer columns than the v0.1 fixed columns. Feature columns are the columns
/// between `side_sign` and `event_ns`; the caller checks the rest against `features_schema` /
/// `features_full_schema`.
pub fn features_columns(schema: &Schema) -> Option<(Vec<String>, bool)> {
    let all = columns("qt.features")?;
    let added: Vec<&str> = all.iter().filter(|c| c.since.is_some()).map(|c| c.name).collect();
    let core_tail = all.iter().filter(|c| c.since.is_none()).count() - FEATURES_INSERT_AT;
    let names: Vec<&str> = schema.fields().iter().map(|f| f.name().as_str()).collect();
    let n = names.len();
    if n < FEATURES_INSERT_AT + core_tail {
        return None;
    }
    let full = n >= FEATURES_INSERT_AT + core_tail + added.len() && names[n - added.len()..] == added[..];
    let tail = core_tail + if full { added.len() } else { 0 };
    let feats = names[FEATURES_INSERT_AT..n - tail]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
    Some((feats, full))
}

/// Canonical type string used by the schema dumps (same rendering as `queuetruth.schemas.arrow.type_name`).
pub fn type_name(t: &DataType) -> String {
    match t {
        DataType::UInt8 => "uint8".into(),
        DataType::UInt16 => "uint16".into(),
        DataType::UInt32 => "uint32".into(),
        DataType::UInt64 => "uint64".into(),
        DataType::Int8 => "int8".into(),
        DataType::Int16 => "int16".into(),
        DataType::Int32 => "int32".into(),
        DataType::Int64 => "int64".into(),
        DataType::Float64 => "float64".into(),
        DataType::Boolean => "bool".into(),
        DataType::Utf8 => "utf8".into(),
        DataType::LargeUtf8 => "large_utf8".into(),
        DataType::Date32 => "date32".into(),
        DataType::Timestamp(unit, tz) => {
            let u = match unit {
                TimeUnit::Second => "s",
                TimeUnit::Millisecond => "ms",
                TimeUnit::Microsecond => "us",
                TimeUnit::Nanosecond => "ns",
            };
            match tz {
                Some(z) => format!("timestamp[{u}, tz={z}]"),
                None => format!("timestamp[{u}]"),
            }
        }
        DataType::Dictionary(k, v) => format!("dictionary<{}, {}>", type_name(k), type_name(v)),
        DataType::List(f) => format!("list<{}>", type_name(f.data_type())),
        DataType::LargeList(f) => format!("large_list<{}>", type_name(f.data_type())),
        other => format!("{other}"),
    }
}

/// JSON dump of a table schema: name, sorted metadata, fields of the full 1.1 layout (with dictionary values, and
/// `since` on the columns added after v0.1).
pub fn schema_dump(name: &str) -> Option<serde_json::Value> {
    let sch = full_schema(name)?;
    let cols = columns(name)?;
    let mut meta: Vec<(&String, &String)> = sch.metadata().iter().collect();
    meta.sort();
    let mut meta_obj = serde_json::Map::new();
    for (k, v) in meta {
        meta_obj.insert(k.clone(), serde_json::Value::String(v.clone()));
    }
    let fields: Vec<serde_json::Value> = sch
        .fields()
        .iter()
        .zip(cols.iter())
        .map(|(f, c)| {
            let mut o = serde_json::Map::new();
            o.insert("name".into(), f.name().clone().into());
            o.insert("type".into(), type_name(f.data_type()).into());
            o.insert("nullable".into(), f.is_nullable().into());
            if let Some(v) = c.values {
                o.insert("values".into(), v.iter().map(|s| serde_json::Value::from(*s)).collect());
            }
            if let Some(since) = c.since {
                o.insert("since".into(), since.into());
            }
            serde_json::Value::Object(o)
        })
        .collect();
    let mut o = serde_json::Map::new();
    o.insert("name".into(), name.into());
    o.insert("metadata".into(), serde_json::Value::Object(meta_obj));
    let optional: serde_json::Map<String, serde_json::Value> = OPTIONAL_METADATA
        .iter()
        .filter(|(t, _, _)| *t == name)
        .map(|(_, k, d)| ((*k).to_owned(), (*d).into()))
        .collect();
    if !optional.is_empty() {
        o.insert("optional_metadata".into(), serde_json::Value::Object(optional));
    }
    o.insert("fields".into(), fields.into());
    Some(serde_json::Value::Object(o))
}

/// Optional schema-level metadata keys a writer may add to a table file, per table (documented in the dumps as
/// `optional_metadata`; same text as `queuetruth.schemas.arrow.OPTIONAL_METADATA`). Readers must not require them.
pub const OPTIONAL_METADATA: &[(&str, &str, &str)] = &[
    (
        "qt.zoo_trajectory",
        "qt.zoo_trajectory_scope",
        "which shadows the table holds: `all` (write_trajectory) or `traced:<n>` (the n shadows the report's why traces pick)",
    ),
    (
        "qt.live_orders",
        "qt.live.instrument_scales",
        "JSON array of per-instrument scales {venue, instrument, instrument_id, price_exp, size_exp, scale_source, n_orders} (queuetruth.live.instrument_scales)",
    ),
];

/// Pretty JSON text of `schema_dump(name)` plus a trailing newline (the content of `schemas/arrow/<name>.json`).
pub fn schema_json(name: &str) -> Option<String> {
    let v = schema_dump(name)?;
    serde_json::to_string_pretty(&v).ok().map(|s| s + "\n")
}
