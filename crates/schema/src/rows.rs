//! Typed rows of the Rust-written tables (SPEC 3.4, 3.6, 3.7) and the enums behind their dictionary columns.
//! `codec` converts slices of rows to Arrow record batches and back.

use qt_types::{BookSide, Dur, Px, Qty, Side, Ts};

/// An enum stored in a `dictionary<int8, utf8>` column; `VALUES` is the canonical value order.
pub trait DictEnum: Sized + Copy + 'static {
    /// Allowed values in canonical order (the dictionary of every batch written by `codec`).
    const VALUES: &'static [&'static str];
    /// Position in `VALUES`.
    fn index(self) -> usize;
    /// Inverse of `index`.
    fn from_index(i: usize) -> Option<Self>;
    /// The string value.
    fn name(self) -> &'static str {
        Self::VALUES[self.index()]
    }
    /// Parses a string value.
    fn from_name(s: &str) -> Option<Self> {
        Self::VALUES.iter().position(|v| *v == s).and_then(Self::from_index)
    }
}

macro_rules! dict_enum {
    ($(#[$m:meta])* $name:ident { $($(#[$vm:meta])* $var:ident => $s:literal),+ $(,)? }) => {
        $(#[$m])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
        pub enum $name {
            $($(#[$vm])* #[serde(rename = $s)] $var),+
        }
        impl DictEnum for $name {
            const VALUES: &'static [&'static str] = &[$($s),+];
            fn index(self) -> usize {
                self as usize
            }
            fn from_index(i: usize) -> Option<Self> {
                const ALL: &[$name] = &[$($name::$var),+];
                ALL.get(i).copied()
            }
        }
        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str(self.name())
            }
        }
        impl std::str::FromStr for $name {
            type Err = qt_types::QtError;
            fn from_str(s: &str) -> Result<Self, Self::Err> {
                <Self as DictEnum>::from_name(s).ok_or_else(|| {
                    qt_types::QtError::Config(format!(
                        "{}: unknown value {s:?} (allowed: {})",
                        stringify!($name),
                        Self::VALUES.join("|")
                    ))
                })
            }
        }
    };
}

dict_enum!(
    /// `qt.trades.kind`.
    TradeKind {
        /// Displayed liquidity.
        Displayed => "displayed",
        /// Hidden print (side N on a venue with hidden orders).
        Hidden => "hidden",
        /// ITCH cross print.
        Cross => "cross",
    }
);

dict_enum!(
    /// `qt.rank_trace.exec_kind`.
    ExecKind {
        /// Fill price equals the resting price.
        AtPrice => "at_price",
        /// Fill price differs from the resting price.
        OffPrice => "off_price",
    }
);

dict_enum!(
    /// `qt.real_orders.end_reason`.
    RealOrderEnd {
        /// Fully filled.
        Filled => "filled",
        /// Removed without being fully filled.
        Cancelled => "cancelled",
        /// Replaced (lost priority; a new life starts).
        Replaced => "replaced",
        /// Book cleared.
        Cleared => "cleared",
        /// Still resting at end of input.
        OpenAtEnd => "open_at_end",
    }
);

dict_enum!(
    /// `qt.shadow_spec.price_mode`.
    PriceMode {
        /// Absolute price.
        Abs => "abs",
        /// Relative to the same-side touch at activation.
        Touch => "touch",
    }
);

dict_enum!(
    /// `qt.shadow_spec.source`.
    ShadowSource {
        /// Sampled by `shadows sample`.
        Sampled => "sampled",
        /// From a live-log schedule.
        LiveLog => "live_log",
        /// Mirror of a real order.
        RealOrderMirror => "real_order_mirror",
        /// Supplied by the user.
        User => "user",
    }
);

dict_enum!(
    /// `qt.shadow_results.first_fill_reason`.
    FirstFillReason {
        /// Queue ahead depleted (A4: executions and removals of the specific real orders ahead, then an execution
        /// of a real order behind the shadow at its price; never off-price prints).
        QueueDepleted => "queue_depleted",
        /// Trade through the price.
        TradeThrough => "trade_through",
        /// Opposite best at or through the price after an event.
        Crossed => "crossed",
        /// Opposite best at or through the price at activation.
        CrossedAtLive => "crossed_at_live",
        /// Never filled.
        Never => "never",
    }
);

dict_enum!(
    /// `qt.shadow_fills.reason`.
    FillReason {
        /// Queue ahead depleted (A4: executions and removals of the specific real orders ahead, then an execution
        /// of a real order behind the shadow at its price; never off-price prints).
        QueueDepleted => "queue_depleted",
        /// Trade through the price.
        TradeThrough => "trade_through",
        /// Opposite best at or through the price after an event.
        Crossed => "crossed",
        /// Opposite best at or through the price at activation.
        CrossedAtLive => "crossed_at_live",
    }
);

dict_enum!(
    /// Shadow end state (`qt.shadow_results.end_state`).
    EndState {
        /// Fully filled.
        Filled => "filled",
        /// Cancel took effect.
        Cancelled => "cancelled",
        /// Amended to a quantity at or below the filled quantity.
        AmendedToFilled => "amended_to_filled",
        /// Live at the end of a partition that reached the session close.
        CensoredSessionEnd => "censored_session_end",
        /// Book cleared.
        CensoredClear => "censored_clear",
        /// Sequence gap, bad-book flag or missing partition.
        CensoredGap => "censored_gap",
        /// Live at the end of input before the close.
        OpenAtEnd => "open_at_end",
        /// Touch-mode price would lock or cross.
        RejectedWouldCross => "rejected_would_cross",
        /// Touch mode without a same-side best.
        RejectedNoTouch => "rejected_no_touch",
    }
);

dict_enum!(
    /// `qt.queue_trajectory.kind`.
    TrajectoryKind {
        /// Activation row.
        Live => "live",
        /// Event row.
        Event => "event",
        /// End row.
        End => "end",
    }
);

dict_enum!(
    /// L2 row kind (`qt.l2.kind`).
    L2Kind {
        /// Level update.
        Depth => "depth",
        /// Trade.
        Trade => "trade",
        /// Snapshot level.
        Snapshot => "snapshot",
        /// Book clear.
        Clear => "clear",
    }
);

dict_enum!(
    /// `qt.zoo_fills.reason`.
    ZooFillReason {
        /// Queue estimate depleted.
        Queue => "queue",
        /// Trade through the price.
        TradeThrough => "trade_through",
        /// Visible opposite best at or through the price.
        Crossed => "crossed",
        /// Crossed at activation.
        CrossedAtLive => "crossed_at_live",
        /// Successful touch draw (NT prob touch).
        TouchDraw => "touch_draw",
    }
);

dict_enum!(
    /// `qt.zoo_fills.quirk`: faithful-mode quirk that fired.
    Quirk {
        /// No quirk.
        None => "none",
        /// hbt #312: fill equal to leaves.
        Hbt312 => "hbt_312",
        /// NaN queue estimate reset to the new level size.
        HbtNanReset => "hbt_nan_reset",
    }
);

dict_enum!(
    /// `qt.features.population`.
    Population {
        /// Shadow orders.
        Shadows => "shadows",
        /// Real orders extracted from MBO.
        RealOrders => "real_orders",
    }
);

dict_enum!(
    /// `qt.features.cause`.
    Cause {
        /// First fill.
        Fill => "fill",
        /// Policy cancel / real cancel.
        Cancel => "cancel",
        /// Censored.
        Censor => "censor",
    }
);

dict_enum!(
    /// 1.1 (3.11 #2, #6): `liquidity` of `qt.shadow_fills`, `qt.zoo_fills`, `qt.zoo_predictions`.
    Liquidity {
        /// Resting (maker) fill.
        Maker => "maker",
        /// Taker fill: the book crossed the order (A5).
        Taker => "taker",
    }
);

dict_enum!(
    /// 1.1 (3.11 #3): `qt.shadow_results.planned_end_kind`.
    PlannedEndKind {
        /// The schedule's cancel.
        Cancel => "cancel",
        /// Session close.
        SessionClose => "session_close",
        /// No planned end (no cancel, no known close).
        None => "none",
    }
);

impl FillReason {
    /// Liquidity implied by a shadow fill reason (A5 as refined, E48): only `crossed_at_live` (marketable on
    /// arrival, walks the book) is taker; a resting order crossed later fills at its own price as a maker.
    pub const fn liquidity(self) -> Liquidity {
        match self {
            Self::CrossedAtLive => Liquidity::Taker,
            Self::Crossed | Self::QueueDepleted | Self::TradeThrough => Liquidity::Maker,
        }
    }
}

impl ZooFillReason {
    /// Liquidity implied by a zoo fill reason (A5 as refined): only `crossed_at_live` is taker.
    pub const fn liquidity(self) -> Liquidity {
        match self {
            Self::CrossedAtLive => Liquidity::Taker,
            Self::Crossed | Self::Queue | Self::TradeThrough | Self::TouchDraw => Liquidity::Maker,
        }
    }
}

/// Bits of `qt.l2.flags`.
pub mod l2flags {
    /// Last row of an emitted batch.
    pub const LAST_IN_BATCH: u8 = 1;
    /// Level left the top-N view without being emptied.
    pub const OUT_OF_VIEW: u8 = 2;
    /// Level entered the view.
    pub const INTO_VIEW: u8 = 4;
    /// Snapshot row.
    pub const SNAPSHOT: u8 = 8;
    /// Net change over a cadence bucket.
    pub const CONFLATED: u8 = 16;
    /// Hidden trade.
    pub const HIDDEN_TRADE: u8 = 32;
}

/// Shadow price: absolute, or ticks relative to the same-side touch at activation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PriceSpec {
    /// Absolute price.
    Abs(Px),
    /// `0` joins the same-side best, `-k` is k ticks behind, `+k` k ticks improving.
    Touch {
        /// Offset in ticks.
        offset_ticks: i32,
    },
}

/// One shadow order (`qt.shadow_spec` row).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShadowSpec {
    /// Unique id.
    pub shadow_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Side.
    pub side: BookSide,
    /// Price rule.
    pub price: PriceSpec,
    /// Quantity in size units.
    pub qty: Qty,
    /// Submit (send) time.
    pub ts_submit: Ts,
    /// Extra per-order latency (>= 0).
    pub latency_ns: Dur,
    /// Cancel send time.
    pub ts_cancel: Option<Ts>,
    /// Free-form group label.
    pub group: Option<String>,
    /// Where the shadow came from.
    pub source: ShadowSource,
    /// Link to `qt.live_orders.order_key`.
    pub order_key: Option<String>,
}

/// Priority-keeping size reduction of a shadow (`qt.shadow_amend` row).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShadowAmend {
    /// Shadow id.
    pub shadow_id: String,
    /// Amend send time.
    pub ts_send: Ts,
    /// Total order quantity after the amend.
    pub new_qty: Qty,
}

/// One L2 row (`qt.l2`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct L2Row {
    /// Batch `LAST` record time.
    pub ts_event: Ts,
    /// Batch `LAST` record capture time.
    pub ts_recv: Ts,
    /// Instrument.
    pub instrument_id: u32,
    /// Batch id, monotone per partition.
    pub event_id: u64,
    /// Source `F_LAST` record sequence.
    pub sequence: u64,
    /// Row kind.
    pub kind: L2Kind,
    /// Level side (trades: the hit side; `N` for hidden prints and clears).
    pub side: Side,
    /// Level or trade price; `None` for clear.
    pub price: Option<Px>,
    /// Quantity the view last showed.
    pub prev_qty: Qty,
    /// New quantity.
    pub new_qty: Qty,
    /// Count the view last showed.
    pub prev_ct: Option<u32>,
    /// New count.
    pub new_ct: Option<u32>,
    /// Trade quantity (trade rows).
    pub trade_qty: Qty,
    /// Aggressor side (trade rows).
    pub aggressor: Side,
    /// Rank in the new view; 255 = out of view.
    pub level: u8,
    /// `l2flags` bits.
    pub flags: u8,
}

/// One BBO row (`qt.bbo`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BboRow {
    /// Event time.
    pub ts_event: Ts,
    /// Instrument.
    pub instrument_id: u32,
    /// Sequence of the event's `LAST` record.
    pub sequence: u64,
    /// Best bid price.
    pub bid_px: Option<Px>,
    /// Best bid total size (0 if empty).
    pub bid_sz: Qty,
    /// Best bid order count.
    pub bid_ct: u32,
    /// Best ask price.
    pub ask_px: Option<Px>,
    /// Best ask total size.
    pub ask_sz: Qty,
    /// Best ask order count.
    pub ask_ct: u32,
}

/// One depth row (`qt.depth`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DepthRow {
    /// Event time.
    pub ts_event: Ts,
    /// Instrument.
    pub instrument_id: u32,
    /// Sequence of the event's `LAST` record.
    pub sequence: u64,
    /// Side.
    pub side: BookSide,
    /// Level (0 = best).
    pub level: u8,
    /// Price; `None` = level vacated.
    pub price: Option<Px>,
    /// Total size.
    pub size: Qty,
    /// Order count.
    pub count: u32,
}

/// One trade row (`qt.trades`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TradeRow {
    /// Trade time.
    pub ts_event: Ts,
    /// Instrument.
    pub instrument_id: u32,
    /// Record sequence.
    pub sequence: u64,
    /// Price.
    pub price: Px,
    /// Size.
    pub size: Qty,
    /// Aggressor side.
    pub aggressor_side: Side,
    /// Displayed / hidden / cross.
    pub kind: TradeKind,
    /// Resting order id when known.
    pub maker_order_id: Option<u64>,
}

/// One rank-oracle row (`qt.rank_trace`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RankTraceRow {
    /// Execution time.
    pub ts_event: Ts,
    /// Instrument.
    pub instrument_id: u32,
    /// Record sequence.
    pub sequence: u64,
    /// Filled order.
    pub order_id: u64,
    /// Resting side.
    pub side: BookSide,
    /// Resting price.
    pub price: Px,
    /// Orders ahead not consumed earlier in the event.
    pub rank: u32,
    /// Rank ignoring orders executed in the same sweep.
    pub rank_sweep_adjusted: u32,
    /// Better levels still holding an unconsumed order.
    pub level_depth: u32,
    /// Sweep id (per-partition counter from 1).
    pub sweep_id: u64,
    /// At-price or off-price execution.
    pub exec_kind: ExecKind,
}

/// One real resting-order life (`qt.real_orders`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RealOrderRow {
    /// Instrument.
    pub instrument_id: u32,
    /// Session date (days since epoch).
    pub session_date: i32,
    /// Order id.
    pub order_id: u64,
    /// Side.
    pub side: BookSide,
    /// Price.
    pub price: Px,
    /// Size at add.
    pub size_at_add: Qty,
    /// Add time.
    pub ts_add: Ts,
    /// Add record sequence.
    pub sequence_add: u64,
    /// Displayed quantity ahead at add.
    pub queue_ahead_at_add: Qty,
    /// Orders ahead at add.
    pub rank_at_add: u32,
    /// Level quantity before the add.
    pub level_qty_at_add: Qty,
    /// First fill time.
    pub ts_first_fill: Option<Ts>,
    /// Last fill time.
    pub ts_last_fill: Option<Ts>,
    /// Filled quantity.
    pub filled_qty: Qty,
    /// Number of fills.
    pub n_fills: u32,
    /// Reductions not paired with a fill.
    pub n_reduces: u32,
    /// End time.
    pub ts_end: Option<Ts>,
    /// End reason.
    pub end_reason: RealOrderEnd,
    /// Added inside a flagged snapshot.
    pub in_snapshot: bool,
}

/// One shadow result (`qt.shadow_results`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShadowResultRow {
    /// Shadow id.
    pub shadow_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Side.
    pub side: BookSide,
    /// Resolved price (`None` if never resolved).
    pub price: Option<Px>,
    /// Final quantity.
    pub qty: Qty,
    /// Submit time.
    pub ts_submit: Ts,
    /// Target activation time.
    pub ts_live: Ts,
    /// `ts_event` of the first event the shadow saw.
    pub ts_activated_event: Option<Ts>,
    /// Effective cancel time.
    pub ts_cancel_effective: Option<Ts>,
    /// Queue ahead at activation.
    pub queue_ahead_at_live: Option<Qty>,
    /// Rank at activation.
    pub rank_at_live: Option<u32>,
    /// Level quantity at activation.
    pub level_qty_at_live: Option<Qty>,
    /// First fill time.
    pub ts_first_fill: Option<Ts>,
    /// Last fill time.
    pub ts_last_fill: Option<Ts>,
    /// Full fill time.
    pub ts_full_fill: Option<Ts>,
    /// Filled quantity.
    pub filled_qty: Qty,
    /// Number of fills.
    pub n_fills: u32,
    /// Queue ahead before the first fill.
    pub queue_ahead_at_first_fill: Option<Qty>,
    /// Reason of the first fill.
    pub first_fill_reason: FirstFillReason,
    /// End state.
    pub end_state: EndState,
    /// `ts_live - ts_submit`.
    pub latency_applied_ns: Dur,
    /// Activation or first fill within the race window.
    pub ambiguous_race: bool,
    /// Submitted outside the continuous session.
    pub out_of_session: bool,
    /// Data-quality bits.
    pub dq_flags: u32,
}

/// One shadow fill (`qt.shadow_fills`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShadowFillRow {
    /// Shadow id.
    pub shadow_id: String,
    /// 1-based fill number.
    pub fill_seq: u32,
    /// Event time.
    pub ts_event: Ts,
    /// Event `LAST` record sequence.
    pub sequence: u64,
    /// Fill quantity.
    pub fill_qty: Qty,
    /// Cumulative filled quantity.
    pub cum_qty: Qty,
    /// Remaining quantity.
    pub leaves_qty: Qty,
    /// Fill price: the shadow's price for maker fills; the opposite level's price for a taker walk
    /// (`crossed_at_live`, A5) and the execution's price for a twin's execution (A1).
    pub fill_px: Px,
    /// Fill reason.
    pub reason: FillReason,
    /// Queue ahead before the fill.
    pub queue_ahead_before: Qty,
    /// At-price traded volume in the event.
    pub traded_at_px_event: Qty,
    /// Trade-through volume in the event.
    pub trade_through_event: Qty,
    /// Displayed volume executed this event against orders behind the shadow.
    pub behind_volume_executed: Qty,
}

/// One queue-trajectory row (`qt.queue_trajectory`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TrajectoryRow {
    /// Shadow id.
    pub shadow_id: String,
    /// Event time (`ts_live` for kind live).
    pub ts_event: Ts,
    /// Event sequence.
    pub sequence: u64,
    /// Row kind.
    pub kind: TrajectoryKind,
    /// Queue ahead before the event.
    pub queue_ahead_before: Qty,
    /// Queue ahead after the event.
    pub queue_ahead_after: Qty,
    /// Rank before.
    pub rank_before: u32,
    /// Rank after.
    pub rank_after: u32,
    /// Displayed level quantity before (excluding the shadow).
    pub level_qty_before: Qty,
    /// Displayed level quantity after.
    pub level_qty_after: Qty,
    /// Cancelled quantity ahead.
    pub cancel_ahead_qty: Qty,
    /// Cancelled quantity behind.
    pub cancel_behind_qty: Qty,
    /// Quantity added behind.
    pub add_behind_qty: Qty,
    /// At-price traded quantity.
    pub traded_at_px_qty: Qty,
    /// Trade-through quantity.
    pub trade_through_qty: Qty,
    /// Filled quantity after the event.
    pub filled_qty: Qty,
}

/// One zoo prediction (`qt.zoo_predictions`).
#[derive(Clone, Debug, PartialEq)]
pub struct ZooPredictionRow {
    /// Canonical model id.
    pub model_id: String,
    /// Shadow id.
    pub shadow_id: String,
    /// Order quantity.
    pub qty: Qty,
    /// Horizon.
    pub horizon_ns: Dur,
    /// P(first fill within the horizon of ts_live).
    pub p_fill: f64,
    /// Expected filled quantity by the horizon.
    pub e_fill_qty: f64,
    /// Model first-fill time (deterministic models; particle median).
    pub model_ts_first_fill: Option<Ts>,
    /// Exact truth: filled within the horizon.
    pub truth_filled: Option<bool>,
    /// Exact first-fill time.
    pub truth_ts_first_fill: Option<Ts>,
    /// Exact filled quantity by the horizon.
    pub truth_filled_qty: Option<Qty>,
    /// Censored before the horizon and unfilled.
    pub censored: bool,
    /// Exact queue ahead at activation.
    pub queue_ahead_at_live: Option<Qty>,
    /// Exact level quantity at activation.
    pub level_qty_at_live: Option<Qty>,
    /// Visible level quantity at activation.
    pub visible_level_qty_at_live: Qty,
    /// Price not in the visible view at activation.
    pub out_of_view_at_live: bool,
    /// Instrument.
    pub instrument_id: u32,
    /// Session date (days since epoch).
    pub session_date: i32,
    /// Activation target time.
    pub ts_live: Ts,
    /// Displayed volume traded in the 60 s before ts_live.
    pub trade_vol_60s: Qty,
}

/// One zoo model fill (`qt.zoo_fills`).
#[derive(Clone, Debug, PartialEq)]
pub struct ZooFillRow {
    /// Canonical model id.
    pub model_id: String,
    /// Shadow id.
    pub shadow_id: String,
    /// 1-based fill number.
    pub fill_seq: u32,
    /// Fill time.
    pub ts_event: Ts,
    /// Fill quantity.
    pub fill_qty: f64,
    /// Cumulative quantity.
    pub cum_qty: f64,
    /// Model queue-ahead estimate before the fill.
    pub queue_ahead_est_before: f64,
    /// Fill reason.
    pub reason: ZooFillReason,
    /// Faithful-mode quirk that fired.
    pub quirk: Quirk,
}

/// One zoo trajectory row (`qt.zoo_trajectory`).
#[derive(Clone, Debug, PartialEq)]
pub struct ZooTrajectoryRow {
    /// Canonical model id.
    pub model_id: String,
    /// Shadow id.
    pub shadow_id: String,
    /// Batch time.
    pub ts_event: Ts,
    /// Model estimate.
    pub queue_ahead_est: f64,
    /// Exact queue ahead.
    pub queue_ahead_exact: Qty,
}

/// One allocation event (`qt.alloc_events`).
#[derive(Clone, Debug, PartialEq)]
pub struct AllocEventRow {
    /// Shadow id.
    pub shadow_id: String,
    /// Instrument.
    pub instrument_id: u32,
    /// Session date (days since epoch).
    pub session_date: i32,
    /// Event time.
    pub ts_event: Ts,
    /// Exact queue ahead before.
    pub front: f64,
    /// Exact level quantity before minus front.
    pub back: f64,
    /// Cancelled quantity ahead.
    pub ahead_cancel: f64,
    /// Cancelled quantity behind.
    pub behind_cancel: f64,
    /// L2 previous level quantity in the batch.
    pub l2_prev: Option<f64>,
    /// L2 new level quantity in the batch.
    pub l2_new: Option<f64>,
    /// L2 trades at the level in the batch.
    pub l2_trades: Option<f64>,
}

/// One touch record (`qt.touch_records`).
#[derive(Clone, Debug, PartialEq)]
pub struct TouchRecordRow {
    /// Shadow id.
    pub shadow_id: String,
    /// Touch time.
    pub ts_event: Ts,
    /// 1-based touch number.
    pub touch_index: u32,
    /// NT queue-ahead before the touch.
    pub nt_front_before: f64,
    /// NT excess volume.
    pub nt_excess: f64,
    /// Exact engine filled at this touch.
    pub truth_filled_now: bool,
    /// Exact first fill happened at this touch.
    pub truth_first_fill_here: bool,
}

/// One feature row (`qt.features`); `values` align with the feature-name list given to the codec.
#[derive(Clone, Debug, PartialEq)]
pub struct FeatureRow {
    /// Shadow id or `"<instrument_id>:<order_id>"`.
    pub order_ref: String,
    /// Population.
    pub population: Population,
    /// Instrument.
    pub instrument_id: u32,
    /// Session date (days since epoch).
    pub session_date: i32,
    /// Landmark time.
    pub ts_landmark: Ts,
    /// Landmark offset from the order's start.
    pub landmark_ns: Dur,
    /// +1 bid, -1 ask.
    pub side_sign: i8,
    /// Feature values (null when undefined).
    pub values: Vec<Option<f64>>,
    /// Time from landmark to event or censoring.
    pub event_ns: Dur,
    /// Outcome cause.
    pub cause: Cause,
    /// 1.1: local midnight of the instrument-day in UTC ns (`qt.instruments.midnight_utc_ns`); `None` when read
    /// from a file written before 1.1.
    pub midnight_utc_ns: Option<i64>,
}
