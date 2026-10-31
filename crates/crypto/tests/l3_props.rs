//! Property tests for the 1.2 order-level converters:
//! - Kraken window model against a model publisher: random full-depth books and operations are published the way
//!   the level3 channel does (top-D window, no delete on window exit, whole level re-sent on re-entry, CRC32 over
//!   the top 10 levels); every checksum must verify and the replayed window must equal the publisher's view;
//! - replay consistency: for random (also nonsensical) streams of every source, the records never reference a
//!   missing order, never add a live order twice and never cancel more than is live;
//! - never panic: mutated and truncated fixture lines give `Ok` or `Err`.

mod common;

use std::collections::{BTreeMap, HashMap};

use common::{fixture, lines, replay};
use proptest::prelude::*;
use qt_crypto::{coinbase_l3, hyperliquid, kraken_l3};

// ---------------------------------------------------------------- Kraken model publisher

const D: usize = 10;

#[derive(Default)]
struct TrueBook {
    orders: HashMap<u32, (u8, i64, u64)>,
    fifo: [BTreeMap<i64, Vec<u32>>; 2],
    next: u32,
}

fn si(side: u8) -> usize {
    usize::from(side != b'B')
}

impl TrueBook {
    fn add(&mut self, side: u8, px: i64, qty: u64) -> u32 {
        self.next += 1;
        let id = self.next;
        self.orders.insert(id, (side, px, qty));
        self.fifo[si(side)].entry(px).or_default().push(id);
        id
    }

    fn remove(&mut self, id: u32) {
        if let Some((side, px, _)) = self.orders.remove(&id) {
            let q = self.fifo[si(side)].get_mut(&px).unwrap();
            q.retain(|x| *x != id);
            if q.is_empty() {
                self.fifo[si(side)].remove(&px);
            }
        }
    }

    /// Kraken amend: quantity down keeps the place, quantity up or a new price goes to the back.
    fn amend(&mut self, id: u32, qty: u64, px: i64) {
        let Some(&(side, opx, oq)) = self.orders.get(&id) else {
            return;
        };
        if px == opx && qty <= oq {
            self.orders.get_mut(&id).unwrap().2 = qty;
        } else {
            self.remove(id);
            self.orders.insert(id, (side, px, qty));
            self.fifo[si(side)].entry(px).or_default().push(id);
        }
    }

    /// Top-D levels per side: (side, px) -> ids in queue order.
    fn view(&self) -> Vec<(u8, i64, Vec<u32>)> {
        let mut v = Vec::new();
        for (side, rev) in [(b'B', true), (b'A', false)] {
            let lv = &self.fifo[si(side)];
            let ps: Vec<i64> = if rev {
                lv.keys().rev().copied().collect()
            } else {
                lv.keys().copied().collect()
            };
            for p in ps.into_iter().take(D) {
                v.push((side, p, lv[&p].clone()));
            }
        }
        v
    }

    fn checksum(&self) -> u32 {
        let v = self.view();
        let mut s = String::new();
        for want in *b"AB" {
            let mut lv: Vec<&(u8, i64, Vec<u32>)> = v.iter().filter(|x| x.0 == want).collect();
            lv.sort_by_key(|x| if want == b'A' { x.1 } else { -x.1 });
            for (_, p, ids) in lv.into_iter().take(10) {
                for id in ids {
                    s.push_str(&p.to_string());
                    s.push_str(&self.orders[id].2.to_string());
                }
            }
        }
        qt_crypto::l3util::crc32(s.as_bytes())
    }
}

fn kord(id: u32, px: i64, qty: u64, event: Option<&str>) -> String {
    let e = event.map(|e| format!(r#""event":"{e}","#)).unwrap_or_default();
    format!(r#"{{{e}"order_id":"K{id}","limit_price":{px},"order_qty":{qty},"timestamp":"2026-10-11T00:00:00Z"}}"#)
}

/// Final publisher view: (side, price, [(price, qty)] in queue order).
type View = Vec<(u8, i64, Vec<(i64, u64)>)>;
type Level = (u8, i64, Vec<(i64, u64)>);

/// Publishes `ops` (kind, a, b, c bytes) as a level3 capture; returns the lines and the final view.
fn publish(init: &[(bool, u8, u8)], ops: &[Vec<(u8, u8, u8, u8)>]) -> (Vec<String>, View) {
    let mut b = TrueBook::default();
    // bids at 101..=114, asks at 115..=130: never crossed
    let bpx = |x: u8| 101 + i64::from(x % 14);
    let apx = |x: u8| 115 + i64::from(x % 16);
    for &(bid, p, q) in init {
        if bid {
            b.add(b'B', bpx(p), 1 + u64::from(q % 5));
        } else {
            b.add(b'A', apx(p), 1 + u64::from(q % 5));
        }
    }
    let mut out = Vec::new();
    let snap = |b: &TrueBook| {
        let mut bids = Vec::new();
        let mut asks = Vec::new();
        for (side, p, ids) in b.view() {
            for id in ids {
                let o = kord(id, p, b.orders[&id].2, None);
                if side == b'B' { bids.push(o) } else { asks.push(o) }
            }
        }
        format!(
            r#"{{"channel":"level3","type":"snapshot","data":[{{"symbol":"P/Q","checksum":{},"bids":[{}],"asks":[{}],"timestamp":"2026-10-11T00:00:00Z"}}]}}"#,
            b.checksum(),
            bids.join(","),
            asks.join(",")
        )
    };
    out.push(snap(&b));
    for (k, step) in ops.iter().enumerate() {
        // events in engine order: each operation is diffed against the window on its own
        let mut ev: [Vec<String>; 2] = Default::default();
        for &(kind, x, y, z) in step {
            let before: HashMap<u32, (u8, i64, u64)> = b
                .view()
                .into_iter()
                .flat_map(|(s, p, ids)| ids.into_iter().map(move |i| (i, (s, p))))
                .map(|(i, (s, p))| (i, (s, p, b.orders[&i].2)))
                .collect();
            let live: Vec<u32> = {
                let mut v: Vec<u32> = b.orders.keys().copied().collect();
                v.sort_unstable();
                v
            };
            let pick = |n: u8| live.get(usize::from(n) % live.len().max(1)).copied();
            match kind % 5 {
                0 | 1 => {
                    if y % 2 == 0 {
                        b.add(b'B', bpx(x), 1 + u64::from(z % 5));
                    } else {
                        b.add(b'A', apx(x), 1 + u64::from(z % 5));
                    }
                }
                2 => {
                    if let Some(id) = pick(x) {
                        b.remove(id);
                    }
                }
                3 => {
                    if let Some(id) = pick(x) {
                        let (_, p, _) = b.orders[&id];
                        b.amend(id, 1 + u64::from(z % 5), p);
                    }
                }
                _ => {
                    if let Some(id) = pick(x) {
                        let (s, _, q) = b.orders[&id];
                        let np = if s == b'B' { bpx(y) } else { apx(y) };
                        b.amend(id, q, np);
                    }
                }
            }
            let after = b.view();
            let after_ids: HashMap<u32, (u8, i64)> = after
                .iter()
                .flat_map(|(s, p, ids)| ids.iter().map(move |i| (*i, (*s, *p))))
                .collect();
            let mut bk: Vec<(&u32, &(u8, i64, u64))> = before.iter().collect();
            bk.sort();
            for &(&i, &(s, p, q)) in &bk {
                let gone = !b.orders.contains_key(&i);
                let moved = b.orders.get(&i).is_some_and(|o| o.1 != p);
                if gone || moved {
                    ev[si(s)].push(kord(i, p, q, Some("delete")));
                }
            }
            for &(&i, &(s, p, q)) in &bk {
                if let (Some(&(_, ap)), Some(o)) = (after_ids.get(&i), b.orders.get(&i))
                    && ap == p
                    && o.2 != q
                {
                    ev[si(s)].push(kord(i, p, o.2, Some("modify")));
                }
            }
            for (s, p, ids) in &after {
                for i in ids {
                    if before.get(i).is_none_or(|x| x.1 != *p) {
                        ev[si(*s)].push(kord(*i, *p, b.orders[i].2, Some("add")));
                    }
                }
            }
        }
        if !ev[0].is_empty() || !ev[1].is_empty() {
            out.push(format!(
                r#"{{"channel":"level3","type":"update","data":[{{"symbol":"P/Q","checksum":{},"timestamp":"2026-10-11T00:00:{:02}.{:03}Z","bids":[{}],"asks":[{}]}}]}}"#,
                b.checksum(),
                (k / 1000) % 60,
                k % 1000,
                ev[0].join(","),
                ev[1].join(",")
            ));
        }
    }
    let view = b
        .view()
        .into_iter()
        .map(|(s, p, ids)| (s, p, ids.iter().map(|i| (p, b.orders[i].2)).collect()))
        .collect();
    (out, view)
}

fn kcfg() -> kraken_l3::Config {
    kraken_l3::Config {
        depth: Some(10),
        price_exp: 0,
        size_exp: 0,
        ..kraken_l3::Config::default()
    }
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 256, ..ProptestConfig::default() })]

    /// The converter's in-view book equals the model publisher's view after every message.
    #[test]
    fn kraken_window_model_matches_publisher(
        init in prop::collection::vec((any::<bool>(), any::<u8>(), any::<u8>()), 0..40),
        ops in prop::collection::vec(prop::collection::vec((any::<u8>(), any::<u8>(), any::<u8>(), any::<u8>()), 1..4), 0..60),
    ) {
        let (v, view) = publish(&init, &ops);
        let o = kraken_l3::convert_full(lines(&v), &kcfg()).unwrap();
        prop_assert_eq!(o.counters.get("checksum_mismatches").copied(), Some(0), "{:?}", o.counters);
        prop_assert_eq!(o.counters.get("checksums_verified").copied(), Some(v.len() as u64));
        let book = replay(&o.events).map_err(TestCaseError::fail)?;
        for (side, want) in [(b'B', true), (b'A', false)] {
            let mut w: Vec<&Level> = view.iter().filter(|x| x.0 == side).collect();
            w.sort_by_key(|x| if want { -x.1 } else { x.1 });
            let want: Vec<(i64, u64)> = w.into_iter().flat_map(|x| x.2.iter().copied()).collect();
            prop_assert_eq!(book.side(1, side, D), want);
        }
    }

    /// Random, partly nonsensical Kraken event streams still give replay-consistent records.
    #[test]
    fn kraken_records_replay_consistently(
        evs in prop::collection::vec((0u8..4, 0u8..12, 0u8..30, 1u8..5, any::<bool>()), 0..80),
    ) {
        let mut v = vec![format!(
            r#"{{"channel":"level3","type":"snapshot","data":[{{"symbol":"P/Q","bids":[{}],"asks":[],"timestamp":"2026-10-11T00:00:00Z"}}]}}"#,
            (0..12).map(|i| kord(i, 100 - i64::from(i), 1, None)).collect::<Vec<_>>().join(",")
        )];
        for (k, (kind, id, p, q, bid)) in evs.into_iter().enumerate() {
            let e = ["add", "modify", "delete", "add"][usize::from(kind)];
            let o = kord(u32::from(id), 80 + i64::from(p), u64::from(q), Some(e));
            let (b, a) = if bid { (o, String::new()) } else { (String::new(), o) };
            v.push(format!(
                r#"{{"channel":"level3","type":"update","data":[{{"symbol":"P/Q","timestamp":"2026-10-11T00:00:01.{k:03}Z","bids":[{b}],"asks":[{a}]}}]}}"#
            ));
        }
        let o = kraken_l3::convert_full(lines(&v), &kcfg()).unwrap();
        replay(&o.events).map_err(TestCaseError::fail)?;
    }

    /// Random Coinbase level3 streams (unknown ids, gaps, re-snapshots) give replay-consistent records.
    #[test]
    fn coinbase_level3_records_replay_consistently(
        evs in prop::collection::vec((0u8..6, 0u8..8, 0u8..8, 1u8..9, 0u8..3), 0..80),
    ) {
        let mut v = Vec::new();
        let mut seq = 10i64;
        for (k, (kind, a, b, q, skip)) in evs.into_iter().enumerate() {
            seq += 1 + i64::from(skip == 2);
            let t = format!("2026-10-11T00:00:01.{:06}Z", k);
            let px = format!("{}.00", 100 + u32::from(a % 4));
            let line = match kind {
                0 => format!(r#"["open","BTC-USD","{seq}","o{a}","{}","{px}","{q}","{t}"]"#, if b % 2 == 0 { "buy" } else { "sell" }),
                1 => format!(r#"["change","BTC-USD","{seq}","o{a}","{px}","{q}","{t}"]"#),
                2 => format!(r#"["match","BTC-USD","{seq}","o{a}","o{b}","{px}","{q}","{t}"]"#),
                3 => format!(r#"["done","BTC-USD","{seq}","o{a}","{t}"]"#),
                4 => format!(r#"["noop","BTC-USD","{seq}","{t}"]"#),
                _ => format!(
                    r#"{{"recv_ns":1,"product_id":"BTC-USD","book":{{"sequence":{seq},"bids":[["99.00","1","o{a}"],["98.00","2","o{b}"]],"asks":[["105.00","1","o9"]],"time":"{t}"}}}}"#
                ),
            };
            v.push(line);
        }
        let o = coinbase_l3::convert_full(lines(&v), &coinbase_l3::Config::default()).unwrap();
        replay(&o.events).map_err(TestCaseError::fail)?;
    }

    /// Random Hyperliquid diff / fill streams give replay-consistent records.
    #[test]
    fn hyperliquid_records_replay_consistently(
        evs in prop::collection::vec((0u8..5, 0u8..8, 1u8..9, 1u8..9, 0u8..4), 0..80),
        snap in any::<bool>(),
    ) {
        let mut v = Vec::new();
        for (k, (kind, oid, s1, s2, p)) in evs.into_iter().enumerate() {
            let px = format!("{}.0", 100 + u32::from(p));
            let side = if oid % 2 == 0 { "Bid" } else { "Ask" };
            let ev = match kind {
                0 => format!(r#"{{"oid":{oid},"coin":"BTC","side":"{side}","px":"{px}","raw_book_diff":{{"new":{{"sz":"{s1}"}}}}}}"#),
                1 => format!(r#"{{"oid":{oid},"coin":"BTC","side":"{side}","px":"{px}","raw_book_diff":{{"update":{{"origSz":"{s1}","newSz":"{s2}"}}}}}}"#),
                2 => format!(r#"{{"oid":{oid},"coin":"BTC","side":"{side}","px":"{px}","raw_book_diff":"remove"}}"#),
                3 => format!(r#"["0x1",{{"coin":"BTC","px":"{px}","sz":"{s1}","side":"B","time":1,"oid":{oid},"crossed":false,"tid":{k}}}]"#),
                _ => format!(r#"["0x2",{{"coin":"BTC","px":"{px}","sz":"{s1}","side":"A","time":1,"oid":{},"crossed":true,"tid":{}}}]"#, 100 + u32::from(oid), k.saturating_sub(1)),
            };
            v.push(format!(
                r#"{{"local_time":"2026-10-11T00:00:01.{k:03}","block_time":"2026-10-11T00:00:01.{k:03}","block_number":{},"events":[{ev}]}}"#,
                1000 + k / 2
            ));
        }
        let cfg = hyperliquid::Config {
            snapshots: if snap {
                vec![(1003, r#"[["BTC",[[{"limitPx":"100.0","sz":"2.0","oid":1,"isTrigger":false},{"limitPx":"100.0","sz":"1.0","oid":1}],[{"limitPx":"103.0","sz":"1.0","oid":3}]]]]"#.into())]
            } else {
                Vec::new()
            },
            ..hyperliquid::Config::default()
        };
        let o = hyperliquid::convert_full(lines(&v), &cfg).unwrap();
        replay(&o.events).map_err(TestCaseError::fail)?;
    }
}

// ---------------------------------------------------------------- never panic on mutated captures

fn read(rel: &str) -> Vec<String> {
    qt_crypto::input::lines(&[fixture(rel)])
        .unwrap()
        .map(Result::unwrap)
        .collect()
}

const ALPHABET: &[char] = &[
    '0', '1', '9', '-', '.', '"', ',', ':', '[', ']', '{', '}', 'e', 'x', ' ', 'n',
];

fn mutate(src: &[String], muts: &[(usize, usize, u8)], drop: usize, dup: usize) -> Vec<String> {
    let mut v: Vec<Vec<char>> = src.iter().map(|l| l.chars().collect()).collect();
    if v.is_empty() {
        return Vec::new();
    }
    for &(li, ci, c) in muts {
        let n = v.len();
        let l = &mut v[li % n];
        if l.is_empty() {
            continue;
        }
        let k = ci % l.len();
        match c % 3 {
            0 => l[k] = ALPHABET[usize::from(c) % ALPHABET.len()],
            1 => l.truncate(k),
            _ => {
                l.remove(k);
            }
        }
    }
    let n = v.len();
    if drop < n * 2 {
        v.remove(drop % n);
    }
    if !v.is_empty() {
        let d = v[dup % v.len()].clone();
        v.push(d);
    }
    v.into_iter().map(|l| l.into_iter().collect()).collect()
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 300, ..ProptestConfig::default() })]

    #[test]
    fn mutated_captures_never_panic(
        muts in prop::collection::vec((any::<usize>(), any::<usize>(), any::<u8>()), 0..6),
        drop in any::<usize>(),
        dup in any::<usize>(),
    ) {
        let mut cb = read("coinbase_level3/book.jsonl");
        cb.extend(read("coinbase_level3/level3.jsonl"));
        let _ = coinbase_l3::convert(lines(&mutate(&cb, &muts, drop, dup)), &coinbase_l3::Config::default());
        let kr = read("kraken_l3/level3.jsonl");
        let _ = kraken_l3::convert(lines(&mutate(&kr, &muts, drop, dup)), &kraken_l3::Config::default());
        let mut hl = read("hyperliquid_node/node_raw_book_diffs_by_block/hourly/20261011/0");
        hl.extend(read("hyperliquid_node/node_fills_by_block/hourly/20261011/0"));
        hl.extend(read("hyperliquid_node/node_order_statuses_by_block/hourly/20261011/0"));
        let snap = read("hyperliquid_node/l4_snapshot_1000.json");
        let cfg = hyperliquid::Config {
            snapshots: mutate(&snap, &muts, usize::MAX, 0).into_iter().take(1).map(|s| (1000, s)).collect(),
            ..hyperliquid::Config::default()
        };
        let _ = hyperliquid::convert(lines(&mutate(&hl, &muts, drop, dup)), &cfg);
    }
}

/// The model-publisher property above, swept over a fixed pseudo-random set of books: every window path is taken
/// (exits, unchanged / changed re-entries, unseen entries, vanished orders) and no checksum ever mismatches.
#[test]
fn kraken_window_paths_are_all_exercised() {
    let mut x: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut next = || {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        (x & 0xFF) as u8
    };
    let mut tot: std::collections::BTreeMap<String, u64> = Default::default();
    for _ in 0..300 {
        let init: Vec<(bool, u8, u8)> = (0..30).map(|_| (next() % 2 == 0, next(), next())).collect();
        let ops: Vec<Vec<(u8, u8, u8, u8)>> = (0..40)
            .map(|_| (0..1 + next() % 3).map(|_| (next(), next(), next(), next())).collect())
            .collect();
        let (v, _) = publish(&init, &ops);
        let o = kraken_l3::convert_full(lines(&v), &kcfg()).unwrap();
        replay(&o.events).unwrap();
        for (k, n) in o.counters {
            *tot.entry(k).or_insert(0) += n;
        }
    }
    assert_eq!(tot["checksum_mismatches"], 0);
    for k in [
        "window_exits",
        "window_reentries",
        "window_reentries_changed",
        "window_entries",
        "out_of_view_vanished",
        "checksums_verified",
    ] {
        assert!(tot[k] > 0, "{k} never exercised: {tot:?}");
    }
    eprintln!("kraken window sweep: {tot:?}");
}
