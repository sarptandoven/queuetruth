//! DEEP+ messages -> normalized MBO records and source trade prints (`iex` rule set, `qt_types::rules_for`).
//!
//! `sequence` of every record = the IEX-TP message sequence number (so the `T` / `F` / `C` of one message share it
//! and feed gaps are real); `channel_id` = the IEX-TP Channel ID; `ts_event` = the message Timestamp (ns since the
//! epoch, UTC); `ts_recv` = the segment's Send Time (the message Timestamp when the Send Time is earlier, counted
//! `iex_send_time_before_event`); `LAST` on the last record of a message for each instrument it touched.
//! `instrument_id` = order of first appearance of the symbol in the whole feed (1, 2, ...; before the symbol
//! filter, so ids do not depend on it). Prices: 4 decimals scaled exactly to `10^price_exp`; sizes in shares.
//!
//! | DEEP+ (v1.06) | records |
//! |---|---|
//! | `a` Add Order (p.21) | `A` (side `8` -> `B`, `5` -> `A`) |
//! | `M` Order Modify (p.22), bit 0 = 1 (maintain), same price, size down | `C` of the decrease (priority kept) |
//! | `M`, bit 0 = 1, same price and size | none (`iex_modify_noop`) |
//! | `M`, bit 0 = 0 (reset) | `C` of the remaining size + `A` (same id, new price / size, back of the queue) |
//! | `M`, bit 0 = 1 with a new price or a larger size | `C` + `A` (counted `iex_modify_maintain_unrepresentable`) |
//! | `M` to size 0 | `C` of the remaining size |
//! | `R` Order Delete (p.23) | `C` of the remaining size |
//! | `L` Order Executed (p.24) | `T` (aggressor = opposite side, `order_id` 0, at the trade price) + `F` (resting order, trade price) + `C` (resting price); print kind `displayed` with the maker id. With the single-price-cross flag (0x08) the `T` has side `N` and the print kind `cross` (an auction, no aggressor) |
//! | `T` Trade (p.25) | `T` side `N` (hidden print; kind `cross` with flag 0x08) |
//! | `C` Clear Book (p.27) | `R` (the symbol's live orders are dropped) |
//! | `B` Trade Break, `S D H I O P E` | none (statistics) |
//!
//! A duplicate live order id on `a` first cancels the old order (`C`), then adds. Anomalies (unknown references,
//! over-executions, bad sides or prices, ...) are counted and never fatal.

use std::collections::{BTreeMap, HashMap, HashSet};

use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{MboEvent, QtError, QtResult, Side, Ts, UNDEF_PRICE, flags};

use super::message::{MODIFY_MAINTAIN_PRIORITY, Message, Symbol, sale, symbol_str};
use crate::itch::IdBuild;

#[derive(Debug, Clone, Copy)]
struct Order {
    iid: u32,
    side: u8,
    /// Raw price, 4 decimals.
    price: i64,
    remaining: u32,
}

/// Converter anomalies (every counter is reported as `source_specific` `iex_<name>`).
#[derive(Debug, Default, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Anomalies {
    /// `M` / `R` / `L` for an order id never added (or already gone).
    pub unknown_order_ref: u64,
    /// `a` with an order id that is still live (the old order is cancelled first).
    pub duplicate_order_ref: u64,
    /// `L` volume above the tracked remaining size (clamped).
    pub exec_exceeds_resting: u64,
    /// Message symbol differs from the referenced order's symbol (the order's is used).
    pub symbol_mismatch: u64,
    /// `a` with a side other than `8` / `5` (skipped).
    pub bad_side: u64,
    /// `a` / `M` with a price <= 0 or not representable at `price_exp` (skipped), or an `L` / `T` trade price that
    /// is (the resting price is used for `L`; the `T` is skipped).
    pub bad_price: u64,
    /// `a` with size 0, `L` / `T` with volume 0 (skipped).
    pub zero_size: u64,
    /// Order-book message with a timestamp <= 0 (skipped).
    pub bad_timestamp: u64,
    /// `M` with the maintain-priority bit but a new price or a larger size: qt records cannot keep priority across
    /// a price change or an increase, so it is written as `C` + `A` (loses priority).
    pub modify_maintain_unrepresentable: u64,
    /// Segment Send Time earlier than the message Timestamp (`ts_recv` = `ts_event`).
    pub send_time_before_event: u64,
    /// Trade Break messages (prints already written are not withdrawn).
    pub trade_breaks: u64,
    /// Message types not in the v1.06 table (skipped).
    pub unknown_message_type: u64,
    /// Known types shorter than their layout (skipped).
    pub short_message: u64,
    /// Zero-length messages (skipped).
    pub empty_message: u64,
}

impl Anomalies {
    /// `(name, value)` pairs in a fixed order (names prefixed `iex_`).
    pub fn named(&self) -> [(&'static str, u64); 14] {
        [
            ("iex_unknown_order_ref", self.unknown_order_ref),
            ("iex_duplicate_order_ref", self.duplicate_order_ref),
            ("iex_exec_exceeds_resting", self.exec_exceeds_resting),
            ("iex_symbol_mismatch", self.symbol_mismatch),
            ("iex_bad_side", self.bad_side),
            ("iex_bad_price", self.bad_price),
            ("iex_zero_size", self.zero_size),
            ("iex_bad_timestamp", self.bad_timestamp),
            (
                "iex_modify_maintain_unrepresentable",
                self.modify_maintain_unrepresentable,
            ),
            ("iex_send_time_before_event", self.send_time_before_event),
            ("iex_trade_breaks", self.trade_breaks),
            ("iex_unknown_message_type", self.unknown_message_type),
            ("iex_short_message", self.short_message),
            ("iex_empty_message", self.empty_message),
        ]
    }
}

/// Converter statistics.
#[derive(Debug, Default, Clone, serde::Serialize)]
pub struct MapperStats {
    /// Messages seen (all symbols).
    pub messages: u64,
    /// Counts by message type (all messages).
    pub by_type: BTreeMap<String, u64>,
    /// System event code -> timestamp (last of each code).
    pub system_events: BTreeMap<String, i64>,
    /// Security Directory messages.
    pub directory_entries: u64,
    /// Symbols seen in the feed.
    pub symbols_seen: u64,
    /// Requested symbols that never appeared.
    pub symbols_not_found: Vec<String>,
    /// Instruments selected by the symbol filter.
    pub selected_instruments: u64,
    /// Orders added (selected instruments).
    pub orders_seen: u64,
    /// Orders live at the end.
    pub orders_live: u64,
    /// Maximum simultaneously live orders.
    pub orders_live_max: u64,
    /// `M` with the maintain-priority bit.
    pub modify_maintain_priority: u64,
    /// `M` with the reset-priority bit.
    pub modify_reset_priority: u64,
    /// Maintain-priority `M` with unchanged price and size (no record).
    pub modify_noop: u64,
    /// `L` messages applied.
    pub executions: u64,
    /// Shares executed against displayed orders.
    pub executed_shares: u64,
    /// `T` (non-displayed) prints.
    pub hidden_trades: u64,
    /// Shares of `T` prints.
    pub hidden_shares: u64,
    /// Prints flagged single-price cross.
    pub cross_prints: u64,
    /// Clear Book messages applied.
    pub clear_books: u64,
    /// Trading status changes by status byte (selected instruments).
    pub trading_status: BTreeMap<String, u64>,
    /// MBO records emitted.
    pub mbo_records: u64,
    /// Source trade prints emitted.
    pub trade_records: u64,
    /// First message timestamp.
    pub first_ts: Option<i64>,
    /// Last message timestamp.
    pub last_ts: Option<i64>,
    /// Anomaly counters.
    pub anomalies: Anomalies,
}

/// Records produced by one message.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct MapOutput {
    /// MBO records.
    pub mbo: Vec<MboEvent>,
    /// Source trade prints.
    pub trades: Vec<TradeRow>,
}

/// Transport context of one message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MsgCtx {
    /// IEX-TP message sequence number.
    pub seq: u64,
    /// IEX-TP Channel ID (as `MboEvent::channel_id`).
    pub channel: u16,
    /// Segment Send Time (ns since the epoch).
    pub send_time: i64,
}

/// Per-symbol directory entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SymbolInfo {
    /// Symbol text.
    pub symbol: String,
    /// Selected by the filter.
    pub selected: bool,
    /// Round lot from the Security Directory.
    pub round_lot: Option<u32>,
    /// Security Directory flags.
    pub directory_flags: Option<u8>,
}

/// DEEP+ -> MBO converter state for one feed.
pub struct Mapper {
    price_scale: i64,
    filter: Option<HashSet<String>>,
    ids: HashMap<Symbol, u32>,
    /// Instruments by id - 1.
    pub instruments: Vec<SymbolInfo>,
    orders: HashMap<u64, Order, IdBuild>,
    by_type: [u64; 256],
    /// Statistics.
    pub stats: MapperStats,
}

impl Mapper {
    /// `symbols` filters by symbol (`None` keeps every instrument); prices are written at `10^price_exp`
    /// (`-12..=-4`).
    pub fn new(symbols: Option<&[String]>, price_exp: i8) -> QtResult<Self> {
        if !(-12..=-4).contains(&price_exp) {
            return Err(QtError::Config(format!(
                "price_exp: DEEP+ prices have 4 decimals; price_exp must be in -12..=-4, got {price_exp}"
            )));
        }
        Ok(Self {
            price_scale: 10i64.pow(u32::from((-4 - price_exp).unsigned_abs())),
            filter: symbols.map(|s| s.iter().map(|x| x.trim().to_ascii_uppercase()).collect()),
            ids: HashMap::new(),
            instruments: Vec::new(),
            orders: HashMap::with_capacity_and_hasher(1 << 14, IdBuild::default()),
            by_type: [0; 256],
            stats: MapperStats::default(),
        })
    }

    /// Instrument id of a symbol (assigned on first sight) and whether the filter selects it.
    pub fn instrument(&mut self, s: &Symbol) -> (u32, bool) {
        if let Some(&id) = self.ids.get(s) {
            return (id, self.instruments[id as usize - 1].selected);
        }
        let name = symbol_str(s);
        let selected = self
            .filter
            .as_ref()
            .is_none_or(|f| f.contains(&name.to_ascii_uppercase()));
        let id = u32::try_from(self.instruments.len() + 1).unwrap_or(u32::MAX);
        self.ids.insert(*s, id);
        self.instruments.push(SymbolInfo {
            symbol: name,
            selected,
            round_lot: None,
            directory_flags: None,
        });
        self.stats.symbols_seen += 1;
        (id, selected)
    }

    /// Price at `10^price_exp`; `None` when <= 0 or not representable.
    #[inline]
    fn px(&self, raw: i64) -> Option<i64> {
        if raw <= 0 {
            return None;
        }
        raw.checked_mul(self.price_scale).filter(|&p| p != UNDEF_PRICE)
    }

    fn take(&mut self, order_id: u64, iid: u32) -> Option<Order> {
        let Some(o) = self.orders.get(&order_id).copied() else {
            self.stats.anomalies.unknown_order_ref += 1;
            return None;
        };
        if o.iid != iid {
            self.stats.anomalies.symbol_mismatch += 1;
        }
        Some(o)
    }

    fn insert(&mut self, order_id: u64, o: Order) {
        self.orders.insert(order_id, o);
        self.stats.orders_seen += 1;
        self.stats.orders_live_max = self.stats.orders_live_max.max(self.orders.len() as u64);
    }

    /// Converts one message; `out` is cleared first and holds the records it produced. The only errors are
    /// internal (a `QtError::Engine` never caused by input bytes).
    pub fn apply(&mut self, ctx: MsgCtx, msg: &Message, out: &mut MapOutput) -> QtResult<()> {
        out.mbo.clear();
        out.trades.clear();
        self.stats.messages += 1;
        let kind = msg.kind();
        self.by_type[usize::from(kind)] += 1;
        let Some(ts) = msg.ts() else {
            self.stats.anomalies.unknown_message_type += 1;
            return Ok(());
        };
        if self.stats.first_ts.is_none() {
            self.stats.first_ts = Some(ts);
        }
        self.stats.last_ts = Some(ts);
        let sel = msg.symbol().map(|s| self.instrument(s));
        let book_message = matches!(kind, b'a' | b'M' | b'R' | b'L' | b'T' | b'C');
        if book_message && ts <= 0 {
            self.stats.anomalies.bad_timestamp += 1;
            return Ok(());
        }
        let recv = if ctx.send_time >= ts {
            ctx.send_time
        } else {
            if book_message && sel.is_some_and(|(_, s)| s) {
                self.stats.anomalies.send_time_before_event += 1;
            }
            ts
        };
        let mut emit = Emit { out, ts, recv, ctx };
        match *msg {
            Message::SystemEvent { event, .. } => {
                self.stats.system_events.insert((event as char).to_string(), ts);
            }
            Message::SecurityDirectory { flags, round_lot, .. } => {
                self.stats.directory_entries += 1;
                if let Some((iid, _)) = sel {
                    let info = &mut self.instruments[iid as usize - 1];
                    info.round_lot = Some(round_lot);
                    info.directory_flags = Some(flags);
                }
            }
            Message::TradingStatus { status, .. } => {
                if sel.is_some_and(|(_, s)| s) {
                    *self
                        .stats
                        .trading_status
                        .entry((status as char).to_string())
                        .or_insert(0) += 1;
                }
            }
            Message::RetailLiquidity { .. }
            | Message::OperationalHalt { .. }
            | Message::ShortSalePriceTest { .. }
            | Message::SecurityEvent { .. } => {}
            Message::Unknown { .. } => self.stats.anomalies.unknown_message_type += 1,
            Message::TradeBreak { .. } => {
                if sel.is_some_and(|(_, s)| s) {
                    self.stats.anomalies.trade_breaks += 1;
                }
            }
            Message::AddOrder {
                side,
                order_id,
                size,
                price,
                ..
            } => {
                let Some((iid, true)) = sel else { return Ok(()) };
                let side = match side {
                    b'8' => b'B',
                    b'5' => b'A',
                    _ => {
                        self.stats.anomalies.bad_side += 1;
                        return Ok(());
                    }
                };
                let Some(px) = self.px(price) else {
                    self.stats.anomalies.bad_price += 1;
                    return Ok(());
                };
                if size == 0 {
                    self.stats.anomalies.zero_size += 1;
                    return Ok(());
                }
                let id = order_id as u64;
                if let Some(old) = self.orders.remove(&id) {
                    self.stats.anomalies.duplicate_order_ref += 1;
                    let opx = self.px(old.price).unwrap_or(px);
                    emit.mbo(old.iid, b'C', old.side, opx, u64::from(old.remaining), id);
                }
                self.insert(
                    id,
                    Order {
                        iid,
                        side,
                        price,
                        remaining: size,
                    },
                );
                emit.mbo(iid, b'A', side, px, u64::from(size), id);
            }
            Message::OrderModify {
                flags: mflags,
                order_id,
                size,
                price,
                ..
            } => {
                let Some((iid, true)) = sel else { return Ok(()) };
                let id = order_id as u64;
                let Some(o) = self.take(id, iid) else { return Ok(()) };
                let maintain = mflags & MODIFY_MAINTAIN_PRIORITY != 0;
                if maintain {
                    self.stats.modify_maintain_priority += 1;
                } else {
                    self.stats.modify_reset_priority += 1;
                }
                let old_px = self.px(o.price).unwrap_or(UNDEF_PRICE);
                if size == 0 {
                    emit.mbo(o.iid, b'C', o.side, old_px, u64::from(o.remaining), id);
                    self.orders.remove(&id);
                } else if maintain && price == o.price && size <= o.remaining {
                    if size == o.remaining {
                        self.stats.modify_noop += 1;
                    } else {
                        emit.mbo(o.iid, b'C', o.side, old_px, u64::from(o.remaining - size), id);
                        if let Some(r) = self.orders.get_mut(&id) {
                            r.remaining = size;
                        }
                    }
                } else {
                    let Some(px) = self.px(price) else {
                        self.stats.anomalies.bad_price += 1;
                        return Ok(());
                    };
                    if maintain {
                        self.stats.anomalies.modify_maintain_unrepresentable += 1;
                    }
                    emit.mbo(o.iid, b'C', o.side, old_px, u64::from(o.remaining), id);
                    emit.mbo(o.iid, b'A', o.side, px, u64::from(size), id);
                    self.orders.insert(
                        id,
                        Order {
                            iid: o.iid,
                            side: o.side,
                            price,
                            remaining: size,
                        },
                    );
                }
            }
            Message::OrderDelete { order_id, .. } => {
                let Some((iid, true)) = sel else { return Ok(()) };
                let id = order_id as u64;
                let Some(o) = self.take(id, iid) else { return Ok(()) };
                let px = self.px(o.price).unwrap_or(UNDEF_PRICE);
                emit.mbo(o.iid, b'C', o.side, px, u64::from(o.remaining), id);
                self.orders.remove(&id);
            }
            Message::OrderExecuted {
                sale_flags,
                order_id,
                size,
                price,
                ..
            } => {
                let Some((iid, true)) = sel else { return Ok(()) };
                let id = order_id as u64;
                let Some(o) = self.take(id, iid) else { return Ok(()) };
                if size == 0 {
                    self.stats.anomalies.zero_size += 1;
                    return Ok(());
                }
                let exec = if size > o.remaining {
                    self.stats.anomalies.exec_exceeds_resting += 1;
                    o.remaining
                } else {
                    size
                };
                let resting_px = self.px(o.price).unwrap_or(UNDEF_PRICE);
                let px = match self.px(price) {
                    Some(p) => p,
                    None => {
                        self.stats.anomalies.bad_price += 1;
                        resting_px
                    }
                };
                let cross = sale_flags & sale::SINGLE_PRICE_CROSS != 0;
                let (aggr_byte, aggr) = match (cross, o.side) {
                    (true, _) => (b'N', Side::None),
                    (false, b'B') => (b'A', Side::Ask),
                    (false, _) => (b'B', Side::Bid),
                };
                let kind = if cross {
                    self.stats.cross_prints += 1;
                    TradeKind::Cross
                } else {
                    TradeKind::Displayed
                };
                emit.mbo(o.iid, b'T', aggr_byte, px, u64::from(size), 0);
                emit.trade(o.iid, px, u64::from(size), aggr, kind, Some(id));
                emit.mbo(o.iid, b'F', o.side, px, u64::from(exec), id);
                emit.mbo(o.iid, b'C', o.side, resting_px, u64::from(exec), id);
                self.stats.executions += 1;
                self.stats.executed_shares += u64::from(exec);
                if let Some(r) = self.orders.get_mut(&id) {
                    r.remaining -= exec;
                    if r.remaining == 0 {
                        self.orders.remove(&id);
                    }
                }
            }
            Message::Trade {
                sale_flags,
                size,
                price,
                ..
            } => {
                let Some((iid, true)) = sel else { return Ok(()) };
                if size == 0 {
                    self.stats.anomalies.zero_size += 1;
                    return Ok(());
                }
                let Some(px) = self.px(price) else {
                    self.stats.anomalies.bad_price += 1;
                    return Ok(());
                };
                let kind = if sale_flags & sale::SINGLE_PRICE_CROSS != 0 {
                    self.stats.cross_prints += 1;
                    TradeKind::Cross
                } else {
                    TradeKind::Hidden
                };
                self.stats.hidden_trades += 1;
                self.stats.hidden_shares += u64::from(size);
                emit.mbo(iid, b'T', b'N', px, u64::from(size), 0);
                emit.trade(iid, px, u64::from(size), Side::None, kind, None);
            }
            Message::ClearBook { .. } => {
                let Some((iid, true)) = sel else { return Ok(()) };
                self.orders.retain(|_, o| o.iid != iid);
                self.stats.clear_books += 1;
                emit.mbo(iid, b'R', b'N', UNDEF_PRICE, 0, 0);
            }
        }
        mark_last(&mut out.mbo);
        self.stats.mbo_records += out.mbo.len() as u64;
        self.stats.trade_records += out.trades.len() as u64;
        Ok(())
    }

    /// Counts a message the parser rejected (`short`: a known type shorter than its layout; else empty).
    pub fn note_unparsed(&mut self, short: bool) {
        self.stats.messages += 1;
        if short {
            self.stats.anomalies.short_message += 1;
        } else {
            self.stats.anomalies.empty_message += 1;
        }
    }

    /// Drops every live order (a new input file is a new trading day starting from an empty book); instrument
    /// ids and statistics are kept.
    pub fn reset_book(&mut self) {
        self.orders.clear();
    }

    /// Live orders.
    pub fn live_orders(&self) -> usize {
        self.orders.len()
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
        self.stats.selected_instruments = self.instruments.iter().filter(|i| i.selected).count() as u64;
        if let Some(f) = &self.filter {
            let seen: HashSet<&str> = self.instruments.iter().map(|i| i.symbol.as_str()).collect();
            let mut missing: Vec<String> = f.iter().filter(|s| !seen.contains(s.as_str())).cloned().collect();
            missing.sort();
            self.stats.symbols_not_found = missing;
        }
        &self.stats
    }
}

/// `LAST` on the last record of each instrument in one message's output.
fn mark_last(mbo: &mut [MboEvent]) {
    let mut seen: [u32; 4] = [0; 4];
    let mut n = 0;
    for e in mbo.iter_mut().rev() {
        if !seen[..n].contains(&e.instrument_id) {
            e.flags |= flags::LAST;
            if n < seen.len() {
                seen[n] = e.instrument_id;
                n += 1;
            }
        }
    }
}

struct Emit<'a> {
    out: &'a mut MapOutput,
    ts: Ts,
    recv: Ts,
    ctx: MsgCtx,
}

impl Emit<'_> {
    #[inline]
    fn mbo(&mut self, iid: u32, action: u8, side: u8, price: i64, size: u64, order_id: u64) {
        self.out.mbo.push(MboEvent {
            ts_event: self.ts,
            ts_recv: self.recv,
            instrument_id: iid,
            action,
            side,
            price,
            size,
            order_id,
            flags: 0,
            sequence: self.ctx.seq,
            channel_id: self.ctx.channel,
            publisher_id: 0,
        });
    }

    #[inline]
    fn trade(&mut self, iid: u32, price: i64, size: u64, side: Side, kind: TradeKind, maker: Option<u64>) {
        self.out.trades.push(TradeRow {
            ts_event: self.ts,
            instrument_id: iid,
            sequence: self.ctx.seq,
            price,
            size,
            aggressor_side: side,
            kind,
            maker_order_id: maker,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::iex::message::symbol;

    #[test]
    fn last_flag_per_instrument() {
        let mut v: Vec<MboEvent> = [1u32, 2, 1]
            .iter()
            .map(|&i| MboEvent {
                instrument_id: i,
                ..MboEvent::default()
            })
            .collect();
        mark_last(&mut v);
        let f: Vec<bool> = v.iter().map(|e| e.flags & flags::LAST != 0).collect();
        assert_eq!(f, [false, true, true]);
    }

    #[test]
    fn price_scaling_and_filter() {
        assert!(Mapper::new(None, -3).is_err());
        let mut m = Mapper::new(Some(&["spy".into()]), -9).unwrap();
        assert_eq!(m.px(990_500), Some(99_050_000_000));
        assert_eq!(m.px(0), None);
        assert_eq!(m.px(i64::MAX / 10), None);
        assert_eq!(m.instrument(&symbol("QQQ")), (1, false));
        assert_eq!(m.instrument(&symbol("SPY")), (2, true));
        assert_eq!(m.instrument(&symbol("QQQ")), (1, false));
    }
}
