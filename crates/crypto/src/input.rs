//! Input resolution for crypto ingest: path `FileRef`s (files or capture directories), `capture_meta.json`, and
//! line streaming from plain or gzip files with the SPEC 8.10 decompression guard.
//!
//! The guard (review M11) sits on the inflating stream itself: the decoder's output is counted after every chunk
//! (at most the 1 MiB read buffer) and reading fails as soon as it passes `MAX_INFLATE_RATIO` times the file's
//! compressed size or `MAX_INFLATED_BYTES`, so a newline-free gzip bomb is refused after about the limit, not after
//! the whole line was inflated. Lines are also capped at `MAX_LINE_BYTES` (plain and gzip files alike).

use std::fs::File;
use std::io::{self, BufRead, BufReader, Read};
use std::path::{Path, PathBuf};

use qt_schema::specs::FileRef;
use qt_types::{QtError, QtResult};

use crate::jsonx;

/// Inflated/compressed ratio limit (SPEC 8.10).
pub const MAX_INFLATE_RATIO: u64 = 200;
/// Absolute inflated size limit per file (SPEC 8.10).
pub const MAX_INFLATED_BYTES: u64 = 64 << 30;
/// Longest accepted line in bytes, line end excluded (`QT-INPUT-002` beyond; capture lines are at most a few MiB).
pub const MAX_LINE_BYTES: u64 = 64 << 20;
/// Read buffer size (also the largest chunk inflated between two guard checks).
const READ_BUF: usize = 1 << 20;

/// Capture metadata written by et's capture scripts (`capture_meta.json`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct CaptureMeta {
    /// Market / symbol / product list in capture order (`markets`, `symbols` or `products`).
    pub symbols: Vec<String>,
    /// Bitfinex conf flags (`flags`).
    pub flags: Option<u64>,
    /// Hyperliquid: block height of the L4 snapshot (`snapshot_block`).
    pub snapshot_block: Option<u64>,
}

/// Resolved inputs: data files in read order, plus the metadata file if any.
#[derive(Clone, Debug, Default)]
pub struct Inputs {
    /// Data files in read order.
    pub files: Vec<PathBuf>,
    /// `capture_meta.json`, when given or found in a capture directory.
    pub meta_path: Option<PathBuf>,
    /// Parsed metadata.
    pub meta: CaptureMeta,
    /// Hyperliquid L4 snapshot files (`compute-l4-snapshots` output; read whole, not by line).
    pub snapshots: Vec<PathBuf>,
}

/// Largest L4 snapshot file accepted (inflated bytes).
pub const MAX_SNAPSHOT_BYTES: u64 = 1 << 30;

/// Hyperliquid node output directories (`~/hl/data/<name>/hourly/<date>/<hour>`, `*_by_block` variants).
const HL_STREAMS: [&str; 3] = ["node_raw_book_diffs", "node_order_statuses", "node_fills"];

/// True for a Hyperliquid L4 snapshot file name (`l4*.json[.gz]`, e.g. `l4_snapshot_812345678.json`).
pub fn is_l4_snapshot(p: &Path) -> bool {
    let n = p
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    n.starts_with("l4") && (n.ends_with(".json") || n.ends_with(".json.gz"))
}

/// Block height in an L4 snapshot file name: the last run of digits of its stem after the `l4` prefix.
pub fn snapshot_height_from_name(p: &Path) -> Option<u64> {
    let stem = stem_of(p);
    let rest = if stem.to_ascii_lowercase().starts_with("l4") {
        &stem[2..]
    } else {
        &stem[..]
    };
    let digits: String = rest
        .chars()
        .rev()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(char::is_ascii_digit)
        .collect();
    digits.chars().rev().collect::<String>().parse().ok()
}

fn is_hl_file(p: &Path) -> bool {
    p.components().any(|c| {
        c.as_os_str()
            .to_str()
            .is_some_and(|s| HL_STREAMS.iter().any(|h| s.starts_with(h)))
    })
}

/// Files under `dir` (recursively, at most `depth` levels), sorted by path with numeric file names (hours) in
/// numeric order.
fn walk(dir: &Path, depth: usize, out: &mut Vec<PathBuf>) -> QtResult<()> {
    let rd = std::fs::read_dir(dir).map_err(|e| QtError::InputMissing {
        path: dir.display().to_string(),
        msg: e.to_string(),
    })?;
    let mut v: Vec<PathBuf> = rd.filter_map(|e| e.ok().map(|e| e.path())).collect();
    v.sort_by_key(|p| {
        let n = p.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
        (n.parse::<u64>().unwrap_or(u64::MAX), n)
    });
    for p in v {
        if p.is_dir() {
            if depth > 0 {
                walk(&p, depth - 1, out)?;
            }
        } else if p.is_file() {
            out.push(p);
        }
    }
    Ok(())
}

/// Reads a whole (plain or gzip) file of at most `MAX_SNAPSHOT_BYTES` inflated bytes as UTF-8.
pub fn read_whole(path: &Path) -> QtResult<String> {
    let loc = path.display().to_string();
    let miss = |e: io::Error| QtError::InputMissing {
        path: loc.clone(),
        msg: e.to_string(),
    };
    let mut f = File::open(path).map_err(miss)?;
    let compressed = f.metadata().map(|m| m.len()).unwrap_or(0);
    let mut magic = [0u8; 2];
    let k = f.read(&mut magic).map_err(miss)?;
    let f = File::open(path).map_err(miss)?;
    let limit = if k == 2 && magic == [0x1f, 0x8b] {
        inflate_limit(compressed).min(MAX_SNAPSHOT_BYTES)
    } else {
        MAX_SNAPSHOT_BYTES
    };
    let mut r: Box<dyn Read> = if k == 2 && magic == [0x1f, 0x8b] {
        Box::new(flate2::read::MultiGzDecoder::new(f))
    } else {
        Box::new(f)
    };
    let mut buf = Vec::new();
    (&mut r)
        .take(limit.saturating_add(1))
        .read_to_end(&mut buf)
        .map_err(|e| QtError::InputFormat {
            location: loc.clone(),
            msg: e.to_string(),
        })?;
    if buf.len() as u64 > limit {
        return Err(QtError::InputFormat {
            location: loc,
            msg: format!("file larger than {limit} bytes (split the snapshot per coin)"),
        });
    }
    String::from_utf8(buf).map_err(|_| QtError::InputFormat {
        location: loc,
        msg: "invalid UTF-8".into(),
    })
}

/// File-name stems in et's read order per format (a capture directory is expanded in this order, then any other
/// data file by name).
fn preferred_order(format: &str) -> &'static [&'static str] {
    match format {
        "bitstamp" => &["live_orders", "live_trades", "detail_order_book", "order_book"],
        "bitfinex" => &["book_R0", "book_P0", "trades", "hb"],
        "coinbase" => &["full", "matches", "level2", "level2_batch"],
        "coinbase_level3" => &["book", "level3", "heartbeat"],
        "kraken_l3" => &["level3"],
        "hyperliquid_node" => &[],
        _ => &["incremental_book_L2", "trades"],
    }
}

fn is_data_file(p: &Path) -> bool {
    let n = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
    [".jsonl", ".jsonl.gz", ".json.gz", ".csv", ".csv.gz"]
        .iter()
        .any(|s| n.ends_with(s))
}

fn stem_of(p: &Path) -> String {
    let n = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
    n.split('.').next().unwrap_or("").to_string()
}

/// Resolves the spec's inputs: files are kept in the given order; a directory expands to its data files (format
/// read order first, then the rest sorted by name) and its `capture_meta.json`.
pub fn resolve(inputs: &[FileRef], format: &str) -> QtResult<Inputs> {
    let mut out = Inputs::default();
    if inputs.is_empty() {
        return Err(QtError::Config("inputs: at least one input is required".into()));
    }
    for r in inputs {
        let p = r
            .as_path()
            .ok_or_else(|| QtError::Config("inputs: only {\"path\": ...} references can be read locally".into()))?;
        let path = PathBuf::from(p);
        let md = std::fs::metadata(&path).map_err(|e| QtError::InputMissing {
            path: p.to_string(),
            msg: e.to_string(),
        })?;
        if md.is_dir() && format == "hyperliquid_node" {
            let mut found = Vec::new();
            walk(&path, 6, &mut found)?;
            let meta = path.join("capture_meta.json");
            if meta.is_file() && out.meta_path.is_none() {
                out.meta_path = Some(meta.clone());
            }
            for f in found {
                if f == meta {
                    continue;
                }
                if is_l4_snapshot(&f) {
                    out.snapshots.push(f);
                } else if is_hl_file(&f) || is_data_file(&f) {
                    out.files.push(f);
                }
            }
        } else if md.is_dir() {
            let mut found: Vec<PathBuf> = std::fs::read_dir(&path)
                .map_err(|e| QtError::InputMissing {
                    path: p.to_string(),
                    msg: e.to_string(),
                })?
                .filter_map(|e| e.ok().map(|e| e.path()))
                .collect();
            found.sort();
            let meta = path.join("capture_meta.json");
            if meta.is_file() && out.meta_path.is_none() {
                out.meta_path = Some(meta);
            }
            let data: Vec<PathBuf> = found.into_iter().filter(|f| f.is_file() && is_data_file(f)).collect();
            let order = preferred_order(format);
            for stem in order {
                out.files.extend(data.iter().filter(|f| stem_of(f) == *stem).cloned());
            }
            out.files
                .extend(data.iter().filter(|f| !order.contains(&stem_of(f).as_str())).cloned());
        } else if path.file_name().and_then(|n| n.to_str()) == Some("capture_meta.json") {
            out.meta_path = Some(path);
        } else if format == "hyperliquid_node" && is_l4_snapshot(&path) {
            out.snapshots.push(path);
        } else {
            out.files.push(path);
        }
    }
    if out.files.is_empty() {
        return Err(QtError::InputMissing {
            path: inputs
                .iter()
                .filter_map(FileRef::as_path)
                .collect::<Vec<_>>()
                .join(", "),
            msg: "no data files (.jsonl, .jsonl.gz, .csv, .csv.gz) found".into(),
        });
    }
    if let Some(m) = &out.meta_path {
        out.meta = read_meta(m)?;
    }
    Ok(out)
}

/// Parses `capture_meta.json` (only `markets` / `symbols` / `products` and `flags` are used).
pub fn read_meta(path: &Path) -> QtResult<CaptureMeta> {
    let loc = path.display().to_string();
    let text = std::fs::read_to_string(path).map_err(|e| QtError::InputMissing {
        path: loc.clone(),
        msg: e.to_string(),
    })?;
    let v = jsonx::parse(&text).map_err(|m| QtError::InputFormat {
        location: loc.clone(),
        msg: m,
    })?;
    let mut meta = CaptureMeta::default();
    for key in ["markets", "symbols", "products"] {
        if let Some(a) = v.get(key).and_then(jsonx::J::as_arr) {
            meta.symbols = a.iter().filter_map(|x| x.as_str().map(str::to_string)).collect();
            break;
        }
    }
    if let Some(b) = v.get("snapshot_block") {
        meta.snapshot_block = Some(b.as_u64().ok_or_else(|| QtError::InputFormat {
            location: loc.clone(),
            msg: "snapshot_block is not a non-negative integer".into(),
        })?);
    }
    if let Some(f) = v.get("flags") {
        meta.flags = Some(f.as_u64().ok_or_else(|| QtError::InputFormat {
            location: loc,
            msg: "flags is not an integer".into(),
        })?);
    }
    Ok(meta)
}

/// Inflating reader that fails once more than `limit` bytes were inflated; checked after every chunk the decoder
/// returns, so at most one chunk (bounded by the caller's buffer) is produced past the limit.
struct InflateGuard<R> {
    inner: R,
    inflated: u64,
    limit: u64,
    compressed: u64,
}

impl<R: Read> Read for InflateGuard<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = self.inner.read(buf)?;
        self.inflated = self.inflated.saturating_add(n as u64);
        if self.inflated > self.limit {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "decompression guard: {} bytes inflated from {} compressed exceed {} (limit ratio {}, {} bytes)",
                    self.inflated, self.compressed, self.limit, MAX_INFLATE_RATIO, MAX_INFLATED_BYTES
                ),
            ));
        }
        Ok(n)
    }
}

/// Inflated-byte limit of a gzip file of `compressed` bytes (SPEC 8.10).
pub fn inflate_limit(compressed: u64) -> u64 {
    compressed
        .max(1)
        .saturating_mul(MAX_INFLATE_RATIO)
        .min(MAX_INFLATED_BYTES)
}

/// Line iterator over one file (gzip detected by magic bytes), with `\r` stripped, the decompression guard and the
/// line-length cap.
pub struct FileLines {
    path: String,
    reader: Box<dyn BufRead>,
    max_line: u64,
    line_no: u64,
    done: bool,
}

impl FileLines {
    /// Opens `path`.
    pub fn open(path: &Path) -> QtResult<Self> {
        let loc = path.display().to_string();
        let mut f = File::open(path).map_err(|e| QtError::InputMissing {
            path: loc.clone(),
            msg: e.to_string(),
        })?;
        let compressed = f.metadata().map(|m| m.len()).unwrap_or(0);
        let mut magic = [0u8; 2];
        let k = f.read(&mut magic).map_err(|e| QtError::InputMissing {
            path: loc.clone(),
            msg: e.to_string(),
        })?;
        let f = File::open(path).map_err(|e| QtError::InputMissing {
            path: loc.clone(),
            msg: e.to_string(),
        })?;
        let gz = k == 2 && magic == [0x1f, 0x8b];
        let reader: Box<dyn BufRead> = if gz {
            let guard = InflateGuard {
                inner: flate2::read::MultiGzDecoder::new(f),
                inflated: 0,
                limit: inflate_limit(compressed),
                compressed,
            };
            Box::new(BufReader::with_capacity(READ_BUF, guard))
        } else {
            Box::new(BufReader::with_capacity(READ_BUF, f))
        };
        Ok(Self {
            path: loc,
            reader,
            max_line: MAX_LINE_BYTES,
            line_no: 0,
            done: false,
        })
    }

    fn fail(&mut self, msg: String) -> Option<QtResult<String>> {
        self.done = true;
        Some(Err(QtError::InputFormat {
            location: format!("{}:{}", self.path, self.line_no),
            msg,
        }))
    }
}

impl Iterator for FileLines {
    type Item = QtResult<String>;

    fn next(&mut self) -> Option<Self::Item> {
        if self.done {
            return None;
        }
        let mut buf = Vec::new();
        // bounded: never buffers more than max_line + 1 bytes (the guard bounds what is inflated to get them)
        let max = self.max_line;
        match (&mut self.reader)
            .take(max.saturating_add(1))
            .read_until(b'\n', &mut buf)
        {
            Ok(0) => {
                self.done = true;
                None
            }
            Ok(_) => {
                self.line_no += 1;
                if buf.len() as u64 > max && buf.last() != Some(&b'\n') {
                    return self.fail(format!("line longer than {max} bytes"));
                }
                while matches!(buf.last(), Some(b'\n' | b'\r')) {
                    buf.pop();
                }
                match String::from_utf8(buf) {
                    Ok(s) => Some(Ok(s)),
                    Err(_) => self.fail("invalid UTF-8".into()),
                }
            }
            Err(e) => {
                self.line_no += 1;
                self.fail(e.to_string())
            }
        }
    }
}

/// All lines of `files`, in order, as one stream.
pub fn lines(files: &[PathBuf]) -> QtResult<impl Iterator<Item = QtResult<String>>> {
    let mut its = Vec::with_capacity(files.len());
    for f in files {
        its.push(FileLines::open(f)?);
    }
    Ok(its.into_iter().flatten())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn tmpdir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("qt_crypto_input_{name}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn directory_expansion_order_and_meta() {
        let d = tmpdir("dir");
        for n in [
            "trades.jsonl",
            "book_P0.jsonl",
            "hb.jsonl",
            "book_R0.jsonl",
            "zz.jsonl",
            "notes.txt",
        ] {
            std::fs::write(d.join(n), "{}\n").unwrap();
        }
        std::fs::write(
            d.join("capture_meta.json"),
            r#"{"symbols":["tBTCUSD","tETHUSD"],"flags":98304}"#,
        )
        .unwrap();
        let inp = resolve(&[FileRef::path(d.display().to_string())], "bitfinex").unwrap();
        let names: Vec<String> = inp
            .files
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            names,
            ["book_R0.jsonl", "book_P0.jsonl", "trades.jsonl", "hb.jsonl", "zz.jsonl"]
        );
        assert_eq!(inp.meta.symbols, ["tBTCUSD", "tETHUSD"]);
        assert_eq!(inp.meta.flags, Some(98_304));
        assert_eq!(
            resolve(&[FileRef::path(d.join("missing").display().to_string())], "bitfinex")
                .unwrap_err()
                .code(),
            "QT-INPUT-001"
        );
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn gzip_lines_and_guard() {
        let d = tmpdir("gz");
        let p = d.join("a.jsonl.gz");
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        enc.write_all(b"one\r\ntwo\n\nthree").unwrap();
        std::fs::write(&p, enc.finish().unwrap()).unwrap();
        let got: Vec<String> = FileLines::open(&p).unwrap().map(Result::unwrap).collect();
        assert_eq!(got, ["one", "two", "", "three"]);
        // a highly compressible file trips the 200x ratio guard
        let bomb = d.join("bomb.csv.gz");
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::best());
        let line = vec![b'a'; 4095];
        for _ in 0..2048 {
            enc.write_all(&line).unwrap();
            enc.write_all(b"\n").unwrap();
        }
        std::fs::write(&bomb, enc.finish().unwrap()).unwrap();
        let err = FileLines::open(&bomb).unwrap().find_map(Result::err).unwrap();
        assert_eq!(err.code(), "QT-INPUT-002");
        assert!(err.to_string().contains("decompression guard"));
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn line_cap_plain_and_gzip() {
        let d = tmpdir("cap");
        let plain = d.join("a.jsonl");
        std::fs::write(&plain, b"12345678\r\n123456789\nok\n").unwrap();
        let gz = d.join("a.jsonl.gz");
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        enc.write_all(b"12345678\r\n123456789\nok\n").unwrap();
        std::fs::write(&gz, enc.finish().unwrap()).unwrap();
        for p in [&plain, &gz] {
            let mut it = FileLines::open(p).unwrap();
            it.max_line = 9; // "12345678\r" fits exactly; the 9-digit line + "\n" is still fine
            assert_eq!(it.next().unwrap().unwrap(), "12345678");
            assert_eq!(it.next().unwrap().unwrap(), "123456789");
            it.max_line = 1;
            let e = it.next().unwrap().unwrap_err();
            assert_eq!(
                (e.code(), e.to_string().contains("line longer than 1 bytes")),
                ("QT-INPUT-002", true)
            );
            assert!(it.next().is_none());
        }
        assert_eq!(inflate_limit(0), 200);
        assert_eq!(inflate_limit(u64::MAX), MAX_INFLATED_BYTES);
        std::fs::remove_dir_all(&d).unwrap();
    }
}
