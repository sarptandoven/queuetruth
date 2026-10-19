//! Capture framing: classic pcap (microsecond or nanosecond, either byte order) and pcapng (Section Header,
//! Interface Description with `if_tsresol` / `if_tsoffset`, Enhanced / Simple / obsolete Packet blocks; every other
//! block is skipped). IEX HIST files are gzipped pcapng; gzip is handled by the caller (`guard::open_maybe_gzip`).
//!
//! Framing errors that make the rest of the stream unreadable (bad magic, a block length below 12 or not a multiple
//! of 4 or above 16 MiB, a stream ending inside a block) are `QT-INPUT-002` naming the byte offset. A well-framed
//! block with inconsistent contents (unknown interface, captured length beyond the block) is counted in
//! `CaptureStats::bad_packets` and skipped. No input makes this module panic.

use std::io::{self, Read};
use std::path::{Path, PathBuf};

use qt_types::{QtError, QtResult};

use crate::guard::{GuardLimits, io_error, open_maybe_gzip};

/// Largest pcapng block or pcap record accepted.
pub const MAX_BLOCK: usize = 16 << 20;

/// One captured frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Packet<'a> {
    /// Capture time, ns since the epoch (`None` for pcapng Simple Packet blocks or an overflowing stamp).
    pub ts_ns: Option<i64>,
    /// Link type (1 Ethernet, 101 / 228 raw IPv4, 113 Linux SLL).
    pub linktype: u16,
    /// Captured bytes.
    pub data: &'a [u8],
}

/// Counters of the capture walk.
#[derive(Debug, Default, Clone, PartialEq, Eq, serde::Serialize)]
pub struct CaptureStats {
    /// `pcap` or `pcapng`.
    pub format: &'static str,
    /// Packets returned.
    pub packets: u64,
    /// pcapng blocks that carry no packet (section headers, interfaces, statistics, ...).
    pub other_blocks: u64,
    /// Packet blocks skipped as inconsistent.
    pub bad_packets: u64,
    /// Bytes consumed.
    pub bytes: u64,
}

#[derive(Clone, Copy)]
struct Iface {
    linktype: u16,
    /// Ticks per second.
    tps: u64,
    /// Seconds added to every stamp (`if_tsoffset`).
    offset_s: i64,
}

enum Kind {
    Unknown,
    Pcap { big: bool, ns_per_tick: i64, linktype: u16 },
    PcapNg { big: bool, ifaces: Vec<Iface> },
}

/// Packet reader over a (decompressed) capture stream.
pub struct CaptureReader<R: Read> {
    inner: R,
    buf: Vec<u8>,
    kind: Kind,
    path: Option<PathBuf>,
    /// Counters.
    pub stats: CaptureStats,
}

fn read_full<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<usize> {
    let mut got = 0;
    while got < buf.len() {
        match r.read(&mut buf[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(got)
}

#[inline]
fn u16_e(b: &[u8], big: bool) -> u16 {
    let a = [b[0], b[1]];
    if big {
        u16::from_be_bytes(a)
    } else {
        u16::from_le_bytes(a)
    }
}

#[inline]
fn u32_e(b: &[u8], big: bool) -> u32 {
    let a = [b[0], b[1], b[2], b[3]];
    if big {
        u32::from_be_bytes(a)
    } else {
        u32::from_le_bytes(a)
    }
}

/// `ticks / tps` seconds + `offset_s` as ns (`None` on overflow).
fn ticks_to_ns(ticks: u64, tps: u64, offset_s: i64) -> Option<i64> {
    let ns = u128::from(ticks).checked_mul(1_000_000_000)? / u128::from(tps.max(1));
    let ns = i64::try_from(ns).ok()?;
    ns.checked_add(offset_s.checked_mul(1_000_000_000)?)
}

impl<R: Read> CaptureReader<R> {
    /// Wraps a decompressed capture stream.
    pub fn new(inner: R) -> Self {
        Self {
            inner,
            buf: Vec::with_capacity(1 << 16),
            kind: Kind::Unknown,
            path: None,
            stats: CaptureStats::default(),
        }
    }

    fn fmt_err(&self, msg: impl Into<String>) -> QtError {
        let loc = match &self.path {
            Some(p) => format!("{} @ byte {}", p.display(), self.stats.bytes),
            None => format!("capture @ byte {}", self.stats.bytes),
        };
        QtError::InputFormat {
            location: loc,
            msg: msg.into(),
        }
    }

    fn io_err(&self, e: &io::Error) -> QtError {
        match &self.path {
            Some(p) => io_error(p, e),
            None => self.fmt_err(e.to_string()),
        }
    }

    /// Reads exactly `n` bytes into `self.buf[at..at + n]`; with `eof_ok`, `Ok(false)` on a clean end before the
    /// first byte (every other short read is an error).
    fn fill_at(&mut self, at: usize, n: usize, what: &str, eof_ok: bool) -> QtResult<bool> {
        if self.buf.len() < at + n {
            self.buf.resize(at + n, 0);
        }
        let got = read_full(&mut self.inner, &mut self.buf[at..at + n]).map_err(|e| self.io_err(&e))?;
        if got == 0 && eof_ok && n > 0 {
            return Ok(false);
        }
        if got < n {
            self.stats.bytes += got as u64;
            return Err(self.fmt_err(format!("stream ends inside {what}")));
        }
        self.stats.bytes += n as u64;
        Ok(true)
    }

    /// `fill_at(0, n)` where a clean end of stream is allowed.
    fn fill_first(&mut self, n: usize, what: &str) -> QtResult<bool> {
        self.fill_at(0, n, what, true)
    }

    /// `fill_at(at, n)` where the bytes must be there.
    fn fill(&mut self, at: usize, n: usize, what: &str) -> QtResult<()> {
        self.fill_at(at, n, what, false).map(|_| ())
    }

    fn start(&mut self) -> QtResult<bool> {
        if !self.fill_first(4, "the file magic")? {
            return Err(self.fmt_err("empty capture (no pcap / pcapng header)"));
        }
        let magic = [self.buf[0], self.buf[1], self.buf[2], self.buf[3]];
        if magic == [0x0a, 0x0d, 0x0d, 0x0a] {
            self.kind = Kind::PcapNg {
                big: false,
                ifaces: Vec::new(),
            };
            // The block type (the magic) is consumed; read the rest of the section header.
            return self.section_header();
        }
        let (big, ns_per_tick) = match magic {
            [0xd4, 0xc3, 0xb2, 0xa1] => (false, 1_000),
            [0xa1, 0xb2, 0xc3, 0xd4] => (true, 1_000),
            [0x4d, 0x3c, 0xb2, 0xa1] => (false, 1),
            [0xa1, 0xb2, 0x3c, 0x4d] => (true, 1),
            _ => {
                return Err(self.fmt_err(format!(
                    "not a pcap / pcapng capture (magic {:02x}{:02x}{:02x}{:02x})",
                    magic[0], magic[1], magic[2], magic[3]
                )));
            }
        };
        self.fill(4, 20, "the pcap global header")?;
        let linktype = (u32_e(&self.buf[20..24], big) & 0xffff) as u16;
        self.kind = Kind::Pcap {
            big,
            ns_per_tick,
            linktype,
        };
        self.stats.format = "pcap";
        Ok(true)
    }

    /// Reads the rest of a pcapng Section Header Block whose 4-byte type was just consumed.
    fn section_header(&mut self) -> QtResult<bool> {
        self.fill(0, 8, "a pcapng section header")?;
        let bom = [self.buf[4], self.buf[5], self.buf[6], self.buf[7]];
        let big = match bom {
            [0x4d, 0x3c, 0x2b, 0x1a] => false,
            [0x1a, 0x2b, 0x3c, 0x4d] => true,
            _ => return Err(self.fmt_err("pcapng section header with an invalid byte-order magic")),
        };
        let len = u32_e(&self.buf[0..4], big) as usize;
        if !(28..=MAX_BLOCK).contains(&len) || len % 4 != 0 {
            return Err(self.fmt_err(format!("pcapng section header length {len}")));
        }
        // Rest of the body after the byte-order magic, then the trailing length.
        self.fill(0, len - 12, "a pcapng section header")?;
        self.check_trailer_at(len - 16, len, big)?;
        self.kind = Kind::PcapNg {
            big,
            ifaces: Vec::new(),
        };
        self.stats.format = "pcapng";
        self.stats.other_blocks += 1;
        Ok(true)
    }

    /// The trailing block length (`buf[len - 12..len - 8]` after a block body read) must repeat the leading one.
    fn check_trailer(&self, len: usize, big: bool) -> QtResult<()> {
        self.check_trailer_at(len - 12, len, big)
    }

    fn check_trailer_at(&self, at: usize, len: usize, big: bool) -> QtResult<()> {
        let t = u32_e(&self.buf[at..at + 4], big) as usize;
        if t != len {
            return Err(self.fmt_err(format!("pcapng block length {len} does not match its trailer {t}")));
        }
        Ok(())
    }

    /// Next packet; `None` at a clean end of the stream.
    pub fn next_packet(&mut self) -> QtResult<Option<Packet<'_>>> {
        if matches!(self.kind, Kind::Unknown) && !self.start()? {
            return Ok(None);
        }
        loop {
            match &self.kind {
                Kind::Unknown => return Ok(None),
                &Kind::Pcap {
                    big,
                    ns_per_tick,
                    linktype,
                } => {
                    if !self.fill_first(16, "a pcap record header")? {
                        return Ok(None);
                    }
                    let sec = i64::from(u32_e(&self.buf[0..4], big));
                    let frac = i64::from(u32_e(&self.buf[4..8], big));
                    let incl = u32_e(&self.buf[8..12], big) as usize;
                    if incl > MAX_BLOCK {
                        return Err(self.fmt_err(format!("pcap record length {incl}")));
                    }
                    self.fill(0, incl, "a pcap record")?;
                    self.stats.packets += 1;
                    let ts_ns = sec
                        .checked_mul(1_000_000_000)
                        .and_then(|s| s.checked_add(frac.checked_mul(ns_per_tick)?));
                    return Ok(Some(Packet {
                        ts_ns,
                        linktype,
                        data: &self.buf[..incl],
                    }));
                }
                Kind::PcapNg { big, .. } => {
                    let big = *big;
                    if !self.fill_first(4, "a pcapng block")? {
                        return Ok(None);
                    }
                    if self.buf[..4] == [0x0a, 0x0d, 0x0d, 0x0a] {
                        self.section_header()?;
                        continue;
                    }
                    let btype = u32_e(&self.buf[0..4], big);
                    self.fill(0, 4, "a pcapng block length")?;
                    let len = u32_e(&self.buf[0..4], big) as usize;
                    if !(12..=MAX_BLOCK).contains(&len) || len % 4 != 0 {
                        return Err(self.fmt_err(format!("pcapng block type {btype:#x} length {len}")));
                    }
                    self.fill(0, len - 8, "a pcapng block")?;
                    self.check_trailer(len, big)?;
                    let body_len = len - 12;
                    match btype {
                        1 => {
                            self.stats.other_blocks += 1;
                            let iface = self.interface(body_len, big);
                            if let Kind::PcapNg { ifaces, .. } = &mut self.kind {
                                ifaces.push(iface);
                            }
                        }
                        6 | 2 => {
                            if let Some((ts_ns, linktype, start, cap)) = self.packet_block(btype, body_len, big) {
                                self.stats.packets += 1;
                                return Ok(Some(Packet {
                                    ts_ns,
                                    linktype,
                                    data: &self.buf[start..start + cap],
                                }));
                            }
                            self.stats.bad_packets += 1;
                        }
                        3 => {
                            let ifaces = match &self.kind {
                                Kind::PcapNg { ifaces, .. } => ifaces.first().copied(),
                                _ => None,
                            };
                            match (ifaces, body_len >= 4) {
                                (Some(i), true) => {
                                    let orig = u32_e(&self.buf[0..4], big) as usize;
                                    let cap = orig.min(body_len - 4);
                                    self.stats.packets += 1;
                                    return Ok(Some(Packet {
                                        ts_ns: None,
                                        linktype: i.linktype,
                                        data: &self.buf[4..4 + cap],
                                    }));
                                }
                                _ => self.stats.bad_packets += 1,
                            }
                        }
                        _ => self.stats.other_blocks += 1,
                    }
                }
            }
        }
    }

    /// Interface Description Block body in `buf[..body_len]`.
    fn interface(&self, body_len: usize, big: bool) -> Iface {
        let b = &self.buf[..body_len];
        let mut iface = Iface {
            linktype: if b.len() >= 2 { u16_e(&b[0..2], big) } else { 0 },
            tps: 1_000_000,
            offset_s: 0,
        };
        let mut i = 8;
        while i + 4 <= b.len() {
            let code = u16_e(&b[i..i + 2], big);
            let ln = usize::from(u16_e(&b[i + 2..i + 4], big));
            if code == 0 || i + 4 + ln > b.len() {
                break;
            }
            let v = &b[i + 4..i + 4 + ln];
            match (code, ln) {
                (9, 1..) => {
                    let r = v[0];
                    let tps = if r & 0x80 != 0 {
                        1u64.checked_shl(u32::from(r & 0x7f))
                    } else {
                        10u64.checked_pow(u32::from(r & 0x7f))
                    };
                    iface.tps = tps.unwrap_or(0);
                }
                (14, 8..) => {
                    let a = [v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]];
                    iface.offset_s = if big {
                        i64::from_be_bytes(a)
                    } else {
                        i64::from_le_bytes(a)
                    };
                }
                _ => {}
            }
            i += 4 + ln.div_ceil(4) * 4;
        }
        iface
    }

    /// Enhanced (6) or obsolete (2) Packet Block: (ts, linktype, data start, captured length), `None` when
    /// inconsistent.
    fn packet_block(&self, btype: u32, body_len: usize, big: bool) -> Option<(Option<i64>, u16, usize, usize)> {
        if body_len < 20 {
            return None;
        }
        let b = &self.buf[..body_len];
        let iface_id = if btype == 6 {
            u32_e(&b[0..4], big) as usize
        } else {
            usize::from(u16_e(&b[0..2], big))
        };
        let iface = match &self.kind {
            Kind::PcapNg { ifaces, .. } => ifaces.get(iface_id).copied()?,
            _ => return None,
        };
        let ticks = (u64::from(u32_e(&b[4..8], big)) << 32) | u64::from(u32_e(&b[8..12], big));
        let cap = u32_e(&b[12..16], big) as usize;
        if cap > body_len - 20 || iface.tps == 0 {
            return None;
        }
        Some((ticks_to_ns(ticks, iface.tps, iface.offset_s), iface.linktype, 20, cap))
    }
}

impl CaptureReader<Box<dyn io::BufRead + Send>> {
    /// Opens a capture file; gzip is detected from the magic bytes and inflated behind the decompression guard.
    pub fn open(path: &Path, limits: GuardLimits) -> QtResult<Self> {
        let mut r = Self::new(open_maybe_gzip(path, limits)?);
        r.path = Some(path.to_owned());
        Ok(r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn tick_conversion() {
        assert_eq!(ticks_to_ns(1_500_000, 1_000_000, 0), Some(1_500_000_000));
        assert_eq!(ticks_to_ns(3, 1 << 30, 0), Some(2));
        assert_eq!(ticks_to_ns(1, 1, 2), Some(3_000_000_000));
        assert_eq!(ticks_to_ns(u64::MAX, 1, 0), None);
    }

    #[test]
    fn rejects_garbage_without_panicking() {
        for bytes in [vec![], vec![1, 2, 3], vec![0xde, 0xad, 0xbe, 0xef, 0, 0, 0, 0]] {
            let mut r = CaptureReader::new(Cursor::new(bytes));
            assert_eq!(r.next_packet().unwrap_err().code(), "QT-INPUT-002");
        }
    }
}
