//! Line framing of TAQ Integrated Feed files (gzip by magic bytes behind the decompression guard, or raw text) and
//! their file names (TAQ Products Client Specification v2.4b, Appendix A: `EQY_US_<EXCH>_IBF_<n>_<YYYYMMDD>.gz`, one
//! file per data channel, plus `EQY_US_<EXCH>_STOCKSUM_<YYYYMMDD>.gz`).

use std::io::{BufRead, Read};
use std::path::{Path, PathBuf};

use qt_types::{QtError, QtResult};

use super::rules::Market;
use crate::guard::{GuardLimits, io_error, open_maybe_gzip};
use crate::time::Date;

/// Longest line accepted, terminator included (the longest Integrated Feed message, Imbalance, is under 300 bytes).
pub const MAX_LINE: u64 = 65_536;

/// Line reader over one file.
pub struct Reader {
    inner: Box<dyn BufRead + Send>,
    path: Option<PathBuf>,
    buf: Vec<u8>,
    /// Lines read so far (blank lines included; 1-based number of the last line returned).
    pub line_no: u64,
    /// Bytes read (inflated).
    pub bytes: u64,
    /// Blank lines skipped.
    pub blank_lines: u64,
}

impl Reader {
    /// Opens a raw or gzip file (`QT-INPUT-001` if missing).
    pub fn open(path: &Path, limits: GuardLimits) -> QtResult<Self> {
        let mut r = Self::new(open_maybe_gzip(path, limits)?);
        r.path = Some(path.to_owned());
        Ok(r)
    }

    /// Wraps any buffered byte stream.
    pub fn new(inner: Box<dyn BufRead + Send>) -> Self {
        Self {
            inner,
            path: None,
            buf: Vec::with_capacity(256),
            line_no: 0,
            bytes: 0,
            blank_lines: 0,
        }
    }

    /// `<path> line <n>` of the last line returned.
    pub fn location(&self) -> String {
        match &self.path {
            Some(p) => format!("{} line {}", p.display(), self.line_no),
            None => format!("line {}", self.line_no),
        }
    }

    /// The next non-blank line, with its 1-based line number, without its `\n` / `\r\n` terminator; `None` at the end
    /// of the file. A truncated gzip stream, the decompression guard and a line longer than `MAX_LINE` bytes are
    /// `QT-INPUT-002`.
    pub fn next_line(&mut self) -> QtResult<Option<(u64, &[u8])>> {
        loop {
            self.buf.clear();
            let n = (&mut self.inner)
                .take(MAX_LINE)
                .read_until(b'\n', &mut self.buf)
                .map_err(|e| match &self.path {
                    Some(p) => io_error(p, &e),
                    None => QtError::InputFormat {
                        location: format!("line {}", self.line_no + 1),
                        msg: e.to_string(),
                    },
                })?;
            if n == 0 {
                return Ok(None);
            }
            self.line_no += 1;
            if n as u64 == MAX_LINE && self.buf.last() != Some(&b'\n') {
                return Err(QtError::InputFormat {
                    location: self.location(),
                    msg: format!("line longer than {MAX_LINE} bytes: not a TAQ Integrated Feed file"),
                });
            }
            self.bytes += n as u64;
            let mut end = self.buf.len();
            if end > 0 && self.buf[end - 1] == b'\n' {
                end -= 1;
            }
            if end > 0 && self.buf[end - 1] == b'\r' {
                end -= 1;
            }
            if end == 0 {
                self.blank_lines += 1;
                continue;
            }
            return Ok(Some((self.line_no, &self.buf[..end])));
        }
    }
}

/// What a file name says about its content.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FileKind {
    /// `EQY_US_<EXCH>_IBF_<channel>_<YYYYMMDD>`: one Integrated Feed data channel of one day.
    Integrated {
        /// Market of the exchange token.
        market: Market,
        /// Channel number; the NYSE Tape A interim files `1A` to `4A` (2017-11-20 to 2019) are 101 to 104.
        channel: u16,
        /// Trading date.
        date: Date,
    },
    /// `EQY_US_<EXCH>_STOCKSUM_<YYYYMMDD>`: the stock summary file (type 223 only, no orders).
    StockSummary {
        /// Market.
        market: Market,
        /// Trading date.
        date: Date,
    },
}

fn yyyymmdd(s: &str) -> Option<Date> {
    if s.len() != 8 || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    Date::new(s[0..4].parse().ok()?, s[4..6].parse().ok()?, s[6..8].parse().ok()?)
}

fn channel(s: &str) -> Option<u16> {
    let (digits, interim) = match s.strip_suffix(['A', 'a']) {
        Some(d) => (d, true),
        None => (s, false),
    };
    if digits.is_empty() || digits.len() > 2 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let n: u16 = digits.parse().ok()?;
    if n == 0 {
        return None;
    }
    Some(if interim { 100 + n } else { n })
}

/// Parses a TAQ Integrated Feed or stock summary file name (any directory; extensions such as `.gz`, `.csv` or
/// `.csv.gz` are ignored; case-insensitive). `None` for any other name.
pub fn parse_file_name(path: &Path) -> Option<FileKind> {
    let name = path.file_name()?.to_str()?;
    let base = name.split('.').next()?.to_ascii_uppercase();
    let parts: Vec<&str> = base.split('_').collect();
    match parts.as_slice() {
        ["EQY", "US", exch, "IBF", ch, date] => Some(FileKind::Integrated {
            market: Market::from_file_token(exch)?,
            channel: channel(ch)?,
            date: yyyymmdd(date)?,
        }),
        ["EQY", "US", exch, "STOCKSUM", date] => Some(FileKind::StockSummary {
            market: Market::from_file_token(exch)?,
            date: yyyymmdd(date)?,
        }),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Write};

    fn lines(r: &mut Reader) -> Vec<String> {
        let mut v = Vec::new();
        while let Some((n, l)) = r.next_line().unwrap() {
            let s = String::from_utf8_lossy(l).into_owned();
            assert_eq!(n, r.line_no);
            v.push(s);
        }
        v
    }

    #[test]
    fn lines_strip_terminators_and_skip_blank_lines() {
        let mut r = Reader::new(Box::new(Cursor::new(b"3,1,A\r\n\n100,2\n\r\nlast".to_vec())));
        assert_eq!(lines(&mut r), ["3,1,A", "100,2", "last"]);
        assert_eq!((r.line_no, r.blank_lines, r.bytes), (5, 2, 20));
        assert_eq!(r.location(), "line 5");
        let mut empty = Reader::new(Box::new(Cursor::new(Vec::new())));
        assert!(empty.next_line().unwrap().is_none());
        // A line of exactly MAX_LINE bytes (terminator included) passes; one byte more is an input error.
        let mut ok = vec![b'x'; MAX_LINE as usize - 1];
        ok.push(b'\n');
        let mut r = Reader::new(Box::new(Cursor::new(ok)));
        assert_eq!(
            r.next_line().unwrap().map(|(n, l)| (n, l.len())),
            Some((1, MAX_LINE as usize - 1))
        );
        let mut long = vec![b'x'; MAX_LINE as usize];
        long.push(b'\n');
        let mut r = Reader::new(Box::new(Cursor::new(long)));
        let e = r.next_line().unwrap_err();
        assert_eq!(e.code(), "QT-INPUT-002");
        assert!(e.to_string().contains("line 1: line longer than 65536 bytes"), "{e}");
    }

    #[test]
    fn gzip_and_raw_files_read_the_same_and_truncation_is_an_input_error() {
        let dir = std::env::temp_dir().join(format!("qt-nyse-reader-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let text = b"3,2,QTX,1,1,N,C,100,,,0,Y,1,100\n34,3,04:00:00.000000000,QTX,1,B,~,,,,,,~,P\n";
        let raw = dir.join("raw.csv");
        std::fs::write(&raw, text).unwrap();
        let gz = dir.join("file.gz");
        let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        e.write_all(text).unwrap();
        let bytes = e.finish().unwrap();
        std::fs::write(&gz, &bytes).unwrap();
        let a = lines(&mut Reader::open(&raw, GuardLimits::default()).unwrap());
        let b = lines(&mut Reader::open(&gz, GuardLimits::default()).unwrap());
        assert_eq!(a, b);
        assert_eq!(a.len(), 2);
        let mut r = Reader::open(&raw, GuardLimits::default()).unwrap();
        r.next_line().unwrap();
        assert!(r.location().ends_with("raw.csv line 1"), "{}", r.location());
        let cut = dir.join("cut.gz");
        std::fs::write(&cut, &bytes[..bytes.len() - 12]).unwrap();
        let mut r = Reader::open(&cut, GuardLimits::default()).unwrap();
        let err = loop {
            match r.next_line() {
                Ok(Some(_)) => {}
                Ok(None) => panic!("truncated gzip read to the end"),
                Err(e) => break e,
            }
        };
        assert_eq!(err.code(), "QT-INPUT-002", "{err}");
        assert_eq!(
            Reader::open(&dir.join("missing.gz"), GuardLimits::default())
                .err()
                .map(|e| e.code()),
            Some("QT-INPUT-001")
        );
        std::fs::remove_dir_all(dir).ok();
    }

    #[test]
    fn file_names() {
        let d = |y, m, dd| Date::new(y, m, dd).unwrap();
        let p = |s: &str| parse_file_name(Path::new(s));
        assert_eq!(
            p("/x/EQY_US_NYSE_IBF_14_20260702.gz"),
            Some(FileKind::Integrated {
                market: Market::Nyse,
                channel: 14,
                date: d(2026, 7, 2)
            })
        );
        assert_eq!(
            p("eqy_us_arca_ibf_1_20200803.csv.gz"),
            Some(FileKind::Integrated {
                market: Market::Arca,
                channel: 1,
                date: d(2020, 8, 3)
            })
        );
        assert_eq!(
            p("EQY_US_MKT_IBF_6_20170103"),
            Some(FileKind::Integrated {
                market: Market::American,
                channel: 6,
                date: d(2017, 1, 3)
            })
        );
        assert_eq!(
            p("EQY_US_NYSE_IBF_2A_20180105.GZ"),
            Some(FileKind::Integrated {
                market: Market::Nyse,
                channel: 102,
                date: d(2018, 1, 5)
            })
        );
        assert_eq!(
            p("EQY_US_TEXAS_STOCKSUM_20260701.gz"),
            Some(FileKind::StockSummary {
                market: Market::Texas,
                date: d(2026, 7, 1)
            })
        );
        for bad in [
            "EQY_US_NYSE_IBF_0_20260702.gz",
            "EQY_US_NYSE_IBF_123_20260702.gz",
            "EQY_US_NYSE_IBF_1_20261302.gz",
            "EQY_US_NYSE_IBF_1_2026070.gz",
            "EQY_US_NASDAQ_IBF_1_20260702.gz",
            "EQY_US_NYSE_BBO_1_20260702.gz",
            "EQY_US_TAQ_NYSE_BBO_1_20260702.gz",
            "EQY_US_ALL_ARCA_BOOK_20110308.csv.gz",
            "sample.csv",
            "",
        ] {
            assert_eq!(p(bad), None, "{bad}");
        }
    }
}
