//! Unit tests of the zoo: O-ZOO-1 (golden values, extra hbt rows, NT golden), real-hftbacktest replay parity
//! (golden vectors recorded from hftbacktest), O-ZOO-2 (transliteration parity fixtures), O-ZOO-5 (Databento hand trace and
//! determinism), the `ModelId` grammar, and L2-arm alignment / L2-only mode on hand-built row sequences.

use std::collections::BTreeMap;
use std::path::PathBuf;

use qt_schema::rows::{PriceSpec, ZooFillReason};
use qt_types::{BookSide, Side, Ts, Xoshiro256StarStar};
use serde_json::Value;

#[allow(unused_imports)]
use crate::QueueModel as _;
use crate::models::{HbtModel, QUIRK_HBT_NAN_RESET, nt_depth_step, nt_trade_step};
use crate::pair::{ModelSlot, Op, Pair, PairEnd};
use crate::sim::{PartitionSim, Row, RowKind, ShadowInput, SimConfig};
use crate::{Exchange, FillDelta, Mode, ModelId, QState, ShadowL2};

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/zoo")
}

fn read_json(p: PathBuf) -> Value {
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", p.display()))
}

fn mid(s: &str) -> ModelId {
    s.parse().unwrap_or_else(|e| panic!("{s}: {e}"))
}

fn bits_eq(a: f64, b: f64) -> bool {
    a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan())
}

// ---- ModelId grammar --------------------------------------------------------------------------------------------

#[test]
fn model_id_round_trip_and_canonical_form() {
    let cases = [
        ("hbt_power3[n=2]", "hbt_power3[exch=partial;mode=faithful;n=2]"),
        (
            "hbt_power3[mode=corrected;n=2;exch=no_partial]",
            "hbt_power3[exch=no_partial;mode=corrected;n=2]",
        ),
        (
            "databento_mc[k=-0.8]",
            "databento_mc[k=-0.8;mode=faithful;particles=100]",
        ),
        ("nt_prob_touch", "nt_prob_touch[mode=faithful;p=1]"),
        (
            "nt_queue_l2_prob_touch[p=0.25]",
            "nt_queue_l2_prob_touch[mode=faithful;p=0.25]",
        ),
        ("hbt_l3_fifo", "hbt_l3_fifo[mode=faithful]"),
        ("hbt_risk_adverse", "hbt_risk_adverse[exch=partial;mode=faithful]"),
        ("hbt_power[n=1.5]", "hbt_power[exch=partial;mode=faithful;n=1.5]"),
        (
            "hbt_power[n=0.0000001]",
            "hbt_power[exch=partial;mode=faithful;n=0.0000001]",
        ),
        ("front_of_queue", "front_of_queue[mode=faithful]"),
        (
            "trade_through_only[mode=corrected]",
            "trade_through_only[mode=corrected]",
        ),
    ];
    for (input, canon) in cases {
        let id = mid(input);
        assert_eq!(id.to_string(), canon, "{input}");
        let back = mid(canon);
        assert_eq!(back, id, "{canon}");
        assert_eq!(back.to_string(), canon);
    }
    for bad in [
        "hbt_power",
        "hbt_power[n=0]",
        "nope",
        "hbt_power3[n=2;q=1]",
        "databento_mc[k=1]",
        "nt_prob_touch[p=2]",
        "nt_queue_l2[exch=partial]",
        "hbt_power3[n=2",
        "databento_mc[k=-0.5;particles=1.5]",
    ] {
        assert!(bad.parse::<ModelId>().is_err(), "{bad} should not parse");
    }
    let d = ModelId::parse_with("hbt_log2", Mode::Corrected, Exchange::NoPartial, 7).unwrap();
    assert_eq!(d.to_string(), "hbt_log2[exch=no_partial;mode=corrected]");
    let d = ModelId::parse_with("databento_mc[k=-0.95]", Mode::Corrected, Exchange::NoPartial, 7).unwrap();
    assert_eq!(d.to_string(), "databento_mc[k=-0.95;mode=corrected;particles=7]");
}

#[test]
fn default_models_is_the_all_set() {
    let all = crate::default_models(Mode::Faithful, Exchange::Partial);
    let s: Vec<String> = all.iter().map(ToString::to_string).collect();
    assert_eq!(s.len(), 16);
    assert!(
        !s.contains(&"hbt_power[exch=partial;mode=faithful;n=1]".to_owned()),
        "duplicate of power3 n=1"
    );
    assert_eq!(s[0], "hbt_risk_adverse[exch=partial;mode=faithful]");
    assert!(s.contains(&"hbt_power3[exch=partial;mode=faithful;n=2]".to_owned()));
    assert!(s.contains(&"databento_mc[k=-0.95;mode=faithful;particles=100]".to_owned()));
    assert!(s.contains(&"nt_prob_touch[mode=faithful;p=1]".to_owned()));
    for id in &all {
        assert_eq!(&mid(&id.to_string()), id);
        assert!(crate::build(id).is_ok());
    }
    let c = crate::default_models(Mode::Corrected, Exchange::NoPartial);
    assert_eq!(c[1].to_string(), "hbt_power[exch=no_partial;mode=corrected;n=2]");
    assert_eq!(c[10].to_string(), "nt_queue_l2[mode=corrected]");
    let e = crate::expand_models(
        &["all".into(), "hbt_power3[n=2]".into()],
        Mode::Faithful,
        Exchange::Partial,
        50,
    )
    .unwrap();
    assert_eq!(e.len(), 16);
    assert!(
        e.iter()
            .any(|m| m.to_string() == "databento_mc[k=-0.8;mode=faithful;particles=50]")
    );
}

#[test]
fn unknown_family_suggests_the_closest_name() {
    use crate::Family;
    assert_eq!(Family::suggest("hbt_powr3"), Some("hbt_power3"));
    assert_eq!(Family::suggest("power3"), Some("hbt_power3"));
    assert_eq!(Family::suggest("nt_prob_tuch"), Some("nt_prob_touch"));
    assert_eq!(Family::suggest("databento"), Some("databento_mc"));
    assert_eq!(Family::suggest("zzzzzz"), None);
    let e = ModelId::parse_with("hbt_powr3[n=2]", Mode::Faithful, Exchange::Partial, 100)
        .err()
        .map(|e| e.to_string())
        .unwrap_or_default();
    assert!(e.contains("did you mean \"hbt_power3\"?"), "{e}");
    assert!(e.contains("--list-models"), "{e}");
    for f in Family::ALL {
        assert!(!f.summary().is_empty());
    }
}

// ---- O-ZOO-1 ------------------------------------------------------------------------------------------------------

fn hbt_depth(id: &str, f: f64, t: f64, prev: f64, new: f64) -> (f64, u8) {
    let m = HbtModel::new(mid(id));
    let mut s = QState {
        front: f,
        cum_trade: t,
        ..QState::default()
    };
    m.depth_update(&mut s, prev, new);
    (s.front, s.quirk)
}

/// O-ZOO-1 (full): every row of the SPEC table through the model's depth update, the extra rows computed by
/// hbt's own code (`golden_ozoo1_rust.json`), the exec-lots list through the model's trade path, and the NT
/// golden of 5.3.3(d).
#[test]
fn o_zoo_1_golden_full() {
    let exact = cfg!(all(target_os = "macos", target_arch = "aarch64"));
    let g = read_json(fixtures().join("hbt_real/golden_ozoo1_rust.json"));
    let rows = g["prob_rows"].as_array().unwrap();
    assert!(rows.len() >= 9);
    let re = |case: &str| -> String {
        let (fam, rest) = case.split_once(' ').unwrap_or((case, ""));
        let fam = fam.trim_end_matches(',');
        let n = rest
            .split_whitespace()
            .find_map(|w| w.trim_end_matches(',').strip_prefix("n="))
            .map(str::to_owned);
        match n {
            Some(n) => format!("hbt_{fam}[n={n}]"),
            None => format!("hbt_{fam}"),
        }
    };
    let mut checked = 0;
    for r in rows {
        let case = r["case"].as_str().unwrap();
        let num = |k: &str| -> f64 {
            let b = r[format!("{k}_bits")].as_str().map(|s| s.parse::<u64>().unwrap());
            match b {
                Some(b) => f64::from_bits(b),
                None => r[k].as_f64().unwrap(),
            }
        };
        let id = re(case);
        let (f2, quirk) = hbt_depth(&id, num("F"), num("T"), num("prev"), num("new"));
        let want = f64::from_bits(r["F_after_bits"].as_str().unwrap().parse::<u64>().unwrap());
        if exact {
            assert!(bits_eq(f2, want), "{case} ({id}): {f2:?} != {want:?}");
        } else {
            assert!((f2 - want).abs() <= 4.0 * f64::EPSILON * want.abs().max(1.0), "{case}");
        }
        if r["pi_is_nan"].as_bool() == Some(true) {
            assert_eq!(quirk & QUIRK_HBT_NAN_RESET, QUIRK_HBT_NAN_RESET, "{case}: NaN quirk");
        }
        checked += 1;
    }
    assert!(checked >= 9);
    // exec lots through the trade path of the faithful model (F arrives, a trade of 0 triggers exec)
    for e in g["exec_lots"].as_array().unwrap() {
        let f = f64::from_bits(e["F_bits"].as_str().unwrap().parse().unwrap());
        let lot = e["L"].as_f64().unwrap();
        let want = e["fill_qty"].as_f64().unwrap();
        let m = crate::build(&mid("hbt_risk_adverse[exch=partial]")).unwrap();
        let o = ShadowL2 {
            idx: 0,
            side: BookSide::Bid,
            price: 100,
            qty: 100.0,
            lot,
        };
        let mut s = QState {
            front: f,
            leaves: 100.0,
            ..QState::default()
        };
        let mut rng = Xoshiro256StarStar::new(0);
        let d = m.on_trade(&mut s, &o, 0, 0.0, Side::Ask, &mut rng);
        let got = match d {
            FillDelta::None => 0.0,
            FillDelta::Qty(q) => q,
            FillDelta::Full => 100.0,
        };
        assert_eq!(got.to_bits(), want.to_bits(), "exec lots F={f} L={lot}");
    }
    // NT golden (corrected): F = 100; trade 60 -> 40, no fill; depth new 30 -> 30; trade 45 with leaves 10 -> F 0,
    // fill 10. Faithful (NautilusTrader 1.231.0): the depth row leaves F at 40, so the trade fills 5.
    let mut f = 100.0;
    assert_eq!(nt_trade_step(&mut f, 60.0, 10.0), 0.0);
    assert_eq!(f, 40.0);
    let mut g = f;
    nt_depth_step(&mut f, 30.0, 0, Mode::Corrected);
    assert_eq!(f, 30.0);
    assert_eq!(nt_trade_step(&mut f, 45.0, 10.0), 10.0);
    assert_eq!(f, 0.0);
    nt_depth_step(&mut g, 30.0, 0, Mode::Faithful);
    assert_eq!(g, 40.0);
    assert_eq!(nt_trade_step(&mut g, 45.0, 10.0), 5.0);
    assert_eq!(g, 0.0);
    // the same through the model
    let m = crate::build(&mid("nt_queue_l2")).unwrap();
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 10.0,
        lot: 1.0,
    };
    let mut s = QState {
        leaves: 10.0,
        ..QState::default()
    };
    let mut rng = Xoshiro256StarStar::new(0);
    m.on_arrive(&mut s, &o, 100.0, &mut rng);
    assert_eq!(m.on_trade(&mut s, &o, 1, 60.0, Side::Ask, &mut rng), FillDelta::None);
    m.on_depth(&mut s, &o, 2, 40.0, 30.0, 0, &mut rng);
    assert_eq!(s.front, 40.0, "faithful NT never shrinks the queue on a size update");
    assert_eq!(
        m.on_trade(&mut s, &o, 3, 45.0, Side::Ask, &mut rng),
        FillDelta::Qty(5.0)
    );
    assert_eq!(s.front, 0.0);
}

#[test]
fn corrected_hbt_updates() {
    // corrected RiskAdverse depth clamps negative F; exec needs volume strictly beyond the queue
    let m = crate::build(&mid("hbt_risk_adverse[mode=corrected]")).unwrap();
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 2.0,
        lot: 1.0,
    };
    let mut s = QState {
        leaves: 2.0,
        ..QState::default()
    };
    let mut rng = Xoshiro256StarStar::new(0);
    m.on_arrive(&mut s, &o, 10.0, &mut rng);
    assert_eq!(m.on_trade(&mut s, &o, 1, 10.0, Side::Ask, &mut rng), FillDelta::None);
    assert_eq!(m.on_trade(&mut s, &o, 1, 0.5, Side::Ask, &mut rng), FillDelta::None);
    assert_eq!(m.on_trade(&mut s, &o, 1, 0.5, Side::Ask, &mut rng), FillDelta::Qty(1.0));
    assert_eq!(m.on_trade(&mut s, &o, 1, 5.0, Side::Ask, &mut rng), FillDelta::Full);
    // corrected prob: negative F never produces NaN, pi in [0, 1]
    let (f, q) = hbt_depth_mode("hbt_power[n=1.5;mode=corrected]", -0.4, 0.0, 50.0, 40.0);
    assert_eq!(q, 0);
    assert_eq!(f, 0.0);
    let (f, _) = hbt_depth_mode("hbt_power3[n=3;mode=corrected]", 90.0, 0.0, 100.0, 40.0);
    assert!((0.0..=40.0).contains(&f));
}

fn hbt_depth_mode(id: &str, f: f64, t: f64, prev: f64, new: f64) -> (f64, u8) {
    hbt_depth(id, f, t, prev, new)
}

// ---- real hftbacktest replay (recorded golden vectors) ----------------------------------------------------------------

struct Scenario {
    id: String,
    lot: f64,
    rows: Vec<(Ts, Row)>,
    shadows: Vec<(u64, ShadowInput)>,
    /// timeline step -> ts
    steps: Vec<Ts>,
}

fn side_of(s: &str) -> Side {
    match s {
        "B" => Side::Bid,
        "A" => Side::Ask,
        _ => Side::None,
    }
}

fn load_scenarios() -> BTreeMap<String, Scenario> {
    let doc = read_json(fixtures().join("hbt_real/scenarios.json"));
    let mut out = BTreeMap::new();
    for sc in doc["scenarios"].as_array().unwrap() {
        let tick = sc["tick_size"].as_f64().unwrap();
        let lot = sc["lot_size"].as_f64().unwrap();
        let px = |v: &Value| (v.as_f64().unwrap() / tick).round() as i64;
        let mut rows = Vec::new();
        for e in sc["events"].as_array().unwrap() {
            let ts = e["ts"].as_i64().unwrap();
            let kind = e["kind"].as_str().unwrap();
            let row = match kind {
                "depth" | "snapshot" => Row {
                    ts,
                    kind: if kind == "depth" {
                        RowKind::Depth
                    } else {
                        RowKind::Snapshot
                    },
                    side: side_of(e["side"].as_str().unwrap()),
                    price: px(&e["px"]),
                    qty: e["qty"].as_f64().unwrap(),
                    aggressor: Side::None,
                    flags: 0,
                    clear_side: None,
                    clear_upto: None,
                },
                "trade" => {
                    let agg = side_of(e["aggressor"].as_str().unwrap());
                    Row {
                        ts,
                        kind: RowKind::Trade,
                        side: agg.opposite(),
                        price: px(&e["px"]),
                        qty: e["qty"].as_f64().unwrap(),
                        aggressor: agg,
                        flags: 0,
                        clear_side: None,
                        clear_upto: None,
                    }
                }
                "clear" => Row {
                    ts,
                    kind: RowKind::Clear,
                    side: Side::None,
                    price: 0,
                    qty: 0.0,
                    aggressor: Side::None,
                    flags: 0,
                    clear_side: BookSide::from_side(side_of(e["side"].as_str().unwrap_or("N"))),
                    clear_upto: e.get("px").and_then(Value::as_f64).map(|p| (p / tick).round() as i64),
                },
                other => panic!("kind {other}"),
            };
            rows.push((ts, row));
        }
        let mut shadows: Vec<(u64, ShadowInput)> = Vec::new();
        let mut cancels: BTreeMap<u64, Ts> = BTreeMap::new();
        let mut action_ts = Vec::new();
        for a in sc["actions"].as_array().unwrap() {
            let ts = a["ts"].as_i64().unwrap();
            action_ts.push(ts);
            let oid = a["order_id"].as_u64().unwrap();
            if a["kind"] == "cancel" {
                cancels.insert(oid, ts);
                continue;
            }
            let side = BookSide::from_side(side_of(a["side"].as_str().unwrap())).unwrap();
            shadows.push((
                oid,
                ShadowInput {
                    shadow_id: oid.to_string(),
                    instrument_id: 1,
                    side,
                    price: PriceSpec::Abs(px(&a["px"])),
                    qty: 0,
                    ts_live: ts,
                    cancel_at: None,
                    amends: Vec::new(),
                },
            ));
        }
        for (oid, s) in &mut shadows {
            s.cancel_at = cancels.get(oid).copied();
        }
        let mut steps: Vec<(Ts, u8)> = rows
            .iter()
            .map(|r| (r.0, 0))
            .chain(action_ts.iter().map(|t| (*t, 1)))
            .collect();
        steps.sort();
        out.insert(
            sc["id"].as_str().unwrap().to_owned(),
            Scenario {
                id: sc["id"].as_str().unwrap().to_owned(),
                lot,
                rows,
                shadows,
                steps: steps.into_iter().map(|s| s.0).collect(),
            },
        );
        // quantities are f64 in these scenarios; kept separately
        let qtys: Vec<f64> = sc["actions"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|a| a["kind"] == "submit")
            .map(|a| a["qty"].as_f64().unwrap())
            .collect();
        QTYS.with(|q| q.borrow_mut().insert(sc["id"].as_str().unwrap().to_owned(), qtys));
    }
    out
}

thread_local! {
    static QTYS: std::cell::RefCell<BTreeMap<String, Vec<f64>>> = const { std::cell::RefCell::new(BTreeMap::new()) };
}

/// Replays one scenario with one model through the L2 arm, shadow quantities as f64 (pairs are driven directly so
/// fractional order sizes are kept).
fn replay_scenario(sc: &Scenario, model: &ModelId) -> (Vec<Pair>, Vec<u64>) {
    let qtys = QTYS.with(|q| q.borrow().get(&sc.id).cloned().unwrap_or_default());
    let cfg = SimConfig {
        horizons: vec![1_000_000_000_000],
        h_max: 1_000_000_000_000,
        tick: 1,
        lot: sc.lot,
        cadence0: true,
        seed: 0,
        write_trajectory: false,
        trace: true,
    };
    let shadows: Vec<ShadowInput> = sc.shadows.iter().map(|s| s.1.clone()).collect();
    let mut sim = PartitionSim::new(cfg, std::slice::from_ref(model), shadows).unwrap();
    sim.set_qty_override(qtys);
    for (_, r) in &sc.rows {
        sim.on_batch(std::slice::from_ref(r));
    }
    let pairs = (0..sc.shadows.len())
        .map(|i| sim.pair(i as u32, 0).unwrap().clone())
        .collect();
    (pairs, sc.shadows.iter().map(|s| s.0).collect())
}

/// Real hftbacktest (5f3ec40 Rust build, == 2.4.4 wheel) on 83 scenarios x 18 models x 2 exchanges: our faithful
/// L2 arm reproduces every fill (time, bit-exact quantity), every GTX expiry, the run-fatal #312 error step, and the
/// queue state after every hook (bit-exact F and T).
#[test]
fn o_zoo_2_real_hftbacktest_parity() {
    let scs = load_scenarios();
    let runs = read_json(fixtures().join("hbt_real/runs.json"));
    let mut n_runs = 0;
    let mut n_states = 0;
    let mut n_fills = 0;
    let mut n_err = 0;
    for r in runs["runs"].as_array().unwrap() {
        let sc = &scs[r["scenario"].as_str().unwrap()];
        let model = mid(r["model_id"].as_str().unwrap());
        let (pairs, oids) = replay_scenario(sc, &model);
        let err_step = r["error"].as_array().map(|e| e[0].as_u64().unwrap() as usize);
        let ctx = format!("{} {}", sc.id, model);
        // error: the earliest hbt_error among our pairs is hbt's run-fatal error
        let ours_err = pairs.iter().filter_map(|p| p.hbt_error).min();
        match err_step {
            Some(st) => {
                n_err += 1;
                assert_eq!(ours_err, Some(sc.steps[st]), "{ctx}: error step");
            }
            None => assert_eq!(ours_err, None, "{ctx}: unexpected hbt error"),
        }
        let cut = err_step.map(|s| sc.steps[s]).unwrap_or(Ts::MAX);
        // fills
        let mut ours: Vec<(Ts, u64, u64)> = Vec::new();
        for (p, oid) in pairs.iter().zip(&oids) {
            for f in &p.fills {
                if f.ts < cut {
                    ours.push((f.ts, *oid, f.qty.to_bits()));
                }
            }
        }
        ours.sort();
        let mut theirs: Vec<(Ts, u64, u64)> = r["fills"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| (f[2].as_i64().unwrap(), f[1].as_u64().unwrap(), f[3].as_u64().unwrap()))
            .collect();
        theirs.sort();
        assert_eq!(ours, theirs, "{ctx}: fills");
        n_fills += theirs.len();
        // expiries
        for e in r["events"].as_array().unwrap() {
            if e[2] == "expired" {
                let oid = e[1].as_u64().unwrap();
                let k = oids.iter().position(|o| *o == oid).unwrap();
                assert_eq!(pairs[k].end, Some(PairEnd::Expired), "{ctx}: order {oid} expiry");
            }
        }
        // queue states
        for s in r["states"].as_array().unwrap() {
            let step = s[0].as_u64().unwrap() as usize;
            let ts = sc.steps[step];
            let oid = s[1].as_u64().unwrap();
            let k = oids.iter().position(|o| *o == oid).unwrap();
            let tr = pairs[k].trace.as_ref().unwrap();
            let last = tr.iter().rev().find(|t| t.0 == ts);
            let Some(&(_, f, t)) = last else {
                panic!("{ctx}: no state for order {oid} at step {step} (ts {ts})");
            };
            let fb = s[2].as_u64().unwrap();
            assert_eq!(
                f.to_bits(),
                fb,
                "{ctx}: F order {oid} step {step}: {f:?} vs {:?}",
                f64::from_bits(fb)
            );
            if let Some(tb) = s[3].as_u64() {
                assert_eq!(t.to_bits(), tb, "{ctx}: T order {oid} step {step}");
            }
            n_states += 1;
        }
        n_runs += 1;
    }
    assert_eq!(n_runs, 2988);
    assert!(n_states > 20_000, "{n_states}");
    assert!(n_fills > 1_000, "{n_fills}");
    assert!(n_err > 100, "{n_err}");
}

// ---- pair-level behaviour ------------------------------------------------------------------------------------

fn slot(id: &str) -> ModelSlot {
    ModelSlot::new(crate::build(&mid(id)).unwrap())
}

fn arrive(q: f64) -> Op {
    Op::Arrive {
        ts: 0,
        q_view: q,
        q_hbt: q,
        cross_view: false,
        cross_hbt: false,
        opp_view: 0.0,
    }
}

fn trade(ts: Ts, v: f64) -> Op {
    Op::Trade {
        ts,
        qty: v,
        aggressor: Side::Ask,
        hidden: false,
    }
}

#[test]
fn partial_exchange_zombie_and_run_fatal_error() {
    let sl = slot("hbt_risk_adverse[exch=partial]");
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 1.0,
        lot: 1.0,
    };
    let mut p = Pair::new(&sl, 1.0, Xoshiro256StarStar::new(0));
    p.apply(&sl, &o, &arrive(2.0));
    p.apply(&sl, &o, &trade(1, 3.0));
    assert_eq!(p.fills.len(), 1);
    assert_eq!(p.fills[0].quirk, qt_schema::rows::Quirk::Hbt312);
    assert!(p.zombie && p.done && !p.finished());
    p.apply(&sl, &o, &trade(2, 0.2));
    assert!(p.hbt_error.is_none());
    p.apply(&sl, &o, &trade(3, 1.0));
    assert_eq!(p.hbt_error, Some(3));
    assert_eq!(p.end, Some(PairEnd::HbtError));
    // corrected: no zombie
    let sl = slot("hbt_risk_adverse[exch=partial;mode=corrected]");
    let mut p = Pair::new(&sl, 1.0, Xoshiro256StarStar::new(0));
    p.apply(&sl, &o, &arrive(2.0));
    p.apply(&sl, &o, &trade(1, 3.0));
    assert_eq!(p.end, Some(PairEnd::Filled));
    assert!(!p.zombie);
}

#[test]
fn corrected_cross_fills_as_taker_against_opposite_size() {
    let sl = slot("hbt_power3[n=2;mode=corrected]");
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 10.0,
        lot: 1.0,
    };
    let mut p = Pair::new(&sl, 10.0, Xoshiro256StarStar::new(0));
    p.apply(
        &sl,
        &o,
        &Op::Arrive {
            ts: 5,
            q_view: 3.0,
            q_hbt: 3.0,
            cross_view: true,
            cross_hbt: true,
            opp_view: 4.0,
        },
    );
    assert_eq!(p.fills.len(), 1);
    assert_eq!(p.fills[0].qty, 4.0);
    assert_eq!(p.fills[0].reason, ZooFillReason::CrossedAtLive);
    // the same displayed size is never taken twice (A5, review M6): the 4 lots it took are still displayed (or 2 of
    // them left), so nothing more is available
    for opp in [4.0, 4.0, 2.0] {
        p.apply(
            &sl,
            &o,
            &Op::BatchEnd {
                ts: 6,
                cross_view: true,
                opp_view: opp,
                cadence0: true,
            },
        );
    }
    assert_eq!(p.fills.len(), 1);
    // new opposite size beyond what is still displayed of the taken lots (2) is available: 7 - 2 = 5
    p.apply(
        &sl,
        &o,
        &Op::BatchEnd {
            ts: 7,
            cross_view: true,
            opp_view: 7.0,
            cadence0: true,
        },
    );
    assert_eq!(p.fills[1].qty, 5.0);
    assert_eq!(p.fills[1].reason, ZooFillReason::Crossed);
    let total: f64 = p.fills.iter().map(|f| f.qty).sum();
    assert_eq!(total, 9.0);
    // review M6 reproducer: a risk-adverse bid of 10 against 4 displayed lots for several batches takes 4
    let sl = slot("hbt_risk_adverse[mode=corrected]");
    let o101 = ShadowL2 { price: 101, ..o };
    let mut p = Pair::new(&sl, 10.0, Xoshiro256StarStar::new(1));
    p.apply(
        &sl,
        &o101,
        &Op::Arrive {
            ts: 5,
            q_view: 0.0,
            q_hbt: 0.0,
            cross_view: true,
            cross_hbt: true,
            opp_view: 4.0,
        },
    );
    for ts in [5, 10, 15] {
        p.apply(
            &sl,
            &o101,
            &Op::BatchEnd {
                ts,
                cross_view: true,
                opp_view: 4.0,
                cadence0: true,
            },
        );
    }
    let total: f64 = p.fills.iter().map(|f| f.qty).sum();
    assert_eq!(total, 4.0, "{:?}", p.fills);
    // faithful: GTX expiry
    let sl = slot("hbt_power3[n=2]");
    let mut p = Pair::new(&sl, 10.0, Xoshiro256StarStar::new(0));
    p.apply(
        &sl,
        &o,
        &Op::Arrive {
            ts: 5,
            q_view: 3.0,
            q_hbt: 3.0,
            cross_view: true,
            cross_hbt: true,
            opp_view: 4.0,
        },
    );
    assert_eq!(p.end, Some(PairEnd::Expired));
    assert!(p.fills.is_empty());
    // faithful NT: full crossed_at_live
    let sl = slot("nt_queue_l2");
    let mut p = Pair::new(&sl, 10.0, Xoshiro256StarStar::new(0));
    p.apply(
        &sl,
        &o,
        &Op::Arrive {
            ts: 5,
            q_view: 3.0,
            q_hbt: 3.0,
            cross_view: true,
            cross_hbt: true,
            opp_view: 4.0,
        },
    );
    assert_eq!(p.fills[0].qty, 10.0);
}

#[test]
fn clear_faithful_hbt_keeps_order_others_censor() {
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 1.0,
        lot: 1.0,
    };
    for (id, keeps) in [
        ("hbt_power3[n=2]", true),
        ("hbt_power3[n=2;mode=corrected]", false),
        ("nt_queue_l2", false),
    ] {
        let sl = slot(id);
        let mut p = Pair::new(&sl, 1.0, Xoshiro256StarStar::new(0));
        p.apply(&sl, &o, &arrive(5.0));
        p.apply(&sl, &o, &Op::Clear { ts: 1 });
        assert_eq!(p.finished(), !keeps, "{id}");
        if !keeps {
            assert_eq!(p.end, Some(PairEnd::CensoredClear));
        }
    }
}

#[test]
fn nt_touch_models_draw_and_analytic_tracker() {
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 2.0,
        lot: 1.0,
    };
    // p = 1: first touch fills min(leaves, v)
    let sl = slot("nt_prob_touch");
    let mut p = Pair::new(&sl, 2.0, Xoshiro256StarStar::new(0));
    p.apply(&sl, &o, &arrive(50.0));
    p.apply(&sl, &o, &trade(10, 1.0));
    assert_eq!(p.fills.len(), 1);
    assert_eq!(p.fills[0].reason, ZooFillReason::TouchDraw);
    p.apply(&sl, &o, &trade(20, 5.0));
    assert_eq!(p.st.filled, 2.0);
    assert!(!p.finished(), "tracker keeps counting touches after the simulated fill");
    p.apply(&sl, &o, &trade(30, 5.0));
    assert_eq!(p.tracker.as_ref().unwrap().touches, vec![10, 20, 30]);
    // queue-gated: touches count only with excess > 0 on the deterministic F path
    let sl = slot("nt_queue_l2_prob_touch[p=0.5]");
    let mut p = Pair::new(&sl, 2.0, Xoshiro256StarStar::new(0));
    p.apply(&sl, &o, &arrive(5.0));
    p.apply(&sl, &o, &trade(10, 3.0));
    p.apply(&sl, &o, &trade(20, 3.0));
    p.apply(&sl, &o, &trade(30, 3.0));
    assert_eq!(p.tracker.as_ref().unwrap().touches, vec![20, 30]);
    assert_eq!(p.st.touches, 3);
}

/// O-ZOO-5: `databento_mc` with one particle, k = -0.8, on a hand-traced 5-row sequence with the fixed RNG stream
/// `Xoshiro256StarStar::new(stable_seed(0, ["databento_mc[k=-0.8;mode=faithful;particles=1]", "s1"]))`, whose first
/// two `next_f64` draws are u1 = 0.7634727689040633 and u2 = 0.6653113668059256 (r = u^0.2 = 0.94746, 0.92173).
/// Arrive Q = 50 -> F 50. Row 1: depth 50 -> 100 (increase: clamp only, no draw) -> 50. Row 2: 100 -> 80, x =
/// 50/80 = 0.625, r1 = 0.947 >= x -> stays 50. Row 3: 80 -> 60, x = 0.833, r2 = 0.922 >= x -> stays 50. Row 4:
/// 60 -> 0 (deletion) -> 0. Row 5: at-price trade of 1 -> fills floor(1 - 0) = 1 lot at ts 5.
#[test]
fn o_zoo_5_databento_hand_trace() {
    let sl = slot("databento_mc[k=-0.8;particles=1]");
    assert_eq!(sl.id_str, "databento_mc[k=-0.8;mode=faithful;particles=1]");
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 1.0,
        lot: 1.0,
    };
    let seed = qt_types::stable_seed(0, &[sl.id_str.as_bytes(), b"s1"]);
    let mut probe = Xoshiro256StarStar::new(seed);
    assert_eq!(probe.next_f64(), 0.7634727689040633);
    assert_eq!(probe.next_f64(), 0.6653113668059256);
    let mut p = Pair::new(&sl, 1.0, Xoshiro256StarStar::new(seed));
    let dep = |ts: Ts, prev: f64, new: f64| Op::Depth {
        ts,
        prev_view: prev,
        prev_hbt: prev,
        new,
        flags: 0,
    };
    p.apply(&sl, &o, &arrive(50.0));
    let mut positions = vec![p.st.particles[0].front];
    for op in [
        dep(1, 50.0, 100.0),
        dep(2, 100.0, 80.0),
        dep(3, 80.0, 60.0),
        dep(4, 60.0, 0.0),
    ] {
        p.apply(&sl, &o, &op);
        positions.push(p.st.particles[0].front);
    }
    assert_eq!(positions, vec![50.0, 50.0, 50.0, 50.0, 0.0]);
    p.apply(&sl, &o, &trade(5, 1.0));
    assert_eq!(p.st.particles[0].filled, 1.0);
    assert_eq!(p.st.particles[0].first_fill, Some(5));
    assert!(p.finished());
    // with k = 0 (x = r): row 2 draws r1 = 0.763 > 0.625 -> stays; row 3 r2 = 0.665 < 0.833 -> 50 - 20 = 30
    let sl0 = slot("databento_mc[k=0;particles=1]");
    let seed0 = qt_types::stable_seed(0, &[sl0.id_str.as_bytes(), b"s1"]);
    let mut q = Pair::new(&sl0, 1.0, Xoshiro256StarStar::new(seed0));
    let mut probe = Xoshiro256StarStar::new(seed0);
    let (v1, v2) = (probe.next_f64(), probe.next_f64());
    q.apply(&sl0, &o, &arrive(50.0));
    for op in [dep(1, 50.0, 100.0), dep(2, 100.0, 80.0), dep(3, 80.0, 60.0)] {
        q.apply(&sl0, &o, &op);
    }
    let mut f: f64 = 50.0;
    if v1 < 50.0 / 80.0 {
        f -= 20.0;
    }
    if v2 < f / 60.0 {
        f -= 20.0;
    }
    assert_eq!(q.st.particles[0].front, f.clamp(0.0, 60.0));
}

// ---- partition simulation (L2 arm) ----------------------------------------------------------------------------

fn depth(ts: Ts, side: Side, px: i64, q: f64) -> Row {
    Row {
        ts,
        kind: RowKind::Depth,
        side,
        price: px,
        qty: q,
        aggressor: Side::None,
        flags: 0,
        clear_side: None,
        clear_upto: None,
    }
}

fn trow(ts: Ts, agg: Side, px: i64, q: f64) -> Row {
    Row {
        ts,
        kind: RowKind::Trade,
        side: agg.opposite(),
        price: px,
        qty: q,
        aggressor: agg,
        flags: 0,
        clear_side: None,
        clear_upto: None,
    }
}

fn cfg() -> SimConfig {
    SimConfig {
        horizons: vec![10, 100, 1000],
        h_max: 1000,
        tick: 1,
        lot: 1.0,
        cadence0: true,
        seed: 0,
        write_trajectory: false,
        trace: false,
    }
}

fn shadow(id: &str, side: BookSide, price: PriceSpec, qty: u64, ts_live: Ts, cancel: Option<Ts>) -> ShadowInput {
    ShadowInput {
        shadow_id: id.into(),
        instrument_id: 7,
        side,
        price,
        qty,
        ts_live,
        cancel_at: cancel,
        amends: Vec::new(),
    }
}

/// L2-only mode: predictions without truth (truth columns null, `censored` false), touch price from the view,
/// activation at the first batch after `ts_live`, horizons, cancels and the bracket property.
#[test]
fn l2_only_mode_predictions() {
    let models: Vec<ModelId> = [
        "front_of_queue",
        "trade_through_only",
        "hbt_risk_adverse",
        "nt_prob_touch",
    ]
    .iter()
    .map(|s| mid(s))
    .collect();
    let sh = vec![
        shadow("a", BookSide::Bid, PriceSpec::Touch { offset_ticks: 0 }, 2, 15, None),
        shadow("b", BookSide::Bid, PriceSpec::Abs(100), 1, 15, Some(25)),
        shadow("c", BookSide::Ask, PriceSpec::Touch { offset_ticks: 1 }, 1, 15, None),
    ];
    let mut sim = PartitionSim::new(cfg(), &models, sh).unwrap();
    sim.on_batch(&[depth(10, Side::Bid, 100, 5.0), depth(10, Side::Ask, 102, 5.0)]);
    sim.on_batch(&[trow(20, Side::Ask, 100, 3.0), depth(20, Side::Bid, 100, 2.0)]);
    sim.on_batch(&[trow(30, Side::Ask, 100, 4.0), depth(30, Side::Bid, 100, 0.0)]);
    sim.on_batch(&[trow(40, Side::Ask, 99, 1.0)]);
    assert_eq!(sim.price_of(0), Some(100));
    assert_eq!(sim.price_of(2), Some(101));
    let out = sim.finish(None, 3, true, 2000);
    assert_eq!(out.predictions.len(), 4 * 3 * 3);
    for r in &out.predictions {
        assert!(r.truth_filled.is_none() && r.truth_filled_qty.is_none() && r.queue_ahead_at_live.is_none());
        assert!(!r.censored);
        assert_eq!(r.session_date, 3);
        assert_eq!(r.ts_live, 15);
    }
    let get = |m: &str, s: &str, h: i64| {
        out.predictions
            .iter()
            .find(|r| r.model_id.starts_with(m) && r.shadow_id == s && r.horizon_ns == h)
            .unwrap()
            .clone()
    };
    // shadow a (bid 100, 5 ahead): front_of_queue fills 2 at ts 20 (horizon 10 = ts 25)
    assert_eq!(get("front_of_queue", "a", 10).p_fill, 1.0);
    assert_eq!(get("front_of_queue", "a", 10).e_fill_qty, 2.0);
    assert_eq!(get("front_of_queue", "a", 10).visible_level_qty_at_live, 5);
    // trade_through_only fills at the trade through at 40 (horizon 100)
    assert_eq!(get("trade_through_only", "a", 10).p_fill, 0.0);
    assert_eq!(get("trade_through_only", "a", 100).p_fill, 1.0);
    assert_eq!(get("trade_through_only", "a", 100).model_ts_first_fill, Some(40));
    // risk adverse: F 5 -> 2 (trade 3) -> min(2, 2) -> -2 (trade 4): lots 2 at ts 30
    assert_eq!(get("hbt_risk_adverse", "a", 10).p_fill, 0.0);
    assert_eq!(get("hbt_risk_adverse", "a", 100).p_fill, 1.0);
    assert_eq!(get("hbt_risk_adverse", "a", 100).model_ts_first_fill, Some(30));
    // shadow b cancelled at 25: front_of_queue filled at 20, nothing after
    assert_eq!(get("front_of_queue", "b", 1000).e_fill_qty, 1.0);
    assert_eq!(get("trade_through_only", "b", 1000).p_fill, 0.0);
    // nt_prob_touch analytic, p = 1
    assert_eq!(get("nt_prob_touch", "a", 10).p_fill, 1.0);
    // bracket at h_max: front_of_queue >= any >= trade_through_only
    for s in ["a", "b", "c"] {
        let f = get("front_of_queue", s, 1000).p_fill;
        let t = get("trade_through_only", s, 1000).p_fill;
        let r = get("hbt_risk_adverse", s, 1000).p_fill;
        assert!(f >= r && r >= t, "{s}");
    }
    // fills table: deterministic models only, fill_seq from 1
    assert!(
        out.fills
            .iter()
            .all(|f| f.fill_seq >= 1 && !f.model_id.starts_with("databento"))
    );
}

#[test]
fn activation_is_strict_and_cancel_before_live_never_activates() {
    let models = vec![mid("front_of_queue")];
    let sh = vec![
        shadow("x", BookSide::Bid, PriceSpec::Abs(100), 1, 20, None),
        shadow("y", BookSide::Bid, PriceSpec::Abs(100), 1, 20, Some(20)),
    ];
    let mut sim = PartitionSim::new(cfg(), &models, sh).unwrap();
    sim.on_batch(&[depth(10, Side::Bid, 100, 5.0)]);
    sim.on_batch(&[trow(20, Side::Ask, 100, 1.0)]); // ts 20 is not > 20: not live yet
    sim.on_batch(&[trow(21, Side::Ask, 100, 1.0)]);
    let out = sim.finish(None, 0, true, 2000);
    let x: Vec<_> = out.predictions.iter().filter(|r| r.shadow_id == "x").collect();
    assert_eq!(x[0].model_ts_first_fill, Some(21));
    let y: Vec<_> = out.predictions.iter().filter(|r| r.shadow_id == "y").collect();
    assert!(y.iter().all(|r| r.p_fill == 0.0));
}

#[test]
fn horizon_stop_and_admin_censoring() {
    let models = vec![mid("front_of_queue")];
    let sh = vec![shadow("x", BookSide::Bid, PriceSpec::Abs(100), 1, 0, None)];
    let mut c = cfg();
    c.h_max = 50;
    c.horizons = vec![10, 50];
    let mut sim = PartitionSim::new(c, &models, sh).unwrap();
    sim.on_batch(&[depth(5, Side::Bid, 100, 5.0)]);
    sim.on_batch(&[trow(70, Side::Ask, 100, 1.0)]); // past h_max: the pair stopped
    let truth = vec![crate::sim::Truth {
        ts_live: 0,
        ..Default::default()
    }];
    let out = sim.finish(Some(&truth), 0, true, 30);
    assert!(out.predictions.iter().all(|r| r.p_fill == 0.0));
    assert_eq!(out.admin_end, vec![30]);
    // admin end 30 (session close): horizon 10 not censored, horizon 50 censored, whatever the outcome
    assert!(!out.predictions[0].censored);
    assert!(out.predictions[1].censored);
}

/// Faithful hbt per-row cross (the opposite best moves toward the order) vs corrected per-batch cross.
#[test]
fn cross_timing_faithful_per_row_corrected_per_batch() {
    let models = vec![mid("hbt_risk_adverse"), mid("hbt_risk_adverse[mode=corrected]")];
    let sh = vec![shadow("x", BookSide::Bid, PriceSpec::Abs(100), 1, 0, None)];
    let mut sim = PartitionSim::new(cfg(), &models, sh).unwrap();
    sim.on_batch(&[depth(5, Side::Bid, 100, 5.0), depth(5, Side::Ask, 102, 5.0)]);
    // within one batch the ask touches 100 and recedes: hbt fills on the row, the per-batch check does not
    sim.on_batch(&[depth(10, Side::Ask, 100, 2.0), depth(10, Side::Ask, 100, 0.0)]);
    let out = sim.finish(None, 0, true, 2000);
    let f: Vec<_> = out
        .fills
        .iter()
        .map(|f| (f.model_id.clone(), f.ts_event, f.reason))
        .collect();
    assert_eq!(
        f,
        vec![(
            "hbt_risk_adverse[exch=partial;mode=faithful]".to_owned(),
            10,
            ZooFillReason::Crossed
        )]
    );
}

#[test]
fn implied_side_n_trade_reaches_models_when_hit_side_is_known() {
    let models = vec![mid("front_of_queue")];
    let sh = vec![shadow("x", BookSide::Bid, PriceSpec::Abs(100), 1, 0, None)];
    let mut sim = PartitionSim::new(cfg(), &models, sh).unwrap();
    sim.on_batch(&[depth(5, Side::Bid, 100, 5.0)]);
    // CME implied: aggressor N, hit side inferred as B by L2Builder, not flagged hidden
    sim.on_batch(&[Row {
        ts: 6,
        kind: RowKind::Trade,
        side: Side::Bid,
        price: 100,
        qty: 1.0,
        aggressor: Side::None,
        flags: 0,
        clear_side: None,
        clear_upto: None,
    }]);
    // hidden print: not dispatched to non-NT models
    sim.on_batch(&[Row {
        ts: 7,
        kind: RowKind::Trade,
        side: Side::None,
        price: 100,
        qty: 1.0,
        aggressor: Side::None,
        flags: qt_schema::rows::l2flags::HIDDEN_TRADE,
        clear_side: None,
        clear_upto: None,
    }]);
    let out = sim.finish(None, 0, true, 2000);
    assert_eq!(out.fills.len(), 1);
    assert_eq!(out.fills[0].ts_event, 6);
}

/// O-ZOO-5 determinism: same seed -> identical particle predictions; different seed differs.
#[test]
fn o_zoo_5_databento_determinism() {
    let run = |seed: u64| {
        let models = vec![mid("databento_mc[k=-0.8;particles=200]")];
        let sh = vec![shadow("x", BookSide::Bid, PriceSpec::Abs(100), 1, 6, None)];
        let mut c = cfg();
        c.seed = seed;
        c.horizons = vec![10, 98, 1000];
        let mut sim = PartitionSim::new(c, &models, sh).unwrap();
        sim.on_batch(&[depth(5, Side::Bid, 100, 50.0)]);
        sim.on_batch(&[depth(9, Side::Bid, 100, 100.0)]);
        for k in 0..4 {
            sim.on_batch(&[depth(10 + 2 * Ts::from(k), Side::Bid, 100, 90.0)]);
            sim.on_batch(&[depth(11 + 2 * Ts::from(k), Side::Bid, 100, 100.0)]);
        }
        for k in 0..6 {
            sim.on_batch(&[trow(95 + 2 * Ts::from(k), Side::Ask, 100, 10.0 * f64::from(k + 1))]);
        }
        let out = sim.finish(None, 0, true, 2000);
        out.predictions
            .iter()
            .map(|r| (r.p_fill.to_bits(), r.e_fill_qty.to_bits(), r.model_ts_first_fill))
            .collect::<Vec<_>>()
    };
    let a = run(1);
    assert_eq!(a, run(1));
    assert_ne!(a, run(2));
    assert!(a.iter().any(|x| f64::from_bits(x.0) > 0.0 && f64::from_bits(x.0) < 1.0));
}

// ---- O-ZOO-2: transliteration parity fixtures --------------------------------------------------------------------

fn read_parquet(name: &str) -> Vec<arrow_array::RecordBatch> {
    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
    let p = fixtures().join("hbt_parity").join(name);
    let f = std::fs::File::open(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    let r = ParquetRecordBatchReaderBuilder::try_new(f).unwrap().build().unwrap();
    r.map(|b| b.unwrap()).collect()
}

fn col<'a, T: 'static>(b: &'a arrow_array::RecordBatch, name: &str) -> &'a T {
    b.column_by_name(name)
        .unwrap()
        .as_any()
        .downcast_ref::<T>()
        .unwrap_or_else(|| panic!("column {name}"))
}

/// O-ZOO-2: 200 random L2 sequences per hbt family (committed Parquet, generated by
/// `python/tests/zoo/gen_fixtures.py` through the transliteration `hbt_ref.py`): the faithful pair runner's `F`
/// and `T` after every op (bit-exact), every fill quantity, the leaves, and the #312 error all equal the reference
/// under both exchanges.
#[test]
fn o_zoo_2_transliteration_parity() {
    use arrow_array::{Array, BooleanArray, Float64Array, Int32Array, StringArray, UInt64Array};
    type Seq = (String, Option<f64>, f64, f64, Vec<(String, f64, f64)>);
    let mut seqs: BTreeMap<i32, Seq> = BTreeMap::new();
    for b in read_parquet("sequences.parquet") {
        let id = col::<Int32Array>(&b, "seq_id");
        let fam = col::<StringArray>(&b, "family");
        let n = col::<Float64Array>(&b, "n");
        let lot = col::<Float64Array>(&b, "lot");
        let qty = col::<Float64Array>(&b, "qty");
        let op = col::<StringArray>(&b, "op");
        let a = col::<Float64Array>(&b, "a");
        let bb = col::<Float64Array>(&b, "b");
        for i in 0..b.num_rows() {
            let e = seqs.entry(id.value(i)).or_insert_with(|| {
                (
                    fam.value(i).to_owned(),
                    n.is_valid(i).then(|| n.value(i)),
                    lot.value(i),
                    qty.value(i),
                    Vec::new(),
                )
            });
            e.4.push((op.value(i).to_owned(), a.value(i), bb.value(i)));
        }
    }
    type Exp = (u64, Option<u64>, u64, u64, bool);
    let mut exp: BTreeMap<(i32, String), Vec<Exp>> = BTreeMap::new();
    for b in read_parquet("expected.parquet") {
        let id = col::<Int32Array>(&b, "seq_id");
        let exch = col::<StringArray>(&b, "exch");
        let f = col::<UInt64Array>(&b, "f_bits");
        let t = col::<UInt64Array>(&b, "t_bits");
        let fq = col::<UInt64Array>(&b, "fill_qty_bits");
        let lv = col::<UInt64Array>(&b, "leaves_bits");
        let er = col::<BooleanArray>(&b, "error");
        for i in 0..b.num_rows() {
            exp.entry((id.value(i), exch.value(i).to_owned())).or_default().push((
                f.value(i),
                t.is_valid(i).then(|| t.value(i)),
                fq.value(i),
                lv.value(i),
                er.value(i),
            ));
        }
    }
    assert_eq!(seqs.len(), 6 * 200);
    let mut per_family: BTreeMap<String, usize> = BTreeMap::new();
    let mut n_ops = 0usize;
    for (sid, (fam, n, lot, qty, ops)) in &seqs {
        *per_family.entry(fam.clone()).or_default() += 1;
        for exch in ["no_partial", "partial"] {
            let id = match n {
                Some(n) => format!("{fam}[exch={exch};n={n}]"),
                None => format!("{fam}[exch={exch}]"),
            };
            let sl = slot(&id);
            let o = ShadowL2 {
                idx: 0,
                side: BookSide::Bid,
                price: 100,
                qty: *qty,
                lot: *lot,
            };
            let mut p = Pair::new(&sl, *qty, Xoshiro256StarStar::new(0));
            let want = &exp[&(*sid, exch.to_owned())];
            let mut k = 0;
            for (op, a, b) in ops {
                if p.finished() {
                    break;
                }
                let before = p.fills.len();
                let op = match op.as_str() {
                    "arrive" => Op::Arrive {
                        ts: 0,
                        q_view: *a,
                        q_hbt: *a,
                        cross_view: false,
                        cross_hbt: false,
                        opp_view: 0.0,
                    },
                    "depth" => Op::Depth {
                        ts: k as Ts,
                        prev_view: *a,
                        prev_hbt: *a,
                        new: *b,
                        flags: 0,
                    },
                    "trade" => Op::Trade {
                        ts: k as Ts,
                        qty: *a,
                        aggressor: Side::Ask,
                        hidden: false,
                    },
                    "through" => Op::Through {
                        ts: k as Ts,
                        hidden: false,
                    },
                    "cross" => Op::CrossRow { ts: k as Ts },
                    other => panic!("op {other}"),
                };
                p.apply(&sl, &o, &op);
                let ctx = format!("seq {sid} {id} op {k}");
                let (fb, tb, fqb, lb, er) = want.get(k).copied().unwrap_or_else(|| panic!("{ctx}: extra op"));
                assert_eq!(
                    p.st.front.to_bits(),
                    fb,
                    "{ctx}: F {:?} vs {:?}",
                    p.st.front,
                    f64::from_bits(fb)
                );
                if let Some(tb) = tb {
                    assert_eq!(p.st.cum_trade.to_bits(), tb, "{ctx}: T");
                }
                let fill = if p.fills.len() > before {
                    p.fills[before].qty
                } else {
                    0.0
                };
                assert_eq!(fill.to_bits(), fqb, "{ctx}: fill {fill} vs {}", f64::from_bits(fqb));
                assert_eq!(p.st.leaves.to_bits(), lb, "{ctx}: leaves");
                assert_eq!(p.hbt_error.is_some(), er, "{ctx}: error");
                k += 1;
                n_ops += 1;
            }
            assert_eq!(k, want.len(), "seq {sid} {id}: op count");
        }
    }
    assert!(per_family.values().all(|n| *n == 200));
    assert!(n_ops > 20_000);
}

// ---- O-ZOO-4 faithful: hbt L3 FIFO transliteration ---------------------------------------------------------------

/// O-ZOO-4 (faithful part): `hbt_l3_fifo` faithful on 300 constructed single-record-event MBO sequences gives the
/// same first fill (time and kind) or GTX expiry as the Python transliteration of hbt `L3FIFOQueueModel` +
/// `L3NoPartialFillExchange` (`tests/fixtures/zoo/l3_faithful.json`, `python/tests/zoo/gen_fixtures.py`).
#[test]
fn o_zoo_4_l3_faithful_matches_transliteration() {
    use crate::l3::{L3Pair, L3Reason};
    use qt_book::Book;
    use qt_types::{MboEvent, flags};
    let doc = read_json(fixtures().join("l3_faithful.json"));
    let seqs = doc["sequences"].as_array().unwrap();
    assert_eq!(seqs.len(), 300);
    for sq in seqs {
        let sh = &sq["shadow"];
        let s_side = if sh["side"] == "B" {
            BookSide::Bid
        } else {
            BookSide::Ask
        };
        let s_px = sh["tick"].as_i64().unwrap();
        let s_ts = sh["ts"].as_i64().unwrap();
        let mut book = Book::new(qt_types::rules(qt_types::Venue::Cme));
        let mut pair: Option<L3Pair> = None;
        let mut started = false;
        let mut got: Option<(Ts, &str)> = None;
        for (n, r) in sq["records"].as_array().unwrap().iter().enumerate() {
            let ts = r[0].as_i64().unwrap();
            if !started && ts > s_ts {
                started = true;
                let probe = L3Pair::new(s_side, s_px, 1, Mode::Faithful, &book);
                if probe.crossed(&book) {
                    got = Some((s_ts, "expired"));
                    break;
                }
                pair = Some(probe);
            }
            let action = match r[1].as_str().unwrap() {
                "A" => b'A',
                "C" => b'C',
                "M" => b'M',
                _ => b'F',
            };
            let side = if r[2] == "B" { b'B' } else { b'A' };
            let e = MboEvent {
                ts_event: ts,
                ts_recv: ts,
                instrument_id: 1,
                action,
                side,
                price: r[3].as_i64().unwrap(),
                size: r[4].as_u64().unwrap(),
                order_id: r[5].as_u64().unwrap(),
                flags: flags::LAST,
                sequence: n as u64 + 1,
                channel_id: 0,
                publisher_id: 0,
            };
            let effect = book.apply(&e);
            if let Some(p) = pair.as_mut() {
                p.on_record(&e, &effect, &book, (None, None));
                let fills = p.on_event_end(&book);
                if let Some((_, why)) = fills.first() {
                    got = Some((
                        ts,
                        match why {
                            L3Reason::Queue => "queue",
                            L3Reason::TradeThrough => "through",
                            L3Reason::Crossed => "cross",
                        },
                    ));
                    break;
                }
            }
        }
        let want = sq["expected"]
            .as_array()
            .map(|x| (x[0].as_i64().unwrap(), x[1].as_str().unwrap()));
        assert_eq!(got, want, "sequence {}", sq["id"]);
    }
}

// ---- schema 1.1 amends, review m23 / m27 ---------------------------------------------------------------------

fn with_amends(mut s: ShadowInput, amends: Vec<(Ts, u64)>) -> ShadowInput {
    s.amends = amends;
    s
}

/// `ZooSpec.amends` on the L2 arm, exact-engine order: an amend to 0 effective at or before `ts_live` cancels the
/// shadow before it goes live; one before `ts_live` sets the quantity it goes live with; a live amend at or below
/// the filled quantity ends the order (`amended_to_filled`); a live amend above it caps later fills.
#[test]
fn amends_on_the_l2_arm() {
    let models = vec![mid("front_of_queue")];
    let sh = vec![
        with_amends(
            shadow("z", BookSide::Bid, PriceSpec::Abs(100), 5, 15, None),
            vec![(12, 0)],
        ),
        with_amends(
            shadow("r", BookSide::Bid, PriceSpec::Abs(100), 5, 15, None),
            vec![(15, 2)],
        ),
        with_amends(
            shadow("l", BookSide::Bid, PriceSpec::Abs(100), 5, 15, None),
            vec![(25, 1)],
        ),
        with_amends(
            shadow("m", BookSide::Bid, PriceSpec::Abs(100), 5, 15, None),
            vec![(25, 4)],
        ),
    ];
    let mut sim = PartitionSim::new(cfg(), &models, sh).unwrap();
    sim.on_batch(&[depth(10, Side::Bid, 100, 5.0)]);
    sim.on_batch(&[trow(20, Side::Ask, 100, 3.0)]);
    sim.on_batch(&[trow(30, Side::Ask, 100, 5.0)]);
    let out = sim.finish(None, 0, true, 2000);
    let filled = |s: &str| -> f64 { out.fills.iter().filter(|f| f.shadow_id == s).map(|f| f.fill_qty).sum() };
    assert_eq!(filled("z"), 0.0);
    assert!(
        out.predictions
            .iter()
            .filter(|r| r.shadow_id == "z")
            .all(|r| r.p_fill == 0.0)
    );
    assert_eq!(filled("r"), 2.0);
    assert_eq!(filled("l"), 3.0);
    assert_eq!(filled("m"), 4.0);
    // the `qty` column keeps the scheduled quantity; e_fill_qty follows the amends
    let m = out
        .predictions
        .iter()
        .find(|r| r.shadow_id == "m" && r.horizon_ns == 1000)
        .unwrap();
    assert_eq!((m.qty, m.e_fill_qty), (5, 4.0));
}

/// Review m23: a batch's time is its emission time (the latest row), not the last row's, which can be an older
/// trade with `trade_position = after` and conflation; activation uses it.
#[test]
fn batch_time_is_the_latest_row() {
    let models = vec![mid("front_of_queue")];
    let sh = vec![shadow("a", BookSide::Bid, PriceSpec::Abs(100), 1, 25, None)];
    let mut sim = PartitionSim::new(cfg(), &models, sh).unwrap();
    sim.on_batch(&[depth(10, Side::Bid, 100, 5.0)]);
    sim.on_batch(&[depth(30, Side::Bid, 100, 4.0), trow(20, Side::Ask, 100, 1.0)]);
    let out = sim.finish(None, 0, true, 2000);
    assert_eq!(out.activated, vec![Some(30)]);
}

/// Review m27: an L2 depth or trade row without a price is skipped and counted, never read as price 0 (a trade
/// through every bid).
#[test]
fn priceless_l2_rows_are_skipped() {
    use qt_schema::rows::{L2Kind, L2Row, l2flags};
    let row = |ts: Ts, kind: L2Kind, side: Side, price: Option<i64>, qty: u64, last: bool| L2Row {
        ts_event: ts,
        ts_recv: ts,
        instrument_id: 7,
        event_id: ts as u64,
        sequence: ts as u64,
        kind,
        side,
        price,
        prev_qty: 0,
        new_qty: if kind == L2Kind::Depth { qty } else { 0 },
        prev_ct: None,
        new_ct: None,
        trade_qty: if kind == L2Kind::Trade { qty } else { 0 },
        aggressor: if kind == L2Kind::Trade { Side::Ask } else { Side::None },
        level: 0,
        flags: if last { l2flags::LAST_IN_BATCH } else { 0 },
    };
    let models = vec![mid("trade_through_only")];
    let sh = vec![shadow("a", BookSide::Bid, PriceSpec::Abs(100), 1, 5, None)];
    let mut sim = PartitionSim::new(cfg(), &models, sh).unwrap();
    sim.on_l2_batch(&[row(10, L2Kind::Depth, Side::Bid, Some(100), 5, true)]);
    sim.on_l2_batch(&[row(20, L2Kind::Trade, Side::Bid, None, 3, true)]);
    let out = sim.finish(None, 0, true, 2000);
    assert!(out.fills.is_empty(), "{:?}", out.fills);
    assert!(
        out.warnings.iter().any(|w| w.contains("without a price")),
        "{:?}",
        out.warnings
    );
}

/// `hbt_l3_fifo` building blocks for schema 1.1: an amend at or below the filled quantity ends the order; an
/// excluded own twin's execution (A1) at the shadow's price counts only once the queue ahead is gone, and through
/// the price it is a trade-through.
#[test]
fn l3_pair_amend_and_excluded_executions() {
    use crate::l3::{L3Pair, L3Reason};
    use qt_types::{MboEvent, VenueRules, flags};
    let rules = VenueRules {
        supports_hidden_orders: false,
        ..qt_types::rules(qt_types::Venue::Cme)
    };
    let mut book = qt_book::Book::new(rules);
    let rec = |action: u8, side: u8, price: i64, size: u64, order_id: u64| MboEvent {
        ts_event: 1,
        ts_recv: 1,
        instrument_id: 1,
        action,
        side,
        price,
        size,
        order_id,
        flags: flags::LAST,
        sequence: 1,
        channel_id: 0,
        publisher_id: 0,
    };
    book.apply(&rec(b'A', b'B', 100, 4, 1));
    let mut p = L3Pair::new(BookSide::Bid, 100, 10, Mode::Corrected, &book);
    // own twin executed 5 at the price while 4 are ahead: nothing yet
    p.on_excluded_exec(BookSide::Bid, 100, 5, &book);
    assert!(p.on_event_end(&book).is_empty());
    // the order ahead leaves; the twin executes 3 more: they fill the shadow
    book.apply(&rec(b'C', b'B', 100, 4, 1));
    p.on_excluded_exec(BookSide::Bid, 100, 3, &book);
    assert_eq!(p.on_event_end(&book), vec![(3, L3Reason::Queue)]);
    // an own execution resting below the shadow's price trades through it
    p.on_excluded_exec(BookSide::Bid, 99, 2, &book);
    assert_eq!(p.on_event_end(&book), vec![(2, L3Reason::TradeThrough)]);
    // the other side never touches it
    p.on_excluded_exec(BookSide::Ask, 100, 2, &book);
    assert!(p.on_event_end(&book).is_empty());
    assert_eq!(p.leaves(), 5);
    assert!(p.amend(7));
    assert_eq!(p.leaves(), 2);
    assert!(!p.amend(5));
    assert_eq!(p.leaves(), 0);
}

/// Amend validation of the zoo lifecycle (schema 1.1): after `ts_submit`, strictly decreasing quantities, in time
/// order (review m11); effective times add `latency_ns + gateway` as the exact engine.
#[test]
fn amend_lifecycle_validation() {
    use qt_schema::rows::{ShadowAmend, ShadowSource, ShadowSpec};
    let spec = ShadowSpec {
        shadow_id: "s".into(),
        instrument_id: 1,
        side: BookSide::Bid,
        price: PriceSpec::Abs(100),
        qty: 10,
        ts_submit: 100,
        latency_ns: 5,
        ts_cancel: None,
        group: None,
        source: ShadowSource::Sampled,
        order_key: None,
    };
    let am = |t: Ts, q: u64| ShadowAmend {
        shadow_id: "s".into(),
        ts_send: t,
        new_qty: q,
    };
    let lat = qt_types::LatencyModel {
        gateway_ns: 7,
        ..qt_types::LatencyModel::default()
    };
    let (live, cancel, times) =
        crate::runner::lifecycle_with_amends(&spec, &[am(200, 6), am(300, 2)], &lat, 0).unwrap();
    assert_eq!((live, cancel), (112, None));
    assert_eq!(times, vec![(212, 6), (312, 2)]);
    for bad in [vec![am(99, 5)], vec![am(200, 10)], vec![am(300, 6), am(200, 2)]] {
        let e = crate::runner::lifecycle_with_amends(&spec, &bad, &lat, 0).unwrap_err();
        assert_eq!(e.code(), "QT-INPUT-002", "{e}");
    }
}

/// A5 as refined by E48: in corrected mode a resting order later crossed by an opposite
/// arrival fills min(leaves, arriving size) as a maker (`crossed`); only going live into a crossed book is a taker
/// fill (`crossed_at_live`).
#[test]
fn corrected_resting_order_crossed_later_is_a_maker_fill() {
    use qt_schema::rows::Liquidity;
    let sl = slot("hbt_power3[n=2;mode=corrected]");
    let o = ShadowL2 {
        idx: 0,
        side: BookSide::Bid,
        price: 100,
        qty: 10.0,
        lot: 1.0,
    };
    let mut p = Pair::new(&sl, 10.0, Xoshiro256StarStar::new(0));
    p.apply(
        &sl,
        &o,
        &Op::Arrive {
            ts: 5,
            q_view: 3.0,
            q_hbt: 3.0,
            cross_view: false,
            cross_hbt: false,
            opp_view: 0.0,
        },
    );
    assert!(p.fills.is_empty());
    // a 6-lot ask arrives at 99: the resting bid fills 6 at its own price, as a maker
    p.apply(
        &sl,
        &o,
        &Op::BatchEnd {
            ts: 8,
            cross_view: true,
            opp_view: 6.0,
            cadence0: true,
        },
    );
    assert_eq!(p.fills.len(), 1);
    assert_eq!((p.fills[0].qty, p.fills[0].reason), (6.0, ZooFillReason::Crossed));
    assert_eq!(crate::fill_liquidity(p.fills[0].reason), Liquidity::Maker);
    assert_eq!(crate::fill_liquidity(ZooFillReason::CrossedAtLive), Liquidity::Taker);
    for r in [
        ZooFillReason::Queue,
        ZooFillReason::TradeThrough,
        ZooFillReason::TouchDraw,
    ] {
        assert_eq!(crate::fill_liquidity(r), Liquidity::Maker);
    }
}
