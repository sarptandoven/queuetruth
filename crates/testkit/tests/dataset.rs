//! `write_dataset`: SPEC 3.3 layout, manifest (`dataset_id` rule), round trip through `qt_schema` readers, data
//! quality, byte determinism and the `MANIFEST.sha256` writer.

use std::path::{Path, PathBuf};

use qt_schema::codec::{batch_to_instruments, batch_to_mbo, batch_to_trades};
use qt_schema::io::{read_table_as, sha256_file};
use qt_schema::json::{DataQuality, DatasetManifest, Verdict, dataset_id};
use qt_testkit::calendar::date_string;
use qt_testkit::{SOURCE_FORMAT, SynthConfig, generate_sessions, write_dataset, write_manifest_sha256};
use qt_types::{ValidationStatus, Venue};

fn tmp(name: &str) -> PathBuf {
    let d = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("qt-testkit-ds-{}-{name}", std::process::id()));
    if d.exists() {
        std::fs::remove_dir_all(&d).expect("clean");
    }
    d
}

fn read_all<T>(path: &Path, name: &str, f: impl Fn(&arrow_array::RecordBatch) -> qt_types::QtResult<Vec<T>>) -> Vec<T> {
    let mut out = Vec::new();
    for b in read_table_as(path, name).expect("open table") {
        out.extend(f(&b.expect("batch")).expect("decode"));
    }
    out
}

#[test]
fn dataset_layout_round_trip_and_manifest() {
    for venue in [Venue::Cme, Venue::Nasdaq] {
        let mut c = SynthConfig::for_venue(venue, 9);
        c.events_per_instrument = 800;
        let out = generate_sessions(&c, 2);
        let root = tmp(&format!("{venue}"));
        assert_eq!(write_dataset(&out, &root).expect("write"), root);
        let m: DatasetManifest =
            serde_json::from_slice(&std::fs::read(root.join("manifest.json")).expect("manifest")).expect("parse");
        assert_eq!((m.source_format.as_str(), m.venue), (SOURCE_FORMAT, venue));
        assert_eq!(m.validation_status, ValidationStatus::SyntheticOnly);
        assert_eq!(m.partitions.len(), 4);
        let keys: Vec<(i32, u32)> = m.partitions.iter().map(|p| (p.session_date, p.instrument_id)).collect();
        let mut sorted = keys.clone();
        sorted.sort_unstable();
        assert_eq!(keys, sorted, "partitions sorted by (session_date, instrument_id)");
        let mut shas = Vec::new();
        let mut total = 0;
        for p in &m.partitions {
            let f = &p.files[0];
            let path = root.join(&f.path);
            assert_eq!(
                f.path,
                format!(
                    "mbo/date={}/instrument_id={}/part-00000.parquet",
                    date_string(p.session_date),
                    p.instrument_id
                )
            );
            assert_eq!(sha256_file(&path).expect("sha"), f.sha256);
            shas.push(f.sha256.clone());
            let recs = read_all(&path, "qt.mbo", batch_to_mbo);
            assert_eq!(recs.len() as u64, p.rows);
            let inst = out
                .instruments
                .iter()
                .find(|i| i.instrument_id == p.instrument_id && i.session_date == p.session_date)
                .expect("instrument row");
            let day_end = qt_testkit::calendar::local_midnight(venue, p.session_date + 1);
            let want: Vec<_> = out
                .events
                .iter()
                .filter(|e| {
                    e.instrument_id == p.instrument_id && e.ts_event >= inst.midnight_utc_ns && e.ts_event < day_end
                })
                .copied()
                .collect();
            assert_eq!(recs, want, "partition feed order");
            assert_eq!((p.ts_min, p.ts_max), (want[0].ts_event, want[want.len() - 1].ts_event));
            let tpath = root.join(format!(
                "trades_source/date={}/instrument_id={}/part-00000.parquet",
                date_string(p.session_date),
                p.instrument_id
            ));
            let trades = read_all(&tpath, "qt.trades", batch_to_trades);
            let want_t: Vec<_> = out
                .expected_trades
                .iter()
                .filter(|t| {
                    t.instrument_id == p.instrument_id && t.ts_event >= inst.midnight_utc_ns && t.ts_event < day_end
                })
                .copied()
                .collect();
            assert_eq!(trades, want_t);
            total += recs.len();
        }
        assert_eq!(total, out.events.len());
        assert_eq!(m.dataset_id, dataset_id(&shas, SOURCE_FORMAT));
        let mut insts = read_all(
            &root.join("instruments.parquet"),
            "qt.instruments",
            batch_to_instruments,
        );
        let mut want = out.instruments.clone();
        insts.sort_by_key(|i| (i.session_date, i.instrument_id));
        want.sort_by_key(|i| (i.session_date, i.instrument_id));
        assert_eq!(insts, want);
        let dq: DataQuality =
            serde_json::from_slice(&std::fs::read(root.join("data_quality.json")).expect("dq")).expect("parse");
        assert_eq!(dq.records, out.events.len() as u64);
        // Hidden prints and off-price executions are INFO counters (E36(b)): synthetic data is clean.
        assert_eq!(dq.verdict, Verdict::Clean, "{venue}: {:?}", dq.reasons);
        // Byte determinism: a second write gives identical files.
        let root2 = tmp(&format!("{venue}-2"));
        write_dataset(&out, &root2).expect("write again");
        let a = write_manifest_sha256(&root).expect("manifest a");
        let b = write_manifest_sha256(&root2).expect("manifest b");
        assert_eq!(a, b);
        assert!(a.iter().all(|(p, _)| p != "MANIFEST.sha256"));
        let text = std::fs::read_to_string(root.join("MANIFEST.sha256")).expect("read manifest");
        assert_eq!(text.lines().count(), a.len());
        assert!(text.lines().all(|l| l.len() > 66 && &l[64..66] == "  "));
        std::fs::remove_dir_all(root).ok();
        std::fs::remove_dir_all(root2).ok();
    }
}

#[test]
fn dataset_rejects_records_without_instrument_row() {
    let mut c = SynthConfig::for_venue(Venue::Generic, 1);
    c.events_per_instrument = 50;
    let mut out = generate_sessions(&c, 1);
    out.instruments.retain(|i| i.instrument_id == 1);
    let root = tmp("norow");
    let err = write_dataset(&out, &root).expect_err("instrument 2 has no row");
    assert_eq!(err.code(), "QT-INPUT-002");
    std::fs::remove_dir_all(root).ok();
}
