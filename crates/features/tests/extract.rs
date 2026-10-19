//! End-to-end `extract` on a testkit dataset (gate test: needs the zoo tracker models): both populations, the
//! `qt.features` file, the summary, and determinism across runs and threads.

use std::path::{Path, PathBuf};

use qt_features::{FEATURES, PRIVILEGED, extract};
use qt_schema::codec;
use qt_schema::io::{TableWriter, read_table};
use qt_schema::rows::{PriceSpec, ShadowSource, ShadowSpec};
use qt_schema::specs::FeatureSpec;
use qt_testkit::{SynthConfig, generate, write_dataset};
use qt_types::{BookSide, Venue};

fn fixture() -> (PathBuf, PathBuf, PathBuf) {
    let root = Path::new(env!("CARGO_TARGET_TMPDIR")).join("qt_features_e2e");
    let _ = std::fs::remove_dir_all(&root);
    let mut sc = SynthConfig::for_venue(Venue::Nasdaq, 5);
    sc.instruments = 2;
    sc.events_per_instrument = 6_000;
    let out = generate(&sc);
    let ds = write_dataset(&out, &root.join("ds")).unwrap_or_else(|e| panic!("{e}"));
    let mut shadows = Vec::new();
    for inst in &out.instruments {
        let ts0 = out
            .events
            .iter()
            .find(|e| e.instrument_id == inst.instrument_id)
            .map_or(0, |e| e.ts_event);
        for i in 0..60i64 {
            shadows.push(ShadowSpec {
                shadow_id: format!("i{}s{i}", inst.instrument_id),
                instrument_id: inst.instrument_id,
                side: if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask },
                price: PriceSpec::Touch {
                    offset_ticks: -(((i / 2) % 2) as i32),
                },
                qty: 100,
                ts_submit: ts0 + i * 2_000_000_000,
                latency_ns: 0,
                ts_cancel: None,
                group: None,
                source: ShadowSource::Sampled,
                order_key: None,
            });
        }
    }
    let sh = root.join("shadows.parquet");
    let mut w = TableWriter::create(&sh, "qt.shadow_spec").unwrap_or_else(|e| panic!("{e}"));
    w.write(&codec::shadow_specs_to_batch(&shadows))
        .unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    (root, ds, sh)
}

fn spec(v: serde_json::Value) -> FeatureSpec {
    serde_json::from_value(v).unwrap_or_else(|e| panic!("{e}"))
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the zoo tracker models")]
fn extract_end_to_end_both_populations() {
    let (root, ds, sh) = fixture();
    let a = root.join("a");
    let s = spec(serde_json::json!({
        "dataset": {"path": ds.to_string_lossy()}, "shadows": {"path": sh.to_string_lossy()},
        "out": a.to_string_lossy(),
    }));
    let sum = extract(&s).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(
        sum.feature_names,
        FEATURES.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>()
    );
    assert_eq!(
        sum.privileged_names,
        PRIVILEGED.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>()
    );
    assert!(sum.n_orders > 50 && sum.n_rows >= sum.n_orders, "{sum:?}");
    assert_eq!(
        sum.by_cause.fill + sum.by_cause.cancel + sum.by_cause.censor,
        sum.n_rows
    );
    assert_eq!(sum.files.len(), 1);
    assert_eq!(sum.files[0].rows, Some(sum.n_rows));
    let batches: Vec<_> = read_table(&a.join("features.parquet"))
        .unwrap_or_else(|e| panic!("{e}"))
        .collect::<Result<_, _>>()
        .unwrap_or_else(|e| panic!("{e}"));
    let schema = batches[0].schema();
    // 1.1 layout: fixed head, features, event_ns, cause, midnight_utc_ns.
    assert_eq!(schema.fields().len(), 7 + FEATURES.len() + PRIVILEGED.len() + 3);
    assert_eq!(schema.field(7).name(), "dist_ticks");
    assert_eq!(
        schema.fields().last().map(|f| f.name().as_str()),
        Some("midnight_utc_ns")
    );
    let mut rows_a = Vec::new();
    for b in &batches {
        rows_a.extend(codec::batch_to_features(b).unwrap_or_else(|e| panic!("{e}")).1);
    }
    assert_eq!(rows_a.len() as u64, sum.n_rows);
    for r in &rows_a {
        let mid = r
            .midnight_utc_ns
            .unwrap_or_else(|| panic!("midnight_utc_ns missing: {r:?}"));
        let local = r.ts_landmark - mid;
        assert!((0..2 * 86_400_000_000_000).contains(&local), "{r:?}");
    }

    // 1.1: cancel_away_ticks null (or 0) = never cancel: the same orders, no policy cancel, identical files.
    let mut nc = s.clone();
    nc.cancel_away_ticks = None;
    nc.out = Some(root.join("nc").to_string_lossy().into_owned());
    let sum_nc = extract(&nc).unwrap_or_else(|e| panic!("{e}"));
    let mut z = nc.clone();
    z.cancel_away_ticks = Some(0);
    z.out = Some(root.join("z").to_string_lossy().into_owned());
    let sum_z = extract(&z).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(sum_z.files[0].sha256, sum_nc.files[0].sha256);
    assert_eq!(sum_nc.n_orders, sum.n_orders);
    assert!(sum_nc.by_cause.cancel <= sum.by_cause.cancel, "{sum_nc:?} vs {sum:?}");
    assert!(sum_nc.by_cause.fill >= sum.by_cause.fill, "{sum_nc:?} vs {sum:?}");
    assert!(sum_nc.spec["cancel_away_ticks"].is_null());

    let b = root.join("b");
    let mut s2 = s.clone();
    s2.out = Some(b.to_string_lossy().into_owned());
    s2.threads = 3;
    let sum2 = extract(&s2).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(sum2.files[0].sha256, sum.files[0].sha256);
    assert_eq!(sum2.spec, sum.spec);

    let r = root.join("r");
    let sr = spec(serde_json::json!({
        "dataset": {"path": ds.to_string_lossy()}, "population": "real_orders", "n_real_orders": 300,
        "out": r.to_string_lossy(), "privileged": false, "landmarks_ns": [0, 1_000_000_000],
    }));
    let sumr = extract(&sr).unwrap_or_else(|e| panic!("{e}"));
    assert!(sumr.n_orders > 0 && sumr.n_orders <= 300, "{sumr:?}");
    assert!(sumr.privileged_names.is_empty());
    let again = extract(&sr).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(again.files[0].sha256, sumr.files[0].sha256);
    let _ = std::fs::remove_dir_all(&root);
}

/// Schema 1.1 contract: a population-specific field set for the other population is QT-CONFIG-001.
#[test]
fn population_fields_of_the_other_population_are_config_errors() {
    let base = serde_json::json!({"dataset": {"path": "/nonexistent"}, "out": "/nonexistent/out"});
    let mut a = base.clone();
    a["n_real_orders"] = serde_json::json!(10);
    a["shadows"] = serde_json::json!({"path": "/nonexistent/s.parquet"});
    let e = extract(&spec(a)).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001", "{e}");
    assert!(e.to_string().contains("n_real_orders"), "{e}");
    let mut b = base;
    b["population"] = serde_json::json!("real_orders");
    b["shadows"] = serde_json::json!({"path": "/nonexistent/s.parquet"});
    let e = extract(&spec(b)).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001", "{e}");
    assert!(e.to_string().contains("shadows"), "{e}");
}

/// Review m27: `n_orders` counts an order ref once per (instrument, session date), so real orders whose venue ids
/// repeat on another day are not merged.
#[test]
fn n_orders_is_per_instrument_day() {
    use qt_schema::rows::{Cause, FeatureRow, Population};
    let row = |r: &str, inst: u32, date: i32| FeatureRow {
        order_ref: r.into(),
        population: Population::RealOrders,
        instrument_id: inst,
        session_date: date,
        ts_landmark: 0,
        landmark_ns: 0,
        side_sign: 1,
        values: Vec::new(),
        event_ns: 0,
        cause: Cause::Censor,
        midnight_utc_ns: None,
    };
    let rows = vec![
        row("1:7", 1, 100),
        row("1:7", 1, 100),
        row("1:7", 1, 101),
        row("2:7", 2, 100),
    ];
    assert_eq!(qt_features::count_orders(&rows), 3);
}
