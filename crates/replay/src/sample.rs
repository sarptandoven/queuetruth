//! Shadow schedules from a dataset.
//!
//! `sample_shadows`: `n` shadows split over partitions (`equal`: n / P each, the remainder to the first partitions;
//! `proportional`: largest remainder on record counts). Per partition, one stream
//! `Xoshiro256StarStar::new(stable_seed(seed, [b"sample", instrument_id LE, session_date LE]))` draws, per shadow:
//! `ts_submit` uniform in the window `[midnight + open_s + window_open_s, midnight + close_s - window_close_s)`
//! intersected with the partition's `[ts_min, ts_max]` (the whole partition when that is empty), the side, the
//! placement and the quantity rule (weights). Placements are touch-relative (`price_mode touch`); `lots` = `k x
//! lot_size`, `median_order_size` = the median `A` size of the partition rounded down to lots (at least one lot).
//! `ts_cancel = ts_submit + lifetime_ns`. Ids `s<instrument_id>_<YYYYMMDD>_<k>` with `k` in `ts_submit` order.
//!
//! `mirror_real_orders` (SPEC 5.2.10): real lives from the partitions (not `in_snapshot`, not ended `replaced` or
//! `cleared`, which lost or never had a comparable queue position), reservoir-sampled per stratum (instrument-day x
//! {filled, not filled}) with `stable_seed(seed, [b"mirror", instrument_id LE, session_date LE, stratum])`, quotas
//! equal per stratum (`stratify`), else one reservoir per partition. Shadow `r<order_id>` (`_<life>` from the second
//! life of an id), abs price, `qty = size_at_add`, `ts_submit = ts_add`, latency 0, `ts_cancel` = removal time of
//! cancelled lives, source `real_order_mirror`.

use std::collections::HashSet;
use std::path::Path;

use indexmap::IndexMap;
use qt_book::IdMap;
use qt_ingest::Partition;
use qt_schema::codec::shadow_specs_to_batch;
use qt_schema::io::TableWriter;
use qt_schema::json::{Files, ShadowSampleSummary};
use qt_schema::rows::{PriceSpec, RealOrderEnd, RealOrderRow, ShadowSource, ShadowSpec};
use qt_schema::spec_echo;
use qt_schema::specs::{Allocation, MirrorSpec, QtyKind, ShadowSampleSpec, SideName};
use qt_types::{Action, BookSide, QtError, QtResult, Qty, Ts, Xoshiro256StarStar, stable_seed};
use serde_json::Value;

use crate::run::{dataset_path, open_dataset};
use crate::util::{SEC_NS, date_compact};
use crate::{BboMode, Engine, EngineConfig, Sink, TrajectoryMode};

fn echo<T: serde::Serialize>(spec: &T) -> QtResult<serde_json::Map<String, Value>> {
    let v = serde_json::to_value(spec).map_err(|e| QtError::Engine(format!("spec: {e}")))?;
    Ok(match spec_echo(&v) {
        Value::Object(o) => o,
        _ => serde_json::Map::new(),
    })
}

/// Splits `n` over `weights` by largest remainder (ties to the earlier index).
fn allocate(n: u64, weights: &[u64]) -> Vec<u64> {
    let total: u128 = weights.iter().map(|&w| u128::from(w)).sum();
    if weights.is_empty() {
        return Vec::new();
    }
    if total == 0 {
        return allocate(n, &vec![1; weights.len()]);
    }
    let mut out: Vec<u64> = weights
        .iter()
        .map(|&w| (u128::from(n) * u128::from(w) / total) as u64)
        .collect();
    let mut rem: Vec<(u128, usize)> = weights
        .iter()
        .enumerate()
        .map(|(i, &w)| ((u128::from(n) * u128::from(w)) % total, i))
        .collect();
    rem.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    let left = n - out.iter().sum::<u64>();
    for &(_, i) in rem.iter().take(left as usize) {
        out[i] += 1;
    }
    out
}

/// Index drawn by weight (`u` uniform in [0, 1)).
fn pick(weights: &[f64], u: f64) -> usize {
    let total: f64 = weights.iter().sum();
    let mut acc = 0.0;
    for (i, w) in weights.iter().enumerate() {
        acc += w / total;
        if u < acc {
            return i;
        }
    }
    weights.len().saturating_sub(1)
}

fn write_specs(out: &Option<String>, rows: &[ShadowSpec]) -> QtResult<Files> {
    let Some(path) = out else {
        return Ok(Vec::new());
    };
    let path = Path::new(path);
    let mut w = TableWriter::create(path, "qt.shadow_spec")?;
    for chunk in rows.chunks(crate::sinks::CHUNK_ROWS) {
        w.write(&shadow_specs_to_batch(chunk))?;
    }
    if rows.is_empty() {
        w.write(&shadow_specs_to_batch(&[]))?;
    }
    Ok(vec![w.finish()?])
}

fn median_add_size(part: &Partition, lot: Qty) -> QtResult<Qty> {
    let mut sizes = Vec::new();
    for e in part.mbo()? {
        let e = e?;
        if e.action == Action::Add.as_byte() && e.size > 0 {
            sizes.push(e.size);
        }
    }
    if sizes.is_empty() {
        return Ok(lot);
    }
    sizes.sort_unstable();
    let m = sizes[(sizes.len() - 1) / 2];
    Ok((m / lot * lot).max(lot))
}

/// Largest `n` of one `shadows sample` run.
pub const MAX_SAMPLE: u64 = 10_000_000;

/// `shadows sample`.
pub fn sample_shadows(spec: &ShadowSampleSpec) -> QtResult<ShadowSampleSummary> {
    if spec.n > MAX_SAMPLE {
        // Review m22: a huge n is a configuration error, not an allocation panic or an endless loop.
        return Err(QtError::Config(format!(
            "n: at most {MAX_SAMPLE} shadows per sample (got {})",
            spec.n
        )));
    }
    let ds = open_dataset(dataset_path(&spec.dataset)?)?;
    if spec.sides.is_empty() {
        return Err(QtError::Config("sides: at least one side".into()));
    }
    let pw: Vec<f64> = spec.placements.iter().map(|p| p.weight).collect();
    let qw: Vec<f64> = spec.qty.iter().map(|q| q.weight).collect();
    for (name, w) in [("placements", &pw), ("qty", &qw)] {
        if w.is_empty() || w.iter().any(|x| !x.is_finite() || *x < 0.0) || w.iter().sum::<f64>() <= 0.0 {
            return Err(QtError::Config(format!(
                "{name}: weights must be finite, >= 0, with a positive sum"
            )));
        }
    }
    if spec
        .qty
        .iter()
        .any(|q| q.kind == QtyKind::Lots && q.lots.unwrap_or(0) == 0)
    {
        return Err(QtError::Config("qty: kind lots needs lots >= 1".into()));
    }
    if spec.lifetime_ns <= 0 || spec.latency_ns < 0 {
        return Err(QtError::Config("lifetime_ns must be > 0 and latency_ns >= 0".into()));
    }
    let parts = ds.partitions();
    let weights: Vec<u64> = match spec.allocation {
        Allocation::Equal => vec![1; parts.len()],
        Allocation::Proportional => parts.iter().map(|p| p.rows).collect(),
    };
    let quotas = allocate(spec.n, &weights);
    let mut rows = Vec::new();
    let mut by_instrument: IndexMap<String, u64> = IndexMap::new();
    let mut by_placement: IndexMap<String, u64> = IndexMap::new();
    for p in &spec.placements {
        by_placement.entry(p.offset_ticks.to_string()).or_insert(0);
    }
    for (part, &quota) in parts.iter().zip(&quotas) {
        if quota == 0 || part.ts_max < part.ts_min {
            continue;
        }
        let inst = ds.instrument(part.instrument_id, part.session_date);
        let lot = inst.map_or(1, |i| i.lot_size.max(1));
        let (mut lo, mut hi) = (part.ts_min, part.ts_max);
        if let Some(i) = inst {
            let a = i
                .midnight_utc_ns
                .saturating_add((i64::from(i.open_s) + i64::from(spec.window_open_s)).saturating_mul(SEC_NS));
            let b = i
                .midnight_utc_ns
                .saturating_add((i64::from(i.close_s) - i64::from(spec.window_close_s)).saturating_mul(SEC_NS));
            let (wa, wb) = (a.max(part.ts_min), b.min(part.ts_max.saturating_add(1)));
            if wa < wb {
                (lo, hi) = (wa, wb - 1);
            }
        }
        let median = if spec
            .qty
            .iter()
            .any(|q| q.kind == QtyKind::MedianOrderSize && q.weight > 0.0)
        {
            median_add_size(part, lot)?
        } else {
            lot
        };
        let mut rng = Xoshiro256StarStar::new(stable_seed(
            spec.seed,
            &[
                b"sample",
                &part.instrument_id.to_le_bytes(),
                &part.session_date.to_le_bytes(),
            ],
        ));
        let span = (hi - lo) as u64;
        let mut drawn = Vec::with_capacity((quota as usize).min(1 << 16));
        for _ in 0..quota {
            let ts_submit = lo + rng.range_inclusive(0, span) as Ts;
            let side = match spec.sides[(rng.next_u64() % spec.sides.len() as u64) as usize] {
                SideName::B => BookSide::Bid,
                SideName::A => BookSide::Ask,
            };
            let pl = &spec.placements[pick(&pw, rng.next_f64())];
            let qr = &spec.qty[pick(&qw, rng.next_f64())];
            let qty = match qr.kind {
                QtyKind::Lots => qr.lots.unwrap_or(1).saturating_mul(lot),
                QtyKind::MedianOrderSize => median,
            };
            let ts_cancel = ts_submit
                .checked_add(spec.lifetime_ns)
                .ok_or_else(|| QtError::Config("lifetime_ns overflows the timestamp range".into()))?;
            drawn.push((ts_submit, side, pl.offset_ticks, qty, ts_cancel));
        }
        drawn.sort_by_key(|d| d.0);
        let date = date_compact(part.session_date);
        for (k, (ts_submit, side, off, qty, ts_cancel)) in drawn.into_iter().enumerate() {
            *by_instrument.entry(part.instrument_id.to_string()).or_insert(0) += 1;
            *by_placement.entry(off.to_string()).or_insert(0) += 1;
            rows.push(ShadowSpec {
                shadow_id: format!("s{}_{date}_{k:06}", part.instrument_id),
                instrument_id: part.instrument_id,
                side,
                price: PriceSpec::Touch { offset_ticks: off },
                qty,
                ts_submit,
                latency_ns: spec.latency_ns,
                ts_cancel: Some(ts_cancel),
                group: None,
                source: ShadowSource::Sampled,
                order_key: None,
            });
        }
    }
    let files = write_specs(&spec.out, &rows)?;
    Ok(ShadowSampleSummary {
        spec: echo(spec)?,
        n: rows.len() as u64,
        by_instrument,
        by_placement,
        files,
        ..ShadowSampleSummary::default()
    })
}

/// Reservoir sampler of real lives of one partition.
struct Mirror {
    quotas: [u64; 2],
    stratify: bool,
    seen: [u64; 2],
    keep: [Vec<(RealOrderRow, u32)>; 2],
    rngs: [Xoshiro256StarStar; 2],
    lives: IdMap<u64, u32>,
}

impl Sink for Mirror {
    fn real_order(&mut self, r: &RealOrderRow) -> QtResult<()> {
        let life = self.lives.entry(r.order_id).or_insert(0);
        let k = *life;
        *life += 1;
        if r.in_snapshot || matches!(r.end_reason, RealOrderEnd::Replaced | RealOrderEnd::Cleared) {
            return Ok(());
        }
        let s = if self.stratify && r.end_reason != RealOrderEnd::Filled {
            1
        } else {
            0
        };
        let q = self.quotas[s];
        if q == 0 {
            return Ok(());
        }
        self.seen[s] += 1;
        if (self.keep[s].len() as u64) < q {
            self.keep[s].push((*r, k));
        } else {
            let j = self.rngs[s].range_inclusive(0, self.seen[s] - 1);
            if j < q {
                self.keep[s][j as usize] = (*r, k);
            }
        }
        Ok(())
    }
}

/// `shadows mirror-real`.
pub fn mirror_real_orders(spec: &MirrorSpec) -> QtResult<ShadowSampleSummary> {
    let ds = open_dataset(dataset_path(&spec.dataset)?)?;
    let parts = ds.partitions();
    let strata = if spec.stratify { 2 } else { 1 };
    let quotas = allocate(spec.n, &vec![1; parts.len() * strata]);
    let mut cfg = EngineConfig::new(crate::run::dataset_rules(&ds));
    cfg.bbo = BboMode::None;
    cfg.trajectory = TrajectoryMode::None;
    cfg.real_orders = true;
    let mut picked: Vec<(RealOrderRow, u32)> = Vec::new();
    for (pi, part) in parts.iter().enumerate() {
        let q = if spec.stratify {
            [quotas[2 * pi], quotas[2 * pi + 1]]
        } else {
            [quotas[pi], 0]
        };
        if q == [0, 0] {
            continue;
        }
        let rng = |s: u8| {
            Xoshiro256StarStar::new(stable_seed(
                spec.seed,
                &[
                    b"mirror",
                    &part.instrument_id.to_le_bytes(),
                    &part.session_date.to_le_bytes(),
                    &[s],
                ],
            ))
        };
        let mut m = Mirror {
            quotas: q,
            stratify: spec.stratify,
            seen: [0; 2],
            keep: [Vec::new(), Vec::new()],
            rngs: [rng(0), rng(1)],
            lives: IdMap::default(),
        };
        let inst: Vec<_> = ds
            .instrument(part.instrument_id, part.session_date)
            .cloned()
            .into_iter()
            .collect();
        let mut eng = Engine::new(cfg.clone(), &inst, Vec::new(), Vec::new())?;
        eng.set_sequence_gap_detection(false);
        for e in part.mbo()? {
            eng.apply(&e?, &mut m)?;
        }
        eng.finish(&mut m)?;
        let mut keep: Vec<(RealOrderRow, u32)> = m.keep.into_iter().flatten().collect();
        keep.sort_by_key(|(r, k)| (r.ts_add, r.sequence_add, r.order_id, *k));
        picked.extend(keep);
    }
    let mut used: HashSet<String> = HashSet::new();
    let mut rows = Vec::with_capacity(picked.len());
    let mut by_instrument: IndexMap<String, u64> = IndexMap::new();
    for (r, k) in picked {
        let base = if k == 0 {
            format!("r{}", r.order_id)
        } else {
            format!("r{}_{k}", r.order_id)
        };
        let mut id = base.clone();
        if used.contains(&id) {
            id = format!("{base}_i{}", r.instrument_id);
        }
        if used.contains(&id) {
            id = format!("{base}_i{}_d{}", r.instrument_id, date_compact(r.session_date));
        }
        used.insert(id.clone());
        *by_instrument.entry(r.instrument_id.to_string()).or_insert(0) += 1;
        rows.push(ShadowSpec {
            shadow_id: id,
            instrument_id: r.instrument_id,
            side: r.side,
            price: PriceSpec::Abs(r.price),
            qty: r.size_at_add,
            ts_submit: r.ts_add,
            latency_ns: 0,
            ts_cancel: if r.end_reason == RealOrderEnd::Cancelled {
                r.ts_end
            } else {
                None
            },
            group: None,
            source: ShadowSource::RealOrderMirror,
            order_key: None,
        });
    }
    let files = write_specs(&spec.out, &rows)?;
    // `by_placement` keys are tick offsets (`IdKey` pattern `^-?[0-9]+$`): a mirror sits at its real order's own
    // price, directly behind it, so every mirror counts under offset "0".
    let mut by_placement = IndexMap::new();
    by_placement.insert("0".to_owned(), rows.len() as u64);
    Ok(ShadowSampleSummary {
        spec: echo(spec)?,
        n: rows.len() as u64,
        by_instrument,
        by_placement,
        files,
        ..ShadowSampleSummary::default()
    })
}
