//! TAQ Integrated Feed messages -> normalized MBO records and source trade prints (rule set `nyse`, see `rules`).
//!
//! Records: `sequence` = the channel SequenceNumber (the `T` / `F` / `C` of one message share it); `channel_id` =
//! the data channel of the file; `ts_event` = `ts_recv` = SourceTime on the file's session date (the TAQ file has no
//! receive time); `LAST` on the last record of each message; `instrument_id` = order of first appearance of the
//! symbol over all inputs (1, 2, ...; before the symbol filter, so ids do not depend on it); prices from 10^-9 to the
//! dataset `price_exp` exactly; sizes in shares.
//!
//! | TAQ Integrated message | records |
//! |---|---|
//! | Add Order 100 | `A` (side `B` -> bid, `S` -> ask) |
//! | Modify Order 101, same price, smaller size, PositionChange 0 | `C` of the decrease (keeps priority, Rule 7.36(f)(3)) |
//! | Modify Order 101, same price and size, PositionChange 0 | none (`nyse_modify_noop`) |
//! | Modify Order 101, new price or larger size or PositionChange 1 | `C` of the remaining size + `A` (same id, back of the queue) |
//! | Modify Order 101 to size 0 | `C` of the remaining size |
//! | Delete Order 102 | `C` of the remaining size |
//! | Order Execution 103 | `T` (aggressor = opposite side, `order_id` 0, at the execution price; only when printable) + `F` (resting order, execution price) + `C` (resting price); print kind `displayed` with the maker id; a non-printable execution (an auction allocation) gives `F` + `C` flagged `PUBLISHER_SPECIFIC` |
//! | Replace Order 104 | `C` of the old order's remaining size + `A` with the new Order ID (back of the queue) |
//! | Add Order Refresh 106 | first of a run: `R` for the symbol when it had a book; then `A` per order (the book is rebuilt; a feed gap covers the run) |
//! | Non-Displayed Trade 110 | `T` side `N` (hidden print) when printable; none otherwise |
//! | Cross Trade 111 | `T` side `N`, print kind `cross` (none for 0 shares) |
//! | Symbol Index Mapping 3, Security Status 34, Imbalance 105, Trade Cancel 112, Cross Correction 113, Retail Price Improvement 114, Stock Summary 223 | none (statistics and counters) |
//!
//! A live Order ID added again first cancels the old order (`C`). Anomalies (unknown references, over-executions,
//! bad fields, ...) are counted and never fatal, except that a file whose first lines are not Integrated Feed
//! messages is `QT-INPUT-002`.
//!
//! Symbol Sequence Numbers: every symbol-sequenced message of a selected symbol must carry the previous SymbolSeqNum
//! plus one; a jump is a sequence gap (`feed_gaps` entry from the symbol's previous message to this one, so replay
//! censors that instrument's shadows) and a repeat or a decrease is a regression. Add Order Refresh runs are not
//! checked and re-baseline the symbol. Channel SequenceNumbers are only checked for regressions and repeats: the TAQ
//! product omits sequenced control messages, so their holes are expected.
//!
//! Publisher failovers (TAQ Products Client Specification v2.4b section 11; XDP Common Client Specification v2.4
//! section 9.2): the backup publisher clears every symbol of the channel (Symbol Clear, which the TAQ files omit) and
//! re-sends each order still resting as an Add Order Refresh, so a symbol whose resting orders were all removed
//! during the outage gets no refresh message at all. An Add Order Refresh therefore starts a failover of the whole
//! channel ([`FAILOVER_IDLE_NS`] after the previous one, or when a symbol that was refreshed and went back to live
//! messages is refreshed again; the per-symbol refresh sequences of one failover, with their Symbol Index Mapping and
//! Security Status lines in between, are one failover), and every selected symbol of the channel with resting
//! orders becomes a suspect until its next order message: an Add Order Refresh refreshes it; a Modify, Delete,
//! Execution or Replace of one of its resting orders shows that its book survived (`failover_books_kept`: that
//! refresh did not cover the whole channel); an Add, or a Replace of an order it does not have, would add to a book
//! the backup cleared, so its book is cleared first (`R`, `failover_books_cleared`, a feed gap from its last message
//! to this one, the symbol sequence re-baselined). Suspects left at the end of the file are cleared there. Security
//! Status, Imbalance, prints, cancels and references to unknown orders say nothing about the book.

use std::collections::{BTreeMap, HashMap, HashSet};

use qt_schema::json::FeedGap;
use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, Ts, flags};

use super::message::{Message, ParseError, parse_line};
use super::rules::{Allocation, Market};
use crate::itch::IdBuild;
use crate::time::NS_PER_DAY;
use crate::units::rescale;

/// A time-of-day earlier than the previous message's by more than this is a new day (the clock passed midnight).
const ROLLOVER_NS: u64 = 12 * 3_600_000_000_000;
/// An Add Order Refresh this long after the previous one on the channel starts a new failover (a backup publisher
/// re-sends a channel's books in well under a second).
pub const FAILOVER_IDLE_NS: i64 = 60_000_000_000;
/// Feed-gap reason of a book cleared after a failover that did not refresh it.
pub const FAILOVER_GAP_REASON: &str = "nyse failover: symbol cleared without refresh";
/// Input files named in the warning about files without order messages.
const MAX_NAMED_FILES: usize = 10;
/// Core-session transitions kept for the clock check.
const MAX_CLOCK_SAMPLES: usize = 100_000;

/// Native price exponent of the CSV prices as parsed (`message::price_nanos`).
const PARSED_EXP: i8 = -9;

#[derive(Debug, Clone, Copy)]
struct Order {
    /// `B` or `A`.
    side: u8,
    /// At the dataset `price_exp`.
    price: i64,
    remaining: u64,
}

/// Mapper anomalies; every counter is reported as `source_specific` `nyse_<name>` except the two that feed the
/// standard `unknown_order_refs` and `duplicate_adds` counters.
#[derive(Debug, Default, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Anomalies {
    /// Modify / Delete / Execution / Replace of an Order ID that is not live (`unknown_order_refs`).
    pub unknown_order_ref: u64,
    /// Add / Replace / Refresh of an Order ID that is still live; the old order is cancelled first
    /// (`duplicate_adds`).
    pub duplicate_order_ref: u64,
    /// Execution volume above the remaining size (clamped).
    pub exec_exceeds_resting: u64,
    /// Modify / Replace `Side` that disagrees with the order's side (the order's side is kept).
    pub side_mismatch: u64,
    /// Add / Refresh with a side other than `B` / `S` (skipped).
    pub bad_side: u64,
    /// A price that is 0, or not exact at the dataset `price_exp` (the message is skipped; an execution uses the
    /// resting price).
    pub bad_price: u64,
    /// Add / Refresh / Execution / Non-Displayed Trade with 0 shares (skipped).
    pub zero_size: u64,
    /// Lines that do not parse as their message type (skipped; the first is named in a warning).
    pub malformed_lines: u64,
    /// Message types that no TAQ product lists (skipped).
    pub unknown_message_type: u64,
    /// BBO, Trades or TRF message types inside an Integrated file after its first order message (skipped).
    pub other_product_messages: u64,
    /// Add Order Refresh runs: the symbol's book was cleared and rebuilt (a publisher failover).
    pub refresh_runs: u64,
    /// Symbols with resting orders that got no Add Order Refresh after a failover on their channel: their books
    /// were cleared (`R`) at their next Add (or Replace of an order they did not have) or at the end of the file,
    /// inside a feed gap.
    pub failover_books_cleared: u64,
    /// Input files without a single order message (types 100 to 104 and 106): not the channel file of a trading
    /// day (another product renamed, a truncated or empty file).
    pub files_without_order_messages: u64,
    /// The file's clock went back by more than 12 hours: later records are dated one day later.
    pub midnight_rollovers: u64,
    /// The channel SequenceNumber repeated the previous message's.
    pub channel_sequence_repeats: u64,
    /// Symbol Index Mapping `Market ID` that is not the file name's market (the message's market is used).
    pub market_mismatch: u64,
    /// Symbol Index Mapping `Market ID` that is not an NYSE Group equities market (options, bonds, unknown).
    pub non_equities_market: u64,
    /// A selected symbol whose symbol-sequenced messages appear on a second data channel of the same day (its
    /// records are no longer in one feed order).
    pub symbol_on_two_channels: u64,
    /// Symbol messages missing inside symbol-sequence gaps.
    pub symbol_sequence_missing: u64,
}

impl Anomalies {
    /// `(name, value)` pairs in a fixed order (names prefixed `nyse_`).
    pub fn named(&self) -> [(&'static str, u64); 19] {
        [
            ("nyse_unknown_order_ref", self.unknown_order_ref),
            ("nyse_duplicate_order_ref", self.duplicate_order_ref),
            ("nyse_exec_exceeds_resting", self.exec_exceeds_resting),
            ("nyse_side_mismatch", self.side_mismatch),
            ("nyse_bad_side", self.bad_side),
            ("nyse_bad_price", self.bad_price),
            ("nyse_zero_size", self.zero_size),
            ("nyse_malformed_lines", self.malformed_lines),
            ("nyse_unknown_message_type", self.unknown_message_type),
            ("nyse_other_product_messages", self.other_product_messages),
            ("nyse_refresh_runs", self.refresh_runs),
            ("nyse_failover_books_cleared", self.failover_books_cleared),
            ("nyse_files_without_order_messages", self.files_without_order_messages),
            ("nyse_midnight_rollovers", self.midnight_rollovers),
            ("nyse_channel_sequence_repeats", self.channel_sequence_repeats),
            ("nyse_market_mismatch", self.market_mismatch),
            ("nyse_non_equities_market", self.non_equities_market),
            ("nyse_symbol_on_two_channels", self.symbol_on_two_channels),
            ("nyse_symbol_sequence_missing", self.symbol_sequence_missing),
        ]
    }
}

/// Mapper statistics.
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct MapperStats {
    /// Non-blank lines read.
    pub lines: u64,
    /// Lines by message type (all symbols; complete after `finish`).
    pub by_type: BTreeMap<u16, u64>,
    /// Symbols seen.
    pub symbols_seen: u64,
    /// Requested symbols never seen.
    pub symbols_not_found: Vec<String>,
    /// Orders added (selected symbols; refresh orders included).
    pub orders_seen: u64,
    /// Orders live at the end.
    pub orders_live: u64,
    /// Most orders live at once.
    pub orders_live_max: u64,
    /// Firm-attributed Add Orders (FirmID not blank).
    pub attributed_adds: u64,
    /// Modify Order kept priority (same price, smaller size).
    pub modify_keep_priority: u64,
    /// Modify Order lost priority (new price, larger size, PositionChange 1, or size 0).
    pub modify_lose_priority: u64,
    /// Modify Order with the same price and size (no record).
    pub modify_noop: u64,
    /// Replace Order messages applied.
    pub replace_orders: u64,
    /// Replace Orders at the old order's side and price with a smaller size: the reader puts them at the back of
    /// the queue, while a Pillar quantity reduction keeps its priority (`rules` module docs: which message the feed
    /// uses for a reduction is an assumption that waits for a real file).
    pub replace_reduce_same_price: u64,
    /// Order Execution messages applied.
    pub executions: u64,
    /// Shares executed against displayed orders.
    pub executed_shares: u64,
    /// Executions on a market that allocated by parity on the file's session date (`nyse_parity_executions`;
    /// `rules::Market::allocation_on`).
    pub parity_executions: u64,
    /// Trade Cancel messages (prints already written are not withdrawn; a bust does not restore the executed
    /// order, so book builders may ignore them, TAQ spec v2.4b section 14).
    pub trade_cancels: u64,
    /// Cross Correction messages (the cross print already written is not changed; section 15).
    pub cross_corrections: u64,
    /// Symbols with resting orders that referenced one of them after a failover on their channel without being
    /// refreshed: their books were kept (that refresh did not cover the whole channel).
    pub failover_books_kept: u64,
    /// Non-printable executions (auction allocations of displayed orders).
    pub nonprintable_executions: u64,
    /// Printable Non-Displayed Trades (hidden prints).
    pub hidden_trades: u64,
    /// Non-printable Non-Displayed Trades.
    pub nonprintable_hidden_trades: u64,
    /// Cross Trades with shares.
    pub cross_trades: u64,
    /// Cross Trades with 0 shares.
    pub zero_volume_crosses: u64,
    /// Orders added by Add Order Refresh.
    pub refresh_orders: u64,
    /// Time of day (ns after midnight) of Security Status transitions to the core session (`O`), at most 100,000.
    pub core_open_times: Vec<u64>,
    /// MBO records emitted.
    pub mbo_records: u64,
    /// Source trade prints emitted.
    pub trade_records: u64,
    /// Anomaly counters.
    pub anomalies: Anomalies,
}

/// Records produced by one message.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct MapOutput {
    /// MBO records (the last carries `LAST`).
    pub mbo: Vec<MboEvent>,
    /// Source trade prints.
    pub trades: Vec<TradeRow>,
}

/// One input file being mapped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileCtx {
    /// Session date (days since 1970-01-01).
    pub date: i32,
    /// Local midnight of `date` as UTC ns.
    pub midnight: Ts,
    /// Data channel (`MboEvent::channel_id`).
    pub channel: u16,
    /// Market of the file name, when it has one.
    pub market: Option<Market>,
    /// Path for messages.
    pub label: String,
}

/// Per-file clock and sequence state.
#[derive(Debug, Default)]
struct FileState {
    last_seq: Option<u64>,
    last_tod: Option<u64>,
    /// Latest timestamp of the file (where suspects left at its end are cleared).
    max_ts: Option<Ts>,
    rollover_days: i64,
    /// Lines that parsed as a message of an Integrated type.
    good_lines: u64,
    order_messages: u64,
    /// Time of day of the file's Security Status transitions to the core session (at most 100,000).
    core_open: Vec<u64>,
    /// The latest failover of the file's channel.
    failover: Option<Failover>,
    /// `end_file` ran (or no file was begun).
    ended: bool,
}

/// A publisher failover on the current file's channel (module docs).
#[derive(Debug, Clone, Copy)]
struct Failover {
    /// Mapper-wide failover number (1, 2, ...).
    id: u32,
    /// Time of its latest Add Order Refresh.
    last_refresh: Ts,
}

/// An Add Order Refresh run in progress.
#[derive(Debug, Clone, Copy)]
struct Refresh {
    /// Gap start: the symbol's last message before the run (`None` when it had no book).
    start: Option<Ts>,
    last: Ts,
}

/// One symbol.
#[derive(Debug, Default)]
struct Sym {
    name: String,
    selected: bool,
    /// From its Symbol Index Mapping message.
    market: Option<Market>,
    /// Round lot from its Symbol Index Mapping message.
    lot_size: Option<u32>,
    orders: HashMap<u64, Order, IdBuild>,
    last_symbol_seq: Option<u64>,
    last_ts: Option<Ts>,
    refresh: Option<Refresh>,
    /// `(session date, channel)` of its latest symbol-sequenced message of the day.
    home: Option<(i32, u16)>,
    /// Input (1, 2, ...) of its latest symbol-sequenced message.
    last_file: u32,
    /// It already had a book message today (an Add Order Refresh run then starts with `R`).
    booked: bool,
    /// Its resting orders wait for evidence after a failover on its channel: the start of the feed gap if its book
    /// turns out to be cleared (its last message before the failover).
    suspect: Option<Ts>,
    /// Failover of its latest Add Order Refresh (0: none).
    refreshed_in: u32,
    /// Its latest symbol-sequenced message was an Add Order Refresh (selected or not).
    in_refresh: bool,
}

/// Add, Modify, Delete, Execution, Replace and Add Order Refresh: the messages that change a book.
const fn is_order_message(t: u16) -> bool {
    matches!(t, 100..=104 | 106)
}

/// What `apply` needs from a message besides its type.
struct Ctx {
    si: usize,
    iid: u32,
    seq: u64,
    ts: Ts,
}

/// Information about one instrument for the instrument rows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstrumentInfo {
    /// Instrument id.
    pub instrument_id: u32,
    /// Symbol.
    pub symbol: String,
    /// Selected by the symbol filter.
    pub selected: bool,
    /// Market of its Symbol Index Mapping message.
    pub market: Option<Market>,
    /// Round lot.
    pub lot_size: Option<u32>,
}

/// TAQ Integrated Feed -> MBO converter state for one ingest (all inputs, in canonical order).
pub struct Mapper {
    price_exp: i8,
    filter: Option<HashSet<String>>,
    check_symbol_sequence: bool,
    ids: HashMap<Box<[u8]>, u32, IdBuild>,
    syms: Vec<Sym>,
    file: Option<FileCtx>,
    st: FileState,
    gaps: Vec<FeedGap>,
    /// Sequence counters (`sequence_gaps`, `sequence_regressions`).
    pub sequence_gaps: u64,
    /// Channel and symbol sequence regressions and repeats of a symbol sequence.
    pub sequence_regressions: u64,
    /// First malformed line (`location: error`), for the warning.
    pub first_malformed: Option<String>,
    /// The first input files without an order message (at most 10), for the warning.
    pub files_without_orders: Vec<String>,
    /// Statistics.
    pub stats: MapperStats,
    live: u64,
    /// Inputs begun.
    files: u32,
    /// Failovers started.
    failovers: u32,
    /// Lines by message type below 256 (`stats.by_type` is filled from it by `finish`; other types go there
    /// directly).
    types: [u64; 256],
}

impl Mapper {
    /// `symbols` filters by NYSE symbology (`None` keeps every symbol); prices are written at `10^price_exp`
    /// (`-12..=-4`); `check_symbol_sequence` enables the SymbolSeqNum gap check.
    pub fn new(symbols: Option<&[String]>, price_exp: i8, check_symbol_sequence: bool) -> QtResult<Self> {
        if !(-12..=-4).contains(&price_exp) {
            return Err(QtError::Config(format!(
                "price_exp: NYSE prices go to 1/100 of a cent (4 decimals); price_exp must be in -12..=-4, got \
                 {price_exp}"
            )));
        }
        Ok(Self {
            price_exp,
            filter: symbols.map(|s| s.iter().map(|x| x.trim().to_ascii_uppercase()).collect()),
            check_symbol_sequence,
            ids: HashMap::default(),
            syms: Vec::new(),
            file: None,
            st: FileState {
                ended: true,
                ..FileState::default()
            },
            gaps: Vec::new(),
            sequence_gaps: 0,
            sequence_regressions: 0,
            first_malformed: None,
            files_without_orders: Vec::new(),
            stats: MapperStats::default(),
            live: 0,
            files: 0,
            failovers: 0,
            types: [0; 256],
        })
    }

    /// Starts an input. A new session date starts every symbol from an empty book and a new SymbolSeqNum baseline.
    /// Call `end_file` for the previous input first (its records would be lost otherwise).
    pub fn begin_file(&mut self, ctx: FileCtx) {
        self.end_unflushed("begin_file");
        if self.file.as_ref().is_some_and(|f| f.date != ctx.date) {
            for s in &mut self.syms {
                self.live -= s.orders.len() as u64;
                s.orders.clear();
                s.last_symbol_seq = None;
                s.last_ts = None;
                s.refresh = None;
                s.home = None;
                s.booked = false;
            }
        }
        self.file = Some(ctx);
        self.st = FileState::default();
        self.files += 1;
    }

    /// Ends the current input, writing its last records to `out` (cleared first): every symbol still a suspect after
    /// a failover is cleared (`R` at the file's latest timestamp, one message each, inside a feed gap); open refresh
    /// runs get their feed gap (ending at their last refresh message); a file without an order message is counted.
    /// A second call does nothing.
    pub fn end_file(&mut self, out: &mut MapOutput) {
        out.mbo.clear();
        out.trades.clear();
        if self.st.ended || self.file.is_none() {
            return;
        }
        self.st.ended = true;
        let (ts, seq) = (self.st.max_ts.unwrap_or(0), self.st.last_seq.unwrap_or(0));
        for si in 0..self.syms.len() {
            if let Some(start) = self.syms[si].suspect.take() {
                let c = Ctx {
                    si,
                    iid: si as u32 + 1,
                    seq,
                    ts: ts.max(start),
                };
                self.clear_unrefreshed(&c, start, out);
                if let Some(r) = out.mbo.last_mut() {
                    r.flags |= flags::LAST;
                }
            }
            if let Some(r) = self.syms[si].refresh.take() {
                self.refresh_gap(si, r, r.last);
            }
        }
        if self.st.order_messages == 0 {
            self.stats.anomalies.files_without_order_messages += 1;
            if self.files_without_orders.len() < MAX_NAMED_FILES {
                let label = self.file.as_ref().map_or_else(String::new, |f| f.label.clone());
                self.files_without_orders.push(label);
            }
        }
        self.stats.mbo_records += out.mbo.len() as u64;
    }

    /// `end_file` for callers that did not call it: the records it would write are dropped (a caller bug, checked in
    /// debug builds).
    fn end_unflushed(&mut self, caller: &str) {
        let mut dropped = MapOutput::default();
        self.end_file(&mut dropped);
        debug_assert!(
            dropped.mbo.is_empty(),
            "nyse mapper: end_file before {caller} ({} records would be lost)",
            dropped.mbo.len()
        );
    }

    /// Feed gaps found so far (symbol-sequence gaps and refresh runs), in the order found.
    pub fn gaps(&self) -> &[FeedGap] {
        &self.gaps
    }

    /// Every symbol seen, by instrument id.
    pub fn instruments(&self) -> Vec<InstrumentInfo> {
        self.syms
            .iter()
            .enumerate()
            .map(|(i, s)| InstrumentInfo {
                instrument_id: i as u32 + 1,
                symbol: s.name.clone(),
                selected: s.selected,
                market: s.market,
                lot_size: s.lot_size,
            })
            .collect()
    }

    /// Lines of the current file that parsed as Integrated Feed messages.
    pub fn file_good_lines(&self) -> u64 {
        self.st.good_lines
    }

    /// Time of day (ns after midnight, as written) of the current file's Security Status transitions to the core
    /// session, for the clock check (`ingest::clock_warning`).
    pub fn file_core_opens(&self) -> &[u64] {
        &self.st.core_open
    }

    fn ctx(&self) -> &FileCtx {
        // `apply` is only reachable after `begin_file` (checked there).
        self.file.as_ref().expect("begin_file before apply")
    }

    fn input_err(&self, line_no: u64, msg: String) -> QtError {
        QtError::InputFormat {
            location: format!(
                "{} line {line_no}",
                self.file.as_ref().map_or("nyse_ibf", |f| f.label.as_str())
            ),
            msg,
        }
    }

    /// Parses and maps one line (`line_no` names it in errors). A line that does not parse is counted, except that
    /// a file whose first non-blank line does not parse is `QT-INPUT-002` (not an Integrated Feed file).
    pub fn apply_line(&mut self, line: &[u8], line_no: u64, out: &mut MapOutput) -> QtResult<()> {
        if self.st.ended {
            return Err(QtError::Engine("nyse mapper: begin_file before apply_line".into()));
        }
        self.stats.lines += 1;
        match parse_line(line) {
            Ok(msg) => self.apply_at(&msg, line_no, out),
            Err(e) => {
                out.mbo.clear();
                out.trades.clear();
                if self.st.good_lines == 0 {
                    return Err(self.input_err(
                        line_no,
                        format!(
                            "not a TAQ Integrated Feed file: {e} (the first line of an EQY_US_<market>_IBF_<n>_<date> \
                             file is a message, e.g. a Symbol Index Mapping `3,<seq>,<symbol>,<market id>,...`)"
                        ),
                    ));
                }
                self.note_malformed(line_no, &e);
                Ok(())
            }
        }
    }

    fn note_malformed(&mut self, line_no: u64, e: &ParseError) {
        self.stats.anomalies.malformed_lines += 1;
        if self.first_malformed.is_none() {
            let label = self.file.as_ref().map_or("nyse_ibf", |f| f.label.as_str());
            self.first_malformed = Some(format!("{label} line {line_no}: {e}"));
        }
    }

    /// Maps one parsed message of the current file.
    pub fn apply(&mut self, msg: &Message<'_>, out: &mut MapOutput) -> QtResult<()> {
        if self.st.ended {
            return Err(QtError::Engine("nyse mapper: begin_file before apply".into()));
        }
        self.apply_at(msg, 0, out)
    }

    fn apply_at(&mut self, msg: &Message<'_>, line_no: u64, out: &mut MapOutput) -> QtResult<()> {
        out.mbo.clear();
        out.trades.clear();
        let t = msg.msg_type();
        match self.types.get_mut(usize::from(t)) {
            Some(n) => *n += 1,
            None => *self.stats.by_type.entry(t).or_insert(0) += 1,
        }
        match msg {
            Message::OtherProduct { msg_type } => {
                if self.st.order_messages == 0 {
                    return Err(self.input_err(
                        line_no,
                        format!(
                            "message type {msg_type} belongs to the TAQ BBO, Trades or TRF product, not the \
                             Integrated Feed (EQY_US_<market>_IBF_<n>_<date> files)"
                        ),
                    ));
                }
                self.stats.anomalies.other_product_messages += 1;
                return Ok(());
            }
            Message::Unknown { .. } => {
                self.stats.anomalies.unknown_message_type += 1;
                return Ok(());
            }
            _ => {}
        }
        self.st.good_lines += 1;
        if is_order_message(t) {
            self.st.order_messages += 1;
        }
        if let Some(seq) = msg.seq() {
            self.channel_seq(seq);
        }
        let Some(sym) = msg.symbol() else {
            return Ok(());
        };
        let si = self.symbol_index(sym);
        let ts = match msg.time_ns() {
            Some(tod) => self.ts(tod, line_no)?,
            None => 0,
        };
        if let Message::SymbolIndexMapping(m) = msg {
            self.symbol_mapping(si, m.market_id, m.lot_size);
            return Ok(());
        }
        if let Message::SecurityStatus(s) = msg
            && s.status == b'O'
        {
            if self.stats.core_open_times.len() < MAX_CLOCK_SAMPLES {
                self.stats.core_open_times.push(s.head.time_ns);
            }
            if self.st.core_open.len() < MAX_CLOCK_SAMPLES {
                self.st.core_open.push(s.head.time_ns);
            }
        }
        if let Message::StockSummary(_) = msg {
            return Ok(());
        }
        let Some(head) = msg.head() else {
            return Ok(());
        };
        let refresh = matches!(msg, Message::AddOrderRefresh(_));
        // A failover concerns every symbol of the channel, selected or not.
        self.track_failover(si, refresh, ts);
        if !self.syms[si].selected {
            return Ok(());
        }
        // Only the symbol-sequenced messages of a selected symbol name its channel: they are the ones whose records
        // must stay in one feed order (a Symbol Index Mapping or Stock Summary line writes no record).
        self.see_channel(si);
        let c = Ctx {
            si,
            iid: si as u32 + 1,
            seq: head.seq,
            ts,
        };
        self.settle_suspect(&c, msg, out);
        if !refresh && let Some(r) = self.syms[si].refresh.take() {
            self.refresh_gap(si, r, ts);
            // The run re-baselines the symbol sequence: this message starts it again.
            self.syms[si].last_symbol_seq = None;
        }
        if !refresh {
            self.symbol_seq(si, head.symbol_seq, ts);
        }
        match msg {
            Message::AddOrder(a) => {
                if !a.firm_id.is_empty() {
                    self.stats.attributed_adds += 1;
                }
                self.add(&c, a.order_id, a.side, a.price, a.volume, out);
            }
            Message::AddOrderRefresh(a) => self.refresh_add(&c, a.order_id, a.side, a.price, a.volume, out),
            Message::ModifyOrder(m) => {
                self.modify(&c, m.order_id, m.price, m.volume, m.position_change == 1, m.side, out);
            }
            Message::DeleteOrder(d) => {
                if let Some(o) = self.take(&c, d.order_id) {
                    out.mbo
                        .push(self.rec(&c, b'C', o.side, o.price, o.remaining, d.order_id));
                    self.remove(&c, d.order_id);
                }
            }
            Message::OrderExecution(e) => self.execute(&c, e.order_id, e.price, e.volume, e.printable, out),
            Message::ReplaceOrder(r) => self.replace(&c, r.order_id, r.new_order_id, r.price, r.volume, r.side, out),
            Message::NonDisplayedTrade(n) => {
                if n.volume == 0 {
                    self.stats.anomalies.zero_size += 1;
                } else if !n.printable {
                    self.stats.nonprintable_hidden_trades += 1;
                } else if let Some(px) = self.px(n.price) {
                    self.stats.hidden_trades += 1;
                    out.mbo.push(self.rec(&c, b'T', b'N', px, n.volume, 0));
                    out.trades
                        .push(self.trade(&c, px, n.volume, Side::None, TradeKind::Hidden, None));
                }
            }
            Message::CrossTrade(x) => {
                if x.volume == 0 {
                    self.stats.zero_volume_crosses += 1;
                } else if let Some(px) = self.px(x.price) {
                    self.stats.cross_trades += 1;
                    out.mbo.push(self.rec(&c, b'T', b'N', px, x.volume, 0));
                    out.trades
                        .push(self.trade(&c, px, x.volume, Side::None, TradeKind::Cross, None));
                }
            }
            Message::TradeCancel(_) => self.stats.trade_cancels += 1,
            Message::CrossCorrection(_) => self.stats.cross_corrections += 1,
            Message::SecurityStatus(_) | Message::Imbalance(_) | Message::RetailPriceImprovement(_) => {}
            Message::SymbolIndexMapping(_)
            | Message::StockSummary(_)
            | Message::OtherProduct { .. }
            | Message::Unknown { .. } => {}
        }
        if is_order_message(t) {
            self.syms[si].booked = true;
        }
        if let Some(last) = out.mbo.last_mut() {
            last.flags |= flags::LAST;
        }
        self.stats.mbo_records += out.mbo.len() as u64;
        self.stats.trade_records += out.trades.len() as u64;
        self.stats.orders_live_max = self.stats.orders_live_max.max(self.live);
        Ok(())
    }

    // ---- symbols, clock and sequences ---------------------------------------------------------------------------

    fn symbol_index(&mut self, sym: &[u8]) -> usize {
        if let Some(&iid) = self.ids.get(sym) {
            return iid as usize - 1;
        }
        let name = String::from_utf8_lossy(sym).into_owned();
        let selected = self.filter.as_ref().is_none_or(|f| f.contains(&name));
        self.syms.push(Sym {
            name,
            selected,
            ..Sym::default()
        });
        let iid = self.syms.len() as u32;
        self.ids.insert(sym.into(), iid);
        self.stats.symbols_seen += 1;
        iid as usize - 1
    }

    fn see_channel(&mut self, si: usize) {
        let f = self.ctx();
        let here = (f.date, f.channel);
        let file = self.files;
        let s = &mut self.syms[si];
        s.last_file = file;
        match s.home {
            None => s.home = Some(here),
            Some(h) if h != here => {
                s.home = Some(here);
                self.stats.anomalies.symbol_on_two_channels += 1;
            }
            Some(_) => {}
        }
    }

    // ---- failovers (module docs) ---------------------------------------------------------------------------------

    /// Follows the Add Order Refresh messages of the channel (every symbol): the first one of a failover makes every
    /// selected symbol of this file with resting orders, other than `si`, a suspect.
    fn track_failover(&mut self, si: usize, refresh: bool, ts: Ts) {
        if !refresh {
            self.syms[si].in_refresh = false;
            return;
        }
        let s = &self.syms[si];
        let same = self.st.failover.filter(|f| {
            ts.saturating_sub(f.last_refresh) < FAILOVER_IDLE_NS && !(s.refreshed_in == f.id && !s.in_refresh)
        });
        let id = match same {
            Some(f) => f.id,
            None => {
                self.failovers += 1;
                let file = self.files;
                for (i, s) in self.syms.iter_mut().enumerate() {
                    if i != si && s.selected && s.last_file == file && !s.orders.is_empty() && s.suspect.is_none() {
                        s.suspect = Some(s.last_ts.unwrap_or(ts));
                    }
                }
                self.failovers
            }
        };
        self.st.failover = Some(Failover { id, last_refresh: ts });
        let s = &mut self.syms[si];
        s.refreshed_in = id;
        s.in_refresh = true;
    }

    /// Settles a suspect at its first order message after a failover: an Add Order Refresh refreshes it, a reference
    /// to one of its resting orders keeps its book, an Add (or a Replace of an order it does not have) clears its book
    /// first; other messages leave it a suspect.
    fn settle_suspect(&mut self, c: &Ctx, msg: &Message<'_>, out: &mut MapOutput) {
        let Some(start) = self.syms[c.si].suspect else {
            return;
        };
        let orders = &self.syms[c.si].orders;
        let resting = |id: u64| orders.contains_key(&id);
        let clear = match msg {
            Message::AddOrderRefresh(_) => Some(false),
            Message::AddOrder(_) => Some(true),
            Message::ReplaceOrder(r) => Some(!resting(r.order_id)),
            Message::ModifyOrder(m) => resting(m.order_id).then_some(false),
            Message::DeleteOrder(d) => resting(d.order_id).then_some(false),
            Message::OrderExecution(e) => resting(e.order_id).then_some(false),
            _ => None,
        };
        let Some(clear) = clear else {
            return;
        };
        self.syms[c.si].suspect = None;
        if clear {
            self.clear_unrefreshed(c, start, out);
        } else if !matches!(msg, Message::AddOrderRefresh(_)) {
            self.stats.failover_books_kept += 1;
        }
    }

    /// Clears the book of a suspect that was not refreshed (`R` in `out` at `c`'s time and sequence), records the
    /// feed gap from `start` (its last message before the failover) and re-baselines its symbol sequence (the
    /// backup's Symbol Clear names the next one; the lost messages are inside the gap).
    fn clear_unrefreshed(&mut self, c: &Ctx, start: Ts, out: &mut MapOutput) {
        let n = self.syms[c.si].orders.len() as u64;
        self.live -= n;
        let s = &mut self.syms[c.si];
        s.orders.clear();
        s.last_symbol_seq = None;
        self.stats.anomalies.failover_books_cleared += 1;
        out.mbo.push(self.rec(c, b'R', b'N', qt_types::UNDEF_PRICE, 0, 0));
        self.gaps.push(FeedGap {
            instrument_id: c.iid,
            ts_start: start,
            ts_end: c.ts.max(start),
            reason: FAILOVER_GAP_REASON.into(),
        });
    }

    fn symbol_mapping(&mut self, si: usize, market_id: u32, lot_size: u32) {
        let file_market = self.ctx().market;
        let s = &mut self.syms[si];
        match Market::from_market_id(market_id) {
            Some(m) => {
                if file_market.is_some_and(|f| f != m) {
                    self.stats.anomalies.market_mismatch += 1;
                }
                s.market = Some(m);
            }
            None => self.stats.anomalies.non_equities_market += 1,
        }
        if lot_size > 0 {
            s.lot_size = Some(lot_size);
        }
    }

    /// SourceTime on the file's session date, past midnight when the clock rolled over.
    fn ts(&mut self, tod: u64, line_no: u64) -> QtResult<Ts> {
        if let Some(last) = self.st.last_tod
            && tod.saturating_add(ROLLOVER_NS) < last
        {
            self.st.rollover_days += 1;
            self.stats.anomalies.midnight_rollovers += 1;
        }
        self.st.last_tod = Some(tod);
        let f = self.ctx();
        // tod < 86,400 s, so it fits an i64.
        let ts = self
            .st
            .rollover_days
            .checked_mul(NS_PER_DAY)
            .and_then(|d| f.midnight.checked_add(d))
            .and_then(|m| m.checked_add(tod as i64))
            .ok_or_else(|| self.input_err(line_no, format!("SourceTime {tod} ns overflows the timestamp range")))?;
        self.st.max_ts = Some(self.st.max_ts.map_or(ts, |m| m.max(ts)));
        Ok(ts)
    }

    fn channel_seq(&mut self, seq: u64) {
        if let Some(last) = self.st.last_seq {
            if seq == last {
                self.stats.anomalies.channel_sequence_repeats += 1;
            } else if seq < last {
                self.sequence_regressions += 1;
            }
        }
        self.st.last_seq = Some(seq);
    }

    fn symbol_seq(&mut self, si: usize, seq: u64, ts: Ts) {
        let check = self.check_symbol_sequence;
        let s = &mut self.syms[si];
        let prev = (s.last_symbol_seq, s.last_ts);
        s.last_symbol_seq = Some(seq);
        s.last_ts = Some(ts);
        if !check {
            return;
        }
        let (Some(last), Some(last_ts)) = prev else {
            return;
        };
        if seq <= last {
            self.sequence_regressions += 1;
        } else if seq > last + 1 {
            let missing = seq - last - 1;
            self.sequence_gaps += 1;
            self.stats.anomalies.symbol_sequence_missing += missing;
            self.gaps.push(FeedGap {
                instrument_id: si as u32 + 1,
                ts_start: last_ts,
                ts_end: ts,
                reason: format!(
                    "nyse symbol sequence gap: SymbolSeqNum {last} -> {seq} ({missing} message(s) missing)"
                ),
            });
        }
    }

    fn refresh_gap(&mut self, si: usize, r: Refresh, end: Ts) {
        if let Some(start) = r.start {
            self.gaps.push(FeedGap {
                instrument_id: si as u32 + 1,
                ts_start: start,
                ts_end: end.max(start),
                reason: "nyse book refresh (Add Order Refresh run): the book was cleared and rebuilt".into(),
            });
        }
    }

    // ---- records ------------------------------------------------------------------------------------------------

    #[inline]
    fn px(&mut self, nanos: i64) -> Option<i64> {
        let v = (nanos > 0)
            .then(|| rescale(nanos, PARSED_EXP, self.price_exp))
            .flatten();
        if v.is_none() {
            self.stats.anomalies.bad_price += 1;
        }
        v
    }

    #[inline]
    fn rec(&self, c: &Ctx, action: u8, side: u8, price: i64, size: u64, order_id: u64) -> MboEvent {
        MboEvent {
            ts_event: c.ts,
            ts_recv: c.ts,
            instrument_id: c.iid,
            action,
            side,
            price,
            size,
            order_id,
            flags: 0,
            sequence: c.seq,
            channel_id: self.ctx().channel,
            publisher_id: 0,
        }
    }

    #[inline]
    fn trade(&self, c: &Ctx, price: i64, size: u64, aggressor: Side, kind: TradeKind, maker: Option<u64>) -> TradeRow {
        TradeRow {
            ts_event: c.ts,
            instrument_id: c.iid,
            sequence: c.seq,
            price,
            size,
            aggressor_side: aggressor,
            kind,
            maker_order_id: maker,
        }
    }

    fn take(&mut self, c: &Ctx, id: u64) -> Option<Order> {
        let o = self.syms[c.si].orders.get(&id).copied();
        if o.is_none() {
            self.stats.anomalies.unknown_order_ref += 1;
        }
        o
    }

    fn remove(&mut self, c: &Ctx, id: u64) {
        if self.syms[c.si].orders.remove(&id).is_some() {
            self.live -= 1;
        }
    }

    fn insert(&mut self, c: &Ctx, id: u64, o: Order) {
        if self.syms[c.si].orders.insert(id, o).is_none() {
            self.live += 1;
        }
        self.stats.orders_seen += 1;
    }

    /// Cancels a live order that an add reuses (`duplicate_order_ref`).
    fn cancel_duplicate(&mut self, c: &Ctx, id: u64, out: &mut MapOutput) {
        if let Some(old) = self.syms[c.si].orders.get(&id).copied() {
            self.stats.anomalies.duplicate_order_ref += 1;
            out.mbo.push(self.rec(c, b'C', old.side, old.price, old.remaining, id));
            self.remove(c, id);
        }
    }

    fn book_side(&mut self, side: u8) -> Option<u8> {
        match side {
            b'B' => Some(b'B'),
            b'S' => Some(b'A'),
            _ => {
                self.stats.anomalies.bad_side += 1;
                None
            }
        }
    }

    fn add(&mut self, c: &Ctx, id: u64, side: u8, price: i64, volume: u64, out: &mut MapOutput) {
        let Some(side) = self.book_side(side) else {
            return;
        };
        if volume == 0 {
            self.stats.anomalies.zero_size += 1;
            return;
        }
        let Some(px) = self.px(price) else {
            return;
        };
        self.cancel_duplicate(c, id, out);
        self.insert(
            c,
            id,
            Order {
                side,
                price: px,
                remaining: volume,
            },
        );
        out.mbo.push(self.rec(c, b'A', side, px, volume, id));
    }

    fn refresh_add(&mut self, c: &Ctx, id: u64, side: u8, price: i64, volume: u64, out: &mut MapOutput) {
        let s = &self.syms[c.si];
        // A run starts at the first refresh message, or again when an order id repeats inside a run (a second
        // failover: the Symbol Clear that separated them is not in the TAQ file).
        let new_run = s.refresh.is_none() || s.orders.contains_key(&id);
        if new_run {
            if let Some(r) = self.syms[c.si].refresh.take() {
                self.refresh_gap(c.si, r, c.ts);
            }
            self.stats.anomalies.refresh_runs += 1;
            let s = &mut self.syms[c.si];
            let start = if s.booked {
                Some(s.last_ts.unwrap_or(c.ts))
            } else {
                None
            };
            if s.booked || !s.orders.is_empty() {
                self.live -= s.orders.len() as u64;
                s.orders.clear();
                out.mbo.push(self.rec(c, b'R', b'N', qt_types::UNDEF_PRICE, 0, 0));
            }
            self.syms[c.si].refresh = Some(Refresh { start, last: c.ts });
        }
        if let Some(r) = self.syms[c.si].refresh.as_mut() {
            r.last = c.ts;
        }
        let before = out.mbo.len();
        self.add(c, id, side, price, volume, out);
        if out.mbo.len() > before {
            self.stats.refresh_orders += 1;
        }
        self.syms[c.si].last_ts = Some(c.ts);
    }

    #[allow(clippy::too_many_arguments)]
    fn modify(&mut self, c: &Ctx, id: u64, price: i64, volume: u64, lost: bool, side: u8, out: &mut MapOutput) {
        let Some(o) = self.take(c, id) else {
            return;
        };
        if matches!(side, b'B' | b'S') && (side == b'B') != (o.side == b'B') {
            self.stats.anomalies.side_mismatch += 1;
        }
        if volume == 0 {
            self.stats.modify_lose_priority += 1;
            out.mbo.push(self.rec(c, b'C', o.side, o.price, o.remaining, id));
            self.remove(c, id);
            return;
        }
        let Some(px) = self.px(price) else {
            return;
        };
        let lost = lost || px != o.price || volume > o.remaining;
        if !lost && volume == o.remaining {
            self.stats.modify_noop += 1;
            return;
        }
        if !lost {
            self.stats.modify_keep_priority += 1;
            out.mbo
                .push(self.rec(c, b'C', o.side, o.price, o.remaining - volume, id));
            if let Some(x) = self.syms[c.si].orders.get_mut(&id) {
                x.remaining = volume;
            }
            return;
        }
        self.stats.modify_lose_priority += 1;
        out.mbo.push(self.rec(c, b'C', o.side, o.price, o.remaining, id));
        out.mbo.push(self.rec(c, b'A', o.side, px, volume, id));
        if let Some(x) = self.syms[c.si].orders.get_mut(&id) {
            *x = Order {
                side: o.side,
                price: px,
                remaining: volume,
            };
        }
    }

    fn execute(&mut self, c: &Ctx, id: u64, price: i64, volume: u64, printable: bool, out: &mut MapOutput) {
        let Some(o) = self.take(c, id) else {
            return;
        };
        if volume == 0 {
            self.stats.anomalies.zero_size += 1;
            return;
        }
        let exec = if volume > o.remaining {
            self.stats.anomalies.exec_exceeds_resting += 1;
            o.remaining
        } else {
            volume
        };
        let px = self.px(price).unwrap_or(o.price);
        let aggressor = if o.side == b'B' { Side::Ask } else { Side::Bid };
        // A non-printable execution is an auction allocation (open, close, reopening): its F and C carry
        // PUBLISHER_SPECIFIC, so consumers can tell them from continuous-book executions.
        let auction = if printable {
            out.mbo.push(self.rec(c, b'T', aggressor.as_byte(), px, volume, 0));
            out.trades
                .push(self.trade(c, px, volume, aggressor, TradeKind::Displayed, Some(id)));
            0
        } else {
            self.stats.nonprintable_executions += 1;
            flags::PUBLISHER_SPECIFIC
        };
        let mut f = self.rec(c, b'F', o.side, px, exec, id);
        let mut x = self.rec(c, b'C', o.side, o.price, exec, id);
        f.flags |= auction;
        x.flags |= auction;
        out.mbo.push(f);
        out.mbo.push(x);
        self.stats.executions += 1;
        self.stats.executed_shares += exec;
        let date = self.ctx().date;
        if self.syms[c.si]
            .market
            .or(self.ctx().market)
            .is_some_and(|m| m.allocation_on(date) == Allocation::Parity)
        {
            self.stats.parity_executions += 1;
        }
        if exec == o.remaining {
            self.remove(c, id);
        } else if let Some(x) = self.syms[c.si].orders.get_mut(&id) {
            x.remaining -= exec;
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn replace(&mut self, c: &Ctx, id: u64, new_id: u64, price: i64, volume: u64, side: u8, out: &mut MapOutput) {
        self.stats.replace_orders += 1;
        let Some(o) = self.take(c, id) else {
            // The new order is still added when the message names its side (files from spec 2.3g on), so later
            // references to it resolve.
            if matches!(side, b'B' | b'S') {
                self.add(c, new_id, side, price, volume, out);
            }
            return;
        };
        if matches!(side, b'B' | b'S') && (side == b'B') != (o.side == b'B') {
            self.stats.anomalies.side_mismatch += 1;
        }
        out.mbo.push(self.rec(c, b'C', o.side, o.price, o.remaining, id));
        self.remove(c, id);
        if volume == 0 {
            self.stats.anomalies.zero_size += 1;
            return;
        }
        let Some(px) = self.px(price) else {
            return;
        };
        if px == o.price && volume < o.remaining {
            // A same-price reduction: on Pillar it keeps priority, so if real files publish reductions this way the
            // back-of-the-queue placement below is wrong (counted so that the first licensed file settles it).
            self.stats.replace_reduce_same_price += 1;
        }
        self.cancel_duplicate(c, new_id, out);
        self.insert(
            c,
            new_id,
            Order {
                side: o.side,
                price: px,
                remaining: volume,
            },
        );
        out.mbo.push(self.rec(c, b'A', o.side, px, volume, new_id));
    }

    /// Final bookkeeping: live orders and requested symbols never seen. Call `end_file` for the last input first
    /// (its records would be lost otherwise).
    pub fn finish(&mut self) -> &MapperStats {
        self.end_unflushed("finish");
        for (t, n) in self.types.iter_mut().enumerate() {
            if *n > 0 {
                *self.stats.by_type.entry(t as u16).or_insert(0) += std::mem::take(n);
            }
        }
        self.stats.orders_live = self.live;
        if let Some(f) = &self.filter {
            let mut missing: Vec<String> = f
                .iter()
                .filter(|s| !self.ids.contains_key(s.as_bytes()))
                .cloned()
                .collect();
            missing.sort();
            self.stats.symbols_not_found = missing;
        }
        &self.stats
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MID: Ts = 1_767_589_200_000_000_000; // 2026-01-05 00:00 US/Eastern (EST)
    const S: u64 = 1_000_000_000;

    fn mapper(filter: Option<&[String]>, market: Option<Market>) -> Mapper {
        let mut m = Mapper::new(filter, -9, true).unwrap();
        m.begin_file(FileCtx {
            date: 20_458,
            midnight: MID,
            channel: 3,
            market,
            label: "t.csv".into(),
        });
        m
    }

    fn run(m: &mut Mapper, lines: &[&str]) -> Vec<MboEvent> {
        let mut out = MapOutput::default();
        let mut all = Vec::new();
        for (i, l) in lines.iter().enumerate() {
            m.apply_line(l.as_bytes(), i as u64 + 1, &mut out).unwrap();
            all.extend(out.mbo.iter().copied());
        }
        all
    }

    fn ev(
        action: u8,
        side: u8,
        price_cents: i64,
        size: u64,
        id: u64,
        seq: u64,
        last: bool,
    ) -> (u8, u8, i64, u64, u64, u64, bool) {
        (action, side, price_cents * 10_000_000, size, id, seq, last)
    }

    fn shape(v: &[MboEvent]) -> Vec<(u8, u8, i64, u64, u64, u64, bool)> {
        v.iter()
            .map(|e| (e.action, e.side, e.price, e.size, e.order_id, e.sequence, e.is_last()))
            .collect()
    }

    #[test]
    fn construction_checks_price_exp_and_order_of_calls() {
        assert_eq!(
            Mapper::new(None, -3, true).err().map(|e| e.code()),
            Some("QT-CONFIG-001")
        );
        assert_eq!(
            Mapper::new(None, -13, true).err().map(|e| e.code()),
            Some("QT-CONFIG-001")
        );
        let mut m = Mapper::new(None, -4, true).unwrap();
        let mut out = MapOutput::default();
        assert_eq!(
            m.apply_line(b"3,1,QTX,1", 1, &mut out).err().map(|e| e.code()),
            Some("QT-ENGINE-001")
        );
    }

    #[test]
    fn add_modify_delete_replace_and_executions() {
        let mut m = mapper(None, Some(Market::Nyse));
        let recs = run(
            &mut m,
            &[
                "3,1,QTX,1,1,N,C,100,,,0,Y,1,100",
                "100,2,09:30:00.000000001,QTX,1,11,25.10,300,B,,",
                "100,4,09:30:00.000000002,QTX,2,12,25.12,200,S,ABCD,",
                // keep priority (same price, smaller size), no-op, lose priority (larger size), new price
                "101,5,09:30:01.000000000,QTX,3,11,25.10,200,,B,",
                "101,6,09:30:01.000000001,QTX,4,11,25.10,200,,B,",
                "101,7,09:30:01.000000002,QTX,5,11,25.10,400,,B,",
                "101,8,09:30:01.000000003,QTX,6,11,25.09,400,,,",
                // PositionChange 1 forces a priority loss at the same price and size
                "101,9,09:30:01.000000004,QTX,7,12,25.12,200,1,S,",
                // printable execution with price improvement, then a non-printable one
                "103,10,09:30:02.000000000,QTX,8,12,9001,25.11,50,1,,@,,,",
                "103,11,09:30:02.000000001,QTX,9,12,9002,25.12,50,0,,,,,",
                "104,12,09:30:03.000000000,QTX,10,11,13,25.08,100,B,",
                "102,13,09:30:04.000000000,QTX,11,13,",
                "102,14,09:30:04.000000001,QTX,12,12,",
            ],
        );
        // The non-printable execution (an auction allocation) is flagged PUBLISHER_SPECIFIC, the printable one not.
        let auction = |seq| -> Vec<u8> {
            recs.iter()
                .filter(|e| e.sequence == seq)
                .map(|e| e.flags & flags::PUBLISHER_SPECIFIC)
                .collect()
        };
        assert_eq!(
            (auction(10), auction(11)),
            (vec![0; 3], vec![flags::PUBLISHER_SPECIFIC; 2])
        );
        let (b, a, t, f, c) = (b'B', b'A', b'T', b'F', b'C');
        assert_eq!(
            shape(&recs),
            vec![
                ev(b'A', b, 2510, 300, 11, 2, true),
                ev(b'A', a, 2512, 200, 12, 4, true),
                ev(c, b, 2510, 100, 11, 5, true),
                ev(c, b, 2510, 200, 11, 7, false),
                ev(b'A', b, 2510, 400, 11, 7, true),
                ev(c, b, 2510, 400, 11, 8, false),
                ev(b'A', b, 2509, 400, 11, 8, true),
                ev(c, a, 2512, 200, 12, 9, false),
                ev(b'A', a, 2512, 200, 12, 9, true),
                ev(t, b, 2511, 50, 0, 10, false),
                ev(f, a, 2511, 50, 12, 10, false),
                ev(c, a, 2512, 50, 12, 10, true),
                ev(f, a, 2512, 50, 12, 11, false),
                ev(c, a, 2512, 50, 12, 11, true),
                ev(c, b, 2509, 400, 11, 12, false),
                ev(b'A', b, 2508, 100, 13, 12, true),
                ev(c, b, 2508, 100, 13, 13, true),
                ev(c, a, 2512, 100, 12, 14, true),
            ]
        );
        assert!(
            recs.iter()
                .all(|e| e.channel_id == 3 && e.instrument_id == 1 && e.ts_recv == e.ts_event)
        );
        assert_eq!(recs[0].ts_event, MID + 34_200 * S as i64 + 1);
        let st = m.finish().clone();
        assert_eq!(
            (st.modify_keep_priority, st.modify_noop, st.modify_lose_priority),
            (1, 1, 3)
        );
        assert_eq!((st.executions, st.executed_shares, st.parity_executions), (2, 100, 2));
        assert_eq!(
            (st.nonprintable_executions, st.replace_orders, st.attributed_adds),
            (1, 1, 1)
        );
        assert_eq!((st.orders_seen, st.orders_live, st.trade_records), (3, 0, 1));
        assert_eq!(st.anomalies, Anomalies::default());
        assert!(m.gaps().is_empty());
        assert_eq!((m.sequence_gaps, m.sequence_regressions), (0, 0));
    }

    /// Parity executions follow the market of the symbol (its Symbol Index Mapping, else the file name); the
    /// dataset label itself is per partition (`ingest::allocation_labels`).
    #[test]
    fn parity_executions_follow_the_market() {
        let lines = [
            "100,2,09:30:00,QTX,1,11,25.10,300,B,,",
            "103,3,09:30:01,QTX,2,11,1,25.10,100,1,,,,,",
        ];
        let mut arca = mapper(None, Some(Market::Arca));
        run(&mut arca, &lines);
        assert_eq!(arca.stats.parity_executions, 0);
        let mut unknown = mapper(None, None);
        run(&mut unknown, &lines);
        assert_eq!((unknown.stats.executions, unknown.stats.parity_executions), (1, 0));
        // The Symbol Index Mapping market wins over the file name (and a mismatch is counted).
        let mut mixed = mapper(None, Some(Market::Arca));
        run(&mut mixed, &["3,1,QTX,1,1,N,C,100,,,0,Y,1,100", lines[0], lines[1]]);
        assert_eq!(
            (mixed.stats.parity_executions, mixed.stats.anomalies.market_mismatch),
            (1, 1)
        );
        assert_eq!(mixed.instruments()[0].market, Some(Market::Nyse));
        let mut options = mapper(None, Some(Market::Arca));
        run(&mut options, &["3,1,QTX,4,1,P,C,100,,,0,Y,1,100"]);
        assert_eq!(options.stats.anomalies.non_equities_market, 1);
    }

    #[test]
    fn hidden_and_cross_prints_and_counters_without_records() {
        let mut m = mapper(None, Some(Market::Nyse));
        let recs = run(
            &mut m,
            &[
                "110,2,09:30:00,QTX,1,500,25.105,40,1,@,,,I",
                "110,3,09:30:00,QTX,2,501,25.105,40,0,,,,",
                "110,4,09:30:00,QTX,3,502,25.105,,1,,,,",
                "111,5,09:30:00,QTX,4,7,25.10,12000,O",
                "111,6,09:30:00,QTX,5,8,25.10,,O",
                "112,7,09:30:01,QTX,6,500",
                "113,8,09:30:01,QTX,7,7,11900",
                "114,9,09:30:01,QTX,8,A",
                "105,10,09:30:01,QTX,9,25.10,100,0,0,1600,C,B,,,,,,,0,0,0,0,,",
                "34,11,09:30:00,QTX,10,O,~,,,,,,~,O",
                "223,12,16:00:00,QTX,,,,,12345",
            ],
        );
        assert_eq!(
            shape(&recs),
            vec![
                (b'T', b'N', 25_105_000_000, 40, 0, 2, true),
                (b'T', b'N', 25_100_000_000, 12_000, 0, 5, true),
            ]
        );
        let st = m.finish().clone();
        assert_eq!(
            (st.hidden_trades, st.nonprintable_hidden_trades, st.cross_trades),
            (1, 1, 1)
        );
        assert_eq!((st.zero_volume_crosses, st.anomalies.zero_size), (1, 1));
        assert_eq!((st.trade_cancels, st.cross_corrections), (1, 1));
        assert_eq!(st.core_open_times, vec![34_200 * S]);
        assert_eq!(st.by_type.values().sum::<u64>(), 11);
        assert_eq!(m.stats.trade_records, 2);
        // Not one order message: the file is counted and named.
        assert_eq!(st.anomalies.files_without_order_messages, 1);
        assert_eq!(m.files_without_orders, vec!["t.csv".to_owned()]);
    }

    #[test]
    fn anomalies_are_counted_not_fatal() {
        let mut m = mapper(None, Some(Market::Nyse));
        let recs = run(
            &mut m,
            &[
                "100,2,09:30:00,QTX,1,11,25.10,300,B,,",
                "100,3,09:30:00,QTX,2,11,25.11,100,B,,", // duplicate live id: C then A
                "100,4,09:30:00,QTX,3,12,25.11,100,X,,", // bad side
                "100,5,09:30:00,QTX,4,13,0,100,B,,",     // zero price
                "100,6,09:30:00,QTX,5,14,25.11,,B,,",    // zero size
                "101,7,09:30:00,QTX,6,99,25.11,100,,B,", // unknown
                "101,8,09:30:00,QTX,7,11,25.11,50,,S,",  // side mismatch, keeps priority
                "103,9,09:30:00,QTX,8,11,1,25.11,70,1,,,,,", // over-execution clamped to 50
                "999,10,x",                              // unknown type
                "100,11,09:30:00,QTX,9,15,25.1x,100,B,,", // malformed
                "140,12,09:30:00,,QTX,10,1,1,1,1,R,",    // BBO type after order messages
                "102,13,09:30:00,QTX,11,15,",            // unknown (15 never added)
            ],
        );
        assert_eq!(
            shape(&recs),
            vec![
                ev(b'A', b'B', 2510, 300, 11, 2, true),
                ev(b'C', b'B', 2510, 300, 11, 3, false),
                ev(b'A', b'B', 2511, 100, 11, 3, true),
                ev(b'C', b'B', 2511, 50, 11, 8, true),
                ev(b'T', b'A', 2511, 70, 0, 9, false),
                ev(b'F', b'B', 2511, 50, 11, 9, false),
                ev(b'C', b'B', 2511, 50, 11, 9, true),
            ]
        );
        let a = m.finish().anomalies.clone();
        assert_eq!(
            (
                a.duplicate_order_ref,
                a.bad_side,
                a.bad_price,
                a.zero_size,
                a.unknown_order_ref
            ),
            (1, 1, 1, 1, 2)
        );
        assert_eq!(
            (
                a.side_mismatch,
                a.exec_exceeds_resting,
                a.unknown_message_type,
                a.malformed_lines
            ),
            (1, 1, 1, 1)
        );
        assert_eq!(a.other_product_messages, 1);
        // The malformed line (SymbolSeqNum 9) and the BBO line (10) are lost messages of the symbol: a gap before 11.
        assert_eq!((m.sequence_gaps, a.symbol_sequence_missing), (1, 2));
        assert_eq!(
            m.first_malformed.as_deref(),
            Some("t.csv line 10: message type 100: field 7 is not a decimal price")
        );
        assert_eq!(m.stats.orders_live, 0);
    }

    #[test]
    fn wrong_files_fail_fast() {
        let mut m = mapper(None, None);
        let mut out = MapOutput::default();
        let e = m.apply_line(b"MsgType,SequenceNumber,Symbol", 1, &mut out).unwrap_err();
        assert_eq!(e.code(), "QT-INPUT-002");
        assert!(
            e.to_string().contains("t.csv line 1") && e.to_string().contains("not a TAQ Integrated Feed file"),
            "{e}"
        );
        let mut m = mapper(None, None);
        m.apply_line(b"3,1,QTX,1,1,N,C,100,,,0,Y,1,100", 1, &mut out).unwrap();
        let e = m
            .apply_line(b"140,2,09:30:00,QTX,1,25.1,100,25.0,100,R,", 2, &mut out)
            .unwrap_err();
        assert!(e.to_string().contains("BBO, Trades or TRF"), "{e}");
    }

    #[test]
    fn symbol_filter_ids_and_two_channels() {
        let f = vec!["QTB A".to_owned(), "MISSING".to_owned()];
        let mut m = mapper(Some(&f), Some(Market::Nyse));
        let recs = run(
            &mut m,
            &[
                "3,1,QTA,1,1,N,C,100,,,0,Y,1,100",
                "3,2,QTB A,1,1,N,C,100,,,0,Y,1,100",
                "100,3,09:30:00,QTA,1,11,25.10,300,B,,",
                "100,4,09:30:00,QTB A,1,11,25.10,300,B,,",
            ],
        );
        assert_eq!(recs.len(), 1);
        assert_eq!(
            recs[0].instrument_id, 2,
            "ids follow first appearance before the filter"
        );
        m.begin_file(FileCtx {
            date: 20_458,
            midnight: MID,
            channel: 4,
            market: Some(Market::Nyse),
            label: "u.csv".into(),
        });
        // A Symbol Index Mapping line and an unselected symbol's messages on the second channel write no record and
        // are not counted; the delete of the selected symbol is.
        let recs = run(
            &mut m,
            &[
                "3,1,QTB A,1,1,N,C,100,,,0,Y,1,100",
                "100,2,09:31:00,QTA,2,12,25.11,100,B,,",
                "102,3,09:31:00,QTB A,2,11,",
            ],
        );
        assert_eq!(
            (recs.len(), recs[0].channel_id),
            (1, 4),
            "the book carries over within a day"
        );
        let st = m.finish().clone();
        assert_eq!(st.anomalies.symbol_on_two_channels, 1);
        assert_eq!(st.symbols_not_found, vec!["MISSING".to_owned()]);
        let info = m.instruments();
        assert_eq!(
            info.iter()
                .map(|i| (i.instrument_id, i.symbol.as_str(), i.selected))
                .collect::<Vec<_>>(),
            vec![(1, "QTA", false), (2, "QTB A", true)]
        );
        assert_eq!((info[1].market, info[1].lot_size), (Some(Market::Nyse), Some(100)));
    }

    #[test]
    fn a_new_day_starts_from_an_empty_book() {
        let mut m = mapper(None, Some(Market::Nyse));
        run(&mut m, &["100,2,09:30:00,QTX,1,11,25.10,300,B,,"]);
        m.begin_file(FileCtx {
            date: 20_459,
            midnight: MID + NS_PER_DAY,
            channel: 3,
            market: Some(Market::Nyse),
            label: "next.csv".into(),
        });
        // Symbol sequence 1 again on the new day is no regression; order 11 is gone.
        run(&mut m, &["102,2,09:30:00,QTX,1,11,"]);
        assert_eq!(m.stats.anomalies.unknown_order_ref, 1);
        assert_eq!((m.sequence_gaps, m.sequence_regressions), (0, 0));
        assert_eq!(m.finish().orders_live, 0);
    }

    #[test]
    fn sequences_gaps_regressions_and_rollover() {
        let mut m = mapper(None, Some(Market::Nyse));
        run(
            &mut m,
            &[
                "100,10,09:30:00,QTX,1,11,25.10,300,B,,",
                "100,12,09:30:01,QTX,2,12,25.10,300,B,,", // channel hole (an omitted control message): fine
                "100,12,09:30:02,QTX,3,13,25.10,300,B,,", // channel repeat
                "100,11,09:30:03,QTX,4,14,25.10,300,B,,", // channel regression
                "100,20,09:30:04,QTX,7,15,25.10,300,B,,", // symbol gap: 5 and 6 missing
                "100,21,09:30:05,QTX,7,16,25.10,300,B,,", // symbol repeat
                "100,22,23:59:59,QTX,8,17,25.10,300,B,,",
                "100,23,00:00:01,QTX,9,18,25.10,300,B,,", // clock passed midnight
            ],
        );
        assert_eq!(m.stats.anomalies.channel_sequence_repeats, 1);
        assert_eq!((m.sequence_gaps, m.sequence_regressions), (1, 2));
        assert_eq!(m.stats.anomalies.symbol_sequence_missing, 2);
        assert_eq!(
            m.gaps(),
            [FeedGap {
                instrument_id: 1,
                ts_start: MID + 34_203 * S as i64,
                ts_end: MID + 34_204 * S as i64,
                reason: "nyse symbol sequence gap: SymbolSeqNum 4 -> 7 (2 message(s) missing)".into(),
            }]
        );
        assert_eq!(m.stats.anomalies.midnight_rollovers, 1);
        let mut out = MapOutput::default();
        m.apply_line(b"102,24,00:00:02,QTX,10,18,", 9, &mut out).unwrap();
        assert_eq!(out.mbo[0].ts_event, MID + NS_PER_DAY + 2 * S as i64);
        // With the check off nothing is recorded for symbol sequences.
        let mut off = Mapper::new(None, -9, false).unwrap();
        off.begin_file(FileCtx {
            date: 20_458,
            midnight: MID,
            channel: 1,
            market: None,
            label: "x".into(),
        });
        run(
            &mut off,
            &[
                "100,1,09:30:00,QTX,1,11,25.10,300,B,,",
                "100,2,09:30:00,QTX,9,12,25.10,300,B,,",
            ],
        );
        assert_eq!((off.sequence_gaps, off.gaps().len()), (0, 0));
    }

    #[test]
    fn refresh_runs_clear_and_rebuild_the_book() {
        let mut m = mapper(None, Some(Market::Nyse));
        let recs = run(
            &mut m,
            &[
                // a refresh at the start of the day: no clear, no gap
                "106,1,04:00:00,QTX,1,11,25.10,300,B,,",
                "100,2,09:30:00,QTX,2,12,25.11,100,S,,",
                "100,3,09:30:01,QTX,3,13,25.12,100,S,,",
                // failover: Symbol Clear (not in TAQ), then a refresh of what is live
                "106,5,09:31:00,QTX,40,11,25.10,300,B,,",
                "106,6,09:31:00,QTX,41,13,25.12,100,S,,",
                // a second failover inside the run: order 11 repeats
                "106,7,09:31:05,QTX,50,11,25.10,300,B,,",
                "102,8,09:32:00,QTX,51,11,",
            ],
        );
        let actions: Vec<u8> = recs.iter().map(|e| e.action).collect();
        assert_eq!(actions, b"AAARAARAC");
        assert_eq!(recs[3].price, qt_types::UNDEF_PRICE);
        let st = m.finish().clone();
        assert_eq!((st.anomalies.refresh_runs, st.refresh_orders), (3, 4));
        assert_eq!(st.orders_live, 0, "orders 12 and 13 were cleared, 11 deleted");
        assert_eq!(st.anomalies.unknown_order_ref, 0);
        assert_eq!(m.sequence_gaps, 0, "refresh messages re-baseline the symbol sequence");
        let gaps: Vec<(i64, i64)> = m.gaps().iter().map(|g| (g.ts_start, g.ts_end)).collect();
        assert_eq!(
            gaps,
            vec![
                (MID + 34_201 * S as i64, MID + 34_265 * S as i64),
                (MID + 34_260 * S as i64, MID + 34_320 * S as i64),
            ]
        );
    }

    /// `(instrument, action, order id, sequence, LAST)` of each record.
    fn ids(v: &[MboEvent]) -> Vec<(u32, u8, u64, u64, bool)> {
        v.iter()
            .map(|e| (e.instrument_id, e.action, e.order_id, e.sequence, e.is_last()))
            .collect()
    }

    fn at(h: i64, m: i64, s: i64) -> Ts {
        MID + ((h * 60 + m) * 60 + s) * S as i64
    }

    /// TAQ spec v2.4b section 11: after a publisher failover the backup clears every symbol of the channel and
    /// re-sends only the orders still resting, so a book emptied during the outage gets no refresh. Its stale orders
    /// go at its first Add after the failover (or at the end of the file); a symbol that references one of its
    /// resting orders kept its book; Security Status lines and references to unknown orders say nothing.
    #[test]
    fn a_failover_clears_the_books_it_did_not_refresh() {
        let mut m = mapper(None, Some(Market::Arca));
        let mut recs = run(
            &mut m,
            &[
                "100,2,09:40:00,QTF,1,11,12.40,300,B,,",
                "100,3,09:40:01,QTG,1,21,30.00,200,B,,",
                "100,4,09:40:02,QTH,1,31,50.00,100,S,,",
                "100,5,09:40:03,QTJ,1,41,7.00,100,B,,",
                "100,6,09:40:04,QTK,1,51,9.00,100,B,,",
                // 10:00:05 failover: of the five books only QTF's order still rests at the backup
                "106,8,10:00:05,QTF,2,11,12.40,300,B,,",
                "34,9,10:00:06,QTK,2,O,~,,,,,,~,O",
                "102,10,10:00:07,QTK,3,99,",
                // QTG's delete (SymbolSeqNum 2 and 3) was lost in the outage; its 29.90 offer would cross the stale bid
                "100,11,10:01:00,QTG,4,22,29.90,100,S,,",
                "102,12,10:02:00,QTH,2,31,",
                "100,13,10:03:00,QTK,5,52,9.01,100,B,,",
            ],
        );
        let mut out = MapOutput::default();
        m.end_file(&mut out);
        recs.extend(out.mbo.iter().copied());
        let (a, r) = (b'A', b'R');
        assert_eq!(
            ids(&recs),
            vec![
                (1, a, 11, 2, true),
                (2, a, 21, 3, true),
                (3, a, 31, 4, true),
                (4, a, 41, 5, true),
                (5, a, 51, 6, true),
                (1, r, 0, 8, false),
                (1, a, 11, 8, true),
                (2, r, 0, 11, false),
                (2, a, 22, 11, true),
                (3, b'C', 31, 12, true),
                (5, r, 0, 13, false),
                (5, a, 52, 13, true),
                // QTJ never sent another message: cleared at the end of the file, at its latest time
                (4, r, 0, 13, true),
            ]
        );
        assert_eq!(recs.last().map(|e| e.ts_event), Some(at(10, 3, 0)));
        // A second end_file does nothing, and the ended file takes no more lines.
        m.end_file(&mut out);
        assert!(out.mbo.is_empty());
        assert_eq!(
            m.apply_line(b"102,14,10:04:00,QTF,3,11,", 14, &mut out)
                .err()
                .map(|e| e.code()),
            Some("QT-ENGINE-001")
        );
        let st = m.finish().clone();
        assert_eq!(
            (
                st.anomalies.failover_books_cleared,
                st.failover_books_kept,
                st.anomalies.refresh_runs
            ),
            (3, 1, 1)
        );
        assert_eq!((st.anomalies.unknown_order_ref, st.orders_live), (1, 3));
        assert_eq!(
            (m.sequence_gaps, m.sequence_regressions),
            (0, 0),
            "a cleared book re-baselines its symbol sequence"
        );
        let mut gaps: Vec<(u32, Ts, Ts, bool)> = m
            .gaps()
            .iter()
            .map(|g| (g.instrument_id, g.ts_start, g.ts_end, g.reason == FAILOVER_GAP_REASON))
            .collect();
        gaps.sort_unstable();
        assert_eq!(
            gaps,
            vec![
                (1, at(9, 40, 0), at(10, 0, 5), false),
                (2, at(9, 40, 1), at(10, 1, 0), true),
                (4, at(9, 40, 3), at(10, 3, 0), true),
                (5, at(9, 40, 4), at(10, 3, 0), true),
            ]
        );
    }

    /// The backup's spin (XDP Common Client Specification v2.4 section 9.2: per symbol its Symbol Index Mapping, the
    /// Symbol Clear, its last Security Status, its refresh) is one failover, so a symbol refreshed early in it is no
    /// suspect when the next symbol's refresh starts. A symbol refreshed again after going back to live messages
    /// starts a new failover.
    #[test]
    fn a_failover_spin_is_one_failover_and_a_second_refresh_starts_another() {
        let mut m = mapper(None, Some(Market::Arca));
        let recs = run(
            &mut m,
            &[
                "100,2,09:40:00,QTA,1,11,10.00,100,B,,",
                "100,3,09:40:00,QTB,1,21,20.00,100,B,,",
                "100,4,09:40:00,QTC,1,31,30.00,100,B,,",
                // the spin; QTB lost its order in the outage, so it gets no refresh
                "3,6,QTA,3,1,N,C,100,,,0,Y,1,100",
                "34,7,10:00:00,QTA,2,O,~,,,,,,~,O",
                "106,8,10:00:00,QTA,3,11,10.00,100,B,,",
                "3,9,QTB,3,1,N,C,100,,,0,Y,1,100",
                "34,10,10:00:00,QTB,2,O,~,,,,,,~,O",
                "3,11,QTC,3,1,N,C,100,,,0,Y,1,100",
                "34,12,10:00:00,QTC,2,O,~,,,,,,~,O",
                "106,13,10:00:00,QTC,3,31,30.00,100,B,,",
                // live again
                "100,15,10:00:01,QTA,4,12,10.01,100,B,,",
                "100,16,10:00:01,QTB,4,22,20.01,100,B,,",
                "102,17,10:00:02,QTC,4,31,",
                // QTA refreshed again: a new failover; QTB then deletes its resting order (this refresh missed it)
                "106,19,10:00:30,QTA,5,11,10.00,100,B,,",
                "106,20,10:00:30,QTA,6,12,10.01,100,B,,",
                "102,21,10:00:31,QTB,5,22,",
            ],
        );
        let (a, r, c) = (b'A', b'R', b'C');
        assert_eq!(
            ids(&recs),
            vec![
                (1, a, 11, 2, true),
                (2, a, 21, 3, true),
                (3, a, 31, 4, true),
                (1, r, 0, 8, false),
                (1, a, 11, 8, true),
                (3, r, 0, 13, false),
                (3, a, 31, 13, true),
                (1, a, 12, 15, true),
                (2, r, 0, 16, false),
                (2, a, 22, 16, true),
                (3, c, 31, 17, true),
                (1, r, 0, 19, false),
                (1, a, 11, 19, true),
                (1, a, 12, 20, true),
                (2, c, 22, 21, true),
            ]
        );
        let mut out = MapOutput::default();
        m.end_file(&mut out);
        assert!(out.mbo.is_empty());
        assert_eq!(m.failovers, 2);
        let st = m.finish().clone();
        assert_eq!(
            (
                st.anomalies.failover_books_cleared,
                st.failover_books_kept,
                st.anomalies.refresh_runs
            ),
            (1, 1, 3)
        );
        assert_eq!((m.sequence_gaps, m.sequence_regressions, st.orders_live), (0, 0, 2));
        let cleared: Vec<(u32, Ts, Ts)> = m
            .gaps()
            .iter()
            .filter(|g| g.reason == FAILOVER_GAP_REASON)
            .map(|g| (g.instrument_id, g.ts_start, g.ts_end))
            .collect();
        assert_eq!(cleared, vec![(2, at(9, 40, 0), at(10, 0, 1))]);
    }

    /// The refresh messages of symbols outside the filter still mark the failover of the channel.
    #[test]
    fn a_failover_seen_on_unselected_symbols_clears_selected_books() {
        let f = vec!["QTB".to_owned()];
        let mut m = mapper(Some(&f), Some(Market::Nyse));
        let recs = run(
            &mut m,
            &[
                "100,2,09:40:00,QTA,1,11,10.00,100,B,,",
                "100,3,09:40:00,QTB,1,21,20.00,100,B,,",
                "106,5,10:00:00,QTA,2,11,10.00,100,B,,",
                "100,6,10:00:01,QTB,3,22,19.99,100,S,,",
            ],
        );
        assert_eq!(
            ids(&recs),
            vec![(2, b'A', 21, 3, true), (2, b'R', 0, 6, false), (2, b'A', 22, 6, true)]
        );
        assert_eq!(m.finish().anomalies.failover_books_cleared, 1);
    }

    /// A Replace Order at the old side and price with a smaller size is counted: Pillar keeps the priority of such a
    /// reduction, which the reader expects as a Modify Order (`rules` module docs).
    #[test]
    fn same_price_replace_reductions_are_counted() {
        let mut m = mapper(None, Some(Market::Nyse));
        let recs = run(
            &mut m,
            &[
                "100,2,09:30:00,QTX,1,11,25.10,300,B,,",
                "104,3,09:30:01,QTX,2,11,12,25.10,200,B,",
                "104,4,09:30:02,QTX,3,12,13,25.10,200,B,",
                "104,5,09:30:03,QTX,4,13,14,25.11,100,B,",
            ],
        );
        assert_eq!(recs.iter().filter(|e| e.action == b'A').count(), 4);
        assert_eq!((m.stats.replace_orders, m.stats.replace_reduce_same_price), (3, 1));
    }
}
