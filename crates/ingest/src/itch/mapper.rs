//! ITCH messages -> normalized MBO records and source trade prints (port of et `convert.rs`, et semantics).
//!
//! One `sequence` per ITCH message that produces records (u64, checked); `LAST` on its last record;
//! `instrument_id` = stock locate; `Price (4)` scaled exactly to `10^price_exp` (default `-9`: `x 100_000`).
//! - `A`/`F` -> `A` (side B -> bid, S -> ask).
//! - `E` -> `T` (aggressor = opposite of the resting side, `order_id` 0, at the resting price) + `F` (resting order)
//!   + `C` (executed size); source print kind `displayed` with the maker order id.
//! - `C` -> same, but `T` and the print only when printable is `Y`; `T`/`F` at the execution price, `C` at the
//!   resting price.
//! - `X` -> `C` partial; `D` -> `C` of the remaining size tracked here; `U` -> `C` old (remaining) + `A` new (new id,
//!   back of queue, side kept from the old order).
//! - `P` -> `T` side `N` (hidden print, kind `hidden`); `Q` -> `T` side `N` (kind `cross`), omitted when shares are
//!   0. `B` and the administrative types produce no records.
//!
//! Anomalies (unknown references, over-executions, ...) are counted and clamped, never fatal.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};

use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, Ts, flags};

use super::message::{Body, Message, StockDirectory, stock_str};

/// Fixed multiplicative hasher for order ids (deterministic; never iterated for output).
#[derive(Default, Clone, Copy)]
pub struct IdHasher(u64);

impl Hasher for IdHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0.rotate_left(8) ^ u64::from(b)).wrapping_mul(0x9e37_79b9_7f4a_7c15);
        }
    }
    #[inline]
    fn write_u64(&mut self, v: u64) {
        let x = (self.0 ^ v).wrapping_mul(0x9e37_79b9_7f4a_7c15);
        self.0 = x ^ (x >> 29);
    }
}

/// `BuildHasher` of `IdHasher`.
pub type IdBuild = BuildHasherDefault<IdHasher>;

#[derive(Debug, Clone, Copy)]
struct Order {
    locate: u16,
    side: u8,
    price: u32,
    remaining: u32,
}

/// Converter anomalies (et `Anomalies`).
#[derive(Debug, Default, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Anomalies {
    /// `E`/`C`/`X`/`D`/`U` for an order id never added (or already gone).
    pub unknown_order_ref: u64,
    /// Executed shares above the tracked remaining size (clamped).
    pub exec_exceeds_resting: u64,
    /// `X` shares above the tracked remaining size (clamped).
    pub cancel_exceeds_resting: u64,
    /// `A`/`F`/`U` introducing an order id that is already live (replaced).
    pub duplicate_order_ref: u64,
    /// Message stock locate differs from the order's add (the order's is used).
    pub locate_mismatch: u64,
    /// `Q` with zero shares (no `T` emitted).
    pub zero_share_cross: u64,
    /// `A`/`F`/`P` with a Buy/Sell indicator other than B or S (skipped).
    pub bad_side: u64,
    /// Message types not in the ITCH 5.0 table (skipped via the length prefix).
    pub unknown_message_type: u64,
    /// Known types shorter than their layout (skipped).
    pub short_message: u64,
}

impl Anomalies {
    /// Sum of every counter.
    pub fn total(&self) -> u64 {
        self.unknown_order_ref
            + self.exec_exceeds_resting
            + self.cancel_exceeds_resting
            + self.duplicate_order_ref
            + self.locate_mismatch
            + self.zero_share_cross
            + self.bad_side
            + self.unknown_message_type
            + self.short_message
    }

    /// `(name, value)` pairs in a fixed order (names prefixed `itch_`).
    pub fn named(&self) -> [(&'static str, u64); 9] {
        [
            ("itch_unknown_order_ref", self.unknown_order_ref),
            ("itch_exec_exceeds_resting", self.exec_exceeds_resting),
            ("itch_cancel_exceeds_resting", self.cancel_exceeds_resting),
            ("itch_duplicate_order_ref", self.duplicate_order_ref),
            ("itch_locate_mismatch", self.locate_mismatch),
            ("itch_zero_share_cross", self.zero_share_cross),
            ("itch_bad_side", self.bad_side),
            ("itch_unknown_message_type", self.unknown_message_type),
            ("itch_short_message", self.short_message),
        ]
    }
}

/// Converter statistics (et `stats.json`).
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct MapperStats {
    /// Messages seen.
    pub messages: u64,
    /// Counts by ITCH message type (all messages, before the symbol filter).
    pub by_type: BTreeMap<String, u64>,
    /// System event code -> ns since midnight.
    pub system_events: BTreeMap<String, u64>,
    /// Stock directory messages.
    pub directory_entries: u64,
    /// Requested symbols absent from the stock directory.
    pub symbols_not_found: Vec<String>,
    /// Locates selected by the symbol filter (all directory entries without a filter).
    pub selected_instruments: u64,
    /// Orders added.
    pub orders_seen: u64,
    /// Orders live at the end.
    pub orders_live: u64,
    /// Maximum simultaneously live orders.
    pub orders_live_max: u64,
    /// Largest order size.
    pub max_order_size: u32,
    /// Executions.
    pub executions: u64,
    /// Executed shares.
    pub executed_shares: u64,
    /// MBO records emitted.
    pub mbo_records: u64,
    /// Source trade prints emitted.
    pub trade_records: u64,
    /// First message timestamp (ns since midnight).
    pub first_ts_ns: Option<u64>,
    /// Last message timestamp (ns since midnight).
    pub last_ts_ns: Option<u64>,
    /// Anomaly counters.
    pub anomalies: Anomalies,
}

/// Records produced by one ITCH message.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct MapOutput {
    /// MBO records (the last carries `LAST`).
    pub mbo: Vec<MboEvent>,
    /// Source trade prints.
    pub trades: Vec<TradeRow>,
}

/// ITCH -> MBO converter state for one trading day.
pub struct Mapper {
    midnight: Ts,
    price_scale: i64,
    symbols: Option<HashSet<String>>,
    selected: Vec<bool>,
    orders: HashMap<u64, Order, IdBuild>,
    seq: u64,
    by_type: [u64; 256],
    /// Statistics.
    pub stats: MapperStats,
    /// Selected stock directory entries in arrival order: (locate, directory).
    pub directory: Vec<(u16, StockDirectory)>,
}

impl Mapper {
    /// `midnight_utc_ns` is added to every timestamp; `symbols` filters by stock directory symbol (`None` keeps
    /// every instrument); prices are written at `10^price_exp` (`-12..=-4`).
    pub fn new(midnight_utc_ns: Ts, symbols: Option<&[String]>, price_exp: i8) -> QtResult<Self> {
        if !(-12..=-4).contains(&price_exp) {
            return Err(QtError::Config(format!(
                "price_exp: ITCH prices have 4 decimals; price_exp must be in -12..=-4, got {price_exp}"
            )));
        }
        if midnight_utc_ns < 0 {
            return Err(QtError::Config(format!(
                "midnight_utc_ns: must be >= 0, got {midnight_utc_ns}"
            )));
        }
        let price_scale = 10i64.pow(u32::from((-4 - price_exp).unsigned_abs()));
        Ok(Self {
            midnight: midnight_utc_ns,
            price_scale,
            symbols: symbols.map(|s| s.iter().map(|x| x.trim().to_ascii_uppercase()).collect()),
            selected: vec![false; 65_536],
            orders: HashMap::with_capacity_and_hasher(1 << 16, IdBuild::default()),
            seq: 0,
            by_type: [0; 256],
            stats: MapperStats::default(),
            directory: Vec::new(),
        })
    }

    #[inline]
    fn selected(&self, locate: u16) -> bool {
        self.symbols.is_none() || self.selected[usize::from(locate)]
    }

    #[inline]
    fn ts(&self, ts_ns: u64) -> QtResult<Ts> {
        i64::try_from(ts_ns)
            .ok()
            .and_then(|t| self.midnight.checked_add(t))
            .ok_or_else(|| QtError::InputFormat {
                location: "itch".into(),
                msg: format!("timestamp {ts_ns} overflows"),
            })
    }

    #[inline]
    fn next_seq(&mut self) -> QtResult<()> {
        self.seq = self
            .seq
            .checked_add(1)
            .ok_or_else(|| QtError::Engine("itch sequence counter overflow".into()))?;
        Ok(())
    }

    #[inline]
    fn px(&self, p: u32) -> i64 {
        // u32 x <= 10^8 always fits in i64.
        i64::from(p) * self.price_scale
    }

    #[allow(clippy::too_many_arguments)]
    #[inline]
    fn mbo(&self, ts: Ts, locate: u16, action: u8, side: u8, price: i64, size: u64, id: u64) -> MboEvent {
        MboEvent {
            ts_event: ts,
            ts_recv: ts,
            instrument_id: u32::from(locate),
            action,
            side,
            price,
            size,
            order_id: id,
            flags: 0,
            sequence: self.seq,
            channel_id: 0,
            publisher_id: 0,
        }
    }

    #[allow(clippy::too_many_arguments)]
    #[inline]
    fn trade(
        &self,
        ts: Ts,
        locate: u16,
        price: i64,
        size: u64,
        side: Side,
        kind: TradeKind,
        maker: Option<u64>,
    ) -> TradeRow {
        TradeRow {
            ts_event: ts,
            instrument_id: u32::from(locate),
            sequence: self.seq,
            price,
            size,
            aggressor_side: side,
            kind,
            maker_order_id: maker,
        }
    }

    fn take_order(&mut self, order_ref: u64, locate: u16) -> Option<Order> {
        let Some(o) = self.orders.get(&order_ref).copied() else {
            self.stats.anomalies.unknown_order_ref += 1;
            return None;
        };
        if o.locate != locate {
            self.stats.anomalies.locate_mismatch += 1;
        }
        Some(o)
    }

    fn insert_order(&mut self, order_ref: u64, o: Order) {
        if self.orders.insert(order_ref, o).is_some() {
            self.stats.anomalies.duplicate_order_ref += 1;
        }
        self.stats.orders_seen += 1;
        self.stats.max_order_size = self.stats.max_order_size.max(o.remaining);
        self.stats.orders_live_max = self.stats.orders_live_max.max(self.orders.len() as u64);
    }

    fn shrink(&mut self, order_ref: u64, by: u32) {
        if let Some(o) = self.orders.get_mut(&order_ref) {
            o.remaining = o.remaining.saturating_sub(by);
            if o.remaining == 0 {
                self.orders.remove(&order_ref);
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn execute(
        &mut self,
        out: &mut MapOutput,
        ts: Ts,
        locate: u16,
        order_ref: u64,
        shares: u32,
        print: bool,
        exec_price: Option<u32>,
    ) -> QtResult<()> {
        let Some(o) = self.take_order(order_ref, locate) else {
            return Ok(());
        };
        let exec = if shares > o.remaining {
            self.stats.anomalies.exec_exceeds_resting += 1;
            o.remaining
        } else {
            shares
        };
        if exec == 0 {
            return Ok(());
        }
        self.next_seq()?;
        let px = self.px(exec_price.unwrap_or(o.price));
        let resting_px = self.px(o.price);
        let (aggr_byte, aggr) = if o.side == b'B' {
            (b'A', Side::Ask)
        } else {
            (b'B', Side::Bid)
        };
        let (shares, exec_sz) = (u64::from(shares), u64::from(exec));
        if print {
            out.mbo.push(self.mbo(ts, o.locate, b'T', aggr_byte, px, shares, 0));
            out.trades
                .push(self.trade(ts, o.locate, px, shares, aggr, TradeKind::Displayed, Some(order_ref)));
        }
        out.mbo
            .push(self.mbo(ts, o.locate, b'F', o.side, px, exec_sz, order_ref));
        out.mbo
            .push(self.mbo(ts, o.locate, b'C', o.side, resting_px, exec_sz, order_ref));
        self.stats.executions += 1;
        self.stats.executed_shares += u64::from(exec);
        self.shrink(order_ref, exec);
        Ok(())
    }

    /// Converts one message; `out` is cleared first and holds the records it produced.
    pub fn apply(&mut self, msg: &Message, out: &mut MapOutput) -> QtResult<()> {
        out.mbo.clear();
        out.trades.clear();
        let h = msg.header;
        self.stats.messages += 1;
        self.by_type[usize::from(h.kind)] += 1;
        if self.stats.first_ts_ns.is_none() {
            self.stats.first_ts_ns = Some(h.ts_ns);
        }
        self.stats.last_ts_ns = Some(h.ts_ns);
        let ts = self.ts(h.ts_ns)?;
        match msg.body {
            Body::SystemEvent { event } => {
                self.stats.system_events.insert((event as char).to_string(), h.ts_ns);
            }
            Body::StockDirectory(dir) => {
                self.stats.directory_entries += 1;
                let keep = match &self.symbols {
                    None => true,
                    Some(set) => set.contains(stock_str(&dir.stock)),
                };
                if keep {
                    self.selected[usize::from(h.locate)] = true;
                    self.directory.push((h.locate, dir));
                }
            }
            Body::AddOrder {
                order_ref,
                buy_sell,
                shares,
                price,
                ..
            } => {
                if !self.selected(h.locate) {
                    return Ok(());
                }
                let side = match buy_sell {
                    b'B' => b'B',
                    b'S' => b'A',
                    _ => {
                        self.stats.anomalies.bad_side += 1;
                        return Ok(());
                    }
                };
                self.next_seq()?;
                self.insert_order(
                    order_ref,
                    Order {
                        locate: h.locate,
                        side,
                        price,
                        remaining: shares,
                    },
                );
                let px = self.px(price);
                out.mbo
                    .push(self.mbo(ts, h.locate, b'A', side, px, u64::from(shares), order_ref));
            }
            Body::Executed { order_ref, shares, .. } => {
                if self.selected(h.locate) {
                    self.execute(out, ts, h.locate, order_ref, shares, true, None)?;
                }
            }
            Body::ExecutedWithPrice {
                order_ref,
                shares,
                printable,
                price,
                ..
            } => {
                if self.selected(h.locate) {
                    self.execute(out, ts, h.locate, order_ref, shares, printable == b'Y', Some(price))?;
                }
            }
            Body::Cancel { order_ref, shares } => {
                if !self.selected(h.locate) {
                    return Ok(());
                }
                let Some(o) = self.take_order(order_ref, h.locate) else {
                    return Ok(());
                };
                let by = if shares > o.remaining {
                    self.stats.anomalies.cancel_exceeds_resting += 1;
                    o.remaining
                } else {
                    shares
                };
                if by == 0 {
                    return Ok(());
                }
                self.next_seq()?;
                let px = self.px(o.price);
                out.mbo
                    .push(self.mbo(ts, o.locate, b'C', o.side, px, u64::from(by), order_ref));
                self.shrink(order_ref, by);
            }
            Body::Delete { order_ref } => {
                if !self.selected(h.locate) {
                    return Ok(());
                }
                let Some(o) = self.take_order(order_ref, h.locate) else {
                    return Ok(());
                };
                self.next_seq()?;
                let px = self.px(o.price);
                out.mbo
                    .push(self.mbo(ts, o.locate, b'C', o.side, px, u64::from(o.remaining), order_ref));
                self.orders.remove(&order_ref);
            }
            Body::Replace {
                old_ref,
                new_ref,
                shares,
                price,
            } => {
                if !self.selected(h.locate) {
                    return Ok(());
                }
                let Some(o) = self.take_order(old_ref, h.locate) else {
                    return Ok(());
                };
                self.next_seq()?;
                let old_px = self.px(o.price);
                out.mbo
                    .push(self.mbo(ts, o.locate, b'C', o.side, old_px, u64::from(o.remaining), old_ref));
                self.orders.remove(&old_ref);
                self.insert_order(
                    new_ref,
                    Order {
                        locate: o.locate,
                        side: o.side,
                        price,
                        remaining: shares,
                    },
                );
                let px = self.px(price);
                out.mbo
                    .push(self.mbo(ts, o.locate, b'A', o.side, px, u64::from(shares), new_ref));
            }
            Body::Trade {
                buy_sell,
                shares,
                price,
                ..
            } => {
                if !self.selected(h.locate) {
                    return Ok(());
                }
                if !matches!(buy_sell, b'B' | b'S') {
                    self.stats.anomalies.bad_side += 1;
                    return Ok(());
                }
                self.next_seq()?;
                let px = self.px(price);
                let size = u64::from(shares);
                out.mbo.push(self.mbo(ts, h.locate, b'T', b'N', px, size, 0));
                out.trades
                    .push(self.trade(ts, h.locate, px, size, Side::None, TradeKind::Hidden, None));
            }
            Body::CrossTrade { shares, price, .. } => {
                if !self.selected(h.locate) {
                    return Ok(());
                }
                if shares == 0 {
                    self.stats.anomalies.zero_share_cross += 1;
                    return Ok(());
                }
                self.next_seq()?;
                let px = self.px(price);
                out.mbo.push(self.mbo(ts, h.locate, b'T', b'N', px, shares, 0));
                out.trades
                    .push(self.trade(ts, h.locate, px, shares, Side::None, TradeKind::Cross, None));
            }
            Body::Unknown => self.stats.anomalies.unknown_message_type += 1,
            Body::TradingAction { .. }
            | Body::RegSho { .. }
            | Body::MarketParticipant { .. }
            | Body::MwcbLevels { .. }
            | Body::MwcbStatus { .. }
            | Body::IpoQuoting { .. }
            | Body::LuldCollar { .. }
            | Body::OperationalHalt { .. }
            | Body::BrokenTrade { .. }
            | Body::Noii(_)
            | Body::Rpii { .. }
            | Body::DirectListing(_) => {}
        }
        if let Some(last) = out.mbo.last_mut() {
            last.flags |= flags::LAST;
        }
        self.stats.mbo_records += out.mbo.len() as u64;
        self.stats.trade_records += out.trades.len() as u64;
        Ok(())
    }

    /// Counts a message the parser rejected as too short.
    pub fn note_short_message(&mut self) {
        self.stats.messages += 1;
        self.stats.anomalies.short_message += 1;
    }

    /// Live orders (bounded memory: this, not the file, is what the mapper holds).
    pub fn live_orders(&self) -> usize {
        self.orders.len()
    }

    /// Last sequence number assigned.
    pub fn sequence(&self) -> u64 {
        self.seq
    }

    /// Final bookkeeping of the statistics.
    pub fn finish(&mut self) -> &MapperStats {
        self.stats.orders_live = self.orders.len() as u64;
        self.stats.by_type = self
            .by_type
            .iter()
            .enumerate()
            .filter(|(_, n)| **n > 0)
            .map(|(k, n)| (((k as u8) as char).to_string(), *n))
            .collect();
        self.stats.selected_instruments = if self.symbols.is_some() {
            self.directory.len() as u64
        } else {
            self.stats.directory_entries
        };
        if let Some(set) = &self.symbols {
            let found: HashSet<&str> = self.directory.iter().map(|(_, d)| stock_str(&d.stock)).collect();
            let mut missing: Vec<String> = set.iter().filter(|s| !found.contains(s.as_str())).cloned().collect();
            missing.sort();
            self.stats.symbols_not_found = missing;
        }
        &self.stats
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sequence_is_u64_and_checked() {
        let mut m = Mapper::new(0, None, -9).unwrap();
        m.seq = u64::from(u32::MAX);
        m.next_seq().unwrap();
        assert_eq!(m.sequence(), u64::from(u32::MAX) + 1, "no u32 wrap (et audit defect)");
        m.seq = u64::MAX;
        assert_eq!(m.next_seq().unwrap_err().code(), "QT-ENGINE-001");
        assert!(Mapper::new(0, None, -3).is_err());
        assert!(Mapper::new(-1, None, -9).is_err());
        let m = Mapper::new(0, None, -4).unwrap();
        assert_eq!(m.px(1_852_500), 1_852_500);
        let m = Mapper::new(0, None, -12).unwrap();
        assert_eq!(m.px(u32::MAX), i64::from(u32::MAX) * 100_000_000);
    }
}
