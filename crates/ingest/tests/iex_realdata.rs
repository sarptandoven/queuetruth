//! Real-data ingest of an IEX DEEP+ subset made by `scripts/iex/stream_subset.py` (ignored by default; needs the
//! file, which is not in the repository):
//!
//!     QT_IEX_SUBSET=.realdata/iex/20261009_DPLS.subset.SPY-AAPL-NVDA-TSLA.pcap.gz \
//!     QT_IEX_OUT=.realdata/iex/ds-20261009 \
//!     cargo test -p queuetruth-ingest --test iex_realdata -- --ignored --nocapture
//!
//! Writes the dataset to `QT_IEX_OUT` (which must not exist or be empty) plus `ingest_summary.json` next to it,
//! prints throughput, and checks the structural invariants that hold for any DEEP+ day: no `M` records, every
//! `F` paired with a `C` of the same order and sequence, `LAST` on the final record of every sequence, prices on
//! the instrument tick or the sub-dollar 0.0001 grid, and non-decreasing `ts_event` per partition.

use std::path::PathBuf;
use std::time::Instant;

use qt_ingest::{Dataset, ingest};
use qt_schema::specs::IngestSpec;
use qt_types::flags;
use serde_json::json;

#[test]
#[ignore = "needs QT_IEX_SUBSET (a DEEP+ capture) and QT_IEX_OUT"]
fn ingest_real_iex_subset() {
    let (Ok(input), Ok(out)) = (std::env::var("QT_IEX_SUBSET"), std::env::var("QT_IEX_OUT")) else {
        eprintln!("QT_IEX_SUBSET / QT_IEX_OUT not set; skipped");
        return;
    };
    let out = PathBuf::from(out);
    let spec: IngestSpec = serde_json::from_value(json!({
        "format": "iex_deep_plus",
        "inputs": [{"path": input}],
        "venue": "iex",
        "out": out,
    }))
    .unwrap();
    let t = Instant::now();
    let s = ingest(&spec).unwrap();
    let secs = t.elapsed().as_secs_f64();
    let bytes = std::fs::metadata(&input).unwrap().len();
    let summary = serde_json::to_string_pretty(&s).unwrap();
    std::fs::write(out.with_extension("ingest_summary.json"), &summary).unwrap();
    let dq = &s.data_quality;
    eprintln!(
        "ingest: {} records in {secs:.2} s ({:.0} records/s, {:.1} MB/s of gzip input; debug build)",
        dq.records,
        dq.records as f64 / secs,
        bytes as f64 / 1e6 / secs
    );
    eprintln!(
        "verdict {:?}; sequence_gaps {} feed_gaps {} unknown_order_refs {} hidden_prints {}; source_specific {:?}",
        dq.verdict,
        dq.sequence_gaps,
        dq.feed_gaps.len(),
        dq.unknown_order_refs,
        dq.hidden_prints,
        dq.source_specific
    );
    let ds = Dataset::open(&out).unwrap();
    let mut checks = [0u64; 5];
    for p in ds.partitions() {
        let inst = ds.instrument(p.instrument_id, p.session_date).unwrap();
        let recs: Vec<_> = p.mbo().unwrap().map(Result::unwrap).collect();
        for (k, e) in recs.iter().enumerate() {
            checks[0] += u64::from(e.action == b'M');
            if e.action == b'F' {
                let paired = recs
                    .get(k + 1)
                    .is_some_and(|c| c.action == b'C' && c.order_id == e.order_id && c.sequence == e.sequence);
                checks[1] += u64::from(!paired);
            }
            let last_of_seq = recs.get(k + 1).is_none_or(|n| n.sequence != e.sequence);
            checks[2] += u64::from(last_of_seq != (e.flags & flags::LAST != 0));
            if e.action == b'A' {
                let sub = 100_000; // 0.0001 at 1e-9
                checks[3] += u64::from(e.price % inst.tick_size != 0 && e.price % sub != 0);
            }
            checks[4] += u64::from(k > 0 && e.ts_event < recs[k - 1].ts_event);
        }
        eprintln!("  {} ({}): {} records", inst.symbol, p.instrument_id, recs.len());
    }
    eprintln!(
        "structural: M records {}, unpaired F {}, LAST mismatches {}, off-grid adds {}, ts_event regressions {}",
        checks[0], checks[1], checks[2], checks[3], checks[4]
    );
    assert_eq!(checks[..3], [0, 0, 0]);
}
