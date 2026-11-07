//! Landmark feature and outcome extraction for the learned fill model (SPEC 4.1 `qt_features`, 5.5.1, 5.5.2), for
//! shadows or real orders, written as `qt.features` (`<out>/features.parquet`).
//!
//! Tracker columns follow `FeatureSpec.trackers`: `f_<family>` plus `_<key><value>` per numeric parameter (e.g.
//! `hbt_power3[n=1]` -> `f_hbt_power3_n1`); with the default trackers the columns are exactly `FEATURES`.
//! `queue_share_ra` uses the `hbt_risk_adverse` tracker (null when it is not among the trackers).
//!
//! Schema 1.1: the file has the full `qt.features` layout, whose last column `midnight_utc_ns` is the instrument-day's
//! local midnight in UTC (`qt.instruments`), so `ts_landmark - midnight_utc_ns` is the exact exchange-local time of
//! day. `cancel_away_ticks` null or 0 disables the shadows' policy cancel.

#![forbid(unsafe_code)]

pub mod extract;
pub mod market;
pub mod order;

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use qt_ingest::{Dataset, Partition};
use qt_schema::io::TableWriter;
use qt_schema::rows::{Cause, FeatureRow, RealOrderRow};
use qt_schema::specs::PopulationName;
use qt_schema::{codec, json, specs};
use qt_types::{QtError, QtResult, Xoshiro256StarStar, stable_seed};
use qt_zoo::{Family, ModelId, QueueModel};
use serde_json::Value;

use crate::extract::{ExtractConfig, PopOrder, extract_events_with_spans};

/// Feature columns of SPEC 5.5.2 in column order (`side_sign` is a fixed column of `qt.features`).
pub const FEATURES: &[&str] = &[
    "dist_ticks",
    "spread_ticks",
    "level_qty",
    "level_ct",
    "own_qty_lots",
    "f_hbt_risk_adverse",
    "f_hbt_power3_n1",
    "f_hbt_power3_n3",
    "f_hbt_log2",
    "queue_share_ra",
    "elapsed_ns",
    "traded_at_px_since_entry",
    "unexplained_dec_since_entry",
    "imb_l1",
    "imb_l3",
    "wmid_offset_ticks",
    "same_sz_l1",
    "same_sz_l2",
    "same_sz_l3",
    "opp_sz_l1",
    "opp_sz_l2",
    "opp_sz_l3",
    "ofi_1s",
    "ofi_10s",
    "trade_vol_px_10s",
    "trade_vol_side_10s",
    "trade_vol_all_60s",
    "dec_rate_px_10s",
    "vol_ticks_60s",
    "bbo_changes_10s",
    "tod_sin",
    "tod_cos",
    "log_mid_over_tick",
];

/// Privileged (exact-book) columns, written only when `privileged = true`.
pub const PRIVILEGED: &[&str] = &["p_f_exact", "p_rank_exact", "p_level_qty_exact"];

const TRACKER_AT: usize = 5;
const DEFAULT_TRACKERS: usize = 4;

fn family_name(id: &ModelId) -> String {
    let s = id.to_string();
    s.split('[').next().unwrap_or_default().to_owned()
}

/// Column name of one tracker (`f_<family>[_<key><value>...]`, `.` -> `p`, `-` -> `m`).
pub fn tracker_column(id: &ModelId) -> String {
    let mut s = format!("f_{}", family_name(id));
    for (k, v) in id.params.iter().filter(|(k, _)| k.as_str() != "particles") {
        let v = format!("{v}").replace('.', "p").replace('-', "m");
        s.push_str(&format!("_{k}{v}"));
    }
    s
}

/// Feature columns for a tracker list (`FEATURES` with the tracker columns replaced).
pub fn feature_names(trackers: &[ModelId]) -> Vec<String> {
    let mut v: Vec<String> = FEATURES[..TRACKER_AT].iter().map(|s| (*s).to_owned()).collect();
    v.extend(trackers.iter().map(tracker_column));
    v.extend(
        FEATURES[TRACKER_AT + DEFAULT_TRACKERS..]
            .iter()
            .map(|s| (*s).to_owned()),
    );
    v
}

fn parse_trackers(spec: &specs::FeatureSpec) -> QtResult<Vec<ModelId>> {
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for t in &spec.trackers {
        let id: ModelId = t
            .parse()
            .map_err(|e| QtError::Config(format!("trackers: {t:?}: {e}")))?;
        if !seen.insert(tracker_column(&id)) {
            return Err(QtError::Config(format!("trackers: duplicate tracker column for {t:?}")));
        }
        out.push(id);
    }
    Ok(out)
}

fn dataset_path(d: &specs::DatasetRef) -> QtResult<&Path> {
    d.as_path()
        .map(Path::new)
        .ok_or_else(|| QtError::Config("dataset: local runs need {\"path\": ...}, not a service dataset_id".into()))
}

/// Extraction settings from a spec.
pub fn extract_config(spec: &specs::FeatureSpec, trackers: &[ModelId]) -> QtResult<ExtractConfig> {
    if spec.landmarks_ns.iter().any(|l| *l < 0) {
        return Err(QtError::Config("landmarks_ns: must be >= 0".into()));
    }
    if spec.h_max_ns <= 0 {
        return Err(QtError::Config("h_max_ns: must be > 0".into()));
    }
    let l2: qt_l2::L2Config = (&spec.l2).into();
    l2.validate()?;
    Ok(ExtractConfig {
        l2,
        landmarks_ns: spec.landmarks_ns.clone(),
        // null or 0 = never cancel (1.1)
        cancel_away_ticks: spec.cancel_away_ticks.unwrap_or(0),
        h_max_ns: spec.h_max_ns,
        privileged: spec.privileged,
        seed: spec.seed,
        ra_index: trackers.iter().position(|t| t.family == Family::HbtRiskAdverse),
    })
}

/// Real-order lives of every partition (pass 1), in partition then emission order.
fn real_order_lives(ds: &Dataset, threads: usize, seed: u64) -> QtResult<Vec<Vec<RealOrderRow>>> {
    struct Lives(Vec<RealOrderRow>);
    impl qt_replay::Sink for Lives {
        fn real_order(&mut self, r: &RealOrderRow) -> QtResult<()> {
            self.0.push(*r);
            Ok(())
        }
    }
    let mut cfg = qt_replay::EngineConfig::new(ds.rules());
    cfg.real_orders = true;
    cfg.bbo = qt_replay::BboMode::None;
    cfg.trajectory = qt_replay::TrajectoryMode::None;
    cfg.seed = seed;
    qt_replay::for_each_partition(ds.partitions(), threads, |p| {
        let mut s = Lives(Vec::new());
        qt_replay::run_partition(p, ds, &cfg, Vec::new(), Vec::new(), Vec::new(), &mut s)?;
        Ok(s.0.into_iter().filter(|r| !r.in_snapshot).collect())
    })
}

/// Seeded selection of `n` of `total` positions (partial Fisher-Yates on
/// `Xoshiro256StarStar::new(stable_seed(seed, &[b"features_real_orders"]))`), returned sorted.
pub fn sample_positions(total: usize, n: usize, seed: u64) -> Vec<usize> {
    if n >= total {
        return (0..total).collect();
    }
    let mut rng = Xoshiro256StarStar::new(stable_seed(seed, &[b"features_real_orders"]));
    let mut v: Vec<usize> = (0..total).collect();
    for i in 0..n {
        let j = rng.range_inclusive(i as u64, (total - 1) as u64) as usize;
        v.swap(i, j);
    }
    let mut s = v[..n].to_vec();
    s.sort_unstable();
    s
}

fn populations(spec: &specs::FeatureSpec, ds: &Dataset) -> QtResult<BTreeMap<(i32, u32), Vec<PopOrder>>> {
    let threads = spec.threads.max(1) as usize;
    match spec.population {
        PopulationName::Shadows => {
            let path = spec
                .shadows
                .as_ref()
                .and_then(|f| f.as_path())
                .ok_or_else(|| QtError::Config("shadows: required for population shadows (local path)".into()))?;
            let shadows = codec::read_shadow_specs(Path::new(path))?;
            Ok(qt_replay::split_shadows(ds, shadows)?
                .into_iter()
                .map(|(k, v)| (k, v.into_iter().map(PopOrder::Shadow).collect()))
                .collect())
        }
        PopulationName::RealOrders => {
            let lives = real_order_lives(ds, threads, spec.seed)?;
            let flat: Vec<RealOrderRow> = lives.into_iter().flatten().collect();
            let keep = sample_positions(
                flat.len(),
                spec.n_real_orders.map_or(usize::MAX, |n| n as usize),
                spec.seed,
            );
            let mut m: BTreeMap<(i32, u32), Vec<PopOrder>> = BTreeMap::new();
            for i in keep {
                let r = flat[i];
                m.entry((r.session_date, r.instrument_id))
                    .or_default()
                    .push(PopOrder::Real(r));
            }
            Ok(m)
        }
    }
}

/// Feature rows of a dataset (partition order) with the model instances used.
pub fn extract_rows(
    spec: &specs::FeatureSpec,
    ds: &Dataset,
    models: &[Box<dyn QueueModel>],
    cfg: &ExtractConfig,
) -> QtResult<Vec<FeatureRow>> {
    let pops = populations(spec, ds)?;
    let parts: Vec<Partition> = ds
        .partitions()
        .iter()
        .filter(|p| pops.contains_key(&(p.session_date, p.instrument_id)))
        .cloned()
        .collect();
    let jobs: BTreeMap<(i32, u32), Mutex<Vec<PopOrder>>> = pops.into_iter().map(|(k, v)| (k, Mutex::new(v))).collect();
    // Ingest-recorded feed gaps and trading halts (as qt_replay::run_dataset): shadows live across a gap or at a
    // halt end censored there.
    let dq = qt_replay::ingest_dq(ds);
    let gaps = dq.as_ref().map(|d| d.feed_gaps.clone()).unwrap_or_default();
    let halts = dq.as_ref().map(qt_replay::halts_of).unwrap_or_default();
    let res = qt_replay::for_each_partition(&parts, spec.threads.max(1) as usize, |p| {
        let pop = jobs
            .get(&(p.session_date, p.instrument_id))
            .and_then(|m| m.lock().ok().map(|mut v| std::mem::take(&mut *v)))
            .unwrap_or_default();
        let inst = ds
            .instrument(p.instrument_id, p.session_date)
            .ok_or_else(|| QtError::InputFormat {
                location: format!("instruments.parquet: {}/{}", p.instrument_id, p.session_date),
                msg: "partition without an instrument row".into(),
            })?;
        let part_gaps = qt_replay::partition_gaps(&gaps, p);
        let part_halts = qt_replay::partition_halts(&halts, p, Some(inst.midnight_utc_ns));
        extract_events_with_spans(p.mbo()?, inst, ds.rules(), pop, models, cfg, &part_gaps, &part_halts)
    })?;
    Ok(res.into_iter().flatten().collect())
}

/// Spec fields that only apply to the other population are `QT-CONFIG-001` when set (never silently ignored).
fn check_population_fields(spec: &specs::FeatureSpec) -> QtResult<()> {
    match spec.population {
        PopulationName::Shadows if spec.n_real_orders.is_some() => Err(QtError::Config(
            "n_real_orders: only applies to population real_orders (population is shadows)".into(),
        )),
        PopulationName::RealOrders if spec.shadows.is_some() => Err(QtError::Config(
            "shadows: only applies to population shadows (population is real_orders)".into(),
        )),
        _ => Ok(()),
    }
}

/// Distinct orders among feature rows: `(instrument_id, session_date, order_ref)`, since real-order refs
/// (`"<instrument_id>:<order_id>"`) can repeat on another session date (review m27: multi-day undercount).
pub fn count_orders(rows: &[FeatureRow]) -> u64 {
    rows.iter()
        .map(|r| (r.instrument_id, r.session_date, r.order_ref.as_str()))
        .collect::<BTreeSet<_>>()
        .len() as u64
}

/// `features`.
pub fn extract(spec: &specs::FeatureSpec) -> QtResult<json::FeatureSummary> {
    check_population_fields(spec)?;
    let out = PathBuf::from(
        spec.out
            .as_deref()
            .ok_or_else(|| QtError::Config("out: features needs an output directory".into()))?,
    );
    let ids = parse_trackers(spec)?;
    let cfg = extract_config(spec, &ids)?;
    let models: Vec<Box<dyn QueueModel>> = ids.iter().map(qt_zoo::build).collect::<QtResult<_>>()?;
    let ds = Dataset::open(dataset_path(&spec.dataset)?)?;
    let rows = extract_rows(spec, &ds, &models, &cfg)?;

    let feats = feature_names(&ids);
    let privileged: Vec<String> = if spec.privileged {
        PRIVILEGED.iter().map(|s| (*s).to_owned()).collect()
    } else {
        Vec::new()
    };
    let names: Vec<&str> = feats.iter().chain(&privileged).map(String::as_str).collect();
    let path = out.join("features.parquet");
    let mut w = TableWriter::create_with_schema(&path, qt_schema::features_full_schema(&names)?)?;
    for chunk in rows.chunks(65_536) {
        w.write(&codec::features_to_batch(&names, chunk)?)?;
    }
    if rows.is_empty() {
        w.write(&codec::features_to_batch(&names, &[])?)?;
    }
    let file = w.finish()?;

    let mut by_cause = json::CauseCounts::default();
    for r in &rows {
        match r.cause {
            Cause::Fill => by_cause.fill += 1,
            Cause::Cancel => by_cause.cancel += 1,
            Cause::Censor => by_cause.censor += 1,
        }
    }
    let spec_value = serde_json::to_value(spec).map_err(|e| QtError::Engine(format!("feature spec: {e}")))?;
    Ok(json::FeatureSummary {
        spec: match qt_schema::spec_echo(&spec_value) {
            Value::Object(m) => m,
            _ => serde_json::Map::new(),
        },
        n_orders: count_orders(&rows),
        n_rows: rows.len() as u64,
        by_cause,
        feature_names: feats,
        privileged_names: privileged,
        files: vec![file],
        ..Default::default()
    })
}
