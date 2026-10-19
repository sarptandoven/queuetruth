//! Length-prefixed message framing of the emi.nasdaq.com daily files (`MMDDYYYY.NASDAQ_ITCH50.gz`): a (gzip)
//! stream of `[u16 big-endian length][message]` records. The length prefix lets unknown message types be skipped;
//! zero-length frames are skipped; a stream that ends inside a frame is `QT-INPUT-002` naming the byte offset.

use std::io::{self, Read};
use std::path::{Path, PathBuf};

use qt_types::{QtError, QtResult};

use crate::guard::{GuardLimits, io_error, open_maybe_gzip};

/// Framed message reader over any byte stream.
pub struct Reader<R: Read> {
    inner: R,
    buf: Vec<u8>,
    path: Option<PathBuf>,
    /// Messages returned so far.
    pub count: u64,
    /// Bytes consumed (prefixes included).
    pub bytes: u64,
}

fn read_full<R: Read>(r: &mut R, buf: &mut [u8]) -> io::Result<usize> {
    let mut got = 0;
    while got < buf.len() {
        match r.read(&mut buf[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(got)
}

impl<R: Read> Reader<R> {
    /// Wraps a raw framed stream.
    pub fn new(inner: R) -> Self {
        Self {
            inner,
            buf: vec![0; 64],
            path: None,
            count: 0,
            bytes: 0,
        }
    }

    fn err(&self, e: &io::Error) -> QtError {
        let loc = match &self.path {
            Some(p) => format!("{} @ byte {}", p.display(), self.bytes),
            None => format!("itch stream @ byte {}", self.bytes),
        };
        match e.kind() {
            io::ErrorKind::UnexpectedEof => QtError::InputFormat {
                location: loc,
                msg: "stream ends inside a frame".into(),
            },
            _ => match &self.path {
                Some(p) => io_error(p, e),
                None => QtError::InputFormat {
                    location: loc,
                    msg: e.to_string(),
                },
            },
        }
    }

    /// Next framed message (without its prefix); `None` at a clean end of stream.
    pub fn next_message(&mut self) -> QtResult<Option<&[u8]>> {
        loop {
            let mut len = [0u8; 2];
            let got = read_full(&mut self.inner, &mut len).map_err(|e| self.err(&e))?;
            if got == 0 {
                return Ok(None);
            }
            if got == 1 {
                return Err(self.err(&io::Error::from(io::ErrorKind::UnexpectedEof)));
            }
            let n = usize::from(u16::from_be_bytes(len));
            if n == 0 {
                self.bytes += 2;
                continue;
            }
            if self.buf.len() < n {
                self.buf.resize(n, 0);
            }
            let got = read_full(&mut self.inner, &mut self.buf[..n]).map_err(|e| self.err(&e))?;
            if got < n {
                return Err(self.err(&io::Error::from(io::ErrorKind::UnexpectedEof)));
            }
            self.bytes += 2 + n as u64;
            self.count += 1;
            return Ok(Some(&self.buf[..n]));
        }
    }
}

impl Reader<Box<dyn io::BufRead + Send>> {
    /// Opens a daily file: gzip is detected from the magic bytes (inflated behind the decompression guard),
    /// anything else is read as a raw framed stream.
    pub fn open(path: &Path) -> QtResult<Self> {
        Self::open_with(path, GuardLimits::default())
    }

    /// `open` with explicit decompression limits.
    pub fn open_with(path: &Path, limits: GuardLimits) -> QtResult<Self> {
        let mut r = Self::new(open_maybe_gzip(path, limits)?);
        r.path = Some(path.to_owned());
        Ok(r)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn frames_skip_zero_lengths_and_detect_truncation() {
        let mut bytes = vec![0, 3, b'Z', 1, 2, 0, 0, 0, 1, b'Y'];
        let mut r = Reader::new(Cursor::new(bytes.clone()));
        assert_eq!(r.next_message().unwrap(), Some(&[b'Z', 1, 2][..]));
        assert_eq!(r.next_message().unwrap(), Some(&b"Y"[..]));
        assert_eq!(r.next_message().unwrap(), None);
        assert_eq!((r.count, r.bytes), (2, 10));
        bytes.extend_from_slice(&[0, 5, b'X']);
        let mut r = Reader::new(Cursor::new(bytes.clone()));
        r.next_message().unwrap();
        r.next_message().unwrap();
        let e = r.next_message().unwrap_err();
        assert_eq!(e.code(), "QT-INPUT-002");
        assert!(e.to_string().contains("byte 10"), "{e}");
        // A lone length byte is a truncated prefix.
        let mut r = Reader::new(Cursor::new(vec![0u8]));
        assert_eq!(r.next_message().unwrap_err().code(), "QT-INPUT-002");
    }

    #[test]
    fn open_reads_gzip_and_raw_files() {
        use flate2::Compression;
        use flate2::write::GzEncoder;
        use std::io::Write;
        let dir = std::env::temp_dir().join(format!("qt-itch-reader-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let framed = [0u8, 2, b'A', 7, 0, 1, b'B'];
        let raw = dir.join("raw.bin");
        std::fs::write(&raw, framed).unwrap();
        let gz = dir.join("01302019.NASDAQ_ITCH50.gz");
        let mut enc = GzEncoder::new(Vec::new(), Compression::fast());
        enc.write_all(&framed).unwrap();
        std::fs::write(&gz, enc.finish().unwrap()).unwrap();
        for p in [raw, gz] {
            let mut r = Reader::open(&p).unwrap();
            assert_eq!(r.next_message().unwrap(), Some(&[b'A', 7][..]));
            assert_eq!(r.next_message().unwrap(), Some(&b"B"[..]));
            assert!(r.next_message().unwrap().is_none());
        }
        assert_eq!(Reader::open(&dir.join("nope")).err().unwrap().code(), "QT-INPUT-001");
        std::fs::remove_dir_all(dir).ok();
    }
}
