//! et CSV layouts (et's CSV format notes, `csvio.rs`): header required, columns in any order, extra columns ignored,
//! CRLF and blank lines tolerated, plain or gzip.
//! - `mbo.csv`: `ts_event, ts_recv, instrument_id, action, side, price, size, order_id, flags, sequence,
//!   channel_id` (`channel_id` optional, default 0; `publisher_id` optional). `size` and `sequence` are u64.
//! - `trades.csv`: `ts_event, instrument_id, price, size, aggressor_side, sequence`.
//! - `instruments.csv`: `instrument_id, raw_symbol` plus optional `min_price_increment` (DBN definition layout,
//!   1e-9 fixed point) and `round_lot_size` (ITCH stock directory layout).
//!
//! Prices are int64 at 1e-9 (`9223372036854775807` = undefined); timestamps u64 ns UTC (checked to i64).
//! Rows are parsed by hand from a 4 MiB buffered reader; a bad row is `QT-INPUT-002` naming file and line.

use std::io::BufRead;
use std::path::{Path, PathBuf};

use qt_schema::rows::{TradeKind, TradeRow};
use qt_types::{Action, MboEvent, QtError, QtResult, Side, VenueRules};

use crate::guard::{GuardLimits, io_error, open_maybe_gzip};

/// `mbo.csv` columns.
pub const MBO_CSV_HEADER: [&str; 11] = [
    "ts_event",
    "ts_recv",
    "instrument_id",
    "action",
    "side",
    "price",
    "size",
    "order_id",
    "flags",
    "sequence",
    "channel_id",
];
/// `trades.csv` columns.
pub const TRADES_CSV_HEADER: [&str; 6] = [
    "ts_event",
    "instrument_id",
    "price",
    "size",
    "aggressor_side",
    "sequence",
];

const MISSING: usize = usize::MAX;

fn parse_u64(f: &[u8]) -> Option<u64> {
    if f.is_empty() {
        return None;
    }
    let mut v: u64 = 0;
    for &b in f {
        if !b.is_ascii_digit() {
            return None;
        }
        v = v.checked_mul(10)?.checked_add(u64::from(b - b'0'))?;
    }
    Some(v)
}

fn parse_i64(f: &[u8]) -> Option<i64> {
    match f.split_first() {
        Some((b'-', rest)) => {
            let v = parse_u64(rest)?;
            if v == 1u64 << 63 {
                Some(i64::MIN)
            } else {
                i64::try_from(v).ok().map(|v| -v)
            }
        }
        _ => parse_u64(f).and_then(|v| i64::try_from(v).ok()),
    }
}

fn trim(f: &[u8]) -> &[u8] {
    let s = f.iter().position(|b| !b.is_ascii_whitespace()).unwrap_or(f.len());
    let e = f.iter().rposition(|b| !b.is_ascii_whitespace()).map_or(s, |i| i + 1);
    &f[s..e.max(s)]
}

/// Line-oriented reader: strips line ends, splits on commas (no quoting: every column is an integer or a letter).
struct Lines {
    rd: Box<dyn BufRead + Send>,
    line: Vec<u8>,
    fields: Vec<(usize, usize)>,
    path: PathBuf,
    lineno: u64,
}

impl Lines {
    fn open(path: &Path) -> QtResult<Self> {
        Ok(Self {
            rd: open_maybe_gzip(path, GuardLimits::default())?,
            line: Vec::with_capacity(256),
            fields: Vec::with_capacity(16),
            path: path.to_path_buf(),
            lineno: 0,
        })
    }

    fn next(&mut self) -> QtResult<bool> {
        loop {
            self.line.clear();
            let n = self
                .rd
                .read_until(b'\n', &mut self.line)
                .map_err(|e| io_error(&self.path, &e))?;
            if n == 0 {
                return Ok(false);
            }
            self.lineno += 1;
            while matches!(self.line.last(), Some(b'\n' | b'\r')) {
                self.line.pop();
            }
            if self.line.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            self.fields.clear();
            let mut start = 0;
            for (i, &b) in self.line.iter().enumerate() {
                if b == b',' {
                    self.fields.push((start, i));
                    start = i + 1;
                }
            }
            self.fields.push((start, self.line.len()));
            return Ok(true);
        }
    }

    #[inline]
    fn field(&self, col: usize) -> &[u8] {
        match self.fields.get(col) {
            Some(&(a, b)) => trim(self.line.get(a..b).unwrap_or(&[])),
            None => &[],
        }
    }

    fn err(&self, what: &str) -> QtError {
        let mut shown = String::from_utf8_lossy(&self.line).into_owned();
        if shown.len() > 200 {
            let mut cut = 200;
            while !shown.is_char_boundary(cut) {
                cut -= 1;
            }
            shown.truncate(cut);
        }
        QtError::InputFormat {
            location: format!("{}:{}", self.path.display(), self.lineno),
            msg: format!("bad {what} in {shown:?}"),
        }
    }

    #[inline]
    fn u64(&self, col: usize, what: &str) -> QtResult<u64> {
        parse_u64(self.field(col)).ok_or_else(|| self.err(what))
    }
    #[inline]
    fn ts(&self, col: usize, what: &str) -> QtResult<i64> {
        i64::try_from(self.u64(col, what)?).map_err(|_| self.err(what))
    }
    #[inline]
    fn u32(&self, col: usize, what: &str) -> QtResult<u32> {
        u32::try_from(self.u64(col, what)?).map_err(|_| self.err(what))
    }
    #[inline]
    fn u16_opt(&self, col: usize, what: &str) -> QtResult<u16> {
        if col == MISSING {
            return Ok(0);
        }
        u16::try_from(self.u64(col, what)?).map_err(|_| self.err(what))
    }
    #[inline]
    fn i64(&self, col: usize, what: &str) -> QtResult<i64> {
        parse_i64(self.field(col)).ok_or_else(|| self.err(what))
    }
    #[inline]
    fn letter(&self, col: usize, what: &str) -> QtResult<u8> {
        match self.field(col) {
            [c] if c.is_ascii_alphabetic() => Ok(*c),
            _ => Err(self.err(what)),
        }
    }

    fn header_names(&mut self) -> QtResult<Vec<String>> {
        if !self.next()? {
            return Err(QtError::InputFormat {
                location: self.path.display().to_string(),
                msg: "empty file (header required)".into(),
            });
        }
        Ok((0..self.fields.len())
            .map(|i| {
                String::from_utf8_lossy(self.field(i))
                    .trim()
                    .trim_start_matches('\u{feff}')
                    .to_string()
            })
            .collect())
    }

    fn header(&mut self, names: &[&str], optional: &[&str]) -> QtResult<Vec<usize>> {
        let cols = self.header_names()?;
        names
            .iter()
            .map(|n| match cols.iter().position(|c| c == n) {
                Some(i) => Ok(i),
                None if optional.contains(n) => Ok(MISSING),
                None => Err(QtError::InputFormat {
                    location: self.path.display().to_string(),
                    msg: format!("header lacks column {n:?} (has {})", cols.join(",")),
                }),
            })
            .collect()
    }
}

/// Which et CSV layout a file has (by its header).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CsvKind {
    /// `mbo.csv`.
    Mbo,
    /// `trades.csv`.
    Trades,
    /// `instruments.csv`.
    Instruments,
}

/// Detects the layout from the header (`QT-INPUT-002` if none matches).
pub fn detect(path: &Path) -> QtResult<CsvKind> {
    let mut l = Lines::open(path)?;
    let cols = l.header_names()?;
    let has = |n: &str| cols.iter().any(|c| c == n);
    if has("action") && has("order_id") {
        Ok(CsvKind::Mbo)
    } else if has("aggressor_side") {
        Ok(CsvKind::Trades)
    } else if has("raw_symbol") && has("instrument_id") {
        Ok(CsvKind::Instruments)
    } else {
        Err(QtError::InputFormat {
            location: path.display().to_string(),
            msg: format!("not an et mbo/trades/instruments CSV (header {})", cols.join(",")),
        })
    }
}

/// Streaming `mbo.csv` reader.
pub struct MboCsvReader {
    lines: Lines,
    cols: Vec<usize>,
}

impl MboCsvReader {
    /// Opens a file and checks its header.
    pub fn open(path: &Path) -> QtResult<Self> {
        let mut lines = Lines::open(path)?;
        let mut names: Vec<&str> = MBO_CSV_HEADER.to_vec();
        names.push("publisher_id");
        let cols = lines.header(&names, &["channel_id", "publisher_id"])?;
        Ok(Self { lines, cols })
    }

    /// Next record, or `None` at end of file. `action` must be a DBN action letter and `side` B/A/N.
    #[allow(clippy::should_implement_trait)]
    pub fn next(&mut self) -> QtResult<Option<MboEvent>> {
        if !self.lines.next()? {
            return Ok(None);
        }
        let (l, c) = (&self.lines, &self.cols);
        let action = l.letter(c[3], "action")?;
        if Action::from_byte(action).is_none() {
            return Err(l.err("action"));
        }
        let side = l.letter(c[4], "side")?;
        if !matches!(side, b'A' | b'B' | b'N') {
            return Err(l.err("side"));
        }
        Ok(Some(MboEvent {
            ts_event: l.ts(c[0], "ts_event")?,
            ts_recv: l.ts(c[1], "ts_recv")?,
            instrument_id: l.u32(c[2], "instrument_id")?,
            action,
            side,
            price: l.i64(c[5], "price")?,
            size: l.u64(c[6], "size")?,
            order_id: l.u64(c[7], "order_id")?,
            flags: u8::try_from(l.u64(c[8], "flags")?).map_err(|_| l.err("flags"))?,
            sequence: l.u64(c[9], "sequence")?,
            channel_id: l.u16_opt(c[10], "channel_id")?,
            publisher_id: l.u16_opt(c[11], "publisher_id")?,
        }))
    }

    /// Current line number (1-based, header included).
    pub fn line(&self) -> u64 {
        self.lines.lineno
    }

    /// `ts_event` of the first row (0 for an empty file); the sort key of multi-file inputs.
    pub fn first_ts_event(path: &Path) -> QtResult<i64> {
        let mut r = Self::open(path)?;
        Ok(r.next()?.map_or(0, |m| m.ts_event))
    }
}

/// Every record of an `mbo.csv` file.
pub fn read_mbo_csv(path: &Path) -> QtResult<Vec<MboEvent>> {
    let mut r = MboCsvReader::open(path)?;
    let mut out = Vec::new();
    while let Some(e) = r.next()? {
        out.push(e);
    }
    Ok(out)
}

/// One `trades.csv` row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TradeCsvRow {
    /// Trade time.
    pub ts_event: i64,
    /// Instrument.
    pub instrument_id: u32,
    /// Price (1e-9 fixed point in et files).
    pub price: i64,
    /// Size.
    pub size: u64,
    /// Aggressor side byte (A/B/N).
    pub aggressor_side: u8,
    /// Sequence.
    pub sequence: u64,
}

impl TradeCsvRow {
    /// As a `qt.trades` row: side N on a venue with hidden orders is a hidden print, everything else displayed.
    pub fn to_trade(&self, rules: &VenueRules) -> TradeRow {
        let side = Side::from_byte(self.aggressor_side);
        TradeRow {
            ts_event: self.ts_event,
            instrument_id: self.instrument_id,
            sequence: self.sequence,
            price: self.price,
            size: self.size,
            aggressor_side: side,
            kind: if side == Side::None && rules.supports_hidden_orders {
                TradeKind::Hidden
            } else {
                TradeKind::Displayed
            },
            maker_order_id: None,
        }
    }
}

/// Calls `f` for every `trades.csv` row; returns the row count.
pub fn read_trades_csv(path: &Path, mut f: impl FnMut(&TradeCsvRow) -> QtResult<()>) -> QtResult<u64> {
    let mut l = Lines::open(path)?;
    let c = l.header(&TRADES_CSV_HEADER, &[])?;
    let mut n = 0;
    while l.next()? {
        let side = l.letter(c[4], "aggressor_side")?;
        if !matches!(side, b'A' | b'B' | b'N') {
            return Err(l.err("aggressor_side"));
        }
        f(&TradeCsvRow {
            ts_event: l.ts(c[0], "ts_event")?,
            instrument_id: l.u32(c[1], "instrument_id")?,
            price: l.i64(c[2], "price")?,
            size: l.u64(c[3], "size")?,
            aggressor_side: side,
            sequence: l.u64(c[5], "sequence")?,
        })?;
        n += 1;
    }
    Ok(n)
}

/// `ts_event` of the first `trades.csv` row (0 for an empty file).
pub fn first_trade_ts(path: &Path) -> QtResult<i64> {
    let mut l = Lines::open(path)?;
    let c = l.header(&TRADES_CSV_HEADER, &[])?;
    if l.next()? { l.ts(c[0], "ts_event") } else { Ok(0) }
}

/// One `instruments.csv` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EtInstrument {
    /// Instrument id.
    pub instrument_id: u32,
    /// Symbol.
    pub raw_symbol: String,
    /// Tick in 1e-9 fixed point (DBN definition layout).
    pub min_price_increment: Option<i64>,
    /// Round lot (ITCH stock directory layout; informational).
    pub round_lot_size: Option<u64>,
}

/// Reads `instruments.csv` (either et layout); duplicate ids are `QT-INPUT-003`.
pub fn read_instruments_csv(path: &Path) -> QtResult<Vec<EtInstrument>> {
    let mut l = Lines::open(path)?;
    let c = l.header(
        &["instrument_id", "raw_symbol", "min_price_increment", "round_lot_size"],
        &["min_price_increment", "round_lot_size"],
    )?;
    let mut out: Vec<EtInstrument> = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    while l.next()? {
        let iid = l.u32(c[0], "instrument_id")?;
        if !seen.insert(iid) {
            return Err(QtError::Duplicate {
                what: "instrument_id".into(),
                id: iid.to_string(),
            });
        }
        let sym = std::str::from_utf8(l.field(c[1]))
            .map_err(|_| l.err("raw_symbol"))?
            .trim()
            .to_owned();
        let opt_i64 = |col: usize, what: &str| -> QtResult<Option<i64>> {
            if col == MISSING || l.field(col).is_empty() {
                Ok(None)
            } else {
                l.i64(col, what).map(Some)
            }
        };
        let mpi = opt_i64(c[2], "min_price_increment")?;
        let rl = if c[3] == MISSING || l.field(c[3]).is_empty() {
            None
        } else {
            Some(l.u64(c[3], "round_lot_size")?)
        };
        out.push(EtInstrument {
            instrument_id: iid,
            raw_symbol: sym,
            min_price_increment: mpi,
            round_lot_size: rl,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use qt_types::{UNDEF_PRICE, Venue, rules};

    #[test]
    fn parses_et_layouts_in_any_order_and_rejects_bad_rows() {
        let dir = std::env::temp_dir().join(format!("qt-etcsv-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("mbo.csv");
        // channel_id absent, columns shuffled, CRLF, blank line, extra column, size above u32::MAX (et csvio test).
        std::fs::write(
            &p,
            "sequence,instrument_id,ts_event,ts_recv,action,side,price,size,order_id,flags,note\r\n\
             5,7,1000,1001,A,B,-5,3,42,128,hello\r\n\
             \r\n\
             6,7,2000,2001,C,A,9223372036854775807,5000000000,42,0,x\n",
        )
        .unwrap();
        assert_eq!(detect(&p).unwrap(), CsvKind::Mbo);
        let mut r = MboCsvReader::open(&p).unwrap();
        let a = r.next().unwrap().unwrap();
        assert_eq!(
            (
                a.ts_event,
                a.ts_recv,
                a.instrument_id,
                a.action,
                a.side,
                a.price,
                a.size,
                a.order_id,
                a.flags
            ),
            (1000, 1001, 7, b'A', b'B', -5, 3, 42, 128)
        );
        assert_eq!((a.sequence, a.channel_id, a.publisher_id), (5, 0, 0));
        assert!(a.is_last());
        let c = r.next().unwrap().unwrap();
        assert_eq!((c.price, c.size, c.sequence), (UNDEF_PRICE, 5_000_000_000, 6));
        assert!(r.next().unwrap().is_none());
        assert_eq!(MboCsvReader::first_ts_event(&p).unwrap(), 1000);

        std::fs::write(&p, "ts_event,instrument_id\n1,2\n").unwrap();
        let e = MboCsvReader::open(&p).err().unwrap().to_string();
        assert!(e.contains("lacks column \"ts_recv\""), "{e}");
        std::fs::write(&p, format!("{}\n1,1,1,A,B,100,x,1,0,1,0\n", MBO_CSV_HEADER.join(","))).unwrap();
        let e = MboCsvReader::open(&p).unwrap().next().unwrap_err();
        assert_eq!(e.code(), "QT-INPUT-002");
        assert!(e.to_string().contains(":2: bad size"), "{e}");
        for bad in [
            "1,1,1,Q,B,100,1,1,0,1,0",
            "1,1,1,A,X,100,1,1,0,1,0",
            "1,1,1,A,B,100,1,1,256,1,0",
            "1,1,1,A,B,100,1,1,0,1,70000",
        ] {
            std::fs::write(&p, format!("{}\n{bad}\n", MBO_CSV_HEADER.join(","))).unwrap();
            assert!(MboCsvReader::open(&p).unwrap().next().is_err(), "{bad}");
        }
        // Timestamps above i64::MAX are rejected (checked cast).
        std::fs::write(
            &p,
            format!(
                "{}\n18446744073709551615,1,1,A,B,100,1,1,0,1,0\n",
                MBO_CSV_HEADER.join(",")
            ),
        )
        .unwrap();
        assert!(MboCsvReader::open(&p).unwrap().next().is_err());

        let t = dir.join("trades.csv");
        std::fs::write(
            &t,
            format!(
                "{}\n10,1,500,2,N,3\n11,1,500,6000000000,B,4\n",
                TRADES_CSV_HEADER.join(",")
            ),
        )
        .unwrap();
        assert_eq!(detect(&t).unwrap(), CsvKind::Trades);
        let mut got = Vec::new();
        read_trades_csv(&t, |r| {
            got.push(r.to_trade(&rules(Venue::Nasdaq)));
            Ok(())
        })
        .unwrap();
        assert_eq!(got.len(), 2);
        assert_eq!(
            (got[0].kind, got[0].aggressor_side, got[0].size),
            (TradeKind::Hidden, Side::None, 2)
        );
        assert_eq!(
            (got[1].kind, got[1].size, got[1].sequence),
            (TradeKind::Displayed, 6_000_000_000, 4)
        );

        let i = dir.join("instruments.csv");
        std::fs::write(
            &i,
            "instrument_id,raw_symbol,market_category,financial_status,round_lot_size,round_lots_only,issue_classification,issue_sub_type,authenticity,short_sale_threshold,ipo_flag,luld_tier,etp_flag,etp_leverage_factor,inverse_indicator\n\
             13,AAPL,Q,N,100,N,C,Z,P,N,N,1,N,0,N\n",
        )
        .unwrap();
        assert_eq!(detect(&i).unwrap(), CsvKind::Instruments);
        let v = read_instruments_csv(&i).unwrap();
        assert_eq!(
            v,
            vec![EtInstrument {
                instrument_id: 13,
                raw_symbol: "AAPL".into(),
                min_price_increment: None,
                round_lot_size: Some(100)
            }]
        );
        std::fs::write(
            &i,
            "instrument_id,raw_symbol,min_price_increment\n1,ESH1,250000000\n1,ESH1,250000000\n",
        )
        .unwrap();
        assert_eq!(read_instruments_csv(&i).unwrap_err().code(), "QT-INPUT-003");
        std::fs::write(&i, "instrument_id,raw_symbol,min_price_increment\n1,ESH1,250000000\n").unwrap();
        assert_eq!(
            read_instruments_csv(&i).unwrap()[0].min_price_increment,
            Some(250_000_000)
        );
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn number_parsers_are_checked() {
        assert_eq!(parse_u64(b"18446744073709551615"), Some(u64::MAX));
        assert_eq!(parse_u64(b"18446744073709551616"), None);
        assert_eq!(parse_i64(b"-9223372036854775808"), Some(i64::MIN));
        assert_eq!(parse_i64(b"9223372036854775808"), None);
        assert_eq!(parse_i64(b"-"), None);
        assert_eq!(parse_u64(b""), None);
        assert_eq!(trim(b"  12 "), b"12");
        assert_eq!(trim(b"   "), b"");
    }
}
