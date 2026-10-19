//! Gate checks against `qt_testkit`: `Dataset::open` reads the testkit dataset layout, and `encode_dbn` output
//! ingests (`format = dbn`) to the same events, partition by partition.

use std::path::PathBuf;

use qt_ingest::{Dataset, ingest};
use qt_types::{MboEvent, Venue};
use serde_json::json;

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-ingest-gate-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    d
}

fn synth(venue: Venue) -> qt_testkit::SynthOutput {
    qt_testkit::generate(&qt_testkit::SynthConfig {
        seed: 3,
        venue,
        instruments: 2,
        events_per_instrument: 2_000,
        tick: 1,
        base_px: 10_000,
        max_levels: 10,
        mean_size: 20,
        p_add: 0.45,
        p_cancel: 0.25,
        p_reduce: 0.1,
        p_modify: if venue == Venue::Nasdaq { 0.0 } else { 0.1 },
        p_trade: 0.1,
        p_clear: 0.0,
        hidden_print_rate: 0.0,
        snapshot_at_start: venue == Venue::Cme,
        ts_start: 1_600_000_000_000_000_000,
        mean_gap_ns: 1_000_000,
    })
}

fn by_partition(root: &std::path::Path) -> Vec<(u32, i32, Vec<MboEvent>)> {
    let ds = Dataset::open(root).unwrap();
    ds.partitions()
        .iter()
        .map(|p| {
            (
                p.instrument_id,
                p.session_date,
                p.mbo().unwrap().map(Result::unwrap).collect(),
            )
        })
        .collect()
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit crate")]
fn gate_dataset_open_reads_testkit_layout_and_dbn_round_trip() {
    let out = synth(Venue::Cme);
    let a = scratch("tk");
    qt_testkit::write_dataset(&out, &a).unwrap();
    let ds = Dataset::open(&a).unwrap();
    let total: u64 = ds.partitions().iter().map(|p| p.rows).sum();
    assert_eq!(total, out.events.len() as u64);
    let mut flat: Vec<MboEvent> = by_partition(&a).into_iter().flat_map(|p| p.2).collect();
    let mut want = out.events.clone();
    flat.sort_by_key(|e| (e.instrument_id, e.sequence, e.ts_event));
    want.sort_by_key(|e| (e.instrument_id, e.sequence, e.ts_event));
    assert_eq!(flat, want);
    for p in ds.partitions() {
        assert!(ds.instrument(p.instrument_id, p.session_date).is_some());
        let _ = p.source_trades().unwrap();
    }

    let dir = scratch("dbn");
    std::fs::create_dir_all(&dir).unwrap();
    let f = dir.join("synth.dbn");
    qt_testkit::encode_dbn(&out.events, &f).unwrap();
    let b = dir.join("ds");
    ingest(
        &serde_json::from_value(json!({"format": "dbn", "venue": "cme", "out": b.to_str().unwrap(),
        "inputs": [{"path": f.to_str().unwrap()}]}))
        .unwrap(),
    )
    .unwrap();
    assert_eq!(
        by_partition(&b),
        by_partition(&a),
        "same partitions, same rows, same order"
    );
    std::fs::remove_dir_all(a).ok();
    std::fs::remove_dir_all(dir).ok();
}
