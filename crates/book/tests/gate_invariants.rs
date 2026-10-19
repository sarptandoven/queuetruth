//! O-BOOK-3 (gate, needs `qt_testkit`): invariants are all zero on `qt_testkit::generate` streams (5 seeds x 3 venues)
//! and equal the injected counts on corrupted streams (unknown cancel, duplicate add outside a snapshot, crossed add,
//! M on nasdaq).

use std::collections::BTreeMap;

use qt_book::{Book, Invariants};
use qt_testkit::{SynthConfig, generate};
use qt_types::{MboEvent, Venue, flags, rules};

fn cfg(seed: u64, venue: Venue) -> SynthConfig {
    SynthConfig {
        seed,
        venue,
        instruments: 2,
        events_per_instrument: 3_000,
        tick: 1,
        base_px: 10_000,
        max_levels: 10,
        mean_size: 10,
        p_add: 0.45,
        p_cancel: 0.25,
        p_reduce: 0.1,
        p_modify: if venue == Venue::Nasdaq { 0.0 } else { 0.05 },
        p_trade: 0.15,
        p_clear: 0.0,
        hidden_print_rate: if venue == Venue::Cme { 0.0 } else { 0.02 },
        snapshot_at_start: venue == Venue::Cme,
        ts_start: 1_700_000_000_000_000_000,
        mean_gap_ns: 1_000_000,
    }
}

fn replay(venue: Venue, events: &[MboEvent]) -> Invariants {
    let mut books: BTreeMap<u32, Book> = BTreeMap::new();
    for e in events {
        let b = books.entry(e.instrument_id).or_insert_with(|| Book::new(rules(venue)));
        b.apply(e);
        if e.flags & flags::LAST != 0 {
            b.note_event_end();
        }
    }
    let mut inv = Invariants::default();
    for b in books.values() {
        assert_eq!(b.audit(), Ok(()));
        inv.add(b.invariants());
    }
    inv
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn o_book_3_invariants_zero_on_synthetic_and_counted_when_injected() {
    for venue in [Venue::Nasdaq, Venue::Cme, Venue::Generic] {
        for seed in 1..=5 {
            let out = generate(&cfg(seed, venue));
            assert!(!out.events.is_empty());
            let clean = replay(venue, &out.events);
            assert_eq!(clean, Invariants::default(), "{venue:?} seed {seed}");

            let mut ev = out.events.clone();
            let n = ev.len();
            // Duplicate add outside a snapshot: a copy of a live add right after it (no LAST, no SNAPSHOT).
            let i = (n / 2..n)
                .find(|&i| ev[i].action == b'A' && ev[i].flags & flags::SNAPSHOT == 0)
                .expect("an add in the second half");
            let mut dup = ev[i];
            dup.flags &= !(flags::LAST | flags::SNAPSHOT);
            ev.insert(i, dup);
            // Unknown cancel inside an event.
            let mut ghost = ev[n / 3];
            ghost.action = b'C';
            ghost.order_id = u64::MAX - 7;
            ghost.size = 1;
            ghost.flags &= !(flags::LAST | flags::SNAPSHOT);
            ev.insert(n / 3, ghost);
            // Crossed add as the last event: a bid far above every ask.
            let last = *ev.last().expect("records");
            ev.push(MboEvent {
                action: b'A',
                side: b'B',
                order_id: u64::MAX - 9,
                price: 10_000 * 10,
                size: 1,
                flags: flags::LAST,
                sequence: last.sequence + 1,
                ..last
            });
            let mut expect = Invariants {
                unknown_order_refs: 1,
                duplicate_adds: 1,
                crossed_books_after_last: 1,
                negative_spreads: 1,
                ..Invariants::default()
            };
            if venue == Venue::Nasdaq {
                let mut m = ev[n / 4];
                m.action = b'M';
                m.flags &= !(flags::LAST | flags::SNAPSHOT);
                ev.insert(n / 4, m);
                expect.unexpected_modifies = 1;
            }
            assert_eq!(replay(venue, &ev), expect, "{venue:?} seed {seed} corrupted");
        }
    }
}
