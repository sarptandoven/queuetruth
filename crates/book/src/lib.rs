//! Exact L3 FIFO order book (SPEC 3.5, 4.1 `qt_book`, 5.1.1): arena + BTreeMap levels, record effects, invariant
//! counters, rank queries, and `reference::RefBook`, a naive Vec-based book for differential tests.
//!
//! Record semantics (et `book.rs`): only `A`, `C`, `M` and `R` change the book. `A` with an undefined price or a zero
//! size is ignored; a duplicate id replaces the resting order (removed, then appended) and counts `duplicate_adds`
//! unless the venue flags snapshot records and the add carries `SNAPSHOT`. `C` removes `size`: less than resting is a
//! priority-keeping `Reduce`, otherwise a `Remove` (`cancels_exceeding_resting` when larger). `M` is ignored and
//! counted on venues without modifies; an unknown id is counted and treated as `A`; size 0 removes; the venue's
//! priority rule decides between `Reduce` (or nothing when unchanged) and `Replace` (tail of the new level); on CME
//! (audit ruling A6, oracle ruling A6b) an `M` keeps priority only at the same price with a size below the order's
//! post-fill remaining quantity (resting size minus the `F` quantities of the same event not yet removed), or equal
//! to it after such a fill; so an unchanged `M`, or one above the post-fill remainder, is a `Replace`. `F`
//! changes nothing in the book but feeds the fill counters (and the CME post-fill remainder). Books are consistent only at event end (`note_event_end`).

#![forbid(unsafe_code)]

mod book;
pub mod reference;

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

use qt_types::{BookSide, Px, Qty, Ts};

pub use book::Book;

/// Aggregate of one price level.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LevelView {
    /// Price.
    pub price: Px,
    /// Total displayed size.
    pub total: Qty,
    /// Order count.
    pub count: u32,
}

/// Best bid and ask.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Bbo {
    /// Best bid level.
    pub bid: Option<LevelView>,
    /// Best ask level.
    pub ask: Option<LevelView>,
}

/// One resting order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RestingOrder {
    /// Order id.
    pub order_id: u64,
    /// Remaining size.
    pub size: Qty,
    /// Capture time of the record that placed it at its queue position.
    pub ts_recv: Ts,
    /// Sequence of that record.
    pub sequence: u64,
}

/// What one record did to the book (et semantics).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Effect {
    /// T, F, N, or C/M for an unknown order (counted).
    None,
    /// Order added at the tail of its level.
    Add {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Price.
        price: Px,
        /// Size.
        size: Qty,
    },
    /// Order removed.
    Remove {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Price.
        price: Px,
        /// Size removed (the resting size).
        size: Qty,
    },
    /// Size reduced, priority kept.
    Reduce {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Price.
        price: Px,
        /// Reduction.
        by: Qty,
    },
    /// Modified with loss of priority (moved to the tail of the new level).
    Replace {
        /// Order id.
        order_id: u64,
        /// Side.
        side: BookSide,
        /// Old price.
        old_price: Px,
        /// Old size.
        old_size: Qty,
        /// New price.
        new_price: Px,
        /// New size.
        new_size: Qty,
    },
    /// Book cleared.
    Clear,
}

/// Book invariant counters.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct Invariants {
    /// Cancels exceeding the resting size.
    pub cancels_exceeding_resting: u64,
    /// Fills exceeding the resting size.
    pub fills_exceeding_resting: u64,
    /// Fills of unknown orders.
    pub fills_unknown_order: u64,
    /// C/M references to unknown orders.
    pub unknown_order_refs: u64,
    /// Duplicate adds outside flagged snapshots.
    pub duplicate_adds: u64,
    /// M records on venues without modifies.
    pub unexpected_modifies: u64,
    /// Both sides present and bid >= ask at event end.
    pub crossed_books_after_last: u64,
    /// bid > ask at event end.
    pub negative_spreads: u64,
}

impl Invariants {
    /// Adds another set of counters (partition merge).
    pub fn add(&mut self, o: &Self) {
        self.cancels_exceeding_resting += o.cancels_exceeding_resting;
        self.fills_exceeding_resting += o.fills_exceeding_resting;
        self.fills_unknown_order += o.fills_unknown_order;
        self.unknown_order_refs += o.unknown_order_refs;
        self.duplicate_adds += o.duplicate_adds;
        self.unexpected_modifies += o.unexpected_modifies;
        self.crossed_books_after_last += o.crossed_books_after_last;
        self.negative_spreads += o.negative_spreads;
    }

    /// Non-zero counters as (name, count), in declaration order.
    pub fn nonzero(&self) -> Vec<(&'static str, u64)> {
        [
            ("cancels_exceeding_resting", self.cancels_exceeding_resting),
            ("fills_exceeding_resting", self.fills_exceeding_resting),
            ("fills_unknown_order", self.fills_unknown_order),
            ("unknown_order_refs", self.unknown_order_refs),
            ("duplicate_adds", self.duplicate_adds),
            ("unexpected_modifies", self.unexpected_modifies),
            ("crossed_books_after_last", self.crossed_books_after_last),
            ("negative_spreads", self.negative_spreads),
        ]
        .into_iter()
        .filter(|(_, n)| *n > 0)
        .collect()
    }
}

/// Queue rank of an order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rank {
    /// Side.
    pub side: BookSide,
    /// Price.
    pub price: Px,
    /// Orders ahead not in `consumed`.
    pub rank: u32,
    /// Better levels on its side still holding an unconsumed order.
    pub level_depth: u32,
    /// Best price on its side.
    pub best_px: Px,
}

/// Fixed multiplicative hasher for integer keys (order ids); never used for output ordering.
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
            self.write_u64(u64::from(b));
        }
    }
    #[inline]
    fn write_u64(&mut self, i: u64) {
        self.0 = (self.0.rotate_left(5) ^ i).wrapping_mul(0x517c_c1b7_2722_0a95);
    }
    #[inline]
    fn write_u32(&mut self, i: u32) {
        self.write_u64(u64::from(i));
    }
    #[inline]
    fn write_usize(&mut self, i: usize) {
        self.write_u64(i as u64);
    }
}

/// Hash map keyed by integer ids with `IdHasher` (lookups only; iterate sorted keys for output).
pub type IdMap<K, V> = HashMap<K, V, BuildHasherDefault<IdHasher>>;
/// Hash set of integer ids with `IdHasher`.
pub type IdSet<K> = std::collections::HashSet<K, BuildHasherDefault<IdHasher>>;
