//! Throughput bench (`#[ignore]`, release): `cargo test --locked --release -p queuetruth-replay -- --ignored
//! bench_replay_2m --nocapture`. 2,000,000 generated records of one instrument through the in-memory engine
//! (records already decoded, so this is the engine's ceiling, not Parquet decoding), then the overhead of shadows by
//! count (0 / 3,000 / 100,000, each live 300 s of a ~2,000 s stream) and of the trajectory.

mod common;

use std::time::Instant;

use common::{INST, Market, instrument};
use qt_replay::sinks::{NullSink, VecSink};
use qt_replay::{BboMode, Engine, EngineConfig, TrajectoryMode};
use qt_schema::rows::{PriceSpec, ShadowSource, ShadowSpec};
use qt_types::{BookSide, MboEvent, Venue, Xoshiro256StarStar, rules};

fn shadows(events: &[MboEvent], n: usize) -> Vec<ShadowSpec> {
    let mut r = Xoshiro256StarStar::new(99);
    let (t0, t1) = (events[0].ts_event, events[events.len() - 1].ts_event);
    (0..n)
        .map(|i| {
            let side = if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask };
            let ts_submit = t0 + (r.next_u64() % (t1 - t0) as u64) as i64;
            let price = if i % 3 == 0 {
                PriceSpec::Touch {
                    offset_ticks: (r.next_u64() % 3) as i32 - 2,
                }
            } else {
                let base = if side == BookSide::Bid { 999 } else { 1_001 };
                let d = (r.next_u64() % 4) as i64;
                PriceSpec::Abs(if side == BookSide::Bid { base - d } else { base + d })
            };
            ShadowSpec {
                shadow_id: format!("b{i}"),
                instrument_id: INST,
                side,
                price,
                qty: 1 + r.next_u64() % 20,
                ts_submit,
                latency_ns: 50_000,
                ts_cancel: Some(ts_submit + 300_000_000_000),
                group: None,
                source: ShadowSource::Sampled,
                order_key: None,
            }
        })
        .collect()
}

fn time(label: &str, cfg: &EngineConfig, events: &[MboEvent], sh: Vec<ShadowSpec>, rows: bool) -> f64 {
    let mut best = f64::MAX;
    for _ in 0..3 {
        let mut eng = Engine::new(cfg.clone(), &[instrument(Venue::Cme)], sh.clone(), vec![]).unwrap();
        let t = Instant::now();
        if rows {
            let mut s = VecSink::default();
            for e in events {
                eng.apply(e, &mut s).unwrap();
            }
            eng.finish(&mut s).unwrap();
        } else {
            let mut s = NullSink;
            for e in events {
                eng.apply(e, &mut s).unwrap();
            }
            eng.finish(&mut s).unwrap();
        }
        best = best.min(t.elapsed().as_secs_f64());
    }
    let rps = events.len() as f64 / best;
    println!("{label:<52} {best:>8.3} s {:>8.2} M rec/s", rps / 1e6);
    best
}

#[test]
#[ignore = "throughput bench; run in release with --ignored"]
fn bench_replay_2m() {
    let mut m = Market::new(2024, Venue::Cme);
    while m.out.len() < 2_000_000 {
        m.step();
    }
    let mut events = m.out;
    events.truncate(2_000_000);
    if let Some(l) = events.last_mut() {
        l.flags |= qt_types::flags::LAST;
    }
    let span_s = (events[events.len() - 1].ts_event - events[0].ts_event) as f64 / 1e9;
    println!(
        "records {} events {} span {span_s:.0} s",
        events.len(),
        events.iter().filter(|e| e.flags & 128 != 0).count()
    );
    let mut book_only = EngineConfig::new(rules(Venue::Cme));
    book_only.bbo = BboMode::None;
    book_only.trajectory = TrajectoryMode::None;
    let base = time("book + invariants, no sinks", &book_only, &events, vec![], false);
    let mut change = book_only.clone();
    change.bbo = BboMode::Change;
    time(
        "change-only BBO + trades rows (VecSink)",
        &change,
        &events,
        vec![],
        true,
    );
    let mut oracle = book_only.clone();
    oracle.rank_oracle = true;
    time(
        "rank oracle + rank_trace rows (VecSink)",
        &oracle,
        &events,
        vec![],
        true,
    );
    let mut sh_cfg = book_only.clone();
    sh_cfg.trajectory = TrajectoryMode::None;
    let s3k = time(
        "3,000 shadows, trajectory none",
        &sh_cfg,
        &events,
        shadows(&events, 3_000),
        false,
    );
    let mut ahead = sh_cfg.clone();
    ahead.trajectory = TrajectoryMode::Ahead;
    let s3k_t = time(
        "3,000 shadows, trajectory ahead (rows kept)",
        &ahead,
        &events,
        shadows(&events, 3_000),
        true,
    );
    let s100k = time(
        "100,000 shadows, trajectory none",
        &sh_cfg,
        &events,
        shadows(&events, 100_000),
        false,
    );
    println!(
        "raw counts on one {span_s:.0} s symbol: 3,000 = {:+.1}%  100,000 = {:.2}x  trajectory ahead (3,000) = {:+.1}%",
        (s3k / base - 1.0) * 100.0,
        s100k / base,
        (s3k_t / s3k - 1.0) * 100.0
    );
    // SPEC 7.1 density: N shadows over a 4-symbol, 23,400 s day = N / 4 * span / 23,400 shadows on this stream.
    let eq = |n: f64| ((n / 4.0) * span_s / 23_400.0).round() as usize;
    let (e3k, e100k) = (eq(3_000.0), eq(100_000.0));
    let d3k = time(
        &format!("SPEC density of 3,000 ({e3k} shadows)"),
        &sh_cfg,
        &events,
        shadows(&events, e3k),
        false,
    );
    let d3k_t = time(
        &format!("SPEC density of 3,000 ({e3k}), trajectory ahead"),
        &ahead,
        &events,
        shadows(&events, e3k),
        true,
    );
    let d100k = time(
        &format!("SPEC density of 100,000 ({e100k} shadows)"),
        &sh_cfg,
        &events,
        shadows(&events, e100k),
        false,
    );
    println!(
        "SPEC-density overhead: 3,000 = {:+.1}% (target <= 15%)  100,000 = {:.2}x (reported; SPEC 7.1 gates >= 5M rec/s)  trajectory = {:+.1}% (target <= 10%)",
        (d3k / base - 1.0) * 100.0,
        d100k / base,
        (d3k_t / d3k - 1.0) * 100.0
    );
    // Fixed costs: shadows that never go live, and live shadows far from the market (never touched).
    let mut far = shadows(&events, e3k);
    for s in &mut far {
        s.ts_submit = events[events.len() - 1].ts_event + 1;
    }
    time("31 pending shadows (never live)", &sh_cfg, &events, far, false);
    let mut away = shadows(&events, e3k);
    for (i, s) in away.iter_mut().enumerate() {
        s.ts_submit = events[0].ts_event;
        s.ts_cancel = None;
        s.price = PriceSpec::Abs(if s.side == BookSide::Bid {
            10 + i as i64
        } else {
            1_000_000 + i as i64
        });
    }
    time("31 live shadows far from the market", &sh_cfg, &events, away, false);
    println!(
        "bench_replay_2m: {:.2} M rec/s (book + invariants)",
        events.len() as f64 / base / 1e6
    );
}
