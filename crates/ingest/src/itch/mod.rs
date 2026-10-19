//! Nasdaq TotalView-ITCH 5.0: `Reader` (gz/raw framing), `parse`, `encode`, `encode_file(messages, path, gzip)` for
//! synthetic fixtures, and `Mapper` (et convert semantics -> `MboEvent` + `TradeRow`).

mod mapper;
mod message;
mod reader;

use std::io::Write;
use std::path::Path;

use qt_types::{QtError, QtResult};

pub use mapper::{Anomalies, IdBuild, IdHasher, MapOutput, Mapper, MapperStats};
pub use message::{
    Body, DirectListing, Header, KNOWN_TYPES, Message, Noii, ParseError, Stock, StockDirectory, encode, message_len,
    parse, stock, stock_str,
};
pub use reader::Reader;

/// Writes `messages` in the emi.nasdaq.com framing (`[u16 BE length][message]`), gzip-compressed when `gzip`.
pub fn encode_file(messages: &[Message], path: &Path, gzip: bool) -> QtResult<()> {
    let mut buf = Vec::with_capacity(messages.len() * 40);
    for m in messages {
        encode(m, &mut buf);
    }
    let io = |e: std::io::Error| QtError::Engine(format!("{}: {e}", path.display()));
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent).map_err(io)?;
    }
    if gzip {
        let f = std::fs::File::create(path).map_err(io)?;
        let mut e = flate2::write::GzEncoder::new(std::io::BufWriter::new(f), flate2::Compression::default());
        e.write_all(&buf).map_err(io)?;
        e.finish().map_err(io)?.flush().map_err(io)?;
    } else {
        std::fs::write(path, &buf).map_err(io)?;
    }
    Ok(())
}
