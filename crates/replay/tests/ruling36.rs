//! E36 and audit minors 15 / 24 in the engine: the shared scenario fixtures
//! (`qt_testkit::cases`) give the hand-written expectations and must also equal the independent reference row for
//! row; a differential on generated streams with injected locked / crossed books and non-snapshot duplicate adds
//! (which the generator never produces) checks the same rules against the reference on random data; `FillRule::Et`
//! keeps the SPEC-as-written behavior.

use std::collections::BTreeMap;

use qt_replay::sinks::VecSink;
use qt_replay::{Engine, EngineConfig, EngineReport, FillRule};
use qt_schema::rows::{FillReason, PriceSpec, ShadowAmend, ShadowSource, ShadowSpec};
use qt_testkit::cases::{Case, ruling36_cases};
use qt_testkit::refbook::{DqTracker, NaiveBook};
use qt_testkit::{RefConfig, ShadowTruth, SynthConfig, generate, try_reference_shadows};
use qt_types::{BookSide, Instrument, MboEvent, Px, Venue, Xoshiro256StarStar, flags, rules};

fn engine_run(
    venue: Venue,
    events: &[MboEvent],
    insts: &[Instrument],
    shadows: &[ShadowSpec],
    amends: &[ShadowAmend],
    race: i64,
    rule: FillRule,
) -> (VecSink, Vec<(u32, EngineReport)>) {
    let mut by: BTreeMap<u32, Vec<MboEvent>> = BTreeMap::new();
    for e in events {
        by.entry(e.instrument_id).or_default().push(*e);
    }
    let mut out = VecSink::default();
    let mut reps = Vec::new();
    for (inst, ev) in by {
        let mut c = EngineConfig::new(rules(venue));
        c.race_window_ns = race;
        let rows: Vec<Instrument> = insts.iter().filter(|i| i.instrument_id == inst).cloned().collect();
        let sh: Vec<ShadowSpec> = shadows.iter().filter(|s| s.instrument_id == inst).cloned().collect();
        let ids: std::collections::BTreeSet<&str> = sh.iter().map(|s| s.shadow_id.as_str()).collect();
        let am = amends
            .iter()
            .filter(|a| ids.contains(a.shadow_id.as_str()))
            .cloned()
            .collect();
        let mut eng = Engine::new(c, &rows, sh, am).expect("engine");
        eng.set_fill_rule(rule);
        for e in &ev {
            eng.apply(e, &mut out).expect("apply");
        }
        reps.push((inst, eng.finish(&mut out).expect("finish")));
    }
    (out, reps)
}

/// Engine rows equal the reference rows of every shadow.
fn same_as_reference(got: &VecSink, truth: &[ShadowTruth]) -> Result<(), String> {
    if got.shadow_results.len() != truth.len() {
        return Err(format!(
            "{} results, reference {}",
            got.shadow_results.len(),
            truth.len()
        ));
    }
    for (r, fills, traj) in truth {
        let g = got
            .shadow_results
            .iter()
            .find(|g| g.shadow_id == r.shadow_id)
            .ok_or(format!("missing {}", r.shadow_id))?;
        if g != r {
            return Err(format!("result\n got {g:?}\nwant {r:?}"));
        }
        let gf: Vec<_> = got
            .shadow_fills
            .iter()
            .filter(|f| f.shadow_id == r.shadow_id)
            .cloned()
            .collect();
        if gf != *fills {
            return Err(format!("fills {}\n got {gf:?}\nwant {fills:?}", r.shadow_id));
        }
        let gt: Vec<_> = got
            .trajectory
            .iter()
            .filter(|t| t.shadow_id == r.shadow_id)
            .cloned()
            .collect();
        if gt != *traj {
            return Err(format!("trajectory {}\n got {gt:?}\nwant {traj:?}", r.shadow_id));
        }
    }
    Ok(())
}

#[test]
fn ruling36_cases_engine_matches_expectations_and_reference() {
    for c in ruling36_cases() {
        // No instrument rows: tick 1, UTC-day session (the reference default).
        let (got, reps) = engine_run(
            c.venue,
            &c.events,
            &[],
            &c.shadows,
            &[],
            c.race_window_ns,
            FillRule::Exact,
        );
        for want in &c.expected {
            let ctx = format!("{} / {}", c.name, want.shadow_id);
            let r = got
                .shadow_results
                .iter()
                .find(|r| r.shadow_id == want.shadow_id)
                .expect("result");
            let fills: Vec<_> = got
                .shadow_fills
                .iter()
                .filter(|f| f.shadow_id == want.shadow_id)
                .map(|f| (f.ts_event, f.fill_qty, f.reason, f.fill_px))
                .collect();
            assert_eq!(fills, want.fills, "{ctx}");
            assert_eq!(
                (r.end_state, r.ambiguous_race),
                (want.end_state, want.ambiguous_race),
                "{ctx}"
            );
        }
        let mut rc = RefConfig::new(rules(c.venue));
        rc.race_window_ns = c.race_window_ns;
        rc.dq_flags = true;
        let truth = try_reference_shadows(&c.events, &rc, &c.shadows, &[]).expect("reference");
        if let Err(m) = same_as_reference(&got, &truth) {
            panic!("{}: {m}", c.name);
        }
        let dq = &reps[0].1.data_quality;
        assert_eq!(
            dq.source_specific
                .get("removal_after_unpaired_fill")
                .copied()
                .unwrap_or(0),
            c.removal_after_unpaired_fill,
            "{}",
            c.name
        );
    }
}

#[test]
fn ruling36_diagnostics_and_et_rule_unchanged() {
    let cases = ruling36_cases();
    let get = |n: &str| cases.iter().find(|c| c.name == n).expect("case").clone();
    let inst = |_: &Case| Vec::<Instrument>::new();
    // 36a: Y's crossed_at_live finds no size beyond the priority volume (X at ts 40 and Y at 50 continue the same
    // crosses: one `crossed_unfilled` per cross, review m14).
    let a = get("a_locked_book_priority_before_shadow");
    let (_, reps) = engine_run(a.venue, &a.events, &inst(&a), &a.shadows, &[], 0, FillRule::Exact);
    let d = &reps[0].1.diagnostics;
    assert_eq!((d.taker_fills, d.crossed_unfilled), (1, 1), "{d:?}");
    // The et rule is SPEC 5.2.5 as written: the crossed book fills X's whole remainder at its price at ts 30.
    let (et, _) = engine_run(a.venue, &a.events, &inst(&a), &a.shadows, &[], 0, FillRule::Et);
    let x: Vec<_> = et
        .shadow_fills
        .iter()
        .filter(|f| f.shadow_id == "X")
        .map(|f| (f.ts_event, f.fill_qty, f.reason, f.fill_px))
        .collect();
    assert_eq!(x, vec![(30, 20, FillReason::Crossed, 100)]);
    // A3d: the prints declined at a shadow's price (XB at 50, XA at 90), and the fill events that used hidden volume
    // at the price (XI at 30, XJ at 40, XB at 80).
    let c = get("c_hidden_print_display_rule");
    let (_, reps) = engine_run(c.venue, &c.events, &inst(&c), &c.shadows, &[], 0, FillRule::Exact);
    let d = &reps[0].1.diagnostics;
    assert_eq!(
        (d.hidden_at_px_with_display_ahead, d.hidden_print_fills),
        (2, 3),
        "{d:?}"
    );
    // 36b: hidden prints are INFO counters.
    let dq = &reps[0].1.data_quality;
    assert_eq!(
        (dq.hidden_prints, dq.verdict),
        (6, qt_schema::json::Verdict::Clean),
        "{:?}",
        dq.reasons
    );
    // 36d under the et rule: the re-added order stays ahead (no change for et parity), so the lock gives X the
    // whole remainder anyway (et `crossed`), and the Nasdaq hidden print never fills (et ignores hidden prints).
    let n = get("d_duplicate_add_requeues_nasdaq");
    let (et, _) = engine_run(n.venue, &n.events, &inst(&n), &n.shadows, &[], 0, FillRule::Et);
    assert!(et.shadow_fills.is_empty());
}

// ---- differential with injected crosses and duplicate adds ----------------------------------------------------------

struct Injected {
    events: Vec<MboEvent>,
    shadows: Vec<ShadowSpec>,
    crosses: usize,
    dups: usize,
}

fn spec(id: String, inst: u32, side: BookSide, px: Px, qty: u64, ts: i64) -> ShadowSpec {
    ShadowSpec {
        shadow_id: id,
        instrument_id: inst,
        side,
        price: PriceSpec::Abs(px),
        qty,
        ts_submit: ts,
        latency_ns: 0,
        ts_cancel: None,
        group: None,
        source: ShadowSource::User,
        order_key: None,
    }
}

/// Inserts, after random event batches, single-record events: an order locking or crossing the opposite best (its
/// full cancel follows 1-6 batches later) or a duplicate add (no snapshot flag) of a resting order at its price
/// (sometimes one tick behind) with a new size; shadows are placed at the touch shortly before each injection and
/// at the injection time (live during the lock). Sequences are renumbered per channel, keeping record groups.
fn inject(venue: Venue, events: &[MboEvent], insts: &[Instrument], seed: u64) -> Injected {
    let mut r = Xoshiro256StarStar::new(seed ^ 0x36);
    let tick_of = |id: u32| {
        insts
            .iter()
            .find(|i| i.instrument_id == id)
            .map_or(1, |i| i.tick_size.max(1))
    };
    let mut books: BTreeMap<u32, NaiveBook> = BTreeMap::new();
    let mut pending: Vec<(u32, u64, usize, MboEvent)> = Vec::new(); // (inst, id, due batch, record)
    let mut out: Vec<(MboEvent, u64)> = Vec::new(); // (record, group key)
    let mut shadows = Vec::new();
    let (mut crosses, mut dups, mut next_id, mut batch, mut key) = (0, 0, 1u64 << 40, 0usize, 1u64 << 62);
    // `group`: false = the record continues the previous injected record's sequence group.
    let mut push_with =
        |out: &mut Vec<(MboEvent, u64)>, e: MboEvent, books: &mut BTreeMap<u32, NaiveBook>, group: bool| {
            books
                .entry(e.instrument_id)
                .or_insert_with(|| NaiveBook::new(rules(venue)))
                .apply(&e);
            if group {
                key += 1;
            }
            out.push((e, key));
        };
    let mut i = 0;
    while i < events.len() {
        let start = i;
        while i < events.len() && !events[i].is_last() {
            i += 1;
        }
        let end = (i + 1).min(events.len());
        i = end;
        for e in &events[start..end] {
            books
                .entry(e.instrument_id)
                .or_insert_with(|| NaiveBook::new(rules(venue)))
                .apply(e);
            out.push((*e, (u64::from(e.channel_id) << 48) | e.sequence));
        }
        batch += 1;
        let last = events[end - 1];
        let inst = last.instrument_id;
        let tpl = |action: u8, side: u8, price: Px, size: u64, order_id: u64| MboEvent {
            action,
            side,
            price,
            size,
            order_id,
            flags: flags::LAST,
            ..last
        };
        // Due cancels of injected crossing orders.
        let due: Vec<usize> = (0..pending.len())
            .filter(|&k| pending[k].0 == inst && pending[k].2 <= batch)
            .collect();
        for k in due.into_iter().rev() {
            let (_, id, _, rec) = pending.remove(k);
            if books.get(&inst).and_then(|b| b.order(id)).is_some() {
                push_with(&mut out, rec, &mut books, true);
            }
        }
        let roll = r.next_u64() % 100;
        let book = books
            .get(&inst)
            .cloned()
            .unwrap_or_else(|| NaiveBook::new(rules(venue)));
        let (bb, ba) = (book.best_px(BookSide::Bid), book.best_px(BookSide::Ask));
        let tick = tick_of(inst);
        let (Some(bb), Some(ba)) = (bb, ba) else {
            continue;
        };
        if batch < 20 {
            // Shadows submitted 1 ns before an injection must belong to the session's partition.
            continue;
        }
        if roll < 4 {
            // Lock or cross: an ask at or below the best bid, or a bid at or above the best ask.
            let ask = r.next_u64().is_multiple_of(2);
            let deep = (r.next_u64() % 2) as i64 * tick;
            let (px, sb) = if ask { (bb - deep, b'A') } else { (ba + deep, b'B') };
            let size = 1 + r.next_u64() % 300;
            next_id += 1;
            let ts = last.ts_event;
            // Shadows on the crossed side: resting before the cross (at the touch and one tick behind), and arriving
            // during it (crossed at live).
            let (shadow_side, touch) = if ask { (BookSide::Bid, bb) } else { (BookSide::Ask, ba) };
            let behind = if ask { bb - tick } else { ba + tick };
            for (k, (px_s, t)) in [(touch, ts - 1), (behind, ts - 1), (touch, ts), (px, ts)]
                .into_iter()
                .enumerate()
            {
                shadows.push(spec(
                    format!("c{inst}_{next_id}_{k}"),
                    inst,
                    shadow_side,
                    px_s,
                    1 + r.next_u64() % 200,
                    t,
                ));
            }
            push_with(&mut out, tpl(b'A', sb, px, size, next_id), &mut books, true);
            let due = batch + 1 + (r.next_u64() % 6) as usize;
            pending.push((inst, next_id, due, tpl(b'C', sb, px, size, next_id)));
            crosses += 1;
        } else if roll < 8 {
            // Duplicate add of a resting order near the touch (no snapshot flag).
            let side = if r.next_u64().is_multiple_of(2) {
                BookSide::Bid
            } else {
                BookSide::Ask
            };
            let levels = book.levels(side);
            if levels.is_empty() {
                continue;
            }
            let (lp, _, _) = levels[(r.next_u64() % levels.len().min(3) as u64) as usize];
            let orders = book.orders_at(side, lp);
            let o = orders[(r.next_u64() % orders.len() as u64) as usize];
            let shift = if r.next_u64().is_multiple_of(4) { tick } else { 0 };
            let px = match side {
                BookSide::Bid => lp - shift,
                BookSide::Ask => lp + shift,
            };
            let size = 1 + r.next_u64() % (2 * o.size.max(1));
            shadows.push(spec(
                format!("d{inst}_{}_{dups}", o.order_id),
                inst,
                side,
                lp,
                1 + r.next_u64() % 200,
                last.ts_event - 1,
            ));
            let sb = if side == BookSide::Bid { b'B' } else { b'A' };
            push_with(&mut out, tpl(b'A', sb, px, size, o.order_id), &mut books, true);
            dups += 1;
        } else if roll < 11 && rules(venue).modify_records_present {
            // Partial execution reported by `F` and the order's `M` in one event (CME priority after fills, A6b):
            // the new size is below, at or above the post-fill remainder.
            let side = if r.next_u64().is_multiple_of(2) {
                BookSide::Bid
            } else {
                BookSide::Ask
            };
            let Some(&(lp, _, _)) = book.levels(side).first() else {
                continue;
            };
            let orders = book.orders_at(side, lp);
            let o = orders[(r.next_u64() % orders.len() as u64) as usize];
            if o.size < 2 {
                continue;
            }
            let f = 1 + r.next_u64() % (o.size - 1);
            let new = 1 + r.next_u64() % o.size;
            shadows.push(spec(
                format!("m{inst}_{}_{dups}", o.order_id),
                inst,
                side,
                lp,
                1 + r.next_u64() % 200,
                last.ts_event - 1,
            ));
            let sb = if side == BookSide::Bid { b'B' } else { b'A' };
            let mut fill = tpl(b'F', sb, lp, f, o.order_id);
            fill.flags = 0;
            push_with(&mut out, fill, &mut books, true);
            push_with(&mut out, tpl(b'M', sb, lp, new, o.order_id), &mut books, false);
            dups += 1;
        }
    }
    // Renumber: per channel, a new number whenever the group key changes.
    let mut seqs: BTreeMap<u16, (u64, u64)> = BTreeMap::new();
    let events = out
        .into_iter()
        .map(|(mut e, k)| {
            let s = seqs.entry(e.channel_id).or_insert((u64::MAX, 0));
            if s.0 != k {
                s.0 = k;
                s.1 += 1;
            }
            e.sequence = s.1;
            e
        })
        .collect();
    Injected {
        events,
        shadows,
        crosses,
        dups,
    }
}

fn differential_case(venue: Venue, seed: u64) -> Result<(usize, usize, usize), String> {
    let mut cfg = SynthConfig::for_venue(venue, seed);
    cfg.events_per_instrument = 500;
    cfg.instruments = 1;
    let synth = generate(&cfg);
    let inj = inject(venue, &synth.events, &synth.instruments, seed);
    let (mut shadows, amends) = qt_testkit::fixtures::sample_shadows(&synth, 15, seed);
    shadows.extend(inj.shadows.iter().cloned());
    let mut rc = RefConfig::new(rules(venue));
    rc.instruments = synth.instruments.clone();
    rc.dq_flags = true;
    let truth = try_reference_shadows(&inj.events, &rc, &shadows, &amends).map_err(|e| e.to_string())?;
    let (got, reps) = engine_run(
        venue,
        &inj.events,
        &synth.instruments,
        &shadows,
        &amends,
        0,
        FillRule::Exact,
    );
    same_as_reference(&got, &truth)?;
    // Data quality: the book-level counters and the verdict agree with the reference tracker.
    for (inst, rep) in &reps {
        let mut t = DqTracker::new(rules(venue));
        for e in inj.events.iter().filter(|e| e.instrument_id == *inst) {
            t.push(e);
        }
        let (want, _) = t.finish();
        let g = &rep.data_quality;
        let key = |d: &qt_schema::json::DataQuality| {
            (
                d.duplicate_adds,
                d.crossed_books_after_last,
                d.hidden_prints,
                d.off_price_executions,
                d.source_specific
                    .get("removal_after_unpaired_fill")
                    .copied()
                    .unwrap_or(0),
                d.verdict,
            )
        };
        if key(g) != key(&want) {
            return Err(format!("data quality\n got {:?}\nwant {:?}", key(g), key(&want)));
        }
    }
    let taker = got
        .shadow_fills
        .iter()
        .filter(|f| matches!(f.reason, FillReason::Crossed | FillReason::CrossedAtLive))
        .count();
    Ok((inj.crosses, inj.dups, taker))
}

#[test]
fn ruling36_differential_with_injected_crosses_and_duplicates() {
    let mut totals = (0, 0, 0);
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for seed in 1..=40u64 {
            match differential_case(venue, seed) {
                Ok((c, d, t)) => {
                    totals.0 += c;
                    totals.1 += d;
                    totals.2 += t;
                }
                Err(m) => panic!("{venue:?} seed {seed}: {m}"),
            }
        }
    }
    // The injections actually exercise the rules.
    assert!(totals.0 > 300 && totals.1 > 300 && totals.2 > 300, "{totals:?}");
}
