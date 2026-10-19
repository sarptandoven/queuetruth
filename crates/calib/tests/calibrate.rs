//! End-to-end `calibrate` on a testkit dataset (gate tests: they need the zoo's NT tracker and particle simulator)
//! and O-CAL-4 (survival recovery with the real particle simulator: truth drawn from the simulator's own curves at
//! `k0 = -0.8` with one seed, fitted with another).

use std::path::{Path, PathBuf};

use qt_calib::{calibrate, fit_survival, optim};
use qt_schema::io::{TableWriter, read_table};
use qt_schema::rows::{PriceSpec, ShadowSource, ShadowSpec};
use qt_schema::specs::CalibrationSpec;
use qt_schema::{codec, json};
use qt_testkit::{SynthConfig, generate, write_dataset};
use qt_types::{BookSide, Dur, QtResult, Venue, Xoshiro256StarStar, stable_seed};
use qt_zoo::{ParticleSimulator, ShadowCurve};

fn fixture(name: &str) -> (PathBuf, PathBuf, PathBuf) {
    let root = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("qt_calib_{name}"));
    let _ = std::fs::remove_dir_all(&root);
    let mut sc = SynthConfig::for_venue(Venue::Cme, 7);
    sc.instruments = 2;
    sc.events_per_instrument = 15_000;
    let out = generate(&sc);
    let ds = write_dataset(&out, &root.join("ds")).unwrap_or_else(|e| panic!("{e}"));
    let mut shadows = Vec::new();
    for inst in &out.instruments {
        let ts0 = out
            .events
            .iter()
            .find(|e| e.instrument_id == inst.instrument_id)
            .map_or(0, |e| e.ts_event);
        for i in 0..150i64 {
            shadows.push(ShadowSpec {
                shadow_id: format!("i{}s{i}", inst.instrument_id),
                instrument_id: inst.instrument_id,
                side: if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask },
                price: PriceSpec::Touch {
                    offset_ticks: -(((i / 2) % 2) as i32),
                },
                qty: 1 + (i % 3) as u64,
                ts_submit: ts0 + i * 4_000_000_000,
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

fn spec(kind: &str, family: &str, ds: &Path, sh: &Path, out: &Path) -> CalibrationSpec {
    let v = serde_json::json!({
        "kind": kind, "family": family,
        "dataset": {"path": ds.to_string_lossy()}, "shadows": {"path": sh.to_string_lossy()},
        "out": out.to_string_lossy(), "bootstrap": {"b": 40, "block": "30min", "seed": 3},
        "particles": 40,
    });
    serde_json::from_value(v).unwrap_or_else(|e| panic!("{e}"))
}

#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: needs the zoo nt_queue_l2 tracker and particle simulator"
)]
fn calibrate_end_to_end_alloc_touch_survival() {
    let (root, ds, sh) = fixture("e2e");
    let s = spec("alloc", "power3", &ds, &sh, &root.join("alloc"));
    let r = calibrate(&s).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(r.run_id.len(), 16);
    assert_eq!(r.param_name.as_deref(), Some("n"));
    let n = r.estimate.unwrap_or_else(|| panic!("no estimate: {r:?}"));
    assert!(n > 0.0 && n.is_finite());
    // Every shadow lives in one 30-minute block, so every bootstrap replicate is the same fit: the zero-width
    // interval is suppressed with a `degenerate_ci` trust line instead of being published as precision.
    eprintln!(
        "power3: n = {n}, ci_bootstrap {:?}, warnings {:#?}",
        r.ci_bootstrap, r.warnings
    );
    match r.ci_bootstrap.unwrap_or((None, None)) {
        (Some(lo), Some(hi)) => assert!(lo < hi && lo <= n && n <= hi, "{:?}", r.ci_bootstrap),
        (None, None) => assert!(
            r.warnings.iter().any(|w| w.starts_with("trust: degenerate_ci")),
            "{:?}",
            r.warnings
        ),
        other => panic!("half-open bootstrap CI {other:?}"),
    }
    assert!(r.n_events > 0 && r.n_shadows > 0);
    assert!(r.held_out.is_empty() && r.exports.hftbacktest.is_none());
    assert!(r.warnings.iter().any(|w| w.starts_with("rust part only")));
    // Structured trust (schema 1.1): a Rust-only result has no held-out comparison, so it is never exportable.
    let t = r.trust.clone().unwrap_or_else(|| panic!("no trust field"));
    assert_eq!(t.verdict, json::TrustVerdict::Refused);
    assert!(t.reasons.iter().any(|x| x == "no_held_out") && t.beats_default.is_empty());
    assert_eq!(t.flags, qt_calib::trust::line_tokens(&r.warnings));
    assert!(!r.spec.contains_key("out") && !r.spec.contains_key("threads"));
    let fit_json = std::fs::read(root.join("alloc/fit.json")).unwrap_or_default();
    let doc: json::CalibrationResult = serde_json::from_slice(&fit_json).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(doc, r);
    let rows: usize = read_table(&root.join("alloc/alloc_events.parquet"))
        .unwrap_or_else(|e| panic!("{e}"))
        .map(|b| b.map(|b| b.num_rows()).unwrap_or(0))
        .sum();
    assert!(rows as u64 >= r.n_events);
    assert!(root.join("alloc/touch_records.parquet").exists());

    // Deterministic across runs, output directories and threads (fit.json bytes).
    let mut s2 = spec("alloc", "power3", &ds, &sh, &root.join("alloc2"));
    s2.threads = 4;
    let r2 = calibrate(&s2).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(r2, r);
    assert_eq!(
        std::fs::read(root.join("alloc2/fit.json")).unwrap_or_default(),
        fit_json
    );
    for name in ["alloc_events.parquet", "touch_records.parquet"] {
        assert_eq!(
            qt_schema::io::sha256_file(&root.join("alloc").join(name)).ok(),
            qt_schema::io::sha256_file(&root.join("alloc2").join(name)).ok(),
            "{name}"
        );
    }

    let db = calibrate(&spec("alloc", "databento", &ds, &sh, &root.join("db"))).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(db.param_name.as_deref(), Some("k"));
    assert!(db.estimate.is_some_and(|k| (-0.99..=0.99).contains(&k)));

    for fam in ["nt_prob_touch", "nt_queue_l2_prob_touch"] {
        let t = calibrate(&spec("nt_touch", fam, &ds, &sh, &root.join(fam))).unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(t.param_name.as_deref(), Some("p"));
        assert!(
            t.estimate.is_some_and(|p| p > 0.0 && p <= 1.0),
            "{fam}: {:?}",
            t.estimate
        );
    }

    let sv = calibrate(&spec("survival", "databento", &ds, &sh, &root.join("surv"))).unwrap_or_else(|e| panic!("{e}"));
    assert!(sv.estimate.is_some_and(|k| (-0.98..=0.9).contains(&k)));

    let bad = calibrate(&spec("alloc", "nt_prob_touch", &ds, &sh, &root.join("bad")));
    assert!(bad.is_err_and(|e| e.code() == "QT-CONFIG-001"));
    let lat = calibrate(&spec("latency", "latency", &ds, &sh, &root.join("lat")));
    assert!(lat.is_err_and(|e| e.code() == "QT-CONFIG-001"));
    let _ = std::fs::remove_dir_all(&root);
}

/// The real simulator with its truth replaced by draws from its own curves at `k0` (seed A).
struct DrawnTruth {
    inner: Box<dyn ParticleSimulator>,
    truth: Vec<(Option<usize>, Option<usize>)>,
}

impl ParticleSimulator for DrawnTruth {
    fn horizons(&self) -> &[Dur] {
        self.inner.horizons()
    }
    fn simulate(&self, theta: f64, seed: u64) -> QtResult<Vec<ShadowCurve>> {
        let mut c = self.inner.simulate(theta, seed)?;
        for (c, t) in c.iter_mut().zip(&self.truth) {
            c.truth_interval = t.0;
            c.censored_from = t.1;
        }
        Ok(c)
    }
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the zoo particle simulator")]
fn o_cal_4_survival_recovers_k0_real_simulator() {
    let (root, ds, sh) = fixture("ocal4");
    let mut s = spec("survival", "databento", &ds, &sh, &root.join("out"));
    s.particles = 100;
    let inner = qt_zoo::particle_simulator(&s).unwrap_or_else(|e| panic!("{e}"));
    let base = inner.simulate(-0.8, 101).unwrap_or_else(|e| panic!("{e}"));
    let mut rng = Xoshiro256StarStar::new(stable_seed(0, &[b"o_cal_4_truth"]));
    let truth = base
        .iter()
        .map(|c| {
            let v = rng.next_f64();
            let fill = c.survival.iter().position(|s| v < 1.0 - s);
            match (fill, c.censored_from) {
                (Some(j), Some(cf)) if j >= cf => (None, Some(cf)),
                (f, cf) => (f, if f.is_some() { None } else { cf }),
            }
        })
        .collect();
    let sim = DrawnTruth { inner, truth };
    let thetas = optim::grid_points(-0.98, 0.9, 25);
    let step = thetas[1] - thetas[0];
    let fit = fit_survival(&sim, &thetas, 202, false).unwrap_or_else(|e| panic!("{e}"));
    assert!(
        (fit.estimate + 0.8).abs() <= step + 1e-12,
        "k_hat = {} grid {:?}",
        fit.estimate,
        fit.grid
    );
    let _ = std::fs::remove_dir_all(&root);
}

fn with_windows(mut s: CalibrationSpec, train: serde_json::Value, test: serde_json::Value) -> CalibrationSpec {
    s.train = serde_json::from_value(train).unwrap_or_else(|e| panic!("{e}"));
    s.test = serde_json::from_value(test).unwrap_or_else(|e| panic!("{e}"));
    s
}

/// Trader review D1: a train window whose date is not in the data used to fit on zero events (n at the grid edge,
/// exported as paste-ready). Now QT-DATA-001 naming the available dates; an empty given test window and an
/// unconflated-only NT touch fit (review M5) are refused too.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the zoo nt_queue_l2 tracker")]
fn calibrate_refuses_empty_windows_and_conflated_touch_fits() {
    let (root, ds, sh) = fixture("failsafe");
    let base = spec("alloc", "power3", &ds, &sh, &root.join("x"));
    let missing = with_windows(
        base.clone(),
        serde_json::json!({"dates": ["2026-06-17"]}),
        serde_json::json!({}),
    );
    let e = calibrate(&missing)
        .err()
        .unwrap_or_else(|| panic!("fit on a date without data"));
    assert_eq!(e.code(), "QT-DATA-001", "{e}");
    let msg = e.to_string();
    assert!(
        msg.contains("2026-06-17") && msg.contains("available: 2026-06-15"),
        "{msg}"
    );
    let missing_test = with_windows(
        base.clone(),
        serde_json::json!({}),
        serde_json::json!({"dates": ["2026-06-16"]}),
    );
    assert!(calibrate(&missing_test).is_err_and(|e| e.code() == "QT-DATA-001"));
    // All shadows sit in the first hour of the session: block 20 of 24 holds none.
    let empty_test = with_windows(
        base.clone(),
        serde_json::json!({}),
        serde_json::json!({"blocks": {"index": [20], "n_blocks": 24}}),
    );
    let e = calibrate(&empty_test)
        .err()
        .unwrap_or_else(|| panic!("empty test window accepted"));
    assert_eq!(e.code(), "QT-DATA-001");
    assert!(e.to_string().contains("test window holds no shadow"), "{e}");
    let empty_train = with_windows(
        base,
        serde_json::json!({"blocks": {"index": [20], "n_blocks": 24}}),
        serde_json::json!({}),
    );
    assert!(calibrate(&empty_train).is_err_and(|e| e.code() == "QT-DATA-001" && e.to_string().contains("train")));
    let mut conflated = spec("nt_touch", "nt_prob_touch", &ds, &sh, &root.join("nt"));
    conflated.l2.cadence_ns = 100_000_000;
    let e = calibrate(&conflated)
        .err()
        .unwrap_or_else(|| panic!("conflated nt_touch accepted"));
    assert_eq!(e.code(), "QT-CONFIG-001");
    assert!(e.to_string().contains("l2.cadence_ns"), "{e}");
    let _ = std::fs::remove_dir_all(&root);
}

/// Trust lines: every `trust:` warning carries a known token, the grid-edge flag matches the estimate, and a
/// zero-width bootstrap interval is never published.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the zoo crate")]
fn calibrate_writes_consistent_trust_lines() {
    let (root, ds, sh) = fixture("trust");
    let known = [
        "at_grid_edge",
        "plateau",
        "flat_likelihood",
        "not_identified",
        "profile_ci_open",
        "degenerate_ci",
        "nonfinite",
        "heterogeneous_strata",
    ];
    for (kind, fam) in [
        ("alloc", "power3"),
        ("alloc", "power"),
        ("alloc", "databento"),
        ("nt_touch", "nt_prob_touch"),
    ] {
        let r = calibrate(&spec(kind, fam, &ds, &sh, &root.join(fam))).unwrap_or_else(|e| panic!("{fam}: {e}"));
        for w in r.warnings.iter().filter(|w| w.starts_with("trust: ")) {
            let tok = w["trust: ".len()..].split(':').next().unwrap_or("");
            assert!(known.contains(&tok), "{fam}: unknown trust token in {w}");
        }
        let t = r.trust.clone().unwrap_or_else(|| panic!("{fam}: no trust field"));
        for f in &t.flags {
            assert!(known.contains(&f.as_str()), "{fam}: unknown flag {f}");
            assert!(r.warnings.iter().any(|w| w.starts_with(&format!("trust: {f}:"))));
        }
        let refusing: Vec<&str> = qt_calib::trust::REFUSING
            .iter()
            .copied()
            .filter(|x| t.reasons.iter().any(|r| r == x))
            .collect();
        assert_eq!(t.reasons, refusing, "{fam}: reasons out of order");
        if let Some((Some(lo), Some(hi))) = r.ci_bootstrap {
            assert!(lo < hi, "{fam}: zero-width bootstrap CI published");
        }
        let edge = r.warnings.iter().any(|w| w.starts_with("trust: at_grid_edge"));
        if kind == "alloc" {
            let g = if fam == "databento" {
                (-0.99, 0.99)
            } else {
                (-3.0f64.exp(), 4.0f64.exp())
            };
            let est = r.estimate.unwrap_or(f64::NAN);
            let near = |a: f64| (est - a).abs() <= 1e-9 * a.abs().max(1.0);
            if near(g.0) || near(g.1) {
                assert!(edge, "{fam}: estimate {est} at the grid end without at_grid_edge");
            }
        }
    }
    let _ = std::fs::remove_dir_all(&root);
}
