//! Property tests of the IEX DEEP+ reader on synthetic packets:
//! - no input (mutated or truncated captures, arbitrary bytes, arbitrary messages) makes it panic;
//! - sequencing: duplicates and overlaps are dropped, every message is delivered once and in order, gaps are counted
//!   exactly;
//! - priority: the queue at every price level rebuilt from the emitted records (`A` joins the back, a partial `C`
//!   keeps the place, `C` of the remainder leaves) equals IEX's own queue (Rule 11.220: a maintain-priority size
//!   decrease keeps the place, a reset moves the order to the back, executions keep it) after every message.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::io::Cursor;

use proptest::prelude::*;
use qt_ingest::iex::synth::{Segment, frame, frames, message_bytes, pcap, pcapng, segment_bytes};
use qt_ingest::iex::{CaptureReader, FeedEvent, FeedReader, MapOutput, Mapper, Message, MsgCtx, parse, symbol};
use qt_types::MboEvent;

const T0: i64 = 1_791_552_600_000_000_000;

fn walk(bytes: Vec<u8>) {
    let mut mapper = Mapper::new(None, -9).unwrap();
    let mut out = MapOutput::default();
    let mut feed = FeedReader::new(CaptureReader::new(Cursor::new(bytes)));
    let _ = feed.for_each(|ev| {
        if let FeedEvent::Message(m) = ev {
            match parse(m.data) {
                Ok(msg) => mapper.apply(
                    MsgCtx {
                        seq: m.seq,
                        channel: 1,
                        send_time: m.send_time,
                    },
                    &msg,
                    &mut out,
                )?,
                Err(_) => mapper.note_unparsed(true),
            }
        }
        Ok(())
    });
    mapper.finish();
}

fn sample_segments() -> Vec<Segment> {
    let s = symbol("SPY");
    let msgs = |base: i64| -> Vec<Vec<u8>> {
        vec![
            message_bytes(&Message::AddOrder {
                side: b'8',
                ts: T0 + base,
                symbol: s,
                order_id: base,
                size: 100,
                price: 5_000_000,
            }),
            message_bytes(&Message::OrderModify {
                flags: 1,
                ts: T0 + base + 1,
                symbol: s,
                order_id: base,
                size: 50,
                price: 5_000_000,
            }),
            message_bytes(&Message::OrderExecuted {
                sale_flags: 0,
                ts: T0 + base + 2,
                symbol: s,
                order_id: base,
                size: 20,
                price: 5_000_000,
                trade_id: 1,
            }),
        ]
    };
    (0..4)
        .map(|k| Segment {
            channel: 1,
            session: 9,
            first_seq: 1 + 3 * k,
            send_time: T0 + 1_000 * k,
            capture_ns: T0 + 1_000 * k + 10,
            messages: msgs(10 * k + 1),
        })
        .collect()
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 400, ..ProptestConfig::default() })]

    #[test]
    fn mutated_captures_never_panic(
        ng in any::<bool>(),
        cut in 0usize..1200,
        flips in prop::collection::vec((0usize..1200, any::<u8>()), 0..12),
    ) {
        let f = frames(&sample_segments());
        let mut bytes = if ng { pcapng(&f) } else { pcap(&f) };
        for (i, b) in flips {
            if let Some(x) = bytes.get_mut(i) {
                *x = b;
            }
        }
        bytes.truncate(cut.max(1));
        walk(bytes);
    }

    #[test]
    fn arbitrary_bytes_never_panic(bytes in prop::collection::vec(any::<u8>(), 0..600), ng in any::<bool>()) {
        walk(bytes.clone());
        // Arbitrary IEX-TP datagrams inside a valid capture, and arbitrary message data.
        walk(if ng { pcapng(&[(T0, frame(&bytes))]) } else { pcap(&[(T0, frame(&bytes))]) });
        let mut mapper = Mapper::new(None, -12).unwrap();
        let mut out = MapOutput::default();
        for chunk in bytes.chunks(41) {
            if let Ok(m) = parse(chunk) {
                mapper.apply(MsgCtx { seq: 1, channel: 1, send_time: 0 }, &m, &mut out).unwrap();
            }
        }
    }

    /// Segments delivered in arbitrary order with duplicates: each sequence number at most once, increasing, and
    /// the gap counters equal the holes the stream skipped over.
    #[test]
    fn sequencing_drops_duplicates_and_counts_gaps(
        order in prop::collection::vec((1i64..40, 1usize..5), 1..30),
    ) {
        let s = symbol("SPY");
        let segs: Vec<Segment> = order
            .iter()
            .enumerate()
            .map(|(k, &(first, n))| Segment {
                channel: 1,
                session: 9,
                first_seq: first,
                send_time: T0 + k as i64,
                capture_ns: T0 + k as i64,
                messages: (0..n)
                    .map(|j| message_bytes(&Message::SecurityEvent { event: b'O', ts: first + j as i64, symbol: s }))
                    .collect(),
            })
            .collect();
        let mut feed = FeedReader::new(CaptureReader::new(Cursor::new(pcap(&frames(&segs)))));
        let mut got = Vec::new();
        feed.for_each(|ev| {
            if let FeedEvent::Message(m) = ev {
                // The message's own payload carries its expected number (the Timestamp field above).
                if let Ok(Message::SecurityEvent { ts, .. }) = parse(m.data) {
                    assert_eq!(ts as u64, m.seq);
                }
                got.push(m.seq);
            }
            Ok(())
        })
        .unwrap();
        // Model: the next expected number starts at the first segment's first number and only moves forward.
        let mut next = order[0].0 as u64;
        let (mut want, mut dups, mut gaps, mut missing) = (Vec::new(), 0u64, 0u64, 0u64);
        for &(first, n) in &order {
            let first = first as u64;
            if first > next {
                gaps += 1;
                missing += first - next;
            }
            for q in first..first + n as u64 {
                if q < next {
                    dups += 1;
                } else {
                    want.push(q);
                    next = q + 1;
                }
            }
        }
        prop_assert_eq!(&got, &want);
        prop_assert!(got.windows(2).all(|w| w[0] < w[1]));
        let st = &feed.stats;
        prop_assert_eq!((st.duplicate_messages, st.sequence_gaps, st.sequence_gap_messages), (dups, gaps, missing));
        prop_assert_eq!(st.messages, want.len() as u64);
    }
}

#[derive(Clone, Copy, Debug)]
enum Op {
    Add { side_buy: bool, size: u32, tick: i64 },
    Maintain { pick: usize, cut: u32 },
    Reset { pick: usize, size: u32, tick: i64 },
    Exec { pick: usize, size: u32 },
    Delete { pick: usize },
}

fn op() -> impl Strategy<Value = Op> {
    prop_oneof![
        3 => (any::<bool>(), 1u32..500, 0i64..4).prop_map(|(b, s, t)| Op::Add { side_buy: b, size: s, tick: t }),
        2 => (any::<usize>(), 0u32..300).prop_map(|(p, c)| Op::Maintain { pick: p, cut: c }),
        2 => (any::<usize>(), 1u32..500, 0i64..4).prop_map(|(p, s, t)| Op::Reset { pick: p, size: s, tick: t }),
        2 => (any::<usize>(), 1u32..300).prop_map(|(p, s)| Op::Exec { pick: p, size: s }),
        1 => any::<usize>().prop_map(|p| Op::Delete { pick: p }),
    ]
}

type Queues = BTreeMap<(u8, i64), VecDeque<(u64, u64)>>;

/// IEX's book: per (side, price) the FIFO of (order id, displayed size).
#[derive(Default)]
struct IexBook {
    q: Queues,
    live: HashMap<u64, (u8, i64)>,
}

impl IexBook {
    fn remove(&mut self, id: u64) -> Option<(u8, i64, u64)> {
        let (side, px) = self.live.remove(&id)?;
        let q = self.q.get_mut(&(side, px))?;
        let pos = q.iter().position(|o| o.0 == id)?;
        let (_, size) = q.remove(pos)?;
        if q.is_empty() {
            self.q.remove(&(side, px));
        }
        Some((side, px, size))
    }
    fn push(&mut self, id: u64, side: u8, px: i64, size: u64) {
        self.q.entry((side, px)).or_default().push_back((id, size));
        self.live.insert(id, (side, px));
    }
    fn set_size(&mut self, id: u64, size: u64) {
        let (side, px) = self.live[&id];
        let q = self.q.get_mut(&(side, px)).unwrap();
        q.iter_mut().find(|o| o.0 == id).unwrap().1 = size;
    }
    fn size(&self, id: u64) -> u64 {
        let (side, px) = self.live[&id];
        self.q[&(side, px)].iter().find(|o| o.0 == id).unwrap().1
    }
}

/// The book rebuilt from qt records with Nasdaq-semantics queue rules.
fn apply_records(book: &mut IexBook, recs: &[MboEvent]) {
    for e in recs {
        match e.action {
            b'A' => book.push(e.order_id, e.side, e.price, e.size),
            b'C' => {
                let left = book.size(e.order_id) - e.size;
                if left == 0 {
                    book.remove(e.order_id);
                } else {
                    book.set_size(e.order_id, left);
                }
            }
            b'R' => *book = IexBook::default(),
            _ => {}
        }
    }
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 300, ..ProptestConfig::default() })]

    #[test]
    fn records_reproduce_iex_queue_priority(ops in prop::collection::vec(op(), 1..120)) {
        let s = symbol("SPY");
        let mut mapper = Mapper::new(None, -9).unwrap();
        let mut out = MapOutput::default();
        let mut iex = IexBook::default();
        let mut rebuilt = IexBook::default();
        let mut ids: Vec<u64> = Vec::new();
        let mut next_id = 1u64;
        let px = |tick: i64| 1_000_000 + tick * 100; // $100.00 + tick cents (4 decimals)
        let qpx = |raw: i64| raw * 100_000; // at price_exp -9
        for (k, op) in ops.into_iter().enumerate() {
            let ts = T0 + k as i64;
            let pick = |p: usize| (!ids.is_empty()).then(|| ids[p % ids.len()]);
            let msg = match op {
                Op::Add { side_buy, size, tick } => {
                    let id = next_id;
                    next_id += 1;
                    ids.push(id);
                    let side = if side_buy { b'B' } else { b'A' };
                    iex.push(id, side, qpx(px(tick)), u64::from(size));
                    Message::AddOrder { side: if side_buy { b'8' } else { b'5' }, ts, symbol: s, order_id: id as i64, size, price: px(tick) }
                }
                Op::Maintain { pick: p, cut } => {
                    let Some(id) = pick(p) else { continue };
                    let (_, price) = iex.live[&id];
                    let cur = iex.size(id);
                    let new = cur.saturating_sub(u64::from(cut)).max(1);
                    iex.set_size(id, new); // Rule 11.220(a)(3): a decrement keeps priority
                    Message::OrderModify { flags: 1, ts, symbol: s, order_id: id as i64, size: new as u32, price: price / 100_000 }
                }
                Op::Reset { pick: p, size, tick } => {
                    let Some(id) = pick(p) else { continue };
                    let (side, _, _) = iex.remove(id).unwrap();
                    iex.push(id, side, qpx(px(tick)), u64::from(size)); // new timestamp: back of the queue
                    Message::OrderModify { flags: 0, ts, symbol: s, order_id: id as i64, size, price: px(tick) }
                }
                Op::Exec { pick: p, size } => {
                    let Some(id) = pick(p) else { continue };
                    let (_, price) = iex.live[&id];
                    let cur = iex.size(id);
                    let x = u64::from(size).min(cur);
                    if x == cur {
                        iex.remove(id);
                        ids.retain(|&i| i != id);
                    } else {
                        iex.set_size(id, cur - x);
                    }
                    Message::OrderExecuted { sale_flags: 0, ts, symbol: s, order_id: id as i64, size: x as u32, price: price / 100_000, trade_id: k as i64 }
                }
                Op::Delete { pick: p } => {
                    let Some(id) = pick(p) else { continue };
                    iex.remove(id);
                    ids.retain(|&i| i != id);
                    Message::OrderDelete { ts, symbol: s, order_id: id as i64 }
                }
            };
            mapper.apply(MsgCtx { seq: k as u64 + 1, channel: 1, send_time: ts }, &msg, &mut out).unwrap();
            prop_assert!(out.mbo.iter().all(|e| e.action != b'M'), "iex records carry no M");
            apply_records(&mut rebuilt, &out.mbo);
            prop_assert_eq!(&rebuilt.q, &iex.q, "after op {} {:?}", k, op);
        }
        let st = mapper.finish();
        prop_assert_eq!(st.anomalies.unknown_order_ref + st.anomalies.modify_maintain_unrepresentable, 0);
    }
}

#[test]
fn segment_bytes_are_a_valid_capture() {
    // A sanity anchor for the generators above: the sample capture maps without anomalies.
    let segs = sample_segments();
    assert_eq!(segment_bytes(&segs[0]).len(), 40 + 3 * 2 + 38 + 38 + 46);
    for ng in [false, true] {
        let f = frames(&segs);
        let bytes = if ng { pcapng(&f) } else { pcap(&f) };
        let mut feed = FeedReader::new(CaptureReader::new(Cursor::new(bytes)));
        let mut n = 0;
        feed.for_each(|ev| {
            n += u64::from(matches!(ev, FeedEvent::Message(_)));
            Ok(())
        })
        .unwrap();
        assert_eq!(n, 12);
        assert_eq!(feed.stats.sequence_gaps + feed.stats.duplicate_messages, 0);
        assert_eq!(feed.capture_stats().packets, 4);
    }
}
