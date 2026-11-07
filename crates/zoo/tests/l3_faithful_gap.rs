//! Why faithful `hbt_l3_fifo` (hftbacktest `L3FIFOQueueModel` + `L3NoPartialFillExchange` at 5f3ec40) fills fewer
//! orders than the exact FIFO replay, which its corrected mode equals (O-ZOO-4). Both modes share the activation, the
//! price, the latency, the population and the "filled" definition (first fill by the deadline), so the gap is in the
//! fill rules alone. The faithful rules stay hftbacktest's; these tests pin which of them miss fills.
//!
//! 1. Trade-through is measured against the feed's best on the shadow's side, not against the shadow's price: an
//!    execution fills a backtest order only when its price is strictly worse than the feed's best bid (ask). On a
//!    feed that removes each executed order before the next execution (Nasdaq ITCH, IEX DEEP+: `T`, `F`, `C` of one
//!    message), the executed order is always at the feed best, so a shadow priced better than every feed order on its
//!    side (it improved the touch, or the orders ahead of it left) is never filled by trade-through; only a cross of
//!    the opposite side reaches it.
//! 2. Hidden (side `N`) prints are ignored, so the exact replay's fills from usable hidden prints at or through the
//!    price (Nasdaq and IEX, rulings A3/A3d) have no faithful counterpart.
//! 3. A GTX order that would cross at activation expires unfilled (exact: it takes liquidity).
//!
//! Whole-order fills change the filled quantity, never whether or when an order first fills, so they do not move the
//! fill rate. `fixture_day_attribution` buckets every fill the faithful mode misses on the synthetic Nasdaq days
//! (mechanisms 1 and 2; its activations are on uncrossed books only, so it does not measure mechanism 3).

use std::collections::BTreeMap;

use qt_book::{Book, Effect};
use qt_testkit::QueueMix;
use qt_testkit::fixtures::fixture_config;
use qt_types::{Action, BookSide, MboEvent, Px, Qty, Ts, Venue, Xoshiro256StarStar, flags, rules};
use qt_zoo::Mode;
use qt_zoo::l3::{L3Pair, L3Reason};

fn rec(action: u8, side: u8, price: Px, size: Qty, order_id: u64, seq: u64, last: bool) -> MboEvent {
    MboEvent {
        ts_event: seq as Ts,
        ts_recv: seq as Ts,
        instrument_id: 1,
        action,
        side,
        price,
        size,
        order_id,
        flags: if last { flags::LAST } else { 0 },
        sequence: seq,
        channel_id: 0,
        publisher_id: 0,
    }
}

/// Both modes on one shadow: `[faithful, corrected]`.
fn pairs(side: BookSide, px: Px, qty: Qty, book: &Book) -> [L3Pair; 2] {
    [
        L3Pair::new(side, px, qty, Mode::Faithful, book),
        L3Pair::new(side, px, qty, Mode::Corrected, book),
    ]
}

/// Applies one event (records in order) and returns each mode's event-end fills.
fn event(book: &mut Book, ps: &mut [L3Pair; 2], recs: &[MboEvent]) -> [Vec<(Qty, L3Reason)>; 2] {
    for e in recs {
        let effect = book.apply(e);
        for p in ps.iter_mut() {
            p.on_record(e, &effect, book, (None, None));
        }
    }
    [ps[0].on_event_end(book), ps[1].on_event_end(book)]
}

fn nasdaq_book() -> Book {
    Book::new(rules(Venue::Nasdaq))
}

/// Mechanism 1, shadow improving the touch: bid 101 above the feed's best bid 100. A seller executes 5 of the order
/// at 100 (`T` + `F` + `C` of one message). The exact replay fills the shadow by trade-through (the seller would
/// have met the better bid first); hbt does not, because the execution price is not below the feed's best bid.
#[test]
fn execution_at_the_feed_best_never_reaches_a_better_priced_shadow() {
    let mut book = nasdaq_book();
    book.apply(&rec(b'A', b'B', 100, 10, 1, 1, true));
    book.apply(&rec(b'A', b'A', 102, 10, 2, 2, true));
    let mut ps = pairs(BookSide::Bid, 101, 1, &book);
    let [f, c] = event(
        &mut book,
        &mut ps,
        &[
            rec(b'T', b'A', 100, 5, 0, 3, false),
            rec(b'F', b'B', 100, 5, 1, 3, false),
            rec(b'C', b'B', 100, 5, 1, 3, true),
        ],
    );
    assert!(f.is_empty(), "faithful: {f:?}");
    assert_eq!(c, vec![(1, L3Reason::TradeThrough)]);
}

/// Mechanism 1, sweep: shadow bid 100 behind o1 (5 @ 100), o3 (5 @ 99) below. A seller sweeps both levels; each
/// execution is its own message, so o1 is gone before o3 executes and the feed best is then 99 = the execution
/// price. The exact replay fills the shadow on o3's execution; hbt never does, although the shadow was skipped.
#[test]
fn a_sweep_through_the_shadow_level_does_not_fill_faithful() {
    let mut book = nasdaq_book();
    book.apply(&rec(b'A', b'B', 100, 5, 1, 1, true));
    book.apply(&rec(b'A', b'B', 99, 5, 3, 2, true));
    book.apply(&rec(b'A', b'A', 102, 10, 2, 3, true));
    let mut ps = pairs(BookSide::Bid, 100, 1, &book);
    // o1 is ahead: its execution fills neither mode
    let [f, c] = event(
        &mut book,
        &mut ps,
        &[
            rec(b'T', b'A', 100, 5, 0, 4, false),
            rec(b'F', b'B', 100, 5, 1, 4, false),
            rec(b'C', b'B', 100, 5, 1, 4, true),
        ],
    );
    assert!(f.is_empty() && c.is_empty(), "{f:?} {c:?}");
    let [f, c] = event(
        &mut book,
        &mut ps,
        &[
            rec(b'T', b'A', 99, 5, 0, 5, false),
            rec(b'F', b'B', 99, 5, 3, 5, false),
            rec(b'C', b'B', 99, 5, 3, 5, true),
        ],
    );
    assert!(f.is_empty(), "faithful: {f:?}");
    assert_eq!(c, vec![(1, L3Reason::TradeThrough)]);
}

/// Mechanism 1, the hbt rule itself: with `F` before `C` in one event (o1 still on the book when o3 executes) the
/// execution at 99 is below the feed best 100 and hbt's `fill_bid_between(best_bid, exec + 1)` fills the shadow.
/// The faithful gap is the feed's record order, not a port error: the same sweep with the removals first misses.
#[test]
fn faithful_trade_through_fires_only_below_the_feed_best() {
    let mut book = nasdaq_book();
    book.apply(&rec(b'A', b'B', 100, 5, 1, 1, true));
    book.apply(&rec(b'A', b'B', 99, 5, 3, 2, true));
    book.apply(&rec(b'A', b'A', 102, 10, 2, 3, true));
    let mut ps = pairs(BookSide::Bid, 100, 1, &book);
    let [f, c] = event(
        &mut book,
        &mut ps,
        &[
            rec(b'F', b'B', 100, 5, 1, 4, false),
            rec(b'F', b'B', 99, 5, 3, 4, false),
            rec(b'C', b'B', 100, 5, 1, 4, false),
            rec(b'C', b'B', 99, 5, 3, 4, true),
        ],
    );
    assert_eq!(f, vec![(1, L3Reason::TradeThrough)]);
    assert_eq!(c, vec![(1, L3Reason::TradeThrough)]);
    // hbt's tick-range shortcut: a shadow above the feed best fills only when the execution is at least 3 ticks
    // below that best (`1 < best - (exec + 1)`); 1 or 2 ticks below it, only orders in [exec + 1, best] fill
    for (exec, want) in [(99, false), (98, false), (97, true)] {
        let mut book = nasdaq_book();
        book.apply(&rec(b'A', b'B', 100, 5, 1, 1, true));
        book.apply(&rec(b'A', b'B', exec, 5, 3, 2, true));
        let mut ps = pairs(BookSide::Bid, 101, 1, &book);
        let [f, c] = event(&mut book, &mut ps, &[rec(b'F', b'B', exec, 5, 3, 3, true)]);
        assert_eq!(!f.is_empty(), want, "exec {exec}: {f:?}");
        assert_eq!(c, vec![(1, L3Reason::TradeThrough)], "exec {exec}");
    }
}

/// Mechanism 2: on Nasdaq (and IEX, whose record semantics are Nasdaq's) a usable hidden print at the shadow's price
/// once no displayed order is ahead, or through it, fills the exact replay (ruling A3d); hbt ignores trade records.
#[test]
fn hidden_prints_fill_only_the_exact_replay() {
    for (px, want) in [(101, L3Reason::Queue), (100, L3Reason::TradeThrough)] {
        let mut book = nasdaq_book();
        book.apply(&rec(b'A', b'B', 99, 10, 1, 1, true));
        book.apply(&rec(b'A', b'A', 102, 10, 2, 2, true));
        let mut ps = pairs(BookSide::Bid, 101, 4, &book);
        let [f, c] = event(&mut book, &mut ps, &[rec(b'T', b'N', px, 3, 0, 3, true)]);
        assert!(f.is_empty(), "faithful at {px}: {f:?}");
        assert_eq!(c, vec![(3, want)], "hidden print at {px}");
    }
}

/// Whole-order fills: an execution behind the shadow fills faithful's whole order and the exact replay's executed
/// volume. Both count the shadow as filled from that event on (`p_fill` = 1, `truth_filled` = true), so the fill
/// rate compares like with like; only the filled quantity differs.
#[test]
fn whole_order_fills_do_not_change_the_first_fill() {
    let mut book = nasdaq_book();
    book.apply(&rec(b'A', b'A', 102, 10, 2, 1, true));
    let mut ps = pairs(BookSide::Bid, 100, 10, &book);
    book.apply(&rec(b'A', b'B', 100, 5, 1, 2, true));
    let [f, c] = event(
        &mut book,
        &mut ps,
        &[
            rec(b'T', b'A', 100, 3, 0, 3, false),
            rec(b'F', b'B', 100, 3, 1, 3, false),
            rec(b'C', b'B', 100, 3, 1, 3, true),
        ],
    );
    assert_eq!(f, vec![(10, L3Reason::Queue)]);
    assert_eq!(c, vec![(3, L3Reason::Queue)]);
}

// ---- fixture-day attribution -------------------------------------------------------------------------------------

const S: Ts = 1_000_000_000;
const H: Ts = 60 * S;
/// The `standard` population's placements (`scripts/repro/breadth.py` STD_PLACEMENTS): touch offset and weight.
const PLACEMENTS: [(i64, f64); 4] = [(0, 0.6), (-1, 0.2), (-2, 0.1), (1, 0.1)];
/// One shadow every this many events, its offset drawn from `PLACEMENTS` and its side at random.
const EVERY: usize = 10;

fn draw_offset(u: f64) -> i64 {
    let mut acc = 0.0;
    for (off, w) in PLACEMENTS {
        acc += w;
        if u < acc {
            return off;
        }
    }
    PLACEMENTS[PLACEMENTS.len() - 1].0
}

#[derive(Default, Clone, Copy)]
struct Seen {
    /// A displayed execution on the shadow's side strictly through its price (resting and execution price).
    disp_through: bool,
    /// The executed order of every such execution was at the feed best on that side when it executed.
    through_at_feed_best: bool,
    hidden_at: bool,
    hidden_through: bool,
}

/// A displayed execution's resting side and price, and whether it rested at the feed best of that side.
type Exec = (BookSide, Px, bool);

struct Live {
    side: BookSide,
    px: Px,
    offset: i64,
    deadline: Ts,
    pair: [Option<L3Pair>; 2],
    first: [Option<(Ts, L3Reason)>; 2],
    /// What the event of the corrected first fill held.
    seen: Seen,
}

fn worse(side: BookSide, px: Px, than: Px) -> bool {
    match side {
        BookSide::Bid => px < than,
        BookSide::Ask => px > than,
    }
}

fn touch_price(side: BookSide, offset: i64, tick: Px, book: &Book) -> Option<Px> {
    let b = book.best(side)?.price;
    let o = book.best(side.opposite())?.price;
    let px = match side {
        BookSide::Bid => b + offset * tick,
        BookSide::Ask => b - offset * tick,
    };
    // the zoo rejects a positive offset that would cross (sim.rs `resolve_price`)
    (offset <= 0 || worse(side, px, o)).then_some(px)
}

/// Runs both modes on the synthetic Nasdaq fixture days (`tests/fixtures/synth_small_nasdaq`, regenerated in
/// memory) for shadows of the `standard` placements, 60 s each, and returns per bucket the shadows filled by one mode
/// only within 60 s.
fn attribution() -> (BTreeMap<&'static str, usize>, usize, [usize; 2]) {
    let cfg = fixture_config(Venue::Nasdaq);
    let out = qt_testkit::generate_sessions_with(&cfg, 2, Some(QueueMix::REALISTIC));
    let mut by_inst: BTreeMap<u32, Vec<MboEvent>> = BTreeMap::new();
    for e in out.events {
        by_inst.entry(e.instrument_id).or_default().push(e);
    }
    let mut buckets: BTreeMap<&'static str, usize> = BTreeMap::new();
    let mut rng = Xoshiro256StarStar::new(7);
    let mut unit = move || (rng.next_u64() >> 11) as f64 / (1u64 << 53) as f64;
    let (mut n, mut filled) = (0usize, [0usize; 2]);
    for events in by_inst.values() {
        let mut book = Book::new(rules(Venue::Nasdaq));
        let mut live: Vec<Live> = Vec::new();
        let mut done: Vec<Live> = Vec::new();
        let mut at_start = true;
        let mut n_events = 0usize;
        let mut ev_recs: Vec<(MboEvent, Option<Exec>)> = Vec::new();
        for e in events {
            if at_start {
                at_start = false;
                n_events += 1;
                // activation points on a two-sided, uncrossed book only (the synthetic Nasdaq book is sometimes
                // crossed): no shadow here crosses at activation, so this harness does not measure mechanism 3
                let open = match (book.best(BookSide::Bid), book.best(BookSide::Ask)) {
                    (Some(b), Some(a)) => b.price < a.price,
                    _ => false,
                };
                if n_events.is_multiple_of(EVERY) && open {
                    let off = draw_offset(unit());
                    let side = if unit() < 0.5 { BookSide::Bid } else { BookSide::Ask };
                    if let Some(px) = touch_price(side, off, cfg.tick, &book) {
                        let mk = |m| Some(L3Pair::new(side, px, 1, m, &book).with_tick(cfg.tick));
                        live.push(Live {
                            side,
                            px,
                            offset: off,
                            deadline: e.ts_event + H,
                            pair: [mk(Mode::Faithful), mk(Mode::Corrected)],
                            first: [None, None],
                            seen: Seen::default(),
                        });
                    }
                }
            }
            // a displayed execution: the executed order's side and resting price, and whether it rests at the feed
            // best of that side (read before the record applies; a Fill leaves the book as it is)
            let exec = (e.action == b'F')
                .then(|| book.order(e.order_id))
                .flatten()
                .map(|(s, p, _)| (s, p, book.best(s).is_some_and(|l| l.price == p)));
            let effect = book.apply(e);
            if matches!(effect, Effect::Clear) {
                // administrative end in the zoo: nothing is scored past it
                for l in &mut live {
                    l.deadline = l.deadline.min(e.ts_event - 1);
                }
            }
            ev_recs.push((*e, exec));
            for l in &mut live {
                for p in l.pair.iter_mut().flatten() {
                    p.on_record(e, &effect, &book, (None, None));
                }
            }
            if !e.is_last() {
                continue;
            }
            at_start = true;
            let ts = e.ts_event;
            for l in &mut live {
                for m in 0..2 {
                    let Some(p) = l.pair[m].as_mut() else { continue };
                    let fills = p.on_event_end(&book);
                    if l.first[m].is_none()
                        && let Some(&(_, r)) = fills.first()
                    {
                        l.first[m] = Some((ts, r));
                        if m == 1 {
                            let mut s = Seen {
                                through_at_feed_best: true,
                                ..Seen::default()
                            };
                            for (r, exec) in &ev_recs {
                                match (Action::from_byte(r.action), exec) {
                                    (Some(Action::Fill), Some((side, rpx, at_best))) => {
                                        if *side == l.side && worse(l.side, *rpx, l.px) && worse(l.side, r.price, l.px)
                                        {
                                            s.disp_through = true;
                                            s.through_at_feed_best &= *at_best;
                                        }
                                    }
                                    (Some(Action::Trade), _) if r.side == b'N' => {
                                        if r.price == l.px {
                                            s.hidden_at = true;
                                        } else if worse(l.side, r.price, l.px) {
                                            s.hidden_through = true;
                                        }
                                    }
                                    _ => {}
                                }
                            }
                            l.seen = s;
                        }
                    }
                }
            }
            ev_recs.clear();
            let (keep, end): (Vec<Live>, Vec<Live>) = live.into_iter().partition(|l| l.deadline > ts);
            live = keep;
            done.extend(end);
        }
        done.extend(live);
        for l in done {
            n += 1;
            let by = |m: usize| l.first[m].is_some_and(|(t, _)| t <= l.deadline);
            let (f, c) = (by(0), by(1));
            filled[0] += f as usize;
            filled[1] += c as usize;
            let key = match (f, c) {
                (true, true) | (false, false) => continue,
                (false, true) => {
                    let (_, r) = l.first[1].unwrap();
                    let s = l.seen;
                    if r == L3Reason::TradeThrough && s.disp_through && s.through_at_feed_best {
                        // split by placement: at the execution the shadow is strictly better than the feed best either
                        // way (it rests above the executed order, which is at that best)
                        if l.offset > 0 {
                            "missed: execution at the feed best, placed +1 (improving the touch)"
                        } else {
                            "missed: execution at the feed best, placed at or behind the touch"
                        }
                    } else if r == L3Reason::TradeThrough && s.hidden_through && !s.disp_through {
                        "missed: hidden print through the price"
                    } else if r == L3Reason::Queue && s.hidden_at {
                        "missed: hidden print at the price"
                    } else {
                        "missed: other"
                    }
                }
                (true, false) => match l.first[0].unwrap().1 {
                    L3Reason::Queue => "extra: queue",
                    L3Reason::TradeThrough => "extra: trade-through",
                    L3Reason::Crossed => "extra: crossed",
                },
            };
            *buckets.entry(key).or_insert(0) += 1;
        }
    }
    (buckets, n, filled)
}

/// Every fill the faithful mode misses within 60 s on the synthetic Nasdaq days falls under mechanism 1 or 2 of the
/// module doc (mechanism 3 is not measured here: activations are on uncrossed books only). The counts are pinned:
/// docs/concepts/l2-model-zoo.md quotes them, so a change to the generator, the harness or either mode must update
/// both. Run with `--nocapture` for the table.
#[test]
fn fixture_day_attribution() {
    let (b, n, filled) = attribution();
    eprintln!(
        "shadows {n}, filled within 60 s: faithful {} ({:.2}%), exact {} ({:.2}%)",
        filled[0],
        100.0 * filled[0] as f64 / n as f64,
        filled[1],
        100.0 * filled[1] as f64 / n as f64
    );
    let missed: usize = b.iter().filter(|(k, _)| k.starts_with("missed")).map(|(_, v)| v).sum();
    let extra: usize = b.iter().filter(|(k, _)| k.starts_with("extra")).map(|(_, v)| v).sum();
    for (k, v) in &b {
        let base = if k.starts_with("missed") { missed } else { extra };
        eprintln!("  {k:<76} {v:>6}  ({:.1}%)", 100.0 * *v as f64 / base.max(1) as f64);
    }
    assert_eq!((n, filled), (5655, [3204, 3321]));
    let want: BTreeMap<&str, usize> = [
        ("extra: crossed", 1),
        ("extra: trade-through", 5),
        (
            "missed: execution at the feed best, placed +1 (improving the touch)",
            15,
        ),
        ("missed: execution at the feed best, placed at or behind the touch", 103),
        ("missed: hidden print at the price", 5),
    ]
    .into_iter()
    .collect();
    assert_eq!(b, want);
}
