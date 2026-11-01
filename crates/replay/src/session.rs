//! Interactive replay over a dataset (et `Replay` session): `advance_to(ts)` applies every record with
//! `ts_event <= ts` of every partition (each partition its own engine, records in feed order; a record past `ts`
//! waits for the next call); queries read the book of the instrument's latest started partition.
//!
//! A session opened from a replay spec (`open_replay`) also takes shadow orders while it runs: `submit`, `cancel` and
//! `amend` at or after the session's position, each routed to the partition `replay` would give it, with the
//! partition engines configured as `replay` configures them (latency model, seed, fill rule, hidden-print policy,
//! exclusions, ingest feed gaps and trading halts). Because a shadow's work happens at the start of the first event
//! after its effective time, which is at or after the position, the fills and results are those of one `replay`
//! run with every shadow, cancel and amend in its input (`tests/incremental.rs`), and the session costs one pass
//! over the data however many orders it takes. `take_fills` / `take_results` drain the rows produced so far;
//! `finish` ends the session (results of the shadows still open).

use std::collections::HashMap;
use std::path::Path;

use arrow_array::RecordBatch;
use qt_book::{Bbo, Book, LevelView};
use qt_ingest::Dataset;
use qt_schema::codec::PlannedEndCols;
use qt_schema::json::DatasetKindName;
use qt_schema::rows::{Liquidity, ShadowFillRow, ShadowResultRow, ShadowSpec};
use qt_schema::specs::ReplaySpec;
use qt_types::{BookSide, MboEvent, Px, QtError, QtResult, Qty, Ts, Venue, flags, rules};

use crate::run::{
    ReplayOptions, dataset_path, engine_config, halts_of, ingest_dq, interactive_partition_engine, open_dataset,
};
use crate::sinks::{FillFull, ResultFull, Row};
use crate::{BboMode, Engine, EngineConfig, ShadowPeek, Sink, TrajectoryMode};

struct Cursor {
    instrument_id: u32,
    session_date: i32,
    iter: Box<dyn Iterator<Item = QtResult<MboEvent>> + Send>,
    pending: Option<MboEvent>,
    /// Records of an event not complete at the last `advance_events_to` (applied, in order, before anything else).
    held: Vec<MboEvent>,
    engine: Engine,
    started: bool,
    done: bool,
}

/// The shadow rows of a session (everything else an engine emits is dropped).
#[derive(Default)]
struct ShadowRows {
    fills: Vec<FillFull>,
    results: Vec<ResultFull>,
}

impl Sink for ShadowRows {
    fn shadow_fill_liquidity(&mut self, r: &ShadowFillRow, liquidity: Liquidity) -> QtResult<()> {
        self.fills.push(FillFull(r.clone(), liquidity));
        Ok(())
    }

    fn shadow_result_planned(&mut self, r: &ShadowResultRow, planned: PlannedEndCols) -> QtResult<()> {
        self.results.push(ResultFull(r.clone(), planned));
        Ok(())
    }
}

/// Interactive replay session over a dataset.
pub struct Session {
    ds: Dataset,
    cursors: Vec<Cursor>,
    position: Ts,
    /// Opened from a replay spec: takes shadows.
    orders: bool,
    rows: ShadowRows,
    /// Shadow id -> its cursor (`None`: no partition covers its submit time, see `unmatched`).
    owner: HashMap<String, Option<usize>>,
    /// The engine configuration of `open_replay` (for `unmatched`).
    cfg: Option<EngineConfig>,
    /// Shadows no partition covers: `replay` ends them `censored_gap` at `ts_live` without replaying them, so they
    /// are kept here, never replayed, and reported so (`shadow_peek`, `finish`).
    unmatched: Option<Engine>,
    finished: bool,
}

impl Session {
    /// Opens a dataset (optional venue override) for book queries only.
    pub fn open(root: &Path, venue: Option<Venue>) -> QtResult<Self> {
        let ds = Dataset::open(root)?;
        let r = venue.map_or_else(|| crate::run::dataset_rules(&ds), rules);
        let mut cfg = EngineConfig::new(r);
        cfg.bbo = BboMode::None;
        cfg.trajectory = TrajectoryMode::None;
        let mut cursors = Vec::new();
        for p in ds.partitions() {
            let inst: Vec<_> = ds
                .instrument(p.instrument_id, p.session_date)
                .cloned()
                .into_iter()
                .collect();
            let mut engine = Engine::new(cfg.clone(), &inst, Vec::new(), Vec::new())?;
            engine.set_sequence_gap_detection(false);
            cursors.push(Cursor {
                instrument_id: p.instrument_id,
                session_date: p.session_date,
                iter: p.mbo()?,
                pending: None,
                held: Vec::new(),
                engine,
                started: false,
                done: false,
            });
        }
        Ok(Self::with(ds, cursors, false))
    }

    /// Opens the dataset of a replay spec for book queries and shadow orders, each partition engine configured as
    /// `replay` configures it. The spec's `shadows` and `amends` files are not read (orders come from `submit`).
    pub fn open_replay(spec: &ReplaySpec) -> QtResult<Self> {
        if spec.shadows.is_some() || spec.amends.is_some() {
            return Err(QtError::Config(
                "session: shadows and amends come from submit / cancel / amend, not from spec files".into(),
            ));
        }
        let ds = open_dataset(dataset_path(&spec.dataset)?)?;
        if ds.manifest.kind == DatasetKindName::L2 {
            return Err(QtError::Config(
                "dataset: exact replay needs MBO (this dataset is L2-only)".into(),
            ));
        }
        let mut cfg = engine_config(spec, &ds)?;
        // Only shadow rows leave a session: no BBO, depth, trajectory, rank-trace or real-order rows.
        cfg.bbo = BboMode::None;
        cfg.trajectory = TrajectoryMode::None;
        cfg.depth_levels = 0;
        cfg.rank_oracle = false;
        cfg.real_orders = false;
        let mut opts = ReplayOptions::resolve(spec, &ReplayOptions::default())?;
        opts.check_honoured(&cfg.rules)?;
        let dq = ingest_dq(&ds);
        let gaps = dq.as_ref().map(|d| d.feed_gaps.clone()).unwrap_or_default();
        if opts.trading_halts.is_empty() {
            opts.trading_halts = dq.as_ref().map(halts_of).unwrap_or_default();
        }
        let mut cursors = Vec::new();
        for p in ds.partitions() {
            cursors.push(Cursor {
                instrument_id: p.instrument_id,
                session_date: p.session_date,
                iter: p.mbo()?,
                pending: None,
                held: Vec::new(),
                engine: interactive_partition_engine(p, &ds, &cfg, &gaps, &opts)?,
                started: false,
                done: false,
            });
        }
        let mut s = Self::with(ds, cursors, true);
        s.cfg = Some(cfg);
        Ok(s)
    }

    fn with(ds: Dataset, cursors: Vec<Cursor>, orders: bool) -> Self {
        Self {
            ds,
            cursors,
            position: Ts::MIN,
            orders,
            rows: ShadowRows::default(),
            owner: HashMap::new(),
            cfg: None,
            unmatched: None,
            finished: false,
        }
    }

    fn check_open(&self) -> QtResult<()> {
        if self.finished {
            return Err(QtError::Config("session: already finished".into()));
        }
        Ok(())
    }

    /// The session's position: every record with `ts_event` at or before it was applied, except, after
    /// `advance_events_to`, the records of an event not complete by then (held until it completes or the next
    /// `advance_to`). Orders are taken at or after it.
    pub fn position(&self) -> Ts {
        self.position
    }

    /// Replays every event with `ts_event <= ts`.
    pub fn advance_to(&mut self, ts: Ts) -> QtResult<()> {
        self.check_open()?;
        // Records held back by `advance_events_to` are at or before the position: they go first, whatever `ts`.
        for c in &mut self.cursors {
            for e in std::mem::take(&mut c.held) {
                c.started = true;
                c.engine.apply(&e, &mut self.rows)?;
            }
        }
        if ts <= self.position {
            return Ok(());
        }
        for c in &mut self.cursors {
            while !c.done {
                let e = match c.pending.take() {
                    Some(e) => e,
                    None => match c.iter.next() {
                        Some(e) => e?,
                        None => {
                            c.done = true;
                            break;
                        }
                    },
                };
                if e.ts_event > ts {
                    c.pending = Some(e);
                    break;
                }
                c.started = true;
                c.engine.apply(&e, &mut self.rows)?;
            }
        }
        self.position = ts;
        Ok(())
    }

    /// Replays whole events up to `ts`: in each partition, the records of the longest run of complete events (each
    /// ending with a `LAST` record) whose records all have `ts_event <= ts`, reading in feed order up to the first
    /// record past `ts`; the records of an event that is not complete by then wait for a later call. When the data
    /// ends with every remaining record at or before `ts`, an unterminated tail is applied and ended as `finish`
    /// ends it (shadows stay open: orders are still taken until `finish`). The rule is that of the agent sandbox's
    /// tape (`queuetruth.sandbox.tape`), so the engine state after a call is that of a `replay` of exactly the
    /// released prefix. The position becomes `ts`.
    pub fn advance_events_to(&mut self, ts: Ts) -> QtResult<()> {
        self.check_open()?;
        for c in &mut self.cursors {
            while !c.done {
                let e = match c.pending.take() {
                    Some(e) => e,
                    None => match c.iter.next() {
                        Some(e) => e?,
                        None => {
                            for e in std::mem::take(&mut c.held) {
                                c.started = true;
                                c.engine.apply(&e, &mut self.rows)?;
                            }
                            c.engine.end_open_events(&mut self.rows)?;
                            c.done = true;
                            break;
                        }
                    },
                };
                if e.ts_event > ts {
                    c.pending = Some(e);
                    break;
                }
                let last = e.flags & flags::LAST != 0;
                c.held.push(e);
                if last {
                    for e in std::mem::take(&mut c.held) {
                        c.started = true;
                        c.engine.apply(&e, &mut self.rows)?;
                    }
                }
            }
        }
        self.position = self.position.max(ts);
        Ok(())
    }

    fn orders_check(&self, ts: Ts, what: &str, shadow_id: &str) -> QtResult<()> {
        self.check_open()?;
        if !self.orders {
            return Err(QtError::Config(
                "session: opened for book queries only; open it from a replay spec to send shadow orders".into(),
            ));
        }
        if ts < self.position {
            return Err(QtError::Config(format!(
                "shadow {shadow_id}: {what} {ts} is before the session position {}",
                self.position
            )));
        }
        Ok(())
    }

    fn owner_of(&self, shadow_id: &str) -> QtResult<Option<usize>> {
        self.owner.get(shadow_id).copied().ok_or_else(|| QtError::InputFormat {
            location: format!("shadow {shadow_id}"),
            msg: "unknown shadow_id".into(),
        })
    }

    /// The engine holding a shadow: its partition's, or `unmatched`.
    fn engine_of(&mut self, shadow_id: &str) -> QtResult<&mut Engine> {
        match self.owner_of(shadow_id)? {
            Some(k) => Ok(&mut self.cursors[k].engine),
            None => self
                .unmatched
                .as_mut()
                .ok_or_else(|| QtError::Engine("session: unmatched shadow without an engine".into())),
        }
    }

    /// Sends a shadow (`ts_submit` at or after the position), routed to the partition `replay` would give it. A
    /// shadow no partition covers (another day, an instrument without data) is taken and ends `censored_gap` at
    /// its `ts_live` without being replayed, as in `replay`.
    pub fn submit(&mut self, spec: ShadowSpec) -> QtResult<()> {
        self.orders_check(spec.ts_submit, "ts_submit", &spec.shadow_id)?;
        if self.owner.contains_key(&spec.shadow_id) {
            return Err(QtError::Duplicate {
                what: "shadow_id".into(),
                id: spec.shadow_id,
            });
        }
        let Some(p) = self.ds.partition_for(spec.instrument_id, spec.ts_submit) else {
            if self.unmatched.is_none() {
                let cfg = self
                    .cfg
                    .clone()
                    .ok_or_else(|| QtError::Engine("session: orders without a replay configuration".into()))?;
                self.unmatched = Some(Engine::new_interactive(cfg, &self.ds.instruments)?);
            }
            let id = spec.shadow_id.clone();
            self.engine_of_unmatched()?.add_shadow(spec)?;
            self.owner.insert(id, None);
            return Ok(());
        };
        let key = (p.instrument_id, p.session_date);
        let k = self
            .cursors
            .iter()
            .position(|c| (c.instrument_id, c.session_date) == key)
            .ok_or_else(|| QtError::Engine("session: partition without a cursor".into()))?;
        let id = spec.shadow_id.clone();
        self.cursors[k].engine.add_shadow(spec)?;
        self.owner.insert(id, Some(k));
        Ok(())
    }

    fn engine_of_unmatched(&mut self) -> QtResult<&mut Engine> {
        self.unmatched
            .as_mut()
            .ok_or_else(|| QtError::Engine("session: no unmatched engine".into()))
    }

    /// Sends a cancel at `ts_cancel` (at or after the position) for a shadow without one.
    pub fn cancel(&mut self, shadow_id: &str, ts_cancel: Ts) -> QtResult<()> {
        self.orders_check(ts_cancel, "ts_cancel", shadow_id)?;
        self.engine_of(shadow_id)?.cancel_shadow(shadow_id, ts_cancel).map(drop)
    }

    /// Sends a priority-keeping reduction to `new_qty` at `ts_send` (at or after the position).
    pub fn amend(&mut self, shadow_id: &str, ts_send: Ts, new_qty: Qty) -> QtResult<()> {
        self.orders_check(ts_send, "ts_send", shadow_id)?;
        self.engine_of(shadow_id)?
            .amend_shadow(shadow_id, ts_send, new_qty)
            .map(drop)
    }

    /// A shadow as a replay ending now would report it (`ShadowPeek`): its result row so far, when it ended, and its
    /// queue ahead and rank. The fills come from `take_fills`.
    pub fn shadow_peek(&self, shadow_id: &str) -> QtResult<ShadowPeek> {
        if self.finished {
            return Err(QtError::Config("session: already finished".into()));
        }
        let owner = self.owner_of(shadow_id)?;
        let engine = match owner {
            Some(k) => &self.cursors[k].engine,
            None => self
                .unmatched
                .as_ref()
                .ok_or_else(|| QtError::Engine("session: unmatched shadow without an engine".into()))?,
        };
        let idx = engine
            .shadow_index(shadow_id)
            .ok_or_else(|| QtError::Engine(format!("session: shadow {shadow_id} has no engine index")))?;
        let mut peek = engine.shadow_peek(idx);
        if owner.is_none() {
            // `replay` ends it `censored_gap` at `ts_live` without replaying it (`Engine::censor_unreplayed`).
            peek.status = crate::ShadowStatus::Done;
            peek.result.end_state = qt_schema::rows::EndState::CensoredGap;
            peek.ts_end = Some(peek.result.ts_live);
        }
        Ok(peek)
    }

    /// The shadow fills produced since the last call, as a `qt.shadow_fills` batch (schema 1.1 `liquidity`).
    pub fn take_fills(&mut self) -> QtResult<RecordBatch> {
        FillFull::batch(&std::mem::take(&mut self.rows.fills))
    }

    /// The shadow results produced since the last call (shadows that ended), as a `qt.shadow_results` batch.
    pub fn take_results(&mut self) -> QtResult<RecordBatch> {
        ResultFull::batch(&std::mem::take(&mut self.rows.results))
    }

    /// Ends the session at the end of its data: the shadows still open end as at the end of a `replay` (their
    /// results join `take_results`). Book queries and orders are refused afterwards.
    pub fn finish(&mut self) -> QtResult<()> {
        self.check_open()?;
        self.advance_to(Ts::MAX)?;
        for c in std::mem::take(&mut self.cursors) {
            c.engine.finish(&mut self.rows)?;
        }
        if let Some(mut eng) = self.unmatched.take() {
            eng.censor_unreplayed(&mut self.rows)?;
            eng.finish(&mut self.rows)?;
        }
        self.finished = true;
        Ok(())
    }

    fn book(&self, instrument_id: u32) -> Option<&Book> {
        self.cursors
            .iter()
            .filter(|c| c.instrument_id == instrument_id && c.started)
            .max_by_key(|c| c.session_date)
            .and_then(|c| c.engine.book(instrument_id))
    }

    /// BBO of an instrument.
    pub fn bbo(&self, instrument_id: u32) -> Option<Bbo> {
        self.book(instrument_id).map(Book::bbo)
    }

    /// Top `n` levels (bids, asks) of an instrument.
    pub fn depth(&self, instrument_id: u32, n: usize) -> (Vec<LevelView>, Vec<LevelView>) {
        self.book(instrument_id).map_or((Vec::new(), Vec::new()), |b| {
            (b.depth(BookSide::Bid, n), b.depth(BookSide::Ask, n))
        })
    }

    /// Displayed quantity and order count at a price (queue ahead of a new order there); `None` before the
    /// instrument's first record.
    pub fn queue_ahead(&self, instrument_id: u32, side: BookSide, px: Px) -> Option<(Qty, u32)> {
        self.book(instrument_id)
            .map(|b| b.level(side, px).map_or((0, 0), |l| (l.total, l.count)))
    }
}
