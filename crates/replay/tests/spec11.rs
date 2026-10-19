//! Schema 1.1 in `run_dataset`: the spec fields `fill_rule`, `hidden_print_policy` and `exclude_order_ids` are
//! honoured (with `ReplayOptions` as an override), a non-default value the engine would not honour is
//! `QT-CONFIG-001` (contract rule), and the writers emit the full 1.1 layout: `qt.shadow_fills.liquidity`,
//! `qt.shadow_results.ts_planned_end` / `planned_end_kind`, and `shadow_diagnostics` in the summary.

use std::path::{Path, PathBuf};

use qt_replay::{FillRule, ReplayOptions, run_dataset, run_dataset_with};
use qt_schema::codec::{TableRow, batch_liquidity, batch_planned_ends};
use qt_schema::io::{read_table_as, sha256_file};
use qt_schema::rows::{ShadowFillRow, ShadowResultRow};
use qt_schema::specs::ReplaySpec;
use serde_json::json;

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures")
        .join(name)
}

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-replay-spec11-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn spec(ds: &Path, out: Option<&Path>, shadows: &Path, extra: serde_json::Value) -> ReplaySpec {
    let mut v = json!({"dataset": {"path": ds.display().to_string()},
                       "shadows": {"path": shadows.display().to_string()}, "bbo": "none"});
    if let Some(o) = out {
        v["out"] = json!(o.display().to_string());
    }
    if let (Some(m), serde_json::Value::Object(e)) = (v.as_object_mut(), extra) {
        m.extend(e);
    }
    serde_json::from_value(v).unwrap()
}

fn read<R: TableRow>(p: &Path) -> Vec<R> {
    let mut out = Vec::new();
    for b in read_table_as(p, R::TABLE).unwrap() {
        out.extend(R::from_batch(&b.unwrap()).unwrap());
    }
    out
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit fixtures")]
fn spec_fields_honoured_and_unhonoured_values_rejected() {
    let nq = fixture("synth_small_nasdaq");
    let cme = fixture("synth_small");
    let nq_shadows = nq.join("truth/shadows.parquet");
    let cme_shadows = cme.join("truth/shadows.parquet");
    // Contract rule: a hidden-print policy the engine would not apply is a configuration error.
    for (ds, sh, extra) in [
        (
            &nq,
            &nq_shadows,
            json!({"fill_rule": "et", "hidden_print_policy": "behind"}),
        ),
        (
            &nq,
            &nq_shadows,
            json!({"fill_rule": "et", "hidden_print_policy": "behind_and_through"}),
        ),
        (&cme, &cme_shadows, json!({"hidden_print_policy": "behind"})),
    ] {
        let err = run_dataset(&spec(ds, None, sh, extra.clone())).expect_err("not honoured");
        assert_eq!(err.code(), "QT-CONFIG-001", "{extra}: {err}");
    }
    // The same through the override.
    let over = ReplayOptions {
        fill_rule: FillRule::Et,
        ..ReplayOptions::default()
    };
    let err = run_dataset_with(
        &spec(&nq, None, &nq_shadows, json!({"hidden_print_policy": "behind"})),
        &over,
    )
    .expect_err("override conflicts with the spec policy");
    assert_eq!(err.code(), "QT-CONFIG-001");
    // `ignore` is honoured anywhere.
    run_dataset(&spec(
        &cme,
        None,
        &cme_shadows,
        json!({"hidden_print_policy": "ignore"}),
    ))
    .unwrap();
    // fill_rule from the spec, and from the override.
    let exact = run_dataset(&spec(&nq, None, &nq_shadows, json!({}))).unwrap();
    let et = run_dataset(&spec(&nq, None, &nq_shadows, json!({"fill_rule": "et"}))).unwrap();
    let et2 = run_dataset_with(&spec(&nq, None, &nq_shadows, json!({})), &over).unwrap();
    assert!(
        et.warnings.iter().any(|w| w.contains("fill rule Et")),
        "{:?}",
        et.warnings
    );
    assert!(et2.warnings.iter().any(|w| w.contains("fill rule Et")));
    assert!(exact.warnings.iter().any(|w| w.contains("fill rule Exact")));
    assert_ne!(
        et.shadows, exact.shadows,
        "the et rule changes the outcome on the Nasdaq fixture"
    );
    // hidden_print_policy from the spec: ignore changes the Nasdaq outcome.
    let ign = run_dataset(&spec(&nq, None, &nq_shadows, json!({"hidden_print_policy": "ignore"}))).unwrap();
    let diag = |s: &qt_schema::json::ReplaySummary| s.shadow_diagnostics.clone().expect("diagnostics");
    assert!(diag(&exact).hidden_print_fills > 0);
    assert_eq!(diag(&ign).hidden_print_fills, 0);
    // exclude_order_ids from the spec reproduce the A1 reference of the own orders byte for byte.
    let own = cme.join("own_orders");
    let orders = read_table_as(&own.join("live/orders.parquet"), "qt.live_orders").unwrap();
    let mut ids = Vec::new();
    for b in orders {
        let b = b.unwrap();
        let inst = b
            .column_by_name("instrument_id")
            .unwrap()
            .as_any()
            .downcast_ref::<arrow_array::UInt32Array>()
            .unwrap()
            .clone();
        let vid = b
            .column_by_name("venue_order_id")
            .unwrap()
            .as_any()
            .downcast_ref::<arrow_array::StringArray>()
            .map(|a| a.iter().map(|x| x.unwrap().parse::<u64>().unwrap()).collect::<Vec<_>>())
            .unwrap();
        for (i, v) in inst.iter().zip(vid) {
            ids.push(json!({"instrument_id": i.unwrap(), "order_id": v}));
        }
    }
    assert!(!ids.is_empty());
    let out = scratch("excluded");
    let s = run_dataset(&spec(
        &cme,
        Some(&out),
        &own.join("schedule.parquet"),
        json!({"exclude_order_ids": ids}),
    ))
    .unwrap();
    assert_eq!(
        sha256_file(&out.join("shadow_results.parquet")).unwrap(),
        sha256_file(&own.join("expected_results.parquet")).unwrap()
    );
    // Fills: the same rows (the engine writes them in event order, the reference grouped by shadow).
    let key = |f: &ShadowFillRow| (f.shadow_id.clone(), f.fill_seq);
    let mut got: Vec<ShadowFillRow> = read(&out.join("shadow_fills.parquet"));
    let mut want: Vec<ShadowFillRow> = read(&own.join("expected_fills.parquet"));
    got.sort_by_key(key);
    want.sort_by_key(key);
    assert_eq!(got, want);
    let d = diag(&s);
    assert_eq!(
        (d.excluded_requested, d.excluded_matched),
        (ids.len() as u64, ids.len() as u64)
    );
    std::fs::remove_dir_all(&out).ok();
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit fixtures")]
fn full_layout_liquidity_planned_end_and_diagnostics() {
    for name in ["synth_small", "synth_small_nasdaq"] {
        let f = fixture(name);
        let out = scratch(name);
        let s = run_dataset(&spec(
            &f,
            Some(&out),
            &f.join("truth/shadows.parquet"),
            json!({"amends": {"path": f.join("truth/amends.parquet").display().to_string()}}),
        ))
        .unwrap();
        assert!(s.shadow_diagnostics.is_some());
        // Liquidity: taker for crossed_at_live and for crossed fills in a locked / crossed tape (A5), maker
        // otherwise; equal to the reference truth written by the testkit (independent tape reading).
        let liq_of = |p: &Path| {
            let mut m = std::collections::BTreeMap::new();
            for b in read_table_as(p, "qt.shadow_fills").unwrap() {
                let b = b.unwrap();
                let liq = batch_liquidity(&b, "qt.shadow_fills").unwrap().expect("1.1 layout");
                for (r, l) in ShadowFillRow::from_batch(&b).unwrap().into_iter().zip(liq) {
                    m.insert((r.shadow_id.clone(), r.fill_seq), (r.reason, l));
                }
            }
            m
        };
        let got = liq_of(&out.join("shadow_fills.parquet"));
        let want = liq_of(&f.join("truth/shadow_fills.parquet"));
        assert_eq!(got, want, "{name}");
        let n_taker = got
            .values()
            .filter(|x| x.1 == Some(qt_schema::rows::Liquidity::Taker))
            .count();
        let n_cross_maker = got
            .values()
            .filter(|x| x.0 == qt_schema::rows::FillReason::Crossed && x.1 == Some(qt_schema::rows::Liquidity::Maker))
            .count();
        assert!(n_taker > 0 && n_cross_maker > 0, "{name}: {n_taker} {n_cross_maker}");
        // Planned ends: equal to the independent reference's (cancel effective time vs session close).
        let ds = qt_ingest::Dataset::open(&f).unwrap();
        let mut n = 0;
        for b in read_table_as(&out.join("shadow_results.parquet"), "qt.shadow_results").unwrap() {
            let b = b.unwrap();
            let planned = batch_planned_ends(&b).unwrap().expect("1.1 layout");
            let rows = ShadowResultRow::from_batch(&b).unwrap();
            for (r, p) in rows.iter().zip(planned) {
                assert_eq!(
                    p,
                    qt_testkit::shadows::planned_end(r, &ds.instruments),
                    "{}",
                    r.shadow_id
                );
                n += 1;
            }
        }
        assert_eq!(n, 1000, "{name}");
        // The committed truth (written by the testkit in the same layout) has the same files.
        assert_eq!(
            sha256_file(&out.join("shadow_results.parquet")).unwrap(),
            sha256_file(&f.join("truth/shadow_results.parquet")).unwrap(),
            "{name}"
        );
        std::fs::remove_dir_all(&out).ok();
    }
}

/// A1 / review F5a: `why` for a live order (live log + order key) removes the order's own MBO records, so its twin
/// never appears in the displayed queue; without the live log the twin is the order queued ahead of the shadow.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit fixtures")]
fn why_live_order_excludes_its_own_twin() {
    let f = fixture("synth_small");
    let own = f.join("own_orders");
    let sched = own.join("schedule.parquet");
    let specs = qt_schema::codec::read_shadow_specs(&sched).unwrap();
    let mut with_twin = 0;
    for s in specs.iter().take(20) {
        let key = s.order_key.clone().expect("live-log schedule rows carry the order key");
        let oid: u64 = key.rsplit(':').next().unwrap().parse().unwrap();
        let base = json!({"dataset": {"path": f.display().to_string()},
                          "shadows": {"path": sched.display().to_string()},
                          "shadow_id": s.shadow_id, "order_key": key, "max_events": 100000});
        let plain: qt_schema::specs::WhySpec = serde_json::from_value(base.clone()).unwrap();
        let mut v = base;
        v["live"] = json!({"path": own.join("live").display().to_string()});
        let live: qt_schema::specs::WhySpec = serde_json::from_value(v).unwrap();
        let t_plain = qt_replay::why(&plain).unwrap();
        let t_live = qt_replay::why(&live).unwrap();
        assert!(t_live.events.iter().all(|e| e.order_id != oid), "{key}");
        with_twin += usize::from(t_plain.events.iter().any(|e| e.order_id == oid && e.is_ahead));
    }
    assert!(with_twin > 0, "the plain trace shows the twin ahead of the shadow");
}

/// `WhySpec.exclude_order_ids` removes every listed order (like the scorer's exact arm), not only a traced live
/// order's own record.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit fixtures")]
fn why_honours_exclude_order_ids() {
    let f = fixture("synth_small");
    let sched = f.join("own_orders/schedule.parquet");
    let specs = qt_schema::codec::read_shadow_specs(&sched).unwrap();
    let ids: Vec<(u32, u64)> = specs
        .iter()
        .map(|s| {
            (
                s.instrument_id,
                s.order_key
                    .as_ref()
                    .unwrap()
                    .rsplit(':')
                    .next()
                    .unwrap()
                    .parse()
                    .unwrap(),
            )
        })
        .collect();
    let excl: Vec<_> = ids
        .iter()
        .map(|&(i, o)| json!({"instrument_id": i, "order_id": o}))
        .collect();
    for s in specs.iter().take(10) {
        let w: qt_schema::specs::WhySpec = serde_json::from_value(json!({
            "dataset": {"path": f.display().to_string()}, "shadows": {"path": sched.display().to_string()},
            "shadow_id": s.shadow_id, "max_events": 100000, "exclude_order_ids": excl
        }))
        .unwrap();
        let t = qt_replay::why(&w).unwrap();
        assert!(
            t.events.iter().all(|e| !ids.iter().any(|&(_, o)| o == e.order_id)),
            "{}",
            s.shadow_id
        );
    }
}

/// `DatasetManifest.rule_set` decides the venue rules: a generic dataset marked `crypto_l3` has no hidden orders, so
/// its side-N prints are not hidden prints.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit crate")]
fn manifest_rule_set_selects_the_rules() {
    let mut cfg = qt_testkit::SynthConfig::for_venue(qt_types::Venue::Generic, 5);
    cfg.instruments = 1;
    cfg.events_per_instrument = 800;
    let out = qt_testkit::generate(&cfg);
    let root = scratch("ruleset");
    qt_testkit::write_dataset(&out, &root).unwrap();
    let run = |root: &Path| {
        let spec: ReplaySpec =
            serde_json::from_value(json!({"dataset": {"path": root.display().to_string()}, "bbo": "none"})).unwrap();
        run_dataset(&spec).unwrap()
    };
    let generic = run(&root);
    assert!(generic.data_quality.hidden_prints > 0);
    let mp = root.join("manifest.json");
    let mut m: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&mp).unwrap()).unwrap();
    m["rule_set"] = json!("crypto_l3");
    std::fs::write(&mp, serde_json::to_string_pretty(&m).unwrap()).unwrap();
    let l3 = run(&root);
    assert_eq!(l3.data_quality.hidden_prints, 0);
    std::fs::remove_dir_all(&root).ok();
}

/// `ExcludeOrderId.session_date`: an exclusion with a session date applies on that date only.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit fixtures")]
fn dated_exclusions_apply_on_their_session_date_only() {
    let cme = fixture("synth_small");
    let own = cme.join("own_orders");
    let specs = qt_schema::codec::read_shadow_specs(&own.join("schedule.parquet")).unwrap();
    let ids: Vec<(u32, u64)> = specs
        .iter()
        .map(|s| {
            (
                s.instrument_id,
                s.order_key
                    .as_ref()
                    .unwrap()
                    .rsplit(':')
                    .next()
                    .unwrap()
                    .parse()
                    .unwrap(),
            )
        })
        .collect();
    let run = |excl: Vec<serde_json::Value>| {
        let out = scratch("dated");
        let s = run_dataset(&spec(
            &cme,
            Some(&out),
            &own.join("schedule.parquet"),
            json!({"exclude_order_ids": excl}),
        ))
        .unwrap();
        let sha = sha256_file(&out.join("shadow_results.parquet")).unwrap();
        std::fs::remove_dir_all(&out).ok();
        (s, sha)
    };
    let want = sha256_file(&own.join("expected_results.parquet")).unwrap();
    // Every own order is dated on its own session date (both fixture dates): the A1 reference again.
    let ds = qt_ingest::Dataset::open(&cme).unwrap();
    let dated: Vec<_> = specs
        .iter()
        .zip(&ids)
        .map(|(s, &(i, o))| {
            let p = ds.partition_for(i, s.ts_submit).unwrap();
            json!({"instrument_id": i, "order_id": o, "session_date": qt_ingest::time::format_date(p.session_date)})
        })
        .collect();
    let (s, sha) = run(dated);
    assert_eq!(sha, want);
    assert_eq!(s.shadow_diagnostics.unwrap().excluded_matched, ids.len() as u64);
    // Dated on a day without data: nothing is excluded.
    let wrong: Vec<_> = ids
        .iter()
        .map(|&(i, o)| json!({"instrument_id": i, "order_id": o, "session_date": "2001-01-01"}))
        .collect();
    let (s, sha) = run(wrong);
    assert_ne!(sha, want);
    assert_eq!(s.shadow_diagnostics.unwrap().excluded_matched, 0);
    let bad = run_dataset(&spec(
        &cme,
        None,
        &own.join("schedule.parquet"),
        json!({"exclude_order_ids": [{"instrument_id": 1, "order_id": 1, "session_date": "2026-13-40"}]}),
    ))
    .expect_err("malformed date");
    assert_eq!(bad.code(), "QT-CONFIG-001");
}
