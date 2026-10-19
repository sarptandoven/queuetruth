//! Sinks: `VecSink` (collect rows), `NullSink` (discard) and the Parquet table writers used by `run_dataset`.
//!
//! Parquet determinism: every table is written through a `Chunker` that encodes record batches of exactly
//! `CHUNK_ROWS` rows (the last one shorter), so a file's bytes depend only on its row sequence, never on how
//! partitions or threads produced the rows.
//!
//! Schema 1.1 (SPEC 3.11): every table is written in its full layout (`TableWriter::create_full`):
//! `qt.shadow_fills.liquidity` is derived from the fill reason (`crossed` / `crossed_at_live` = taker, A5) and
//! `qt.shadow_results` carries `ts_planned_end` / `planned_end_kind` (A2) from `Sink::shadow_result_planned`.

use std::path::Path;

use arrow_array::RecordBatch;
use qt_schema::codec::{
    PlannedEndCols, TableRow, batch_liquidity, batch_planned_ends, batch_to_shadow_fills, batch_to_shadow_results,
    liquidity_column, planned_end_columns, shadow_fills_to_batch, shadow_results_to_batch, with_added_columns,
};
use qt_schema::io::{FileEntry, TableWriter, read_table_as};
use qt_schema::rows::{
    BboRow, DepthRow, L2Row, Liquidity, RankTraceRow, RealOrderRow, ShadowFillRow, ShadowResultRow, TradeRow,
    TrajectoryRow,
};
use qt_types::{QtError, QtResult};

use crate::Sink;

/// Rows per encoded record batch.
pub const CHUNK_ROWS: usize = 65_536;

/// Collects every row in memory.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct VecSink {
    /// BBO rows.
    pub bbo: Vec<BboRow>,
    /// Trade rows.
    pub trades: Vec<TradeRow>,
    /// Depth rows.
    pub depth: Vec<DepthRow>,
    /// Rank-trace rows.
    pub rank_trace: Vec<RankTraceRow>,
    /// Shadow fills.
    pub shadow_fills: Vec<ShadowFillRow>,
    /// Liquidity of each shadow fill (same order as `shadow_fills`; schema 1.1).
    pub fill_liquidity: Vec<Liquidity>,
    /// Trajectory rows.
    pub trajectory: Vec<TrajectoryRow>,
    /// Shadow results.
    pub shadow_results: Vec<ShadowResultRow>,
    /// Real-order lives.
    pub real_orders: Vec<RealOrderRow>,
}

impl Sink for VecSink {
    fn bbo(&mut self, r: &BboRow) -> QtResult<()> {
        self.bbo.push(*r);
        Ok(())
    }
    fn trade(&mut self, r: &TradeRow) -> QtResult<()> {
        self.trades.push(*r);
        Ok(())
    }
    fn depth(&mut self, r: &DepthRow) -> QtResult<()> {
        self.depth.push(*r);
        Ok(())
    }
    fn rank_trace(&mut self, r: &RankTraceRow) -> QtResult<()> {
        self.rank_trace.push(*r);
        Ok(())
    }
    fn shadow_fill(&mut self, r: &ShadowFillRow) -> QtResult<()> {
        self.shadow_fills.push(r.clone());
        self.fill_liquidity.push(crate::shadow_fill_liquidity(r.reason));
        Ok(())
    }
    fn shadow_fill_liquidity(&mut self, r: &ShadowFillRow, liquidity: Liquidity) -> QtResult<()> {
        self.shadow_fills.push(r.clone());
        self.fill_liquidity.push(liquidity);
        Ok(())
    }
    fn trajectory(&mut self, r: &TrajectoryRow) -> QtResult<()> {
        self.trajectory.push(r.clone());
        Ok(())
    }
    fn shadow_result(&mut self, r: &ShadowResultRow) -> QtResult<()> {
        self.shadow_results.push(r.clone());
        Ok(())
    }
    fn real_order(&mut self, r: &RealOrderRow) -> QtResult<()> {
        self.real_orders.push(*r);
        Ok(())
    }
}

/// Discards everything.
#[derive(Clone, Copy, Debug, Default)]
pub struct NullSink;

impl Sink for NullSink {}

/// A row type the replay writers encode (fallible, so 1.1 columns can be appended).
pub(crate) trait Row: Sized {
    /// Table name.
    const TABLE: &'static str;
    /// Encodes rows (v0.1 layout, or full layout when the row carries the 1.1 columns).
    fn batch(rows: &[Self]) -> QtResult<RecordBatch>;
    /// Decodes a batch of a file written by `batch`.
    fn decode(batch: &RecordBatch) -> QtResult<Vec<Self>>;
}

macro_rules! plain_rows {
    ($($t:ty),*) => {$(
        impl Row for $t {
            const TABLE: &'static str = <$t as TableRow>::TABLE;
            fn batch(rows: &[Self]) -> QtResult<RecordBatch> {
                Ok(<$t as TableRow>::to_batch(rows))
            }
            fn decode(batch: &RecordBatch) -> QtResult<Vec<Self>> {
                <$t as TableRow>::from_batch(batch)
            }
        }
    )*};
}

plain_rows!(
    TradeRow,
    BboRow,
    DepthRow,
    RankTraceRow,
    TrajectoryRow,
    RealOrderRow,
    L2Row
);

/// A `qt.shadow_fills` row with its 1.1 `liquidity`.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct FillFull(pub(crate) ShadowFillRow, pub(crate) Liquidity);

impl Row for FillFull {
    const TABLE: &'static str = "qt.shadow_fills";
    fn batch(rows: &[Self]) -> QtResult<RecordBatch> {
        let core: Vec<ShadowFillRow> = rows.iter().map(|r| r.0.clone()).collect();
        with_added_columns(
            Self::TABLE,
            &shadow_fills_to_batch(&core),
            vec![liquidity_column(rows.iter().map(|r| Some(r.1)))],
        )
    }
    fn decode(batch: &RecordBatch) -> QtResult<Vec<Self>> {
        let rows = batch_to_shadow_fills(batch)?;
        let liq = batch_liquidity(batch, Self::TABLE)?
            .ok_or_else(|| QtError::Engine("shadow_fills: temporary file lacks the 1.1 liquidity column".into()))?;
        Ok(rows
            .into_iter()
            .zip(liq)
            .map(|(r, l)| {
                let l = l.unwrap_or(crate::shadow_fill_liquidity(r.reason));
                FillFull(r, l)
            })
            .collect())
    }
}

/// A `qt.shadow_results` row with its 1.1 planned-end columns.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct ResultFull(pub(crate) ShadowResultRow, pub(crate) PlannedEndCols);

impl Row for ResultFull {
    const TABLE: &'static str = "qt.shadow_results";
    fn batch(rows: &[Self]) -> QtResult<RecordBatch> {
        let core: Vec<ShadowResultRow> = rows.iter().map(|r| r.0.clone()).collect();
        let planned: Vec<PlannedEndCols> = rows.iter().map(|r| r.1).collect();
        with_added_columns(
            Self::TABLE,
            &shadow_results_to_batch(&core),
            planned_end_columns(&planned),
        )
    }
    fn decode(batch: &RecordBatch) -> QtResult<Vec<Self>> {
        let rows = batch_to_shadow_results(batch)?;
        let planned = batch_planned_ends(batch)?.ok_or_else(|| {
            QtError::Engine("shadow_results: temporary file lacks the 1.1 planned-end columns".into())
        })?;
        Ok(rows.into_iter().zip(planned).map(|(r, p)| ResultFull(r, p)).collect())
    }
}

/// One Parquet table written in fixed-size batches.
pub(crate) struct Chunker<R: Row> {
    name: String,
    writer: TableWriter,
    rows: Vec<R>,
}

impl<R: Row> Chunker<R> {
    /// `name` is the path relative to the output directory reported in `FileEntry::name`.
    pub(crate) fn create(root: &Path, name: &str) -> QtResult<Self> {
        Ok(Self {
            name: name.to_owned(),
            writer: TableWriter::create_full(&root.join(name), R::TABLE)?,
            rows: Vec::with_capacity(CHUNK_ROWS),
        })
    }

    #[inline]
    pub(crate) fn push(&mut self, r: R) -> QtResult<()> {
        self.rows.push(r);
        if self.rows.len() == CHUNK_ROWS {
            self.flush()?;
        }
        Ok(())
    }

    fn flush(&mut self) -> QtResult<()> {
        if !self.rows.is_empty() {
            self.writer.write(&R::batch(&self.rows)?)?;
            self.rows.clear();
        }
        Ok(())
    }

    /// Appends every row of another file of the same table, in order.
    pub(crate) fn append_file(&mut self, path: &Path) -> QtResult<()> {
        for b in read_table_as(path, R::TABLE)? {
            for r in R::decode(&b?)? {
                self.push(r)?;
            }
        }
        Ok(())
    }

    pub(crate) fn finish(mut self) -> QtResult<FileEntry> {
        self.flush()?;
        let mut f = self.writer.finish()?;
        f.name = self.name;
        Ok(f)
    }
}

/// The table files of one replay output (each optional).
pub(crate) struct Tables {
    pub(crate) trades: Option<Chunker<TradeRow>>,
    pub(crate) bbo: Option<Chunker<BboRow>>,
    pub(crate) depth: Option<Chunker<DepthRow>>,
    pub(crate) rank_trace: Option<Chunker<RankTraceRow>>,
    pub(crate) shadow_results: Option<Chunker<ResultFull>>,
    pub(crate) shadow_fills: Option<Chunker<FillFull>>,
    pub(crate) trajectory: Option<Chunker<TrajectoryRow>>,
    pub(crate) real_orders: Option<Chunker<RealOrderRow>>,
}

/// Which tables a replay writes.
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct TableSet {
    pub(crate) bbo: bool,
    pub(crate) depth: bool,
    pub(crate) rank_trace: bool,
    pub(crate) shadows: bool,
    pub(crate) real_orders: bool,
}

/// File names in the SPEC 5.1.6 order.
pub(crate) const NAMES: [&str; 8] = [
    "trades.parquet",
    "bbo.parquet",
    "depth.parquet",
    "rank_trace.parquet",
    "shadow_results.parquet",
    "shadow_fills.parquet",
    "queue_trajectory.parquet",
    "real_orders.parquet",
];

fn opt<R: Row>(on: bool, root: &Path, name: &str) -> QtResult<Option<Chunker<R>>> {
    if on {
        Chunker::create(root, name).map(Some)
    } else {
        Ok(None)
    }
}

impl Tables {
    pub(crate) fn create(root: &Path, set: TableSet) -> QtResult<Self> {
        Ok(Self {
            trades: opt(true, root, NAMES[0])?,
            bbo: opt(set.bbo, root, NAMES[1])?,
            depth: opt(set.depth, root, NAMES[2])?,
            rank_trace: opt(set.rank_trace, root, NAMES[3])?,
            shadow_results: opt(set.shadows, root, NAMES[4])?,
            shadow_fills: opt(set.shadows, root, NAMES[5])?,
            trajectory: opt(set.shadows, root, NAMES[6])?,
            real_orders: opt(set.real_orders, root, NAMES[7])?,
        })
    }

    /// Appends the files of a temporary table set (same layout) in order.
    pub(crate) fn append(&mut self, other_root: &Path) -> QtResult<()> {
        fn app<R: Row>(c: &mut Option<Chunker<R>>, root: &Path, name: &str) -> QtResult<()> {
            match c {
                Some(c) => c.append_file(&root.join(name)),
                None => Ok(()),
            }
        }
        app(&mut self.trades, other_root, NAMES[0])?;
        app(&mut self.bbo, other_root, NAMES[1])?;
        app(&mut self.depth, other_root, NAMES[2])?;
        app(&mut self.rank_trace, other_root, NAMES[3])?;
        app(&mut self.shadow_results, other_root, NAMES[4])?;
        app(&mut self.shadow_fills, other_root, NAMES[5])?;
        app(&mut self.trajectory, other_root, NAMES[6])?;
        app(&mut self.real_orders, other_root, NAMES[7])?;
        Ok(())
    }

    /// Closes every file; entries in the SPEC order.
    pub(crate) fn finish(self) -> QtResult<Vec<FileEntry>> {
        let mut out = Vec::new();
        fn fin<R: Row>(c: Option<Chunker<R>>, out: &mut Vec<FileEntry>) -> QtResult<()> {
            if let Some(c) = c {
                out.push(c.finish()?);
            }
            Ok(())
        }
        fin(self.trades, &mut out)?;
        fin(self.bbo, &mut out)?;
        fin(self.depth, &mut out)?;
        fin(self.rank_trace, &mut out)?;
        fin(self.shadow_results, &mut out)?;
        fin(self.shadow_fills, &mut out)?;
        fin(self.trajectory, &mut out)?;
        fin(self.real_orders, &mut out)?;
        Ok(out)
    }
}

impl Sink for Tables {
    fn bbo(&mut self, r: &BboRow) -> QtResult<()> {
        match &mut self.bbo {
            Some(c) => c.push(*r),
            None => Ok(()),
        }
    }
    fn trade(&mut self, r: &TradeRow) -> QtResult<()> {
        match &mut self.trades {
            Some(c) => c.push(*r),
            None => Ok(()),
        }
    }
    fn depth(&mut self, r: &DepthRow) -> QtResult<()> {
        match &mut self.depth {
            Some(c) => c.push(*r),
            None => Ok(()),
        }
    }
    fn rank_trace(&mut self, r: &RankTraceRow) -> QtResult<()> {
        match &mut self.rank_trace {
            Some(c) => c.push(*r),
            None => Ok(()),
        }
    }
    fn shadow_fill(&mut self, r: &ShadowFillRow) -> QtResult<()> {
        self.shadow_fill_liquidity(r, crate::shadow_fill_liquidity(r.reason))
    }
    fn shadow_fill_liquidity(&mut self, r: &ShadowFillRow, liquidity: Liquidity) -> QtResult<()> {
        match &mut self.shadow_fills {
            Some(c) => c.push(FillFull(r.clone(), liquidity)),
            None => Ok(()),
        }
    }
    fn trajectory(&mut self, r: &TrajectoryRow) -> QtResult<()> {
        match &mut self.trajectory {
            Some(c) => c.push(r.clone()),
            None => Ok(()),
        }
    }
    fn shadow_result(&mut self, r: &ShadowResultRow) -> QtResult<()> {
        match &self.shadow_results {
            Some(_) => Err(QtError::Engine(format!(
                "shadow result {}: the table writer needs its planned end (shadow_result_planned)",
                r.shadow_id
            ))),
            None => Ok(()),
        }
    }
    fn shadow_result_planned(&mut self, r: &ShadowResultRow, planned: PlannedEndCols) -> QtResult<()> {
        match &mut self.shadow_results {
            Some(c) => c.push(ResultFull(r.clone(), planned)),
            None => Ok(()),
        }
    }
    fn real_order(&mut self, r: &RealOrderRow) -> QtResult<()> {
        match &mut self.real_orders {
            Some(c) => c.push(*r),
            None => Ok(()),
        }
    }
}
