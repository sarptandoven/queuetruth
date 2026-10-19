//! `DatasetWriter`: routes rows into `(session_date, instrument_id)` partitions, keeps memory bounded by spilling
//! partition buffers to fixed-size binary scratch files under `<root>/.spill/`, and on `finish` writes one Parquet
//! file per partition (in parallel, byte-identical whatever the buffering), `instruments.parquet`,
//! `data_quality.json` and `manifest.json` (SPEC 3.3, 3.10).

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use indexmap::IndexMap;
use qt_schema::codec;
use qt_schema::io::{FileEntry, TableWriter, sha256_file};
use qt_schema::json::{self, DataQuality, DatasetKindName, DatasetManifest, InputFile, PartitionEntry, PartitionFile};
use qt_schema::rows::{L2Row, TradeRow};
use qt_types::{Instrument, MboEvent, QtError, QtResult, Ts, ValidationStatus, Venue, rules};

use crate::DatasetKind;
use crate::dq::apply_verdict;
use crate::spill::{FixedRow, spill_append, spill_path, write_table};
use crate::time::{NS_PER_DAY, format_date, midnight_utc_ns, tz_from_name};

/// Total rows buffered in memory (all partitions, all tables) before the largest buffers are spilled.
pub const DEFAULT_MEMORY_ROWS: usize = 8_000_000;

struct PartBuf<T> {
    pending: Vec<T>,
    spilled: bool,
    rows: u64,
    ts_min: Ts,
    ts_max: Ts,
}

impl<T> Default for PartBuf<T> {
    fn default() -> Self {
        Self {
            pending: Vec::new(),
            spilled: false,
            rows: 0,
            ts_min: Ts::MAX,
            ts_max: Ts::MIN,
        }
    }
}

struct Parts<T> {
    index: HashMap<(i32, u32), usize>,
    keys: Vec<(i32, u32)>,
    bufs: Vec<PartBuf<T>>,
    last: Option<((i32, u32), usize)>,
    pending: usize,
}

impl<T: FixedRow> Parts<T> {
    fn new() -> Self {
        Self {
            index: HashMap::new(),
            keys: Vec::new(),
            bufs: Vec::new(),
            last: None,
            pending: 0,
        }
    }

    #[inline]
    fn slot(&mut self, key: (i32, u32)) -> usize {
        if let Some((k, i)) = self.last {
            if k == key {
                return i;
            }
        }
        let i = match self.index.get(&key) {
            Some(&i) => i,
            None => {
                let i = self.bufs.len();
                self.index.insert(key, i);
                self.keys.push(key);
                self.bufs.push(PartBuf::default());
                i
            }
        };
        self.last = Some((key, i));
        i
    }

    #[inline]
    fn push(&mut self, key: (i32, u32), r: T) {
        let i = self.slot(key);
        let b = &mut self.bufs[i];
        let ts = r.ts();
        b.ts_min = b.ts_min.min(ts);
        b.ts_max = b.ts_max.max(ts);
        b.rows += 1;
        b.pending.push(r);
        self.pending += 1;
    }

    /// Spills the largest pending buffers until at most `target` rows remain in memory.
    fn spill_down_to(&mut self, dir: &Path, target: usize) -> QtResult<()> {
        if self.pending <= target {
            return Ok(());
        }
        std::fs::create_dir_all(dir).map_err(|e| QtError::Engine(format!("{}: {e}", dir.display())))?;
        let mut order: Vec<usize> = (0..self.bufs.len())
            .filter(|&i| !self.bufs[i].pending.is_empty())
            .collect();
        order.sort_by_key(|&i| std::cmp::Reverse(self.bufs[i].pending.len()));
        for i in order {
            if self.pending <= target {
                break;
            }
            let (date, iid) = self.keys[i];
            let b = &mut self.bufs[i];
            spill_append(&spill_path(dir, T::TABLE, date, iid), &b.pending)?;
            self.pending -= b.pending.len();
            b.pending = Vec::new();
            b.spilled = true;
        }
        Ok(())
    }
}

/// Writes a dataset directory (layout and `dataset_id` rule of SPEC 3.3 / 3.10).
pub struct DatasetWriter {
    root: PathBuf,
    spill_dir: PathBuf,
    kind: DatasetKind,
    venue: Venue,
    source_format: String,
    source_venue: String,
    validation: ValidationStatus,
    instruments: Vec<Instrument>,
    routes: HashMap<u32, Vec<(Ts, Ts, i32)>>,
    inputs: Vec<InputFile>,
    mbo: Parts<MboEvent>,
    l2: Parts<L2Row>,
    trades: Parts<TradeRow>,
    memory_rows: usize,
    threads: usize,
    warnings: Vec<String>,
}

fn cfg_err(msg: impl Into<String>) -> QtError {
    QtError::Config(msg.into())
}

fn engine_err(path: &Path, e: impl std::fmt::Display) -> QtError {
    QtError::Engine(format!("{}: {e}", path.display()))
}

impl DatasetWriter {
    /// Creates the dataset root (which must not exist or be empty).
    pub fn create(root: &Path, kind: DatasetKind, venue: Venue, source_format: &str) -> QtResult<Self> {
        if root.exists() {
            let mut it = std::fs::read_dir(root).map_err(|e| cfg_err(format!("out: {}: {e}", root.display())))?;
            if it.next().is_some() {
                return Err(cfg_err(format!("out: {} exists and is not empty", root.display())));
            }
        }
        std::fs::create_dir_all(root).map_err(|e| cfg_err(format!("out: {}: {e}", root.display())))?;
        let threads = std::thread::available_parallelism().map_or(1, |n| n.get()).clamp(1, 4);
        Ok(Self {
            root: root.to_owned(),
            spill_dir: root.join(".spill"),
            kind,
            venue,
            source_format: source_format.to_owned(),
            source_venue: venue.name().to_owned(),
            validation: rules(venue).validation,
            instruments: Vec::new(),
            routes: HashMap::new(),
            inputs: Vec::new(),
            mbo: Parts::new(),
            l2: Parts::new(),
            trades: Parts::new(),
            memory_rows: DEFAULT_MEMORY_ROWS,
            threads,
            warnings: Vec::new(),
        })
    }

    /// Instrument rows (replaces any previous rows; rows route `push_*` by instrument and local session).
    pub fn set_instruments(&mut self, v: Vec<Instrument>) {
        self.instruments = v;
        self.rebuild_routes();
    }

    fn rebuild_routes(&mut self) {
        self.routes.clear();
        for i in &self.instruments {
            let tz = tz_from_name(&i.session_tz);
            let end = midnight_utc_ns(i.session_date.saturating_add(1), tz)
                .unwrap_or_else(|_| i.midnight_utc_ns.saturating_add(NS_PER_DAY));
            self.routes
                .entry(i.instrument_id)
                .or_default()
                .push((i.midnight_utc_ns, end, i.session_date));
        }
        for v in self.routes.values_mut() {
            v.sort_unstable();
        }
    }

    /// Source venue written to the manifest (default: the venue name).
    pub fn set_source_venue(&mut self, s: &str) {
        self.source_venue = s.to_owned();
    }

    /// Validation label written to the manifest (default: the venue rules' label).
    pub fn set_validation_status(&mut self, v: ValidationStatus) {
        self.validation = v;
    }

    /// Rows kept in memory before spilling (minimum 1).
    pub fn set_memory_rows(&mut self, n: usize) {
        self.memory_rows = n.max(1);
    }

    /// Worker threads for the final Parquet encoding (output does not depend on it).
    pub fn set_threads(&mut self, n: usize) {
        self.threads = n.max(1);
    }

    /// Adds a warning for the ingest summary: printed to stderr and appended to `data_quality.reasons` after the
    /// verdict is applied (interim channel until `qt.ingest_summary` has a `warnings` field).
    pub fn add_warning(&mut self, msg: impl Into<String>) {
        let msg = msg.into();
        eprintln!("queuetruth ingest: warning: {msg}");
        self.warnings.push(msg);
    }

    /// Records an already hashed input for the manifest.
    pub fn add_input_file(&mut self, f: InputFile) {
        self.inputs.push(f);
    }

    /// Records an input's size and sha256 for the manifest (path kept as given).
    pub fn add_input(&mut self, path: &Path) -> QtResult<()> {
        let bytes = std::fs::metadata(path)
            .map_err(|e| QtError::InputMissing {
                path: path.display().to_string(),
                msg: e.to_string(),
            })?
            .len();
        self.inputs.push(InputFile {
            path: path.display().to_string(),
            bytes,
            sha256: sha256_file(path)?,
        });
        Ok(())
    }

    /// Session date of a record for `instrument_id` at `ts` from the instrument rows: the row whose local
    /// `[midnight, next midnight)` holds `ts`; an instrument with a single row takes every record.
    pub fn route(&self, instrument_id: u32, ts: Ts) -> QtResult<i32> {
        let rows = self.routes.get(&instrument_id).ok_or_else(|| QtError::InputFormat {
            location: format!("instrument_id {instrument_id}"),
            msg: "record for an instrument without an instrument row".into(),
        })?;
        if let [only] = rows.as_slice() {
            return Ok(only.2);
        }
        rows.iter()
            .find(|(a, b, _)| *a <= ts && ts < *b)
            .map(|r| r.2)
            .ok_or_else(|| QtError::InputFormat {
                location: format!("instrument_id {instrument_id}"),
                msg: format!("ts_event {ts} falls in no session of the instrument rows"),
            })
    }

    fn check_kind(&self, want: DatasetKind) -> QtResult<()> {
        if self.kind == want {
            Ok(())
        } else {
            Err(QtError::Engine(format!(
                "DatasetWriter: {want:?} row pushed to a {:?} dataset",
                self.kind
            )))
        }
    }

    fn maybe_spill(&mut self) -> QtResult<()> {
        let total = self.mbo.pending + self.l2.pending + self.trades.pending;
        if total > self.memory_rows {
            let target = self.memory_rows / 2;
            let dir = self.spill_dir.clone();
            self.mbo.spill_down_to(&dir, target / 2)?;
            self.l2.spill_down_to(&dir, target / 2)?;
            self.trades.spill_down_to(&dir, target / 4)?;
        }
        Ok(())
    }

    /// Appends an MBO record (routed by instrument and local date).
    pub fn push_mbo(&mut self, e: &MboEvent) -> QtResult<()> {
        self.check_kind(DatasetKind::Mbo)?;
        let date = self.route(e.instrument_id, e.ts_event)?;
        self.push_mbo_at(e, date)
    }

    /// Appends an MBO record to the partition of `session_date`.
    pub fn push_mbo_at(&mut self, e: &MboEvent, session_date: i32) -> QtResult<()> {
        self.check_kind(DatasetKind::Mbo)?;
        if e.ts_event < 0 || e.ts_recv < 0 {
            return Err(QtError::InputFormat {
                location: format!("instrument_id {} sequence {}", e.instrument_id, e.sequence),
                msg: "market-data timestamps must be >= 0".into(),
            });
        }
        self.mbo.push((session_date, e.instrument_id), *e);
        if self.mbo.pending > self.memory_rows {
            self.maybe_spill()?;
        }
        Ok(())
    }

    /// Appends an L2 row.
    pub fn push_l2(&mut self, r: &L2Row) -> QtResult<()> {
        self.check_kind(DatasetKind::L2)?;
        let date = self.route(r.instrument_id, r.ts_event)?;
        self.push_l2_at(r, date)
    }

    /// Appends an L2 row to the partition of `session_date`.
    pub fn push_l2_at(&mut self, r: &L2Row, session_date: i32) -> QtResult<()> {
        self.check_kind(DatasetKind::L2)?;
        self.l2.push((session_date, r.instrument_id), *r);
        if self.l2.pending > self.memory_rows {
            self.maybe_spill()?;
        }
        Ok(())
    }

    /// Appends a source trade print.
    pub fn push_source_trade(&mut self, t: &TradeRow) -> QtResult<()> {
        let date = self.route(t.instrument_id, t.ts_event)?;
        self.push_source_trade_at(t, date)
    }

    /// Appends a source trade print to the partition of `session_date`.
    pub fn push_source_trade_at(&mut self, t: &TradeRow, session_date: i32) -> QtResult<()> {
        self.trades.push((session_date, t.instrument_id), *t);
        if self.trades.pending + self.mbo.pending + self.l2.pending > self.memory_rows {
            self.maybe_spill()?;
        }
        Ok(())
    }

    /// Partitions that received rows so far, sorted `(session_date, instrument_id)`.
    pub fn partition_keys(&self) -> Vec<(i32, u32)> {
        let mut k: Vec<(i32, u32)> = match self.kind {
            DatasetKind::Mbo => self.mbo.keys.clone(),
            DatasetKind::L2 => self.l2.keys.clone(),
        };
        k.extend(self.trades.keys.iter().copied());
        k.sort_unstable();
        k.dedup();
        k
    }

    /// Writes the manifest and data quality and returns the ingest summary (`spec` left empty for the caller).
    pub fn finish(mut self, mut dq: DataQuality) -> QtResult<json::IngestSummary> {
        let (dir_name, main_rows) = match self.kind {
            DatasetKind::Mbo => ("mbo", self.mbo.bufs.iter().map(|b| b.rows).sum::<u64>()),
            DatasetKind::L2 => ("l2", self.l2.bufs.iter().map(|b| b.rows).sum::<u64>()),
        };
        let mut inst_keys: Vec<(i32, u32)> = self
            .instruments
            .iter()
            .map(|i| (i.session_date, i.instrument_id))
            .collect();
        inst_keys.sort_unstable();
        for w in inst_keys.windows(2) {
            if w[0] == w[1] {
                return Err(QtError::Duplicate {
                    what: "instrument row".into(),
                    id: format!("{} {}", w[0].1, format_date(w[0].0)),
                });
            }
        }
        for k in self.partition_keys() {
            if inst_keys.binary_search(&k).is_err() {
                return Err(QtError::Engine(format!(
                    "DatasetWriter: partition instrument_id {} date {} has no instrument row",
                    k.1,
                    format_date(k.0)
                )));
            }
        }

        let main = match self.kind {
            DatasetKind::Mbo => {
                let parts = std::mem::replace(&mut self.mbo, Parts::new());
                self.write_parts(&parts, dir_name)?
            }
            DatasetKind::L2 => {
                let parts = std::mem::replace(&mut self.l2, Parts::new());
                self.write_parts(&parts, dir_name)?
            }
        };
        let tparts = std::mem::replace(&mut self.trades, Parts::new());
        let trades = self.write_parts(&tparts, "trades_source")?;

        let mut files: Vec<FileEntry> = Vec::new();
        let mut partitions = Vec::new();
        let mut shas = Vec::new();
        for (key, entry, rows, ts_min, ts_max) in &main {
            shas.push(entry.sha256.clone());
            partitions.push(PartitionEntry {
                instrument_id: key.1,
                session_date: key.0,
                files: vec![PartitionFile {
                    path: entry.name.clone(),
                    rows: *rows,
                    sha256: entry.sha256.clone(),
                }],
                rows: *rows,
                ts_min: *ts_min,
                ts_max: *ts_max,
            });
            files.push(entry.clone());
        }
        files.extend(trades.iter().map(|t| t.1.clone()));

        let mut instruments = std::mem::take(&mut self.instruments);
        instruments.sort_by_key(|i| (i.session_date, i.instrument_id));
        let ipath = self.root.join("instruments.parquet");
        let mut w = TableWriter::create(&ipath, "qt.instruments")?;
        w.write(&codec::instruments_to_batch(&instruments))?;
        files.push(w.finish()?);

        let mut pexp: BTreeMap<u32, i8> = BTreeMap::new();
        for i in &instruments {
            pexp.entry(i.instrument_id).or_insert(i.price_exp);
        }
        if dq.records == 0 {
            dq.records = main_rows;
        }
        apply_verdict(&mut dq, None);
        dq.reasons.extend(
            std::mem::take(&mut self.warnings)
                .into_iter()
                .map(|w| format!("warning: {w}")),
        );
        let dq_entry = write_json(&self.root, "data_quality.json", &dq)?;
        files.push(dq_entry);

        let manifest = DatasetManifest {
            dataset_id: json::dataset_id(&shas, &self.source_format),
            kind: match self.kind {
                DatasetKind::Mbo => DatasetKindName::Mbo,
                DatasetKind::L2 => DatasetKindName::L2,
            },
            source_format: self.source_format.clone(),
            venue: self.venue,
            source_venue: self.source_venue.clone(),
            inputs: std::mem::take(&mut self.inputs),
            partitions,
            price_exp_by_instrument: pexp
                .into_iter()
                .map(|(k, v)| (k.to_string(), v))
                .collect::<IndexMap<_, _>>(),
            created_by: format!("queuetruth {}", qt_schema::ENGINE_VERSION),
            validation_status: self.validation,
            ..Default::default()
        };
        files.push(write_json(&self.root, "manifest.json", &manifest)?);
        if self.spill_dir.exists() {
            std::fs::remove_dir_all(&self.spill_dir).map_err(|e| engine_err(&self.spill_dir, e))?;
        }
        Ok(json::IngestSummary {
            manifest,
            data_quality: dq,
            files,
            ..Default::default()
        })
    }

    #[allow(clippy::type_complexity)]
    fn write_parts<T: FixedRow>(
        &self,
        parts: &Parts<T>,
        dir_name: &str,
    ) -> QtResult<Vec<((i32, u32), FileEntry, u64, Ts, Ts)>> {
        let mut order: Vec<usize> = (0..parts.keys.len()).collect();
        order.sort_by_key(|&i| parts.keys[i]);
        let results: Vec<Mutex<Option<QtResult<FileEntry>>>> = order.iter().map(|_| Mutex::new(None)).collect();
        let next = AtomicUsize::new(0);
        let job = |j: usize| -> QtResult<FileEntry> {
            let i = order[j];
            let (date, iid) = parts.keys[i];
            let b = &parts.bufs[i];
            let rel = format!(
                "{dir_name}/date={}/instrument_id={iid}/part-00000.parquet",
                format_date(date)
            );
            let path = self.root.join(&rel);
            let spill = b.spilled.then(|| spill_path(&self.spill_dir, T::TABLE, date, iid));
            let mut e = write_table::<T>(&path, spill.as_deref(), &b.pending)?;
            e.name = rel;
            Ok(e)
        };
        let threads = self.threads.min(order.len()).max(1);
        std::thread::scope(|s| {
            for _ in 0..threads {
                s.spawn(|| {
                    loop {
                        let j = next.fetch_add(1, Ordering::Relaxed);
                        if j >= order.len() {
                            break;
                        }
                        let r = job(j);
                        if let Ok(mut slot) = results[j].lock() {
                            *slot = Some(r);
                        }
                    }
                });
            }
        });
        let mut out = Vec::with_capacity(order.len());
        for (j, m) in results.into_iter().enumerate() {
            let r = m
                .into_inner()
                .map_err(|_| QtError::Engine("partition writer thread panicked".into()))?
                .ok_or_else(|| QtError::Engine("partition writer produced no result".into()))??;
            let i = order[j];
            let b = &parts.bufs[i];
            out.push((parts.keys[i], r, b.rows, b.ts_min, b.ts_max));
        }
        Ok(out)
    }
}

/// Writes a JSON document (pretty, trailing newline) and returns its file entry.
pub fn write_json<T: serde::Serialize>(root: &Path, name: &str, v: &T) -> QtResult<FileEntry> {
    let path = root.join(name);
    let mut s = serde_json::to_string_pretty(v).map_err(|e| engine_err(&path, e))?;
    s.push('\n');
    std::fs::write(&path, s.as_bytes()).map_err(|e| engine_err(&path, e))?;
    Ok(FileEntry {
        name: name.to_owned(),
        rows: None,
        bytes: s.len() as u64,
        sha256: qt_schema::io::sha256_bytes(s.as_bytes()),
    })
}
