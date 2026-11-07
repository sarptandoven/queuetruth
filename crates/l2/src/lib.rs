//! L2 view synthesis from MBO (SPEC 4.1 `qt_l2`, 5.3.1, 5.3.5): `L2Builder` with its own MBP aggregate
//! (independent of `qt_book`), cadence / conflation, out-of-view handling; `VisibleBook` built only from emitted
//! rows; hftbacktest `.npz` export; BBO/trades derived from L2-only datasets.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use qt_ingest::spill::CHUNK_ROWS;
use qt_ingest::{Dataset, DatasetKind, Partition};
use qt_schema::codec;
use qt_schema::io::{FileEntry, TableWriter};
use qt_schema::rows::{BboRow, L2Kind, L2Row, TradeKind, TradeRow, l2flags};
use qt_schema::specs;
use qt_types::{BookSide, Dur, Px, QtError, QtResult, Qty};

mod builder;
mod npz;
mod zipw;

pub use builder::{L2Builder, bucket_end};
pub use npz::{
    EVENT_SIZE, HbtAction, hbt_events, hbt_orders_path, npy_bytes, write_hbt_npz, write_hbt_npz_with_orders,
    write_hbt_orders,
};

/// Trade rows before or after the depth rows of a batch.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum TradePosition {
    /// Trades first.
    #[default]
    Before,
    /// Depth first.
    After,
}

/// L2 synthesis settings.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct L2Config {
    /// Top-N levels per side (0 = all).
    pub depth_levels: u16,
    /// Conflation cadence (0 = per F_LAST event).
    pub cadence_ns: Dur,
    /// Trade-vs-depth ordering.
    pub trade_position: TradePosition,
    /// Emit order counts.
    pub include_counts: bool,
    /// Emit hidden trades.
    pub emit_hidden_trades: bool,
}

impl Default for L2Config {
    fn default() -> Self {
        Self {
            depth_levels: 10,
            cadence_ns: 0,
            trade_position: TradePosition::Before,
            include_counts: true,
            emit_hidden_trades: true,
        }
    }
}

impl From<&specs::L2Config> for L2Config {
    fn from(s: &specs::L2Config) -> Self {
        Self {
            depth_levels: s.depth_levels,
            cadence_ns: s.cadence_ns,
            trade_position: match s.trade_position {
                specs::TradePositionName::Before => TradePosition::Before,
                specs::TradePositionName::After => TradePosition::After,
            },
            include_counts: s.include_counts,
            emit_hidden_trades: s.emit_hidden_trades,
        }
    }
}

impl L2Config {
    /// `QT-CONFIG-001` for a negative cadence.
    pub fn validate(&self) -> QtResult<()> {
        if self.cadence_ns < 0 {
            return Err(QtError::Config(format!(
                "l2.cadence_ns: must be >= 0, got {}",
                self.cadence_ns
            )));
        }
        Ok(())
    }
}

/// What an L2 model can see: built only from emitted L2 rows.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct VisibleBook {
    sides: [BTreeMap<Px, (Qty, Option<u32>)>; 2],
}

const fn si(s: BookSide) -> usize {
    match s {
        BookSide::Bid => 0,
        BookSide::Ask => 1,
    }
}

impl VisibleBook {
    /// Empty view.
    pub fn new() -> Self {
        Self::default()
    }

    /// Applies one row (`clear` empties; depth/snapshot set the level, 0 removes; trades change nothing).
    pub fn apply(&mut self, r: &L2Row) {
        match r.kind {
            L2Kind::Clear => {
                self.sides[0].clear();
                self.sides[1].clear();
            }
            L2Kind::Depth | L2Kind::Snapshot => {
                let (Some(s), Some(px)) = (BookSide::from_side(r.side), r.price) else {
                    return;
                };
                let m = &mut self.sides[si(s)];
                if r.new_qty == 0 {
                    m.remove(&px);
                } else {
                    m.insert(px, (r.new_qty, r.new_ct));
                }
            }
            L2Kind::Trade => {}
        }
    }

    /// Visible quantity at a price (0 if absent).
    pub fn qty_at(&self, side: BookSide, px: Px) -> Qty {
        self.sides[si(side)].get(&px).map_or(0, |l| l.0)
    }

    /// The price is in the visible view.
    pub fn in_view(&self, side: BookSide, px: Px) -> bool {
        self.sides[si(side)].contains_key(&px)
    }

    /// Best visible level.
    pub fn best(&self, side: BookSide) -> Option<(Px, Qty)> {
        let m = &self.sides[si(side)];
        let e = match side {
            BookSide::Bid => m.iter().next_back(),
            BookSide::Ask => m.iter().next(),
        };
        e.map(|(p, l)| (*p, l.0))
    }

    /// Top `n` visible levels (price, qty, count), best first (`n = 0`: all).
    pub fn levels(&self, side: BookSide, n: usize) -> Vec<(Px, Qty, Option<u32>)> {
        let m = &self.sides[si(side)];
        let n = if n == 0 { usize::MAX } else { n };
        match side {
            BookSide::Bid => m.iter().rev().take(n).map(|(p, l)| (*p, l.0, l.1)).collect(),
            BookSide::Ask => m.iter().take(n).map(|(p, l)| (*p, l.0, l.1)).collect(),
        }
    }

    /// Number of visible levels on a side.
    pub fn len(&self, side: BookSide) -> usize {
        self.sides[si(side)].len()
    }

    /// True when both sides are empty.
    pub fn is_empty(&self) -> bool {
        self.sides[0].is_empty() && self.sides[1].is_empty()
    }
}

/// L2 rows of one MBO partition (whole partition in memory).
pub fn synthesize_partition(part: &Partition, ds: &Dataset, cfg: &L2Config) -> QtResult<Vec<L2Row>> {
    cfg.validate()?;
    let mut b = L2Builder::new(*cfg, ds.rules(), part.instrument_id);
    let mut out = Vec::new();
    let mut last_ts = None;
    for e in part.mbo()? {
        let e = e?;
        last_ts = Some(e.ts_event);
        b.push(&e, &mut out);
    }
    if let Some(ts) = last_ts {
        b.flush(ts, &mut out);
    }
    Ok(out)
}

/// Runs `f` on every partition with up to `threads` workers; results in partition order.
pub fn map_partitions<T: Send>(
    parts: &[Partition],
    threads: usize,
    f: impl Fn(&Partition) -> QtResult<T> + Sync,
) -> QtResult<Vec<T>> {
    let slots: Vec<Mutex<Option<QtResult<T>>>> = parts.iter().map(|_| Mutex::new(None)).collect();
    let next = AtomicUsize::new(0);
    let workers = threads.max(1).min(parts.len().max(1));
    std::thread::scope(|s| {
        for _ in 0..workers {
            s.spawn(|| {
                loop {
                    let j = next.fetch_add(1, Ordering::Relaxed);
                    let Some(p) = parts.get(j) else { break };
                    let r = f(p);
                    if let Ok(mut slot) = slots[j].lock() {
                        *slot = Some(r);
                    }
                }
            });
        }
    });
    slots
        .into_iter()
        .map(|m| {
            m.into_inner()
                .map_err(|_| QtError::Engine("partition worker panicked".into()))?
                .ok_or_else(|| QtError::Engine("partition worker produced no result".into()))?
        })
        .collect()
}

fn write_rows<T>(
    path: &Path,
    table: &str,
    rows: &[T],
    enc: fn(&[T]) -> arrow_array::RecordBatch,
) -> QtResult<FileEntry> {
    let mut w = TableWriter::create(path, table)?;
    for c in rows.chunks(CHUNK_ROWS) {
        w.write(&enc(c))?;
    }
    w.finish()
}

/// Writes `l2/date=.../instrument_id=.../part-00000.parquet` for every partition of an MBO dataset.
pub fn synthesize(ds: &Dataset, cfg: &L2Config, out_root: &Path, threads: usize) -> QtResult<Vec<FileEntry>> {
    cfg.validate()?;
    if ds.partitions().first().is_some_and(|p| p.kind != DatasetKind::Mbo) {
        return Err(QtError::Config("synthesize: the dataset is already L2-only".into()));
    }
    map_partitions(ds.partitions(), threads, |p| {
        let rows = synthesize_partition(p, ds, cfg)?;
        let rel = format!(
            "l2/date={}/instrument_id={}/part-00000.parquet",
            qt_ingest::time::format_date(p.session_date),
            p.instrument_id
        );
        let mut e = write_rows(&out_root.join(&rel), "qt.l2", &rows, codec::l2_to_batch)?;
        e.name = rel;
        Ok(e)
    })
}

/// The six values compared in BBO change mode.
type BboKey = (Option<Px>, Qty, u32, Option<Px>, Qty, u32);

/// BBO (change mode, null counts -> 0) and trades derived from the L2 rows of one partition.
pub fn bbo_trades_from_rows(rows: impl IntoIterator<Item = QtResult<L2Row>>) -> QtResult<(Vec<BboRow>, Vec<TradeRow>)> {
    let mut vb = VisibleBook::new();
    let mut bbo = Vec::new();
    let mut trades = Vec::new();
    let mut prev: BboKey = (None, 0, 0, None, 0, 0);
    let top = |vb: &VisibleBook, s: BookSide| -> (Option<Px>, Qty, u32) {
        match vb.levels(s, 1).first() {
            Some(&(p, q, c)) => (Some(p), q, c.unwrap_or(0)),
            None => (None, 0, 0),
        }
    };
    for r in rows {
        let r = r?;
        vb.apply(&r);
        if r.kind == L2Kind::Trade
            && let Some(px) = r.price
        {
            trades.push(TradeRow {
                ts_event: r.ts_event,
                instrument_id: r.instrument_id,
                sequence: r.sequence,
                price: px,
                size: r.trade_qty,
                aggressor_side: r.aggressor,
                kind: if r.flags & l2flags::HIDDEN_TRADE != 0 {
                    TradeKind::Hidden
                } else {
                    TradeKind::Displayed
                },
                maker_order_id: None,
            });
        }
        if r.flags & l2flags::LAST_IN_BATCH != 0 {
            let (bp, bq, bc) = top(&vb, BookSide::Bid);
            let (ap, aq, ac) = top(&vb, BookSide::Ask);
            let cur = (bp, bq, bc, ap, aq, ac);
            if prev != cur {
                bbo.push(BboRow {
                    ts_event: r.ts_event,
                    instrument_id: r.instrument_id,
                    sequence: r.sequence,
                    bid_px: bp,
                    bid_sz: bq,
                    bid_ct: bc,
                    ask_px: ap,
                    ask_sz: aq,
                    ask_ct: ac,
                });
                prev = cur;
            }
        }
    }
    Ok((bbo, trades))
}

/// For L2-only datasets: derive qt.bbo (change mode, counts null -> 0) and qt.trades from the L2 rows.
/// Writes `bbo.parquet` and `trades.parquet` under `out_root` (partition order, emission order).
pub fn bbo_from_l2(ds: &Dataset, out_root: &Path, threads: usize) -> QtResult<Vec<FileEntry>> {
    if ds.partitions().first().is_some_and(|p| p.kind != DatasetKind::L2) {
        return Err(QtError::Config(
            "bbo_from_l2: the dataset is MBO; replay derives BBO and trades".into(),
        ));
    }
    let mut bw = TableWriter::create(&out_root.join("bbo.parquet"), "qt.bbo")?;
    let mut tw = TableWriter::create(&out_root.join("trades.parquet"), "qt.trades")?;
    let threads = threads.max(1);
    // Partitions are processed `threads` at a time and written in order (bounded memory).
    for group in ds.partitions().chunks(threads) {
        let res = map_partitions(group, threads, |p| bbo_trades_from_rows(p.l2()?))?;
        for (bbo, trades) in res {
            for c in bbo.chunks(CHUNK_ROWS) {
                bw.write(&codec::bbo_to_batch(c))?;
            }
            for c in trades.chunks(CHUNK_ROWS) {
                tw.write(&codec::trades_to_batch(c))?;
            }
        }
    }
    let mut b = bw.finish()?;
    b.name = "bbo.parquet".into();
    let mut t = tw.finish()?;
    t.name = "trades.parquet".into();
    Ok(vec![b, t])
}
