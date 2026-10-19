//! Parity (SPEC 5.1.7). Trades: derived `T` rows against the dataset's `trades_source`, matched in order per
//! partition on `(ts_event, sequence, price, size, aggressor_side)`. BBO: `bbo = full` rows in order per
//! `instrument_id` against a reference: a `qt.bbo` Parquet file (all columns compared) or an et `bbo.csv` (header
//! `ts_event,instrument_id,bid_px,bid_sz,ask_px,ask_sz,sequence`, any column order, empty side =
//! `9223372036854775807` with size 0; counts are absent and not compared). A reference that interleaves instruments
//! is filtered to one instrument while streaming.

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use qt_schema::codec::batch_to_bbo;
use qt_schema::io::read_table_as;
use qt_schema::json::{Object, Parity};
use qt_schema::rows::{BboRow, TradeRow};
use qt_types::{QtError, QtResult, Ts, UNDEF_PRICE};
use serde_json::{Value, json};

/// How many mismatch examples a parity report keeps.
pub const MAX_EXAMPLES: usize = 20;

fn trade_json(t: &TradeRow) -> Value {
    json!({"ts_event": t.ts_event, "sequence": t.sequence, "price": t.price, "size": t.size,
           "aggressor_side": (t.aggressor_side.as_byte() as char).to_string()})
}

fn bbo_json(b: &BboRow) -> Value {
    json!({"ts_event": b.ts_event, "instrument_id": b.instrument_id, "sequence": b.sequence,
           "bid_px": b.bid_px, "bid_sz": b.bid_sz, "bid_ct": b.bid_ct,
           "ask_px": b.ask_px, "ask_sz": b.ask_sz, "ask_ct": b.ask_ct})
}

fn example(kind: &str, instrument_id: u32, index: u64, expected: Value, derived: Value) -> Object {
    let mut o = Object::new();
    o.insert("kind".into(), Value::String(kind.into()));
    o.insert("instrument_id".into(), Value::from(instrument_id));
    o.insert("index".into(), Value::from(index));
    o.insert("expected".into(), expected);
    o.insert("derived".into(), derived);
    o
}

/// In-order trade comparison of one partition.
pub struct TradeParity {
    instrument_id: u32,
    source: Vec<TradeRow>,
    next: usize,
    /// Counters and examples so far.
    pub report: Parity,
}

impl TradeParity {
    /// Compares against the partition's source prints.
    pub fn new(instrument_id: u32, source: Vec<TradeRow>) -> Self {
        Self {
            instrument_id,
            source,
            next: 0,
            report: Parity::default(),
        }
    }

    /// One derived trade.
    pub fn derived(&mut self, t: &TradeRow) {
        let i = self.next;
        self.next += 1;
        let Some(s) = self.source.get(i) else {
            self.report.trades_extra_derived += 1;
            if self.report.first_mismatches.len() < MAX_EXAMPLES {
                let ex = example("trade_extra", self.instrument_id, i as u64, Value::Null, trade_json(t));
                self.report.first_mismatches.push(ex);
            }
            return;
        };
        self.report.trades_compared += 1;
        let key = |x: &TradeRow| (x.ts_event, x.sequence, x.price, x.size, x.aggressor_side);
        if key(s) != key(t) {
            self.report.trades_mismatches += 1;
            if self.report.first_mismatches.len() < MAX_EXAMPLES {
                let ex = example("trade", self.instrument_id, i as u64, trade_json(s), trade_json(t));
                self.report.first_mismatches.push(ex);
            }
        }
    }

    /// Final counters (source rows never reached are `trades_missing_derived`).
    pub fn finish(mut self) -> Parity {
        let missing = self.source.len().saturating_sub(self.next) as u64;
        self.report.trades_missing_derived += missing;
        if missing > 0 && self.report.first_mismatches.len() < MAX_EXAMPLES {
            let i = self.next;
            let ex = example(
                "trade_missing",
                self.instrument_id,
                i as u64,
                trade_json(&self.source[i]),
                Value::Null,
            );
            self.report.first_mismatches.push(ex);
        }
        self.report
    }
}

/// Streaming reader of an et `bbo.csv`, filtered to one instrument and a `ts_event` range.
pub struct EtBboCsv {
    path: PathBuf,
    lines: std::io::Lines<BufReader<File>>,
    cols: [usize; 7],
    line_no: u64,
    instrument_id: Option<u32>,
    range: (Ts, Ts),
}

const ET_BBO_HEADER: [&str; 7] = [
    "ts_event",
    "instrument_id",
    "bid_px",
    "bid_sz",
    "ask_px",
    "ask_sz",
    "sequence",
];

impl EtBboCsv {
    /// Opens `path`; rows of other instruments (when `instrument_id` is given) or outside `range` are skipped.
    pub fn open(path: &Path, instrument_id: Option<u32>, range: (Ts, Ts)) -> QtResult<Self> {
        let f = File::open(path).map_err(|e| QtError::InputMissing {
            path: path.display().to_string(),
            msg: e.to_string(),
        })?;
        let mut lines = BufReader::with_capacity(1 << 20, f).lines();
        let header = match lines.next() {
            Some(Ok(h)) => h,
            Some(Err(e)) => return Err(QtError::Io(e)),
            None => String::new(),
        };
        let names: Vec<&str> = header.trim().split(',').map(str::trim).collect();
        let mut cols = [0usize; 7];
        for (k, want) in ET_BBO_HEADER.iter().enumerate() {
            cols[k] = names
                .iter()
                .position(|n| n == want)
                .ok_or_else(|| QtError::InputFormat {
                    location: format!("{} line 1", path.display()),
                    msg: format!("et bbo.csv header lacks {want:?} (got {header:?})"),
                })?;
        }
        Ok(Self {
            path: path.to_owned(),
            lines,
            cols,
            line_no: 1,
            instrument_id,
            range,
        })
    }

    fn parse(&self, line: &str) -> QtResult<BboRow> {
        let f: Vec<&str> = line.split(',').map(str::trim).collect();
        let bad = |what: &str| QtError::InputFormat {
            location: format!("{} line {}", self.path.display(), self.line_no),
            msg: format!("bad {what}"),
        };
        let get = |k: usize| f.get(self.cols[k]).copied().ok_or_else(|| bad(ET_BBO_HEADER[k]));
        let int = |k: usize| get(k)?.parse::<i64>().map_err(|_| bad(ET_BBO_HEADER[k]));
        let uint = |k: usize| get(k)?.parse::<u64>().map_err(|_| bad(ET_BBO_HEADER[k]));
        let px = |v: i64| (v != UNDEF_PRICE).then_some(v);
        let instrument_id = u32::try_from(uint(1)?).map_err(|_| bad("instrument_id"))?;
        Ok(BboRow {
            ts_event: int(0)?,
            instrument_id,
            sequence: uint(6)?,
            bid_px: px(int(2)?),
            bid_sz: uint(3)?,
            bid_ct: 0,
            ask_px: px(int(4)?),
            ask_sz: uint(5)?,
            ask_ct: 0,
        })
    }
}

impl Iterator for EtBboCsv {
    type Item = QtResult<BboRow>;

    fn next(&mut self) -> Option<QtResult<BboRow>> {
        loop {
            let line = match self.lines.next()? {
                Ok(l) => l,
                Err(e) => return Some(Err(QtError::Io(e))),
            };
            self.line_no += 1;
            if line.trim().is_empty() {
                continue;
            }
            let r = match self.parse(&line) {
                Ok(r) => r,
                Err(e) => return Some(Err(e)),
            };
            if self.instrument_id.is_some_and(|i| i != r.instrument_id)
                || r.ts_event < self.range.0
                || r.ts_event > self.range.1
            {
                continue;
            }
            return Some(Ok(r));
        }
    }
}

/// A boxed stream of BBO rows.
pub type BboStream = Box<dyn Iterator<Item = QtResult<BboRow>> + Send>;

/// BBO reference rows of one instrument and time range: `.csv` = et layout, anything else = `qt.bbo` Parquet.
pub fn open_bbo_reference(path: &Path, instrument_id: u32, range: (Ts, Ts)) -> QtResult<(BboStream, bool)> {
    let is_csv = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("csv"));
    if is_csv {
        return Ok((Box::new(EtBboCsv::open(path, Some(instrument_id), range)?), false));
    }
    let reader = read_table_as(path, "qt.bbo")?;
    let it = reader
        .flat_map(|b| match b.and_then(|b| batch_to_bbo(&b)) {
            Ok(rows) => rows.into_iter().map(Ok).collect::<Vec<_>>(),
            Err(e) => vec![Err(e)],
        })
        .filter(move |r| match r {
            Ok(r) => r.instrument_id == instrument_id && r.ts_event >= range.0 && r.ts_event <= range.1,
            Err(_) => true,
        });
    Ok((Box::new(it), true))
}

/// In-order BBO comparison of one instrument.
pub struct BboParity {
    instrument_id: u32,
    reference: Box<dyn Iterator<Item = QtResult<BboRow>> + Send>,
    with_counts: bool,
    index: u64,
    /// Counters and examples so far.
    pub report: Parity,
}

impl BboParity {
    /// Compares against reference rows (`with_counts = false` for et CSV references).
    pub fn new(
        instrument_id: u32,
        reference: Box<dyn Iterator<Item = QtResult<BboRow>> + Send>,
        with_counts: bool,
    ) -> Self {
        Self {
            instrument_id,
            reference,
            with_counts,
            index: 0,
            report: Parity::default(),
        }
    }

    fn note(&mut self, expected: Value, derived: Value) {
        self.report.bbo_mismatches += 1;
        if self.report.first_mismatches.len() < MAX_EXAMPLES {
            let ex = example("bbo", self.instrument_id, self.index, expected, derived);
            self.report.first_mismatches.push(ex);
        }
    }

    /// One derived `bbo = full` row.
    pub fn derived(&mut self, d: &BboRow) -> QtResult<()> {
        let r = self.reference.next().transpose()?;
        match r {
            None => self.note(Value::Null, bbo_json(d)),
            Some(r) => {
                self.report.bbo_compared += 1;
                let same = if self.with_counts {
                    r == *d
                } else {
                    (
                        r.ts_event,
                        r.instrument_id,
                        r.sequence,
                        r.bid_px,
                        r.bid_sz,
                        r.ask_px,
                        r.ask_sz,
                    ) == (
                        d.ts_event,
                        d.instrument_id,
                        d.sequence,
                        d.bid_px,
                        d.bid_sz,
                        d.ask_px,
                        d.ask_sz,
                    )
                };
                if !same {
                    self.note(bbo_json(&r), bbo_json(d));
                }
            }
        }
        self.index += 1;
        Ok(())
    }

    /// Final counters (reference rows never reached count as mismatches).
    pub fn finish(mut self) -> QtResult<Parity> {
        while let Some(r) = self.reference.next().transpose()? {
            self.note(bbo_json(&r), Value::Null);
            self.index += 1;
        }
        Ok(self.report)
    }
}

/// Adds partition parity reports; `pass` = no trade or BBO mismatch, missing or extra row, and something compared.
pub fn merge(parts: &[Parity]) -> Parity {
    let mut m = Parity::default();
    for p in parts {
        m.trades_compared += p.trades_compared;
        m.trades_mismatches += p.trades_mismatches;
        m.trades_missing_derived += p.trades_missing_derived;
        m.trades_extra_derived += p.trades_extra_derived;
        m.bbo_compared += p.bbo_compared;
        m.bbo_mismatches += p.bbo_mismatches;
        for ex in &p.first_mismatches {
            if m.first_mismatches.len() < MAX_EXAMPLES {
                m.first_mismatches.push(ex.clone());
            }
        }
    }
    m.pass = m.trades_mismatches == 0
        && m.trades_missing_derived == 0
        && m.trades_extra_derived == 0
        && m.bbo_mismatches == 0
        && m.trades_compared + m.bbo_compared > 0;
    m
}
