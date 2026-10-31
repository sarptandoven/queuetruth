//! Ingest-recorded trading halts (`data_quality.json` `trading_halts`, SPEC 1.2) in `zoo`: the partition's spans
//! are set on the exact engine exactly as replay does, and the engine's censoring ends every model arm too, so no
//! model books a fill while the instrument is not in continuous trading (finding F2 for the L2 models: the
//! touch and bound models filled through halts as the exact arm did).

use std::collections::{BTreeMap, BTreeSet};

use arrow_array::Array;
use qt_schema::codec::TableRow;
use qt_schema::rows::{EndState, PriceSpec, ShadowResultRow, ShadowSource, ShadowSpec};
use qt_types::{BookSide, Venue, flags};

struct Case {
    root: std::path::PathBuf,
    ds: std::path::PathBuf,
    shadows: std::path::PathBuf,
    /// The halt `[start, end)`.
    halt: (i64, i64),
    /// Shadows whose `ts_live` falls in the halt.
    inside: BTreeSet<String>,
}

/// A one-instrument CME testkit dataset with an ingest-recorded halt over its middle tenth (when `with_halt`), and
/// shadows at and near the touch that are live into the halt, plus shadows submitted during it.
fn case(tag: &str, with_halt: bool) -> Case {
    let root = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("qt_halts_{tag}"));
    let _ = std::fs::remove_dir_all(&root);
    let mut cfg = qt_testkit::SynthConfig::for_venue(Venue::Cme, 17);
    cfg.instruments = 1;
    cfg.events_per_instrument = 3_000;
    let out = qt_testkit::generate(&cfg);
    let ds = root.join("ds");
    qt_testkit::write_dataset(&out, &ds).unwrap();
    let live: Vec<i64> = out
        .events
        .iter()
        .filter(|e| e.flags & flags::SNAPSHOT == 0)
        .map(|e| e.ts_event)
        .collect();
    let n = live.len();
    let halt = (live[n / 2], live[n / 2 + n / 10]);
    if with_halt {
        let p = ds.join("data_quality.json");
        let mut dq: qt_schema::json::DataQuality = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        dq.trading_halts.push(qt_schema::json::TradingHaltSpan {
            instrument_id: 1,
            ts_start: halt.0,
            ts_end: halt.1,
            state: "itch_H:H".into(),
            reason: "T1".into(),
        });
        std::fs::write(&p, serde_json::to_string(&dq).unwrap()).unwrap();
    }
    let mut specs = Vec::new();
    let mut inside = BTreeSet::new();
    for i in 0..36usize {
        let side = if i % 2 == 0 { BookSide::Bid } else { BookSide::Ask };
        // 0..24 join the touch before the halt; 24..36 are submitted during it.
        let ts = if i < 24 {
            live[n / 2 - 300 + 10 * i]
        } else {
            live[n / 2 + 20 * (i - 23)]
        };
        let id = format!("h{i:02}");
        if ts >= halt.0 && ts < halt.1 {
            inside.insert(id.clone());
        }
        specs.push(ShadowSpec {
            shadow_id: id,
            instrument_id: 1,
            side,
            price: PriceSpec::Touch {
                offset_ticks: -((i % 3) as i32),
            },
            qty: 1,
            ts_submit: ts,
            latency_ns: 0,
            ts_cancel: None,
            group: None,
            source: ShadowSource::User,
            order_key: None,
        });
    }
    assert_eq!(inside.len(), 12);
    let shadows = root.join("shadows.parquet");
    let mut w = qt_schema::io::TableWriter::create(&shadows, "qt.shadow_spec").unwrap();
    w.write(&qt_schema::codec::shadow_specs_to_batch(&specs)).unwrap();
    w.finish().unwrap();
    Case {
        root,
        ds,
        shadows,
        halt,
        inside,
    }
}

fn replay_ends(c: &Case) -> BTreeMap<String, EndState> {
    let out = c.root.join("replay");
    let spec: qt_schema::specs::ReplaySpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": c.ds.display().to_string()}, "out": out.display().to_string(),
        "shadows": {"path": c.shadows.display().to_string()}, "bbo": "none", "trajectory": "ahead"
    }))
    .unwrap();
    qt_replay::run_dataset(&spec).unwrap();
    let mut ends = BTreeMap::new();
    for b in qt_schema::io::read_table_as(&out.join("shadow_results.parquet"), "qt.shadow_results").unwrap() {
        for r in ShadowResultRow::from_batch(&b.unwrap()).unwrap() {
            ends.insert(r.shadow_id, r.end_state);
        }
    }
    ends
}

/// (exact end state by shadow, model fills as (model, shadow, ts)).
#[allow(clippy::type_complexity)]
fn zoo(c: &Case) -> (BTreeMap<String, Option<String>>, Vec<(String, String, i64)>) {
    let out = c.root.join("zoo");
    let spec: qt_schema::specs::ZooSpec = serde_json::from_value(serde_json::json!({
        "dataset": {"path": c.ds.display().to_string()}, "shadows": {"path": c.shadows.display().to_string()},
        "out": out.display().to_string(),
        "models": ["front_of_queue", "hbt_risk_adverse", "nt_prob_touch[p=1]"],
        "horizons_ns": [1_000_000_000_i64, 10_000_000_000_i64], "h_max_ns": 60_000_000_000_i64,
    }))
    .unwrap();
    qt_zoo::run_zoo(&spec).unwrap();
    let mut ends = BTreeMap::new();
    for b in qt_schema::io::read_table_as(&out.join("zoo_shadows.parquet"), "qt.zoo_shadows").unwrap() {
        let b = b.unwrap();
        let ids = b.column_by_name("shadow_id").unwrap().clone();
        let ids = ids.as_any().downcast_ref::<arrow_array::StringArray>().unwrap();
        let st = b.column_by_name("exact_end_state").unwrap().clone();
        let st = st.as_any().downcast_ref::<arrow_array::StringArray>().unwrap();
        for i in 0..b.num_rows() {
            ends.insert(
                ids.value(i).to_owned(),
                (!st.is_null(i)).then(|| st.value(i).to_owned()),
            );
        }
    }
    let mut fills = Vec::new();
    for b in qt_schema::io::read_table_as(&out.join("zoo_fills.parquet"), "qt.zoo_fills").unwrap() {
        for r in qt_schema::codec::batch_to_zoo_fills(&b.unwrap()).unwrap() {
            fills.push((r.model_id, r.shadow_id, r.ts_event));
        }
    }
    (ends, fills)
}

#[test]
#[cfg_attr(not(feature = "gate"), ignore = "gate test")]
fn trading_halt_censors_zoo_truth_and_models_like_replay() {
    let c = case("halt", true);
    let replay = replay_ends(&c);
    let (ends, fills) = zoo(&c);
    // The exact arm's end state of every shadow is replay's.
    assert_eq!(ends.len(), replay.len());
    for (id, state) in &replay {
        assert_eq!(ends[id].as_deref(), Some(state.to_string().as_str()), "{id}");
    }
    // Shadows submitted during the halt never go live: censored in the exact arm, no model fill at all.
    for id in &c.inside {
        assert_eq!(replay[id], EndState::CensoredGap, "{id}");
        let f: Vec<_> = fills.iter().filter(|(_, s, _)| s == id).collect();
        assert!(f.is_empty(), "{id} filled by a model: {f:?} halt {:?}", c.halt);
    }
    // Shadows live at the halt that had not filled are censored there.
    let censored_live = replay
        .iter()
        .filter(|(id, s)| **s == EndState::CensoredGap && !c.inside.contains(*id))
        .count();
    assert!(censored_live >= 4, "{replay:?}");
    // No model books a fill while the instrument is halted.
    let during: Vec<_> = fills
        .iter()
        .filter(|(_, _, ts)| *ts > c.halt.0 && *ts < c.halt.1)
        .collect();
    assert!(during.is_empty(), "{during:?}");

    // Without the halt the same shadows fill during that time (the defect the halt removes), and none is
    // censored.
    let clean = case("clean", false);
    let replay0 = replay_ends(&clean);
    assert!(replay0.values().all(|s| *s != EndState::CensoredGap), "{replay0:?}");
    let (_, fills0) = zoo(&clean);
    let models_during: BTreeSet<&str> = fills0
        .iter()
        .filter(|(_, _, ts)| *ts > clean.halt.0 && *ts < clean.halt.1)
        .map(|(m, _, _)| m.as_str())
        .collect();
    assert!(
        models_during.iter().any(|m| m.starts_with("front_of_queue")),
        "the upper bound fills during the would-be halt without it: {models_during:?}"
    );
}
