//! Network framing (Ethernet II with 802.1Q / 802.1ad tags, raw IPv4, Linux SLL -> IPv4 -> UDP) and the IEX-TP
//! outbound segment (IEX Transport Specification v1.26, "Outbound Segment Format", pp.5-6): a 40-byte header
//!
//! | field | offset | length |
//! |---|---|---|
//! | Version (1) | 0 | 1 |
//! | reserved | 1 | 1 |
//! | Message Protocol ID (DEEP+ = 0x8005) | 2 | 2 |
//! | Channel ID | 4 | 4 |
//! | Session ID | 8 | 4 |
//! | Payload Length | 12 | 2 |
//! | Message Count | 14 | 2 |
//! | Stream Offset | 16 | 8 |
//! | First Message Sequence Number | 24 | 8 |
//! | Send Time | 32 | 8 |
//!
//! followed by `Message Count` message blocks `[u16 LE Message Length][Message Data]`. "If there is more than one
//! message in a segment, all subsequent messages are implicitly numbered sequentially." A heartbeat has Payload
//! Length and Message Count 0 and carries the next sequence number. Everything is little endian.

/// IEX-TP header length.
pub const HEADER_LEN: usize = 40;
/// Message Protocol ID of DEEP+ (DEEP+ Specification v1.06, p.4).
pub const DEEP_PLUS_PROTOCOL_ID: u16 = 0x8005;

/// Why a frame carries no UDP payload this reader can use.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Skip {
    /// Link type other than Ethernet, raw IPv4 or Linux SLL.
    LinkType,
    /// Not IPv4 (ARP, IPv6, ...).
    NotIpv4,
    /// IPv4 but not UDP.
    NotUdp,
    /// An IPv4 fragment (IEX-TP segments fit one datagram; fragments are not reassembled).
    Fragment,
    /// Frame shorter than its headers.
    Truncated,
}

impl Skip {
    /// Counter name suffix.
    pub fn name(self) -> &'static str {
        match self {
            Self::LinkType => "linktype",
            Self::NotIpv4 => "not_ipv4",
            Self::NotUdp => "not_udp",
            Self::Fragment => "ip_fragment",
            Self::Truncated => "truncated_frame",
        }
    }
}

#[inline]
fn be16(b: &[u8], o: usize) -> u16 {
    u16::from_be_bytes([b[o], b[o + 1]])
}

/// UDP payload of a frame.
pub fn udp_payload(linktype: u16, frame: &[u8]) -> Result<&[u8], Skip> {
    let ip = match linktype {
        1 => {
            if frame.len() < 14 {
                return Err(Skip::Truncated);
            }
            let (mut et, mut i) = (be16(frame, 12), 14);
            while matches!(et, 0x8100 | 0x88a8) {
                if frame.len() < i + 4 {
                    return Err(Skip::Truncated);
                }
                et = be16(frame, i + 2);
                i += 4;
            }
            if et != 0x0800 {
                return Err(Skip::NotIpv4);
            }
            i
        }
        101 | 228 => 0,
        113 => {
            if frame.len() < 16 {
                return Err(Skip::Truncated);
            }
            if be16(frame, 14) != 0x0800 {
                return Err(Skip::NotIpv4);
            }
            16
        }
        _ => return Err(Skip::LinkType),
    };
    let b = &frame[ip..];
    if b.len() < 20 {
        return Err(Skip::Truncated);
    }
    if b[0] >> 4 != 4 {
        return Err(Skip::NotIpv4);
    }
    let ihl = usize::from(b[0] & 0x0f) * 4;
    if ihl < 20 || b.len() < ihl + 8 {
        return Err(Skip::Truncated);
    }
    if b[9] != 17 {
        return Err(Skip::NotUdp);
    }
    if be16(b, 6) & 0x3fff != 0 {
        return Err(Skip::Fragment);
    }
    let total = usize::from(be16(b, 2));
    let end = if total >= ihl + 8 { total.min(b.len()) } else { b.len() };
    let udp = &b[ihl..end];
    let ulen = usize::from(be16(udp, 4));
    let pend = if ulen >= 8 { ulen.min(udp.len()) } else { udp.len() };
    Ok(&udp[8..pend])
}

/// IEX-TP segment header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SegmentHeader {
    /// Version (1).
    pub version: u8,
    /// Message Protocol ID.
    pub protocol_id: u16,
    /// Channel ID.
    pub channel_id: u32,
    /// Session ID.
    pub session_id: u32,
    /// Payload Length (bytes after the header).
    pub payload_len: u16,
    /// Message Count.
    pub message_count: u16,
    /// Stream Offset.
    pub stream_offset: i64,
    /// First Message Sequence Number.
    pub first_seq: i64,
    /// Send Time, ns since the epoch.
    pub send_time: i64,
}

#[inline]
fn le_i64(b: &[u8], o: usize) -> i64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[o..o + 8]);
    i64::from_le_bytes(a)
}

/// Header of a UDP payload (`None` when shorter than 40 bytes).
pub fn segment_header(p: &[u8]) -> Option<SegmentHeader> {
    if p.len() < HEADER_LEN {
        return None;
    }
    Some(SegmentHeader {
        version: p[0],
        protocol_id: u16::from_le_bytes([p[2], p[3]]),
        channel_id: u32::from_le_bytes([p[4], p[5], p[6], p[7]]),
        session_id: u32::from_le_bytes([p[8], p[9], p[10], p[11]]),
        payload_len: u16::from_le_bytes([p[12], p[13]]),
        message_count: u16::from_le_bytes([p[14], p[15]]),
        stream_offset: le_i64(p, 16),
        first_seq: le_i64(p, 24),
        send_time: le_i64(p, 32),
    })
}

/// Writes a header (synthetic captures and the subset writer's layout).
pub fn encode_header(h: &SegmentHeader, out: &mut Vec<u8>) {
    out.push(h.version);
    out.push(0);
    out.extend_from_slice(&h.protocol_id.to_le_bytes());
    out.extend_from_slice(&h.channel_id.to_le_bytes());
    out.extend_from_slice(&h.session_id.to_le_bytes());
    out.extend_from_slice(&h.payload_len.to_le_bytes());
    out.extend_from_slice(&h.message_count.to_le_bytes());
    out.extend_from_slice(&h.stream_offset.to_le_bytes());
    out.extend_from_slice(&h.first_seq.to_le_bytes());
    out.extend_from_slice(&h.send_time.to_le_bytes());
}

/// How the message blocks of a segment ended.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct BlockWalk {
    /// Message blocks returned.
    pub blocks: u16,
    /// The payload ended before `message_count` blocks (or inside a block).
    pub truncated: bool,
    /// Bytes left after `message_count` blocks.
    pub trailing: usize,
}

/// Message blocks of a segment payload: up to `count` `[u16 LE length][data]` blocks.
pub struct Blocks<'a> {
    rest: &'a [u8],
    left: u16,
    /// Walk result so far.
    pub walk: BlockWalk,
}

impl<'a> Blocks<'a> {
    /// Blocks of `payload` (already cut to the header's Payload Length) announced as `count`.
    pub fn new(payload: &'a [u8], count: u16) -> Self {
        Self {
            rest: payload,
            left: count,
            walk: BlockWalk::default(),
        }
    }
}

impl<'a> Iterator for Blocks<'a> {
    type Item = &'a [u8];

    fn next(&mut self) -> Option<&'a [u8]> {
        if self.left == 0 {
            self.walk.trailing = self.rest.len();
            return None;
        }
        if self.rest.len() < 2 {
            self.walk.truncated = true;
            self.left = 0;
            self.rest = &[];
            return None;
        }
        let n = usize::from(u16::from_le_bytes([self.rest[0], self.rest[1]]));
        if self.rest.len() < 2 + n {
            self.walk.truncated = true;
            self.left = 0;
            self.rest = &[];
            return None;
        }
        let (m, rest) = self.rest[2..].split_at(n);
        self.rest = rest;
        self.left -= 1;
        self.walk.blocks += 1;
        Some(m)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_roundtrip_and_blocks() {
        let h = SegmentHeader {
            version: 1,
            protocol_id: DEEP_PLUS_PROTOCOL_ID,
            channel_id: 1,
            session_id: 0x50ff_0000,
            payload_len: 7,
            message_count: 2,
            stream_offset: 99,
            first_seq: 1,
            send_time: 1_791_543_934_139_284_000,
        };
        let mut p = Vec::new();
        encode_header(&h, &mut p);
        assert_eq!(p.len(), HEADER_LEN);
        assert_eq!(segment_header(&p), Some(h));
        p.extend_from_slice(&[2, 0, b'x', b'y', 1, 0, b'z']);
        let mut b = Blocks::new(&p[HEADER_LEN..], 2);
        assert_eq!(b.next(), Some(&b"xy"[..]));
        assert_eq!(b.next(), Some(&b"z"[..]));
        assert_eq!(b.next(), None);
        assert_eq!(
            b.walk,
            BlockWalk {
                blocks: 2,
                truncated: false,
                trailing: 0
            }
        );
        let mut b = Blocks::new(&p[HEADER_LEN..p.len() - 1], 2);
        assert_eq!(b.by_ref().count(), 1);
        assert!(b.walk.truncated);
        assert_eq!(segment_header(&p[..39]), None);
    }

    /// The first heartbeat of the 2026-10-09 DPLS capture, byte for byte (Ethernet / IPv4 / UDP / IEX-TP).
    #[test]
    fn real_heartbeat_frame() {
        let hex = "01005e571508a088c28f28d7080045000044000000004011c86217e29b85e9d71508288a288a00302aff01000580\
                   010000000000ff500000000000000000000000000100000000000000c4bd79b4aed7dc18";
        let f: Vec<u8> = (0..hex.len() / 2)
            .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap())
            .collect();
        let p = udp_payload(1, &f).unwrap();
        let h = segment_header(p).unwrap();
        assert_eq!((h.version, h.protocol_id, h.channel_id), (1, DEEP_PLUS_PROTOCOL_ID, 1));
        assert_eq!((h.payload_len, h.message_count, h.first_seq), (0, 0, 1));
        assert_eq!(h.session_id, 0x50ff_0000);
        assert_eq!(udp_payload(1, &f[..20]), Err(Skip::Truncated));
        assert_eq!(udp_payload(7, &f), Err(Skip::LinkType));
    }
}
