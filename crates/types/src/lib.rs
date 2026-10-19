//! Core types shared by every queuetruth crate (SPEC 3.1, 3.2): MBO records, sides and actions with DBN byte
//! values, venue rule sets, instruments, latency model, error codes and the deterministic RNG.

#![forbid(unsafe_code)]

mod rng;

pub use rng::{Xoshiro256StarStar, splitmix64, stable_seed};

/// Nanoseconds since the Unix epoch, UTC.
pub type Ts = i64;
/// Duration in nanoseconds.
pub type Dur = i64;
/// Fixed-point price: real price = `price * 10^price_exp`.
pub type Px = i64;
/// Size in size units: real size = `size * 10^size_exp`.
pub type Qty = u64;
/// Undefined price sentinel (DBN convention), used only inside `qt.mbo`.
pub const UNDEF_PRICE: Px = i64::MAX;

/// DBN record flag bits (identical values to DBN).
pub mod flags {
    /// Last record of a venue event for the instrument; the book is consistent after it.
    pub const LAST: u8 = 1 << 7;
    /// Top-of-book record.
    pub const TOB: u8 = 1 << 6;
    /// Record from a snapshot / replay.
    pub const SNAPSHOT: u8 = 1 << 5;
    /// Aggregated price-level record.
    pub const MBP: u8 = 1 << 4;
    /// `ts_recv` is inaccurate.
    pub const BAD_TS_RECV: u8 = 1 << 3;
    /// Unrecoverable gap detected in the channel.
    pub const MAYBE_BAD_BOOK: u8 = 1 << 2;
    /// Publisher-specific meaning.
    pub const PUBLISHER_SPECIFIC: u8 = 1 << 1;
}

/// MBO action with DBN byte values.
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum Action {
    /// Add a resting order.
    Add = b'A',
    /// Cancel (full or partial) a resting order.
    Cancel = b'C',
    /// Modify a resting order.
    Modify = b'M',
    /// Clear the book.
    Clear = b'R',
    /// Trade print.
    Trade = b'T',
    /// Fill of a resting order.
    Fill = b'F',
    /// No action.
    None = b'N',
}

impl Action {
    /// Parses a DBN action byte; unknown bytes give `None`.
    pub const fn from_byte(b: u8) -> Option<Self> {
        match b {
            b'A' => Some(Self::Add),
            b'C' => Some(Self::Cancel),
            b'M' => Some(Self::Modify),
            b'R' => Some(Self::Clear),
            b'T' => Some(Self::Trade),
            b'F' => Some(Self::Fill),
            b'N' => Some(Self::None),
            _ => None,
        }
    }

    /// The DBN action byte.
    pub const fn as_byte(self) -> u8 {
        self as u8
    }
}

/// Side with DBN byte values; on a `T` record the aggressor side.
#[repr(u8)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum Side {
    /// Bid / buy.
    Bid = b'B',
    /// Ask / sell.
    Ask = b'A',
    /// None or unknown.
    None = b'N',
}

impl Side {
    /// Parses a DBN side byte; unknown bytes map to `Side::None`.
    pub const fn from_byte(b: u8) -> Self {
        match b {
            b'B' => Self::Bid,
            b'A' => Self::Ask,
            _ => Self::None,
        }
    }

    /// The DBN side byte.
    pub const fn as_byte(self) -> u8 {
        self as u8
    }

    /// Bid <-> Ask; `None` stays `None`.
    pub const fn opposite(self) -> Self {
        match self {
            Self::Bid => Self::Ask,
            Self::Ask => Self::Bid,
            Self::None => Self::None,
        }
    }
}

/// One side of a book.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub enum BookSide {
    /// Bid side.
    Bid,
    /// Ask side.
    Ask,
}

impl BookSide {
    /// `Side::Bid`/`Side::Ask` map to a book side; `Side::None` gives `None`.
    pub const fn from_side(s: Side) -> Option<Self> {
        match s {
            Side::Bid => Some(Self::Bid),
            Side::Ask => Some(Self::Ask),
            Side::None => None,
        }
    }

    /// Parses `b'B'` / `b'A'`; anything else gives `None`.
    pub const fn from_byte(b: u8) -> Option<Self> {
        Self::from_side(Side::from_byte(b))
    }

    /// The other side.
    pub const fn opposite(self) -> Self {
        match self {
            Self::Bid => Self::Ask,
            Self::Ask => Self::Bid,
        }
    }

    /// `b'B'` or `b'A'`.
    pub const fn as_byte(self) -> u8 {
        match self {
            Self::Bid => b'B',
            Self::Ask => b'A',
        }
    }

    /// The matching `Side`.
    pub const fn to_side(self) -> Side {
        match self {
            Self::Bid => Side::Bid,
            Self::Ask => Side::Ask,
        }
    }
}

/// One normalized MBO record (DBN MboMsg semantics, u64 size, u64 sequence).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MboEvent {
    /// Venue matching-engine time (`ClockSemantics::MatchTime`).
    pub ts_event: Ts,
    /// Capture time (`CaptureTime`); equals `ts_event` when the source has none.
    pub ts_recv: Ts,
    /// Instrument id.
    pub instrument_id: u32,
    /// `Action` byte.
    pub action: u8,
    /// `Side` byte; on `T` the aggressor side.
    pub side: u8,
    /// Price; `UNDEF_PRICE` when not applicable.
    pub price: Px,
    /// Size in size units.
    pub size: Qty,
    /// Order id; 0 when not applicable.
    pub order_id: u64,
    /// `flags::*` bits.
    pub flags: u8,
    /// Venue/source sequence.
    pub sequence: u64,
    /// Channel id.
    pub channel_id: u16,
    /// Publisher id; 0 = unknown.
    pub publisher_id: u16,
}

impl Default for MboEvent {
    fn default() -> Self {
        Self {
            ts_event: 0,
            ts_recv: 0,
            instrument_id: 0,
            action: b'N',
            side: b'N',
            price: UNDEF_PRICE,
            size: 0,
            order_id: 0,
            flags: 0,
            sequence: 0,
            channel_id: 0,
            publisher_id: 0,
        }
    }
}

impl MboEvent {
    /// The parsed action, `None` for an unknown byte.
    pub fn action(&self) -> Option<Action> {
        Action::from_byte(self.action)
    }

    /// The parsed side (unknown bytes map to `Side::None`).
    pub fn side(&self) -> Side {
        Side::from_byte(self.side)
    }

    /// True when the record carries `flags::LAST`.
    pub fn is_last(&self) -> bool {
        self.flags & flags::LAST != 0
    }

    /// True when the record carries `flags::SNAPSHOT`.
    pub fn is_snapshot(&self) -> bool {
        self.flags & flags::SNAPSHOT != 0
    }
}

/// Venue rule set selector stored per instrument; crypto L3 sources use `Generic` (the schema 1.1 `crypto_l3`
/// rule set is selected by name through `RuleSet`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Venue {
    /// Generic FIFO venue.
    #[default]
    Generic,
    /// Nasdaq TotalView-ITCH.
    Nasdaq,
    /// CME Globex MDP 3.0.
    Cme,
    /// Coinbase Exchange full channel.
    Coinbase,
}

impl Venue {
    /// Canonical lower-case name (`generic`, `nasdaq`, `cme`, `coinbase`).
    pub const fn name(self) -> &'static str {
        match self {
            Self::Generic => "generic",
            Self::Nasdaq => "nasdaq",
            Self::Cme => "cme",
            Self::Coinbase => "coinbase",
        }
    }
}

impl std::fmt::Display for Venue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.name())
    }
}

impl std::str::FromStr for Venue {
    type Err = QtError;

    /// Accepts the aliases of SPEC 3.1 (case-insensitive); anything else is `QT-CONFIG-001`.
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "nasdaq" | "itch" | "xnas" | "xnas.itch" => Ok(Self::Nasdaq),
            "cme" | "glbx" | "glbx.mdp3" => Ok(Self::Cme),
            "coinbase" => Ok(Self::Coinbase),
            "generic" | "bitstamp" | "bitfinex" => Ok(Self::Generic),
            other => Err(QtError::Config(format!(
                "venue: unknown venue {other:?} (use nasdaq|cme|coinbase|generic)"
            ))),
        }
    }
}

/// Venue rule-set name (schema 1.1, SPEC 3.2): every `Venue` plus `crypto_l3` (E38, M25).
///
/// `Venue` stays the per-instrument selector stored in `qt.instruments`; `crypto_l3` is selected by name (a
/// `ReplaySpec.venue` / `IngestSpec.venue` string) and runs on top of `Venue::Generic` records, so
/// `RuleSet::CryptoL3.venue()` is `Venue::Generic`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleSet {
    /// Generic FIFO venue (consolidated or unknown data).
    #[default]
    Generic,
    /// Nasdaq TotalView-ITCH.
    Nasdaq,
    /// CME Globex MDP 3.0.
    Cme,
    /// Coinbase Exchange full channel.
    Coinbase,
    /// Crypto L3 feeds (Bitstamp, Bitfinex): `generic` without hidden orders, so side-`N` trades consume displayed
    /// queue. Bitfinex hidden orders are opt-in and flagged in the feed.
    CryptoL3,
    /// IEX DEEP+ (schema 1.2): US equities price-display-time priority with Nasdaq-style records (no `M`; the DEEP+
    /// Order Modify priority bit becomes a partial `C` or a `C` + `A`), so it runs on `Venue::Nasdaq` records.
    Iex,
}

impl RuleSet {
    /// Canonical lower-case name (`generic`, `nasdaq`, `cme`, `coinbase`, `crypto_l3`, `iex`).
    pub const fn name(self) -> &'static str {
        match self {
            Self::Generic => "generic",
            Self::Nasdaq => "nasdaq",
            Self::Cme => "cme",
            Self::Coinbase => "coinbase",
            Self::CryptoL3 => "crypto_l3",
            Self::Iex => "iex",
        }
    }

    /// The record-semantics venue this rule set runs on (`crypto_l3` -> `Generic`, `iex` -> `Nasdaq`).
    pub const fn venue(self) -> Venue {
        match self {
            Self::Generic | Self::CryptoL3 => Venue::Generic,
            Self::Nasdaq | Self::Iex => Venue::Nasdaq,
            Self::Cme => Venue::Cme,
            Self::Coinbase => Venue::Coinbase,
        }
    }
}

impl From<Venue> for RuleSet {
    fn from(v: Venue) -> Self {
        match v {
            Venue::Generic => Self::Generic,
            Venue::Nasdaq => Self::Nasdaq,
            Venue::Cme => Self::Cme,
            Venue::Coinbase => Self::Coinbase,
        }
    }
}

impl std::fmt::Display for RuleSet {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.name())
    }
}

impl std::str::FromStr for RuleSet {
    type Err = QtError;

    /// Every `Venue` alias plus `crypto_l3` / `crypto-l3` and `iex` / `iexg` (case-insensitive); anything else is
    /// `QT-CONFIG-001`. `bitstamp` and `bitfinex` keep mapping to `generic` (as `Venue::from_str`); pick `crypto_l3`
    /// explicitly.
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "crypto_l3" | "crypto-l3" => Ok(Self::CryptoL3),
            "iex" | "iexg" => Ok(Self::Iex),
            other => other.parse::<Venue>().map(Self::from).map_err(|_| {
                QtError::Config(format!(
                    "venue: unknown rule set {other:?} (use nasdaq|cme|coinbase|generic|crypto_l3|iex)"
                ))
            }),
        }
    }
}

/// What a modify does to queue priority.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PriorityRule {
    /// Priority kept iff the price is unchanged and the size did not increase.
    KeepOnSizeDecrease,
    /// Every modify loses priority.
    AlwaysLose,
}

impl PriorityRule {
    /// True when a modify from `(old_px, old_sz)` to `(new_px, new_sz)` keeps queue priority.
    pub fn keeps_priority(self, old_px: Px, old_sz: Qty, new_px: Px, new_sz: Qty) -> bool {
        match self {
            Self::KeepOnSizeDecrease => old_px == new_px && new_sz <= old_sz,
            Self::AlwaysLose => false,
        }
    }
}

/// How a rule set or result was validated.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ValidationStatus {
    /// Validated on real data.
    RealData,
    /// Validated on synthetic data only.
    SyntheticOnly,
    /// Assumed from documentation.
    Assumed,
    /// Validated on hand-written fixtures only.
    FixtureOnly,
}

/// Venue record semantics (SPEC 3.2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VenueRules {
    /// The venue these rules describe.
    pub venue: Venue,
    /// False: an `M` record is a data error (counted, ignored).
    pub modify_records_present: bool,
    /// Priority rule for `M` records.
    pub modify_priority: PriorityRule,
    /// Files must start with a snapshot introduced by `R` (else anomaly).
    pub snapshot_begins_with_clear: bool,
    /// A duplicate `A` inside a flagged snapshot is not an anomaly.
    pub snapshot_records_flagged: bool,
    /// `T.side` is the aggressor side.
    pub trades_have_aggressor: bool,
    /// `T` with side `N` consumes no displayed queue.
    pub supports_hidden_orders: bool,
    /// Pair `F` with its removing `C` by `sequence`, else within the `F_LAST` event.
    pub fill_removal_shares_sequence: bool,
    /// The source inserts a snapshot at UTC midnight.
    pub utc_midnight_snapshot: bool,
    /// Sequence gap/regression counters are meaningful.
    pub sequence_meaningful: bool,
    /// How these rules were validated.
    pub validation: ValidationStatus,
    /// Schema 1.1: the rule-set name these rules came from (`crypto_l3` for `rules_for(RuleSet::CryptoL3)`).
    pub rule_set: RuleSet,
    /// Schema 1.1 (3.11 #4, A6, E36): an `M` with unchanged price AND unchanged size loses priority (CME iceberg
    /// refresh / account change); an `M` raising the size above the post-fill remaining size loses it too (A6b).
    pub iceberg_refresh_loses_priority: bool,
    /// Schema 1.1 (3.11 #4, A3, E36): displayed interest has priority over non-displayed interest at a price
    /// (Nasdaq), so a side-`N` print can fill a shadow (5.2.4a, governed by `HiddenPrintPolicy`).
    pub hidden_lower_priority: bool,
}

/// Rule set per venue; values are normative (SPEC 3.2, ported from et `venue-semantics`).
pub const fn rules(v: Venue) -> VenueRules {
    match v {
        Venue::Nasdaq => VenueRules {
            venue: v,
            modify_records_present: false,
            modify_priority: PriorityRule::AlwaysLose,
            snapshot_begins_with_clear: false,
            snapshot_records_flagged: false,
            trades_have_aggressor: true,
            supports_hidden_orders: true,
            fill_removal_shares_sequence: true,
            utc_midnight_snapshot: false,
            sequence_meaningful: true,
            validation: ValidationStatus::RealData,
            rule_set: RuleSet::Nasdaq,
            iceberg_refresh_loses_priority: false,
            hidden_lower_priority: true,
        },
        Venue::Cme => VenueRules {
            venue: v,
            modify_records_present: true,
            modify_priority: PriorityRule::KeepOnSizeDecrease,
            snapshot_begins_with_clear: true,
            snapshot_records_flagged: true,
            trades_have_aggressor: true,
            supports_hidden_orders: false,
            fill_removal_shares_sequence: false,
            utc_midnight_snapshot: true,
            sequence_meaningful: true,
            validation: ValidationStatus::SyntheticOnly,
            rule_set: RuleSet::Cme,
            iceberg_refresh_loses_priority: true,
            hidden_lower_priority: false,
        },
        Venue::Coinbase => VenueRules {
            venue: v,
            modify_records_present: true,
            modify_priority: PriorityRule::KeepOnSizeDecrease,
            snapshot_begins_with_clear: false,
            snapshot_records_flagged: false,
            trades_have_aggressor: true,
            supports_hidden_orders: false,
            fill_removal_shares_sequence: true,
            utc_midnight_snapshot: false,
            sequence_meaningful: true,
            validation: ValidationStatus::Assumed,
            rule_set: RuleSet::Coinbase,
            iceberg_refresh_loses_priority: false,
            hidden_lower_priority: false,
        },
        Venue::Generic => VenueRules {
            venue: v,
            modify_records_present: true,
            modify_priority: PriorityRule::KeepOnSizeDecrease,
            snapshot_begins_with_clear: false,
            snapshot_records_flagged: true,
            trades_have_aggressor: false,
            supports_hidden_orders: true,
            fill_removal_shares_sequence: true,
            utc_midnight_snapshot: false,
            sequence_meaningful: false,
            validation: ValidationStatus::Assumed,
            rule_set: RuleSet::Generic,
            iceberg_refresh_loses_priority: false,
            hidden_lower_priority: false,
        },
    }
}

/// Rule set by name (schema 1.1): `rules(rs.venue())` for the venue rule sets; `crypto_l3` = the `generic` values
/// with `supports_hidden_orders = false` (E38, M25; `generic` itself is unchanged).
pub const fn rules_for(rs: RuleSet) -> VenueRules {
    match rs {
        RuleSet::Generic => rules(Venue::Generic),
        RuleSet::Nasdaq => rules(Venue::Nasdaq),
        RuleSet::Cme => rules(Venue::Cme),
        RuleSet::Coinbase => rules(Venue::Coinbase),
        RuleSet::CryptoL3 => {
            let mut r = rules(Venue::Generic);
            r.rule_set = RuleSet::CryptoL3;
            r.supports_hidden_orders = false;
            r
        }
        RuleSet::Iex => IEX_RULES,
    }
}

/// `iex` (schema 1.2): IEX DEEP+ as mapped by `qt_ingest::iex`. Citations: "DEEP+" = IEX DEEP+ Specification v1.06
/// (2026-10-07/08); "IEX-TP" = IEX Transport Specification v1.26 (2026-06-02); "Rule" = Investors Exchange Rule Book
/// (2026-10-02). The values equal `rules(Venue::Nasdaq)` except `rule_set` (and `validation`), which is why `iex`
/// runs on `Venue::Nasdaq` records.
const IEX_RULES: VenueRules = VenueRules {
    venue: Venue::Nasdaq,
    // DEEP+ p.22 Order Modify: "Modify Flags - Bit 0 - Priority: 0 = Reset Priority, 1 = Maintain Priority". The
    // reader turns a reset into `C` (old remaining) + `A` (same order id, back of the queue) and a maintained size
    // decrease into a partial `C` (Rule 11.220(a)(3): the unexecuted / reduced size keeps its priority), so `iex`
    // datasets carry no `M` records and a stray `M` is a data error.
    modify_records_present: false,
    modify_priority: PriorityRule::AlwaysLose,
    // DEEP+ p.13 System Event `O` "Start of Messages ... the first message sent in any trading session": a HIST day
    // starts from an empty book, with no snapshot; DEEP+ p.27 Clear Book `C` maps to `R` for its symbol.
    snapshot_begins_with_clear: false,
    snapshot_records_flagged: false,
    // DEEP+ p.24 Order Executed `L` names the resting displayed order ("A displayed order that was executed
    // against"), so the aggressor is the opposite side.
    trades_have_aggressor: true,
    // DEEP+ p.3: "Non-displayed orders and non-displayed portions of reserve orders are not represented in DEEP+ ...
    // Trades resulting from either displayed or non-displayed orders matching on IEX will be reported"; p.25 Trade
    // `T` ("A non-displayed order on the book that executed against another non-displayed order") is a side-`N`
    // print that consumes no displayed queue.
    supports_hidden_orders: true,
    // One DEEP+ message is one IEX-TP sequence number; the `T`, `F` and `C` of an `L` share it.
    fill_removal_shares_sequence: true,
    utc_midnight_snapshot: false,
    // IEX-TP p.6: "First Message Sequence Number ... If there is more than one message in a segment, all subsequent
    // messages are implicitly numbered sequentially": record `sequence` is that number, so gaps are real feed gaps.
    sequence_meaningful: true,
    // Real data: the 2026-10-09 DPLS day (SPY, AAPL, NVDA, TSLA; 330.7 M feed messages, 4.44 M records) replays
    // with every invariant at 0 and all 21,301 displayed executions at the head of their FIFO level (rank oracle);
    // ignoring the reset-priority bit would put 33 of them behind the head (docs/how-to/iex-public-demo.md).
    validation: ValidationStatus::RealData,
    rule_set: RuleSet::Iex,
    // Rule 11.220(a)(1)(C)(v): a replenished reserve order gets a new timestamp; DEEP+ publishes that as a
    // reset-priority Modify, which the reader already maps to `C` + `A`, so no `M` is ever a refresh.
    iceberg_refresh_loses_priority: false,
    // Rule 11.220(a)(1)(B): "Displayed orders and displayed portions of orders will have precedence over
    // non-displayed orders and non-displayed portions of orders at a given price."
    hidden_lower_priority: true,
};

/// Meaning of a timestamp column.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ClockSemantics {
    /// Venue matching-engine time.
    MatchTime,
    /// Venue record/publication time.
    RecordTime,
    /// Gateway time.
    GatewayTime,
    /// Capture time.
    CaptureTime,
    /// Local client time.
    LocalTime,
    /// Unknown.
    Unknown,
}

/// Precision of a source timestamp.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TsPrecision {
    /// Seconds.
    S,
    /// Milliseconds.
    Ms,
    /// Microseconds.
    Us,
    /// Nanoseconds.
    Ns,
}

/// One instrument for one session date (`qt.instruments` row).
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Instrument {
    /// Instrument id (ITCH: stock locate, per day).
    pub instrument_id: u32,
    /// Session date, days since 1970-01-01.
    pub session_date: i32,
    /// Symbol.
    pub symbol: String,
    /// Rule set.
    pub venue: Venue,
    /// Source venue name (`nasdaq`, `cme`, `coinbase`, `bitstamp`, ...).
    pub source_venue: String,
    /// Price exponent (`-12..=0`).
    pub price_exp: i8,
    /// Tick size in price units.
    pub tick_size: Px,
    /// Minimum size increment in size units.
    pub lot_size: Qty,
    /// Size exponent.
    pub size_exp: i8,
    /// Currency per (real price unit x real size unit).
    pub contract_multiplier: f64,
    /// Currency code.
    pub currency: String,
    /// Informational IANA time zone name.
    pub session_tz: String,
    /// Authoritative local midnight of `session_date` as UTC ns.
    pub midnight_utc_ns: Ts,
    /// Continuous-session open, seconds after local midnight.
    pub open_s: u32,
    /// Continuous-session close, seconds after local midnight.
    pub close_s: u32,
    /// Ingest format name.
    pub source: String,
}

/// Shadow-order latency components (et semantics, SPEC 3.1).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct LatencyModel {
    /// Market-data feed latency.
    pub feed_ns: Dur,
    /// Decision latency.
    pub decision_ns: Dur,
    /// Order gateway latency.
    pub gateway_ns: Dur,
    /// Maximum uniform jitter added per submit / cancel / amend.
    pub jitter_ns: Dur,
}

/// Every error carries a stable `QT-<AREA>-<NNN>` code (SPEC 0.2); `Display` = `"<code>: <message>"`.
#[derive(Debug, thiserror::Error)]
pub enum QtError {
    /// Bad CLI usage.
    #[error("QT-USAGE-001: {0}")]
    Usage(String),
    /// Invalid spec or config value.
    #[error("QT-CONFIG-001: {0}")]
    Config(String),
    /// Input file missing or unreadable.
    #[error("QT-INPUT-001: {path}: {msg}")]
    InputMissing {
        /// Path of the input.
        path: String,
        /// What went wrong.
        msg: String,
    },
    /// Input format or schema mismatch.
    #[error("QT-INPUT-002: {location}: {msg}")]
    InputFormat {
        /// File, row or byte offset.
        location: String,
        /// What went wrong.
        msg: String,
    },
    /// Duplicate id.
    #[error("QT-INPUT-003: duplicate {what}: {id}")]
    Duplicate {
        /// Kind of id (`shadow_id`, `order_key`, `fill_id`).
        what: String,
        /// The duplicated id.
        id: String,
    },
    /// Unit conversion not exact.
    #[error("QT-INPUT-004: {0}")]
    Units(String),
    /// Data-quality verdict `unusable`.
    #[error("QT-DATA-001: {0}")]
    DataUnusable(String),
    /// `--strict` and a non-zero invariant / anomaly counter.
    #[error("QT-DATA-002: {0}")]
    Strict(String),
    /// Internal invariant broken (bug).
    #[error("QT-ENGINE-001: {0}")]
    Engine(String),
    /// I/O error.
    #[error("QT-INPUT-001: {0}")]
    Io(#[from] std::io::Error),
}

impl QtError {
    /// The stable error code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Usage(_) => "QT-USAGE-001",
            Self::Config(_) => "QT-CONFIG-001",
            Self::InputMissing { .. } | Self::Io(_) => "QT-INPUT-001",
            Self::InputFormat { .. } => "QT-INPUT-002",
            Self::Duplicate { .. } => "QT-INPUT-003",
            Self::Units(_) => "QT-INPUT-004",
            Self::DataUnusable(_) => "QT-DATA-001",
            Self::Strict(_) => "QT-DATA-002",
            Self::Engine(_) => "QT-ENGINE-001",
        }
    }

    /// The message without the code prefix.
    pub fn message(&self) -> String {
        let full = self.to_string();
        let prefix = format!("{}: ", self.code());
        full.strip_prefix(&prefix).map(str::to_owned).unwrap_or(full)
    }
}

/// Result alias used by every fallible function.
pub type QtResult<T> = Result<T, QtError>;

#[cfg(test)]
mod tests;
