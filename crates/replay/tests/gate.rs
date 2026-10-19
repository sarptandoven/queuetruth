//! Gate tests (need `qt_testkit` and `qt_ingest`): O-PAR-1, O-SH-2, O-SH-4, O-PAR-2, O-PAR-4, and `run_dataset`,
//! `sample_shadows`, `mirror_real_orders`, `why` and `Session` on datasets (which also proves that `Dataset::open`
//! reads the testkit's layout).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use proptest::prelude::*;
use qt_replay::sinks::VecSink;
use qt_replay::{BboMode, Engine, EngineConfig, Session, run_dataset};
use qt_schema::codec::TableRow;
use qt_schema::io::{read_table_as, sha256_file};
use qt_schema::rows::{BboRow, RealOrderRow, ShadowResultRow, ShadowSpec, TradeRow};
use qt_schema::specs::{MirrorSpec, ReplaySpec, ShadowSampleSpec, WhySpec};
use qt_testkit::{RefConfig, SynthConfig, generate, try_reference_shadows, write_dataset};
use qt_types::{BookSide, Instrument, LatencyModel, MboEvent, Venue, flags, rules};
use serde_json::json;

const GATE: &str = "gate test: needs the testkit and ingest crates";

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-replay-gate-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn read<R: TableRow>(p: &Path) -> Vec<R> {
    let mut out = Vec::new();
    for b in read_table_as(p, R::TABLE).unwrap() {
        out.extend(R::from_batch(&b.unwrap()).unwrap());
    }
    out
}

fn small(venue: Venue, seed: u64, events: u64) -> SynthConfig {
    let mut c = SynthConfig::for_venue(venue, seed);
    c.events_per_instrument = events;
    c
}

/// One engine per instrument (single-session streams), like `run_partition`.
fn per_instrument(
    venue: Venue,
    events: &[MboEvent],
    insts: &[Instrument],
    mut c: EngineConfig,
    shadows: &[ShadowSpec],
    amends: &[qt_schema::rows::ShadowAmend],
) -> VecSink {
    c.rules = rules(venue);
    let mut by: BTreeMap<u32, Vec<MboEvent>> = BTreeMap::new();
    for e in events {
        by.entry(e.instrument_id).or_default().push(*e);
    }
    let mut out = VecSink::default();
    for (inst, ev) in by {
        let rows: Vec<Instrument> = insts.iter().filter(|i| i.instrument_id == inst).cloned().collect();
        let sh: Vec<ShadowSpec> = shadows.iter().filter(|s| s.instrument_id == inst).cloned().collect();
        let ids: std::collections::BTreeSet<&str> = sh.iter().map(|s| s.shadow_id.as_str()).collect();
        let am = amends
            .iter()
            .filter(|a| ids.contains(a.shadow_id.as_str()))
            .cloned()
            .collect();
        let mut eng = Engine::new(c.clone(), &rows, sh, am).unwrap();
        for e in &ev {
            eng.apply(e, &mut out).unwrap();
        }
        eng.finish(&mut out).unwrap();
    }
    out
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn o_par_1_bbo_and_trades_equal_generator_expectations() {
    let _ = GATE;
    for venue in [Venue::Nasdaq, Venue::Cme, Venue::Generic] {
        for seed in 1..=5 {
            let out = generate(&small(venue, seed, 3_000));
            let mut c = EngineConfig::new(rules(venue));
            c.bbo = BboMode::Full;
            let full = per_instrument(venue, &out.events, &out.instruments, c.clone(), &[], &[]);
            let key = |b: &BboRow| (b.instrument_id, b.ts_event, b.sequence);
            let (mut a, mut b) = (full.bbo.clone(), out.expected_bbo.clone());
            a.sort_by_key(key);
            b.sort_by_key(key);
            assert_eq!(a, b, "{venue:?} seed {seed}: bbo full");
            let tk = |t: &TradeRow| (t.instrument_id, t.ts_event, t.sequence);
            let (mut x, mut y) = (full.trades.clone(), out.expected_trades.clone());
            x.sort_by_key(tk);
            y.sort_by_key(tk);
            assert_eq!(x, y, "{venue:?} seed {seed}: trades");
            c.bbo = BboMode::Change;
            let change = per_instrument(venue, &out.events, &out.instruments, c, &[], &[]);
            let mut want = Vec::new();
            let mut last: BTreeMap<u32, BboRow> = BTreeMap::new();
            for r in &full.bbo {
                let same = last.get(&r.instrument_id).is_some_and(|l| {
                    (l.bid_px, l.bid_sz, l.bid_ct, l.ask_px, l.ask_sz, l.ask_ct)
                        == (r.bid_px, r.bid_sz, r.bid_ct, r.ask_px, r.ask_sz, r.ask_ct)
                });
                if !same {
                    want.push(*r);
                }
                last.insert(r.instrument_id, *r);
            }
            assert_eq!(change.bbo, want, "{venue:?} seed {seed}: change mode");
        }
    }
}

fn o_sh_2_case(seed: u64, venue: Venue, events: u64, jitter: i64) -> Result<(), String> {
    let out = generate(&small(venue, seed, events));
    let (shadows, amends) = qt_testkit::fixtures::sample_shadows(&out, 25, seed);
    let latency = LatencyModel {
        feed_ns: 1_000,
        decision_ns: 2_000,
        gateway_ns: 3_000,
        jitter_ns: jitter,
    };
    let mut rc = RefConfig::new(rules(venue));
    rc.latency = latency;
    rc.seed = seed;
    rc.instruments = out.instruments.clone();
    rc.dq_flags = true;
    let truth = try_reference_shadows(&out.events, &rc, &shadows, &amends).map_err(|e| e.to_string())?;
    let mut c = EngineConfig::new(rules(venue));
    c.latency = latency;
    c.seed = seed;
    let got = per_instrument(venue, &out.events, &out.instruments, c, &shadows, &amends);
    let by: BTreeMap<&str, &ShadowResultRow> = got.shadow_results.iter().map(|r| (r.shadow_id.as_str(), r)).collect();
    for (r, fills, traj) in &truth {
        let g = by.get(r.shadow_id.as_str()).ok_or(format!("missing {}", r.shadow_id))?;
        if *g != r {
            return Err(format!("result\n got {g:?}\nwant {r:?}"));
        }
        let gf: Vec<_> = got
            .shadow_fills
            .iter()
            .filter(|f| f.shadow_id == r.shadow_id)
            .cloned()
            .collect();
        if gf != *fills {
            return Err(format!("fills {}\n got {gf:?}\nwant {fills:?}", r.shadow_id));
        }
        let gt: Vec<_> = got
            .trajectory
            .iter()
            .filter(|t| t.shadow_id == r.shadow_id)
            .cloned()
            .collect();
        if gt != *traj {
            return Err(format!("trajectory {}\n got {gt:?}\nwant {traj:?}", r.shadow_id));
        }
    }
    if truth.len() != got.shadow_results.len() {
        return Err("result count".into());
    }
    Ok(())
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 1000, failure_persistence: None, ..ProptestConfig::default() })]

    #[test]
    #[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
    fn o_sh_2_differential_vs_reference_shadows(seed in any::<u64>(), v in 0usize..3, jitter in prop_oneof![Just(0i64), 1i64..2_000_000]) {
        let venue = [Venue::Nasdaq, Venue::Cme, Venue::Generic][v];
        if let Err(m) = o_sh_2_case(seed, venue, 400, jitter) {
            prop_assert!(false, "seed {seed} {venue:?} jitter {jitter}: {m}");
        }
    }
}

fn replay_spec(ds: &Path, out: Option<&Path>, extra: serde_json::Value) -> ReplaySpec {
    let mut v = json!({"dataset": {"path": ds.display().to_string()}});
    if let Some(o) = out {
        v["out"] = json!(o.display().to_string());
    }
    if let (Some(m), serde_json::Value::Object(e)) = (v.as_object_mut(), extra) {
        m.extend(e);
    }
    serde_json::from_value(v).unwrap()
}

fn shas(dir: &Path, names: &[&str]) -> Vec<(String, String)> {
    names
        .iter()
        .filter(|n| dir.join(n).exists())
        .map(|n| (n.to_string(), sha256_file(&dir.join(n)).unwrap()))
        .collect()
}

const TABLES: [&str; 9] = [
    "trades.parquet",
    "bbo.parquet",
    "depth.parquet",
    "rank_trace.parquet",
    "shadow_results.parquet",
    "shadow_fills.parquet",
    "queue_trajectory.parquet",
    "real_orders.parquet",
    "shadow_planned_end.parquet",
];

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn run_dataset_deterministic_across_threads_and_matches_engine() {
    let dir = scratch("runds");
    let out = qt_testkit::generate_sessions(&small(Venue::Cme, 9, 2_000), 2);
    let ds = write_dataset(&out, &dir.join("ds")).unwrap();
    let (shadows, amends) = qt_testkit::fixtures::sample_shadows(&out, 30, 9);
    let sp = dir.join("shadows.parquet");
    let mut w = qt_schema::io::TableWriter::create(&sp, "qt.shadow_spec").unwrap();
    w.write(&qt_schema::codec::shadow_specs_to_batch(&shadows)).unwrap();
    w.finish().unwrap();
    let ap = dir.join("amends.parquet");
    let mut w = qt_schema::io::TableWriter::create(&ap, "qt.shadow_amend").unwrap();
    w.write(&qt_schema::codec::shadow_amends_to_batch(&amends)).unwrap();
    w.finish().unwrap();
    let extra = json!({"shadows": {"path": sp.display().to_string()}, "amends": {"path": ap.display().to_string()},
        "rank_oracle": true, "depth_levels": 5, "real_orders": true, "parity_trades": true,
        "l2": {"depth_levels": 5}});
    let mut runs = Vec::new();
    for threads in [1, 4] {
        let mut e = extra.clone();
        e["threads"] = json!(threads);
        let o = dir.join(format!("out{threads}"));
        let s = run_dataset(&replay_spec(&ds, Some(&o), e)).unwrap();
        runs.push((o, s));
    }
    let (a, b) = (&runs[0], &runs[1]);
    assert_eq!(shas(&a.0, &TABLES), shas(&b.0, &TABLES));
    let strip = |s: &qt_schema::json::ReplaySummary| {
        let mut s = s.clone();
        s.timings = None;
        serde_json::to_value(&s).unwrap()
    };
    assert_eq!(strip(&a.1), strip(&b.1), "summary identical across threads");
    let s = &a.1;
    assert_eq!(s.stats.partitions, 4);
    assert_eq!(s.stats.records, out.events.len() as u64);
    assert_eq!(s.invariants, qt_schema::json::Invariants::default());
    assert!(s.parity.as_ref().unwrap().pass, "{:?}", s.parity);
    assert_eq!(s.shadows.as_ref().unwrap().n, shadows.len() as u64);
    assert_eq!(s.run_id.len(), 16);
    assert!(a.0.join("summary.json").exists() && a.0.join("data_quality.json").exists());
    assert!(s.files.iter().any(|f| f.name.starts_with("l2/date=")));
    // The truth of the same schedule from the reference.
    let mut rc = RefConfig::new(rules(Venue::Cme));
    rc.instruments = out.instruments.clone();
    rc.dq_flags = true;
    let truth = try_reference_shadows(&out.events, &rc, &shadows, &amends).unwrap();
    let got: Vec<ShadowResultRow> = read(&a.0.join("shadow_results.parquet"));
    let by: BTreeMap<&str, &ShadowResultRow> = got.iter().map(|r| (r.shadow_id.as_str(), r)).collect();
    for (r, _, _) in &truth {
        assert_eq!(by[r.shadow_id.as_str()], r);
    }
    // Rerun into the same directory: byte-identical tables.
    let again = run_dataset(&replay_spec(&ds, Some(&a.0), extra)).unwrap();
    assert_eq!(again.run_id, a.1.run_id);
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn sample_and_mirror_shadows_deterministic_and_o_sh_4() {
    let dir = scratch("mirror");
    let out = generate(&small(Venue::Cme, 21, 4_000));
    let ds = write_dataset(&out, &dir.join("ds")).unwrap();
    let dsj = json!({"path": ds.display().to_string()});
    let spec = |o: &Path| -> ShadowSampleSpec {
        serde_json::from_value(json!({"dataset": dsj, "out": o.display().to_string(), "n": 200, "seed": 3})).unwrap()
    };
    let s1 = qt_replay::sample_shadows(&spec(&dir.join("s1.parquet"))).unwrap();
    let s2 = qt_replay::sample_shadows(&spec(&dir.join("s2.parquet"))).unwrap();
    assert_eq!(s1.n, 200);
    assert_eq!(s1.files[0].sha256, s2.files[0].sha256);
    let sampled: Vec<ShadowSpec> = read(&dir.join("s1.parquet"));
    assert!(
        sampled
            .iter()
            .all(|s| matches!(s.price, qt_schema::rows::PriceSpec::Touch { .. }))
    );
    // O-SH-4: mirrors never fill before their real order's first fill, never fill when it did not.
    let mspec: MirrorSpec = serde_json::from_value(
        json!({"dataset": dsj, "out": dir.join("m.parquet").display().to_string(), "n": 400, "seed": 1}),
    )
    .unwrap();
    let m = qt_replay::mirror_real_orders(&mspec).unwrap();
    assert!(m.n > 100, "{}", m.n);
    let o = dir.join("out");
    run_dataset(&replay_spec(
        &ds,
        Some(&o),
        json!({"shadows": {"path": dir.join("m.parquet").display().to_string()}, "real_orders": true}),
    ))
    .unwrap();
    let lives: Vec<RealOrderRow> = read(&o.join("real_orders.parquet"));
    let res: Vec<ShadowResultRow> = read(&o.join("shadow_results.parquet"));
    let mirrors: Vec<ShadowSpec> = read(&dir.join("m.parquet"));
    let life: BTreeMap<(u32, u64, i64), &RealOrderRow> = lives
        .iter()
        .map(|l| ((l.instrument_id, l.order_id, l.ts_add), l))
        .collect();
    let (mut shadow_only, mut before_real, mut both) = (0, 0, 0);
    for (s, r) in mirrors.iter().zip(&res) {
        assert_eq!(s.shadow_id, r.shadow_id);
        let oid: u64 = s.shadow_id[1..].split('_').next().unwrap().parse().unwrap();
        let l = life[&(s.instrument_id, oid, s.ts_submit)];
        match (l.ts_first_fill, r.ts_first_fill) {
            (None, Some(_)) => shadow_only += 1,
            (Some(a), Some(b)) => {
                both += 1;
                if b < a {
                    before_real += 1;
                }
            }
            _ => {}
        }
    }
    assert_eq!((shadow_only, before_real), (0, 0));
    assert!(both > 10, "{both}");
    std::fs::remove_dir_all(&dir).ok();
}

fn write_mbo_csv(events: &[MboEvent], path: &Path) {
    let mut s =
        String::from("ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence,channel_id\n");
    for e in events {
        s.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},{}\n",
            e.ts_event,
            e.ts_recv,
            e.instrument_id,
            e.action as char,
            e.side as char,
            e.price,
            e.size,
            e.order_id,
            e.flags,
            e.sequence,
            e.channel_id
        ));
    }
    std::fs::write(path, s).unwrap();
}

/// Partition tables of a replay output, as rows.
fn tables(o: &Path) -> (Vec<BboRow>, Vec<TradeRow>) {
    (read(&o.join("bbo.parquet")), read(&o.join("trades.parquet")))
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn o_par_2_dbn_round_trip_equals_direct_dataset() {
    let dir = scratch("dbn");
    let out = generate(&small(Venue::Cme, 4, 2_000));
    let direct = write_dataset(&out, &dir.join("direct")).unwrap();
    let dbn = dir.join("x.mbo.dbn");
    qt_testkit::encode_dbn(&out.events, &dbn).unwrap();
    let tick = out.instruments[0].tick_size as f64 * 1e-9;
    let ispec: qt_schema::specs::IngestSpec = serde_json::from_value(json!({
        "format": "dbn", "inputs": [{"path": dbn.display().to_string()}], "venue": "cme",
        "out": dir.join("viadbn").display().to_string(), "tz": "UTC", "tick_size": format!("{tick}")
    }))
    .unwrap();
    qt_ingest::ingest(&ispec).unwrap();
    let opts = json!({"bbo": "full", "rank_oracle": true, "depth_levels": 3});
    let a = run_dataset(&replay_spec(&direct, Some(&dir.join("oa")), opts.clone())).unwrap();
    let b = run_dataset(&replay_spec(&dir.join("viadbn"), Some(&dir.join("ob")), opts)).unwrap();
    assert_eq!(a.stats, b.stats);
    assert_eq!(a.invariants, b.invariants);
    let names = ["trades.parquet", "bbo.parquet", "depth.parquet", "rank_trace.parquet"];
    assert_eq!(shas(&dir.join("oa"), &names), shas(&dir.join("ob"), &names));
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn o_par_4_etcsv_ingest_equals_direct_replay() {
    let dir = scratch("etcsv");
    let out = generate(&small(Venue::Nasdaq, 6, 2_000));
    let csv = dir.join("mbo.csv");
    write_mbo_csv(&out.events, &csv);
    let midnight = out.instruments[0].midnight_utc_ns;
    let ispec: qt_schema::specs::IngestSpec = serde_json::from_value(json!({
        "format": "etcsv", "inputs": [{"path": csv.display().to_string()}], "venue": "nasdaq",
        "out": dir.join("ds").display().to_string(), "midnight_utc_ns": midnight, "tick_size": "0.01"
    }))
    .unwrap();
    qt_ingest::ingest(&ispec).unwrap();
    let o = dir.join("o");
    run_dataset(&replay_spec(&dir.join("ds"), Some(&o), json!({"bbo": "full"}))).unwrap();
    let (bbo, trades) = tables(&o);
    let mut c = EngineConfig::new(rules(Venue::Nasdaq));
    c.bbo = BboMode::Full;
    let want = per_instrument(Venue::Nasdaq, &out.events, &out.instruments, c, &[], &[]);
    assert_eq!(bbo, want.bbo);
    assert_eq!(trades, want.trades);
    std::fs::remove_dir_all(&dir).ok();
}

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/synth_small")
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn run_dataset_on_synth_small_matches_truth() {
    let dir = scratch("synth_small");
    let f = fixture();
    let s = run_dataset(&replay_spec(
        &f,
        Some(&dir),
        json!({"shadows": {"path": f.join("truth/shadows.parquet").display().to_string()},
               "amends": {"path": f.join("truth/amends.parquet").display().to_string()}, "bbo": "full"}),
    ))
    .unwrap();
    assert_eq!(s.stats.partitions, 4);
    let got: Vec<ShadowResultRow> = read(&dir.join("shadow_results.parquet"));
    let want: Vec<ShadowResultRow> = read(&f.join("truth/shadow_results.parquet"));
    let mut g = got.clone();
    let mut w = want.clone();
    g.sort_by(|a, b| a.shadow_id.cmp(&b.shadow_id));
    w.sort_by(|a, b| a.shadow_id.cmp(&b.shadow_id));
    assert_eq!(g, w);
    let bbo: Vec<BboRow> = read(&dir.join("bbo.parquet"));
    let wb: Vec<BboRow> = read(&f.join("truth/bbo.parquet"));
    assert_eq!(bbo.len(), wb.len());
    std::fs::remove_dir_all(&dir).ok();
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit and ingest crates")]
fn why_and_session_on_synth_small() {
    let f = fixture();
    let want: Vec<ShadowResultRow> = read(&f.join("truth/shadow_results.parquet"));
    let filled = want
        .iter()
        .find(|r| r.filled_qty > 0 && r.n_fills > 1)
        .unwrap_or(&want[0]);
    let spec: WhySpec = serde_json::from_value(json!({
        "dataset": {"path": f.display().to_string()},
        "shadows": {"path": f.join("truth/shadows.parquet").display().to_string()},
        "shadow_id": filled.shadow_id
    }))
    .unwrap();
    // Amends of the fixture are not part of a why spec: compare only shadows without amends.
    let amends: Vec<qt_schema::rows::ShadowAmend> = read(&f.join("truth/amends.parquet"));
    let t = qt_replay::why(&spec).unwrap();
    if !amends.iter().any(|a| a.shadow_id == filled.shadow_id) {
        assert_eq!(t.outcome.filled_qty, filled.filled_qty);
        assert_eq!(t.outcome.ts_first_fill, filled.ts_first_fill);
        assert_eq!(t.outcome.end_state, filled.end_state.to_string());
    }
    assert!(!t.events.is_empty());
    // Session: BBO after advancing equals the truth bbo stream at that time.
    let bbo: Vec<BboRow> = read(&f.join("truth/bbo.parquet"));
    let mut s = Session::open(&f, None).unwrap();
    for probe in [bbo.len() / 4, bbo.len() / 2, bbo.len() - 1] {
        let r = bbo[probe];
        // The last row of that instrument at or before r.ts_event.
        let want = bbo
            .iter()
            .rfind(|b| b.instrument_id == r.instrument_id && b.ts_event <= r.ts_event)
            .unwrap();
        s.advance_to(r.ts_event).unwrap();
        let got = s.bbo(r.instrument_id).unwrap();
        assert_eq!(
            (
                got.bid.map(|l| (l.price, l.total, l.count)),
                got.ask.map(|l| (l.price, l.total, l.count))
            ),
            (
                want.bid_px.map(|p| (p, want.bid_sz, want.bid_ct)),
                want.ask_px.map(|p| (p, want.ask_sz, want.ask_ct))
            )
        );
        let (b, a) = s.depth(r.instrument_id, 3);
        assert!(b.len() <= 3 && a.len() <= 3);
        if let Some(l) = got.bid {
            assert_eq!(
                s.queue_ahead(r.instrument_id, BookSide::Bid, l.price),
                Some((l.total, l.count))
            );
        }
    }
    let _ = flags::LAST;
}
