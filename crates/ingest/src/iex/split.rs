//! Incremental IEX DEEP+ symbol splitter: the fast engine of `scripts/scale/iex_split.py` (its default `--engine
//! fast`, through `queuetruth._core.IexSplit`).
//!
//! A port of the Python engine, which stays the reference (`--engine python`): `Capture`, `udp_payload`,
//! `ip_checksum` and `PcapWriter` of `queuetruth.demo.iex`, and `IexSplitter` of `iex_split.py`. For every input and
//! every chunking of it, it reads the same packets, keeps the same counters and maps, and writes the same classic
//! pcap records to each bucket. The crate's own readers ([`super::capture`], [`super::transport`]) are not used:
//! they differ from the Python reader on malformed input (block size limits, trailer checks, unknown interfaces,
//! skip reasons).
//!
//! [`Splitter::feed`] takes the inflated capture in pieces of any size and appends each bucket's pcap records to that
//! bucket's output ([`Splitter::outputs_mut`]). The 24-byte pcap file header is not in it: the caller writes it first,
//! then hashes and gzips the outputs, as the Python engine does.
//!
//! On a few malformed inputs the Python engine stops with a Python exception other than `CaptureError`: a routed
//! symbol field with a byte above 0x7f, a capture time after 2106, a pcapng interface option cut by the end of its
//! block, an Enhanced Packet Block shorter than its header at the end of the bytes received, a renumbered segment
//! too long for one datagram, or a renumbered sequence number or offset outside int64. There this stops with
//! [`SplitError::Unsupported`]. No input makes this module panic.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::hash::{BuildHasherDefault, Hasher};

use indexmap::IndexMap;

/// Message Protocol ID of DEEP+.
const DEEP_PLUS: u16 = 0x8005;
const NS: u128 = 1_000_000_000;
/// Route of a symbol that is not routed (`--symbols` given and the symbol not in it).
const DROP: u32 = u32::MAX;
/// Kept-message target of a System Event: every bucket.
const ALL: u32 = u32::MAX;
const SHB: [u8; 4] = [0x0a, 0x0d, 0x0d, 0x0a];
/// Largest IEX-TP segment (header and messages) of a written packet: `20 + 8 + len` must fit the IPv4 total length.
const MAX_TP_LEN: usize = 65_535 - 28;

/// Why a split stopped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SplitError {
    /// Not a pcap / pcapng capture, or a pcapng block length below 12 or not a multiple of 4 (the Python engine's
    /// `CaptureError`, with its message).
    Capture(String),
    /// An input on which the Python engine stops with another Python exception (module docs).
    Unsupported(String),
}

impl fmt::Display for SplitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Capture(m) | Self::Unsupported(m) => f.write_str(m),
        }
    }
}

impl std::error::Error for SplitError {}

/// Multiply-rotate hasher for the small integer keys of the hot path (symbol fields, channel and session).
#[derive(Default)]
struct FastHasher(u64);

impl Hasher for FastHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.write_u64(u64::from(b));
        }
    }

    fn write_u32(&mut self, n: u32) {
        self.write_u64(u64::from(n));
    }

    fn write_u64(&mut self, n: u64) {
        self.0 = (self.0.rotate_left(5) ^ n).wrapping_mul(0x51_7c_c1_b7_27_22_0a_95);
    }
}

type FastMap<K, V> = HashMap<K, V, BuildHasherDefault<FastHasher>>;

#[inline]
fn le16(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}

#[inline]
fn le32(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]])
}

#[inline]
fn le64(b: &[u8], o: usize) -> i64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[o..o + 8]);
    i64::from_le_bytes(a)
}

#[inline]
fn be16(b: &[u8], o: usize) -> u16 {
    u16::from_be_bytes([b[o], b[o + 1]])
}

#[inline]
fn u16_e(b: &[u8], o: usize, big: bool) -> u16 {
    let a = [b[o], b[o + 1]];
    if big {
        u16::from_be_bytes(a)
    } else {
        u16::from_le_bytes(a)
    }
}

#[inline]
fn u32_e(b: &[u8], o: usize, big: bool) -> u32 {
    let a = [b[o], b[o + 1], b[o + 2], b[o + 3]];
    if big {
        u32::from_be_bytes(a)
    } else {
        u32::from_le_bytes(a)
    }
}

/// `b[start:stop]` with Python's slice clamping.
#[inline]
fn py_slice(b: &[u8], start: usize, stop: usize) -> &[u8] {
    let stop = stop.min(b.len());
    if start >= stop { &[] } else { &b[start..stop] }
}

/// Python's `str.isspace` on ASCII: `\t \n \v \f \r`, the separators 0x1c to 0x1f, and space.
#[inline]
fn py_space(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\u{0b}' | '\u{0c}' | '\r' | '\u{1c}'..='\u{1f}' | ' ')
}

/// `b.decode("ascii", "replace")`.
fn ascii_replace(b: &[u8]) -> String {
    b.iter()
        .map(|&x| if x < 0x80 { char::from(x) } else { '\u{fffd}' })
        .collect()
}

/// `crc32(name ASCII, right-padded with spaces to 8 bytes) mod k`, the bucket rule of `itch_split.py`.
pub fn bucket_of(name: &str, k: u32) -> u32 {
    let mut padded = name.as_bytes().to_vec();
    if padded.len() < 8 {
        padded.resize(8, b' ');
    }
    let mut crc = flate2::Crc::new();
    crc.update(&padded);
    crc.sum() % k.max(1)
}

/// `ip_checksum` of a 20-byte IPv4 header.
fn ip_checksum(h: &[u8; 20]) -> u16 {
    let mut s: u32 = h.chunks_exact(2).map(|w| u32::from(be16(w, 0))).sum();
    s = (s & 0xffff) + (s >> 16);
    s = (s & 0xffff) + (s >> 16);
    !(s as u16)
}

/// (IPv4 source and destination addresses, UDP ports, UDP payload) of a frame.
pub type UdpParts<'a> = (&'a [u8], &'a [u8], &'a [u8]);

/// `udp_payload` of `queuetruth.demo.iex`: the parts of an IPv4 / UDP frame, or the reason it is skipped.
pub fn udp_payload(linktype: u16, frame: &[u8]) -> Result<UdpParts<'_>, &'static str> {
    let n = frame.len();
    let i = match linktype {
        1 => {
            if n < 14 {
                return Err("short_frame");
            }
            let (mut et, mut i) = (be16(frame, 12), 14);
            while (et == 0x8100 || et == 0x88a8) && n >= i + 4 {
                et = be16(frame, i + 2);
                i += 4;
            }
            if et != 0x0800 {
                return Err("not_ipv4");
            }
            i
        }
        101 | 228 => 0,
        113 => {
            if n < 16 || be16(frame, 14) != 0x0800 {
                return Err("not_ipv4");
            }
            16
        }
        _ => return Err("linktype"),
    };
    if n < i + 20 || frame[i] >> 4 != 4 {
        return Err("not_ipv4");
    }
    let ihl = usize::from(frame[i] & 0x0f) * 4;
    let total = usize::from(be16(frame, i + 2));
    let flags_frag = be16(frame, i + 6);
    if frame[i + 9] != 17 {
        return Err("not_udp");
    }
    if flags_frag & 0x3fff != 0 {
        return Err("ip_fragment");
    }
    let u = i + ihl;
    if n < u + 8 {
        return Err("short_frame");
    }
    let ulen = usize::from(be16(frame, u + 4));
    let ip_end = if total != 0 { i + total } else { n };
    let udp_end = if ulen >= 8 { u + ulen } else { n };
    let end = n.min(ip_end).min(udp_end);
    Ok((&frame[i + 12..i + 20], &frame[u..u + 4], py_slice(frame, u + 8, end)))
}

/// Ticks per second of a pcapng interface (`if_tsresol`, default microseconds). A resolution above `u128::MAX`
/// saturates, which gives the same capture times (0 either way).
fn tsresol(opts: &[u8], big: bool) -> Result<u128, SplitError> {
    let mut i = 0usize;
    while i + 4 <= opts.len() {
        let code = u16_e(opts, i, big);
        let ln = usize::from(u16_e(opts, i + 2, big));
        if code == 0 {
            break;
        }
        if code == 9 && ln >= 1 {
            let Some(&v) = opts.get(i + 4) else {
                return Err(SplitError::Unsupported(
                    "pcapng interface option if_tsresol cut by the end of its block".into(),
                ));
            };
            let e = u32::from(v & 0x7f);
            return Ok(if v & 0x80 != 0 {
                1u128 << e
            } else {
                10u128.checked_pow(e).unwrap_or(u128::MAX)
            });
        }
        i += 4 + ((ln + 3) & !3);
    }
    Ok(1_000_000)
}

/// Format of the capture.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureKind {
    /// Classic pcap.
    Pcap,
    /// pcapng.
    PcapNg,
}

impl CaptureKind {
    /// `pcap` or `pcapng` (the Python reader's `kind`).
    pub fn name(self) -> &'static str {
        match self {
            Self::Pcap => "pcap",
            Self::PcapNg => "pcapng",
        }
    }
}

/// Incremental reader with the semantics of the Python `Capture`: every complete block of the bytes received so far
/// is read, and a packet whose header or data runs past its block reads the bytes after it, up to the end of the
/// bytes received (as the slices of the Python reader's buffer do).
#[derive(Default)]
struct Capture {
    buf: Vec<u8>,
    pos: usize,
    kind: Option<CaptureKind>,
    big: bool,
    /// (linktype, ticks per second) per pcapng interface.
    ifaces: Vec<(u16, u128)>,
    pcap_linktype: u16,
    pcap_ns: u128,
    blocks: IndexMap<u32, u64>,
    n_blocks: u64,
}

impl Capture {
    /// `_detect`: false while fewer than 24 bytes are there.
    fn detect(&mut self) -> Result<bool, SplitError> {
        let b = &self.buf[self.pos..];
        if b.len() < 24 {
            return Ok(false);
        }
        let magic = [b[0], b[1], b[2], b[3]];
        if magic == SHB {
            self.kind = Some(CaptureKind::PcapNg);
            return Ok(true);
        }
        let (big, ns) = match magic {
            [0xd4, 0xc3, 0xb2, 0xa1] => (false, 1000),
            [0xa1, 0xb2, 0xc3, 0xd4] => (true, 1000),
            [0x4d, 0x3c, 0xb2, 0xa1] => (false, 1),
            [0xa1, 0xb2, 0x3c, 0x4d] => (true, 1),
            _ => {
                let hex: String = magic.iter().map(|x| format!("{x:02x}")).collect();
                return Err(SplitError::Capture(format!(
                    "not a pcap / pcapng capture (magic {hex})"
                )));
            }
        };
        self.kind = Some(CaptureKind::Pcap);
        self.big = big;
        self.pcap_ns = ns;
        self.pcap_linktype = (u32_e(b, 20, big) & 0xffff) as u16;
        self.pos += 24;
        Ok(true)
    }

    /// Calls `f(capture ns, linktype, frame)` for every packet of the complete blocks received.
    fn packets<F>(&mut self, mut f: F) -> Result<(), SplitError>
    where
        F: FnMut(u128, u16, &[u8]) -> Result<(), SplitError>,
    {
        if self.kind.is_none() && !self.detect()? {
            return Ok(());
        }
        let buf = &self.buf;
        if self.kind == Some(CaptureKind::Pcap) {
            let big = self.big;
            while buf.len() - self.pos >= 16 {
                let p = self.pos;
                let sec = u128::from(u32_e(buf, p, big));
                let frac = u128::from(u32_e(buf, p + 4, big));
                let incl = u32_e(buf, p + 8, big) as usize;
                if buf.len() - p - 16 < incl {
                    return Ok(());
                }
                self.pos = p + 16 + incl;
                f(
                    sec * NS + frac * self.pcap_ns,
                    self.pcap_linktype,
                    &buf[p + 16..self.pos],
                )?;
            }
            return Ok(());
        }
        while buf.len() - self.pos >= 12 {
            let p = self.pos;
            if buf[p..p + 4] == SHB {
                self.big = buf[p + 8..p + 12] != [0x4d, 0x3c, 0x2b, 0x1a];
            }
            let big = self.big;
            let btype = u32_e(buf, p, big);
            let blen = u32_e(buf, p + 4, big) as usize;
            if blen < 12 || !blen.is_multiple_of(4) {
                return Err(SplitError::Capture(format!(
                    "pcapng block length {blen} at block {}",
                    self.n_blocks
                )));
            }
            if buf.len() - p < blen {
                return Ok(());
            }
            let body = p + 8;
            let end = p + blen - 4;
            self.pos = p + blen;
            *self.blocks.entry(btype).or_insert(0) += 1;
            self.n_blocks += 1;
            match btype {
                0x0a0d_0d0a => self.ifaces.clear(),
                1 => {
                    let linktype = u16_e(buf, body, big);
                    let tps = tsresol(py_slice(buf, body + 8, end), big)?;
                    self.ifaces.push((linktype, tps));
                }
                6 => {
                    if buf.len() < body + 20 {
                        return Err(SplitError::Unsupported(format!(
                            "pcapng Enhanced Packet Block of {blen} bytes, shorter than its header, at the end of \
                             the bytes received"
                        )));
                    }
                    let iface = u32_e(buf, body, big) as usize;
                    let ticks = (u128::from(u32_e(buf, body + 4, big)) << 32) | u128::from(u32_e(buf, body + 8, big));
                    let cap = u32_e(buf, body + 12, big) as usize;
                    let (linktype, tps) = self.ifaces.get(iface).copied().unwrap_or((1, 1_000_000));
                    let ts = if tps <= NS {
                        ticks * (NS / tps)
                    } else {
                        ticks * NS / tps
                    };
                    f(ts, linktype, py_slice(buf, body + 20, body + 20 + cap))?;
                }
                3 => {
                    let linktype = self.ifaces.first().map_or(1, |i| i.0);
                    let orig = u32_e(buf, body, big) as usize;
                    f(0, linktype, py_slice(buf, body + 4, end.min(body + 4 + orig)))?;
                }
                _ => {}
            }
        }
        Ok(())
    }
}

/// Literal search of `re.compile(b"|".join(re.escape(p) for p in patterns)).search(haystack)`.
struct Prefilter {
    any_empty: bool,
    by_first: Vec<Vec<Vec<u8>>>,
}

impl Prefilter {
    fn new(patterns: Vec<Vec<u8>>) -> Self {
        let mut by_first = vec![Vec::new(); 256];
        let mut any_empty = false;
        for p in patterns {
            match p.first().copied() {
                Some(b) => by_first[usize::from(b)].push(p),
                None => any_empty = true,
            }
        }
        Self { any_empty, by_first }
    }

    fn found_in(&self, h: &[u8]) -> bool {
        self.any_empty
            || h.iter().enumerate().any(|(i, &b)| {
                let pats = &self.by_first[usize::from(b)];
                !pats.is_empty() && pats.iter().any(|p| h[i..].starts_with(p))
            })
    }
}

/// The counters of `IexSplitter.c`.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Counters {
    /// Packets read.
    pub packets: u64,
    /// IEX-TP segments that are not version 1 or shorter than 40 bytes.
    pub bad_segments: u64,
    /// DEEP+ segments.
    pub segments: u64,
    /// DEEP+ segments without messages.
    pub heartbeats: u64,
    /// Messages announced by the DEEP+ segments.
    pub messages: u64,
    /// Sequence gaps (a segment starting after the next expected sequence number).
    pub sequence_gaps: u64,
    /// Messages missing in the gaps.
    pub sequence_gap_messages: i128,
    /// Messages already seen.
    pub duplicate_messages: u64,
    /// New messages of segments without a requested symbol or System Event pattern (`--symbols`).
    pub prefiltered: u64,
    /// New messages walked.
    pub parsed: u64,
    /// Empty messages, and symbol messages shorter than 18 bytes.
    pub malformed: u64,
    /// System Events.
    pub system: u64,
    /// Symbol messages routed to a bucket.
    pub routed: u64,
    /// Symbol messages of symbols not requested.
    pub dropped: u64,
    /// Messages of an unknown type.
    pub unknown_type: u64,
}

/// One routed symbol: its name, bucket, instrument id (order of first appearance in the bucket, from 1) and
/// counters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SymbolStats {
    /// `symbol field.decode("ascii", "replace").rstrip()`.
    pub name: String,
    /// Bucket.
    pub bucket: u32,
    /// Instrument id in the bucket.
    pub id: u32,
    /// Routed messages.
    pub messages: u64,
    /// `L` and `T` messages.
    pub executions: u64,
    /// A Security Directory message flagged a test security (0x80).
    pub test_issue: bool,
    /// Trading Status messages: (ns since midnight New York, status byte, reason).
    pub trading_status: Vec<(i128, u8, String)>,
}

/// A (channel, session) at its first DEEP+ segment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SessionStart {
    /// Channel ID.
    pub channel: u32,
    /// Session ID.
    pub session: u32,
    /// First Message Sequence Number of its first DEEP+ segment.
    pub first_seq: i64,
}

/// Sequence state of one (channel, session).
struct KeyState {
    /// Next expected source sequence number.
    src: i128,
    /// Per bucket: (next sequence number, stream offset) of the renumbered stream.
    dst: Vec<(i128, i128)>,
}

/// A kept message of the current segment: target bucket (or `ALL`) and its bytes in the segment body.
#[derive(Clone, Copy)]
struct Kept {
    target: u32,
    start: usize,
    end: usize,
}

/// Where the kept messages of a segment go.
#[derive(Clone, Copy)]
struct Targets {
    /// A System Event: every bucket.
    all: bool,
    /// Bucket of the first kept symbol message (`ALL` when none).
    first: u32,
    /// Kept symbol messages of more than one bucket.
    multi: bool,
}

/// The current segment's fields needed to write it.
struct Seg<'a> {
    ts: u128,
    addrs: &'a [u8],
    ports: &'a [u8],
    chan: u32,
    sess: u32,
    send: i64,
    body: &'a [u8],
    key: usize,
}

/// Routes the DEEP+ messages of a capture into `k` bucket pcaps (module docs).
pub struct Splitter {
    cap: Capture,
    failed: Option<SplitError>,
    state: SplitState,
}

impl Splitter {
    /// A splitter into `k` buckets; `midnight_utc_ns` is New York midnight of the session date. With `symbols`
    /// (non-empty), only those are routed, and `patterns` (the padded symbols and the System Event pattern, as the
    /// Python engine builds them) is the prefilter that skips a segment holding none of them.
    pub fn new(
        k: usize,
        midnight_utc_ns: i128,
        symbols: Option<Vec<String>>,
        patterns: Option<Vec<Vec<u8>>>,
    ) -> Result<Self, SplitError> {
        let k = u32::try_from(k)
            .ok()
            .filter(|&k| (1..u32::MAX).contains(&k))
            .ok_or_else(|| SplitError::Unsupported("one output per bucket".into()))?;
        let n = k as usize;
        let only: Option<HashSet<String>> = symbols.filter(|s| !s.is_empty()).map(|s| s.into_iter().collect());
        let prefilter = if only.is_some() {
            patterns.map(Prefilter::new)
        } else {
            None
        };
        Ok(Self {
            cap: Capture::default(),
            failed: None,
            state: SplitState {
                k,
                midnight: midnight_utc_ns,
                only,
                prefilter,
                route: FastMap::default(),
                names: HashMap::new(),
                ids: vec![0; n],
                key_index: FastMap::default(),
                last_key: None,
                keys: Vec::new(),
                kept: Vec::new(),
                outs: vec![Vec::new(); n],
                counters: Counters::default(),
                skipped_packets: IndexMap::new(),
                other_protocol: IndexMap::new(),
                unknown_types: IndexMap::new(),
                sessions: Vec::new(),
                first_send_ns: None,
                last_send_ns: None,
                system_events: IndexMap::new(),
                symbols: Vec::new(),
                trading_status_order: Vec::new(),
                bucket_messages: vec![0; n],
                bucket_segments: vec![0; n],
            },
        })
    }

    /// Takes the next bytes of the capture and routes every packet of the complete blocks received so far. After an
    /// error every later call returns it again.
    pub fn feed(&mut self, data: &[u8]) -> Result<(), SplitError> {
        if let Some(e) = &self.failed {
            return Err(e.clone());
        }
        let cap = &mut self.cap;
        if cap.pos > 0 {
            cap.buf.drain(..cap.pos);
            cap.pos = 0;
        }
        cap.buf.extend_from_slice(data);
        let st = &mut self.state;
        let r = cap.packets(|ts, lt, frame| st.packet(ts, lt, frame));
        if let Err(e) = &r {
            self.failed = Some(e.clone());
        }
        r
    }

    /// Each bucket's pcap records since the outputs were last cleared (the caller clears them once written).
    pub fn outputs_mut(&mut self) -> &mut [Vec<u8>] {
        &mut self.state.outs
    }

    /// The counters and maps.
    pub fn state(&self) -> &SplitState {
        &self.state
    }

    /// Bytes received and not read yet (an incomplete block at the end of the input).
    pub fn pending(&self) -> usize {
        self.cap.buf.len() - self.cap.pos
    }

    /// Format of the capture, once its first 24 bytes are there.
    pub fn capture_kind(&self) -> Option<CaptureKind> {
        self.cap.kind
    }

    /// pcapng blocks read per block type, in order of first appearance.
    pub fn blocks(&self) -> &IndexMap<u32, u64> {
        &self.cap.blocks
    }
}

/// The routing state of a [`Splitter`]: the counters and maps of the Python `IexSplitter` (public) and the
/// bookkeeping behind them.
pub struct SplitState {
    k: u32,
    midnight: i128,
    only: Option<HashSet<String>>,
    prefilter: Option<Prefilter>,
    /// Symbol field -> index into `symbols`, or `DROP`.
    route: FastMap<u64, u32>,
    /// Symbol name -> index into `symbols`.
    names: HashMap<String, u32>,
    /// Symbols per bucket so far (the last instrument id given).
    ids: Vec<u32>,
    key_index: FastMap<(u32, u32), usize>,
    last_key: Option<(u32, u32, usize)>,
    keys: Vec<KeyState>,
    kept: Vec<Kept>,
    outs: Vec<Vec<u8>>,
    /// Counters.
    pub counters: Counters,
    /// Skipped packets per `udp_payload` reason, in order of first appearance.
    pub skipped_packets: IndexMap<&'static str, u64>,
    /// Non-DEEP+ segments per Message Protocol ID, in order of first appearance.
    pub other_protocol: IndexMap<u16, u64>,
    /// Messages of unknown types per type byte, in order of first appearance.
    pub unknown_types: IndexMap<u8, u64>,
    /// Every (channel, session), in order of first appearance.
    pub sessions: Vec<SessionStart>,
    /// Send Time of the first DEEP+ segment.
    pub first_send_ns: Option<i64>,
    /// Send Time of the last DEEP+ segment.
    pub last_send_ns: Option<i64>,
    /// System Event code -> the timestamp of its last event in ns since midnight New York, in order of first
    /// appearance.
    pub system_events: IndexMap<u8, i128>,
    /// Routed symbols, in order of first routing.
    pub symbols: Vec<SymbolStats>,
    /// Indices into `symbols` of the symbols with Trading Status messages, in order of their first one.
    pub trading_status_order: Vec<u32>,
    /// Messages per bucket.
    pub bucket_messages: Vec<u64>,
    /// Segments per bucket.
    pub bucket_segments: Vec<u64>,
}

#[inline]
fn is_symbol_type(t: u8) -> bool {
    matches!(
        t,
        b'D' | b'H' | b'I' | b'O' | b'P' | b'E' | b'a' | b'M' | b'R' | b'L' | b'T' | b'B' | b'C'
    )
}

impl SplitState {
    #[inline]
    fn packet(&mut self, ts: u128, linktype: u16, frame: &[u8]) -> Result<(), SplitError> {
        self.counters.packets += 1;
        match udp_payload(linktype, frame) {
            Ok((addrs, ports, p)) => self.segment(ts, addrs, ports, p),
            Err(reason) => {
                *self.skipped_packets.entry(reason).or_insert(0) += 1;
                Ok(())
            }
        }
    }

    /// Index of the (channel, session) state, registered at its first DEEP+ segment.
    #[inline]
    fn key(&mut self, chan: u32, sess: u32, seq: i64) -> usize {
        if let Some((c, s, i)) = self.last_key
            && c == chan
            && s == sess
        {
            return i;
        }
        let i = match self.key_index.get(&(chan, sess)) {
            Some(&i) => i,
            None => {
                let i = self.keys.len();
                self.keys.push(KeyState {
                    src: i128::from(seq),
                    dst: vec![(i128::from(seq), 0); self.k as usize],
                });
                self.key_index.insert((chan, sess), i);
                self.sessions.push(SessionStart {
                    channel: chan,
                    session: sess,
                    first_seq: seq,
                });
                i
            }
        };
        self.last_key = Some((chan, sess, i));
        i
    }

    fn segment(&mut self, ts: u128, addrs: &[u8], ports: &[u8], p: &[u8]) -> Result<(), SplitError> {
        if p.len() < 40 || p[0] != 1 {
            self.counters.bad_segments += 1;
            return Ok(());
        }
        let proto = le16(p, 2);
        if proto != DEEP_PLUS {
            *self.other_protocol.entry(proto).or_insert(0) += 1;
            return Ok(());
        }
        let chan = le32(p, 4);
        let sess = le32(p, 8);
        let plen = usize::from(le16(p, 12));
        let count = le16(p, 14);
        let seq64 = le64(p, 24);
        let send = le64(p, 32);
        self.counters.segments += 1;
        if self.first_send_ns.is_none() {
            self.first_send_ns = Some(send);
        }
        self.last_send_ns = Some(send);
        let ki = self.key(chan, sess, seq64);
        let c = &mut self.counters;
        if count == 0 {
            c.heartbeats += 1;
            return Ok(());
        }
        c.messages += u64::from(count);
        let seq = i128::from(seq64);
        let n = i128::from(count);
        let key = &mut self.keys[ki];
        let mut expected = key.src;
        if seq > expected {
            let gap = seq - expected;
            c.sequence_gaps += 1;
            c.sequence_gap_messages += gap;
            for d in &mut key.dst {
                d.0 += gap;
            }
            expected = seq;
        } else if seq + n <= expected {
            c.duplicate_messages += u64::from(count);
            return Ok(());
        }
        // 0 <= skip < count here
        let skip = if seq < expected { (expected - seq) as u32 } else { 0 };
        c.duplicate_messages += u64::from(skip);
        key.src = seq + n;
        if let Some(pf) = &self.prefilter
            && !pf.found_in(&p[40..])
        {
            c.prefiltered += u64::from(u32::from(count) - skip);
            return Ok(());
        }
        let seg = Seg {
            ts,
            addrs,
            ports,
            chan,
            sess,
            send,
            body: py_slice(p, 40, 40 + plen),
            key: ki,
        };
        let mut kept = std::mem::take(&mut self.kept);
        kept.clear();
        let r = self
            .walk(&seg, u32::from(count), skip, &mut kept)
            .and_then(|to| self.write_kept(&seg, &kept, to));
        self.kept = kept;
        r
    }

    /// Bucket of a symbol field, or `DROP` (`IexSplitter._bucket`).
    fn route(&mut self, sym8: &[u8]) -> Result<u32, SplitError> {
        let mut a = [0u8; 8];
        a.copy_from_slice(sym8);
        let key = u64::from_le_bytes(a);
        if let Some(&r) = self.route.get(&key) {
            return Ok(r);
        }
        let mut name = ascii_replace(sym8);
        name.truncate(name.trim_end_matches(py_space).len());
        let r = if self.only.as_ref().is_some_and(|o| !o.contains(&name)) {
            DROP
        } else if !name.is_ascii() {
            let hex: String = sym8.iter().map(|x| format!("{x:02x}")).collect();
            return Err(SplitError::Unsupported(format!("symbol field {hex} is not ASCII")));
        } else if let Some(&i) = self.names.get(&name) {
            i
        } else {
            let bucket = bucket_of(&name, self.k);
            let ids = &mut self.ids[bucket as usize];
            *ids += 1;
            let i = self.symbols.len() as u32;
            self.symbols.push(SymbolStats {
                name: name.clone(),
                bucket,
                id: *ids,
                messages: 0,
                executions: 0,
                test_issue: false,
                trading_status: Vec::new(),
            });
            self.names.insert(name, i);
            i
        };
        self.route.insert(key, r);
        Ok(r)
    }

    /// Walks the messages of a segment body (the first `skip` were already seen) into `kept`.
    fn walk(&mut self, seg: &Seg<'_>, count: u32, skip: u32, kept: &mut Vec<Kept>) -> Result<Targets, SplitError> {
        let body = seg.body;
        let len = body.len();
        let mut to = Targets {
            all: false,
            first: ALL,
            multi: false,
        };
        let (mut i, mut n) = (0usize, 0u32);
        while n < count && i + 2 <= len {
            let ln = usize::from(body[i]) | (usize::from(body[i + 1]) << 8);
            let start = i + 2;
            let end = (start + ln).min(len);
            i = start + ln;
            n += 1;
            if n <= skip {
                continue;
            }
            self.counters.parsed += 1;
            if start >= end {
                self.counters.malformed += 1;
                continue;
            }
            let m = &body[start..end];
            let t = m[0];
            if t == b'S' {
                if m.len() >= 10 {
                    self.system_events.insert(m[1], i128::from(le64(m, 2)) - self.midnight);
                }
                self.counters.system += 1;
                kept.push(Kept {
                    target: ALL,
                    start,
                    end,
                });
                to.all = true;
            } else if is_symbol_type(t) {
                if m.len() < 18 {
                    self.counters.malformed += 1;
                    continue;
                }
                let si = self.route(&m[10..18])?;
                if si == DROP {
                    self.counters.dropped += 1;
                    continue;
                }
                self.counters.routed += 1;
                let sym = &mut self.symbols[si as usize];
                sym.messages += 1;
                match t {
                    b'L' | b'T' => sym.executions += 1,
                    b'H' if m.len() >= 22 => {
                        if sym.trading_status.is_empty() {
                            self.trading_status_order.push(si);
                        }
                        let mut reason = ascii_replace(&m[18..22]);
                        reason.truncate(reason.trim_end_matches(py_space).len());
                        let reason = reason.trim_start_matches(py_space).to_owned();
                        let ts = i128::from(le64(m, 2)) - self.midnight;
                        sym.trading_status.push((ts, m[1], reason));
                    }
                    b'D' if m[1] & 0x80 != 0 => sym.test_issue = true,
                    _ => {}
                }
                let b = sym.bucket;
                if to.first == ALL {
                    to.first = b;
                } else if b != to.first {
                    to.multi = true;
                }
                kept.push(Kept { target: b, start, end });
            } else {
                self.counters.unknown_type += 1;
                *self.unknown_types.entry(t).or_insert(0) += 1;
            }
        }
        Ok(to)
    }

    /// Writes the packet of every bucket with kept messages, in bucket order.
    fn write_kept(&mut self, seg: &Seg<'_>, kept: &[Kept], to: Targets) -> Result<(), SplitError> {
        if kept.is_empty() {
            Ok(())
        } else if to.all {
            (0..self.k).try_for_each(|b| self.write(b, seg, kept))
        } else if !to.multi {
            self.write(to.first, seg, kept)
        } else {
            let mut bs: Vec<u32> = kept.iter().map(|x| x.target).collect();
            bs.sort_unstable();
            bs.dedup();
            bs.into_iter().try_for_each(|b| self.write(b, seg, kept))
        }
    }

    /// Writes bucket `b`'s packet of the segment, its kept messages (and the System Events) renumbered, as
    /// `TP_HEADER.pack` and `PcapWriter.write` lay it out.
    fn write(&mut self, b: u32, seg: &Seg<'_>, kept: &[Kept]) -> Result<(), SplitError> {
        let sel = || kept.iter().filter(move |x| x.target == b || x.target == ALL);
        let (plen, cnt) = sel().fold((0usize, 0usize), |(p, c), x| (p + 2 + x.end - x.start, c + 1));
        let st = &mut self.keys[seg.key].dst[b as usize];
        let (seq, off) = *st;
        let (Ok(seq), Ok(off)) = (i64::try_from(seq), i64::try_from(off)) else {
            return Err(SplitError::Unsupported(format!(
                "renumbered sequence number {seq} or stream offset {off} of bucket {b} outside int64"
            )));
        };
        let tp_len = 40 + plen;
        if tp_len > MAX_TP_LEN {
            return Err(SplitError::Unsupported(format!(
                "renumbered segment of {tp_len} bytes is too long for one IPv4 datagram"
            )));
        }
        let Ok(sec) = u32::try_from(seg.ts / NS) else {
            return Err(SplitError::Unsupported(format!(
                "capture time {} ns is after 2106",
                seg.ts
            )));
        };
        st.0 += cnt as i128;
        st.1 += plen as i128;
        self.bucket_messages[b as usize] += cnt as u64;
        self.bucket_segments[b as usize] += 1;
        let frame_len = (14 + 20 + 8 + tp_len) as u32;
        let a = seg.addrs;
        let mut ip = [0u8; 20];
        ip[..12].copy_from_slice(&[0x45, 0, 0, 0, 0, 0, 0x40, 0, 64, 17, 0, 0]);
        ip[2..4].copy_from_slice(&((20 + 8 + tp_len) as u16).to_be_bytes());
        ip[12..].copy_from_slice(a);
        let ck = ip_checksum(&ip);
        ip[10..12].copy_from_slice(&ck.to_be_bytes());
        let out = &mut self.outs[b as usize];
        out.reserve(16 + frame_len as usize);
        out.extend_from_slice(&sec.to_le_bytes());
        out.extend_from_slice(&((seg.ts % NS) as u32).to_le_bytes());
        out.extend_from_slice(&frame_len.to_le_bytes());
        out.extend_from_slice(&frame_len.to_le_bytes());
        out.extend_from_slice(&[0x01, 0x00, 0x5e, a[5] & 0x7f, a[6], a[7], 0, 0, 0, 0, 0, 0, 0x08, 0x00]);
        out.extend_from_slice(&ip);
        out.extend_from_slice(seg.ports);
        out.extend_from_slice(&((8 + tp_len) as u16).to_be_bytes());
        // UDP checksum 0, then the IEX-TP header: version 1, reserved, DEEP+, ...
        out.extend_from_slice(&[0, 0, 1, 0]);
        out.extend_from_slice(&DEEP_PLUS.to_le_bytes());
        out.extend_from_slice(&seg.chan.to_le_bytes());
        out.extend_from_slice(&seg.sess.to_le_bytes());
        out.extend_from_slice(&(plen as u16).to_le_bytes());
        out.extend_from_slice(&(cnt as u16).to_le_bytes());
        out.extend_from_slice(&off.to_le_bytes());
        out.extend_from_slice(&seq.to_le_bytes());
        out.extend_from_slice(&seg.send.to_le_bytes());
        for x in sel() {
            out.extend_from_slice(&((x.end - x.start) as u16).to_le_bytes());
            out.extend_from_slice(&seg.body[x.start..x.end]);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
