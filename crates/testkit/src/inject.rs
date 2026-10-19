//! Corrupted streams with known counter values (O-BOOK-3 support): each injection inserts one standalone event
//! (`LAST`) at an event boundary, reusing the previous record's timestamps, instrument, channel and sequence so no
//! sequence gap or timestamp regression is introduced. The reference book (`refbook::NaiveBook`) of the clean stream
//! decides where an injection is possible.

use qt_types::{Action, BookSide, MboEvent, Px, Side, UNDEF_PRICE, VenueRules, flags};

use crate::dist::Rng;
use crate::refbook::{NaiveBook, RefInvariants};

/// Kind of corruption.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Corruption {
    /// `C` for an order id never seen (+1 `unknown_order_refs` each).
    UnknownCancel,
    /// Plain `A` re-adding a resting order with its own side, price and size (+1 `duplicate_adds` each).
    DuplicateAdd,
    /// `A` on the ask at the best bid (one event later removed by a `C`): +1 `crossed_books_after_last` each.
    CrossedAdd,
    /// `M` repeating a resting order (+1 `unexpected_modifies` each on venues without modifies; else no effect).
    Modify,
}

/// Result of an injection.
#[derive(Clone, Debug, PartialEq)]
pub struct Injected {
    /// The corrupted stream.
    pub events: Vec<MboEvent>,
    /// Number of injections performed.
    pub injected: u64,
    /// Invariant counters the reference book reports on the corrupted stream.
    pub expected: RefInvariants,
}

fn base(prev: &MboEvent) -> MboEvent {
    MboEvent {
        ts_event: prev.ts_event,
        ts_recv: prev.ts_recv,
        instrument_id: prev.instrument_id,
        channel_id: prev.channel_id,
        publisher_id: prev.publisher_id,
        sequence: prev.sequence,
        flags: flags::LAST,
        ..MboEvent::default()
    }
}

fn best(book: &NaiveBook, side: BookSide) -> Option<Px> {
    book.best_px(side)
}

/// Inserts up to `count` corruptions of one kind at seeded event boundaries of a single-instrument-per-book stream
/// (books are kept per instrument). Injected order ids start at `u64::MAX / 2`.
pub fn corrupt(events: &[MboEvent], rules: VenueRules, kind: Corruption, count: u64, seed: u64) -> Injected {
    let mut rng = Rng::new(seed);
    let n_events = events.iter().filter(|e| e.is_last()).count().max(1) as f64;
    let p = (count as f64 * 2.0 / n_events).min(1.0);
    let mut books: std::collections::BTreeMap<u32, NaiveBook> = std::collections::BTreeMap::new();
    let mut out = Vec::with_capacity(events.len() + 2 * count as usize);
    let mut injected = 0;
    let mut next_id = u64::MAX / 2;
    for e in events {
        let book = books.entry(e.instrument_id).or_insert_with(|| NaiveBook::new(rules));
        book.apply(e);
        out.push(*e);
        if !e.is_last() || e.flags & flags::SNAPSHOT != 0 || injected >= count || !rng.chance(p) {
            continue;
        }
        let ids = book.ids_at(BookSide::Bid, best(book, BookSide::Bid).unwrap_or(UNDEF_PRICE));
        let mut extra: Vec<MboEvent> = Vec::new();
        match kind {
            Corruption::UnknownCancel => {
                next_id += 1;
                let mut r = base(e);
                r.action = Action::Cancel.as_byte();
                r.side = Side::Bid.as_byte();
                r.price = best(book, BookSide::Bid).unwrap_or(UNDEF_PRICE);
                r.size = 1;
                r.order_id = next_id;
                extra.push(r);
            }
            Corruption::DuplicateAdd | Corruption::Modify => {
                let Some(&id) = ids.first() else { continue };
                let Some(o) = book.order(id) else { continue };
                let mut r = base(e);
                r.action = if kind == Corruption::Modify {
                    Action::Modify
                } else {
                    Action::Add
                }
                .as_byte();
                r.side = o.side.as_byte();
                r.price = o.price;
                r.size = o.size;
                r.order_id = id;
                extra.push(r);
            }
            Corruption::CrossedAdd => {
                let (Some(b), Some(_)) = (best(book, BookSide::Bid), best(book, BookSide::Ask)) else {
                    continue;
                };
                next_id += 1;
                let mut a = base(e);
                a.action = Action::Add.as_byte();
                a.side = Side::Ask.as_byte();
                a.price = b;
                a.size = 1;
                a.order_id = next_id;
                let mut c = a;
                c.action = Action::Cancel.as_byte();
                extra.push(a);
                extra.push(c);
            }
        }
        for r in extra {
            book.apply(&r);
            book.note_event_end();
            out.push(r);
        }
        injected += 1;
    }
    // Recount on the corrupted stream with fresh books.
    let mut books: std::collections::BTreeMap<u32, NaiveBook> = std::collections::BTreeMap::new();
    for e in &out {
        let b = books.entry(e.instrument_id).or_insert_with(|| NaiveBook::new(rules));
        b.apply(e);
        if e.is_last() {
            b.note_event_end();
        }
    }
    let mut expected = RefInvariants::default();
    for b in books.values() {
        let i = b.invariants();
        expected.cancels_exceeding_resting += i.cancels_exceeding_resting;
        expected.fills_exceeding_resting += i.fills_exceeding_resting;
        expected.fills_unknown_order += i.fills_unknown_order;
        expected.unknown_order_refs += i.unknown_order_refs;
        expected.duplicate_adds += i.duplicate_adds;
        expected.unexpected_modifies += i.unexpected_modifies;
        expected.crossed_books_after_last += i.crossed_books_after_last;
        expected.negative_spreads += i.negative_spreads;
    }
    Injected {
        events: out,
        injected,
        expected,
    }
}
