//! The reference data-quality verdict (`refbook::finalize_verdict`) classifies `source_specific` counters with the
//! ingest's INFO list (`qt_ingest::dq::INFO_SOURCE_COUNTERS`): an INFO counter never raises the verdict, any other
//! non-zero counter does.

use qt_ingest::dq::INFO_SOURCE_COUNTERS;
use qt_schema::json::{DataQuality, Verdict};
use qt_testkit::refbook::finalize_verdict;

fn with_counter(name: &str, v: u64) -> DataQuality {
    let mut dq = DataQuality {
        records: 100,
        ..DataQuality::default()
    };
    dq.source_specific.insert(name.to_string(), v);
    dq
}

#[test]
fn every_ingest_info_counter_keeps_the_verdict_clean() {
    for name in INFO_SOURCE_COUNTERS {
        let mut dq = with_counter(name, 7);
        finalize_verdict(&mut dq, 100);
        assert_eq!(dq.verdict, Verdict::Clean, "{name}");
        assert!(dq.reasons.is_empty(), "{name}: {:?}", dq.reasons);
    }
}

#[test]
fn other_source_counters_raise_warnings() {
    for name in [
        "removal_after_unpaired_fill",
        "unterminated_events",
        "nyse_parity_ranks_approximate",
    ] {
        let mut dq = with_counter(name, 2);
        finalize_verdict(&mut dq, 100);
        assert_eq!(dq.verdict, Verdict::Warnings, "{name}");
        assert_eq!(dq.reasons, vec![format!("{name} = 2")]);
        // zero is never a reason
        let mut dq = with_counter(name, 0);
        finalize_verdict(&mut dq, 100);
        assert_eq!(dq.verdict, Verdict::Clean, "{name}");
    }
}
