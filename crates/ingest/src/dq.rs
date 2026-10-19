//! Ingest-time data-quality counters (`qt.data_quality`) and the verdict rules of SPEC 5.1.5. Ingest sees records,
//! not books: it counts record flags, hidden prints, `ts_event` regressions per partition and source anomalies;
//! book-level counters (crossed books, unknown references found by the book) are filled by replay.

use std::collections::HashMap;

use qt_schema::json::{DataQuality, Verdict};
use qt_types::{MboEvent, VenueRules, flags};

/// `source_specific` counters reported for information only, never part of the verdict: zero-share ITCH
/// opening / closing crosses (sent for every issue after each cross), ITCH cross prints (OC-28: a
/// `Q` print is neither a hidden print nor an anomaly), DBN instrument ids remapped from the definitions
/// (Databento re-issues remap ids routinely) and the IEX DEEP+ volumes of priority-maintaining / priority-resetting /
/// no-op Order Modify messages, A/B duplicate messages dropped and segments of other IEX-TP protocols.
pub const INFO_SOURCE_COUNTERS: [&str; 8] = [
    "itch_zero_share_cross",
    "cross_prints",
    "dbn_instrument_id_remapped",
    "iex_modify_maintain_priority",
    "iex_modify_reset_priority",
    "iex_modify_noop",
    "iex_duplicate_messages",
    "iex_other_protocol_segments",
];

/// Whether a `source_specific` counter is an INFO counter (`INFO_SOURCE_COUNTERS`).
pub fn is_info_source_counter(name: &str) -> bool {
    INFO_SOURCE_COUNTERS.contains(&name)
}

/// Streaming record counter for one dataset.
#[derive(Debug, Default)]
pub struct MboDqScan {
    dq: DataQuality,
    last_ts: HashMap<(i32, u32), i64>,
    cache: Option<((i32, u32), i64)>,
}

impl MboDqScan {
    /// Empty counters.
    pub fn new() -> Self {
        Self::default()
    }

    /// Counts one record of partition `(session_date, instrument_id)`.
    #[inline]
    pub fn record(&mut self, e: &MboEvent, session_date: i32, rules: &VenueRules) {
        let d = &mut self.dq;
        d.records += 1;
        if e.flags & flags::MAYBE_BAD_BOOK != 0 {
            d.maybe_bad_book_records += 1;
        }
        if e.flags & flags::BAD_TS_RECV != 0 {
            d.bad_ts_recv_records += 1;
        }
        let snapshot = e.flags & flags::SNAPSHOT != 0;
        if snapshot {
            d.snapshot_records += 1;
        }
        if e.action == b'T' && e.side == b'N' && rules.supports_hidden_orders {
            d.hidden_prints += 1;
        }
        if snapshot {
            // Snapshot records carry the snapshot's own timestamps: never an ordering anomaly, and they do not
            // move the partition's clock.
            return;
        }
        let key = (session_date, e.instrument_id);
        let last = match self.cache {
            Some((k, t)) if k == key => t,
            other => {
                if let Some((k, t)) = other {
                    self.last_ts.insert(k, t);
                }
                self.last_ts.get(&key).copied().unwrap_or(i64::MIN)
            }
        };
        if e.ts_event < last {
            d.out_of_order_ts_event += 1;
        }
        self.cache = Some((key, e.ts_event));
    }

    /// Reclassifies the last counted side-`N` print as an ITCH cross print (OC-28): `hidden_prints` - 1,
    /// `source_specific.cross_prints` + 1. For mappers that know a `T` record came from a `Q` message.
    pub fn cross_print(&mut self) {
        self.dq.hidden_prints = self.dq.hidden_prints.saturating_sub(1);
        *self.dq.source_specific.entry("cross_prints".to_owned()).or_insert(0) += 1;
    }

    /// Adds a named source-specific counter (zero values are not stored).
    pub fn add_source(&mut self, name: &str, v: u64) {
        if v > 0 {
            *self.dq.source_specific.entry(name.to_owned()).or_insert(0) += v;
        }
    }

    /// Mutable access to the counters.
    pub fn counters_mut(&mut self) -> &mut DataQuality {
        &mut self.dq
    }

    /// The counters with the verdict applied.
    pub fn finish(mut self) -> DataQuality {
        apply_verdict(&mut self.dq, None);
        self.dq
    }
}

/// Threshold of the crossed-book and unknown-reference ratios (SPEC 5.1.5).
pub const UNUSABLE_RATIO: f64 = 1e-3;

/// Sets `verdict` and `reasons` from the counters (SPEC 5.1.5). The one verdict function of the workspace: ingest
/// (`None`: no book, `records` is the denominator), replay (`Some(events)`, the replayed event count; 0 = unknown)
/// and qt_crypto call it; `qt_replay::dq` re-exports it.
///
/// `unusable` when `maybe_bad_book_records > 0`, `crossed_books_after_last / events > 1e-3` or
/// `unknown_order_refs / records > 1e-3` (a zero denominator counts as 1); reasons list those conditions.
/// Otherwise `warnings` when any anomaly counter is non-zero, any feed gap exists, or any `source_specific` counter
/// that is not an INFO counter (`INFO_SOURCE_COUNTERS`: zero-share ITCH crosses, cross prints, DBN instrument id
/// remaps) is non-zero; reasons list those counters. Else `clean`. Never part of the verdict: the volumes `records`
/// and `snapshot_records`, and the INFO counters `hidden_prints` and `off_price_executions` (normal on Nasdaq,
/// ruling 36b). Snapshot records are never ordering anomalies (`MboDqScan::record` and the replay engine skip
/// them for `out_of_order_ts_event`).
pub fn apply_verdict(dq: &mut DataQuality, events: impl Into<Option<u64>>) {
    let events = events.into().filter(|&e| e > 0).unwrap_or(dq.records);
    let mut reasons = Vec::new();
    if dq.maybe_bad_book_records > 0 {
        reasons.push(format!("maybe_bad_book_records = {}", dq.maybe_bad_book_records));
    }
    if dq.crossed_books_after_last as f64 / events.max(1) as f64 > UNUSABLE_RATIO {
        reasons.push(format!(
            "crossed_books_after_last / events = {} / {events} > {UNUSABLE_RATIO}",
            dq.crossed_books_after_last
        ));
    }
    if dq.unknown_order_refs as f64 / dq.records.max(1) as f64 > UNUSABLE_RATIO {
        reasons.push(format!(
            "unknown_order_refs / records = {} / {} > {UNUSABLE_RATIO}",
            dq.unknown_order_refs, dq.records
        ));
    }
    if !reasons.is_empty() {
        dq.verdict = Verdict::Unusable;
        dq.reasons = reasons;
        return;
    }
    let anomalies = [
        ("bad_ts_recv_records", dq.bad_ts_recv_records),
        ("pre_snapshot_rows_skipped", dq.pre_snapshot_rows_skipped),
        ("unknown_order_refs", dq.unknown_order_refs),
        ("duplicate_adds", dq.duplicate_adds),
        ("unexpected_modifies", dq.unexpected_modifies),
        ("sequence_gaps", dq.sequence_gaps),
        ("sequence_regressions", dq.sequence_regressions),
        ("out_of_order_ts_event", dq.out_of_order_ts_event),
        ("crossed_books_after_last", dq.crossed_books_after_last),
        ("feed_gaps", dq.feed_gaps.len() as u64),
    ];
    for (name, v) in anomalies {
        if v > 0 {
            reasons.push(format!("{name} = {v}"));
        }
    }
    for (k, v) in &dq.source_specific {
        if *v > 0 && !is_info_source_counter(k) {
            reasons.push(format!("{k} = {v}"));
        }
    }
    dq.verdict = if reasons.is_empty() {
        Verdict::Clean
    } else {
        Verdict::Warnings
    };
    dq.reasons = reasons;
}

#[cfg(test)]
mod tests {
    use super::*;
    use qt_types::{Venue, rules};

    #[test]
    fn scan_counts_flags_and_verdicts() {
        let r = rules(Venue::Nasdaq);
        let mut s = MboDqScan::new();
        let e = |ts, action, side, fl| MboEvent {
            ts_event: ts,
            action,
            side,
            flags: fl,
            ..MboEvent::default()
        };
        s.record(&e(10, b'A', b'B', flags::LAST), 0, &r);
        s.record(&e(9, b'T', b'N', flags::LAST | flags::BAD_TS_RECV), 0, &r);
        let mut info = MboDqScan::new();
        info.record(&e(10, b'T', b'N', flags::LAST), 0, &r);
        let info = info.finish();
        // Hidden prints (and off-price executions) are INFO counters: reported, never a warning (ruling 36b).
        assert_eq!((info.hidden_prints, info.verdict), (1, Verdict::Clean));
        assert!(info.reasons.is_empty());
        let mut off = DataQuality {
            records: 10,
            off_price_executions: 3,
            hidden_prints: 2,
            ..Default::default()
        };
        apply_verdict(&mut off, None);
        assert_eq!(off.verdict, Verdict::Clean);
        // INFO source counters: zero-share crosses and cross prints never raise the verdict.
        let mut crosses = DataQuality {
            records: 10,
            ..Default::default()
        };
        crosses.source_specific.insert("itch_zero_share_cross".into(), 12);
        crosses.source_specific.insert("cross_prints".into(), 2);
        apply_verdict(&mut crosses, None);
        assert_eq!((crosses.verdict, crosses.reasons.len()), (Verdict::Clean, 0));
        crosses.source_specific.insert("itch_unknown_message_type".into(), 1);
        apply_verdict(&mut crosses, None);
        assert_eq!(crosses.verdict, Verdict::Warnings);
        // Snapshot records (older snapshot timestamps) are not ordering anomalies.
        let mut snap = MboDqScan::new();
        snap.record(&e(100, b'A', b'B', flags::LAST), 0, &r);
        snap.record(&e(50, b'A', b'B', flags::SNAPSHOT | flags::LAST), 0, &r);
        snap.record(&e(110, b'A', b'B', flags::LAST), 0, &r);
        assert_eq!(snap.finish().out_of_order_ts_event, 0);
        let mut cs = MboDqScan::new();
        cs.record(&e(10, b'T', b'N', flags::LAST), 0, &r);
        cs.cross_print();
        let d = cs.finish();
        assert_eq!(
            (d.hidden_prints, d.source_specific.get("cross_prints").copied()),
            (0, Some(1))
        );
        s.record(&e(8, b'A', b'B', flags::SNAPSHOT), 1, &r);
        let dq = s.finish();
        assert_eq!(
            (
                dq.records,
                dq.hidden_prints,
                dq.out_of_order_ts_event,
                dq.bad_ts_recv_records,
                dq.snapshot_records
            ),
            (3, 1, 1, 1, 1)
        );
        assert_eq!(dq.verdict, Verdict::Warnings);
        let mut clean = DataQuality {
            records: 10,
            snapshot_records: 10,
            ..Default::default()
        };
        apply_verdict(&mut clean, None);
        assert_eq!(clean.verdict, Verdict::Clean);
        let mut bad = DataQuality {
            records: 100,
            maybe_bad_book_records: 1,
            ..Default::default()
        };
        apply_verdict(&mut bad, None);
        assert_eq!(bad.verdict, Verdict::Unusable);
        let mut refs = DataQuality {
            records: 1000,
            unknown_order_refs: 2,
            ..Default::default()
        };
        apply_verdict(&mut refs, None);
        assert_eq!(refs.verdict, Verdict::Unusable);
        refs.unknown_order_refs = 1;
        apply_verdict(&mut refs, None);
        assert_eq!(refs.verdict, Verdict::Warnings);
    }

    /// The single verdict function: ingest (`None`) and replay (`Some(events)`) calls agree; 0 events means
    /// unknown; INFO counters (hidden prints, off-price executions, cross prints, zero-share crosses, DBN id remaps)
    /// never raise it; unusable reasons list only the unusable conditions.
    #[test]
    fn one_verdict_for_ingest_and_replay() {
        let base = || DataQuality {
            records: 1000,
            ..Default::default()
        };
        let mut info = base();
        info.hidden_prints = 40;
        info.off_price_executions = 7;
        info.snapshot_records = 1000;
        for k in INFO_SOURCE_COUNTERS {
            info.source_specific.insert(k.into(), 3);
        }
        for ev in [None, Some(0), Some(1000), Some(5)] {
            let mut d = info.clone();
            apply_verdict(&mut d, ev);
            assert_eq!((d.verdict, d.reasons.len()), (Verdict::Clean, 0), "{ev:?}");
        }
        // Events as the crossed-book denominator: 2 crossed books are above 1e-3 of 1000 events, not of 10_000.
        let mut crossed = base();
        crossed.crossed_books_after_last = 2;
        let (mut a, mut b, mut c) = (crossed.clone(), crossed.clone(), crossed.clone());
        apply_verdict(&mut a, None);
        apply_verdict(&mut b, 1000);
        apply_verdict(&mut c, 10_000);
        assert_eq!((a.verdict, b.verdict), (Verdict::Unusable, Verdict::Unusable));
        assert_eq!(
            a.reasons,
            vec!["crossed_books_after_last / events = 2 / 1000 > 0.001".to_owned()]
        );
        assert_eq!(
            (c.verdict, c.reasons.clone()),
            (Verdict::Warnings, vec!["crossed_books_after_last = 2".to_owned()])
        );
        // Unusable reasons carry only the unusable conditions, not the warning counters.
        let mut bad = base();
        bad.maybe_bad_book_records = 1;
        bad.duplicate_adds = 4;
        apply_verdict(&mut bad, 1000);
        assert_eq!(bad.reasons, vec!["maybe_bad_book_records = 1".to_owned()]);
        // A non-INFO source counter and a feed gap warn.
        let mut src = base();
        src.source_specific.insert("removal_after_unpaired_fill".into(), 1);
        src.feed_gaps.push(qt_schema::json::FeedGap {
            instrument_id: 1,
            ts_start: 1,
            ts_end: 2,
            reason: "t".into(),
        });
        apply_verdict(&mut src, None);
        assert_eq!(src.verdict, Verdict::Warnings);
        assert_eq!(
            src.reasons,
            vec!["feed_gaps = 1".to_owned(), "removal_after_unpaired_fill = 1".to_owned()]
        );
        // Re-applying is idempotent (replay applies the verdict again after merging ingest counters).
        let before = src.reasons.clone();
        apply_verdict(&mut src, None);
        assert_eq!(src.reasons, before);
    }
}
