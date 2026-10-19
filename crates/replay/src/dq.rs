//! Data-quality verdict (SPEC 5.1.5) and partition merge.
//!
//! The verdict rules live in one place, `qt_ingest::dq::apply_verdict` (re-exported here with the INFO counter
//! list): replay calls it with the replayed event count. This module adds the `dq_flags` bits of a shadow result
//! and the partition merge.

pub use qt_ingest::dq::{INFO_SOURCE_COUNTERS, UNUSABLE_RATIO, apply_verdict, is_info_source_counter};
use qt_schema::json::{DataQuality, Verdict};

/// `dq_flags` bits of a shadow result: 1 = verdict warnings, 2 = verdict unusable.
pub fn dq_flags(v: Verdict) -> u32 {
    match v {
        Verdict::Clean => 0,
        Verdict::Warnings => 1,
        Verdict::Unusable => 2,
    }
}

/// Sums partition counters (feed gaps concatenated, source-specific counters added by key); the verdict is the
/// worst partition verdict and reasons are prefixed with the partition label.
pub fn merge(parts: &[(String, DataQuality)]) -> DataQuality {
    let mut m = DataQuality::default();
    for (label, p) in parts {
        m.records += p.records;
        m.maybe_bad_book_records += p.maybe_bad_book_records;
        m.bad_ts_recv_records += p.bad_ts_recv_records;
        m.snapshot_records += p.snapshot_records;
        m.pre_snapshot_rows_skipped += p.pre_snapshot_rows_skipped;
        m.unknown_order_refs += p.unknown_order_refs;
        m.duplicate_adds += p.duplicate_adds;
        m.unexpected_modifies += p.unexpected_modifies;
        m.sequence_gaps += p.sequence_gaps;
        m.sequence_regressions += p.sequence_regressions;
        m.out_of_order_ts_event += p.out_of_order_ts_event;
        m.crossed_books_after_last += p.crossed_books_after_last;
        m.hidden_prints += p.hidden_prints;
        m.off_price_executions += p.off_price_executions;
        m.feed_gaps.extend(p.feed_gaps.iter().cloned());
        for (k, v) in &p.source_specific {
            *m.source_specific.entry(k.clone()).or_insert(0) += v;
        }
        m.verdict = m.verdict.max(p.verdict);
        m.reasons.extend(p.reasons.iter().map(|r| format!("{label}: {r}")));
    }
    m
}
