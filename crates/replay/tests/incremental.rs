//! Interactive sessions (`Engine::new_interactive`, `add_shadow`, `cancel_shadow`, `amend_shadow`): sending every
//! shadow, cancel and amend as late as the replay allows (just before the first record after its send time) gives the
//! same fills, liquidity, results and trajectory as one run with all of them in its input. Random venue-consistent
//! streams, abs and touch shadows, latency with jitter, both fill rules, a feed gap and a trading halt.

mod common;

use common::{INST, Market, cfg, instrument, random_shadows};
use qt_replay::sinks::VecSink;
use qt_replay::{Engine, EngineConfig, FillRule, TradingHalt, TrajectoryMode};
use qt_schema::json::FeedGap;
use qt_schema::rows::ShadowSpec;
use qt_types::{LatencyModel, MboEvent, Venue};

enum Op {
    Add(ShadowSpec),
    Cancel(String, i64),
    Amend(String, i64, u64),
}

impl Op {
    fn at(&self) -> i64 {
        match self {
            Op::Add(s) => s.ts_submit,
            Op::Cancel(_, t) | Op::Amend(_, t, _) => *t,
        }
    }

    fn send(self, eng: &mut Engine) {
        match self {
            Op::Add(s) => eng.add_shadow(s).map(drop),
            Op::Cancel(id, t) => eng.cancel_shadow(&id, t).map(drop),
            Op::Amend(id, t, q) => eng.amend_shadow(&id, t, q).map(drop),
        }
        .expect("op accepted");
    }
}

fn setup(eng: &mut Engine, rule: FillRule, events: &[MboEvent]) {
    let (t0, t1) = (events[0].ts_event, events[events.len() - 1].ts_event);
    let span = (t1 - t0).max(20);
    eng.set_fill_rule(rule);
    eng.set_feed_gaps(&[FeedGap {
        instrument_id: INST,
        ts_start: t0 + span / 4,
        ts_end: t0 + span / 4 + span / 50,
        reason: "test".into(),
    }]);
    eng.set_trading_halts(&[TradingHalt {
        instrument_id: INST,
        ts_start: t0 + span / 2,
        ts_end: t0 + span / 2 + span / 20,
    }])
    .unwrap();
}

fn config(seed: u64, venue: Venue) -> EngineConfig {
    let mut c = cfg(venue);
    c.seed = seed;
    c.trajectory = TrajectoryMode::Full;
    c.latency = LatencyModel {
        feed_ns: 1_000,
        decision_ns: 500,
        gateway_ns: 700,
        jitter_ns: 50_000,
    };
    c
}

fn check(seed: u64, venue: Venue, rule: FillRule) {
    let events = Market::generate(seed, venue, 1500);
    let (mut shadows, amends) = random_shadows(seed, &events, 80);
    // Input order = submit order, so that sending them in time order gives every shadow the same index.
    shadows.sort_by_key(|s| s.ts_submit);
    let c = config(seed, venue);
    let ctx = format!("seed {seed} {venue:?} {rule:?}");

    let mut batch = Engine::new(c.clone(), &[instrument(venue)], shadows.clone(), amends.clone()).unwrap();
    setup(&mut batch, rule, &events);
    let mut a = VecSink::default();
    for e in &events {
        batch.apply(e, &mut a).unwrap();
    }
    batch.finish(&mut a).unwrap();

    // Adds first, then cancels and amends: a stable sort by send time keeps a shadow's add before its own cancel or
    // amend at the same instant, and same-instant adds in input order.
    let mut ops: Vec<Op> = Vec::new();
    let mut cancels = Vec::new();
    for s in &shadows {
        let mut s = s.clone();
        if let Some(t) = s.ts_cancel.take() {
            cancels.push(Op::Cancel(s.shadow_id.clone(), t));
        }
        ops.push(Op::Add(s));
    }
    ops.extend(cancels);
    ops.extend(
        amends
            .iter()
            .map(|a| Op::Amend(a.shadow_id.clone(), a.ts_send, a.new_qty)),
    );
    ops.sort_by_key(Op::at);

    let mut inc = Engine::new_interactive(c, &[instrument(venue)]).unwrap();
    setup(&mut inc, rule, &events);
    let mut b = VecSink::default();
    let mut ops = ops.into_iter().peekable();
    for e in &events {
        while let Some(op) = ops.next_if(|o| o.at() < e.ts_event) {
            op.send(&mut inc);
        }
        inc.apply(e, &mut b).unwrap();
    }
    for op in ops {
        op.send(&mut inc);
    }
    inc.finish(&mut b).unwrap();

    assert!(!a.shadow_results.is_empty(), "{ctx}");
    assert_eq!(a.shadow_fills, b.shadow_fills, "{ctx}: fills");
    assert_eq!(a.fill_liquidity, b.fill_liquidity, "{ctx}: liquidity");
    assert_eq!(a.shadow_results, b.shadow_results, "{ctx}: results");
    assert_eq!(a.trajectory, b.trajectory, "{ctx}: trajectory");
}

#[test]
fn late_sends_replay_like_one_run() {
    for venue in [Venue::Cme, Venue::Nasdaq, Venue::Generic] {
        for rule in [FillRule::Exact, FillRule::Et] {
            for seed in 0..12 {
                check(seed, venue, rule);
            }
        }
    }
}

#[test]
fn sends_before_the_replay_position_or_out_of_rule_are_refused() {
    let venue = Venue::Nasdaq;
    let events = Market::generate(3, venue, 300);
    let mut eng = Engine::new_interactive(config(3, venue), &[instrument(venue)]).unwrap();
    let mut sink = VecSink::default();
    let mid = events.len() / 2;
    for e in &events[..mid] {
        eng.apply(e, &mut sink).unwrap();
    }
    let now = events[mid - 1].ts_event;
    let (shadows, _) = random_shadows(3, &events, 1);
    let mut s = shadows[0].clone();
    s.ts_cancel = None;
    s.ts_submit = now - 1;
    assert!(eng.add_shadow(s.clone()).is_err(), "submit before the position");
    s.ts_submit = now;
    eng.add_shadow(s.clone()).unwrap();
    assert!(eng.add_shadow(s.clone()).is_err(), "duplicate id");
    assert!(
        eng.amend_shadow(&s.shadow_id, now, s.qty).is_err(),
        "an amend must reduce the qty"
    );
    assert!(eng.cancel_shadow("nope", now).is_err(), "unknown shadow");
    eng.cancel_shadow(&s.shadow_id, now + 5).unwrap();
    assert!(eng.cancel_shadow(&s.shadow_id, now + 6).is_err(), "second cancel");
}

/// `Session::open_replay` on a dataset with two instruments: sending each shadow, cancel and amend at its send time
/// (advancing the session to it first) gives the fills and results of one `run_dataset` over the same orders.
#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the testkit crate")]
fn session_orders_match_run_dataset() {
    use qt_schema::codec::{TableRow, batch_to_shadow_fills, shadow_amends_to_batch, shadow_specs_to_batch};
    use qt_schema::rows::{PriceSpec, ShadowFillRow, ShadowResultRow};

    let mut cfg = qt_testkit::SynthConfig::for_venue(Venue::Cme, 5);
    cfg.instruments = 2;
    cfg.events_per_instrument = 1500;
    let out = qt_testkit::generate(&cfg);
    let root = std::env::temp_dir().join(format!("qt-replay-session-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    qt_testkit::write_dataset(&out, &root).unwrap();
    let (mut shadows, amends) = random_shadows(5, &out.events, 120);
    for (i, s) in shadows.iter_mut().enumerate() {
        s.instrument_id = 1 + (i % 2) as u32;
        let offset = match s.price {
            PriceSpec::Touch { offset_ticks } => offset_ticks,
            PriceSpec::Abs(p) => (p - 1_000) as i32,
        };
        s.price = PriceSpec::Touch { offset_ticks: offset };
    }
    shadows.sort_by_key(|s| s.ts_submit);
    let spec = |extra: serde_json::Value| -> qt_schema::specs::ReplaySpec {
        let mut v = serde_json::json!({
            "dataset": {"path": root.display().to_string()}, "bbo": "none", "seed": 5,
            "latency": {"feed_ns": 1000, "decision_ns": 500, "gateway_ns": 700, "jitter_ns": 50000},
        });
        v.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        serde_json::from_value(v).unwrap()
    };
    let write = |name: &str, table: &str, batch: arrow_array::RecordBatch| {
        let p = root.join(name);
        let mut w = qt_schema::io::TableWriter::create(&p, table).unwrap();
        w.write(&batch).unwrap();
        w.finish().unwrap();
        p.display().to_string()
    };
    let sh = write("shadows.parquet", "qt.shadow_spec", shadow_specs_to_batch(&shadows));
    let am = write("amends.parquet", "qt.shadow_amend", shadow_amends_to_batch(&amends));
    let run_out = root.join("run");
    qt_replay::run_dataset(&spec(serde_json::json!({
        "out": run_out.display().to_string(), "shadows": {"path": sh}, "amends": {"path": am},
    })))
    .unwrap();
    let read = |name: &str, table: &str| -> Vec<arrow_array::RecordBatch> {
        qt_schema::io::read_table_as(&run_out.join(name), table)
            .unwrap()
            .map(Result::unwrap)
            .collect()
    };
    let mut want_fills: Vec<ShadowFillRow> = read("shadow_fills.parquet", "qt.shadow_fills")
        .iter()
        .flat_map(|b| batch_to_shadow_fills(b).unwrap())
        .collect();
    let mut want_results: Vec<ShadowResultRow> = read("shadow_results.parquet", "qt.shadow_results")
        .iter()
        .flat_map(|b| ShadowResultRow::from_batch(b).unwrap())
        .collect();

    let mut ops: Vec<Op> = Vec::new();
    let mut later = Vec::new();
    for s in &shadows {
        let mut s = s.clone();
        if let Some(t) = s.ts_cancel.take() {
            later.push(Op::Cancel(s.shadow_id.clone(), t));
        }
        ops.push(Op::Add(s));
    }
    ops.extend(later);
    ops.extend(
        amends
            .iter()
            .map(|a| Op::Amend(a.shadow_id.clone(), a.ts_send, a.new_qty)),
    );
    ops.sort_by_key(Op::at);
    let mut session = qt_replay::Session::open_replay(&spec(serde_json::json!({}))).unwrap();
    let mut fills = Vec::new();
    let mut results = Vec::new();
    for op in ops {
        session.advance_to(op.at()).unwrap();
        match op {
            Op::Add(s) => session.submit(s),
            Op::Cancel(id, t) => session.cancel(&id, t),
            Op::Amend(id, t, q) => session.amend(&id, t, q),
        }
        .unwrap();
        fills.extend(batch_to_shadow_fills(&session.take_fills().unwrap()).unwrap());
    }
    session.finish().unwrap();
    fills.extend(batch_to_shadow_fills(&session.take_fills().unwrap()).unwrap());
    results.extend(ShadowResultRow::from_batch(&session.take_results().unwrap()).unwrap());
    assert!(session.advance_to(i64::MAX - 1).is_err(), "finished");

    let fill_key = |f: &ShadowFillRow| (f.shadow_id.clone(), f.fill_seq);
    for v in [&mut want_fills, &mut fills] {
        v.sort_by_key(fill_key);
    }
    for v in [&mut want_results, &mut results] {
        v.sort_by(|a, b| a.shadow_id.cmp(&b.shadow_id));
    }
    assert_eq!(results.len(), shadows.len());
    assert!(!fills.is_empty());
    assert_eq!(fills, want_fills);
    assert_eq!(results, want_results);
    std::fs::remove_dir_all(&root).ok();
}
