//! Synthetic TAQ Integrated Feed files for tests, fixtures and throughput measurements: a small per-symbol
//! matching model whose executions follow the market's allocation rule, written as CSV lines the reader parses.
//!
//! Parity (NYSE) follows NYSE Rule 7.37(b) in a simplified form: an order with Setter Priority (the first
//! round-lot order that set a new best price on its side; one per price) trades in full first (as amended by
//! SR-NYSE-2020-55), then round lots go to the Participants on the price's allocation wheel from the pointer on
//! (7.37(b)(2) to (4)): the pointer advances after a full round lot, stays with a Participant that received an odd
//! lot, and moves on when a Participant runs out; an odd-lot aggressing order goes to the first Participant from the
//! pointer that has an order able to fill it in full; inside a Participant orders fill by time. Participants are the
//! Book Participant, the DMM and a few Floor brokers, drawn at random per order. Not modelled: market orders,
//! non-displayed interest, MTS and D orders, Floor broker sub-parity, the NBBO (a new best price on this book counts
//! as setting the NBBO). Price-time markets fill strictly by time.
//!
//! The generator also returns, for every execution, the executed order's FIFO rank (orders ahead of it at its price,
//! in time order, when its Order Execution message prints) and its Participant: the ground truth that the replay
//! rank oracle should reproduce from the generated file.
//!
//! Channel sequence numbers skip one number at each new second, as the Source Time Reference message the TAQ
//! product omits would; Symbol Sequence Numbers are contiguous per symbol.

use std::collections::BTreeMap;

use qt_types::Xoshiro256StarStar;

use super::message::{
    AddOrder, DeleteOrder, Head, Message, ModifyOrder, NonDisplayedTrade, OrderExecution, ReplaceOrder, SecurityStatus,
    SymbolIndexMapping, encode,
};
use super::rules::{Allocation, Market};

const NS: u64 = 1_000_000_000;
/// One cent at 10^-9.
const CENT: i64 = 10_000_000;
const ROUND_LOT: u64 = 100;
/// Price bounds in cents ($1 to $1,000).
const MIN_PX: i64 = 100;
const MAX_PX: i64 = 100_000;

/// Who entered an order (NYSE Rule 7.36(a)(5)).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Participant {
    /// The Book Participant (every order not entered by a Floor broker or the DMM).
    Book,
    /// The DMM.
    Dmm,
    /// A Floor broker trading license.
    Floor(u8),
}

/// What to generate.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SynthSpec {
    /// Market: its Market ID goes into the Symbol Index Mapping messages and its allocation rule fills the orders.
    pub market: Market,
    /// Symbols (NYSE symbology).
    pub symbols: Vec<String>,
    /// Order-book events per symbol after the open.
    pub events_per_symbol: usize,
    /// RNG seed.
    pub seed: u64,
    /// Floor broker Participants.
    pub floor_brokers: u8,
}

/// One execution of the generated day.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SynthExec {
    /// Symbol.
    pub symbol: String,
    /// Channel sequence number of the Order Execution message.
    pub seq: u64,
    /// Resting order executed.
    pub order_id: u64,
    /// Its Participant.
    pub participant: Participant,
    /// Shares.
    pub volume: u64,
    /// Orders ahead of it at its price, in time order, when it executed (0 = head of the FIFO queue).
    pub fifo_rank: u32,
}

/// A generated file.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SynthDay {
    /// CSV text (one message per line).
    pub csv: Vec<u8>,
    /// Every execution, in feed order.
    pub executions: Vec<SynthExec>,
    /// Lines written.
    pub messages: u64,
}

/// `(order id, shares, FIFO rank, Participant)` of one fill.
type Fill = (u64, u64, u32, Participant);

#[derive(Clone, Copy, Debug)]
struct Resting {
    id: u64,
    who: Participant,
    size: u64,
}

#[derive(Default)]
struct Level {
    /// Time order.
    orders: Vec<Resting>,
    /// Participants in order of their first order at this price.
    wheel: Vec<Participant>,
    pointer: usize,
    setter: Option<u64>,
}

impl Level {
    fn left(&self, who: Participant) -> u64 {
        self.orders.iter().filter(|o| o.who == who).map(|o| o.size).sum()
    }

    fn largest(&self, who: Participant) -> u64 {
        self.orders
            .iter()
            .filter(|o| o.who == who)
            .map(|o| o.size)
            .max()
            .unwrap_or(0)
    }

    fn push(&mut self, o: Resting) {
        if !self.wheel.contains(&o.who) {
            self.wheel.push(o.who);
        }
        self.orders.push(o);
    }

    /// Fills up to `q` shares from `who`'s orders by time; an order's index when it fills is its FIFO rank.
    fn take_from(&mut self, who: Participant, mut q: u64, fills: &mut Vec<Fill>) -> u64 {
        let mut got = 0;
        let mut i = 0;
        while q > 0 && i < self.orders.len() {
            if self.orders[i].who != who {
                i += 1;
                continue;
            }
            let n = q.min(self.orders[i].size);
            fills.push((self.orders[i].id, n, i as u32, who));
            self.orders[i].size -= n;
            q -= n;
            got += n;
            if self.orders[i].size == 0 {
                self.orders.remove(i);
            } else {
                i += 1;
            }
        }
        got
    }

    /// Participants without orders leave the wheel; the pointer keeps naming its Participant, or the next one in
    /// wheel order when its Participant left. The setter loses Setter Priority when it is gone.
    fn prune(&mut self) {
        let alive: Vec<bool> = self
            .wheel
            .iter()
            .map(|w| self.orders.iter().any(|o| o.who == *w))
            .collect();
        let n = self.wheel.len();
        let target = (0..n)
            .map(|k| (self.pointer % n.max(1) + k) % n.max(1))
            .find(|&i| alive.get(i).copied().unwrap_or(false))
            .map(|i| self.wheel[i]);
        let mut k = 0;
        self.wheel.retain(|_| {
            k += 1;
            alive[k - 1]
        });
        self.pointer = target
            .and_then(|t| self.wheel.iter().position(|w| *w == t))
            .unwrap_or(0);
        if let Some(s) = self.setter
            && !self.orders.iter().any(|o| o.id == s)
        {
            self.setter = None;
        }
    }

    /// Allocates an aggressing order of `q` shares under NYSE parity (module docs).
    fn allocate_parity(&mut self, q: u64) -> Vec<Fill> {
        let mut fills = Vec::new();
        let odd_aggressor = q < ROUND_LOT;
        let mut q = q;
        if let Some(s) = self.setter
            && let Some(i) = self.orders.iter().position(|o| o.id == s)
        {
            let n = q.min(self.orders[i].size);
            fills.push((s, n, i as u32, self.orders[i].who));
            self.orders[i].size -= n;
            q -= n;
            if self.orders[i].size == 0 {
                self.orders.remove(i);
            }
            self.prune();
        }
        if q > 0 && odd_aggressor && !self.wheel.is_empty() {
            let n = self.wheel.len();
            if let Some(p) = (0..n)
                .map(|k| (self.pointer + k) % n)
                .find(|&p| self.largest(self.wheel[p]) >= q)
            {
                self.pointer = p;
                q -= self.take_from(self.wheel[p], q, &mut fills);
                self.prune();
            }
        }
        while q > 0 && !self.wheel.is_empty() {
            self.pointer %= self.wheel.len();
            let who = self.wheel[self.pointer];
            let got = self.take_from(who, q.min(ROUND_LOT), &mut fills);
            q -= got;
            if self.left(who) == 0 {
                self.prune();
            } else if got == ROUND_LOT {
                self.pointer = (self.pointer + 1) % self.wheel.len();
            }
            // Otherwise `who` got an odd lot that ended the order: the pointer stays (7.37(b)(4)(B)).
        }
        fills
    }

    fn allocate_fifo(&mut self, mut q: u64) -> Vec<Fill> {
        let mut fills = Vec::new();
        while q > 0 && !self.orders.is_empty() {
            let n = q.min(self.orders[0].size);
            fills.push((self.orders[0].id, n, 0, self.orders[0].who));
            self.orders[0].size -= n;
            q -= n;
            if self.orders[0].size == 0 {
                self.orders.remove(0);
            }
        }
        self.prune();
        fills
    }
}

#[derive(Default)]
struct Book {
    /// Price in cents -> level.
    bids: BTreeMap<i64, Level>,
    asks: BTreeMap<i64, Level>,
    /// Order id -> (bid, price in cents).
    index: BTreeMap<u64, (bool, i64)>,
    symbol_seq: u64,
}

impl Book {
    fn best(&self, bid: bool) -> Option<i64> {
        if bid {
            self.bids.keys().next_back().copied()
        } else {
            self.asks.keys().next().copied()
        }
    }

    fn side(&mut self, bid: bool) -> &mut BTreeMap<i64, Level> {
        if bid { &mut self.bids } else { &mut self.asks }
    }

    fn add(&mut self, id: u64, bid: bool, px: i64, size: u64, who: Participant) {
        let new_best = self.best(bid).is_none_or(|b| if bid { px > b } else { px < b });
        let level = self.side(bid).entry(px).or_default();
        level.push(Resting { id, who, size });
        if new_best && size >= ROUND_LOT && level.setter.is_none() {
            level.setter = Some(id);
        }
        self.index.insert(id, (bid, px));
    }

    fn remove(&mut self, id: u64) -> Option<(bool, i64, Resting)> {
        let (bid, px) = self.index.remove(&id)?;
        let side = self.side(bid);
        let level = side.get_mut(&px)?;
        let i = level.orders.iter().position(|o| o.id == id)?;
        let o = level.orders.remove(i);
        level.prune();
        if level.orders.is_empty() {
            side.remove(&px);
        }
        Some((bid, px, o))
    }

    /// A live order id chosen by `pick` (an index into the live orders).
    fn pick(&self, k: u64) -> Option<u64> {
        self.index.keys().nth(k as usize).copied()
    }
}

struct Gen<'a> {
    spec: &'a SynthSpec,
    rng: Xoshiro256StarStar,
    out: Vec<u8>,
    seq: u64,
    tod: u64,
    second: u64,
    next_id: u64,
    next_trade: u64,
    messages: u64,
    executions: Vec<SynthExec>,
}

impl Gen<'_> {
    fn below(&mut self, n: u64) -> u64 {
        self.rng.range_inclusive(0, n.max(1) - 1)
    }

    fn chance(&mut self, pct: u64) -> bool {
        self.below(100) < pct
    }

    /// Advances the clock by 1 us to 20 ms and the channel sequence (one extra number per new second).
    fn tick(&mut self) {
        self.tod += 1_000 + self.below(20_000_000);
        if self.tod / NS != self.second {
            self.second = self.tod / NS;
            self.seq += 1; // the omitted Source Time Reference message
        }
        self.seq += 1;
    }

    fn emit(&mut self, m: &Message<'_>) {
        encode(m, &mut self.out);
        self.messages += 1;
    }

    fn head<'s>(&mut self, sym: &'s str, book: &mut Book) -> Head<'s> {
        self.tick();
        book.symbol_seq += 1;
        Head {
            seq: self.seq,
            time_ns: self.tod,
            symbol: sym.as_bytes(),
            symbol_seq: book.symbol_seq,
        }
    }

    fn participant(&mut self) -> Participant {
        let r = self.below(100);
        if r < 60 || (self.spec.floor_brokers == 0 && r >= 75) {
            Participant::Book
        } else if r < 75 {
            Participant::Dmm
        } else {
            Participant::Floor(self.below(u64::from(self.spec.floor_brokers)) as u8)
        }
    }

    fn size(&mut self) -> u64 {
        if self.chance(15) {
            1 + self.below(99)
        } else {
            ROUND_LOT * (1 + self.below(5))
        }
    }

    fn side_code(bid: bool) -> u8 {
        if bid { b'B' } else { b'S' }
    }

    fn event(&mut self, sym: &str, book: &mut Book) {
        let r = self.below(100);
        let (bb, ba) = (book.best(true).unwrap_or(2_499), book.best(false).unwrap_or(2_501));
        if r < 45 || book.index.len() < 6 {
            // A passive order within 5 ticks of the touch that never crosses: bids below the best ask, asks above
            // the best bid; one tick inside the spread when it is wider than a tick.
            let bid = self.chance(50);
            let k = self.below(5) as i64;
            let px = if bid {
                (bb + 1 - k).min(ba - 1).max(MIN_PX)
            } else {
                (ba - 1 + k).max(bb + 1).min(MAX_PX)
            };
            if (bid && px >= ba) || (!bid && px <= bb) {
                return;
            }
            let (id, size, who) = (self.next_id, self.size(), self.participant());
            self.next_id += 1;
            book.add(id, bid, px, size, who);
            let head = self.head(sym, book);
            self.emit(&Message::AddOrder(AddOrder {
                head,
                order_id: id,
                price: px * CENT,
                volume: size,
                side: Self::side_code(bid),
                firm_id: b"",
            }));
        } else if r < 65 {
            let k = self.below(book.index.len() as u64);
            if let Some(id) = book.pick(k)
                && book.remove(id).is_some()
            {
                let head = self.head(sym, book);
                self.emit(&Message::DeleteOrder(DeleteOrder { head, order_id: id }));
            }
        } else if r < 72 {
            // A size decrease at the same price keeps the order's place (Rule 7.36(f)(3)).
            let k = self.below(book.index.len() as u64);
            let Some(id) = book.pick(k) else {
                return;
            };
            let Some(&(bid, px)) = book.index.get(&id) else {
                return;
            };
            let Some(o) = book
                .side(bid)
                .get_mut(&px)
                .and_then(|l| l.orders.iter_mut().find(|o| o.id == id))
            else {
                return;
            };
            if o.size < 2 {
                return;
            }
            o.size -= 1 + (o.size - 1) / 2;
            let size = o.size;
            let head = self.head(sym, book);
            self.emit(&Message::ModifyOrder(ModifyOrder {
                head,
                order_id: id,
                price: px * CENT,
                volume: size,
                position_change: 0,
                side: Self::side_code(bid),
            }));
        } else if r < 77 {
            // Cancel/replace to a new id one tick away from the touch: the back of the new price's queue.
            let k = self.below(book.index.len() as u64);
            let Some(id) = book.pick(k) else {
                return;
            };
            let Some((bid, px, o)) = book.remove(id) else {
                return;
            };
            let (bb, ba) = (book.best(true).unwrap_or(2_499), book.best(false).unwrap_or(2_501));
            let new_px = if bid {
                (px - 1).min(ba - 1).max(MIN_PX)
            } else {
                (px + 1).max(bb + 1).min(MAX_PX)
            };
            let new_id = self.next_id;
            self.next_id += 1;
            book.add(new_id, bid, new_px, o.size, o.who);
            let head = self.head(sym, book);
            self.emit(&Message::ReplaceOrder(ReplaceOrder {
                head,
                order_id: id,
                new_order_id: new_id,
                price: new_px * CENT,
                volume: o.size,
                side: Self::side_code(bid),
            }));
        } else if r < 80 {
            let px = (bb + ba) * CENT / 2;
            let (trade, volume) = (self.next_trade, ROUND_LOT * (1 + self.below(3)));
            self.next_trade += 1;
            let head = self.head(sym, book);
            self.emit(&Message::NonDisplayedTrade(NonDisplayedTrade {
                head,
                trade_id: trade,
                price: px,
                volume,
                printable: true,
                conditions: [b'@', 0, 0, 0],
            }));
        } else {
            self.aggress(sym, book);
        }
    }

    /// An aggressing order of 1 to 800 shares against the best opposite price.
    fn aggress(&mut self, sym: &str, book: &mut Book) {
        let resting_bid = self.chance(50);
        let q = if self.chance(10) {
            1 + self.below(99)
        } else {
            ROUND_LOT * (1 + self.below(8))
        };
        let parity = self.spec.market.allocation() == Allocation::Parity;
        let Some(px) = book.best(resting_bid) else {
            return;
        };
        let Some(level) = book.side(resting_bid).get_mut(&px) else {
            return;
        };
        let fills = if parity {
            level.allocate_parity(q)
        } else {
            level.allocate_fifo(q)
        };
        let live: Vec<u64> = level.orders.iter().map(|o| o.id).collect();
        if live.is_empty() {
            book.side(resting_bid).remove(&px);
        }
        for (id, n, rank, who) in fills {
            if !live.contains(&id) {
                book.index.remove(&id);
            }
            let trade = self.next_trade;
            self.next_trade += 1;
            let head = self.head(sym, book);
            let seq = head.seq;
            self.emit(&Message::OrderExecution(OrderExecution {
                head,
                order_id: id,
                trade_id: trade,
                price: px * CENT,
                volume: n,
                printable: true,
                conditions: [b'@', 0, 0, if n < ROUND_LOT { b'I' } else { 0 }],
            }));
            self.executions.push(SynthExec {
                symbol: sym.to_owned(),
                seq,
                order_id: id,
                participant: who,
                volume: n,
                fifo_rank: rank,
            });
        }
    }
}

/// Generates one channel file of one day: Symbol Index Mapping messages, the pre-opening and core-session Security
/// Status transitions (07:00 and 09:30:00 local), then `events_per_symbol` order-book events per symbol.
pub fn generate(spec: &SynthSpec) -> SynthDay {
    let mut g = Gen {
        spec,
        rng: Xoshiro256StarStar::new(spec.seed),
        out: Vec::new(),
        seq: 1,
        tod: 7 * 3_600 * NS,
        second: 7 * 3_600,
        next_id: 1_000,
        next_trade: 1,
        messages: 0,
        executions: Vec::new(),
    };
    let mut books: Vec<Book> = spec.symbols.iter().map(|_| Book::default()).collect();
    for sym in &spec.symbols {
        g.seq += 1;
        g.emit(&Message::SymbolIndexMapping(SymbolIndexMapping {
            seq: g.seq,
            symbol: sym.as_bytes(),
            market_id: spec.market.market_id(),
            system_id: 1,
            exchange_code: b'N',
            security_type: b'C',
            lot_size: ROUND_LOT as u32,
            price_resolution: b'0',
            round_lot: b'Y',
            mpv: 1,
            unit_of_trade: 100,
        }));
    }
    for (state, at) in [(b'P', 7 * 3_600 * NS), (b'O', 34_200 * NS)] {
        g.tod = g.tod.max(at);
        for (sym, book) in spec.symbols.iter().zip(&mut books) {
            let head = g.head(sym, book);
            g.emit(&Message::SecurityStatus(SecurityStatus {
                head: Head { time_ns: at, ..head },
                status: state,
                halt_condition: b'~',
                ssr_state: b'~',
                market_state: state,
            }));
        }
    }
    let n = spec.symbols.len();
    for _ in 0..spec.events_per_symbol * n {
        let k = g.below(n as u64) as usize;
        g.event(&spec.symbols[k], &mut books[k]);
    }
    SynthDay {
        csv: g.out,
        executions: g.executions,
        messages: g.messages,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::nyse::message::parse_line;
    use Participant::{Book as B, Dmm, Floor};

    fn spec(market: Market, seed: u64) -> SynthSpec {
        SynthSpec {
            market,
            symbols: vec!["QTA".into(), "QTB A".into()],
            events_per_symbol: 2_000,
            seed,
            floor_brokers: 3,
        }
    }

    #[test]
    fn deterministic_and_parseable() {
        let a = generate(&spec(Market::Nyse, 7));
        assert_eq!(a, generate(&spec(Market::Nyse, 7)));
        assert_ne!(a.csv, generate(&spec(Market::Nyse, 8)).csv);
        let mut lines = 0;
        for l in a.csv.split(|&b| b == b'\n').filter(|l| !l.is_empty()) {
            parse_line(l).unwrap_or_else(|e| panic!("{}: {e}", String::from_utf8_lossy(l)));
            lines += 1;
        }
        assert_eq!(lines, a.messages);
        assert!(a.executions.len() > 200, "{}", a.executions.len());
    }

    #[test]
    fn price_time_fills_the_head_and_parity_does_not() {
        let fifo = generate(&spec(Market::Arca, 3));
        assert!(fifo.executions.iter().all(|e| e.fifo_rank == 0));
        let parity = generate(&spec(Market::Nyse, 3));
        let behind = parity.executions.iter().filter(|e| e.fifo_rank > 0).count();
        assert!(
            behind * 20 > parity.executions.len(),
            "{behind} of {}",
            parity.executions.len()
        );
        assert!(parity.executions.iter().any(|e| matches!(e.participant, Floor(_))));
    }

    fn level(orders: &[(u64, Participant, u64)]) -> Level {
        let mut l = Level::default();
        for &(id, who, size) in orders {
            l.push(Resting { id, who, size });
        }
        l
    }

    fn ids(f: &[Fill]) -> Vec<(u64, u64)> {
        f.iter().map(|x| (x.0, x.1)).collect()
    }

    /// The parity example of former NYSE Rule 72(c), in Pillar terms: the Book Participant (two 100-share orders),
    /// Floor broker A, the DMM and Floor brokers C and D at one price, no setter. Two sell orders of 300 go to the
    /// Book (its first order), A and the DMM, then to C, D and the Book (its second order).
    #[test]
    fn parity_wheel_round_lots() {
        let mut l = level(&[
            (1, B, 100),
            (2, B, 100),
            (3, Floor(0), 100),
            (4, Dmm, 100),
            (5, Floor(2), 100),
            (6, Floor(3), 100),
        ]);
        assert_eq!(ids(&l.allocate_parity(300)), vec![(1, 100), (3, 100), (4, 100)]);
        assert_eq!(ids(&l.allocate_parity(300)), vec![(5, 100), (6, 100), (2, 100)]);
        assert!(l.orders.is_empty() && l.wheel.is_empty());
    }

    /// Setter Priority trades in full first; the pointer stays with a Participant that got an odd lot; an odd-lot
    /// aggressor goes to the first Participant with an order that can fill it in full.
    #[test]
    fn setter_priority_odd_lots_and_pointer() {
        let mut l = level(&[(1, B, 200), (2, Floor(0), 300), (3, Dmm, 300)]);
        l.setter = Some(2);
        let f = l.allocate_parity(450);
        // The setter takes 300 (FIFO rank 1: order 1 is ahead), the Book a round lot, the DMM the odd 50.
        assert_eq!(
            f.iter().map(|x| (x.0, x.1, x.2)).collect::<Vec<_>>(),
            vec![(2, 300, 1), (1, 100, 0), (3, 50, 1)]
        );
        assert_eq!(
            l.wheel[l.pointer], Dmm,
            "the pointer stays with the odd-lot Participant"
        );
        assert_eq!(l.setter, None);
        assert_eq!(
            ids(&l.allocate_parity(60)),
            vec![(3, 60)],
            "the pointer's Participant fills the odd lot"
        );
        // The DMM has 190 left: a round lot, then the Book gets the odd 50 and keeps the pointer.
        assert_eq!(ids(&l.allocate_parity(150)), vec![(3, 100), (1, 50)]);
        assert_eq!(l.wheel[l.pointer], B);
        // An odd-lot aggressor skips a Participant whose orders are all smaller than it.
        let mut l = level(&[(1, B, 30), (2, Dmm, 80)]);
        assert_eq!(ids(&l.allocate_parity(50)), vec![(2, 50)]);
        let mut fifo = level(&[(1, B, 200), (2, Floor(0), 300)]);
        let f = fifo.allocate_fifo(250);
        assert_eq!(
            f.iter().map(|x| (x.0, x.1, x.2)).collect::<Vec<_>>(),
            vec![(1, 200, 0), (2, 50, 0)]
        );
    }

    #[test]
    fn books_track_setter_priority_and_removal() {
        let mut b = Book::default();
        b.add(1, true, 2_500, 100, B);
        b.add(2, true, 2_500, 100, Dmm);
        b.add(3, true, 2_501, 50, Dmm); // new best but an odd lot: no setter
        b.add(4, false, 2_510, 200, Floor(1));
        assert_eq!(b.bids[&2_500].setter, Some(1));
        assert_eq!(b.bids[&2_501].setter, None);
        assert_eq!(b.asks[&2_510].setter, Some(4));
        assert_eq!(b.remove(1).map(|x| (x.0, x.1, x.2.id)), Some((true, 2_500, 1)));
        assert_eq!(b.bids[&2_500].setter, None, "the setter left");
        assert_eq!(b.bids[&2_500].wheel, vec![Dmm]);
        assert!(b.remove(1).is_none());
        b.remove(3);
        assert!(!b.bids.contains_key(&2_501));
        assert_eq!(b.best(true), Some(2_500));
        assert_eq!(b.pick(1), Some(4));
    }
}
