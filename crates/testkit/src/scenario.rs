//! Scenario DSL for hand-built streams: `Scenario::new(venue).event(ts, |e| e.add(id, side, px, qty).cancel(id,
//! qty).modify(id, px, qty).trade(aggressor, px, qty).fill(id, qty).hidden_print(px, qty)).clear(ts) ... .build()`.
//!
//! Each `.event(...)` block becomes one `F_LAST` batch (`LAST` on its last record; `ts_recv = ts_event`). Every
//! record gets the next `sequence` (1-based index in the whole stream, SPEC 6.2), except that record groups standing
//! for one venue message share a sequence on venues that pair fills with removals by sequence
//! (`fill_removal_shares_sequence`: Nasdaq, generic, Coinbase): `fill` (`F` + `C`), `execute` (`T` + `F` + `C`) and
//! `replace` (`C` + `A`). On CME every record has its own sequence and pairing is within the event. The DSL keeps
//! a small order map per instrument so `cancel`, `fill`, `execute`, `modify` and `replace` know the resting side and
//! price; references to unknown ids produce side `N` (and `UNDEF_PRICE` for `C`/`F`), which is how injected
//! unknown-order records are written.

use std::collections::BTreeMap;

use qt_types::{
    Action, BookSide, Instrument, MboEvent, Px, Qty, Side, Ts, UNDEF_PRICE, Venue, VenueRules, flags, rules,
};

use crate::calendar;

type Orders = BTreeMap<u64, (BookSide, Px, Qty)>;

/// Builder of a hand-made MBO stream.
#[derive(Clone, Debug)]
pub struct Scenario {
    venue: Venue,
    rules: VenueRules,
    instrument_id: u32,
    channel_id: u16,
    publisher_id: u16,
    seq: u64,
    records: Vec<MboEvent>,
    books: BTreeMap<u32, Orders>,
}

/// Records of one event under construction.
#[derive(Clone, Debug)]
pub struct EventBuilder {
    ts: Ts,
    instrument_id: u32,
    channel_id: u16,
    publisher_id: u16,
    share_seq: bool,
    seq: u64,
    orders: Orders,
    recs: Vec<MboEvent>,
}

impl EventBuilder {
    fn rec(&mut self, action: Action, side: u8, price: Px, size: Qty, order_id: u64, seq: u64) -> &mut Self {
        self.recs.push(MboEvent {
            ts_event: self.ts,
            ts_recv: self.ts,
            instrument_id: self.instrument_id,
            action: action.as_byte(),
            side,
            price,
            size,
            order_id,
            flags: 0,
            sequence: seq,
            channel_id: self.channel_id,
            publisher_id: self.publisher_id,
        });
        self
    }

    fn next_seq(&mut self) -> u64 {
        self.seq += 1;
        self.seq
    }

    /// Sequence for the next record of a message group (shared on by-sequence venues).
    fn group_seq(&mut self, first: bool) -> u64 {
        if first || !self.share_seq {
            self.next_seq()
        } else {
            self.seq
        }
    }

    fn resting(&self, id: u64) -> (u8, Px) {
        self.orders
            .get(&id)
            .map_or((b'N', UNDEF_PRICE), |o| (o.0.as_byte(), o.1))
    }

    fn reduce(&mut self, id: u64, qty: Qty) {
        if let Some(o) = self.orders.get_mut(&id) {
            if qty >= o.2 {
                self.orders.remove(&id);
            } else {
                o.2 -= qty;
            }
        }
    }

    /// `A`: a resting order.
    pub fn add(&mut self, id: u64, side: BookSide, px: Px, qty: Qty) -> &mut Self {
        self.orders.insert(id, (side, px, qty));
        let s = self.next_seq();
        self.rec(Action::Add, side.as_byte(), px, qty, id, s)
    }

    /// `C` of `qty` (partial or full) at the resting side and price.
    pub fn cancel(&mut self, id: u64, qty: Qty) -> &mut Self {
        let (side, px) = self.resting(id);
        self.reduce(id, qty);
        let s = self.next_seq();
        self.rec(Action::Cancel, side, px, qty, id, s)
    }

    /// `M` to a new price and total size (side kept; size 0 removes).
    pub fn modify(&mut self, id: u64, px: Px, qty: Qty) -> &mut Self {
        let (side, _) = self.resting(id);
        if let Some(o) = self.orders.get_mut(&id) {
            o.1 = px;
            o.2 = qty;
        }
        if qty == 0 {
            self.orders.remove(&id);
        }
        let s = self.next_seq();
        self.rec(Action::Modify, side, px, qty, id, s)
    }

    /// `T` print with the given aggressor side (`Side::None` = no aggressor).
    pub fn trade(&mut self, aggressor: Side, px: Px, qty: Qty) -> &mut Self {
        let s = self.next_seq();
        self.rec(Action::Trade, aggressor.as_byte(), px, qty, 0, s)
    }

    /// `F` + its removing `C` for a resting order (venue-correct pairing).
    pub fn fill(&mut self, id: u64, qty: Qty) -> &mut Self {
        let (side, px) = self.resting(id);
        let s = self.group_seq(true);
        self.rec(Action::Fill, side, px, qty, id, s);
        self.reduce(id, qty);
        let s = self.group_seq(false);
        self.rec(Action::Cancel, side, px, qty, id, s)
    }

    /// `F` alone (no removal; for injected inconsistencies).
    pub fn fill_only(&mut self, id: u64, qty: Qty) -> &mut Self {
        let (side, px) = self.resting(id);
        let s = self.next_seq();
        self.rec(Action::Fill, side, px, qty, id, s)
    }

    /// ITCH-style execution: `T` (aggressor = opposite of the resting side, resting price) + `F` + `C`.
    pub fn execute(&mut self, id: u64, qty: Qty) -> &mut Self {
        let (side, px) = self.resting(id);
        let aggr = Side::from_byte(side).opposite().as_byte();
        let s = self.group_seq(true);
        self.rec(Action::Trade, aggr, px, qty, 0, s);
        let s = self.group_seq(false);
        self.rec(Action::Fill, side, px, qty, id, s);
        self.reduce(id, qty);
        let s = self.group_seq(false);
        self.rec(Action::Cancel, side, px, qty, id, s)
    }

    /// ITCH "executed with price" message: optional `T` at `exec_px` (aggressor = opposite of the resting side), `F`
    /// at `exec_px`, then the removing `C` at the resting price; `printed = false` omits the `T` (non-printable).
    pub fn execute_at(&mut self, id: u64, qty: Qty, exec_px: Px, printed: bool) -> &mut Self {
        let (side, px) = self.resting(id);
        let mut first = true;
        if printed {
            let aggr = Side::from_byte(side).opposite().as_byte();
            let s = self.group_seq(true);
            self.rec(Action::Trade, aggr, exec_px, qty, 0, s);
            first = false;
        }
        let s = self.group_seq(first);
        self.rec(Action::Fill, side, exec_px, qty, id, s);
        self.reduce(id, qty);
        let s = self.group_seq(false);
        self.rec(Action::Cancel, side, px, qty, id, s)
    }

    /// ITCH-style replace: `C` of the old order's remaining size + `A` of a new id at a new price and size.
    pub fn replace(&mut self, old_id: u64, new_id: u64, px: Px, qty: Qty) -> &mut Self {
        let Some(&(side, old_px, old_qty)) = self.orders.get(&old_id) else {
            return self;
        };
        let s = self.group_seq(true);
        self.rec(Action::Cancel, side.as_byte(), old_px, old_qty, old_id, s);
        self.orders.remove(&old_id);
        self.orders.insert(new_id, (side, px, qty));
        let s = self.group_seq(false);
        self.rec(Action::Add, side.as_byte(), px, qty, new_id, s)
    }

    /// Hidden print: `T` with side `N` and no `F`/`C`.
    pub fn hidden_print(&mut self, px: Px, qty: Qty) -> &mut Self {
        self.trade(Side::None, px, qty)
    }

    /// `R` inside the event.
    pub fn clear(&mut self) -> &mut Self {
        self.orders.clear();
        let s = self.next_seq();
        self.rec(Action::Clear, b'N', UNDEF_PRICE, 0, 0, s)
    }

    /// Any record (ts, instrument, channel, publisher and the next sequence are filled in).
    pub fn record(&mut self, action: Action, side: Side, px: Px, qty: Qty, order_id: u64) -> &mut Self {
        let s = self.next_seq();
        self.rec(action, side.as_byte(), px, qty, order_id, s)
    }

    /// ORs flag bits into the last record so far (e.g. `MAYBE_BAD_BOOK`, `SNAPSHOT`).
    pub fn flag(&mut self, bits: u8) -> &mut Self {
        if let Some(r) = self.recs.last_mut() {
            r.flags |= bits;
        }
        self
    }

    /// Skips `n` sequence numbers before the next record (injects a sequence gap).
    pub fn skip_sequence(&mut self, n: u64) -> &mut Self {
        self.seq += n;
        self
    }
}

impl Scenario {
    /// Empty scenario for a venue: instrument 1, channel 0, the venue's publisher id.
    pub fn new(venue: Venue) -> Self {
        Self {
            venue,
            rules: rules(venue),
            instrument_id: 1,
            channel_id: 0,
            publisher_id: calendar::session_rules(venue).publisher_id,
            seq: 0,
            records: Vec::new(),
            books: BTreeMap::new(),
        }
    }

    /// Venue of the scenario.
    pub fn venue(&self) -> Venue {
        self.venue
    }

    /// Switches the instrument used by the following events.
    pub fn instrument(mut self, instrument_id: u32) -> Self {
        self.instrument_id = instrument_id;
        self
    }

    /// Switches the channel id used by the following events.
    pub fn channel(mut self, channel_id: u16) -> Self {
        self.channel_id = channel_id;
        self
    }

    fn builder(&mut self, ts: Ts) -> EventBuilder {
        EventBuilder {
            ts,
            instrument_id: self.instrument_id,
            channel_id: self.channel_id,
            publisher_id: self.publisher_id,
            share_seq: self.rules.fill_removal_shares_sequence,
            seq: self.seq,
            orders: self.books.remove(&self.instrument_id).unwrap_or_default(),
            recs: Vec::new(),
        }
    }

    fn commit(&mut self, mut b: EventBuilder, extra_flags: u8) {
        self.seq = b.seq;
        self.books.insert(self.instrument_id, std::mem::take(&mut b.orders));
        for r in &mut b.recs {
            r.flags |= extra_flags;
        }
        if let Some(last) = b.recs.last_mut() {
            last.flags |= flags::LAST;
        }
        self.records.extend(b.recs);
    }

    /// One `F_LAST` batch at `ts`.
    pub fn event(mut self, ts: Ts, f: impl FnOnce(&mut EventBuilder) -> &mut EventBuilder) -> Self {
        let mut b = self.builder(ts);
        f(&mut b);
        self.commit(b, 0);
        self
    }

    /// A flagged snapshot batch at `ts`: `R` then the orders added by `f`, every record flagged `SNAPSHOT`.
    pub fn snapshot(mut self, ts: Ts, f: impl FnOnce(&mut EventBuilder) -> &mut EventBuilder) -> Self {
        let mut b = self.builder(ts);
        b.clear();
        f(&mut b);
        self.commit(b, flags::SNAPSHOT);
        self
    }

    /// An event holding a single `R`.
    pub fn clear(self, ts: Ts) -> Self {
        self.event(ts, |e| e.clear())
    }

    /// The records built so far.
    pub fn records(&self) -> &[MboEvent] {
        &self.records
    }

    /// One `qt.instruments` row per instrument used (session from its first record, venue conventions of
    /// `calendar`, price_exp -9, lot 1).
    pub fn instrument_rows(&self, tick: Px) -> Vec<Instrument> {
        let mut first: BTreeMap<u32, Ts> = BTreeMap::new();
        for r in &self.records {
            first.entry(r.instrument_id).or_insert(r.ts_event);
        }
        first
            .into_iter()
            .map(|(id, ts)| calendar::instrument_for(self.venue, id, &format!("SCN{id}"), ts, tick, 1))
            .collect()
    }

    /// The finished stream.
    pub fn build(self) -> Vec<MboEvent> {
        self.records
    }
}
