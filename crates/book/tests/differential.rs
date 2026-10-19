//! O-BOOK-1 / O-BOOK-2: `Book` against the naive `RefBook` on random record streams (nasdaq, cme, generic), checked
//! after every record: effect, BBO, every level, FIFO order of every level, `queue_ahead_of` of every resting
//! order, and `Book::audit()`.

use proptest::prelude::*;
use qt_book::{Book, LevelView, RestingOrder, reference::RefBook};
use qt_types::{BookSide, MboEvent, UNDEF_PRICE, Venue, Xoshiro256StarStar, flags, rules};

const VENUES: [Venue; 3] = [Venue::Nasdaq, Venue::Cme, Venue::Generic];

/// Random stream over a small id pool and a narrow price band, so duplicates, unknown ids, oversize cancels and
/// fills, crossing adds, clears, snapshot flags and every modify case all occur.
fn stream(seed: u64, len: usize) -> Vec<MboEvent> {
    let mut r = Xoshiro256StarStar::new(seed);
    let mut out = Vec::with_capacity(len);
    for i in 0..len {
        let k = r.next_u64() % 100;
        let action = match k {
            0..=37 => b'A',
            38..=57 => b'C',
            58..=71 => b'M',
            72..=79 => b'T',
            80..=89 => b'F',
            90 => b'R',
            91..=92 => b'N',
            93..=94 => b'X', // unknown action byte: ignored
            _ => b'A',
        };
        let side = match r.next_u64() % 40 {
            0 => b'N',
            1..=20 => b'B',
            _ => b'A',
        };
        let price = if r.next_u64() % 100 == 0 {
            UNDEF_PRICE
        } else {
            95 + (r.next_u64() % 11) as i64
        };
        let size = match r.next_u64() % 30 {
            0 => 0,
            1 => 5_000_000_000,
            _ => 1 + r.next_u64() % 40,
        };
        let mut f = 0u8;
        if r.next_u64() % 2 == 0 {
            f |= flags::LAST;
        }
        if r.next_u64() % 20 == 0 {
            f |= flags::SNAPSHOT;
        }
        out.push(MboEvent {
            ts_event: i as i64,
            ts_recv: i as i64 + 3,
            instrument_id: 1,
            action,
            side,
            price,
            size,
            order_id: 1 + r.next_u64() % 60,
            flags: f,
            sequence: i as u64,
            ..MboEvent::default()
        });
    }
    out
}

fn compare(book: &Book, rb: &RefBook) -> Result<(), String> {
    if book.bbo() != rb.bbo() {
        return Err(format!("bbo {:?} != {:?}", book.bbo(), rb.bbo()));
    }
    let mut resting = 0usize;
    for side in [BookSide::Bid, BookSide::Ask] {
        let a: Vec<LevelView> = book.levels(side).collect();
        let b: Vec<LevelView> = rb.levels(side).collect();
        if a != b {
            return Err(format!("{side:?} levels {a:?} != {b:?}"));
        }
        if book.depth(side, 3) != b.iter().take(3).copied().collect::<Vec<_>>() {
            return Err(format!("{side:?} depth(3) differs"));
        }
        for l in &b {
            if book.level(side, l.price) != Some(*l) || rb.level(side, l.price) != Some(*l) {
                return Err(format!("level lookup at {side:?}@{}", l.price));
            }
            let x: Vec<&RestingOrder> = book.orders_at(side, l.price).collect();
            let y: Vec<&RestingOrder> = rb.orders_at(side, l.price).collect();
            if x != y {
                return Err(format!("FIFO at {side:?}@{}: {x:?} != {y:?}", l.price));
            }
            let (mut qty, mut n) = (0u64, 0u32);
            for o in y {
                let want = Some((qty, n));
                if book.queue_ahead_of(o.order_id) != want {
                    return Err(format!(
                        "queue_ahead_of({}) {:?} != {want:?}",
                        o.order_id,
                        book.queue_ahead_of(o.order_id)
                    ));
                }
                if book.order(o.order_id).map(|(s, p, _)| (s, p)) != Some((side, l.price)) {
                    return Err(format!("order({}) locator", o.order_id));
                }
                qty += o.size;
                n += 1;
                resting += 1;
            }
        }
    }
    if resting != book.order_count() {
        return Err(format!("order_count {} != {resting}", book.order_count()));
    }
    Ok(())
}

fn run(seed: u64, len: usize, venue: Venue) -> Result<(), String> {
    let rules = rules(venue);
    let mut book = Book::new(rules);
    let mut rb = RefBook::new(rules);
    for (i, e) in stream(seed, len).iter().enumerate() {
        let x = book.apply(e);
        let y = rb.apply(e);
        if x != y {
            return Err(format!("record {i} {e:?}: effect {x:?} != {y:?}"));
        }
        if e.flags & flags::LAST != 0 {
            book.note_event_end();
        }
        compare(&book, &rb).map_err(|m| format!("record {i} {e:?}: {m}"))?;
        book.audit().map_err(|m| format!("record {i}: audit: {m}"))?;
        if let Some(err) = book.error() {
            return Err(format!("record {i}: engine error {err}"));
        }
        if book.queue_ahead_of(e.order_id) != rb.queue_ahead_of(e.order_id) {
            return Err(format!(
                "record {i}: queue_ahead_of({}) differs from the reference",
                e.order_id
            ));
        }
    }
    Ok(())
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 2000, failure_persistence: None, ..ProptestConfig::default() })]

    #[test]
    fn o_book_1_and_o_book_2_differential_with_audit(seed in any::<u64>(), len in 0usize..=3000, v in 0usize..3) {
        if let Err(m) = run(seed, len, VENUES[v]) {
            prop_assert!(false, "seed {seed} len {len} venue {:?}: {m}", VENUES[v]);
        }
    }
}

#[test]
fn o_book_1_fixed_seeds_all_venues() {
    for v in VENUES {
        for seed in 0..5 {
            assert_eq!(run(seed, 3000, v), Ok(()), "{v:?} seed {seed}");
        }
    }
}
