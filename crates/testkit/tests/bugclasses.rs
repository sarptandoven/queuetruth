//! Referee bug-class fixtures (SPEC 5.8): designed exact outcomes and the written layout.

#![allow(clippy::type_complexity)]

use std::path::PathBuf;

use qt_schema::codec::{batch_to_shadow_fills, batch_to_shadow_results, batch_to_shadow_specs};
use qt_schema::io::read_table_as;
use qt_schema::json::DatasetManifest;
use qt_schema::rows::{EndState, FillReason};
use qt_testkit::bugclasses::{all, write_fixtures};

#[test]
fn bugclasses_exact_outcomes() {
    let fx = all().expect("fixtures");
    assert_eq!(fx.len(), 16);
    let outcome: Vec<(String, u64, EndState, Vec<(u64, FillReason)>)> = fx
        .iter()
        .map(|f| {
            let (r, fills, _) = &f.expected[0];
            (
                f.name.clone(),
                r.filled_qty,
                r.end_state,
                fills.iter().map(|x| (x.fill_qty, x.reason)).collect(),
            )
        })
        .collect();
    use EndState::*;
    use FillReason::*;
    let want: Vec<(&str, u64, EndState, Vec<(u64, FillReason)>)> = vec![
        ("01_unrelated_delete_keeps_position", 0, OpenAtEnd, vec![]),
        ("02_level_shrink_caps_queue", 5, OpenAtEnd, vec![(5, QueueDepleted)]),
        ("03_distinct_trades_each_consume", 10, Filled, vec![(10, QueueDepleted)]),
        ("04_post_trade_book_decides", 15, OpenAtEnd, vec![(15, Crossed)]),
        (
            "05_partial_fills_sum_to_cum",
            30,
            Filled,
            vec![(10, QueueDepleted), (15, QueueDepleted), (5, QueueDepleted)],
        ),
        (
            "06_fill_equal_to_leaves_completes",
            10,
            Filled,
            vec![(4, QueueDepleted), (6, QueueDepleted)],
        ),
        (
            "07_size_increase_loses_priority",
            5,
            OpenAtEnd,
            vec![(5, QueueDepleted)],
        ),
        ("08_price_change_loses_priority", 5, OpenAtEnd, vec![(5, QueueDepleted)]),
        ("09_hidden_print_no_queue", 0, OpenAtEnd, vec![]),
        ("10_trade_through_fills", 10, Filled, vec![(10, TradeThrough)]),
        (
            "11_fill_before_cancel_effective_stands",
            10,
            Filled,
            vec![(10, QueueDepleted)],
        ),
        (
            "12_hidden_execution_at_price_after_queue",
            30,
            Filled,
            vec![(20, QueueDepleted), (10, TradeThrough)],
        ),
        ("13_execution_without_print", 5, OpenAtEnd, vec![(5, QueueDepleted)]),
        ("14_off_price_print_not_trade_through", 0, OpenAtEnd, vec![]),
        (
            "15_cme_same_size_modify_loses_priority",
            5,
            OpenAtEnd,
            vec![(5, QueueDepleted)],
        ),
        (
            "16_crossed_at_live_takes_opposite",
            25,
            OpenAtEnd,
            vec![(10, CrossedAtLive), (15, CrossedAtLive)],
        ),
    ];
    assert_eq!(outcome.len(), want.len());
    let x16 = &fx[15].expected[0].1;
    assert_eq!((x16[0].fill_px, x16[1].fill_px), (5_000_250_000_000, 5_000_500_000_000));
    let x14 = &fx[13].expected[0];
    assert_eq!(
        x14.2
            .iter()
            .find(|t| t.kind == qt_schema::rows::TrajectoryKind::Event)
            .map(|t| (t.queue_ahead_after, t.trade_through_qty)),
        Some((40, 0))
    );
    for (got, w) in outcome.iter().zip(&want) {
        assert_eq!(
            (got.0.as_str(), got.1, got.2, got.3.clone()),
            (w.0, w.1, w.2, w.3.clone())
        );
    }
    for f in &fx {
        let (r, fills, _) = &f.expected[0];
        assert!(!r.out_of_session, "{}", f.name);
        // Cumulative quantities sum (hbt #316) and leaves reach 0 exactly on completion (hbt #312).
        let mut cum = 0;
        for x in fills {
            cum += x.fill_qty;
            assert_eq!((x.cum_qty, x.leaves_qty), (cum, r.qty - cum));
        }
        assert!(f.readme.len() > 100);
    }
    let race = &fx[10].expected[0].0;
    assert_eq!(
        (
            race.ts_cancel_effective,
            race.ts_first_fill.map(|t| t < race.ts_cancel_effective.unwrap_or(0))
        ),
        (race.ts_cancel_effective, Some(true))
    );
}

#[test]
fn bugclasses_written_layout() {
    let out = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("qt-testkit-ref-{}", std::process::id()));
    let s = write_fixtures(&out).expect("write");
    assert_eq!(s.fixtures.len(), 16);
    for f in all().expect("fixtures") {
        let d = out.join(&f.name);
        for name in [
            "schedule.parquet",
            "expected_results.parquet",
            "expected_fills.parquet",
            "README.txt",
            "dataset/manifest.json",
        ] {
            assert!(d.join(name).is_file(), "{}/{name}", f.name);
        }
        let m: DatasetManifest =
            serde_json::from_slice(&std::fs::read(d.join("dataset/manifest.json")).expect("read")).expect("parse");
        assert_eq!(m.partitions.len(), 1);
        let sched: Vec<_> = read_table_as(&d.join("schedule.parquet"), "qt.shadow_spec")
            .expect("open")
            .flat_map(|b| batch_to_shadow_specs(&b.expect("batch")).expect("decode"))
            .collect();
        assert_eq!(sched, f.schedule);
        let res: Vec<_> = read_table_as(&d.join("expected_results.parquet"), "qt.shadow_results")
            .expect("open")
            .flat_map(|b| batch_to_shadow_results(&b.expect("batch")).expect("decode"))
            .collect();
        assert_eq!(res, vec![f.expected[0].0.clone()]);
        let fills: Vec<_> = read_table_as(&d.join("expected_fills.parquet"), "qt.shadow_fills")
            .expect("open")
            .flat_map(|b| batch_to_shadow_fills(&b.expect("batch")).expect("decode"))
            .collect();
        assert_eq!(fills, f.expected[0].1);
    }
    assert!(out.join("MANIFEST.sha256").is_file());
    std::fs::remove_dir_all(out).ok();
}
