//! What the L2 models can see. [`View`] follows the `VisibleBook` semantics of SPEC 5.3.1 (built only from emitted
//! L2 rows: `clear` empties, depth / snapshot rows set the level, 0 removes; trades change nothing) with `f64`
//! quantities. [`HbtDepth`] is a port of hftbacktest's `HashMapMarketDepth` L2 bookkeeping at 5f3ec40 (MIT): the
//! level map, the tracked best ticks (including its uncrossing rule and lot rounding) and `clear_depth`, used by
//! faithful hbt models for arrival quantity, `prev_qty` and the per-row cross check.

use std::collections::BTreeMap;

use qt_types::{BookSide, Px};

/// Visible L2 book with `f64` quantities.
#[derive(Clone, Debug, Default)]
pub struct View {
    bids: BTreeMap<Px, f64>,
    asks: BTreeMap<Px, f64>,
}

impl View {
    /// Empty view.
    pub fn new() -> Self {
        Self::default()
    }

    fn side(&self, side: BookSide) -> &BTreeMap<Px, f64> {
        match side {
            BookSide::Bid => &self.bids,
            BookSide::Ask => &self.asks,
        }
    }

    /// Sets a level (0 or less removes it); returns the previous quantity.
    pub fn set(&mut self, side: BookSide, px: Px, qty: f64) -> f64 {
        let m = match side {
            BookSide::Bid => &mut self.bids,
            BookSide::Ask => &mut self.asks,
        };
        let prev = if qty > 0.0 { m.insert(px, qty) } else { m.remove(&px) };
        prev.unwrap_or(0.0)
    }

    /// Clears one side up to a price (bids at or above, asks at or below) or everything.
    pub fn clear(&mut self, side: Option<BookSide>, upto: Option<Px>) {
        match (side, upto) {
            (None, _) => {
                self.bids.clear();
                self.asks.clear();
            }
            (Some(BookSide::Bid), None) => self.bids.clear(),
            (Some(BookSide::Ask), None) => self.asks.clear(),
            (Some(BookSide::Bid), Some(p)) => self.bids.retain(|k, _| *k < p),
            (Some(BookSide::Ask), Some(p)) => self.asks.retain(|k, _| *k > p),
        }
    }

    /// Visible quantity at a price (0 if absent).
    pub fn qty_at(&self, side: BookSide, px: Px) -> f64 {
        self.side(side).get(&px).copied().unwrap_or(0.0)
    }

    /// The price is in the view.
    pub fn in_view(&self, side: BookSide, px: Px) -> bool {
        self.side(side).contains_key(&px)
    }

    /// Best price of a side.
    pub fn best(&self, side: BookSide) -> Option<Px> {
        match side {
            BookSide::Bid => self.bids.keys().next_back().copied(),
            BookSide::Ask => self.asks.keys().next().copied(),
        }
    }

    /// Visible opposite quantity at prices at or through a resting price on `side` (taker size).
    pub fn opposite_through(&self, side: BookSide, px: Px) -> f64 {
        match side {
            BookSide::Bid => self.asks.range(..=px).map(|(_, q)| *q).sum(),
            BookSide::Ask => self.bids.range(px..).map(|(_, q)| *q).sum(),
        }
    }

    /// The visible opposite best is at or through a resting price on `side`.
    pub fn crosses(&self, side: BookSide, px: Px) -> bool {
        match side {
            BookSide::Bid => self.best(BookSide::Ask).is_some_and(|a| a <= px),
            BookSide::Ask => self.best(BookSide::Bid).is_some_and(|b| b >= px),
        }
    }
}

/// hbt `INVALID_MIN`.
pub const INVALID_MIN: i64 = i64::MIN;
/// hbt `INVALID_MAX`.
pub const INVALID_MAX: i64 = i64::MAX;

/// Port of hftbacktest `HashMapMarketDepth` (L2 part) on integer ticks.
#[derive(Clone, Debug)]
pub struct HbtDepth {
    tick: Px,
    lot: f64,
    bid: BTreeMap<i64, f64>,
    ask: BTreeMap<i64, f64>,
    /// Best bid tick (`INVALID_MIN` when empty).
    pub best_bid_tick: i64,
    /// Best ask tick (`INVALID_MAX` when empty).
    pub best_ask_tick: i64,
    low_bid_tick: i64,
    high_ask_tick: i64,
}

/// Highest tick in `[end, start)` with a positive quantity (hbt `depth_below`, which steps one tick at a time from
/// `start - 1` down to `end`); an ordered map makes it independent of the price gap (review M7: a defaulted tick of
/// one price unit made each best-level removal walk ~1e8 keys).
fn depth_below(depth: &BTreeMap<i64, f64>, start: i64, end: i64) -> i64 {
    if end >= start {
        return INVALID_MIN;
    }
    depth
        .range(end..start)
        .rev()
        .find(|(_, q)| **q > 0.0)
        .map_or(INVALID_MIN, |(t, _)| *t)
}

/// Lowest tick in `(start, end]` with a positive quantity (hbt `depth_above`).
fn depth_above(depth: &BTreeMap<i64, f64>, start: i64, end: i64) -> i64 {
    if start >= end || start == INVALID_MAX {
        return INVALID_MAX;
    }
    depth
        .range((std::ops::Bound::Excluded(start), std::ops::Bound::Included(end)))
        .find(|(_, q)| **q > 0.0)
        .map_or(INVALID_MAX, |(t, _)| *t)
}

/// Result of one depth update: `(prev_qty, prev_best_tick, new_best_tick)` of the updated side.
pub type DepthUpdate = (f64, i64, i64);

impl HbtDepth {
    /// Empty depth with a tick size (price units) and a lot size (size units).
    pub fn new(tick: Px, lot: f64) -> Self {
        Self {
            tick: tick.max(1),
            lot,
            bid: BTreeMap::new(),
            ask: BTreeMap::new(),
            best_bid_tick: INVALID_MIN,
            best_ask_tick: INVALID_MAX,
            low_bid_tick: INVALID_MAX,
            high_ask_tick: INVALID_MIN,
        }
    }

    /// Price -> tick (`(px / tick).round()`).
    pub fn tick_of(&self, px: Px) -> i64 {
        let q = px.div_euclid(self.tick);
        let r = px.rem_euclid(self.tick);
        if 2 * r >= self.tick { q + 1 } else { q }
    }

    /// Quantity at a tick (0 if absent).
    pub fn qty_at_tick(&self, side: BookSide, t: i64) -> f64 {
        match side {
            BookSide::Bid => *self.bid.get(&t).unwrap_or(&0.0),
            BookSide::Ask => *self.ask.get(&t).unwrap_or(&0.0),
        }
    }

    /// Best tick of a side.
    pub fn best_tick(&self, side: BookSide) -> i64 {
        match side {
            BookSide::Bid => self.best_bid_tick,
            BookSide::Ask => self.best_ask_tick,
        }
    }

    /// `update_bid_depth` / `update_ask_depth`.
    pub fn update(&mut self, side: BookSide, px: Px, qty: f64) -> DepthUpdate {
        let t = self.tick_of(px);
        let qty_lot = (qty / self.lot).round() as i64;
        match side {
            BookSide::Bid => {
                let prev_best = self.best_bid_tick;
                let prev_qty = if qty_lot > 0 {
                    self.bid.insert(t, qty)
                } else {
                    self.bid.remove(&t)
                }
                .unwrap_or(0.0);
                if qty_lot == 0 {
                    if t == self.best_bid_tick {
                        self.best_bid_tick = depth_below(&self.bid, self.best_bid_tick, self.low_bid_tick);
                        if self.best_bid_tick == INVALID_MIN {
                            self.low_bid_tick = INVALID_MAX;
                        }
                    }
                } else {
                    if t > self.best_bid_tick {
                        self.best_bid_tick = t;
                        if self.best_bid_tick >= self.best_ask_tick {
                            self.best_ask_tick = depth_above(&self.ask, self.best_bid_tick, self.high_ask_tick);
                        }
                    }
                    self.low_bid_tick = self.low_bid_tick.min(t);
                }
                (prev_qty, prev_best, self.best_bid_tick)
            }
            BookSide::Ask => {
                let prev_best = self.best_ask_tick;
                let prev_qty = if qty_lot > 0 {
                    self.ask.insert(t, qty)
                } else {
                    self.ask.remove(&t)
                }
                .unwrap_or(0.0);
                if qty_lot == 0 {
                    if t == self.best_ask_tick {
                        self.best_ask_tick = depth_above(&self.ask, self.best_ask_tick, self.high_ask_tick);
                        if self.best_ask_tick == INVALID_MAX {
                            self.high_ask_tick = INVALID_MIN;
                        }
                    }
                } else {
                    if t < self.best_ask_tick {
                        self.best_ask_tick = t;
                        if self.best_bid_tick >= self.best_ask_tick {
                            self.best_bid_tick = depth_below(&self.bid, self.best_ask_tick, self.low_bid_tick);
                        }
                    }
                    self.high_ask_tick = self.high_ask_tick.max(t);
                }
                (prev_qty, prev_best, self.best_ask_tick)
            }
        }
    }

    /// `clear_depth(side, clear_upto_price)`; `None` side clears both, `None` price clears the whole side.
    pub fn clear(&mut self, side: Option<BookSide>, upto: Option<Px>) {
        match side {
            None => {
                self.bid.clear();
                self.ask.clear();
                self.best_bid_tick = INVALID_MIN;
                self.best_ask_tick = INVALID_MAX;
                self.low_bid_tick = INVALID_MAX;
                self.high_ask_tick = INVALID_MIN;
            }
            Some(BookSide::Bid) => {
                match upto {
                    Some(p) => {
                        let upto = self.tick_of(p);
                        if self.best_bid_tick != INVALID_MIN && upto <= self.best_bid_tick {
                            // every tick in [upto, best]: the levels present (not one key per tick, review M7)
                            let gone: Vec<i64> = self.bid.range(upto..=self.best_bid_tick).map(|(t, _)| *t).collect();
                            for t in gone {
                                self.bid.remove(&t);
                            }
                        }
                        self.best_bid_tick = depth_below(&self.bid, upto - 1, self.low_bid_tick);
                    }
                    None => {
                        self.bid.clear();
                        self.best_bid_tick = INVALID_MIN;
                    }
                }
                if self.best_bid_tick == INVALID_MIN {
                    self.low_bid_tick = INVALID_MAX;
                }
            }
            Some(BookSide::Ask) => {
                match upto {
                    Some(p) => {
                        let upto = self.tick_of(p);
                        if self.best_ask_tick != INVALID_MAX && self.best_ask_tick <= upto {
                            let gone: Vec<i64> = self.ask.range(self.best_ask_tick..=upto).map(|(t, _)| *t).collect();
                            for t in gone {
                                self.ask.remove(&t);
                            }
                        }
                        self.best_ask_tick = depth_above(&self.ask, upto + 1, self.high_ask_tick);
                    }
                    None => {
                        self.ask.clear();
                        self.best_ask_tick = INVALID_MAX;
                    }
                }
                if self.best_ask_tick == INVALID_MAX {
                    self.high_ask_tick = INVALID_MIN;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn view_semantics() {
        let mut v = View::new();
        assert_eq!(v.set(BookSide::Bid, 100, 5.0), 0.0);
        assert_eq!(v.set(BookSide::Bid, 101, 3.0), 0.0);
        assert_eq!(v.best(BookSide::Bid), Some(101));
        assert_eq!(v.set(BookSide::Bid, 101, 0.0), 3.0);
        assert_eq!(v.best(BookSide::Bid), Some(100));
        v.set(BookSide::Ask, 102, 1.0);
        assert!(!v.crosses(BookSide::Bid, 101));
        assert!(v.crosses(BookSide::Bid, 102));
        v.clear(None, None);
        assert_eq!(v.qty_at(BookSide::Bid, 100), 0.0);
    }

    /// Review M7: the mirror's cost does not grow with the price gap between levels (a tick defaulted to one price
    /// unit at `price_exp -9` puts levels 1e7..1e9 units apart; the per-unit walk took seconds per removal).
    #[test]
    fn hbt_depth_cost_independent_of_price_gap() {
        for gap in [1_000_000i64, 50_000_000, 2_500_000_000] {
            let mut d = HbtDepth::new(1, 1.0);
            let lo = 5_000_000_000_000i64;
            d.update(BookSide::Bid, lo, 5.0);
            d.update(BookSide::Bid, lo + gap, 5.0);
            d.update(BookSide::Ask, lo + 3 * gap, 5.0);
            d.update(BookSide::Ask, lo + 2 * gap, 5.0);
            let t = std::time::Instant::now();
            d.update(BookSide::Bid, lo + gap, 0.0);
            d.update(BookSide::Ask, lo + 2 * gap, 0.0);
            assert_eq!(d.best_bid_tick, lo);
            assert_eq!(d.best_ask_tick, lo + 3 * gap);
            d.clear(Some(BookSide::Bid), Some(lo));
            assert_eq!(d.best_bid_tick, INVALID_MIN);
            assert_eq!(d.qty_at_tick(BookSide::Bid, lo), 0.0);
            d.clear(Some(BookSide::Ask), Some(lo + 3 * gap));
            assert_eq!(d.best_ask_tick, INVALID_MAX);
            assert!(
                t.elapsed() < std::time::Duration::from_millis(50),
                "gap {gap}: {:?}",
                t.elapsed()
            );
        }
    }

    #[test]
    fn hbt_depth_uncross_and_sub_lot() {
        let mut d = HbtDepth::new(1, 1.0);
        d.update(BookSide::Ask, 101, 5.0);
        d.update(BookSide::Ask, 103, 5.0);
        assert_eq!(d.best_ask_tick, 101);
        // a bid at 102 crosses: hbt moves the best ask above it, ignoring 101
        d.update(BookSide::Bid, 102, 1.0);
        assert_eq!(d.best_bid_tick, 102);
        assert_eq!(d.best_ask_tick, 103);
        // sub-lot quantity deletes the level, prev_qty of the next row is 0
        let (prev, _, _) = d.update(BookSide::Bid, 100, 0.3);
        assert_eq!(prev, 0.0);
        let (prev, _, _) = d.update(BookSide::Bid, 100, 2.0);
        assert_eq!(prev, 0.0);
        assert_eq!(d.tick_of(149), 149);
        let d2 = HbtDepth::new(100, 1.0);
        assert_eq!(d2.tick_of(149), 1);
        assert_eq!(d2.tick_of(150), 2);
    }
}
