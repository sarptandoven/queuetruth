//! Naive Vec-based reference book for differential tests: the same record semantics as `Book`, written
//! independently with linear scans (O(n) per record) and no shared code paths.

use qt_types::{BookSide, MboEvent, Px, Qty, UNDEF_PRICE, VenueRules};

use crate::{Bbo, Effect, LevelView, RestingOrder};

#[derive(Clone, Debug)]
struct RefLevel {
    price: Px,
    orders: Vec<RestingOrder>,
}

/// O(n) reference book with the same record semantics as `Book`.
#[derive(Clone, Debug)]
pub struct RefBook {
    rules: VenueRules,
    /// Ascending price.
    bids: Vec<RefLevel>,
    /// Ascending price.
    asks: Vec<RefLevel>,
    /// CME: (order id, filled quantity) reported by `F` in the current event and not yet removed (A6b).
    unremoved_fills: Vec<(u64, Qty)>,
}

impl RefBook {
    /// Empty book.
    pub fn new(rules: VenueRules) -> Self {
        Self {
            rules,
            bids: Vec::new(),
            asks: Vec::new(),
            unremoved_fills: Vec::new(),
        }
    }

    fn unremoved(&mut self, order_id: u64) -> Qty {
        let q = self
            .unremoved_fills
            .iter()
            .filter(|f| f.0 == order_id)
            .map(|f| f.1)
            .sum();
        self.unremoved_fills.retain(|f| f.0 != order_id);
        q
    }

    fn levels_of(&self, side: BookSide) -> &Vec<RefLevel> {
        match side {
            BookSide::Bid => &self.bids,
            BookSide::Ask => &self.asks,
        }
    }

    fn levels_of_mut(&mut self, side: BookSide) -> &mut Vec<RefLevel> {
        match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        }
    }

    /// (side, level index, position) of an order.
    fn find(&self, order_id: u64) -> Option<(BookSide, usize, usize)> {
        for side in [BookSide::Bid, BookSide::Ask] {
            for (li, l) in self.levels_of(side).iter().enumerate() {
                if let Some(pos) = l.orders.iter().position(|o| o.order_id == order_id) {
                    return Some((side, li, pos));
                }
            }
        }
        None
    }

    fn take(&mut self, side: BookSide, li: usize, pos: usize) -> (Px, RestingOrder) {
        let levels = self.levels_of_mut(side);
        let price = levels[li].price;
        let o = levels[li].orders.remove(pos);
        if levels[li].orders.is_empty() {
            levels.remove(li);
        }
        (price, o)
    }

    fn append(&mut self, side: BookSide, price: Px, o: RestingOrder) {
        let levels = self.levels_of_mut(side);
        match levels.iter().position(|l| l.price >= price) {
            Some(i) if levels[i].price == price => levels[i].orders.push(o),
            Some(i) => levels.insert(i, RefLevel { price, orders: vec![o] }),
            None => levels.push(RefLevel { price, orders: vec![o] }),
        }
    }

    fn side_of(b: u8) -> Option<BookSide> {
        match b {
            b'B' => Some(BookSide::Bid),
            b'A' => Some(BookSide::Ask),
            _ => None,
        }
    }

    fn do_add(&mut self, e: &MboEvent) -> Effect {
        let Some(side) = Self::side_of(e.side) else {
            return Effect::None;
        };
        if e.price == UNDEF_PRICE || e.size == 0 {
            return Effect::None;
        }
        if let Some((s, li, pos)) = self.find(e.order_id) {
            self.take(s, li, pos);
        }
        self.append(
            side,
            e.price,
            RestingOrder {
                order_id: e.order_id,
                size: e.size,
                ts_recv: e.ts_recv,
                sequence: e.sequence,
            },
        );
        Effect::Add {
            order_id: e.order_id,
            side,
            price: e.price,
            size: e.size,
        }
    }

    /// Applies one record.
    pub fn apply(&mut self, e: &MboEvent) -> Effect {
        let out = self.apply_record(e);
        if e.flags & qt_types::flags::LAST != 0 {
            self.unremoved_fills.clear();
        }
        out
    }

    fn apply_record(&mut self, e: &MboEvent) -> Effect {
        let cme = self.rules.venue == qt_types::Venue::Cme;
        if cme && e.action == b'F' && self.find(e.order_id).is_some() {
            self.unremoved_fills.push((e.order_id, e.size));
        }
        let unremoved = if cme && (e.action == b'C' || e.action == b'M') {
            self.unremoved(e.order_id)
        } else {
            0
        };
        match e.action {
            b'R' => {
                self.bids.clear();
                self.asks.clear();
                self.unremoved_fills.clear();
                Effect::Clear
            }
            b'A' => self.do_add(e),
            b'C' => {
                let Some((side, li, pos)) = self.find(e.order_id) else {
                    return Effect::None;
                };
                let resting = self.levels_of(side)[li].orders[pos].size;
                let price = self.levels_of(side)[li].price;
                if e.size >= resting {
                    self.take(side, li, pos);
                    Effect::Remove {
                        order_id: e.order_id,
                        side,
                        price,
                        size: resting,
                    }
                } else {
                    self.levels_of_mut(side)[li].orders[pos].size = resting - e.size;
                    Effect::Reduce {
                        order_id: e.order_id,
                        side,
                        price,
                        by: e.size,
                    }
                }
            }
            b'M' => {
                if !self.rules.modify_records_present {
                    return Effect::None;
                }
                let Some((side, li, pos)) = self.find(e.order_id) else {
                    return self.do_add(e);
                };
                let old = self.levels_of(side)[li].orders[pos].clone();
                let old_price = self.levels_of(side)[li].price;
                if e.size == 0 {
                    self.take(side, li, pos);
                    return Effect::Remove {
                        order_id: e.order_id,
                        side,
                        price: old_price,
                        size: old.size,
                    };
                }
                let keeps = match self.rules.modify_priority {
                    qt_types::PriorityRule::KeepOnSizeDecrease if cme => {
                        let left = old.size.saturating_sub(unremoved);
                        e.price == old_price && (e.size < left || (e.size == left && unremoved > 0))
                    }
                    qt_types::PriorityRule::KeepOnSizeDecrease => e.price == old_price && e.size <= old.size,
                    qt_types::PriorityRule::AlwaysLose => false,
                };
                if keeps {
                    if e.size == old.size {
                        return Effect::None;
                    }
                    self.levels_of_mut(side)[li].orders[pos].size = e.size;
                    return Effect::Reduce {
                        order_id: e.order_id,
                        side,
                        price: old_price,
                        by: old.size - e.size,
                    };
                }
                self.take(side, li, pos);
                self.append(
                    side,
                    e.price,
                    RestingOrder {
                        order_id: e.order_id,
                        size: e.size,
                        ts_recv: e.ts_recv,
                        sequence: e.sequence,
                    },
                );
                Effect::Replace {
                    order_id: e.order_id,
                    side,
                    old_price,
                    old_size: old.size,
                    new_price: e.price,
                    new_size: e.size,
                }
            }
            _ => Effect::None,
        }
    }

    fn view(l: &RefLevel) -> LevelView {
        LevelView {
            price: l.price,
            total: l.orders.iter().map(|o| o.size).sum(),
            count: l.orders.len() as u32,
        }
    }

    /// Best bid and ask.
    pub fn bbo(&self) -> Bbo {
        Bbo {
            bid: self.bids.last().map(Self::view),
            ask: self.asks.first().map(Self::view),
        }
    }

    /// Level at a price.
    pub fn level(&self, side: BookSide, price: Px) -> Option<LevelView> {
        self.levels_of(side).iter().find(|l| l.price == price).map(Self::view)
    }

    /// Levels of a side, best first.
    pub fn levels(&self, side: BookSide) -> impl Iterator<Item = LevelView> + '_ {
        let mut v: Vec<LevelView> = self.levels_of(side).iter().map(Self::view).collect();
        if side == BookSide::Bid {
            v.reverse();
        }
        v.into_iter()
    }

    /// Orders resting at a price in FIFO order.
    pub fn orders_at(&self, side: BookSide, price: Px) -> impl Iterator<Item = &RestingOrder> + '_ {
        self.levels_of(side)
            .iter()
            .filter(move |l| l.price == price)
            .flat_map(|l| l.orders.iter())
    }

    /// Displayed quantity and order count ahead of an order.
    pub fn queue_ahead_of(&self, order_id: u64) -> Option<(Qty, u32)> {
        let (side, li, pos) = self.find(order_id)?;
        let orders = &self.levels_of(side)[li].orders;
        Some((orders[..pos].iter().map(|o| o.size).sum(), pos as u32))
    }
}
