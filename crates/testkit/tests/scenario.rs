//! Scenario DSL: F_LAST batching, venue-correct F/C pairing by sequence, snapshot flags, unknown references.

use qt_testkit::scenario::Scenario;
use qt_types::{BookSide, Side, UNDEF_PRICE, Venue, flags};

#[test]
fn scenario_batches_and_cme_sequences() {
    let ev = Scenario::new(Venue::Cme)
        .snapshot(10, |e| e.add(1, BookSide::Bid, 100, 60).add(9, BookSide::Ask, 101, 5))
        .event(20, |e| e.trade(Side::Ask, 100, 10).fill(1, 10))
        .clear(30)
        .build();
    assert_eq!(ev.len(), 7);
    assert!(ev[..3].iter().all(|r| r.flags & flags::SNAPSHOT != 0));
    assert_eq!(ev[0].action, b'R');
    let last: Vec<bool> = ev.iter().map(|r| r.is_last()).collect();
    assert_eq!(last, vec![false, false, true, false, false, true, true]);
    assert_eq!(
        ev.iter().map(|r| r.sequence).collect::<Vec<_>>(),
        vec![1, 2, 3, 4, 5, 6, 7]
    );
    assert_eq!(
        (ev[4].action, ev[5].action, ev[5].price, ev[5].side),
        (b'F', b'C', 100, b'B')
    );
    assert!(ev.iter().all(|r| r.ts_recv == r.ts_event && r.publisher_id == 1));
}

#[test]
fn scenario_nasdaq_groups_share_sequence() {
    let ev = Scenario::new(Venue::Nasdaq)
        .event(10, |e| e.add(1, BookSide::Bid, 100, 60))
        .event(20, |e| e.execute(1, 10))
        .event(30, |e| e.replace(1, 2, 99, 70))
        .event(40, |e| e.hidden_print(100, 500).cancel(77, 1))
        .build();
    let seq: Vec<(u8, u64)> = ev.iter().map(|r| (r.action, r.sequence)).collect();
    assert_eq!(
        seq,
        vec![
            (b'A', 1),
            (b'T', 2),
            (b'F', 2),
            (b'C', 2),
            (b'C', 3),
            (b'A', 3),
            (b'T', 4),
            (b'C', 5)
        ]
    );
    assert_eq!((ev[1].side, ev[1].price), (b'A', 100));
    assert_eq!((ev[4].size, ev[5].order_id, ev[5].price), (50, 2, 99));
    assert_eq!((ev[6].side, ev[7].side, ev[7].price), (b'N', b'N', UNDEF_PRICE));
    let rows = Scenario::new(Venue::Nasdaq)
        .event(1_781_532_000_000_000_000, |e| e.add(1, BookSide::Bid, 1, 1))
        .instrument_rows(1);
    assert_eq!(
        (rows[0].open_s, rows[0].close_s, rows[0].session_tz.as_str()),
        (34_200, 57_600, "America/New_York")
    );
}

#[test]
fn scenario_flag_and_gap_helpers() {
    let ev = Scenario::new(Venue::Cme)
        .event(10, |e| {
            e.add(1, BookSide::Bid, 100, 5)
                .flag(flags::MAYBE_BAD_BOOK)
                .skip_sequence(3)
                .add(2, BookSide::Bid, 100, 5)
        })
        .instrument(2)
        .event(11, |e| e.add(1, BookSide::Ask, 101, 5))
        .build();
    assert_eq!(ev[0].flags & flags::MAYBE_BAD_BOOK, flags::MAYBE_BAD_BOOK);
    assert_eq!((ev[1].sequence, ev[2].instrument_id), (5, 2));
}
