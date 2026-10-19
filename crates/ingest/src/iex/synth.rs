//! Synthetic DEEP+ captures for tests and fixtures: messages -> IEX-TP segments -> Ethernet / IPv4 / UDP frames ->
//! pcap or pcapng bytes (optionally gzip). The layouts are the ones the reader parses (`message::encode`,
//! `transport::encode_header`).

use std::io::Write;
use std::path::Path;

use qt_types::{QtError, QtResult};

use super::message::{Message, encode};
use super::transport::{DEEP_PLUS_PROTOCOL_ID, SegmentHeader, encode_header};

/// One segment to build.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Segment {
    /// Channel ID.
    pub channel: u32,
    /// Session ID.
    pub session: u32,
    /// First Message Sequence Number.
    pub first_seq: i64,
    /// Send Time.
    pub send_time: i64,
    /// Capture time of the packet (ns).
    pub capture_ns: i64,
    /// Message Data of each block (use `message_bytes` for known messages).
    pub messages: Vec<Vec<u8>>,
}

/// Message Data of `m`.
pub fn message_bytes(m: &Message) -> Vec<u8> {
    let mut v = Vec::with_capacity(46);
    encode(m, &mut v);
    v
}

/// IEX-TP datagram of a segment (header + blocks).
pub fn segment_bytes(s: &Segment) -> Vec<u8> {
    let mut payload = Vec::new();
    for m in &s.messages {
        payload.extend_from_slice(&(m.len() as u16).to_le_bytes());
        payload.extend_from_slice(m);
    }
    let mut out = Vec::with_capacity(40 + payload.len());
    encode_header(
        &SegmentHeader {
            version: 1,
            protocol_id: DEEP_PLUS_PROTOCOL_ID,
            channel_id: s.channel,
            session_id: s.session,
            payload_len: payload.len() as u16,
            message_count: s.messages.len() as u16,
            stream_offset: 0,
            first_seq: s.first_seq,
            send_time: s.send_time,
        },
        &mut out,
    );
    out.extend_from_slice(&payload);
    out
}

/// Ethernet II / IPv4 / UDP frame carrying `udp_payload` to the DEEP+ group 233.215.21.8:10378.
pub fn frame(udp_payload: &[u8]) -> Vec<u8> {
    let udp_len = 8 + udp_payload.len();
    let mut f = Vec::with_capacity(14 + 20 + udp_len);
    f.extend_from_slice(&[0x01, 0x00, 0x5e, 0x57, 0x15, 0x08, 0, 0, 0, 0, 0, 0, 0x08, 0x00]);
    let total = (20 + udp_len) as u16;
    f.extend_from_slice(&[0x45, 0]);
    f.extend_from_slice(&total.to_be_bytes());
    f.extend_from_slice(&[0, 0, 0x40, 0, 64, 17, 0, 0, 23, 226, 155, 133, 233, 215, 21, 8]);
    f.extend_from_slice(&10378u16.to_be_bytes());
    f.extend_from_slice(&10378u16.to_be_bytes());
    f.extend_from_slice(&(udp_len as u16).to_be_bytes());
    f.extend_from_slice(&[0, 0]);
    f.extend_from_slice(udp_payload);
    f
}

/// Classic pcap (little endian, nanosecond stamps, Ethernet) of `(capture_ns, frame)` packets.
pub fn pcap(packets: &[(i64, Vec<u8>)]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&0xa1b2_3c4du32.to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&4u16.to_le_bytes());
    out.extend_from_slice(&[0; 8]);
    out.extend_from_slice(&65_535u32.to_le_bytes());
    out.extend_from_slice(&1u32.to_le_bytes());
    for (ts, f) in packets {
        let ts = (*ts).max(0) as u64;
        out.extend_from_slice(&((ts / 1_000_000_000) as u32).to_le_bytes());
        out.extend_from_slice(&((ts % 1_000_000_000) as u32).to_le_bytes());
        out.extend_from_slice(&(f.len() as u32).to_le_bytes());
        out.extend_from_slice(&(f.len() as u32).to_le_bytes());
        out.extend_from_slice(f);
    }
    out
}

fn block(out: &mut Vec<u8>, btype: u32, body: &[u8]) {
    let pad = (4 - body.len() % 4) % 4;
    let len = (12 + body.len() + pad) as u32;
    out.extend_from_slice(&btype.to_le_bytes());
    out.extend_from_slice(&len.to_le_bytes());
    out.extend_from_slice(body);
    out.extend_from_slice(&vec![0; pad]);
    out.extend_from_slice(&len.to_le_bytes());
}

/// pcapng (little endian; one Ethernet interface with microsecond stamps, as IEX HIST) of `(capture_ns, frame)`.
pub fn pcapng(packets: &[(i64, Vec<u8>)]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut shb = Vec::new();
    shb.extend_from_slice(&0x1a2b_3c4du32.to_le_bytes());
    shb.extend_from_slice(&1u16.to_le_bytes());
    shb.extend_from_slice(&0u16.to_le_bytes());
    shb.extend_from_slice(&(-1i64).to_le_bytes());
    block(&mut out, 0x0a0d_0d0a, &shb);
    let mut idb = Vec::new();
    idb.extend_from_slice(&1u16.to_le_bytes());
    idb.extend_from_slice(&0u16.to_le_bytes());
    idb.extend_from_slice(&65_535u32.to_le_bytes());
    idb.extend_from_slice(&[9, 0, 1, 0, 6, 0, 0, 0, 0, 0, 0, 0]); // if_tsresol = 6, opt_endofopt
    block(&mut out, 1, &idb);
    block(&mut out, 5, &[0; 12]); // an Interface Statistics block, skipped
    for (ts, f) in packets {
        let us = ((*ts).max(0) / 1_000) as u64;
        let mut epb = Vec::with_capacity(20 + f.len());
        epb.extend_from_slice(&0u32.to_le_bytes());
        epb.extend_from_slice(&((us >> 32) as u32).to_le_bytes());
        epb.extend_from_slice(&(us as u32).to_le_bytes());
        epb.extend_from_slice(&(f.len() as u32).to_le_bytes());
        epb.extend_from_slice(&(f.len() as u32).to_le_bytes());
        epb.extend_from_slice(f);
        block(&mut out, 6, &epb);
    }
    out
}

/// Frames of `segments` as `(capture_ns, frame)`.
pub fn frames(segments: &[Segment]) -> Vec<(i64, Vec<u8>)> {
    segments
        .iter()
        .map(|s| (s.capture_ns, frame(&segment_bytes(s))))
        .collect()
}

/// Writes `bytes` to `path`, gzip-compressed when `gzip`.
pub fn write_file(bytes: &[u8], path: &Path, gzip: bool) -> QtResult<()> {
    let io = |e: std::io::Error| QtError::Engine(format!("{}: {e}", path.display()));
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent).map_err(io)?;
    }
    if gzip {
        let f = std::fs::File::create(path).map_err(io)?;
        let mut e = flate2::write::GzEncoder::new(std::io::BufWriter::new(f), flate2::Compression::fast());
        e.write_all(bytes).map_err(io)?;
        e.finish().map_err(io)?.flush().map_err(io)?;
    } else {
        std::fs::write(path, bytes).map_err(io)?;
    }
    Ok(())
}
