//! Input opening with the decompression guard of SPEC 8.10: an inflating reader fails once the inflated byte count
//! exceeds `max_ratio` times the compressed bytes consumed (checked after `min_checked` inflated bytes, so small
//! highly compressible inputs pass) or the absolute `max_bytes`. gzip (by magic bytes) is inflated here; zstd DBN
//! files are inflated by the `dbn` crate behind the same guard.

use std::fs::File;
use std::io::{self, BufRead, BufReader, Read};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use flate2::read::MultiGzDecoder;
use qt_types::{QtError, QtResult};

/// Read buffer size for large inputs.
pub const READ_BUF: usize = 4 << 20;
const GUARD_TAG: &str = "decompression guard";

/// Decompression limits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GuardLimits {
    /// Maximum inflated / compressed ratio.
    pub max_ratio: u64,
    /// Maximum inflated bytes.
    pub max_bytes: u64,
    /// The ratio is checked only once this many bytes were inflated.
    pub min_checked: u64,
}

impl Default for GuardLimits {
    fn default() -> Self {
        Self {
            max_ratio: 200,
            max_bytes: 64 << 30,
            min_checked: 16 << 20,
        }
    }
}

/// Counts bytes read from the underlying (compressed) source.
pub struct CountingReader<R> {
    inner: R,
    count: Arc<AtomicU64>,
}

impl<R> CountingReader<R> {
    /// Wraps `inner`; `count` accumulates bytes read.
    pub fn new(inner: R, count: Arc<AtomicU64>) -> Self {
        Self { inner, count }
    }
}

impl<R: Read> Read for CountingReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = self.inner.read(buf)?;
        self.count.fetch_add(n as u64, Ordering::Relaxed);
        Ok(n)
    }
}

/// Checks inflated output against the compressed input count.
pub struct GuardedReader<R> {
    inner: R,
    compressed: Arc<AtomicU64>,
    inflated: u64,
    limits: GuardLimits,
}

impl<R> GuardedReader<R> {
    /// Wraps an inflating reader whose compressed source is counted in `compressed`.
    pub fn new(inner: R, compressed: Arc<AtomicU64>, limits: GuardLimits) -> Self {
        Self {
            inner,
            compressed,
            inflated: 0,
            limits,
        }
    }

    /// Bytes inflated so far.
    pub fn inflated(&self) -> u64 {
        self.inflated
    }
}

impl<R: Read> Read for GuardedReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = self.inner.read(buf)?;
        self.inflated = self.inflated.saturating_add(n as u64);
        let compressed = self.compressed.load(Ordering::Relaxed).max(1);
        if self.inflated > self.limits.max_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("{GUARD_TAG}: inflated size exceeds {} bytes", self.limits.max_bytes),
            ));
        }
        if self.inflated >= self.limits.min_checked && self.inflated / compressed > self.limits.max_ratio {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "{GUARD_TAG}: inflated/compressed ratio above {} ({} bytes from {compressed})",
                    self.limits.max_ratio, self.inflated
                ),
            ));
        }
        Ok(n)
    }
}

/// Maps an I/O error on `path` to `QT-INPUT-001` (open/read failure) or `QT-INPUT-002` (truncation, bad data,
/// decompression guard).
pub fn io_error(path: &Path, e: &io::Error) -> QtError {
    match e.kind() {
        io::ErrorKind::UnexpectedEof | io::ErrorKind::InvalidData | io::ErrorKind::InvalidInput => {
            QtError::InputFormat {
                location: path.display().to_string(),
                msg: e.to_string(),
            }
        }
        _ => QtError::InputMissing {
            path: path.display().to_string(),
            msg: e.to_string(),
        },
    }
}

/// Opens a file for reading (`QT-INPUT-001` if missing).
pub fn open_file(path: &Path) -> QtResult<File> {
    File::open(path).map_err(|e| QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    })
}

/// True when the file starts with the gzip magic bytes.
pub fn is_gzip(path: &Path) -> QtResult<bool> {
    let mut f = open_file(path)?;
    let mut magic = [0u8; 2];
    let mut got = 0;
    while got < 2 {
        match f.read(&mut magic[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(io_error(path, &e)),
        }
    }
    Ok(got == 2 && magic == [0x1f, 0x8b])
}

/// Opens a raw or gzip file (detected by magic bytes) as a buffered byte stream behind the decompression guard.
pub fn open_maybe_gzip(path: &Path, limits: GuardLimits) -> QtResult<Box<dyn BufRead + Send>> {
    let gz = is_gzip(path)?;
    let file = open_file(path)?;
    if gz {
        let count = Arc::new(AtomicU64::new(0));
        let src = BufReader::with_capacity(READ_BUF, CountingReader::new(file, count.clone()));
        let inflate = GuardedReader::new(MultiGzDecoder::new(src), count, limits);
        Ok(Box::new(BufReader::with_capacity(READ_BUF, inflate)))
    } else {
        Ok(Box::new(BufReader::with_capacity(READ_BUF, file)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::Compression;
    use flate2::write::GzEncoder;
    use std::io::Write;

    fn gz(bytes: &[u8]) -> Vec<u8> {
        let mut e = GzEncoder::new(Vec::new(), Compression::default());
        e.write_all(bytes).unwrap();
        e.finish().unwrap()
    }

    #[test]
    fn decompression_guard_rejects_bombs_and_passes_normal_data() {
        let dir = std::env::temp_dir().join(format!("qt-ingest-guard-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        // 24 MiB of zeros compress about 1000:1: above the default ratio once 16 MiB were inflated.
        let bomb = dir.join("bomb.gz");
        std::fs::write(&bomb, gz(&vec![0u8; 24 << 20])).unwrap();
        let mut r = open_maybe_gzip(&bomb, GuardLimits::default()).unwrap();
        let e = io::copy(&mut r, &mut io::sink()).unwrap_err();
        assert_eq!(e.kind(), io::ErrorKind::InvalidData);
        let q = io_error(&bomb, &e);
        assert_eq!(q.code(), "QT-INPUT-002");
        assert!(q.to_string().contains("decompression guard"), "{q}");
        // Absolute limit.
        let small = GuardLimits {
            max_ratio: u64::MAX,
            max_bytes: 1000,
            min_checked: 0,
        };
        let mut r = open_maybe_gzip(&bomb, small).unwrap();
        assert!(io::copy(&mut r, &mut io::sink()).is_err());
        // Small compressible input below min_checked passes; raw input is not inflated at all.
        let ok = dir.join("ok.gz");
        std::fs::write(&ok, gz(&vec![7u8; 1 << 20])).unwrap();
        let mut r = open_maybe_gzip(&ok, GuardLimits::default()).unwrap();
        assert_eq!(io::copy(&mut r, &mut io::sink()).unwrap(), 1 << 20);
        let raw = dir.join("raw.bin");
        std::fs::write(&raw, [1u8, 2, 3]).unwrap();
        assert!(!is_gzip(&raw).unwrap());
        let mut r = open_maybe_gzip(&raw, small).unwrap();
        assert_eq!(io::copy(&mut r, &mut io::sink()).unwrap(), 3);
        assert_eq!(
            open_maybe_gzip(&dir.join("missing"), small).err().unwrap().code(),
            "QT-INPUT-001"
        );
        std::fs::remove_dir_all(dir).ok();
    }
}
