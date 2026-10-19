//! Ingest-time sequence-gap detection (SPEC 5.1.3, 5.2.7; review M2). Partition replays turn the engine's
//! record-level detector off, because one partition holds one instrument of a feed whose sequence numbers span all
//! instruments of a channel; the gaps are therefore found here, over the whole feed in file order and before the
//! symbol filter and partitioning, and handed to replay as `qt.data_quality.feed_gaps`.
//!
//! Rules (only when `VenueRules::sequence_meaningful`):
//! - state per `(session_date, channel_id)`: venues restart their numbering per session (Nasdaq each day), so a new
//!   session date starts a new baseline;
//! - `sequence > previous + 1` is a gap, `sequence < previous` a regression, equal sequences are records of one
//!   message;
//! - snapshot records (`flags & SNAPSHOT`) reset the channel (the first record after the snapshot is the new
//!   baseline) and are never checked; an unflagged `R` (clear) is checked like any record, but when the next record
//!   of its channel starts a snapshot run the `R` belongs to that snapshot and its gap or regression is withdrawn
//!   (Databento's midnight-UTC snapshots may begin with a clear);
//! - every gap gives one `FeedGap {instrument_id, ts_start, ts_end}` per instrument whose partition on that
//!   channel and session date has records on both sides of the gap (`ts_start` = `ts_event` of the channel's last
//!   record before the gap, `ts_end` = that of the first record after it); replay ends that instrument's live
//!   shadows `censored_gap` at its first event after `ts_start`.
//!
//! ITCH is not checked: the qt sequence of an ITCH day is synthetic (one per message that produces records) and
//! contiguous by construction; lost ITCH messages would only show in the MoldUDP/tracking numbers, which the file
//! format does not carry.

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

use qt_schema::json::{DataQuality, FeedGap};
use qt_types::{MboEvent, Ts, flags};

/// Multiplicative hasher for the small integer keys looked up once per record (deterministic; maps are never
/// iterated into output order).
#[derive(Default, Clone, Copy)]
pub(crate) struct KeyHasher(u64);

impl Hasher for KeyHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.write_u64(u64::from(b));
        }
    }
    #[inline]
    fn write_u64(&mut self, v: u64) {
        self.0 = (self.0.rotate_left(26) ^ v).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    }
    #[inline]
    fn write_u32(&mut self, v: u32) {
        self.write_u64(u64::from(v));
    }
    #[inline]
    fn write_u16(&mut self, v: u16) {
        self.write_u64(u64::from(v));
    }
    #[inline]
    fn write_i32(&mut self, v: i32) {
        self.write_u64(u64::from(v as u32));
    }
}

/// `HashMap` with [`KeyHasher`].
pub(crate) type FastMap<K, V> = HashMap<K, V, BuildHasherDefault<KeyHasher>>;

#[derive(Clone, Copy)]
enum Provisional {
    Gap(usize),
    Regression,
}

#[derive(Clone, Copy, Default)]
struct Chan {
    last: Option<u64>,
    ts: Ts,
    clear: Option<Provisional>,
}

/// `(instrument_id, first, last)` feed ordinals of the kept records of one partition on one channel.
type Span = (u32, u64, u64);

struct Gap {
    date: i32,
    channel: u16,
    at: u64,
    ts_start: Ts,
    ts_end: Ts,
    from: u64,
    to: u64,
    void: bool,
}

/// Streaming detector for one ingest (feed order, all records, kept or not).
pub(crate) struct SeqGaps {
    on: bool,
    n: u64,
    chans: FastMap<(i32, u16), Chan>,
    gaps: Vec<Gap>,
    regressions: u64,
    spans: FastMap<(i32, u16, u32), (u64, u64)>,
    last_span: Option<(i32, u16, u32)>,
}

impl SeqGaps {
    /// A detector; `on = false` (venue without meaningful sequences) observes nothing.
    pub(crate) fn new(on: bool) -> Self {
        Self {
            on,
            n: 0,
            chans: FastMap::default(),
            gaps: Vec::new(),
            regressions: 0,
            spans: FastMap::default(),
            last_span: None,
        }
    }

    /// True when sequences are checked (venue rule `sequence_meaningful`).
    pub(crate) fn on(&self) -> bool {
        self.on
    }

    /// Observes the next feed record of session `date`; `kept` is the instrument id it was written under (`None`
    /// when the symbol filter dropped it).
    #[inline]
    pub(crate) fn observe(&mut self, e: &MboEvent, date: i32, kept: Option<u32>) {
        if !self.on {
            return;
        }
        let ord = self.n;
        self.n += 1;
        let snapshot = e.flags & flags::SNAPSHOT != 0;
        let ch = self.chans.entry((date, e.channel_id)).or_default();
        if let Some(p) = ch.clear.take() {
            if snapshot {
                match p {
                    Provisional::Gap(i) => self.gaps[i].void = true,
                    Provisional::Regression => self.regressions -= 1,
                }
            }
        }
        if snapshot {
            ch.last = None;
        } else {
            if let Some(prev) = ch.last {
                if e.sequence > prev.saturating_add(1) {
                    if e.action == b'R' {
                        ch.clear = Some(Provisional::Gap(self.gaps.len()));
                    }
                    self.gaps.push(Gap {
                        date,
                        channel: e.channel_id,
                        at: ord,
                        ts_start: ch.ts,
                        ts_end: e.ts_event,
                        from: prev,
                        to: e.sequence,
                        void: false,
                    });
                } else if e.sequence < prev {
                    self.regressions += 1;
                    if e.action == b'R' {
                        ch.clear = Some(Provisional::Regression);
                    }
                }
            }
            ch.last = Some(e.sequence);
            ch.ts = e.ts_event;
        }
        if let Some(iid) = kept {
            let key = (date, e.channel_id, iid);
            if self.last_span == Some(key) {
                if let Some(s) = self.spans.get_mut(&key) {
                    s.1 = ord;
                }
            } else {
                self.spans.entry(key).and_modify(|s| s.1 = ord).or_insert((ord, ord));
                self.last_span = Some(key);
            }
        }
    }

    /// Adds the counters and the per-instrument feed gaps to `dq` (feed gaps sorted by instrument and time).
    pub(crate) fn finish(self, dq: &mut DataQuality) {
        if !self.on {
            return;
        }
        let live: Vec<&Gap> = self.gaps.iter().filter(|g| !g.void).collect();
        dq.sequence_gaps += live.len() as u64;
        dq.sequence_regressions += self.regressions;
        if live.is_empty() {
            return;
        }
        let mut by_chan: HashMap<(i32, u16), Vec<Span>> = HashMap::new();
        for (&(date, ch, iid), &(first, last)) in &self.spans {
            by_chan.entry((date, ch)).or_default().push((iid, first, last));
        }
        let mut out = Vec::new();
        for g in live {
            let Some(v) = by_chan.get(&(g.date, g.channel)) else {
                continue;
            };
            for &(iid, first, last) in v {
                if first < g.at && last >= g.at {
                    out.push(FeedGap {
                        instrument_id: iid,
                        ts_start: g.ts_start,
                        ts_end: g.ts_end,
                        reason: format!(
                            "sequence gap on channel {}: {} -> {} ({} missing)",
                            g.channel,
                            g.from,
                            g.to,
                            g.to - g.from - 1
                        ),
                    });
                }
            }
        }
        out.sort_by(|a, b| {
            (a.instrument_id, a.ts_start, a.ts_end, &a.reason).cmp(&(b.instrument_id, b.ts_start, b.ts_end, &b.reason))
        });
        out.dedup();
        dq.feed_gaps.extend(out);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(iid: u32, ch: u16, seq: u64, ts: Ts, action: u8, fl: u8) -> MboEvent {
        MboEvent {
            ts_event: ts,
            ts_recv: ts,
            instrument_id: iid,
            action,
            side: b'B',
            sequence: seq,
            channel_id: ch,
            flags: fl,
            ..MboEvent::default()
        }
    }

    fn run(recs: &[(MboEvent, i32, bool)]) -> DataQuality {
        let mut g = SeqGaps::new(true);
        for (e, d, k) in recs {
            g.observe(e, *d, k.then_some(e.instrument_id));
        }
        let mut dq = DataQuality::default();
        g.finish(&mut dq);
        dq
    }

    #[test]
    fn gaps_regressions_and_spanning_instruments() {
        let l = flags::LAST;
        let dq = run(&[
            (rec(1, 0, 1, 10, b'A', l), 0, true),
            (rec(2, 0, 2, 11, b'A', l), 0, true),
            (rec(3, 0, 3, 12, b'A', l), 0, false), // filtered out, still part of the feed
            (rec(9, 1, 7, 12, b'A', l), 0, true),  // another channel: its own numbering
            (rec(1, 0, 3, 13, b'F', 0), 0, true),  // same sequence: one message
            (rec(1, 0, 9, 20, b'C', l), 0, true),  // gap 3 -> 9
            (rec(2, 0, 10, 21, b'C', l), 0, false),
            (rec(9, 1, 8, 22, b'A', l), 0, true),
            (rec(1, 0, 4, 30, b'A', l), 0, true), // regression
            (rec(4, 0, 1, 40, b'A', l), 1, true), // next session date restarts the numbering
        ]);
        assert_eq!((dq.sequence_gaps, dq.sequence_regressions), (1, 1));
        // Instrument 1 spans the gap; 2 is not kept after it; 9 is on another channel.
        assert_eq!(
            dq.feed_gaps,
            vec![FeedGap {
                instrument_id: 1,
                ts_start: 13,
                ts_end: 20,
                reason: "sequence gap on channel 0: 3 -> 9 (5 missing)".into()
            }]
        );
        assert!(SeqGaps::new(false).spans.is_empty());
    }

    #[test]
    fn snapshots_reset_and_a_clear_before_a_snapshot_is_part_of_it() {
        let (l, s) = (flags::LAST, flags::SNAPSHOT);
        let dq = run(&[
            (rec(1, 0, 100, 10, b'A', l), 0, true),
            // midnight snapshot: an unflagged R with an unrelated sequence, then flagged adds
            (rec(1, 0, 0, 20, b'R', 0), 0, true),
            (rec(1, 0, 0, 20, b'A', s), 0, true),
            (rec(1, 0, 0, 20, b'A', s | l), 0, true),
            (rec(1, 0, 101, 30, b'A', l), 0, true),
            (rec(1, 0, 102, 31, b'A', l), 0, true),
            // a flagged R (testkit / CME style) with a higher sequence
            (rec(1, 0, 900, 40, b'R', s), 0, true),
            (rec(1, 0, 103, 41, b'A', l), 0, true),
        ]);
        assert_eq!(
            (dq.sequence_gaps, dq.sequence_regressions, dq.feed_gaps.len()),
            (0, 0, 0)
        );
        // An unflagged R that is not followed by a snapshot is an ordinary record.
        let dq = run(&[
            (rec(1, 0, 1, 10, b'A', l), 0, true),
            (rec(1, 0, 5, 20, b'R', l), 0, true),
            (rec(1, 0, 6, 30, b'A', l), 0, true),
        ]);
        assert_eq!((dq.sequence_gaps, dq.feed_gaps.len()), (1, 1));
    }
}
