//! Generator self-consistency: the stream replayed on the independent naive book (`refbook::NaiveBook`) reproduces
//! `expected_bbo`, `expected_trades` and `expected_depth_final`; invariants stay zero (never crossed after `LAST`)
//! unless corruption is injected; venue encodings, sequences and timestamps follow the rules; every book action,
//! priority rule, sweep and hidden execution the SPEC names occurs.

#![allow(clippy::type_complexity)]

use std::collections::BTreeMap;

use qt_schema::json::Verdict;
use qt_schema::rows::{BboRow, TradeKind, TradeRow};
use qt_testkit::calendar::{self, NS_PER_S, local_date};
use qt_testkit::inject::{Corruption, corrupt};
use qt_testkit::refbook::{DqTracker, NaiveBook, RefEffect, RefInvariants, SeqCheck, SeqTracker};
use qt_testkit::{SynthConfig, SynthOutput, generate, generate_sessions};
use qt_types::{BookSide, MboEvent, Side, Venue, flags, rules};

const VENUES: [Venue; 3] = [Venue::Cme, Venue::Nasdaq, Venue::Generic];

fn cfg(venue: Venue, seed: u64) -> SynthConfig {
    let mut c = SynthConfig::for_venue(venue, seed);
    c.events_per_instrument = 2_500;
    c.p_clear = if venue == Venue::Nasdaq { 0.0 } else { 0.004 };
    c
}

/// Events (records up to and including `LAST`) of one instrument, in order.
fn events_of(recs: &[MboEvent]) -> Vec<Vec<MboEvent>> {
    let mut open: BTreeMap<u32, Vec<MboEvent>> = BTreeMap::new();
    let mut out = Vec::new();
    for r in recs {
        let v = open.entry(r.instrument_id).or_default();
        v.push(*r);
        if r.is_last() {
            out.push(std::mem::take(v));
        }
    }
    assert!(open.values().all(Vec::is_empty), "every event ends with LAST");
    out
}

fn replay(
    out: &SynthOutput,
) -> (
    Vec<BboRow>,
    Vec<TradeRow>,
    RefInvariants,
    Vec<(u32, BookSide, i64, u64, u32)>,
) {
    let r = rules(out.instruments[0].venue);
    let mut books: BTreeMap<u32, NaiveBook> = BTreeMap::new();
    let mut bbo = Vec::new();
    let mut trades = Vec::new();
    for e in &out.events {
        let b = books.entry(e.instrument_id).or_insert_with(|| NaiveBook::new(r));
        b.apply(e);
        if e.action == b'T' {
            let side = Side::from_byte(e.side);
            trades.push(TradeRow {
                ts_event: e.ts_event,
                instrument_id: e.instrument_id,
                sequence: e.sequence,
                price: e.price,
                size: e.size,
                aggressor_side: side,
                kind: if side == Side::None && r.supports_hidden_orders {
                    TradeKind::Hidden
                } else {
                    TradeKind::Displayed
                },
                maker_order_id: None,
            });
        }
        if e.is_last() {
            b.note_event_end();
            let bid = b.best(BookSide::Bid);
            let ask = b.best(BookSide::Ask);
            bbo.push(BboRow {
                ts_event: e.ts_event,
                instrument_id: e.instrument_id,
                sequence: e.sequence,
                bid_px: bid.map(|x| x.0),
                bid_sz: bid.map_or(0, |x| x.1),
                bid_ct: bid.map_or(0, |x| x.2),
                ask_px: ask.map(|x| x.0),
                ask_sz: ask.map_or(0, |x| x.1),
                ask_ct: ask.map_or(0, |x| x.2),
            });
        }
    }
    let mut inv = RefInvariants::default();
    let mut depth = Vec::new();
    for (id, b) in &books {
        let i = b.invariants();
        inv.cancels_exceeding_resting += i.cancels_exceeding_resting;
        inv.fills_exceeding_resting += i.fills_exceeding_resting;
        inv.fills_unknown_order += i.fills_unknown_order;
        inv.unknown_order_refs += i.unknown_order_refs;
        inv.duplicate_adds += i.duplicate_adds;
        inv.unexpected_modifies += i.unexpected_modifies;
        inv.crossed_books_after_last += i.crossed_books_after_last;
        inv.negative_spreads += i.negative_spreads;
        for s in [BookSide::Bid, BookSide::Ask] {
            for (p, t, c) in b.levels(s) {
                depth.push((*id, s, p, t, c));
            }
        }
    }
    (bbo, trades, inv, depth)
}

#[test]
fn generator_self_consistent_5_seeds_3_venues() {
    for venue in VENUES {
        for seed in 1..=5 {
            let c = cfg(venue, seed);
            let out = generate(&c);
            assert_eq!(out, generate(&c), "{venue} seed {seed}: deterministic");
            let evs = events_of(&out.events);
            assert_eq!(
                evs.len() as u64,
                c.events_per_instrument * u64::from(c.instruments),
                "{venue} {seed}"
            );
            assert_eq!(out.expected_bbo.len(), evs.len());
            let (bbo, trades, inv, depth) = replay(&out);
            assert_eq!(bbo, out.expected_bbo, "{venue} seed {seed}: bbo");
            assert_eq!(trades, out.expected_trades, "{venue} seed {seed}: trades");
            assert_eq!(depth, out.expected_depth_final, "{venue} seed {seed}: final depth");
            assert_eq!(inv, RefInvariants::default(), "{venue} seed {seed}: invariants");
            for b in &out.expected_bbo {
                if let (Some(bp), Some(ap)) = (b.bid_px, b.ask_px) {
                    assert!(bp < ap, "never crossed or locked after LAST");
                }
            }
        }
    }
}

#[test]
fn generator_sequences_timestamps_and_flags() {
    for venue in VENUES {
        let out = generate(&cfg(venue, 11));
        let r = rules(venue);
        let mut seqs = SeqTracker::default();
        let mut last_ts: BTreeMap<u32, i64> = BTreeMap::new();
        for e in &out.events {
            assert_eq!(seqs.check(e), SeqCheck::Ok, "{venue}: contiguous sequence per channel");
            let t = last_ts.entry(e.instrument_id).or_insert(e.ts_event);
            assert!(e.ts_event >= *t, "{venue}: ts_event non-decreasing per instrument");
            *t = e.ts_event;
            assert!(e.ts_recv > e.ts_event && e.ts_recv - e.ts_event <= 25_000);
            assert_eq!(e.channel_id as u32, e.instrument_id - 1);
            assert!(e.flags & !(flags::LAST | flags::SNAPSHOT) == 0);
            assert!(e.size > 0 || e.action == b'R' || (e.action == b'M' && venue == Venue::Generic));
            if !r.modify_records_present {
                assert_ne!(e.action, b'M', "{venue}: no M");
            }
        }
        let evs = events_of(&out.events);
        for ev in &evs {
            if venue != Venue::Generic {
                assert!(
                    ev.iter().all(|r| r.sequence == ev[0].sequence),
                    "{venue}: one sequence per event"
                );
            }
            // Records of one event share the timestamps.
            assert!(
                ev.iter()
                    .all(|r| r.ts_event == ev[0].ts_event && r.ts_recv == ev[0].ts_recv)
            );
            // Every F is followed by its C (same id and size) later in the event; generic pairs share sequence.
            for (i, f) in ev.iter().enumerate().filter(|(_, r)| r.action == b'F') {
                let c = ev[i + 1..]
                    .iter()
                    .find(|r| r.action == b'C' && r.order_id == f.order_id);
                let c = c.expect("F has its C");
                assert_eq!(c.size, f.size);
                if r.fill_removal_shares_sequence {
                    assert_eq!(c.sequence, f.sequence);
                }
            }
        }
        // Instruments: session rows on the stream's local date, events inside the day.
        for i in &out.instruments {
            assert_eq!(i.midnight_utc_ns, calendar::local_midnight(venue, i.session_date));
            assert_eq!(i.venue, venue);
        }
        assert!(
            out.events
                .iter()
                .all(|e| local_date(venue, e.ts_event) == out.instruments[0].session_date)
        );
    }
}

#[derive(Default, Debug)]
struct Coverage {
    adds: u64,
    full_cancels: u64,
    partial_cancels: u64,
    modify_keep: u64,
    modify_lose_size: u64,
    modify_lose_price: u64,
    modify_noop: u64,
    modify_refresh: u64,
    modify_delete: u64,
    replaces: u64,
    multi_level_sweeps: u64,
    exact_level_clears: u64,
    remainder_rests: u64,
    hidden_prints: u64,
    nonprinted_execs: u64,
    off_price: u64,
    clears: u64,
    snapshot_readds: u64,
    zero_gap_events: u64,
    empty_side_events: u64,
    trade_side_none_displayed: u64,
}

fn coverage(out: &SynthOutput) -> Coverage {
    let venue = out.instruments[0].venue;
    let r = rules(venue);
    let mut cov = Coverage::default();
    let mut books: BTreeMap<u32, NaiveBook> = BTreeMap::new();
    let mut prev_ts: BTreeMap<u32, i64> = BTreeMap::new();
    let mut prev_exec: BTreeMap<u32, bool> = BTreeMap::new();
    for ev in events_of(&out.events) {
        let inst = ev[0].instrument_id;
        // Nasdaq: the aggressor's remainder rests in its own message right after the executions.
        let after_exec = prev_exec
            .insert(inst, ev.iter().any(|x| x.action == b'F'))
            .unwrap_or(false);
        if venue == Venue::Nasdaq
            && after_exec
            && ev.len() == 1
            && ev[0].action == b'A'
            && prev_ts.get(&inst) == Some(&ev[0].ts_event)
        {
            cov.remainder_rests += 1;
        }
        let book = books.entry(inst).or_insert_with(|| NaiveBook::new(r));
        if prev_ts.insert(inst, ev[0].ts_event) == Some(ev[0].ts_event) {
            cov.zero_gap_events += 1;
        }
        let mut t_prices = Vec::new();
        let has_t = ev.iter().any(|x| x.action == b'T');
        for (i, e) in ev.iter().enumerate() {
            let before = book.order(e.order_id);
            if e.action == b'F' {
                if !has_t {
                    cov.nonprinted_execs += 1;
                }
                if before.is_some_and(|o| o.price != e.price) {
                    cov.off_price += 1;
                }
            }
            if e.action == b'A' && e.flags & flags::SNAPSHOT != 0 && before.is_some() {
                cov.snapshot_readds += 1;
            }
            if e.action == b'A' && i > 0 && ev[..i].iter().any(|x| x.action == b'F') && e.flags & flags::SNAPSHOT == 0 {
                cov.remainder_rests += 1;
            }
            let eff = book.apply(e);
            match (e.action, eff) {
                (b'A', RefEffect::Add { .. }) if e.flags & flags::SNAPSHOT == 0 => cov.adds += 1,
                (b'C', RefEffect::Remove { .. })
                    if !ev.iter().any(|x| x.action == b'F' && x.order_id == e.order_id) =>
                {
                    cov.full_cancels += 1;
                }
                (b'C', RefEffect::Reduce { .. })
                    if !ev.iter().any(|x| x.action == b'F' && x.order_id == e.order_id) =>
                {
                    cov.partial_cancels += 1;
                }
                (b'M', RefEffect::Reduce { .. }) => cov.modify_keep += 1,
                (
                    b'M',
                    RefEffect::Replace {
                        old_price,
                        new_price,
                        old_size,
                        new_size,
                        ..
                    },
                ) if old_price == new_price && old_size == new_size => {
                    cov.modify_refresh += 1;
                }
                (
                    b'M',
                    RefEffect::Replace {
                        old_price, new_price, ..
                    },
                ) if old_price == new_price => cov.modify_lose_size += 1,
                (b'M', RefEffect::Replace { .. }) => cov.modify_lose_price += 1,
                (b'M', RefEffect::None) => cov.modify_noop += 1,
                (b'M', RefEffect::Remove { .. }) => cov.modify_delete += 1,
                (b'R', RefEffect::Clear) if ev.len() > 1 && !prev_ts.is_empty() => cov.clears += 1,
                (b'T', _) if e.side == b'N' && !ev.iter().any(|x| x.action == b'F') => cov.hidden_prints += 1,
                (b'T', _) if e.side == b'N' => cov.trade_side_none_displayed += 1,
                (b'T', _) => t_prices.push(e.price),
                _ => {}
            }
        }
        if ev.len() == 2 && ev[0].action == b'C' && ev[1].action == b'A' && ev[0].order_id != ev[1].order_id {
            cov.replaces += 1;
        }
        t_prices.dedup();
        if t_prices.len() >= 2 {
            cov.multi_level_sweeps += 1;
        }
        if has_t && ev.iter().any(|x| x.action == b'F') {
            let p = ev
                .iter()
                .find(|x| x.action == b'F')
                .map(|x| x.price)
                .unwrap_or_default();
            let side = ev
                .iter()
                .find(|x| x.action == b'F')
                .map(|x| BookSide::from_byte(x.side));
            if let Some(Some(s)) = side
                && book.level(s, p).1 == 0
            {
                cov.exact_level_clears += 1;
            }
        }
        if book.best(BookSide::Bid).is_none() || book.best(BookSide::Ask).is_none() {
            cov.empty_side_events += 1;
        }
    }
    cov
}

#[test]
fn generator_covers_every_named_action() {
    let mut per_venue = Vec::new();
    for venue in VENUES {
        let mut total = Coverage::default();
        for seed in 1..=5 {
            let c = coverage(&generate(&cfg(venue, seed)));
            total.adds += c.adds;
            total.full_cancels += c.full_cancels;
            total.partial_cancels += c.partial_cancels;
            total.modify_keep += c.modify_keep;
            total.modify_lose_size += c.modify_lose_size;
            total.modify_lose_price += c.modify_lose_price;
            total.modify_noop += c.modify_noop;
            total.modify_refresh += c.modify_refresh;
            total.modify_delete += c.modify_delete;
            total.replaces += c.replaces;
            total.multi_level_sweeps += c.multi_level_sweeps;
            total.exact_level_clears += c.exact_level_clears;
            total.remainder_rests += c.remainder_rests;
            total.hidden_prints += c.hidden_prints;
            total.nonprinted_execs += c.nonprinted_execs;
            total.off_price += c.off_price;
            total.clears += c.clears;
            total.snapshot_readds += c.snapshot_readds;
            total.zero_gap_events += c.zero_gap_events;
            total.empty_side_events += c.empty_side_events;
            total.trade_side_none_displayed += c.trade_side_none_displayed;
        }
        per_venue.push((venue, total));
    }
    for (venue, c) in &per_venue {
        let msg = format!("{venue}: {c:?}");
        assert!(c.adds > 0 && c.full_cancels > 0 && c.zero_gap_events > 0, "{msg}");
        assert!(c.exact_level_clears > 0 && c.remainder_rests > 0, "{msg}");
        match venue {
            Venue::Cme => {
                assert!(
                    c.modify_keep > 0 && c.modify_lose_size > 0 && c.modify_lose_price > 0,
                    "{msg}"
                );
                assert!(
                    c.modify_refresh > 0 && c.multi_level_sweeps > 0 && c.clears > 0,
                    "{msg}"
                );
                assert_eq!(c.modify_noop, 0, "{msg}: audit A6, a same-size M loses priority on CME");
                assert_eq!(c.hidden_prints + c.partial_cancels + c.replaces, 0, "{msg}");
            }
            Venue::Nasdaq => {
                assert!(c.partial_cancels > 0 && c.replaces > 0 && c.hidden_prints > 0, "{msg}");
                assert!(c.nonprinted_execs > 0 && c.off_price > 0, "{msg}");
                assert_eq!(c.modify_keep + c.clears, 0, "{msg}");
            }
            _ => {
                assert!(
                    c.partial_cancels > 0 && c.modify_keep > 0 && c.modify_lose_price > 0,
                    "{msg}"
                );
                assert!(
                    c.modify_delete > 0 && c.hidden_prints > 0 && c.snapshot_readds > 0,
                    "{msg}"
                );
                assert!(c.trade_side_none_displayed > 0 && c.clears > 0, "{msg}");
            }
        }
    }
}

#[test]
fn generator_data_quality_verdicts() {
    for venue in VENUES {
        let out = generate(&cfg(venue, 3));
        for inst in &out.instruments {
            let mut t = DqTracker::new(rules(venue));
            for e in out.events.iter().filter(|e| e.instrument_id == inst.instrument_id) {
                t.push(e);
            }
            let (dq, events) = t.finish();
            assert!(events > 0);
            assert_eq!(
                (dq.unknown_order_refs, dq.duplicate_adds, dq.crossed_books_after_last),
                (0, 0, 0)
            );
            assert_eq!(
                (dq.sequence_gaps, dq.sequence_regressions, dq.out_of_order_ts_event),
                (0, 0, 0)
            );
            // Hidden prints and off-price executions are INFO counters (E36(b)): every venue is clean.
            assert_eq!(dq.verdict, Verdict::Clean, "{:?}", dq.reasons);
            if venue == Venue::Nasdaq {
                assert!(dq.hidden_prints > 0 && dq.off_price_executions > 0, "{venue:?}");
            }
        }
    }
}

#[test]
fn generator_injected_corruption_counts() {
    for venue in VENUES {
        let out = generate(&cfg(venue, 5));
        let r = rules(venue);
        for (kind, n) in [
            (Corruption::UnknownCancel, 7),
            (Corruption::DuplicateAdd, 5),
            (Corruption::CrossedAdd, 4),
            (Corruption::Modify, 3),
        ] {
            let inj = corrupt(&out.events, r, kind, n, 99);
            assert_eq!(inj.injected, n, "{venue} {kind:?}");
            let e = inj.expected;
            let want = match kind {
                Corruption::UnknownCancel => e.unknown_order_refs,
                Corruption::DuplicateAdd => e.duplicate_adds,
                Corruption::CrossedAdd => e.crossed_books_after_last,
                Corruption::Modify if !r.modify_records_present => e.unexpected_modifies,
                Corruption::Modify => n,
            };
            assert_eq!(want, n, "{venue} {kind:?}: {e:?}");
            let others = e.unknown_order_refs + e.duplicate_adds + e.crossed_books_after_last + e.unexpected_modifies;
            assert_eq!(
                others,
                if kind == Corruption::Modify && r.modify_records_present {
                    0
                } else {
                    n
                }
            );
            let mut seqs = SeqTracker::default();
            assert!(inj.events.iter().all(|x| seqs.check(x) == SeqCheck::Ok));
        }
    }
}

#[test]
fn generate_sessions_two_days() {
    for venue in VENUES {
        let mut c = cfg(venue, 2);
        c.events_per_instrument = 400;
        let out = generate_sessions(&c, 2);
        assert_eq!(out.instruments.len(), 4);
        let d0 = local_date(venue, c.ts_start);
        let dates: Vec<i32> = out.instruments.iter().map(|i| i.session_date).collect();
        assert_eq!(dates, vec![d0, d0, d0 + 1, d0 + 1]);
        let first_day1 = out
            .events
            .iter()
            .find(|e| local_date(venue, e.ts_event) == d0 + 1)
            .map(|e| e.ts_event);
        let off0 = c.ts_start - calendar::local_midnight(venue, d0);
        assert_eq!(
            first_day1.map(|t| t - calendar::local_midnight(venue, d0 + 1) >= off0),
            Some(true)
        );
        let one = generate(&c);
        assert_eq!(&out.events[..one.events.len()], &one.events[..]);
        // Order ids of the two days never collide.
        let ids = |d: i32| -> std::collections::BTreeSet<u64> {
            out.events
                .iter()
                .filter(|e| e.order_id != 0 && local_date(venue, e.ts_event) == d)
                .map(|e| e.order_id)
                .collect()
        };
        assert!(ids(d0).is_disjoint(&ids(d0 + 1)));
        assert!(
            out.events.iter().all(
                |e| e.ts_event - calendar::local_midnight(venue, local_date(venue, e.ts_event)) < 86_400 * NS_PER_S
            )
        );
    }
}
