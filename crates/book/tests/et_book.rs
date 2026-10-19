//! O-BOOK-4 (book part): every et `book.rs` unit test, ported to the `qt_book` API and renamed by topic.

use qt_book::{Book, Effect, Invariants, LevelView, reference::RefBook};
use qt_types::{BookSide, MboEvent, PriorityRule, UNDEF_PRICE, Venue, flags, rules};

fn rec(action: u8, side: u8, order_id: u64, price: i64, size: u64) -> MboEvent {
    MboEvent {
        ts_event: 1_000,
        ts_recv: 1_000,
        instrument_id: 1,
        action,
        side,
        price,
        size,
        order_id,
        sequence: 1,
        ..MboEvent::default()
    }
}

fn ids(b: &Book, side: BookSide, px: i64) -> Vec<u64> {
    b.orders_at(side, px).map(|o| o.order_id).collect()
}

fn lv(price: i64, total: u64, count: u32) -> LevelView {
    LevelView { price, total, count }
}

#[test]
fn o_book_4_modify_priority_truth_table() {
    let r = rules(Venue::Cme).modify_priority;
    assert_eq!(r, PriorityRule::KeepOnSizeDecrease);
    assert!(r.keeps_priority(100, 10, 100, 5));
    assert!(r.keeps_priority(100, 10, 100, 10));
    assert!(!r.keeps_priority(100, 10, 100, 11));
    assert!(!r.keeps_priority(100, 10, 101, 5));
    assert!(!rules(Venue::Nasdaq).modify_priority.keeps_priority(100, 10, 100, 5));
}

/// Audit ruling A6: on CME an `M` with unchanged price and size loses priority (iceberg refresh, account change);
/// on generic/coinbase it is a no-op.
#[test]
fn o_book_4_a6_cme_unchanged_modify_loses_priority() {
    for (venue, loses) in [(Venue::Cme, true), (Venue::Generic, false), (Venue::Coinbase, false)] {
        let mut b = Book::new(rules(venue));
        let mut r = RefBook::new(rules(venue));
        for e in [rec(b'A', b'B', 1, 100, 5), rec(b'A', b'B', 2, 100, 7)] {
            b.apply(&e);
            r.apply(&e);
        }
        let m = rec(b'M', b'B', 1, 100, 5);
        let eff = b.apply(&m);
        assert_eq!(r.apply(&m), eff);
        if loses {
            assert_eq!(
                eff,
                Effect::Replace {
                    order_id: 1,
                    side: BookSide::Bid,
                    old_price: 100,
                    old_size: 5,
                    new_price: 100,
                    new_size: 5
                }
            );
            assert_eq!(ids(&b, BookSide::Bid, 100), vec![2, 1]);
        } else {
            assert_eq!(eff, Effect::None);
            assert_eq!(ids(&b, BookSide::Bid, 100), vec![1, 2]);
        }
        // A strict decrease keeps priority everywhere.
        assert!(matches!(
            b.apply(&rec(b'M', b'B', 2, 100, 3)),
            Effect::Reduce { by: 4, .. }
        ));
        assert_eq!(b.audit(), Ok(()));
    }
}

#[test]
fn o_book_4_add_cancel_modify_fifo() {
    let mut b = Book::new(rules(Venue::Cme));
    b.apply(&rec(b'A', b'B', 1, 100, 5));
    b.apply(&rec(b'A', b'B', 2, 100, 7));
    b.apply(&rec(b'A', b'A', 3, 101, 4));
    let bbo = b.bbo();
    assert_eq!((bbo.bid, bbo.ask), (Some(lv(100, 12, 2)), Some(lv(101, 4, 1))));
    assert_eq!(ids(&b, BookSide::Bid, 100), vec![1, 2]);
    assert_eq!(b.queue_ahead_of(2), Some((5, 1)));

    // size decrease keeps priority
    assert_eq!(
        b.apply(&rec(b'M', b'B', 1, 100, 3)),
        Effect::Reduce {
            order_id: 1,
            side: BookSide::Bid,
            price: 100,
            by: 2
        }
    );
    assert_eq!(ids(&b, BookSide::Bid, 100), vec![1, 2]);
    // size increase loses priority
    assert!(matches!(b.apply(&rec(b'M', b'B', 1, 100, 9)), Effect::Replace { .. }));
    assert_eq!(ids(&b, BookSide::Bid, 100), vec![2, 1]);
    // price change moves level
    assert!(matches!(b.apply(&rec(b'M', b'B', 2, 99, 7)), Effect::Replace { .. }));
    assert_eq!(ids(&b, BookSide::Bid, 100), vec![1]);
    assert_eq!(ids(&b, BookSide::Bid, 99), vec![2]);
    // partial cancel keeps, full cancel removes
    assert_eq!(
        b.apply(&rec(b'C', b'B', 1, 100, 4)),
        Effect::Reduce {
            order_id: 1,
            side: BookSide::Bid,
            price: 100,
            by: 4
        }
    );
    assert_eq!(
        b.apply(&rec(b'C', b'B', 1, 100, 5)),
        Effect::Remove {
            order_id: 1,
            side: BookSide::Bid,
            price: 100,
            size: 5
        }
    );
    assert_eq!(b.bbo().bid.map(|l| l.price), Some(99));
    // trade and fill are informational; unknown cancel is counted
    assert_eq!(b.apply(&rec(b'T', b'A', 0, 99, 1)), Effect::None);
    assert_eq!(b.apply(&rec(b'F', b'B', 2, 99, 1)), Effect::None);
    assert_eq!(b.apply(&rec(b'C', b'B', 42, 99, 1)), Effect::None);
    assert_eq!(b.invariants().unknown_order_refs, 1);
    assert_eq!(b.audit(), Ok(()));
    // clear
    assert_eq!(b.apply(&rec(b'R', b'N', 0, UNDEF_PRICE, 0)), Effect::Clear);
    assert_eq!(b.order_count(), 0);
    assert_eq!(b.bbo().bid, None);
    assert_eq!(b.audit(), Ok(()));
}

#[test]
fn o_book_4_nasdaq_rules_reject_modify_and_count_duplicates() {
    let mut b = Book::new(rules(Venue::Nasdaq));
    b.apply(&rec(b'A', b'B', 1, 100, 5));
    // M is a data error on ITCH: ignored, counted, book unchanged.
    assert_eq!(b.apply(&rec(b'M', b'B', 1, 100, 9)), Effect::None);
    assert_eq!(b.apply(&rec(b'M', b'B', 7, 100, 9)), Effect::None);
    assert_eq!(
        (b.invariants().unexpected_modifies, b.invariants().unknown_order_refs),
        (2, 0)
    );
    assert_eq!(b.bbo().bid.map(|l| l.total), Some(5));
    // Duplicate add outside a snapshot: counted, replaces.
    assert!(matches!(b.apply(&rec(b'A', b'B', 1, 101, 2)), Effect::Add { .. }));
    assert_eq!(b.invariants().duplicate_adds, 1);
    assert_eq!(b.depth(BookSide::Bid, 5), vec![lv(101, 2, 1)]);
    // Fill larger than resting, fill of unknown id, cancel larger than resting.
    b.apply(&rec(b'F', b'B', 1, 101, 3));
    b.apply(&rec(b'F', b'B', 2, 101, 3));
    b.apply(&rec(b'C', b'B', 1, 101, 3));
    let i = b.invariants();
    assert_eq!(
        (
            i.fills_exceeding_resting,
            i.fills_unknown_order,
            i.cancels_exceeding_resting
        ),
        (1, 1, 1)
    );
    assert_eq!(b.order_count(), 0);
    assert_eq!(b.audit(), Ok(()));
    // CME: a snapshot re-add is not a duplicate; a live one is.
    let mut c = Book::new(rules(Venue::Cme));
    c.apply(&rec(b'A', b'A', 1, 100, 5));
    let mut snap = rec(b'A', b'A', 1, 100, 5);
    snap.flags |= flags::SNAPSHOT;
    c.apply(&snap);
    c.apply(&rec(b'A', b'A', 1, 100, 5));
    assert_eq!(c.invariants().duplicate_adds, 1);
    // Locked and crossed books after F_LAST.
    let mut x = Book::new(rules(Venue::Cme));
    x.apply(&rec(b'A', b'B', 1, 100, 1));
    x.apply(&rec(b'A', b'A', 2, 100, 1));
    x.note_event_end();
    x.apply(&rec(b'A', b'B', 3, 101, 1));
    x.note_event_end();
    x.apply(&rec(b'C', b'B', 1, 100, 1));
    x.apply(&rec(b'C', b'B', 3, 101, 1));
    x.note_event_end(); // one side empty: not counted
    let i = *x.invariants();
    assert_eq!((i.crossed_books_after_last, i.negative_spreads), (2, 1));
    assert_eq!(
        i.nonzero(),
        vec![("crossed_books_after_last", 2), ("negative_spreads", 1)]
    );
    let mut sum = Invariants::default();
    sum.add(&i);
    sum.add(&i);
    assert_eq!(sum.negative_spreads, 2);
}

#[test]
fn o_book_4_order_rank_skips_consumed_orders_and_levels() {
    let mut b = Book::new(rules(Venue::Cme));
    b.apply(&rec(b'A', b'B', 1, 100, 5));
    b.apply(&rec(b'A', b'B', 2, 100, 7));
    b.apply(&rec(b'A', b'B', 3, 99, 7));
    b.apply(&rec(b'A', b'A', 4, 101, 7));
    let r = b.order_rank(2, &[], None).unwrap();
    assert_eq!((r.rank, r.level_depth, r.best_px), (1, 0, 100));
    assert_eq!(b.order_rank(2, &[1], None).unwrap().rank, 0);
    let r = b.order_rank(3, &[], None).unwrap();
    assert_eq!((r.rank, r.level_depth, r.best_px), (0, 1, 100));
    // Level 100 fully consumed earlier in the event: 99 is the effective best.
    let r = b.order_rank(3, &[1, 2], None).unwrap();
    assert_eq!((r.rank, r.level_depth, r.best_px), (0, 0, 99));
    assert_eq!(b.order_rank(4, &[], None).unwrap().side, BookSide::Ask);
    assert!(b.order_rank(9, &[], None).is_none());
    // Ids ahead (live only) for the sweep-adjusted rank; the buffer is reset.
    let mut ahead = vec![42];
    b.apply(&rec(b'A', b'B', 5, 100, 1));
    let r = b.order_rank(5, &[2], Some(&mut ahead)).unwrap();
    assert_eq!((r.rank, ahead), (1, vec![1]));
    // Sizes beyond u32 are exact.
    b.apply(&rec(b'A', b'A', 6, 101, 5_000_000_000));
    assert_eq!(b.bbo().ask.map(|l| l.total), Some(5_000_000_007));
    assert_eq!(b.level(BookSide::Ask, 101).map(|l| l.total), Some(5_000_000_007));
    assert_eq!(b.audit(), Ok(()));
}

#[test]
fn o_book_4_modify_unknown_is_add() {
    let mut b = Book::new(rules(Venue::Cme));
    assert!(matches!(b.apply(&rec(b'M', b'A', 9, 105, 2)), Effect::Add { .. }));
    assert_eq!(b.bbo().ask.map(|l| l.total), Some(2));
    assert_eq!(b.invariants().unknown_order_refs, 1);
    assert!(b.depth(BookSide::Bid, 5).is_empty());
    assert_eq!(b.depth(BookSide::Ask, 5), vec![lv(105, 2, 1)]);
    // The reference book agrees on the same records.
    let mut r = RefBook::new(rules(Venue::Cme));
    r.apply(&rec(b'M', b'A', 9, 105, 2));
    assert_eq!(r.bbo(), b.bbo());
}

#[test]
fn o_book_4_ignored_adds_and_zero_size_modify() {
    let mut b = Book::new(rules(Venue::Generic));
    assert_eq!(b.apply(&rec(b'A', b'B', 1, UNDEF_PRICE, 5)), Effect::None);
    assert_eq!(b.apply(&rec(b'A', b'B', 1, 100, 0)), Effect::None);
    assert_eq!(b.apply(&rec(b'A', b'N', 1, 100, 5)), Effect::None);
    assert_eq!(b.order_count(), 0);
    b.apply(&rec(b'A', b'B', 1, 100, 5));
    assert_eq!(
        b.apply(&rec(b'M', b'B', 1, 100, 0)),
        Effect::Remove {
            order_id: 1,
            side: BookSide::Bid,
            price: 100,
            size: 5
        }
    );
    b.apply(&rec(b'A', b'B', 2, 100, 5));
    assert_eq!(b.apply(&rec(b'M', b'B', 2, 100, 5)), Effect::None);
    assert_eq!(
        b.order(2).map(|(s, p, o)| (s, p, o.size)),
        Some((BookSide::Bid, 100, 5))
    );
    assert_eq!(b.audit(), Ok(()));
}
