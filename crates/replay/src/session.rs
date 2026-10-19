//! Interactive replay over a dataset (et `Replay` session): `advance_to(ts)` applies every record with
//! `ts_event <= ts` of every partition (each partition its own engine, records in feed order; a record past `ts`
//! waits for the next call); queries read the book of the instrument's latest started partition.

use std::path::Path;

use qt_book::{Bbo, Book, LevelView};
use qt_ingest::Dataset;
use qt_types::{BookSide, MboEvent, Px, QtResult, Qty, Ts, Venue, rules};

use crate::sinks::NullSink;
use crate::{BboMode, Engine, EngineConfig, TrajectoryMode};

struct Cursor {
    instrument_id: u32,
    session_date: i32,
    iter: Box<dyn Iterator<Item = QtResult<MboEvent>> + Send>,
    pending: Option<MboEvent>,
    engine: Engine,
    started: bool,
    done: bool,
}

/// Interactive replay session over a dataset.
pub struct Session {
    cursors: Vec<Cursor>,
    position: Ts,
}

impl Session {
    /// Opens a dataset (optional venue override).
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
                engine,
                started: false,
                done: false,
            });
        }
        Ok(Self {
            cursors,
            position: Ts::MIN,
        })
    }

    /// Replays every event with `ts_event <= ts`.
    pub fn advance_to(&mut self, ts: Ts) -> QtResult<()> {
        if ts <= self.position {
            return Ok(());
        }
        let mut sink = NullSink;
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
                c.engine.apply(&e, &mut sink)?;
            }
        }
        self.position = ts;
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
