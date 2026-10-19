//! Ingest-recorded feed gaps (`data_quality.json`) in `features`: with record-level sequence-gap detection
//! off (multi-instrument feeds carry one global sequence), the partition's ingest gaps are set on the exact engine exactly as replay
//! does, so a shadow live across a gap is labelled censored at replay's `censored_gap` time.

use qt_schema::rows::Cause;

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the zoo tracker models")]
fn feed_gap_censors_feature_labels_like_replay() {
    let run = |c: &GapCase, out: &str| {
        let spec: qt_schema::specs::FeatureSpec = serde_json::from_value(serde_json::json!({
            "dataset": {"path": c.root.display().to_string()}, "shadows": {"path": c.shadows.display().to_string()},
            "out": c.root.join(out).display().to_string(), "landmarks_ns": [0], "h_max_ns": 86_400_000_000_000_i64,
            "cancel_away_ticks": null, "privileged": false,
        }))
        .unwrap_or_else(|e| panic!("{e}"));
        qt_features::extract(&spec).unwrap_or_else(|e| panic!("{e}"));
        let mut rows = std::collections::BTreeMap::new();
        for b in qt_schema::io::read_table(&c.root.join(out).join("features.parquet")).unwrap_or_else(|e| panic!("{e}"))
        {
            let b = b.unwrap_or_else(|e| panic!("{e}"));
            for r in qt_schema::codec::batch_to_features(&b)
                .unwrap_or_else(|e| panic!("{e}"))
                .1
            {
                rows.insert(r.order_ref.clone(), (r.cause, r.ts_landmark + r.event_ns));
            }
        }
        rows
    };
    let gap = gap_case("features_gap", true);
    let rows = run(&gap, "f");
    let censored = gap_censored(&gap);
    for (id, t) in &censored {
        assert_eq!(
            rows.get(id),
            Some(&(Cause::Censor, *t)),
            "{id}: replay censored_gap at {t}"
        );
    }
    // Every other outcome agrees with replay too (fills at replay's first fill, nothing censored at the gap).
    for (id, (cause, t)) in &rows {
        let (state, end) = gap.replay[id];
        if state == qt_schema::rows::EndState::Filled {
            assert_eq!(*cause, Cause::Fill, "{id}");
        }
        if state != qt_schema::rows::EndState::CensoredGap {
            assert!(!censored.iter().any(|(_, g)| g == t) || *t == end, "{id}");
        }
    }
    // Control: the same data without the ingest gap runs those shadows past it.
    let clean = gap_case("features_clean", false);
    let rows0 = run(&clean, "f");
    for (id, t) in &censored {
        let (_, t0) = rows0[id];
        assert!(t0 > *t, "{id}: without the gap the label ends later ({t0} vs {t})");
    }
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
