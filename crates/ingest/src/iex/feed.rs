//! The DEEP+ message stream of a capture: packets -> UDP -> IEX-TP segments of Message Protocol ID 0x8005 ->
//! sequenced messages, with duplicate suppression per `(channel, session)`.
//!
//! Sequence rules (IEX-TP v1.26, p.6): message `k` of a segment has number `First Message Sequence Number + k`.
//! Per `(Channel ID, Session ID)` the reader keeps the next expected number: a message below it was already
//! delivered (an A/B duplicate, a gap fill or an overlapping retransmission) and is dropped and counted; a segment
//! starting above it is a gap of that many messages (counted here; `qt_ingest` turns it into `sequence_gaps` /
//! `feed_gaps` through its ingest-time detector). Heartbeats (Message Count 0) only advance nothing. A new Session ID
//! on a channel starts a new numbering (`FeedEvent::NewSession`).

use std::collections::{BTreeMap, HashMap};
use std::io::Read;

use qt_types::QtResult;

use super::capture::CaptureReader;
use super::transport::{Blocks, DEEP_PLUS_PROTOCOL_ID, HEADER_LEN, segment_header, udp_payload};

/// Counters of the transport walk (`source_specific` `iex_tp_<name>` / `iex_<name>`).
#[derive(Debug, Default, Clone, PartialEq, Eq, serde::Serialize)]
pub struct FeedStats {
    /// Frames skipped by reason (`linktype`, `not_ipv4`, `not_udp`, `ip_fragment`, `truncated_frame`).
    pub skipped_frames: BTreeMap<String, u64>,
    /// UDP payloads shorter than an IEX-TP header or with a version other than 1.
    pub bad_segments: u64,
    /// Segments of another Message Protocol ID (e.g. TOPS 0x8003, DEEP 0x8004).
    pub other_protocol_segments: u64,
    /// DEEP+ segments.
    pub segments: u64,
    /// Heartbeats (Message Count 0).
    pub heartbeats: u64,
    /// Segments whose blocks ended before Message Count (or inside a block).
    pub truncated_segments: u64,
    /// Segments with bytes after their last announced block (or a Payload Length beyond the datagram).
    pub segment_length_mismatch: u64,
    /// Messages delivered.
    pub messages: u64,
    /// Messages dropped as already delivered.
    pub duplicate_messages: u64,
    /// Forward jumps of the sequence number.
    pub sequence_gaps: u64,
    /// Messages missing in those jumps.
    pub sequence_gap_messages: u64,
    /// Segments with a negative First Message Sequence Number (skipped).
    pub bad_sequence: u64,
    /// `(channel, session)` pairs seen.
    pub sessions: u64,
}

/// One delivered message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FeedMessage<'a> {
    /// Sequence number.
    pub seq: u64,
    /// Channel ID.
    pub channel: u32,
    /// Session ID.
    pub session: u32,
    /// Segment Send Time.
    pub send_time: i64,
    /// Capture time of the packet.
    pub capture_ts: Option<i64>,
    /// Message Data (without the length prefix).
    pub data: &'a [u8],
}

/// What the walk hands to the consumer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FeedEvent<'a> {
    /// A new `(channel, session)` begins (its numbering starts at `first_seq`).
    NewSession {
        /// Channel ID.
        channel: u32,
        /// Session ID.
        session: u32,
        /// First sequence number seen.
        first_seq: u64,
        /// Send Time of the segment that opened it.
        send_time: i64,
    },
    /// A sequenced message.
    Message(FeedMessage<'a>),
}

/// DEEP+ feed reader over a capture.
pub struct FeedReader<R: Read> {
    cap: CaptureReader<R>,
    next: HashMap<(u32, u32), u64>,
    /// Transport counters.
    pub stats: FeedStats,
}

impl<R: Read> FeedReader<R> {
    /// Reads the DEEP+ stream of `cap`.
    pub fn new(cap: CaptureReader<R>) -> Self {
        Self {
            cap,
            next: HashMap::new(),
            stats: FeedStats::default(),
        }
    }

    /// Capture-level counters.
    pub fn capture_stats(&self) -> &super::capture::CaptureStats {
        &self.cap.stats
    }

    /// Walks the whole capture, calling `f` for every event in feed order.
    pub fn for_each<F>(&mut self, mut f: F) -> QtResult<()>
    where
        F: FnMut(FeedEvent<'_>) -> QtResult<()>,
    {
        let Self { cap, next, stats } = self;
        while let Some(pkt) = cap.next_packet()? {
            let p = match udp_payload(pkt.linktype, pkt.data) {
                Ok(p) => p,
                Err(s) => {
                    *stats.skipped_frames.entry(s.name().to_owned()).or_insert(0) += 1;
                    continue;
                }
            };
            let Some(h) = segment_header(p) else {
                stats.bad_segments += 1;
                continue;
            };
            if h.version != 1 {
                stats.bad_segments += 1;
                continue;
            }
            if h.protocol_id != DEEP_PLUS_PROTOCOL_ID {
                stats.other_protocol_segments += 1;
                continue;
            }
            stats.segments += 1;
            let Ok(first) = u64::try_from(h.first_seq) else {
                stats.bad_sequence += 1;
                continue;
            };
            let key = (h.channel_id, h.session_id);
            let expected = match next.get(&key) {
                Some(&n) => n,
                None => {
                    stats.sessions += 1;
                    next.insert(key, first);
                    f(FeedEvent::NewSession {
                        channel: h.channel_id,
                        session: h.session_id,
                        first_seq: first,
                        send_time: h.send_time,
                    })?;
                    first
                }
            };
            if h.message_count == 0 {
                stats.heartbeats += 1;
                continue;
            }
            let body = &p[HEADER_LEN..];
            let plen = usize::from(h.payload_len);
            if plen > body.len() {
                stats.segment_length_mismatch += 1;
            }
            let payload = &body[..plen.min(body.len())];
            if first > expected {
                stats.sequence_gaps += 1;
                stats.sequence_gap_messages += first - expected;
            }
            let mut blocks = Blocks::new(payload, h.message_count);
            let mut seq = first;
            let mut last = expected;
            for data in blocks.by_ref() {
                if seq < expected {
                    stats.duplicate_messages += 1;
                } else {
                    stats.messages += 1;
                    f(FeedEvent::Message(FeedMessage {
                        seq,
                        channel: h.channel_id,
                        session: h.session_id,
                        send_time: h.send_time,
                        capture_ts: pkt.ts_ns,
                        data,
                    }))?;
                    last = seq.saturating_add(1);
                }
                seq = seq.saturating_add(1);
            }
            if blocks.walk.truncated {
                stats.truncated_segments += 1;
            } else if blocks.walk.trailing > 0 {
                stats.segment_length_mismatch += 1;
            }
            if last > expected {
                next.insert(key, last);
            }
        }
        Ok(())
    }

    /// `(name, value)` counters for `source_specific` (zero values included; the caller drops them).
    pub fn named(&self) -> Vec<(String, u64)> {
        let s = &self.stats;
        let mut v: Vec<(String, u64)> = s
            .skipped_frames
            .iter()
            .map(|(k, n)| (format!("iex_skipped_frames_{k}"), *n))
            .collect();
        v.extend([
            ("iex_tp_bad_segments".to_owned(), s.bad_segments),
            ("iex_tp_truncated_segments".to_owned(), s.truncated_segments),
            ("iex_tp_segment_length_mismatch".to_owned(), s.segment_length_mismatch),
            ("iex_tp_bad_sequence".to_owned(), s.bad_sequence),
            ("iex_capture_bad_packets".to_owned(), self.cap.stats.bad_packets),
        ]);
        v
    }
}
