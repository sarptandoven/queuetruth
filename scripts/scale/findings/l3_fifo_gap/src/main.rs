//! Real-data attribution of the `hbt_l3_fifo` gap (`scripts/scale/findings/l3-fifo-gap.md`), one instrument per run.
//!
//! Runs the faithful mode (hftbacktest's rules) and the corrected mode (equal to the exact replay, O-ZOO-4) of
//! `qt_zoo::l3::L3Pair` side by side on one instrument's MBO records, for shadows of the `standard` placements
//! drawn every `every` events on a two-sided, uncrossed book, each live 300 s. At the corrected mode's first fill it
//! records what the event held, and names the cause of a fill the faithful mode lacks.
//!
//! Input: the records `dump_mbo.py` writes (48 bytes each, little endian: ts_event i64, price i64, size u64,
//! order_id u64, sequence u64, action u8, side u8, flags u8, 5 bytes of padding), in dataset order.
//!
//! Usage: l3-fifo-gap <records.bin> <tick> <start_ns> <end_ns> <every> <seed> <out.jsonl> <symbol>
//!
//! Output: one JSON line per shadow with its side, price, touch offset, live time, end, the first fill of each mode
//! (`[ts, reason]` or null) and the cause of a corrected fill (empty when it did not fill).
use std::fmt::Write as _;
use std::io::{Read, Write};

use qt_book::{Book, Effect};
use qt_types::{Action, BookSide, MboEvent, Px, RuleSet, Ts, Xoshiro256StarStar, rules_for};
use qt_zoo::Mode;
use qt_zoo::l3::{L3Pair, L3Reason};

const S: Ts = 1_000_000_000;

/// A displayed execution: resting side and price, whether that price was the feed best on the side, and that best.
type Exec = (BookSide, Px, bool, Option<Px>);
const HMAX: Ts = 300 * S;
const PLACEMENTS: [(i64, f64); 4] = [(0, 0.6), (-1, 0.2), (-2, 0.1), (1, 0.1)];

fn worse(side: BookSide, px: Px, than: Px) -> bool {
    match side {
        BookSide::Bid => px < than,
        BookSide::Ask => px > than,
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Emptied {
    NotYet,
    Executed,
    Cancelled,
}

struct Live {
    id: usize,
    side: BookSide,
    px: Px,
    offset: i64,
    ts_live: Ts,
    deadline: Ts,
    pair: [Option<L3Pair>; 2],
    first: [Option<(Ts, L3Reason)>; 2],
    had_orders_at_live: bool,
    emptied: Emptied,
    emptied_ts: Ts,
    cause: String,
    best_gap_ticks: i64,
}

fn reason(r: L3Reason) -> &'static str {
    match r {
        L3Reason::Queue => "queue",
        L3Reason::TradeThrough => "trade_through",
        L3Reason::Crossed => "crossed",
    }
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (path, tick, rs, re, every, seed, out, sym) = (
        &a[1],
        a[2].parse::<Px>().unwrap(),
        a[3].parse::<Ts>().unwrap(),
        a[4].parse::<Ts>().unwrap(),
        a[5].parse::<usize>().unwrap(),
        a[6].parse::<u64>().unwrap(),
        &a[7],
        &a[8],
    );
    let mut buf = Vec::new();
    std::fs::File::open(path).unwrap().read_to_end(&mut buf).unwrap();
    let rd = |b: &[u8], o: usize| u64::from_le_bytes(b[o..o + 8].try_into().unwrap());
    let events: Vec<MboEvent> = buf
        .chunks_exact(48)
        .map(|c| MboEvent {
            ts_event: rd(c, 0) as i64,
            ts_recv: rd(c, 0) as i64,
            instrument_id: 1,
            price: rd(c, 8) as i64,
            size: rd(c, 16),
            order_id: rd(c, 24),
            sequence: rd(c, 32),
            action: c[40],
            side: c[41],
            flags: c[42],
            channel_id: 0,
            publisher_id: 0,
        })
        .collect();
    let mut rng = Xoshiro256StarStar::new(seed);
    let mut unit = move || (rng.next_u64() >> 11) as f64 / (1u64 << 53) as f64;
    let mut book = Book::new(rules_for(RuleSet::Iex));
    let mut live: Vec<Live> = Vec::new();
    let mut done: Vec<Live> = Vec::new();
    let mut at_start = true;
    let mut n_events = 0usize;
    let mut prev_ts: Ts = i64::MIN;
    let mut next_id = 0usize;
    // records of the current event with, for a Fill, (resting side, resting px, at feed best before the record)
    let mut ev: Vec<(MboEvent, Option<Exec>)> = Vec::new();
    let mut filled_in_event: Vec<u64> = Vec::new();
    for e in &events {
        if at_start {
            at_start = false;
            n_events += 1;
            let open = match (book.best(BookSide::Bid), book.best(BookSide::Ask)) {
                (Some(b), Some(a)) => b.price < a.price,
                _ => false,
            };
            // activate after the previous event, before this one (hbt issues the order after every row with
            // exch_ts <= ts_live, so this event must be strictly later)
            if n_events.is_multiple_of(every) && open && e.ts_event > prev_ts && prev_ts >= rs && prev_ts < re {
                let u = unit();
                let mut acc = 0.0;
                let mut off = PLACEMENTS[3].0;
                for (o, w) in PLACEMENTS {
                    acc += w;
                    if u < acc {
                        off = o;
                        break;
                    }
                }
                let side = if unit() < 0.5 { BookSide::Bid } else { BookSide::Ask };
                let b = book.best(side).unwrap().price;
                let o = book.best(side.opposite()).unwrap().price;
                let px = match side {
                    BookSide::Bid => b + off * tick,
                    BookSide::Ask => b - off * tick,
                };
                if off <= 0 || worse(side, px, o) {
                    let mk = |m| Some(L3Pair::new(side, px, 100, m, &book).with_tick(tick));
                    let had = book.level_total(side, px) > 0;
                    live.push(Live {
                        id: next_id,
                        side,
                        px,
                        offset: off,
                        ts_live: prev_ts,
                        deadline: prev_ts + HMAX,
                        pair: [mk(Mode::Faithful), mk(Mode::Corrected)],
                        first: [None, None],
                        had_orders_at_live: had,
                        emptied: Emptied::NotYet,
                        emptied_ts: 0,
                        cause: String::new(),
                        best_gap_ticks: 0,
                    });
                    next_id += 1;
                }
            }
        }
        let exec = (e.action == b'F')
            .then(|| book.order(e.order_id))
            .flatten()
            .map(|(s, p, _)| {
                let best = book.best(s).map(|l| l.price);
                (s, p, best == Some(p), best)
            });
        if e.action == b'F' {
            filled_in_event.push(e.order_id);
        }
        let removed = match e.action {
            b'C' => book.order(e.order_id).map(|(_, _, o)| (o.order_id, o.size == e.size)),
            _ => None,
        };
        let effect = book.apply(e);
        if matches!(effect, Effect::Clear) {
            for l in &mut live {
                l.deadline = l.deadline.min(e.ts_event - 1);
            }
        }
        ev.push((*e, exec));
        for l in &mut live {
            for p in l.pair.iter_mut().flatten() {
                p.on_record(e, &effect, &book, (None, None));
            }
            if l.emptied == Emptied::NotYet
                && let Effect::Remove { side, price, .. }
                | Effect::Replace {
                    side, old_price: price, ..
                } = effect
                && side == l.side
                && price == l.px
                && book.level_total(l.side, l.px) == 0
            {
                let by_exec = removed.is_some_and(|(id, _)| filled_in_event.contains(&id));
                l.emptied = if by_exec { Emptied::Executed } else { Emptied::Cancelled };
                l.emptied_ts = e.ts_event;
            }
        }
        if !e.is_last() {
            continue;
        }
        at_start = true;
        let ts = e.ts_event;
        prev_ts = ts;
        for l in &mut live {
            for m in 0..2 {
                let Some(p) = l.pair[m].as_mut() else { continue };
                let fills = p.on_event_end(&book);
                if l.first[m].is_none()
                    && let Some(&(_, r)) = fills.first()
                {
                    l.first[m] = Some((ts, r));
                    if m == 1 {
                        // what the corrected first-fill event held
                        let (mut disp_through, mut at_best, mut hidden_at, mut hidden_through) =
                            (false, true, false, false);
                        let mut gap = 0;
                        for (r, x) in &ev {
                            match (Action::from_byte(r.action), x) {
                                (Some(Action::Fill), Some((side, rpx, best, bpx))) => {
                                    if *side == l.side && worse(l.side, *rpx, l.px) && worse(l.side, r.price, l.px) {
                                        disp_through = true;
                                        at_best &= *best;
                                        if let Some(b) = bpx {
                                            gap = ((b - r.price) / tick).abs();
                                        }
                                    }
                                }
                                (Some(Action::Trade), _) if r.side == b'N' => {
                                    if r.price == l.px {
                                        hidden_at = true;
                                    } else if worse(l.side, r.price, l.px) {
                                        hidden_through = true;
                                    }
                                }
                                _ => {}
                            }
                        }
                        l.best_gap_ticks = gap;
                        l.cause = match r {
                            L3Reason::TradeThrough if disp_through && at_best => {
                                if l.offset > 0 {
                                    "R1 at feed best: placed +1".into()
                                } else if !l.had_orders_at_live && l.emptied == Emptied::NotYet {
                                    "R1 at feed best: placed at an empty level".into()
                                } else {
                                    let same = if l.emptied_ts == ts { "same ts" } else { "earlier" };
                                    format!("R1 at feed best: level emptied by {:?} ({same})", l.emptied)
                                }
                            }
                            L3Reason::TradeThrough if disp_through => "R1 below feed best: tick-range shortcut".into(),
                            L3Reason::TradeThrough if hidden_through => "R2 hidden print through the price".into(),
                            L3Reason::Queue if hidden_at => "R2 hidden print at the price".into(),
                            _ => format!("other {}", reason(r)),
                        };
                    }
                }
            }
        }
        ev.clear();
        filled_in_event.clear();
        let (keep, end): (Vec<Live>, Vec<Live>) = live.into_iter().partition(|l| l.deadline > ts);
        live = keep;
        done.extend(end);
    }
    done.extend(live);
    let mut f = std::fs::File::create(out).unwrap();
    let mut s = String::new();
    for l in &done {
        let fr = |m: usize| match l.first[m] {
            Some((t, r)) => format!("[{},\"{}\"]", t, reason(r)),
            None => "null".into(),
        };
        let _ = writeln!(
            s,
            "{{\"symbol\":\"{sym}\",\"id\":{},\"side\":\"{}\",\"px\":{},\"offset\":{},\"ts_live\":{},\"deadline\":{},\"faithful\":{},\"corrected\":{},\"cause\":\"{}\",\"gap\":{}}}",
            l.id,
            if l.side == BookSide::Bid { "B" } else { "A" },
            l.px,
            l.offset,
            l.ts_live,
            l.deadline,
            fr(0),
            fr(1),
            l.cause,
            l.best_gap_ticks
        );
    }
    f.write_all(s.as_bytes()).unwrap();
    eprintln!("{sym}: {} events, {} shadows", n_events, done.len());
}
