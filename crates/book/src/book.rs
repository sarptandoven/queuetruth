//! The arena-backed FIFO book: two `BTreeMap<Px, Level>` sides, each level a doubly linked list of nodes in one
//! arena (append and removal O(1) at any depth), and an id map with a fixed multiplicative hasher.

use std::collections::BTreeMap;

use qt_types::{Action, BookSide, MboEvent, Px, Qty, UNDEF_PRICE, VenueRules, flags};

use crate::{Bbo, Effect, IdMap, Invariants, LevelView, Rank, RestingOrder};

const NIL: u32 = u32::MAX;

/// Audit ruling A6: on CME an `M` keeps priority only when it strictly decreases the size at the same price; an `M`
/// with unchanged price and size (iceberg refresh, account change) loses priority. Other venues keep their rule
/// (`KeepOnSizeDecrease`: unchanged is a no-op).
pub(crate) fn unchanged_modify_loses_priority(rules: &VenueRules) -> bool {
    rules.venue == qt_types::Venue::Cme
}

/// Oracle ruling A6b (CME): an `M` keeps priority only when, at the same price, its size is below the order's
/// post-fill remaining quantity `old_size - pending_fill` (fills reported by `F` earlier in the same event and not
/// yet removed by a `C`/`M` of the order; the event ends at the `LAST` record), or equal to it when there was such a fill (the book update of the execution itself). A size above
/// the post-fill remaining quantity is a refresh / increase and loses priority.
pub(crate) fn cme_modify_keeps_priority(
    old_px: Px,
    old_size: Qty,
    pending_fill: Qty,
    new_px: Px,
    new_size: Qty,
) -> bool {
    let base = old_size.saturating_sub(pending_fill);
    old_px == new_px && (new_size < base || (new_size == base && pending_fill > 0))
}

#[derive(Clone, Debug)]
struct Node {
    order: RestingOrder,
    prev: u32,
    next: u32,
    side: BookSide,
    price: Px,
}

#[derive(Clone, Copy, Debug)]
struct Level {
    head: u32,
    tail: u32,
    total: Qty,
    count: u32,
}

impl Level {
    const EMPTY: Self = Self {
        head: NIL,
        tail: NIL,
        total: 0,
        count: 0,
    };

    fn view(&self, price: Px) -> LevelView {
        LevelView {
            price,
            total: self.total,
            count: self.count,
        }
    }
}

/// Per-instrument venue-faithful FIFO book.
#[derive(Clone, Debug)]
pub struct Book {
    rules: VenueRules,
    bids: BTreeMap<Px, Level>,
    asks: BTreeMap<Px, Level>,
    nodes: Vec<Node>,
    free: Vec<u32>,
    orders: IdMap<u64, u32>,
    inv: Invariants,
    error: Option<String>,
    /// Cached best prices (the extreme keys of `bids` / `asks`).
    best_bid: Option<Px>,
    best_ask: Option<Px>,
    /// CME: `F` quantities of the current event not yet removed by a `C`/`M` of the order (A6b).
    pending_fills: Vec<(u64, Qty)>,
}

/// FIFO iterator over the orders of one level.
struct LevelOrders<'a> {
    nodes: &'a [Node],
    cur: u32,
}

impl<'a> Iterator for LevelOrders<'a> {
    type Item = &'a RestingOrder;

    #[inline]
    fn next(&mut self) -> Option<&'a RestingOrder> {
        let n = self.nodes.get(self.cur as usize)?;
        self.cur = n.next;
        Some(&n.order)
    }
}

/// Levels of one side, best first.
enum SideLevels<'a> {
    Bids(std::iter::Rev<std::collections::btree_map::Iter<'a, Px, Level>>),
    Asks(std::collections::btree_map::Iter<'a, Px, Level>),
}

impl Iterator for SideLevels<'_> {
    type Item = LevelView;

    fn next(&mut self) -> Option<LevelView> {
        let (p, l) = match self {
            Self::Bids(it) => it.next()?,
            Self::Asks(it) => it.next()?,
        };
        Some(l.view(*p))
    }
}

impl Book {
    /// Empty book with the venue's rules.
    pub fn new(rules: VenueRules) -> Self {
        Self {
            rules,
            bids: BTreeMap::new(),
            asks: BTreeMap::new(),
            nodes: Vec::new(),
            free: Vec::new(),
            orders: IdMap::default(),
            inv: Invariants::default(),
            error: None,
            best_bid: None,
            best_ask: None,
            pending_fills: Vec::new(),
        }
    }

    /// Takes the pending (unremoved) fill quantity of an order (A6b; CME only, else 0).
    #[inline]
    fn take_pending_fill(&mut self, order_id: u64) -> Qty {
        if self.pending_fills.is_empty() {
            return 0;
        }
        match self.pending_fills.iter().position(|p| p.0 == order_id) {
            Some(i) => self.pending_fills.swap_remove(i).1,
            None => 0,
        }
    }

    /// The rules.
    pub fn rules(&self) -> &VenueRules {
        &self.rules
    }

    #[inline]
    fn side(&self, side: BookSide) -> &BTreeMap<Px, Level> {
        match side {
            BookSide::Bid => &self.bids,
            BookSide::Ask => &self.asks,
        }
    }

    #[inline]
    fn side_mut(&mut self, side: BookSide) -> &mut BTreeMap<Px, Level> {
        match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        }
    }

    /// Applies one record; only A, C, M, R change the book.
    pub fn apply(&mut self, e: &MboEvent) -> Effect {
        let effect = self.apply_inner(e);
        if e.flags & flags::LAST != 0 && !self.pending_fills.is_empty() {
            self.pending_fills.clear();
        }
        effect
    }

    #[inline]
    fn apply_inner(&mut self, e: &MboEvent) -> Effect {
        let Some(action) = Action::from_byte(e.action) else {
            return Effect::None;
        };
        match action {
            Action::Clear => {
                self.bids.clear();
                self.asks.clear();
                self.orders.clear();
                self.nodes.clear();
                self.free.clear();
                self.best_bid = None;
                self.best_ask = None;
                self.pending_fills.clear();
                Effect::Clear
            }
            Action::Add => match BookSide::from_byte(e.side) {
                Some(side) => self.add(side, e),
                None => Effect::None,
            },
            Action::Cancel => {
                self.take_pending_fill(e.order_id);
                self.cancel(e.order_id, e.size)
            }
            Action::Modify if !self.rules.modify_records_present => {
                self.inv.unexpected_modifies += 1;
                Effect::None
            }
            Action::Modify => self.modify(e),
            Action::Fill => {
                match self.orders.get(&e.order_id) {
                    None => self.inv.fills_unknown_order += 1,
                    Some(&n) => {
                        if e.size > self.nodes[n as usize].order.size {
                            self.inv.fills_exceeding_resting += 1;
                        }
                        if unchanged_modify_loses_priority(&self.rules) {
                            match self.pending_fills.iter_mut().find(|p| p.0 == e.order_id) {
                                Some(p) => p.1 = p.1.saturating_add(e.size),
                                None => self.pending_fills.push((e.order_id, e.size)),
                            }
                        }
                    }
                }
                Effect::None
            }
            Action::Trade | Action::None => Effect::None,
        }
    }

    fn add(&mut self, side: BookSide, e: &MboEvent) -> Effect {
        if e.price == UNDEF_PRICE || e.size == 0 {
            return Effect::None;
        }
        if self.orders.contains_key(&e.order_id) {
            if !(self.rules.snapshot_records_flagged && e.flags & flags::SNAPSHOT != 0) {
                self.inv.duplicate_adds += 1;
            }
            self.remove_order(e.order_id);
        }
        let order = RestingOrder {
            order_id: e.order_id,
            size: e.size,
            ts_recv: e.ts_recv,
            sequence: e.sequence,
        };
        if !self.push_back(side, e.price, order) {
            return Effect::None;
        }
        Effect::Add {
            order_id: e.order_id,
            side,
            price: e.price,
            size: e.size,
        }
    }

    fn set_error(&mut self, msg: String) {
        if self.error.is_none() {
            self.error = Some(msg);
        }
    }

    /// Appends at the tail of (side, price); false (and the error flag set) when the arena or a total overflows.
    fn push_back(&mut self, side: BookSide, price: Px, order: RestingOrder) -> bool {
        let order_id = order.order_id;
        let size = order.size;
        let total_ok = self
            .side(side)
            .get(&price)
            .is_none_or(|l| l.total.checked_add(size).is_some() && l.count < u32::MAX);
        if !total_ok {
            self.set_error(format!("level total overflow at {side:?}@{price} (order {order_id})"));
            return false;
        }
        let node = Node {
            order,
            prev: NIL,
            next: NIL,
            side,
            price,
        };
        let idx = match self.free.pop() {
            Some(i) => {
                self.nodes[i as usize] = node;
                i
            }
            None => {
                let Ok(i) = u32::try_from(self.nodes.len()) else {
                    self.set_error("order arena exceeds u32 node indices".into());
                    return false;
                };
                if i == NIL {
                    self.set_error("order arena exceeds u32 node indices".into());
                    return false;
                }
                self.nodes.push(node);
                i
            }
        };
        match side {
            BookSide::Bid => {
                if self.best_bid.is_none_or(|b| price > b) {
                    self.best_bid = Some(price);
                }
            }
            BookSide::Ask => {
                if self.best_ask.is_none_or(|a| price < a) {
                    self.best_ask = Some(price);
                }
            }
        }
        let level = self.side_mut(side).entry(price).or_insert(Level::EMPTY);
        let tail = level.tail;
        level.tail = idx;
        if tail == NIL {
            level.head = idx;
        }
        level.total += size;
        level.count += 1;
        if tail != NIL {
            self.nodes[tail as usize].next = idx;
            self.nodes[idx as usize].prev = tail;
        }
        self.orders.insert(order_id, idx);
        true
    }

    /// Removes an order entirely; returns (side, price, size).
    fn remove_order(&mut self, order_id: u64) -> Option<(BookSide, Px, Qty)> {
        let idx = self.orders.remove(&order_id)?;
        let (prev, next, side, price, size) = {
            let n = &self.nodes[idx as usize];
            (n.prev, n.next, n.side, n.price, n.order.size)
        };
        if prev != NIL {
            self.nodes[prev as usize].next = next;
        }
        if next != NIL {
            self.nodes[next as usize].prev = prev;
        }
        let levels = self.side_mut(side);
        if let Some(level) = levels.get_mut(&price) {
            if level.head == idx {
                level.head = next;
            }
            if level.tail == idx {
                level.tail = prev;
            }
            level.total -= size;
            level.count -= 1;
            if level.count == 0 {
                levels.remove(&price);
                match side {
                    BookSide::Bid if self.best_bid == Some(price) => {
                        self.best_bid = self.bids.keys().next_back().copied();
                    }
                    BookSide::Ask if self.best_ask == Some(price) => {
                        self.best_ask = self.asks.keys().next().copied();
                    }
                    _ => {}
                }
            }
        }
        self.free.push(idx);
        Some((side, price, size))
    }

    /// Shrinks a resting order in place (priority kept).
    fn shrink(&mut self, idx: u32, by: Qty) {
        let (side, price) = {
            let n = &mut self.nodes[idx as usize];
            n.order.size -= by;
            (n.side, n.price)
        };
        if let Some(l) = self.side_mut(side).get_mut(&price) {
            l.total -= by;
        }
    }

    fn cancel(&mut self, order_id: u64, size: Qty) -> Effect {
        let Some(&idx) = self.orders.get(&order_id) else {
            self.inv.unknown_order_refs += 1;
            return Effect::None;
        };
        let (resting, side, price) = {
            let n = &self.nodes[idx as usize];
            (n.order.size, n.side, n.price)
        };
        if size < resting {
            self.shrink(idx, size);
            Effect::Reduce {
                order_id,
                side,
                price,
                by: size,
            }
        } else {
            if size > resting {
                self.inv.cancels_exceeding_resting += 1;
            }
            self.remove_order(order_id);
            Effect::Remove {
                order_id,
                side,
                price,
                size: resting,
            }
        }
    }

    fn modify(&mut self, e: &MboEvent) -> Effect {
        let Some(&idx) = self.orders.get(&e.order_id) else {
            self.inv.unknown_order_refs += 1;
            return match BookSide::from_byte(e.side) {
                Some(side) => self.add(side, e),
                None => Effect::None,
            };
        };
        let (old_size, side, old_price) = {
            let n = &self.nodes[idx as usize];
            (n.order.size, n.side, n.price)
        };
        if e.size == 0 {
            self.remove_order(e.order_id);
            return Effect::Remove {
                order_id: e.order_id,
                side,
                price: old_price,
                size: old_size,
            };
        }
        let keeps = if unchanged_modify_loses_priority(&self.rules) {
            let pending = self.take_pending_fill(e.order_id);
            cme_modify_keeps_priority(old_price, old_size, pending, e.price, e.size)
        } else {
            self.rules
                .modify_priority
                .keeps_priority(old_price, old_size, e.price, e.size)
        };
        if keeps {
            if old_size == e.size {
                return Effect::None;
            }
            let by = old_size - e.size;
            self.shrink(idx, by);
            return Effect::Reduce {
                order_id: e.order_id,
                side,
                price: old_price,
                by,
            };
        }
        self.remove_order(e.order_id);
        let order = RestingOrder {
            order_id: e.order_id,
            size: e.size,
            ts_recv: e.ts_recv,
            sequence: e.sequence,
        };
        if !self.push_back(side, e.price, order) {
            return Effect::Remove {
                order_id: e.order_id,
                side,
                price: old_price,
                size: old_size,
            };
        }
        Effect::Replace {
            order_id: e.order_id,
            side,
            old_price,
            old_size,
            new_price: e.price,
            new_size: e.size,
        }
    }

    /// Best level of a side.
    #[inline]
    pub fn best(&self, side: BookSide) -> Option<LevelView> {
        let p = self.best_price(side)?;
        self.side(side).get(&p).map(|l| l.view(p))
    }

    /// Best price of a side.
    #[inline]
    pub fn best_price(&self, side: BookSide) -> Option<Px> {
        match side {
            BookSide::Bid => self.best_bid,
            BookSide::Ask => self.best_ask,
        }
    }

    /// Best bid and ask.
    #[inline]
    pub fn bbo(&self) -> Bbo {
        Bbo {
            bid: self.best(BookSide::Bid),
            ask: self.best(BookSide::Ask),
        }
    }

    /// Level at a price.
    #[inline]
    pub fn level(&self, side: BookSide, price: Px) -> Option<LevelView> {
        self.side(side).get(&price).map(|l| l.view(price))
    }

    /// Total displayed size at a price (0 when empty).
    #[inline]
    pub fn level_total(&self, side: BookSide, price: Px) -> Qty {
        self.side(side).get(&price).map_or(0, |l| l.total)
    }

    /// Levels of a side, best first.
    pub fn levels(&self, side: BookSide) -> impl Iterator<Item = LevelView> + '_ {
        match side {
            BookSide::Bid => SideLevels::Bids(self.bids.iter().rev()),
            BookSide::Ask => SideLevels::Asks(self.asks.iter()),
        }
    }

    /// Top `n` levels of a side, best first.
    pub fn depth(&self, side: BookSide, n: usize) -> Vec<LevelView> {
        self.levels(side).take(n).collect()
    }

    /// Orders resting at a price in FIFO order.
    pub fn orders_at(&self, side: BookSide, price: Px) -> impl Iterator<Item = &RestingOrder> + '_ {
        LevelOrders {
            nodes: &self.nodes,
            cur: self.side(side).get(&price).map_or(NIL, |l| l.head),
        }
    }

    /// Side, price and resting state of an order.
    #[inline]
    pub fn order(&self, order_id: u64) -> Option<(BookSide, Px, &RestingOrder)> {
        let n = &self.nodes[*self.orders.get(&order_id)? as usize];
        Some((n.side, n.price, &n.order))
    }

    /// Displayed quantity and order count ahead of an order at its level.
    pub fn queue_ahead_of(&self, order_id: u64) -> Option<(Qty, u32)> {
        let (side, price, _) = self.order(order_id)?;
        let mut qty: Qty = 0;
        for (count, o) in self.orders_at(side, price).enumerate() {
            if o.order_id == order_id {
                return Some((qty, count as u32));
            }
            qty += o.size;
        }
        None
    }

    /// Rank of an order ignoring orders in `consumed`; optionally collects the ids ahead.
    pub fn order_rank(&self, order_id: u64, consumed: &[u64], ahead: Option<&mut Vec<u64>>) -> Option<Rank> {
        let (side, price, _) = self.order(order_id)?;
        let live = |o: &RestingOrder| !consumed.contains(&o.order_id);
        let mut ahead = ahead;
        if let Some(a) = ahead.as_deref_mut() {
            a.clear();
        }
        let mut rank = 0u32;
        for o in self
            .orders_at(side, price)
            .take_while(|o| o.order_id != order_id)
            .filter(|o| live(o))
        {
            rank += 1;
            if let Some(a) = ahead.as_deref_mut() {
                a.push(o.order_id);
            }
        }
        let mut level_depth = 0u32;
        let mut best_px = price;
        let better: Box<dyn Iterator<Item = &Px>> = match side {
            BookSide::Bid => Box::new(
                self.bids
                    .range((std::ops::Bound::Excluded(price), std::ops::Bound::Unbounded))
                    .rev()
                    .map(|(p, _)| p),
            ),
            BookSide::Ask => Box::new(self.asks.range(..price).map(|(p, _)| p)),
        };
        for px in better {
            if self.orders_at(side, *px).any(live) {
                level_depth += 1;
                if level_depth == 1 {
                    best_px = *px;
                }
            }
        }
        Some(Rank {
            side,
            price,
            rank,
            level_depth,
            best_px,
        })
    }

    /// Event-end checks (crossed / negative-spread counters).
    #[inline]
    pub fn note_event_end(&mut self) {
        if let (Some(b), Some(a)) = (self.best_price(BookSide::Bid), self.best_price(BookSide::Ask)) {
            if b >= a {
                self.inv.crossed_books_after_last += 1;
            }
            if b > a {
                self.inv.negative_spreads += 1;
            }
        }
    }

    /// Invariant counters.
    pub fn invariants(&self) -> &Invariants {
        &self.inv
    }

    /// First internal error (arena or level-total overflow), if any; the record that caused it was dropped.
    pub fn error(&self) -> Option<&str> {
        self.error.as_deref()
    }

    /// O(n) consistency check: level totals/counts, no empty level, locators consistent.
    pub fn audit(&self) -> Result<(), String> {
        let mut seen = 0usize;
        for (side, levels) in [(BookSide::Bid, &self.bids), (BookSide::Ask, &self.asks)] {
            for (price, level) in levels {
                if level.count == 0 {
                    return Err(format!("empty level {side:?}@{price}"));
                }
                let mut count = 0u32;
                let mut sum: u128 = 0;
                let mut prev = NIL;
                let mut cur = level.head;
                while cur != NIL {
                    let Some(n) = self.nodes.get(cur as usize) else {
                        return Err(format!("dangling node {cur} at {side:?}@{price}"));
                    };
                    if n.prev != prev {
                        return Err(format!("broken prev link at node {cur} ({side:?}@{price})"));
                    }
                    if n.side != side || n.price != *price {
                        return Err(format!(
                            "node {cur} claims {:?}@{} but sits at {side:?}@{price}",
                            n.side, n.price
                        ));
                    }
                    if n.order.size == 0 {
                        return Err(format!("zero-size order {}", n.order.order_id));
                    }
                    if self.orders.get(&n.order.order_id) != Some(&cur) {
                        return Err(format!("locator mismatch for order {}", n.order.order_id));
                    }
                    count += 1;
                    if count > level.count {
                        return Err(format!("level list longer than count at {side:?}@{price}"));
                    }
                    sum += u128::from(n.order.size);
                    prev = cur;
                    cur = n.next;
                }
                if prev != level.tail {
                    return Err(format!("tail mismatch at {side:?}@{price}"));
                }
                if count != level.count {
                    return Err(format!(
                        "level count {} != {count} linked at {side:?}@{price}",
                        level.count
                    ));
                }
                if sum != u128::from(level.total) {
                    return Err(format!("level total {} != sum {sum} at {side:?}@{price}", level.total));
                }
                seen += count as usize;
            }
        }
        if self.best_bid != self.bids.keys().next_back().copied() || self.best_ask != self.asks.keys().next().copied() {
            return Err(format!(
                "best price cache {:?}/{:?} differs from the levels",
                self.best_bid, self.best_ask
            ));
        }
        if seen != self.orders.len() {
            return Err(format!("{} locators but {seen} resting orders", self.orders.len()));
        }
        if self.nodes.len() != self.orders.len() + self.free.len() {
            return Err("node arena leak".into());
        }
        Ok(())
    }

    /// Resting orders.
    pub fn order_count(&self) -> usize {
        self.orders.len()
    }
}
