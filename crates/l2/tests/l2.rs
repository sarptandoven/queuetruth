//! L2 view synthesis: O-L2-3 (scroll-out / scroll-in flags, trade-before-depth ordering, snapshots, hidden trades),
//! `VisibleBook` against an independent naive aggregate on random streams (cadence 0 and conflated), the hftbacktest
//! `.npz` layout, `synthesize` / `bbo_from_l2` on datasets, and the gate oracles O-L2-1 / O-L2-2 against `qt_book`
//! on `qt_testkit::generate` streams.

use std::collections::BTreeMap;
use std::path::PathBuf;

use proptest::prelude::*;
use qt_ingest::{Dataset, DatasetKind, DatasetWriter};
use qt_l2::{
    L2Builder, L2Config, TradePosition, VisibleBook, bbo_from_l2, synthesize, synthesize_partition, write_hbt_npz,
};
use qt_schema::json::DataQuality;
use qt_schema::rows::{L2Kind, L2Row, l2flags};
use qt_types::{BookSide, Instrument, MboEvent, Px, Qty, Side, UNDEF_PRICE, Venue, VenueRules, flags, rules};

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-l2-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    d
}

fn e(ts: i64, action: u8, side: u8, price: Px, size: Qty, id: u64, last: bool) -> MboEvent {
    MboEvent {
        ts_event: ts,
        ts_recv: ts + 1,
        instrument_id: 1,
        action,
        side,
        price,
        size,
        order_id: id,
        flags: if last { flags::LAST } else { 0 },
        sequence: ts as u64,
        ..MboEvent::default()
    }
}

fn cfg(depth: u16, pos: TradePosition) -> L2Config {
    L2Config {
        depth_levels: depth,
        cadence_ns: 0,
        trade_position: pos,
        include_counts: true,
        emit_hidden_trades: true,
    }
}

/// (kind, side, price, prev_qty, new_qty, level, flags) of each row.
type Brief = (L2Kind, Side, Option<Px>, Qty, Qty, u8, u8);

fn brief(rows: &[L2Row]) -> Vec<Brief> {
    rows.iter()
        .map(|r| (r.kind, r.side, r.price, r.prev_qty, r.new_qty, r.level, r.flags))
        .collect()
}

fn push(b: &mut L2Builder, evs: &[MboEvent]) -> Vec<L2Row> {
    let mut out = Vec::new();
    for x in evs {
        b.push(x, &mut out);
    }
    out
}

use L2Kind::{Clear, Depth, Snapshot, Trade};
const LB: u8 = l2flags::LAST_IN_BATCH;
const IN: u8 = l2flags::INTO_VIEW;
const OUT: u8 = l2flags::OUT_OF_VIEW;

#[test]
fn o_l2_3_scroll_out_scroll_in_and_trade_ordering() {
    let r = rules(Venue::Cme);
    let mut b = L2Builder::new(cfg(2, TradePosition::Before), r, 1);
    // First batch: clear + snapshot.
    let rows = push(&mut b, &[e(10, b'A', b'B', 100, 10, 1, true)]);
    assert_eq!(
        brief(&rows),
        vec![
            (Clear, Side::None, None, 0, 0, 255, 0),
            (Snapshot, Side::Bid, Some(100), 0, 10, 0, l2flags::SNAPSHOT | LB),
        ]
    );
    assert!(
        rows.iter()
            .all(|r| r.event_id == 1 && r.ts_event == 10 && r.ts_recv == 11 && r.sequence == 10)
    );
    // A new best enters the view at level 0; 100 keeps its qty (no row).
    let rows = push(&mut b, &[e(20, b'A', b'B', 101, 5, 2, true)]);
    assert_eq!(brief(&rows), vec![(Depth, Side::Bid, Some(101), 0, 5, 0, IN | LB)]);
    assert_eq!(rows[0].event_id, 2);
    // A third bid pushes 100 out of the top 2 (still present): OUT_OF_VIEW, new_qty 0, level 255.
    let rows = push(&mut b, &[e(30, b'A', b'B', 102, 7, 3, true)]);
    assert_eq!(
        brief(&rows),
        vec![
            (Depth, Side::Bid, Some(102), 0, 7, 0, IN),
            (Depth, Side::Bid, Some(100), 10, 0, 255, OUT | LB)
        ]
    );
    // Cancelling 102 empties it (no OUT_OF_VIEW) and 100 scrolls back in at level 1.
    let rows = push(&mut b, &[e(40, b'C', b'B', 102, 7, 3, true)]);
    assert_eq!(
        brief(&rows),
        vec![
            (Depth, Side::Bid, Some(102), 7, 0, 255, 0),
            (Depth, Side::Bid, Some(100), 0, 10, 1, IN | LB)
        ]
    );
    // An add below the view changes nothing visible: no rows, no batch id.
    let rows = push(&mut b, &[e(45, b'A', b'B', 98, 4, 9, true)]);
    assert!(rows.is_empty());
    assert_eq!(b.batches(), 4);
    // Execution: trade (hit side B, aggressor A) before the depth change; the trade keeps its own ts.
    let rows = push(
        &mut b,
        &[
            e(50, b'T', b'A', 101, 3, 0, false),
            e(51, b'F', b'B', 101, 3, 2, false),
            e(52, b'C', b'B', 101, 3, 2, true),
        ],
    );
    assert_eq!(
        brief(&rows),
        vec![
            (Trade, Side::Bid, Some(101), 0, 0, 0, 0),
            (Depth, Side::Bid, Some(101), 5, 2, 0, LB)
        ]
    );
    assert_eq!(
        (rows[0].ts_event, rows[0].trade_qty, rows[0].aggressor, rows[0].sequence),
        (50, 3, Side::Ask, 50)
    );
    assert_eq!((rows[1].ts_event, rows[1].sequence, rows[1].event_id), (52, 52, 5));
    assert_eq!((rows[1].prev_ct, rows[1].new_ct), (Some(1), Some(1)));
    // R inside a batch: clear + snapshot of the view after the batch, trades after it.
    let rows = push(
        &mut b,
        &[
            e(60, b'R', b'N', UNDEF_PRICE, 0, 0, false),
            e(60, b'A', b'A', 105, 2, 7, false),
            e(61, b'T', b'B', 105, 1, 0, true),
        ],
    );
    assert_eq!(
        brief(&rows),
        vec![
            (Clear, Side::None, None, 0, 0, 255, 0),
            (Snapshot, Side::Ask, Some(105), 0, 2, 0, l2flags::SNAPSHOT),
            (Trade, Side::Ask, Some(105), 0, 0, 0, LB),
        ]
    );

    // Trade-after-depth ordering.
    let mut b = L2Builder::new(cfg(2, TradePosition::After), r, 1);
    push(&mut b, &[e(10, b'A', b'B', 100, 10, 1, true)]);
    let rows = push(
        &mut b,
        &[e(20, b'T', b'A', 100, 4, 0, false), e(20, b'C', b'B', 100, 4, 1, true)],
    );
    assert_eq!(
        brief(&rows),
        vec![
            (Depth, Side::Bid, Some(100), 10, 6, 0, 0),
            (Trade, Side::Bid, Some(100), 0, 0, 0, LB)
        ]
    );

    // Hidden prints (Nasdaq: side N) carry HIDDEN_TRADE and are dropped when not emitted.
    let nas = rules(Venue::Nasdaq);
    let mut b = L2Builder::new(cfg(2, TradePosition::Before), nas, 1);
    push(&mut b, &[e(10, b'A', b'B', 100, 10, 1, true)]);
    let rows = push(&mut b, &[e(20, b'T', b'N', 100, 500, 0, true)]);
    assert_eq!(
        brief(&rows),
        vec![(Trade, Side::None, Some(100), 0, 0, 255, l2flags::HIDDEN_TRADE | LB)]
    );
    let mut b = L2Builder::new(
        L2Config {
            emit_hidden_trades: false,
            ..cfg(2, TradePosition::Before)
        },
        nas,
        1,
    );
    push(&mut b, &[e(10, b'A', b'B', 100, 10, 1, true)]);
    assert!(push(&mut b, &[e(20, b'T', b'N', 100, 500, 0, true)]).is_empty());

    // Counts off: a count-only change emits nothing; counts are null.
    let mut b = L2Builder::new(
        L2Config {
            include_counts: false,
            ..cfg(0, TradePosition::Before)
        },
        r,
        1,
    );
    let rows = push(&mut b, &[e(10, b'A', b'B', 100, 10, 1, true)]);
    assert_eq!(rows[1].new_ct, None);
    let rows = push(
        &mut b,
        &[
            e(20, b'A', b'B', 100, 0, 2, false),
            e(20, b'C', b'B', 100, 1, 1, false),
            e(20, b'A', b'B', 100, 1, 3, true),
        ],
    );
    assert!(rows.is_empty(), "qty 10 -> 10 with 2 orders: invisible without counts");
}

#[test]
fn o_l2_3_cadence_conflates_depth_but_keeps_trades() {
    let r = rules(Venue::Cme);
    let c = L2Config {
        cadence_ns: 100,
        ..cfg(0, TradePosition::Before)
    };
    let mut b = L2Builder::new(c, r, 1);
    let mut out = Vec::new();
    b.push(&e(10, b'A', b'B', 100, 10, 1, true), &mut out); // first batch: immediate snapshot
    assert_eq!(out.len(), 2);
    out.clear();
    b.push(&e(20, b'A', b'B', 100, 5, 2, true), &mut out);
    b.push(&e(30, b'T', b'A', 100, 2, 0, false), &mut out);
    b.push(&e(30, b'C', b'B', 100, 2, 1, true), &mut out);
    b.push(&e(40, b'A', b'A', 110, 1, 3, false), &mut out);
    b.push(&e(40, b'C', b'A', 110, 1, 3, true), &mut out);
    assert!(out.is_empty(), "inside the bucket [0, 100)");
    b.push(&e(100, b'A', b'B', 99, 1, 4, true), &mut out);
    let cf = l2flags::CONFLATED;
    assert_eq!(
        brief(&out),
        vec![
            (Trade, Side::Bid, Some(100), 0, 0, 0, 0),
            (Depth, Side::Bid, Some(100), 10, 13, 0, cf),
            (Depth, Side::Bid, Some(99), 0, 1, 1, cf | IN | LB),
        ]
    );
    assert_eq!(out[0].ts_event, 30, "trades keep their own time");
    assert_eq!(out[1].ts_event, 100);
    out.clear();
    // Next bucket ends at 200; flush forces the pending batch out.
    b.push(&e(150, b'C', b'B', 99, 1, 4, true), &mut out);
    assert!(out.is_empty());
    b.flush(160, &mut out);
    assert_eq!(brief(&out), vec![(Depth, Side::Bid, Some(99), 1, 0, 255, cf | LB)]);
    assert_eq!(qt_l2::bucket_end(150, 100), 200);
}

#[test]
fn implied_trades_take_their_aggressor_from_the_prevailing_bbo() {
    // CME (no hidden orders): T side N at the best bid = sell aggressor, at the best ask = buy; elsewhere unresolved.
    let mut b = L2Builder::new(cfg(5, TradePosition::Before), rules(Venue::Cme), 1);
    push(
        &mut b,
        &[
            e(10, b'A', b'B', 100, 10, 1, false),
            e(10, b'A', b'A', 102, 10, 2, true),
        ],
    );
    let rows = push(
        &mut b,
        &[e(20, b'T', b'N', 100, 3, 0, false), e(20, b'C', b'B', 100, 3, 1, true)],
    );
    assert_eq!(
        (rows[0].aggressor, rows[0].side, rows[0].flags & l2flags::HIDDEN_TRADE),
        (Side::Ask, Side::Bid, 0)
    );
    let rows = push(&mut b, &[e(30, b'T', b'N', 102, 1, 0, true)]);
    assert_eq!((rows[0].aggressor, rows[0].side), (Side::Bid, Side::Ask));
    let rows = push(&mut b, &[e(40, b'T', b'N', 101, 1, 0, true)]);
    assert_eq!((rows[0].aggressor, rows[0].side), (Side::None, Side::None));
    assert_eq!(b.unresolved_aggressor_trades(), 1);
    // Nasdaq hidden prints are never inferred.
    let mut b = L2Builder::new(cfg(5, TradePosition::Before), rules(Venue::Nasdaq), 1);
    push(&mut b, &[e(10, b'A', b'B', 100, 10, 1, true)]);
    let rows = push(&mut b, &[e(20, b'T', b'N', 100, 3, 0, true)]);
    assert_eq!(
        (rows[0].aggressor, rows[0].flags & l2flags::HIDDEN_TRADE),
        (Side::None, l2flags::HIDDEN_TRADE)
    );
    assert_eq!(b.unresolved_aggressor_trades(), 0);
}

// ---- random streams vs an independent naive aggregate --------------------------------------------------------------

/// Naive aggregate: a list of live orders, levels recomputed from scratch.
#[derive(Default)]
struct Naive {
    orders: Vec<(u64, BookSide, Px, Qty)>,
}

impl Naive {
    fn apply(&mut self, e: &MboEvent, r: &VenueRules) {
        let pos = self.orders.iter().position(|o| o.0 == e.order_id);
        match e.action {
            b'A' => {
                if let (Some(s), true) = (BookSide::from_byte(e.side), e.price != UNDEF_PRICE && e.size > 0) {
                    if let Some(i) = pos {
                        self.orders.remove(i);
                    }
                    self.orders.push((e.order_id, s, e.price, e.size));
                }
            }
            b'C' => {
                if let Some(i) = pos {
                    if e.size < self.orders[i].3 {
                        self.orders[i].3 -= e.size;
                    } else {
                        self.orders.remove(i);
                    }
                }
            }
            b'M' if r.modify_records_present => match pos {
                None => {
                    if let (Some(s), true) = (BookSide::from_byte(e.side), e.price != UNDEF_PRICE && e.size > 0) {
                        self.orders.push((e.order_id, s, e.price, e.size));
                    }
                }
                Some(i) if e.size == 0 => {
                    self.orders.remove(i);
                }
                Some(i) => {
                    self.orders[i].2 = e.price;
                    self.orders[i].3 = e.size;
                }
            },
            b'R' => self.orders.clear(),
            _ => {}
        }
    }

    fn levels(&self, s: BookSide, n: usize) -> Vec<(Px, Qty, u32)> {
        let mut m: BTreeMap<Px, (Qty, u32)> = BTreeMap::new();
        for o in self.orders.iter().filter(|o| o.1 == s) {
            let l = m.entry(o.2).or_default();
            l.0 += o.3;
            l.1 += 1;
        }
        let v: Vec<(Px, Qty, u32)> = m.into_iter().map(|(p, (q, c))| (p, q, c)).collect();
        let v: Vec<_> = if s == BookSide::Bid {
            v.into_iter().rev().collect()
        } else {
            v
        };
        if n == 0 { v } else { v.into_iter().take(n).collect() }
    }
}

fn arb_events() -> impl Strategy<Value = Vec<MboEvent>> {
    let rec = (
        0u8..7,
        0u64..12,
        95i64..106,
        0u64..20,
        any::<bool>(),
        any::<bool>(),
        0i64..40,
    );
    prop::collection::vec(rec, 1..300).prop_map(|v| {
        let mut ts = 0;
        v.into_iter()
            .enumerate()
            .map(|(i, (a, id, px, sz, bid, last, dt))| {
                ts += dt;
                let action = b"AACMTRF"[usize::from(a)];
                let action = if action == b'R' && i % 5 != 0 { b'A' } else { action };
                MboEvent {
                    ts_event: ts,
                    ts_recv: ts,
                    instrument_id: 1,
                    action,
                    side: if bid { b'B' } else { b'A' },
                    price: px,
                    size: sz,
                    order_id: id,
                    flags: if last { flags::LAST } else { 0 },
                    sequence: i as u64,
                    ..MboEvent::default()
                }
            })
            .collect()
    })
}

fn view_of(vb: &VisibleBook, s: BookSide, counts: bool) -> Vec<(Px, Qty, Option<u32>)> {
    vb.levels(s, 0)
        .into_iter()
        .map(|(p, q, c)| (p, q, if counts { c } else { None }))
        .collect()
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 600, ..ProptestConfig::default() })]

    /// `aggregate_level` equals the naive aggregate after every record; the `VisibleBook` rebuilt from the emitted rows
    /// equals the naive top-N after every emission (cadence 0: after every LAST record), for N in {0, 1, 3}, with and
    /// without counts and conflation.
    #[test]
    fn visible_book_tracks_naive_top_n(evs in arb_events(), n in prop::sample::select(vec![0u16, 1, 3]),
                                       cadence in prop::sample::select(vec![0i64, 50]), counts in any::<bool>(),
                                       venue in prop::sample::select(vec![Venue::Cme, Venue::Nasdaq, Venue::Generic])) {
        let r = rules(venue);
        let c = L2Config { depth_levels: n, cadence_ns: cadence, trade_position: TradePosition::Before, include_counts: counts, emit_hidden_trades: true };
        let mut b = L2Builder::new(c, r, 1);
        let mut naive = Naive::default();
        let mut vb = VisibleBook::new();
        let mut out = Vec::new();
        let mut last_event_id = 0;
        let check = |vb: &VisibleBook, naive: &Naive| -> Result<(), TestCaseError> {
            for s in [BookSide::Bid, BookSide::Ask] {
                let want: Vec<_> = naive.levels(s, usize::from(n)).into_iter().map(|(p, q, ct)| (p, q, counts.then_some(ct))).collect();
                prop_assert_eq!(view_of(vb, s, counts), want);
            }
            Ok(())
        };
        for x in &evs {
            naive.apply(x, &r);
            let before = out.len();
            b.push(x, &mut out);
            for s in [BookSide::Bid, BookSide::Ask] {
                for (p, q, ct) in naive.levels(s, 0) {
                    prop_assert_eq!(b.aggregate_level(s, p), Some((q, ct)));
                }
            }
            for row in &out[before..] {
                prop_assert_eq!(row.event_id, last_event_id + 1, "one id per emitted batch");
                vb.apply(row);
            }
            if out.len() > before {
                prop_assert!(out.last().unwrap().flags & l2flags::LAST_IN_BATCH != 0);
                prop_assert_eq!(out[before..].iter().filter(|r| r.flags & l2flags::LAST_IN_BATCH != 0).count(), 1);
                last_event_id = out.last().unwrap().event_id;
                check(&vb, &naive)?;
            } else if x.is_last() && cadence == 0 {
                check(&vb, &naive)?;
            }
        }
        let before = out.len();
        b.flush(evs.last().unwrap().ts_event, &mut out);
        for row in &out[before..] {
            vb.apply(row);
        }
        check(&vb, &naive)?;
        // Depth rows only for levels inside the view or leaving it.
        for row in &out {
            if row.kind == L2Kind::Depth && n > 0 && row.new_qty > 0 {
                prop_assert!(usize::from(row.level) < usize::from(n));
            }
        }
    }
}

// ---- hftbacktest npz ------------------------------------------------------------------------------------------------

fn inst(iid: u32, date: i32, venue: Venue, price_exp: i8, size_exp: i8) -> Instrument {
    Instrument {
        instrument_id: iid,
        session_date: date,
        symbol: format!("X{iid}"),
        venue,
        source_venue: venue.name().into(),
        price_exp,
        tick_size: 1,
        lot_size: 1,
        size_exp,
        contract_multiplier: 1.0,
        currency: "USD".into(),
        session_tz: "UTC".into(),
        midnight_utc_ns: i64::from(date) * 86_400_000_000_000,
        open_s: 0,
        close_s: 86_400,
        source: "test".into(),
    }
}

fn le16(b: &[u8], o: usize) -> usize {
    usize::from(u16::from_le_bytes([b[o], b[o + 1]]))
}

fn le32(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}

/// The bytes of the only member of a (non-ZIP64) zip archive, checked to be STORED, named `name`, with matching
/// local / central sizes, a DOS 1980-01-01 timestamp and an end-of-central-directory record that covers the file.
fn stored_zip_single_member(z: &[u8], name: &str) -> Vec<u8> {
    let eocd = z.len() - 22;
    assert_eq!(le32(z, eocd), 0x0605_4b50, "EOCD at the end (no comment)");
    assert_eq!((le16(z, eocd + 8), le16(z, eocd + 10)), (1, 1), "one entry");
    let (cd_size, cd_off) = (le32(z, eocd + 12) as usize, le32(z, eocd + 16) as usize);
    assert_eq!(cd_off + cd_size, eocd);
    assert_eq!(le32(z, cd_off), 0x0201_4b50);
    assert_eq!(le16(z, cd_off + 10), 0, "STORED");
    assert_eq!(
        (le16(z, cd_off + 12), le16(z, cd_off + 14)),
        (0, 0x21),
        "1980-01-01 00:00"
    );
    let size = le32(z, cd_off + 24) as usize;
    assert_eq!(le32(z, cd_off + 20) as usize, size, "stored: compressed = uncompressed");
    let nl = le16(z, cd_off + 28);
    assert_eq!(&z[cd_off + 46..cd_off + 46 + nl], name.as_bytes());
    let lh = le32(z, cd_off + 42) as usize;
    assert_eq!((lh, le32(z, lh)), (0, 0x0403_4b50));
    assert_eq!(le16(z, lh + 8), 0, "STORED");
    assert_eq!(le32(z, lh + 14), le32(z, cd_off + 16), "same CRC");
    assert_eq!(le32(z, lh + 22) as usize, size);
    let data = lh + 30 + le16(z, lh + 26) + le16(z, lh + 28);
    assert_eq!(data + size, cd_off, "member data ends at the central directory");
    let member = &z[data..data + size];
    // Bitwise CRC-32 (independent of the writer's table implementation).
    let mut crc = !0u32;
    for &b in member {
        crc ^= u32::from(b);
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                0xEDB8_8320 ^ (crc >> 1)
            } else {
                crc >> 1
            };
        }
    }
    assert_eq!(le32(z, cd_off + 16), !crc, "CRC-32 of the member");
    member.to_vec()
}

#[test]
fn npz_layout_header_dtype_and_64_byte_records() {
    let mut b = L2Builder::new(cfg(5, TradePosition::Before), rules(Venue::Nasdaq), 1);
    let mut rows = push(
        &mut b,
        &[
            e(10, b'A', b'B', 185_250_000_000, 100, 1, true),
            e(20, b'A', b'A', 185_260_000_000, 30, 2, true),
        ],
    );
    rows.extend(push(
        &mut b,
        &[
            e(30, b'T', b'B', 185_260_000_000, 10, 0, false),
            e(30, b'C', b'A', 185_260_000_000, 10, 2, true),
        ],
    ));
    rows.extend(push(&mut b, &[e(40, b'T', b'N', 185_255_000_000, 7, 0, true)])); // hidden: omitted
    let i = inst(1, 0, Venue::Nasdaq, -9, 0);
    let dir = scratch("npz");
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("l2.npz");
    let fe = write_hbt_npz(&rows, &i, 1_000, &p).unwrap();
    let fe2 = write_hbt_npz(&rows, &i, 1_000, &dir.join("again.npz")).unwrap();
    assert_eq!(fe.sha256, fe2.sha256, "byte-reproducible");
    assert_eq!(fe.rows, Some(5)); // clear, snapshot, depth ask, trade, depth ask
    let npy = stored_zip_single_member(&std::fs::read(&p).unwrap(), "data.npy");
    assert_eq!(&npy[..8], b"\x93NUMPY\x01\x00");
    let hl = usize::from(u16::from_le_bytes([npy[8], npy[9]]));
    assert_eq!((10 + hl) % 64, 0, "header padded to 64 bytes");
    let header = std::str::from_utf8(&npy[10..10 + hl]).unwrap();
    assert!(header.starts_with("{'descr': [('ev', '<u8'), ('exch_ts', '<i8'), ('local_ts', '<i8'), ('px', '<f8'), ('qty', '<f8'), ('order_id', '<u8'), ('ival', '<i8'), ('fval', '<f8')], 'fortran_order': False, 'shape': (5,), }"), "{header}");
    assert!(header.ends_with('\n'));
    let data = &npy[10 + hl..];
    assert_eq!(data.len(), 5 * 64);
    let rec = |k: usize| {
        let r = &data[k * 64..(k + 1) * 64];
        let u = |o: usize| u64::from_le_bytes(r[o..o + 8].try_into().unwrap());
        let f = |o: usize| f64::from_le_bytes(r[o..o + 8].try_into().unwrap());
        (u(0), u(8) as i64, u(16) as i64, f(24), f(32), u(40), u(48), f(56))
    };
    const EX: u64 = (1 << 31) | (1 << 30);
    assert_eq!(rec(0), (3 | EX, 10, 1_011, 0.0, 0.0, 0, 0, 0.0));
    assert_eq!(rec(1), (4 | EX | (1 << 29), 10, 1_011, 185.25, 100.0, 0, 0, 0.0));
    assert_eq!(rec(2), (1 | EX | (1 << 28), 20, 1_021, 185.26, 30.0, 0, 0, 0.0));
    assert_eq!(rec(3), (2 | EX | (1 << 29), 30, 1_031, 185.26, 10.0, 0, 0, 0.0));
    assert_eq!(rec(4).0, 1 | EX | (1 << 28));
    assert_eq!(rec(4).4, 20.0);
    // Size exponent and overflowing latency.
    let crypto = inst(1, 0, Venue::Generic, -2, -8);
    let ev = qt_l2::hbt_events(&rows, &crypto, 0).unwrap();
    assert_eq!(
        f64::from_le_bytes(ev[64 + 32..64 + 40].try_into().unwrap()),
        100.0 / 1e8
    );
    assert!(write_hbt_npz(&rows, &i, i64::MAX, &dir.join("x.npz")).is_err());
    // Orders sidecar next to the npz (O-ZOO-3 driver format), real units.
    let side: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(dir.join("l2.orders.json")).unwrap()).unwrap();
    assert_eq!(
        side,
        serde_json::json!({"id": "l2", "tick_size": 1e-9, "lot_size": 1.0, "actions": []})
    );
    let act = qt_l2::HbtAction {
        ts: 15,
        submit: true,
        order_id: 7,
        side: BookSide::Bid,
        price: 185_250_000_000,
        qty: 2,
        gtx: true,
    };
    let (_, se) = qt_l2::write_hbt_npz_with_orders(&rows, &i, 0, &dir.join("w.npz"), &[act]).unwrap();
    assert_eq!(se.name, "w.orders.json");
    let side: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(dir.join("w.orders.json")).unwrap()).unwrap();
    assert_eq!(
        side["actions"],
        serde_json::json!([{"ts": 15, "kind": "submit", "order_id": 7, "side": "B", "px": 185.25, "qty": 2.0, "tif": "GTX"}])
    );
    // A trade row without an aggressor (e.g. from an L2-only dataset) is sided by the prevailing visible BBO.
    let mut implied = rows[..3].to_vec(); // clear, snapshot bid 185.25, depth ask 185.26
    let mut t = rows[3];
    t.aggressor = Side::None;
    t.side = Side::None;
    t.price = Some(185_250_000_000);
    implied.push(t);
    t.price = Some(185_255_000_000);
    implied.push(t);
    let ev = qt_l2::hbt_events(&implied, &i, 0).unwrap();
    assert_eq!(ev.len(), 4 * 64, "the unresolvable trade is omitted");
    assert_eq!(
        u64::from_le_bytes(ev[3 * 64..3 * 64 + 8].try_into().unwrap()),
        2 | EX | (1 << 28),
        "at the bid: sell"
    );
    std::fs::remove_dir_all(dir).ok();
}

// ---- datasets -----------------------------------------------------------------------------------------------------

fn mbo_dataset(root: &std::path::Path) -> Dataset {
    let mut w = DatasetWriter::create(root, DatasetKind::Mbo, Venue::Cme, "test").unwrap();
    w.set_instruments(vec![inst(1, 0, Venue::Cme, -9, 0), inst(2, 0, Venue::Cme, -9, 0)]);
    for k in 0..4000u64 {
        let iid = 1 + (k % 2) as u32;
        let id = k / 3;
        let action = b"AACT"[((k / 2) % 4) as usize];
        let ev = MboEvent {
            ts_event: k as i64 * 10,
            ts_recv: k as i64 * 10 + 1,
            instrument_id: iid,
            action,
            side: if id % 2 == 0 { b'B' } else { b'A' },
            price: if id % 2 == 0 {
                100 - (id % 5) as i64
            } else {
                101 + (id % 5) as i64
            },
            size: 1 + k % 7,
            order_id: id,
            flags: if k % 3 == 0 { flags::LAST } else { 0 },
            sequence: k,
            ..MboEvent::default()
        };
        w.push_mbo(&ev).unwrap();
    }
    w.finish(DataQuality::default()).unwrap();
    Dataset::open(root).unwrap()
}

#[test]
fn synthesize_and_bbo_from_l2_are_deterministic() {
    let dir = scratch("synth");
    let ds = mbo_dataset(&dir.join("mbo"));
    let c = L2Config::default();
    let a = synthesize(&ds, &c, &dir.join("o1"), 1).unwrap();
    let b = synthesize(&ds, &c, &dir.join("o3"), 3).unwrap();
    assert_eq!(a, b, "threads do not change outputs");
    assert_eq!(a.len(), 2);
    assert_eq!(a[0].name, "l2/date=1970-01-01/instrument_id=1/part-00000.parquet");
    let rows = synthesize_partition(&ds.partitions()[0], &ds, &c).unwrap();
    assert_eq!(a[0].rows, Some(rows.len() as u64));
    let mut back = Vec::new();
    for bt in qt_schema::io::read_table_as(&dir.join("o1").join(&a[0].name), "qt.l2").unwrap() {
        back.extend(qt_schema::codec::batch_to_l2(&bt.unwrap()).unwrap());
    }
    assert_eq!(back, rows);
    assert!(bbo_from_l2(&ds, &dir.join("x"), 1).is_err(), "MBO dataset");

    // An L2-only dataset from those rows: BBO in change mode and trades.
    let l2root = dir.join("l2ds");
    let mut w = DatasetWriter::create(&l2root, DatasetKind::L2, Venue::Cme, "tardis_l2").unwrap();
    w.set_instruments(vec![inst(1, 0, Venue::Cme, -9, 0), inst(2, 0, Venue::Cme, -9, 0)]);
    let mut all = Vec::new();
    for p in ds.partitions() {
        all.push(synthesize_partition(p, &ds, &c).unwrap());
    }
    for r in all.iter().flatten() {
        w.push_l2(r).unwrap();
    }
    w.finish(DataQuality::default()).unwrap();
    let l2ds = Dataset::open(&l2root).unwrap();
    let f1 = bbo_from_l2(&l2ds, &dir.join("b1"), 1).unwrap();
    let f2 = bbo_from_l2(&l2ds, &dir.join("b2"), 2).unwrap();
    assert_eq!(f1, f2);
    let (bbo, trades) = qt_l2::bbo_trades_from_rows(all[0].iter().copied().map(Ok)).unwrap();
    let n_trades = all.iter().flatten().filter(|r| r.kind == L2Kind::Trade).count() as u64;
    assert_eq!(f1[1].rows, Some(n_trades));
    assert!(!bbo.is_empty() && !trades.is_empty());
    for w2 in bbo.windows(2) {
        assert_ne!(
            (
                w2[0].bid_px,
                w2[0].bid_sz,
                w2[0].bid_ct,
                w2[0].ask_px,
                w2[0].ask_sz,
                w2[0].ask_ct
            ),
            (
                w2[1].bid_px,
                w2[1].bid_sz,
                w2[1].bid_ct,
                w2[1].ask_px,
                w2[1].ask_sz,
                w2[1].ask_ct
            ),
            "change mode"
        );
    }
    assert!(synthesize(&l2ds, &c, &dir.join("y"), 1).is_err(), "already L2");
    std::fs::remove_dir_all(dir).ok();
}

// ---- gate oracles (qt_book + qt_testkit) ---------------------------------------------------------------------------

fn synth(venue: Venue, seed: u64) -> qt_testkit::SynthOutput {
    qt_testkit::generate(&qt_testkit::SynthConfig {
        seed,
        venue,
        instruments: 2,
        events_per_instrument: 3_000,
        tick: 1,
        base_px: 10_000,
        max_levels: 12,
        mean_size: 50,
        p_add: 0.45,
        p_cancel: 0.25,
        p_reduce: 0.1,
        p_modify: if venue == Venue::Nasdaq { 0.0 } else { 0.1 },
        p_trade: 0.1,
        p_clear: 0.002,
        hidden_print_rate: if venue == Venue::Nasdaq { 0.05 } else { 0.0 },
        snapshot_at_start: venue == Venue::Cme,
        ts_start: 1_000_000_000,
        mean_gap_ns: 1_000,
    })
}

fn book_top(book: &qt_book::Book, s: BookSide, n: usize) -> Vec<(Px, Qty, Option<u32>)> {
    let it = book.levels(s).map(|l| (l.price, l.total, Some(l.count)));
    if n == 0 { it.collect() } else { it.take(n).collect() }
}

/// O-L2-1: `L2Builder::aggregate_level` equals `Book::level` (total, count) after every event, all venues.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and book crates")]
fn o_l2_1_aggregate_equals_book_level() {
    for venue in [Venue::Nasdaq, Venue::Cme, Venue::Generic] {
        for seed in 0..5 {
            let out = synth(venue, seed);
            let r = rules(venue);
            let mut books: BTreeMap<u32, (qt_book::Book, L2Builder)> = BTreeMap::new();
            let mut sink = Vec::new();
            for ev in &out.events {
                let (book, b) = books.entry(ev.instrument_id).or_insert_with(|| {
                    (
                        qt_book::Book::new(r),
                        L2Builder::new(L2Config::default(), r, ev.instrument_id),
                    )
                });
                book.apply(ev);
                b.push(ev, &mut sink);
                sink.clear();
                if ev.is_last() {
                    for s in [BookSide::Bid, BookSide::Ask] {
                        let lv: Vec<_> = book.levels(s).collect();
                        for l in &lv {
                            assert_eq!(
                                b.aggregate_level(s, l.price),
                                Some((l.total, l.count)),
                                "{venue:?} seed {seed}"
                            );
                        }
                        assert_eq!(b.aggregate_levels(s, 0).len(), lv.len(), "{venue:?} seed {seed}");
                    }
                }
            }
        }
    }
}

/// O-L2-2: the `VisibleBook` rebuilt from emitted rows equals the book's top N after every batch (cadence 0),
/// `depth_levels = 0` reproduces the full book, and with a cadence it equals the book at each emission time.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and book crates")]
fn o_l2_2_visible_book_equals_book_top_n() {
    for venue in [Venue::Nasdaq, Venue::Cme, Venue::Generic] {
        for seed in 0..3 {
            for (n, cadence) in [(10u16, 0i64), (0, 0), (3, 0), (10, 50_000), (0, 50_000)] {
                let out = synth(venue, seed);
                let r = rules(venue);
                let c = L2Config {
                    depth_levels: n,
                    cadence_ns: cadence,
                    ..L2Config::default()
                };
                let mut st: BTreeMap<u32, (qt_book::Book, L2Builder, VisibleBook)> = BTreeMap::new();
                let mut rows = Vec::new();
                for ev in &out.events {
                    let (book, b, vb) = st.entry(ev.instrument_id).or_insert_with(|| {
                        (
                            qt_book::Book::new(r),
                            L2Builder::new(c, r, ev.instrument_id),
                            VisibleBook::new(),
                        )
                    });
                    book.apply(ev);
                    rows.clear();
                    b.push(ev, &mut rows);
                    for row in &rows {
                        vb.apply(row);
                    }
                    if !rows.is_empty() || (ev.is_last() && cadence == 0) {
                        for s in [BookSide::Bid, BookSide::Ask] {
                            assert_eq!(
                                vb.levels(s, 0),
                                book_top(book, s, usize::from(n)),
                                "{venue:?} seed {seed} n {n} cadence {cadence}"
                            );
                        }
                    }
                }
            }
        }
    }
}

/// L2Builder throughput, depth 10, cadence 0, on 5M generated records. `cargo test --release -p queuetruth-l2
/// --test l2 -- --ignored bench_l2_builder --nocapture`.
#[test]
#[ignore]
fn bench_l2_builder() {
    let mut evs = Vec::with_capacity(5_000_000);
    let mut live: Vec<(u64, u8, i64, u64)> = Vec::new();
    let mut x = 0x9e37_79b9_7f4a_7c15u64;
    let mut next = || {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        x
    };
    for i in 0..5_000_000u64 {
        let r = next();
        let ts = i as i64 * 100;
        if live.len() < 50 || r % 3 == 0 {
            let side = if r % 2 == 0 { b'B' } else { b'A' };
            let px = if side == b'B' {
                10_000 - (r >> 8) as i64 % 30
            } else {
                10_001 + (r >> 8) as i64 % 30
            };
            live.push((i, side, px, 1 + (r >> 20) % 100));
            evs.push(e(ts, b'A', side, px, 1 + (r >> 20) % 100, i, true));
        } else {
            let k = (r >> 5) as usize % live.len();
            let o = live.swap_remove(k);
            if r % 5 == 0 {
                evs.push(e(ts, b'T', if o.1 == b'B' { b'A' } else { b'B' }, o.2, o.3, 0, false));
            }
            evs.push(e(ts, b'C', o.1, o.2, o.3, o.0, true));
        }
    }
    let mut b = L2Builder::new(L2Config::default(), rules(Venue::Nasdaq), 1);
    let mut out = Vec::with_capacity(1 << 16);
    let mut rows = 0usize;
    let t0 = std::time::Instant::now();
    for ev in &evs {
        b.push(ev, &mut out);
        if out.len() > 60_000 {
            rows += out.len();
            out.clear();
        }
    }
    let s = t0.elapsed().as_secs_f64();
    rows += out.len();
    println!(
        "L2Builder depth 10: {} records -> {rows} rows in {s:.2} s = {:.2}M rec/s",
        evs.len(),
        evs.len() as f64 / s / 1e6
    );
}
