//! Ingest-recorded feed gaps (`data_quality.json`) in calibration collection: with record-level
//! sequence-gap detection off (multi-instrument feeds carry one global sequence), the partition's ingest gaps are set on the exact
//! engine exactly as replay does, so a shadow live across a gap leaves the collection at replay's `censored_gap`
//! time (no allocation or touch rows from then on).

use qt_calib::collect::{CollectConfig, collect_dataset, nt_tracker_id};

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test")]
fn feed_gap_stops_collection_like_replay() {
    let nt = qt_zoo::build(&nt_tracker_id(qt_schema::specs::ModeName::Faithful).unwrap_or_else(|e| panic!("{e}")))
        .unwrap_or_else(|e| panic!("{e}"));
    let cfg = CollectConfig {
        l2: qt_l2::L2Config::default(),
        seed: 0,
        faithful: true,
    };
    let run = |c: &GapCase| {
        let ds = qt_ingest::Dataset::open(&c.root).unwrap_or_else(|e| panic!("{e}"));
        let shadows = qt_schema::codec::read_shadow_specs(&c.shadows).unwrap_or_else(|e| panic!("{e}"));
        let col = collect_dataset(&ds, shadows, nt.as_ref(), &cfg, 1).unwrap_or_else(|e| panic!("{e}"));
        // Last row time per shadow (allocation and touch rows).
        let mut last: std::collections::BTreeMap<String, i64> = std::collections::BTreeMap::new();
        for (id, t) in col
            .alloc
            .iter()
            .map(|r| (&r.shadow_id, r.ts_event))
            .chain(col.touch.iter().map(|r| (&r.shadow_id, r.ts_event)))
        {
            let e = last.entry(id.clone()).or_insert(t);
            *e = (*e).max(t);
        }
        let live: Vec<String> = col.shadows.iter().map(|s| s.shadow_id.clone()).collect();
        (last, live)
    };
    let gap = gap_case("calib_gap", true);
    let (last, live) = run(&gap);
    let censored = gap_censored(&gap);
    for (id, t) in &censored {
        assert!(live.contains(id), "{id}");
        if let Some(l) = last.get(id) {
            assert!(l < t, "{id}: a row at {l} after replay's censored_gap time {t}");
        }
    }
    // Control: without the ingest gap the same shadows keep producing rows past that time.
    let clean = gap_case("calib_clean", false);
    let (last0, live0) = run(&clean);
    assert_eq!(live0, live);
    assert!(
        censored.iter().any(|(id, t)| last0.get(id).is_some_and(|l| l >= t)),
        "the gap must matter for at least one shadow: {last0:?} vs {censored:?}"
    );
}

// ---- shared fixture (same in zoo, calib and features `tests/feed_gaps.rs`) ---------------------------------------

/// A one-instrument CME testkit dataset with shadows live across its middle, optionally with an ingest-recorded
/// feed gap there (`data_quality.json`, as ingest writes it), and replay's verdict on the same data.
struct GapCase {
    root: std::path::PathBuf,
    shadows: std::path::PathBuf,
    /// Replay: shadow id -> (end state, ts of its trajectory `end` row).
    replay: std::collections::BTreeMap<String, (qt_schema::rows::EndState, i64)>,
}

fn gap_case(tag: &str, with_gap: bool) -> GapCase {
    use qt_schema::codec::TableRow;
    use qt_schema::rows::{PriceSpec, ShadowResultRow, ShadowSource, ShadowSpec, TrajectoryKind, TrajectoryRow};
    use qt_types::{BookSide, Venue, flags};

    let root = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("qt_feed_gap_{tag}"));
    let _ = std::fs::remove_dir_all(&root);
    let mut cfg = qt_testkit::SynthConfig::for_venue(Venue::Cme, 11);
    cfg.instruments = 1;
    cfg.events_per_instrument = 3_000;
    let out = qt_testkit::generate(&cfg);
    let ds = root.join("ds");
    qt_testkit::write_dataset(&out, &ds).unwrap_or_else(|e| panic!("{e}"));
    let live: Vec<i64> = out
        .events
        .iter()
        .filter(|e| e.flags & flags::SNAPSHOT == 0)
        .map(|e| e.ts_event)
        .collect();
    let mid = live[live.len() / 2];
    if with_gap {
        let p = ds.join("data_quality.json");
        let mut dq: qt_schema::json::DataQuality =
            serde_json::from_str(&std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{e}")))
                .unwrap_or_else(|e| panic!("{e}"));
        dq.feed_gaps.push(qt_schema::json::FeedGap {
            instrument_id: 1,
            ts_start: mid,
            ts_end: mid + 1,
            reason: "sequence gap (test)".into(),
        });
        std::fs::write(&p, serde_json::to_string(&dq).unwrap_or_default()).unwrap_or_else(|e| panic!("{e}"));
    }
    // Touch shadows joining the best (behind the queue) shortly before the gap, plus deep resting ones from the
    // start: every one is live across the gap unless it filled first.
    let mut specs = Vec::new();
    for i in 0..24usize {
        let side = if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask };
        let (price, ts) = if i < 18 {
            (
                PriceSpec::Touch {
                    offset_ticks: -((i % 3) as i32),
                },
                live[live.len() / 2 - 400 + 20 * i],
            )
        } else {
            let d = 200 * cfg.tick;
            (
                PriceSpec::Abs(if side == BookSide::Bid {
                    cfg.base_px - d
                } else {
                    cfg.base_px + d
                }),
                live[0] + 1,
            )
        };
        specs.push(ShadowSpec {
            shadow_id: format!("g{i:02}"),
            instrument_id: 1,
            side,
            price,
            qty: 1,
            ts_submit: ts,
            latency_ns: 0,
            ts_cancel: None,
            group: None,
            source: ShadowSource::User,
            order_key: None,
        });
    }
    let shadows = root.join("shadows.parquet");
    let mut w = qt_schema::io::TableWriter::create(&shadows, "qt.shadow_spec").unwrap_or_else(|e| panic!("{e}"));
    w.write(&qt_schema::codec::shadow_specs_to_batch(&specs))
        .unwrap_or_else(|e| panic!("{e}"));
    w.finish().unwrap_or_else(|e| panic!("{e}"));
    let rout = root.join("replay");
    let spec: qt_schema::specs::ReplaySpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": ds.display().to_string()}, "out": rout.display().to_string(),
        "shadows": {"path": shadows.display().to_string()}, "bbo": "none", "trajectory": "ahead"
    }))
    .unwrap_or_else(|e| panic!("{e}"));
    let sum = qt_replay::run_dataset(&spec).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(sum.data_quality.feed_gaps.len(), usize::from(with_gap));
    let mut ends = std::collections::BTreeMap::new();
    for b in qt_schema::io::read_table_as(&rout.join("queue_trajectory.parquet"), "qt.queue_trajectory")
        .unwrap_or_else(|e| panic!("{e}"))
    {
        for r in TrajectoryRow::from_batch(&b.unwrap_or_else(|e| panic!("{e}"))).unwrap_or_else(|e| panic!("{e}")) {
            if r.kind == TrajectoryKind::End {
                ends.insert(r.shadow_id, r.ts_event);
            }
        }
    }
    let mut replay = std::collections::BTreeMap::new();
    for b in qt_schema::io::read_table_as(&rout.join("shadow_results.parquet"), "qt.shadow_results")
        .unwrap_or_else(|e| panic!("{e}"))
    {
        for r in ShadowResultRow::from_batch(&b.unwrap_or_else(|e| panic!("{e}"))).unwrap_or_else(|e| panic!("{e}")) {
            let t = ends.get(&r.shadow_id).copied().unwrap_or(i64::MAX);
            replay.insert(r.shadow_id, (r.end_state, t));
        }
    }
    let n_gap = replay
        .values()
        .filter(|(s, _)| *s == qt_schema::rows::EndState::CensoredGap)
        .count();
    if with_gap {
        assert!(n_gap >= 6, "replay censors the shadows live across the gap: {replay:?}");
    } else {
        assert_eq!(n_gap, 0, "{replay:?}");
    }
    GapCase {
        root: ds,
        shadows,
        replay,
    }
}

/// Replay's `censored_gap` shadows and their censoring time.
fn gap_censored(c: &GapCase) -> Vec<(String, i64)> {
    c.replay
        .iter()
        .filter(|(_, (s, _))| *s == qt_schema::rows::EndState::CensoredGap)
        .map(|(id, (_, t))| (id.clone(), *t))
        .collect()
}
