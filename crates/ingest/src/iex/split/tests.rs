//! Unit tests of the splitter. The differential tests against the Python engine, on generated captures of every
//! layout the reader accepts, are in `python/tests/scale/test_iex_split_engines.py`.

use std::io::Cursor;

use proptest::prelude::*;

use super::*;
use crate::iex::capture::CaptureReader;
use crate::iex::synth::{self, Segment};
use crate::iex::transport::{self, Blocks, SegmentHeader};

/// New York midnight of 2026-10-09 in UTC ns.
const MID: i64 = 1_791_518_400_000_000_000;

/// `PcapWriter`'s file header: little endian, nanosecond stamps, snaplen 65535, Ethernet.
fn pcap_header() -> Vec<u8> {
    let mut h = Vec::new();
    h.extend_from_slice(&0xa1b2_3c4du32.to_le_bytes());
    h.extend_from_slice(&[2, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
    h.extend_from_slice(&65_535u32.to_le_bytes());
    h.extend_from_slice(&1u32.to_le_bytes());
    h
}

/// A message with the symbol field at offset 10: type, flag byte, timestamp, symbol, then `extra` bytes 0xab.
fn sym_msg(t: u8, flag: u8, ts: i64, sym: &str, extra: usize) -> Vec<u8> {
    let mut m = vec![t, flag];
    m.extend_from_slice(&ts.to_le_bytes());
    let mut s = sym.as_bytes().to_vec();
    s.resize(8, b' ');
    m.extend_from_slice(&s);
    m.extend(std::iter::repeat_n(0xab, extra));
    m
}

fn system(code: u8, ts: i64) -> Vec<u8> {
    let mut m = vec![b'S', code];
    m.extend_from_slice(&ts.to_le_bytes());
    m
}

fn segment(seq: i64, messages: Vec<Vec<u8>>, t: i64) -> Segment {
    Segment {
        channel: 1,
        session: 77,
        first_seq: seq,
        send_time: t,
        capture_ns: t,
        messages,
    }
}

/// Feeds `data` in pieces of the given sizes (cycled; empty: whole).
fn run(k: usize, data: &[u8], sizes: &[usize], symbols: Option<&[&str]>) -> Splitter {
    let only = symbols.map(|s| s.iter().map(|x| (*x).to_owned()).collect::<Vec<_>>());
    let patterns = symbols.map(|s| {
        let mut p: Vec<Vec<u8>> = s
            .iter()
            .map(|x| {
                let mut v = x.as_bytes().to_vec();
                v.resize(8.max(v.len()), b' ');
                v
            })
            .collect();
        p.sort();
        p.push(b"\x0a\x00S".to_vec());
        p
    });
    let mut sp = Splitter::new(k, i128::from(MID), only, patterns).unwrap();
    if sizes.is_empty() {
        sp.feed(data).unwrap();
        return sp;
    }
    let (mut i, mut j) = (0, 0);
    while i < data.len() {
        let n = sizes[j % sizes.len()].max(1).min(data.len() - i);
        sp.feed(&data[i..i + n]).unwrap();
        i += n;
        j += 1;
    }
    sp
}

/// (capture ns, header, messages) of every packet of one bucket's output.
fn packets(out: &[u8]) -> Vec<(i64, SegmentHeader, Vec<Vec<u8>>)> {
    let mut bytes = pcap_header();
    bytes.extend_from_slice(out);
    let mut r = CaptureReader::new(Cursor::new(bytes));
    let mut v = Vec::new();
    while let Some(p) = r.next_packet().unwrap() {
        let payload = transport::udp_payload(p.linktype, p.data).unwrap();
        let h = transport::segment_header(payload).unwrap();
        let body = &payload[transport::HEADER_LEN..transport::HEADER_LEN + usize::from(h.payload_len)];
        let mut blocks = Blocks::new(body, h.message_count);
        let msgs: Vec<Vec<u8>> = blocks.by_ref().map(<[u8]>::to_vec).collect();
        assert!(!blocks.walk.truncated && blocks.walk.trailing == 0);
        v.push((p.ts_ns.unwrap(), h, msgs));
    }
    v
}

fn outputs(sp: &mut Splitter) -> Vec<Vec<u8>> {
    sp.outputs_mut().iter_mut().map(std::mem::take).collect()
}

#[test]
fn bucket_rule_and_checksum() {
    assert_eq!(bucket_of("AAPL", 32), 28);
    assert_eq!(bucket_of("MSFT", 32), 17);
    assert_eq!(bucket_of("SPY", 7), 3);
    assert_eq!(bucket_of("ABCDEFGH", 7), 5);
    assert_eq!(
        (bucket_of("AAPL", 3), bucket_of("TSLA", 3), bucket_of("SPY", 3)),
        (2, 0, 1)
    );
    // the IPv4 header of the first heartbeat of the 2026-10-09 capture (`transport` tests)
    let mut h = [
        0x45, 0, 0, 0x44, 0, 0, 0, 0, 0x40, 0x11, 0, 0, 0x17, 0xe2, 0x9b, 0x85, 0xe9, 0xd7, 0x15, 0x08,
    ];
    assert_eq!(ip_checksum(&h), 0xc862);
    h[10..12].copy_from_slice(&[0xc8, 0x62]);
    assert_eq!(ip_checksum(&h), 0);
}

/// One kept segment is the source frame with the IPv4 checksum filled in, at the capture's (microsecond) stamp.
#[test]
fn one_record_is_the_source_frame() {
    let t = MID + 34_200_000_000_123;
    let seg = segment(5, vec![sym_msg(b'a', b'8', t, "AAPL", 20)], t);
    let mut sp = run(1, &synth::pcapng(&synth::frames(std::slice::from_ref(&seg))), &[], None);
    let out = outputs(&mut sp).remove(0);
    let mut frame = synth::frame(&synth::segment_bytes(&seg));
    let us = u64::try_from(t / 1000 * 1000).unwrap();
    let mut want = Vec::new();
    want.extend_from_slice(&u32::try_from(us / 1_000_000_000).unwrap().to_le_bytes());
    want.extend_from_slice(&u32::try_from(us % 1_000_000_000).unwrap().to_le_bytes());
    want.extend_from_slice(&u32::try_from(frame.len()).unwrap().to_le_bytes());
    want.extend_from_slice(&u32::try_from(frame.len()).unwrap().to_le_bytes());
    let ip: [u8; 20] = out[16 + 14..16 + 34].try_into().unwrap();
    assert_eq!(ip_checksum(&ip), 0, "the written checksum sums to 0xffff");
    frame[24..26].copy_from_slice(&ip[10..12]);
    want.extend_from_slice(&frame);
    assert_eq!(out, want);
    assert_eq!(sp.capture_kind(), Some(CaptureKind::PcapNg));
    assert_eq!(sp.pending(), 0);
    assert_eq!(
        sp.blocks().iter().map(|(&b, &n)| (b, n)).collect::<Vec<_>>(),
        [(0x0a0d_0d0a, 1), (1, 1), (5, 1), (6, 1)]
    );
}

/// A day in miniature over three buckets: System Events in every bucket, a source gap carried as a jump in each,
/// a duplicate and a partial duplicate dropped, heartbeats, unknown and malformed messages, a Trading Status and a
/// test security.
fn small_day() -> Vec<Segment> {
    let t = |s: i64| MID + 34_200_000_000_000 + s * 1_000_000;
    let a = |sym: &str, s: i64| sym_msg(b'a', b'8', t(s), sym, 20);
    let l = |sym: &str, s: i64| sym_msg(b'L', 0, t(s), sym, 28);
    let mut h = sym_msg(b'H', b'P', t(9), "SPY", 0);
    h.extend_from_slice(b"LUDP");
    vec![
        segment(1, vec![system(b'O', t(1))], t(1)),
        segment(2, vec![a("AAPL", 2), a("TSLA", 2)], t(2)),
        segment(4, vec![], t(3)),
        segment(4, vec![a("AAPL", 4)], t(4)),
        segment(7, vec![a("TSLA", 5), l("AAPL", 5)], t(5)),
        segment(7, vec![a("TSLA", 5), l("AAPL", 5)], t(6)),
        segment(8, vec![l("AAPL", 5), a("SPY", 7)], t(7)),
        segment(
            10,
            vec![
                sym_msg(b'Z', 0, t(8), "AAPL", 2),
                vec![],
                sym_msg(b'a', 0, t(8), "AA", 0)[..10].to_vec(),
            ],
            t(8),
        ),
        segment(13, vec![h, sym_msg(b'D', 0x80, t(9), "ZIEXT", 13)], t(9)),
    ]
}

#[test]
fn routes_and_renumbers_a_small_day() {
    let data = synth::pcapng(&synth::frames(&small_day()));
    let mut sp = run(3, &data, &[], None);
    let outs = outputs(&mut sp);
    let st = sp.state();
    let c = &st.counters;
    assert_eq!((c.packets, c.segments, c.heartbeats, c.messages), (9, 9, 1, 15));
    assert_eq!(
        (c.sequence_gaps, c.sequence_gap_messages, c.duplicate_messages),
        (1, 2, 3)
    );
    assert_eq!(
        (c.parsed, c.system, c.routed, c.unknown_type, c.malformed),
        (12, 1, 8, 1, 2)
    );
    assert_eq!((c.dropped, c.prefiltered, c.bad_segments), (0, 0, 0));
    assert_eq!(
        st.unknown_types.iter().map(|(&t, &n)| (t, n)).collect::<Vec<_>>(),
        [(b'Z', 1)]
    );
    assert_eq!(
        st.sessions,
        [SessionStart {
            channel: 1,
            session: 77,
            first_seq: 1
        }]
    );
    assert_eq!(st.system_events.get(&b'O'), Some(&34_200_001_000_000i128));
    let syms: Vec<(&str, u32, u32, u64, u64, bool)> = st
        .symbols
        .iter()
        .map(|s| (s.name.as_str(), s.bucket, s.id, s.messages, s.executions, s.test_issue))
        .collect();
    assert_eq!(
        syms,
        [
            ("AAPL", 2, 1, 3, 1, false),
            ("TSLA", 0, 1, 2, 0, false),
            ("SPY", 1, 1, 2, 0, false),
            ("ZIEXT", 2, 2, 1, 0, true)
        ]
    );
    assert_eq!(st.trading_status_order, [2]);
    assert_eq!(
        st.symbols[2].trading_status,
        [(34_200_009_000_000i128, b'P', "LUDP".to_owned())]
    );
    assert_eq!(
        (st.bucket_messages.as_slice(), st.bucket_segments.as_slice()),
        (&[3, 3, 5][..], &[3, 3, 5][..])
    );
    // per bucket: the renumbered first sequence numbers (the gap of 2 carried) and stream offsets
    let seqs = |b: usize| {
        packets(&outs[b])
            .iter()
            .map(|(_, h, _)| (h.first_seq, h.stream_offset))
            .collect::<Vec<_>>()
    };
    assert_eq!(seqs(0), [(1, 0), (2, 12), (5, 52)]);
    assert_eq!(seqs(1), [(1, 0), (4, 12), (5, 52)]);
    assert_eq!(seqs(2), [(1, 0), (2, 12), (3, 52), (6, 92), (7, 140)]);
    for out in &outs {
        let p = packets(out);
        assert_eq!(
            p[0].2,
            [system(b'O', MID + 34_200_001_000_000)],
            "the System Event is in every bucket"
        );
        assert!(p.iter().all(|(_, h, _)| h.channel_id == 1 && h.session_id == 77));
    }
}

#[test]
fn chunking_does_not_change_anything() {
    for data in [
        synth::pcapng(&synth::frames(&small_day())),
        synth::pcap(&synth::frames(&small_day())),
    ] {
        let mut whole = run(3, &data, &[], None);
        let want = outputs(&mut whole);
        let cases: [&[usize]; 4] = [&[1], &[7], &[13, 1000, 2], &[4096]];
        for sizes in cases {
            let mut sp = run(3, &data, sizes, None);
            assert_eq!(outputs(&mut sp), want, "{sizes:?}");
            assert_eq!(sp.state().counters, whole.state().counters);
            assert_eq!(sp.state().symbols, whole.state().symbols);
        }
    }
}

#[test]
fn symbol_filter_and_prefilter() {
    let data = synth::pcapng(&synth::frames(&small_day()));
    let mut sp = run(2, &data, &[], Some(&["SPY"]));
    let outs = outputs(&mut sp);
    let st = sp.state();
    let c = &st.counters;
    // parsed: the System Event segment, the AAPL / SPY segment (its new SPY message) and the H SPY / D ZIEXT one
    assert_eq!(
        (c.parsed, c.prefiltered, c.routed, c.dropped, c.system),
        (4, 8, 2, 1, 1)
    );
    assert_eq!(st.symbols.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["SPY"]);
    assert_eq!(c.messages - c.duplicate_messages, c.parsed + c.prefiltered);
    let b = bucket_of("SPY", 2) as usize;
    assert_eq!(packets(&outs[b]).len(), 3);
    assert_eq!(packets(&outs[1 - b]).len(), 1);
}

#[test]
fn big_endian_pcapng_and_its_resolutions() {
    fn block(out: &mut Vec<u8>, btype: u32, body: &[u8]) {
        let len = u32::try_from(12 + body.len()).unwrap();
        out.extend_from_slice(&btype.to_be_bytes());
        out.extend_from_slice(&len.to_be_bytes());
        out.extend_from_slice(body);
        out.extend_from_slice(&len.to_be_bytes());
    }
    let t = MID + 1_234_567_891;
    let ut = u64::try_from(t).unwrap();
    let frame = synth::frame(&synth::segment_bytes(&segment(
        1,
        vec![sym_msg(b'a', 0, t, "AAPL", 20)],
        t,
    )));
    // if_tsresol 9 (nanoseconds) and 0x80 | 10 (1024 ticks per second)
    for res in [9u8, 0x80 | 10] {
        let ticks = if res == 9 {
            ut
        } else {
            u64::try_from(u128::from(ut) * 1024 / NS).unwrap()
        };
        let mut b = Vec::new();
        block(
            &mut b,
            0x0a0d_0d0a,
            &[
                0x1a, 0x2b, 0x3c, 0x4d, 0, 1, 0, 0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
            ],
        );
        block(&mut b, 1, &[0, 1, 0, 0, 0, 0, 0xff, 0xff, 0, 9, 0, 1, res, 0, 0, 0]);
        let mut epb = Vec::new();
        epb.extend_from_slice(&0u32.to_be_bytes());
        epb.extend_from_slice(&u32::try_from(ticks >> 32).unwrap().to_be_bytes());
        epb.extend_from_slice(&(ticks as u32).to_be_bytes());
        epb.extend_from_slice(&u32::try_from(frame.len()).unwrap().to_be_bytes());
        epb.extend_from_slice(&u32::try_from(frame.len()).unwrap().to_be_bytes());
        epb.extend_from_slice(&frame);
        epb.resize(epb.len().div_ceil(4) * 4, 0);
        block(&mut b, 6, &epb);
        let mut sp = run(1, &b, &[5], None);
        let p = packets(&outputs(&mut sp)[0]);
        let want = if res == 9 {
            t
        } else {
            i64::try_from(u128::from(ticks) * (NS / 1024)).unwrap()
        };
        assert_eq!(p.len(), 1);
        assert_eq!(p[0].0, want);
    }
}

#[test]
fn errors_are_the_reference_ones_and_stick() {
    let mut sp = Splitter::new(2, 0, None, None).unwrap();
    let e = sp.feed(b"definitely not a pcap file at all").unwrap_err();
    assert_eq!(
        e,
        SplitError::Capture("not a pcap / pcapng capture (magic 64656669)".into())
    );
    assert_eq!(sp.feed(b"more").unwrap_err(), e);

    let mut data = synth::pcapng(&[]);
    data.extend_from_slice(&[6, 0, 0, 0, 13, 0, 0, 0, 0, 0, 0, 0]);
    let mut sp = Splitter::new(1, 0, None, None).unwrap();
    assert_eq!(
        sp.feed(&data).unwrap_err(),
        SplitError::Capture("pcapng block length 13 at block 3".into())
    );

    let t = MID + 1;
    let data = synth::pcapng(&synth::frames(&[segment(
        1,
        vec![sym_msg(b'a', 0, t, "A\u{e9}", 20)],
        t,
    )]));
    let mut sp = Splitter::new(1, 0, None, None).unwrap();
    assert!(matches!(sp.feed(&data), Err(SplitError::Unsupported(m)) if m.contains("not ASCII")));
    // a symbol not requested is dropped before its name is encoded (no prefilter here, so it is walked)
    let mut sp = Splitter::new(1, 0, Some(vec!["AAPL".to_owned()]), None).unwrap();
    sp.feed(&data).unwrap();
    assert_eq!(sp.state().counters.dropped, 1);
    assert!(outputs(&mut sp)[0].is_empty());
    assert!(Splitter::new(0, 0, None, None).is_err());
}

#[test]
fn incomplete_input_is_pending() {
    let data = synth::pcap(&synth::frames(&small_day()));
    let mut sp = run(1, &data[..data.len() - 5], &[], None);
    assert_eq!(sp.capture_kind(), Some(CaptureKind::Pcap));
    assert!(sp.pending() > 0);
    assert_eq!(sp.state().counters.packets, 8);
    sp.feed(&data[data.len() - 5..]).unwrap();
    assert_eq!((sp.pending(), sp.state().counters.packets), (0, 9));
    let mut short = Splitter::new(1, 0, None, None).unwrap();
    short.feed(&data[..23]).unwrap();
    assert_eq!((short.capture_kind(), short.pending()), (None, 23));
}

/// A valid capture with bytes overwritten, or arbitrary bytes after a capture header.
fn mangled(seed: u64, base: &[u8], flips: &[(usize, u8)], tail: &[u8]) -> Vec<u8> {
    let mut v = base.to_vec();
    for &(i, b) in flips {
        if !v.is_empty() {
            let at = (i ^ seed as usize) % v.len();
            v[at] = b;
        }
    }
    v.extend_from_slice(tail);
    v
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 256, ..ProptestConfig::default() })]

    /// No input and no chunking makes the splitter panic, and an error stops it for good.
    #[test]
    fn never_panics(
        seed in any::<u64>(),
        which in 0u8..4,
        flips in prop::collection::vec((any::<usize>(), any::<u8>()), 0..12),
        tail in prop::collection::vec(any::<u8>(), 0..300),
        sizes in prop::collection::vec(1usize..400, 1..6),
        k in 1usize..5,
        filtered in any::<bool>(),
    ) {
        let base = match which {
            0 => synth::pcapng(&synth::frames(&small_day())),
            1 => synth::pcap(&synth::frames(&small_day())),
            2 => synth::pcapng(&[]),
            _ => Vec::new(),
        };
        let data = mangled(seed, &base, &flips, &tail);
        let symbols = filtered.then(|| vec!["SPY".to_owned(), "AAPL".to_owned()]);
        let patterns = filtered.then(|| vec![b"AAPL    ".to_vec(), b"SPY     ".to_vec(), b"\x0a\x00S".to_vec()]);
        let mut sp = Splitter::new(k, i128::from(MID), symbols, patterns).unwrap();
        let (mut i, mut j) = (0, 0);
        let mut failed: Option<SplitError> = None;
        while i < data.len() {
            let n = sizes[j % sizes.len()].min(data.len() - i);
            let r = sp.feed(&data[i..i + n]);
            if let Some(e) = &failed {
                prop_assert_eq!(&r, &Err(e.clone()));
            } else if let Err(e) = r {
                failed = Some(e);
            }
            i += n;
            j += 1;
        }
        let st = sp.state();
        let c = &st.counters;
        // (messages - duplicates = parsed + prefiltered only holds while no segment announces more messages than it
        // carries, a manifest check of the split rather than an invariant)
        if failed.is_none() {
            prop_assert_eq!(c.parsed, c.system + c.routed + c.dropped + c.unknown_type + c.malformed);
            prop_assert!(c.messages - c.duplicate_messages >= c.parsed + c.prefiltered);
            prop_assert_eq!(st.bucket_messages.iter().sum::<u64>(), c.routed + k as u64 * c.system);
        }
    }
}
