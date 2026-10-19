//! Dataset writer (SPEC 3.3 layout through `qt_schema::io`), expectation builder for hand-made streams and the
//! `MANIFEST.sha256` writer of the fixture directories.
//!
//! Records are routed to the partition (instrument, session date) of the instrument row with the greatest
//! `midnight_utc_ns <= ts_event` (at most 25 h after it), keeping feed order. `trades_source` holds the derived
//! trades of every partition (also when empty). `data_quality.json` holds this crate's own counters
//! (`refbook::DqTracker`), summed over partitions. `dataset_id` follows SPEC 3.10 with `source_format =
//! "synthetic"` over the `mbo` partition files.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use qt_schema::codec::{instruments_to_batch, mbo_to_batch, trades_to_batch};
use qt_schema::io::{TableWriter, sha256_file};
use qt_schema::json::{DatasetKindName, DatasetManifest, PartitionEntry, PartitionFile, dataset_id};
use qt_schema::rows::{BboRow, TradeRow};
use qt_types::{BookSide, Instrument, MboEvent, QtError, QtResult, Ts, ValidationStatus, rules};

use crate::SynthOutput;
use crate::calendar::{NS_PER_S, date_string};
use crate::refbook::{DqTracker, NaiveBook, merge_dq};
use crate::synth::trade_row;

/// `source_format` written by this crate.
pub const SOURCE_FORMAT: &str = "synthetic";

/// Partition key (session_date, instrument_id) -> (instrument row, records, derived trades).
type Parts = BTreeMap<(i32, u32), (usize, Vec<MboEvent>, Vec<TradeRow>)>;

const MAX_DAY_NS: i64 = 25 * 3_600 * NS_PER_S;

/// Index of the instrument row of a record (module doc).
pub(crate) fn route(insts: &[Instrument], inst: u32, ts: Ts) -> Option<usize> {
    let mut best: Option<usize> = None;
    for (i, r) in insts.iter().enumerate() {
        if r.instrument_id == inst
            && r.midnight_utc_ns <= ts
            && ts - r.midnight_utc_ns < MAX_DAY_NS
            && best.is_none_or(|b| insts[b].midnight_utc_ns < r.midnight_utc_ns)
        {
            best = Some(i);
        }
    }
    best
}

fn no_row(e_inst: u32, ts: Ts) -> QtError {
    QtError::InputFormat {
        location: format!("instrument {e_inst} at ts {ts}"),
        msg: "no instrument row covers this record".into(),
    }
}

fn io_err(path: &Path, e: impl std::fmt::Display) -> QtError {
    QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    }
}

/// Expected `bbo = full` rows, derived trades and final depth of a stream, computed with the naive reference book
/// per partition (instrument row of `route`; records of instruments without rows share one book per instrument).
pub fn expectations(events: Vec<MboEvent>, instruments: Vec<Instrument>) -> SynthOutput {
    let mut books: BTreeMap<(u32, Option<usize>), NaiveBook> = BTreeMap::new();
    let mut bbo = Vec::new();
    let mut trades = Vec::new();
    for e in &events {
        let row = route(&instruments, e.instrument_id, e.ts_event);
        let venue = row.map_or(qt_types::Venue::Generic, |r| instruments[r].venue);
        let r = rules(venue);
        let book = books.entry((e.instrument_id, row)).or_insert_with(|| NaiveBook::new(r));
        book.apply(e);
        if e.action == b'T' {
            trades.push(trade_row(e, &r));
        }
        if e.is_last() {
            book.note_event_end();
            let b = book.best(BookSide::Bid);
            let a = book.best(BookSide::Ask);
            bbo.push(BboRow {
                ts_event: e.ts_event,
                instrument_id: e.instrument_id,
                sequence: e.sequence,
                bid_px: b.map(|x| x.0),
                bid_sz: b.map_or(0, |x| x.1),
                bid_ct: b.map_or(0, |x| x.2),
                ask_px: a.map(|x| x.0),
                ask_sz: a.map_or(0, |x| x.1),
                ask_ct: a.map_or(0, |x| x.2),
            });
        }
    }
    let mut last_book: BTreeMap<u32, &NaiveBook> = BTreeMap::new();
    for ((inst, _), b) in &books {
        last_book.insert(*inst, b);
    }
    let mut depth = Vec::new();
    for (inst, b) in last_book {
        for s in [BookSide::Bid, BookSide::Ask] {
            for (p, t, c) in b.levels(s) {
                depth.push((inst, s, p, t, c));
            }
        }
    }
    SynthOutput {
        events,
        instruments,
        expected_bbo: bbo,
        expected_trades: trades,
        expected_depth_final: depth,
    }
}

fn write_json<T: serde::Serialize>(path: &Path, doc: &T) -> QtResult<()> {
    let mut text = serde_json::to_string_pretty(doc).map_err(|e| QtError::Engine(format!("json: {e}")))?;
    text.push('\n');
    std::fs::write(path, text).map_err(|e| io_err(path, e))
}

/// `write_dataset` with an explicit validation label (default `synthetic_only`).
pub fn write_dataset_with(out: &SynthOutput, root: &Path, validation: Option<ValidationStatus>) -> QtResult<PathBuf> {
    let insts = &out.instruments;
    let first = insts
        .first()
        .ok_or_else(|| QtError::Config("write_dataset: no instruments".into()))?;
    let venue = first.venue;
    let rules = rules(venue);
    std::fs::create_dir_all(root).map_err(|e| io_err(root, e))?;
    // Partition key = (session_date, instrument_id) -> row index.
    let mut parts: Parts = BTreeMap::new();
    for e in &out.events {
        let r = route(insts, e.instrument_id, e.ts_event).ok_or_else(|| no_row(e.instrument_id, e.ts_event))?;
        parts
            .entry((insts[r].session_date, e.instrument_id))
            .or_insert_with(|| (r, Vec::new(), Vec::new()))
            .1
            .push(*e);
    }
    for t in &out.expected_trades {
        let r = route(insts, t.instrument_id, t.ts_event).ok_or_else(|| no_row(t.instrument_id, t.ts_event))?;
        if let Some(p) = parts.get_mut(&(insts[r].session_date, t.instrument_id)) {
            p.2.push(*t);
        }
    }
    let mut rows: Vec<Instrument> = insts.clone();
    rows.sort_by_key(|r| (r.session_date, r.instrument_id));
    let mut w = TableWriter::create(&root.join("instruments.parquet"), "qt.instruments")?;
    w.write(&instruments_to_batch(&rows))?;
    w.finish()?;
    let mut entries = Vec::new();
    let mut shas = Vec::new();
    let mut dqs = Vec::new();
    for ((date, inst), (_, recs, trades)) in &parts {
        let rel = format!(
            "mbo/date={}/instrument_id={inst}/part-00000.parquet",
            date_string(*date)
        );
        let mut w = TableWriter::create(&root.join(&rel), "qt.mbo")?;
        for chunk in recs.chunks(65_536) {
            w.write(&mbo_to_batch(chunk))?;
        }
        let fe = w.finish()?;
        let trel = format!(
            "trades_source/date={}/instrument_id={inst}/part-00000.parquet",
            date_string(*date)
        );
        let mut tw = TableWriter::create(&root.join(&trel), "qt.trades")?;
        tw.write(&trades_to_batch(trades))?;
        tw.finish()?;
        let mut dq = DqTracker::new(rules);
        for e in recs {
            dq.push(e);
        }
        dqs.push((format!("{}/{inst}", date_string(*date)), dq.finish().0));
        shas.push(fe.sha256.clone());
        entries.push(PartitionEntry {
            instrument_id: *inst,
            session_date: *date,
            files: vec![PartitionFile {
                path: rel,
                rows: recs.len() as u64,
                sha256: fe.sha256,
            }],
            rows: recs.len() as u64,
            ts_min: recs.iter().map(|e| e.ts_event).min().unwrap_or(0),
            ts_max: recs.iter().map(|e| e.ts_event).max().unwrap_or(0),
        });
    }
    let mut price_exp = IndexMap::new();
    let mut ids: Vec<(u32, i8)> = rows.iter().map(|r| (r.instrument_id, r.price_exp)).collect();
    ids.sort_unstable();
    ids.dedup_by_key(|x| x.0);
    for (id, exp) in ids {
        price_exp.insert(id.to_string(), exp);
    }
    let manifest = DatasetManifest {
        dataset_id: dataset_id(&shas, SOURCE_FORMAT),
        kind: DatasetKindName::Mbo,
        source_format: SOURCE_FORMAT.to_owned(),
        venue,
        source_venue: first.source_venue.clone(),
        inputs: Vec::new(),
        partitions: entries,
        price_exp_by_instrument: price_exp,
        created_by: format!("queuetruth-testkit {}", env!("CARGO_PKG_VERSION")),
        validation_status: validation.unwrap_or(ValidationStatus::SyntheticOnly),
        ..DatasetManifest::default()
    };
    write_json(&root.join("data_quality.json"), &merge_dq(&dqs))?;
    write_json(&root.join("manifest.json"), &manifest)?;
    Ok(root.to_owned())
}

fn collect_files(dir: &Path, base: &Path, out: &mut Vec<String>) -> QtResult<()> {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(dir)
        .map_err(|e| io_err(dir, e))?
        .map(|e| e.map(|e| e.path()).map_err(|e| io_err(dir, e)))
        .collect::<QtResult<_>>()?;
    entries.sort();
    for p in entries {
        if p.is_dir() {
            collect_files(&p, base, out)?;
        } else {
            let rel = p
                .strip_prefix(base)
                .map_err(|e| QtError::Engine(format!("{}: {e}", p.display())))?
                .components()
                .map(|c| c.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/");
            if rel != "MANIFEST.sha256" {
                out.push(rel);
            }
        }
    }
    Ok(())
}

/// Rewrites `<dir>/MANIFEST.sha256` (`shasum -a 256 -c` format, paths relative to `dir`, sorted) and returns the
/// listed `(path, sha256)` pairs.
pub fn write_manifest_sha256(dir: &Path) -> QtResult<Vec<(String, String)>> {
    let mut files = Vec::new();
    collect_files(dir, dir, &mut files)?;
    files.sort();
    let mut text = String::new();
    let mut out = Vec::with_capacity(files.len());
    for f in files {
        let sha = sha256_file(&dir.join(&f))?;
        text.push_str(&format!("{sha}  {f}\n"));
        out.push((f, sha));
    }
    let path = dir.join("MANIFEST.sha256");
    std::fs::write(&path, text).map_err(|e| io_err(&path, e))?;
    Ok(out)
}
