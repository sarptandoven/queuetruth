//! Dataset format and order-level ingest (SPEC 3.3, 4.1 `qt_ingest`, 5.1.6a, 8.10): `Dataset` / `Partition`
//! (streaming, feed order), `DatasetWriter`, and `ingest()` for ITCH 5.0, IEX DEEP+, DBN MBO, the et CSV layout and
//! our Parquet.
//!
//! A dataset directory holds `manifest.json`, `instruments.parquet`, one Parquet file per
//! `(session_date, instrument_id)` partition under `mbo/` (or `l2/`), optional `trades_source/` prints and
//! `data_quality.json`. Rows inside a partition are in source feed order and are never re-sorted.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use qt_schema::io::{TableReader, read_table_as, read_table_parallel_as};
use qt_schema::json::{DatasetKindName, DatasetManifest};
use qt_schema::rows::{L2Row, TradeRow};
use qt_schema::{codec, json, specs};
use qt_types::{Instrument, MboEvent, QtError, QtResult, Ts, VenueRules};

pub mod dq;
pub mod guard;
pub mod iex;
mod ingest_impl;
mod seqgap;
pub mod spill;
pub mod units;
mod writer;

pub use writer::{DEFAULT_MEMORY_ROWS, DatasetWriter, write_json};

/// Order-level (MBO) or L2-only dataset.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum DatasetKind {
    /// Order-level records (`mbo/`).
    Mbo,
    /// L2 rows (`l2/`, Tardis input).
    L2,
}

/// An opened dataset directory (SPEC 3.3).
pub struct Dataset {
    /// Dataset root.
    pub root: PathBuf,
    /// `manifest.json`.
    pub manifest: json::DatasetManifest,
    /// `instruments.parquet`.
    pub instruments: Vec<Instrument>,
    partitions: Vec<Partition>,
    by_key: BTreeMap<(u32, i32), usize>,
}

fn fmt_err(location: impl Into<String>, msg: impl Into<String>) -> QtError {
    QtError::InputFormat {
        location: location.into(),
        msg: msg.into(),
    }
}

fn check_rel_path(p: &str) -> bool {
    let path = Path::new(p);
    !p.is_empty() && path.is_relative() && path.components().all(|c| matches!(c, std::path::Component::Normal(_)))
}

impl Dataset {
    /// Opens and validates a dataset directory (manifest, instruments, partition files present).
    pub fn open(root: &Path) -> QtResult<Self> {
        let mpath = root.join("manifest.json");
        let text = std::fs::read_to_string(&mpath).map_err(|e| QtError::InputMissing {
            path: mpath.display().to_string(),
            msg: e.to_string(),
        })?;
        let manifest: DatasetManifest =
            serde_json::from_str(&text).map_err(|e| fmt_err(mpath.display().to_string(), e.to_string()))?;
        let ipath = root.join("instruments.parquet");
        let mut instruments = Vec::new();
        for b in read_table_as(&ipath, "qt.instruments")? {
            instruments.extend(codec::batch_to_instruments(&b?)?);
        }
        instruments.sort_by_key(|i| (i.session_date, i.instrument_id));
        let mut by_key = BTreeMap::new();
        for (n, i) in instruments.iter().enumerate() {
            if by_key.insert((i.instrument_id, i.session_date), n).is_some() {
                return Err(QtError::Duplicate {
                    what: "instrument row".into(),
                    id: format!("{} {}", i.instrument_id, i.session_date),
                });
            }
        }
        let kind = match manifest.kind {
            DatasetKindName::Mbo => DatasetKind::Mbo,
            DatasetKindName::L2 => DatasetKind::L2,
        };
        let mut partitions = Vec::with_capacity(manifest.partitions.len());
        for p in &manifest.partitions {
            let mut files = Vec::with_capacity(p.files.len());
            for f in &p.files {
                if !check_rel_path(&f.path) {
                    return Err(fmt_err(
                        mpath.display().to_string(),
                        format!("partition file path {:?}", f.path),
                    ));
                }
                let fp = root.join(&f.path);
                if !fp.is_file() {
                    return Err(QtError::InputMissing {
                        path: fp.display().to_string(),
                        msg: "partition file listed in manifest.json is missing".into(),
                    });
                }
                files.push(fp);
            }
            partitions.push(Partition {
                instrument_id: p.instrument_id,
                session_date: p.session_date,
                files,
                rows: p.rows,
                ts_min: p.ts_min,
                ts_max: p.ts_max,
                kind,
            });
        }
        partitions.sort_by_key(|p| (p.session_date, p.instrument_id));
        for w in partitions.windows(2) {
            if (w[0].session_date, w[0].instrument_id) == (w[1].session_date, w[1].instrument_id) {
                return Err(fmt_err(
                    mpath.display().to_string(),
                    format!("duplicate partition {} {}", w[0].instrument_id, w[0].session_date),
                ));
            }
        }
        Ok(Self {
            root: root.to_owned(),
            manifest,
            instruments,
            partitions,
            by_key,
        })
    }

    /// Partitions sorted by (session_date, instrument_id).
    pub fn partitions(&self) -> &[Partition] {
        &self.partitions
    }

    /// Instrument row of one session date.
    pub fn instrument(&self, instrument_id: u32, session_date: i32) -> Option<&Instrument> {
        self.by_key
            .get(&(instrument_id, session_date))
            .and_then(|&i| self.instruments.get(i))
    }

    /// Venue rules of the dataset.
    pub fn rules(&self) -> VenueRules {
        qt_types::rules(self.manifest.venue)
    }

    /// Partition holding `ts` for an instrument (by local session date: the instrument row whose
    /// `[midnight, next midnight)` contains `ts`; partitions without a row fall back to `[ts_min, ts_max]`).
    pub fn partition_for(&self, instrument_id: u32, ts: Ts) -> Option<&Partition> {
        self.partitions
            .iter()
            .filter(|p| p.instrument_id == instrument_id)
            .find(|p| match self.instrument(instrument_id, p.session_date) {
                Some(i) => {
                    let tz = time::tz_from_name(&i.session_tz);
                    let end = time::midnight_utc_ns(p.session_date.saturating_add(1), tz)
                        .unwrap_or_else(|_| i.midnight_utc_ns.saturating_add(time::NS_PER_DAY));
                    i.midnight_utc_ns <= ts && ts < end
                }
                None => p.ts_min <= ts && ts <= p.ts_max,
            })
    }
}

/// One instrument for one session date.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Partition {
    /// Instrument.
    pub instrument_id: u32,
    /// Session date (days since epoch).
    pub session_date: i32,
    /// Parquet files in order.
    pub files: Vec<PathBuf>,
    /// Rows.
    pub rows: u64,
    /// Minimum ts_event.
    pub ts_min: Ts,
    /// Maximum ts_event.
    pub ts_max: Ts,
    /// Kind.
    pub kind: DatasetKind,
}

/// Streams the rows of a list of Parquet files of one table, decoding batch by batch.
struct RowStream<T> {
    files: Vec<PathBuf>,
    next_file: usize,
    table: &'static str,
    reader: Option<TableReader>,
    rows: std::vec::IntoIter<T>,
    decode: fn(&arrow_array::RecordBatch) -> QtResult<Vec<T>>,
    failed: bool,
    /// Decode each file's columns on several threads (`read_table_parallel`; large partitions).
    parallel: bool,
}

impl<T> RowStream<T> {
    /// The next decoded batch (`None` at the end, and after an error).
    fn next_batch(&mut self) -> Option<QtResult<Vec<T>>> {
        loop {
            if self.failed {
                return None;
            }
            if let Some(rd) = self.reader.as_mut() {
                match rd.next() {
                    Some(Ok(b)) => {
                        let r = (self.decode)(&b);
                        self.failed = r.is_err();
                        return Some(r);
                    }
                    Some(Err(e)) => {
                        self.failed = true;
                        return Some(Err(e));
                    }
                    None => self.reader = None,
                }
            }
            let f = self.files.get(self.next_file)?.clone();
            self.next_file += 1;
            let opened = if self.parallel {
                read_table_parallel_as(&f, self.table)
            } else {
                read_table_as(&f, self.table)
            };
            match opened {
                Ok(r) => self.reader = Some(r),
                Err(e) => {
                    self.failed = true;
                    return Some(Err(e));
                }
            }
        }
    }
}

impl<T> Iterator for RowStream<T> {
    type Item = QtResult<T>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if let Some(r) = self.rows.next() {
                return Some(Ok(r));
            }
            match self.next_batch()? {
                Ok(v) => self.rows = v.into_iter(),
                Err(e) => return Some(Err(e)),
            }
        }
    }
}

/// Partitions of at least this many rows are decoded on a background thread (`Prefetch`).
const PREFETCH_MIN_ROWS: u64 = 131_072;
/// Decoded batches queued ahead of the consumer.
const PREFETCH_BATCHES: usize = 4;

type Batches<T> = std::sync::mpsc::Receiver<QtResult<Vec<T>>>;

/// A `RowStream` decoded on a background thread, up to `PREFETCH_BATCHES` batches ahead of the consumer: the same
/// rows and the same error, in the same order (the decoder stops at its first error, like `RowStream`).
struct Prefetch<T> {
    rx: Option<Batches<T>>,
    rows: std::vec::IntoIter<T>,
    handle: Option<std::thread::JoinHandle<()>>,
}

impl<T: Send + 'static> Prefetch<T> {
    /// Starts decoding `make()` on a thread; `None` when no thread could be started.
    fn start(make: impl FnOnce() -> RowStream<T> + Send + 'static) -> Option<Self> {
        let (tx, rx) = std::sync::mpsc::sync_channel(PREFETCH_BATCHES);
        let handle = std::thread::Builder::new()
            .name("qt-decode".into())
            .spawn(move || {
                let mut s = make();
                while let Some(b) = s.next_batch() {
                    if tx.send(b).is_err() {
                        return; // consumer gone
                    }
                }
            })
            .ok()?;
        Some(Self {
            rx: Some(rx),
            rows: Vec::new().into_iter(),
            handle: Some(handle),
        })
    }
}

impl<T> Iterator for Prefetch<T> {
    type Item = QtResult<T>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if let Some(r) = self.rows.next() {
                return Some(Ok(r));
            }
            let rx = self.rx.as_ref()?;
            match rx.recv() {
                Ok(Ok(v)) => self.rows = v.into_iter(),
                Ok(Err(e)) => {
                    self.rx = None;
                    return Some(Err(e));
                }
                Err(_) => {
                    // decoder finished (or died: never end a partition early without an error)
                    self.rx = None;
                    let panicked = self.handle.take().is_some_and(|h| h.join().is_err());
                    return panicked.then(|| Err(QtError::Engine("Parquet decode thread panicked".into())));
                }
            }
        }
    }
}

impl<T> Drop for Prefetch<T> {
    fn drop(&mut self) {
        self.rx = None; // unblocks the decoder
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

/// `RowStream` of a partition's files, decoded ahead on a thread when the partition is large.
fn partition_stream<T: Send + 'static>(
    files: Vec<PathBuf>,
    rows: u64,
    table: &'static str,
    decode: fn(&arrow_array::RecordBatch) -> QtResult<Vec<T>>,
) -> Box<dyn Iterator<Item = QtResult<T>> + Send> {
    if rows >= PREFETCH_MIN_ROWS {
        let f = files.clone();
        let make = move || RowStream {
            parallel: true,
            ..stream(f, table, decode)
        };
        if let Some(p) = Prefetch::start(make) {
            return Box::new(p);
        }
    }
    Box::new(stream(files, table, decode))
}

fn stream<T: 'static>(
    files: Vec<PathBuf>,
    table: &'static str,
    decode: fn(&arrow_array::RecordBatch) -> QtResult<Vec<T>>,
) -> RowStream<T> {
    RowStream {
        files,
        next_file: 0,
        table,
        reader: None,
        rows: Vec::new().into_iter(),
        decode,
        failed: false,
        parallel: false,
    }
}

impl Partition {
    /// MBO records in source feed order (streaming).
    pub fn mbo(&self) -> QtResult<Box<dyn Iterator<Item = QtResult<MboEvent>> + Send>> {
        if self.kind != DatasetKind::Mbo {
            return Err(QtError::Config(
                "exact replay needs MBO: this is an L2-only dataset".into(),
            ));
        }
        Ok(partition_stream(
            self.files.clone(),
            self.rows,
            "qt.mbo",
            codec::batch_to_mbo,
        ))
    }

    /// L2 rows in order (streaming).
    pub fn l2(&self) -> QtResult<Box<dyn Iterator<Item = QtResult<L2Row>> + Send>> {
        if self.kind != DatasetKind::L2 {
            return Err(QtError::Config(
                "this is an MBO dataset: synthesize L2 rows with qt_l2".into(),
            ));
        }
        Ok(partition_stream(
            self.files.clone(),
            self.rows,
            "qt.l2",
            codec::batch_to_l2,
        ))
    }

    /// The `trades_source/date=.../instrument_id=.../` directory of this partition.
    pub fn source_trades_dir(&self) -> Option<PathBuf> {
        // <root>/<mbo|l2>/date=.../instrument_id=.../part-00000.parquet
        let root = self.files.first()?.parent()?.parent()?.parent()?.parent()?;
        Some(
            root.join("trades_source")
                .join(format!("date={}", time::format_date(self.session_date)))
                .join(format!("instrument_id={}", self.instrument_id)),
        )
    }

    /// Source trade prints of the partition, when the format has them.
    pub fn source_trades(&self) -> QtResult<Option<Vec<TradeRow>>> {
        let Some(dir) = self.source_trades_dir() else {
            return Ok(None);
        };
        if !dir.is_dir() {
            return Ok(None);
        }
        let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
            .map_err(|e| QtError::InputMissing {
                path: dir.display().to_string(),
                msg: e.to_string(),
            })?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| {
                p.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| n.starts_with("part-") && n.ends_with(".parquet"))
            })
            .collect();
        files.sort();
        let mut out = Vec::new();
        for r in stream(files, "qt.trades", codec::batch_to_trades) {
            out.push(r?);
        }
        Ok(Some(out))
    }
}

/// `ingest` for formats `itch | dbn | etcsv | parquet`.
pub fn ingest(spec: &specs::IngestSpec) -> QtResult<json::IngestSummary> {
    ingest_impl::ingest(spec)
}

/// Nasdaq TotalView-ITCH 5.0: `Reader` (gz/raw framing), `parse`, `encode`, `encode_file(messages, path, gzip)` for
/// synthetic fixtures, and `Mapper` (et convert semantics -> `MboEvent` + `TradeRow`).
pub mod itch;

/// Databento DBN MBO reader (the `dbn` crate): multi-file chronological, zstd, framing / version / truncation checks,
/// dated symbology (`Symbology`: ids per UTC date) and data vintage (`vintage`).
pub mod dbn;

/// et `mbo.csv` / `trades.csv` / `instruments.csv` readers.
pub mod etcsv;

/// US/Eastern local midnight as UTC ns (DST rules as et `time.rs`) and date parsing.
pub mod time;
