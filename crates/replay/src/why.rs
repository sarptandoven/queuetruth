//! `why` (SPEC 3.10 `qt.why_trace`): the record-level story of one shadow. While the shadow is live, every record of
//! its instrument that is relevant to it (an order ahead of it, its price on either side, a change at its level, a
//! trade through its price) becomes one event row; rows carry the shadow's queue ahead, rank and level quantity
//! after that record's event, and the event's fill quantity on the event's last row. A fill at activation
//! (`crossed_at_live`) is one synthetic row. Rows are capped at `max_events` (`truncated`).
//!
//! A live order's trace (audit ruling A1, review F5a): `WhySpec.exclude_order_ids` are removed from the tape like in
//! the scorer's exact arm, and when `WhySpec.live` names the live log and `order_key` the order, that order's own MBO
//! records (its `venue_order_id` on its instrument, from the log's `orders.parquet`) are removed too, so the trace
//! never shows the user's orders queued ahead of the shadow twin. Rules come from the manifest's `rule_set`.
//! `trace_shadow_excluding` does the same for any list of (instrument_id, order_id).

use std::path::Path;

use qt_book::Effect;
use qt_schema::codec::read_shadow_specs;
use qt_schema::json::{WhyEvent, WhyOutcome, WhySubject, WhyTrace};
use qt_schema::rows::{ShadowAmend, ShadowFillRow, ShadowSpec};
use qt_schema::specs::WhySpec;
use qt_types::{BookSide, Instrument, MboEvent, Px, QtError, QtResult, Qty, Ts, UNDEF_PRICE};

use crate::run::{ReplayOptions, dataset_path, dataset_rules, ingest_dq, open_dataset, partition_engine, write_json};
use crate::sinks::NullSink;
use crate::{Engine, EngineConfig};

fn effect_name(e: &Effect) -> &'static str {
    match e {
        Effect::None => "none",
        Effect::Add { .. } => "add",
        Effect::Remove { .. } => "remove",
        Effect::Reduce { .. } => "reduce",
        Effect::Replace { .. } => "replace",
        Effect::Clear => "clear",
    }
}

fn letter(b: u8) -> String {
    (b as char).to_string()
}

/// Collects the trace of one shadow (owned by the shadow engine while replaying).
pub(crate) struct Tracer {
    pub(crate) idx: u32,
    max: usize,
    events: Vec<WhyEvent>,
    truncated: bool,
    pending: Vec<WhyEvent>,
    last: Option<WhyEvent>,
}

impl Tracer {
    pub(crate) fn new(idx: u32, max: usize) -> Self {
        Self {
            idx,
            max,
            events: Vec::new(),
            truncated: false,
            pending: Vec::new(),
            last: None,
        }
    }

    fn push(&mut self, ev: WhyEvent) {
        if self.events.len() < self.max {
            self.events.push(ev);
        } else {
            self.truncated = true;
        }
    }

    /// One record of the shadow's instrument while it is live (before its accounting).
    pub(crate) fn record(&mut self, e: &MboEvent, effect: &Effect, is_ahead: bool, relevant: bool) {
        let ev = WhyEvent {
            ts_event: e.ts_event,
            sequence: e.sequence,
            action: letter(e.action),
            order_id: e.order_id,
            side: letter(e.side),
            price: (e.price != UNDEF_PRICE).then_some(e.price),
            size: e.size,
            effect: effect_name(effect).into(),
            is_ahead,
            ..WhyEvent::default()
        };
        if relevant {
            self.pending.push(ev);
        } else {
            self.last = Some(ev);
        }
    }

    pub(crate) fn has_pending(&self) -> bool {
        !self.pending.is_empty()
    }

    /// End of an event in which the shadow was processed (or had relevant records).
    pub(crate) fn end_event(&mut self, qa: Qty, rank: u32, level: Qty, filled_now: Qty, fills: &[ShadowFillRow]) {
        if filled_now > 0 && self.pending.is_empty() {
            if let Some(l) = self.last.take() {
                self.pending.push(l);
            }
        }
        let n = self.pending.len();
        let note = (!fills.is_empty()).then(|| {
            fills
                .iter()
                .map(|f| format!("fill {} {}", f.fill_qty, f.reason))
                .collect::<Vec<_>>()
                .join("; ")
        });
        for (k, mut ev) in std::mem::take(&mut self.pending).into_iter().enumerate() {
            ev.queue_ahead_after = qa;
            ev.rank_after = rank;
            ev.level_qty_after = level;
            if k + 1 == n {
                ev.fill_qty = filled_now;
                ev.note.clone_from(&note);
            }
            self.push(ev);
        }
        self.last = None;
    }

    /// A fill at activation.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn activation_fill(
        &mut self,
        ts_live: Ts,
        sequence: u64,
        side: BookSide,
        price: Px,
        qty: Qty,
        qa: Qty,
        rank: u32,
        level: Qty,
    ) {
        self.push(WhyEvent {
            ts_event: ts_live,
            sequence,
            action: "N".into(),
            order_id: 0,
            side: letter(side.as_byte()),
            price: Some(price),
            size: 0,
            effect: "none".into(),
            is_ahead: false,
            queue_ahead_after: qa,
            rank_after: rank,
            level_qty_after: level,
            fill_qty: qty,
            note: Some(format!("fill {qty} crossed_at_live")),
        });
    }
}

/// Replays one partition's `events` with one shadow and returns its trace (record-level gap detection off, as in
/// replay; feed gaps are not applied: `why` passes them through `run::partition_engine`).
pub fn trace_shadow(
    events: impl IntoIterator<Item = QtResult<MboEvent>>,
    cfg: EngineConfig,
    instruments: &[Instrument],
    shadow: ShadowSpec,
    amends: Vec<ShadowAmend>,
    max_events: usize,
    subject: WhySubject,
) -> QtResult<WhyTrace> {
    trace_shadow_excluding(events, cfg, instruments, shadow, amends, max_events, subject, &[])
}

/// `trace_shadow` with real orders (instrument_id, order_id) removed from the tape first (A1: the user's own
/// orders never appear in the displayed queue).
#[allow(clippy::too_many_arguments)]
pub fn trace_shadow_excluding(
    events: impl IntoIterator<Item = QtResult<MboEvent>>,
    cfg: EngineConfig,
    instruments: &[Instrument],
    shadow: ShadowSpec,
    amends: Vec<ShadowAmend>,
    max_events: usize,
    subject: WhySubject,
    excluded: &[(u32, u64)],
) -> QtResult<WhyTrace> {
    let mut eng = Engine::new(cfg, instruments, vec![shadow], amends)?;
    // One partition's records, like replay: its sequence numbers may jump (review M3).
    eng.set_sequence_gap_detection(false);
    if !excluded.is_empty() {
        eng.set_excluded_orders(excluded.iter().copied());
        eng.set_twin_linking(true);
    }
    run_traced(&mut eng, events, max_events)?;
    finish_trace(eng, subject)
}

fn run_traced(eng: &mut Engine, events: impl IntoIterator<Item = QtResult<MboEvent>>, max: usize) -> QtResult<()> {
    eng.trace(0, max);
    let mut sink = NullSink;
    for e in events {
        eng.apply(&e?, &mut sink)?;
    }
    Ok(())
}

fn finish_trace(eng: Engine, subject: WhySubject) -> QtResult<WhyTrace> {
    let spec = eng.shadow_spec(0).clone();
    let mut results = crate::sinks::VecSink::default();
    let (_, t) = eng.finish_traced(&mut results)?;
    let r = results
        .shadow_results
        .first()
        .ok_or_else(|| QtError::Engine("why: no shadow result".into()))?;
    let (events, truncated) = t.map_or((Vec::new(), false), |t| (t.events, t.truncated));
    Ok(WhyTrace {
        subject,
        instrument_id: spec.instrument_id,
        side: letter(spec.side.as_byte()),
        price: r.price,
        qty: spec.qty,
        ts_live: r.ts_activated_event.is_some().then_some(r.ts_live),
        outcome: WhyOutcome {
            filled_qty: r.filled_qty,
            ts_first_fill: r.ts_first_fill,
            end_state: r.end_state.to_string(),
        },
        events,
        truncated,
        ..WhyTrace::default()
    })
}

/// `why`: replays the shadow's partition with that shadow alone (latency zero, dataset rules) through replay's own
/// partition engine (`run::partition_engine`), so the trace and `replay` agree.
pub fn why(spec: &WhySpec) -> QtResult<WhyTrace> {
    let ds = open_dataset(dataset_path(&spec.dataset)?)?;
    let Some(shadows) = spec.shadows.as_ref() else {
        return Err(QtError::Usage(
            "why needs shadows + shadow_id (live logs are converted to a schedule by the Python layer)".into(),
        ));
    };
    let path = shadows
        .as_path()
        .ok_or_else(|| QtError::Usage("why: shadows must be a local path".into()))?;
    let id = spec
        .shadow_id
        .as_deref()
        .ok_or_else(|| QtError::Usage("why: shadow_id is required with shadows".into()))?;
    let shadow = read_shadow_specs(Path::new(path))?
        .into_iter()
        .find(|s| s.shadow_id == id)
        .ok_or_else(|| QtError::Config(format!("shadow_id: {id:?} not found in {path}")))?;
    let subject = WhySubject {
        shadow_id: Some(id.to_owned()),
        order_key: spec.order_key.clone(),
    };
    let max = spec.max_events as usize;
    let cfg = EngineConfig::new(dataset_rules(&ds));
    // Every listed real order leaves the tape (like the scorer's exact arm), plus the traced live order's own twin.
    let mut excluded: Vec<(u32, u64)> = Vec::new();
    let mut excluded_dated: Vec<(u32, u64, i32)> = Vec::new();
    for x in &spec.exclude_order_ids {
        match &x.session_date {
            None => excluded.push((x.instrument_id, x.order_id)),
            Some(d) => {
                let day = qt_ingest::time::parse_date(d)
                    .map_err(|e| QtError::Config(format!("exclude_order_ids: session_date {d:?}: {e}")))?;
                excluded_dated.push((x.instrument_id, x.order_id, day));
            }
        }
    }
    if let (Some(live), Some(key)) = (&spec.live, &spec.order_key) {
        let dir = live
            .as_path()
            .ok_or_else(|| QtError::Usage("why: live must be a local path".into()))?;
        excluded.extend(own_order_ids(Path::new(dir), key)?);
    }
    excluded.sort_unstable();
    excluded.dedup();
    let trace = match ds.partition_for(shadow.instrument_id, shadow.ts_submit) {
        Some(part) => {
            // Exactly replay's partition engine (review M3): gap detection off, ingest feed gaps, exclusions.
            let gaps = ingest_dq(&ds).map(|d| d.feed_gaps).unwrap_or_default();
            let opts = ReplayOptions {
                exclude_order_ids: excluded,
                exclude_order_ids_dated: excluded_dated,
                ..ReplayOptions::default()
            };
            let mut eng = partition_engine(part, &ds, &cfg, vec![shadow], Vec::new(), &gaps, &opts)?;
            run_traced(&mut eng, part.mbo()?, max)?;
            finish_trace(eng, subject)?
        }
        None => {
            let mut t = trace_shadow(std::iter::empty(), cfg, &[], shadow, Vec::new(), max, subject)?;
            t.outcome.end_state = "censored_gap".into();
            t
        }
    };
    if let Some(out) = &spec.out {
        write_json(Path::new(out), &trace)?;
    }
    Ok(trace)
}

/// (instrument_id, venue order id) of the live order `order_key` in a canonical live-log directory
/// (`orders.parquet`); empty when its venue order id is not an MBO order id (not a decimal integer) or unknown.
fn own_order_ids(dir: &Path, order_key: &str) -> QtResult<Vec<(u32, u64)>> {
    use arrow_array::{Array, StringArray, UInt32Array};
    let path = if dir.is_dir() {
        dir.join("orders.parquet")
    } else {
        dir.to_path_buf()
    };
    let mut out = Vec::new();
    for b in qt_schema::io::read_table_as(&path, "qt.live_orders")? {
        let b = b?;
        let col = |name: &str| {
            b.column_by_name(name)
                .ok_or_else(|| QtError::Engine(format!("{}: no column {name}", path.display())))
        };
        let keys = col("order_key")?;
        let keys = keys.as_any().downcast_ref::<StringArray>();
        let insts = col("instrument_id")?;
        let insts = insts.as_any().downcast_ref::<UInt32Array>();
        let vids = col("venue_order_id")?;
        let vids = vids.as_any().downcast_ref::<StringArray>();
        let (Some(keys), Some(insts), Some(vids)) = (keys, insts, vids) else {
            return Err(QtError::Engine(format!(
                "{}: unexpected live_orders column types",
                path.display()
            )));
        };
        for i in 0..b.num_rows() {
            if keys.is_null(i) || keys.value(i) != order_key || insts.is_null(i) || vids.is_null(i) {
                continue;
            }
            if let Ok(id) = vids.value(i).parse::<u64>() {
                out.push((insts.value(i), id));
            }
        }
    }
    Ok(out)
}
