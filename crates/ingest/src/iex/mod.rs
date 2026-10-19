//! IEX HIST DEEP+ reader (`IngestSpec.format = "iex_deep_plus"`, rule set `iex`).
//!
//! Sources: IEX DEEP+ Specification v1.06 (updated 2026-10-07/08; message layouts, Message Protocol ID 0x8005) and
//! IEX Transport Specification v1.26 (2026-06-02; IEX-TP segment framing and sequencing). IEX publishes each
//! trading day of DEEP+ at <https://iextrading.com/trading/market-data/> (HIST, T+1, feed code `DPLS`) as a gzipped
//! pcapng capture of the multicast IEX-TP stream.
//!
//! Layers: [`capture`] (pcap / pcapng) -> [`transport`] (Ethernet / IPv4 / UDP, IEX-TP header and message blocks)
//! -> [`feed`] (sequencing and duplicates per channel and session) -> [`message`] (DEEP+ layouts) -> [`mapper`]
//! (MBO records and trade prints). [`synth`] builds synthetic captures for tests.
//!
//! IEX's terms for redistributing HIST data require this attribution: "Data provided for free by IEX. By accessing
//! or using IEX Historical Data, you agree to the IEX Historical Data Terms of Use."
//! (<https://iextrading.com/iex-historical-data-terms/>).

pub mod capture;
pub mod feed;
pub mod mapper;
pub mod message;
pub mod synth;
pub mod transport;

pub use capture::{CaptureReader, CaptureStats, Packet};
pub use feed::{FeedEvent, FeedMessage, FeedReader, FeedStats};
pub use mapper::{Anomalies, MapOutput, Mapper, MapperStats, MsgCtx, SymbolInfo};
pub use message::{Message, ParseError, Symbol, parse, symbol, symbol_str};

/// IEX's required attribution line for redistributed HIST data (IEX Historical Data Terms of Use, clause 01).
pub const ATTRIBUTION: &str = "Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to \
                               the IEX Historical Data Terms of Use.";

/// Link to the IEX Historical Data Terms of Use.
pub const TERMS_URL: &str = "https://iextrading.com/iex-historical-data-terms/";
