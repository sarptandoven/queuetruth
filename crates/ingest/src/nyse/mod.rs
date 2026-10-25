//! NYSE Group TAQ Integrated Feed reader (`IngestSpec.format = "nyse_ibf"`, rule set `nyse`).
//!
//! Source: TAQ Products Client Specification (Pillar Integrated, BBO, Trades and Imbalance Feeds) v2.4b, NYSE Group,
//! 2026-09-08: the historical record of each XDP Integrated Feed data channel of NYSE, NYSE American, NYSE Arca, NYSE
//! National and NYSE Texas as one gzipped CSV file per channel and day (`EQY_US_<EXCH>_IBF_<n>_<YYYYMMDD>.gz`), every
//! record a real-time message in feed order. The feed is order by order: every displayed order is added, modified,
//! deleted, replaced or executed under its Order ID. Sequencing and refresh semantics come from the Pillar Equities
//! Common Client Specification v2.4p and the XDP Integrated Feed Client Specification v2.3b.
//!
//! Layers: [`reader`] (gzip or raw lines, file names) -> [`message`] (CSV layouts) -> [`mapper`] (MBO records, trade
//! prints, symbol sequences, book refreshes) -> `ingest` (dataset). [`rules`] documents the `nyse` rule set and the
//! allocation of each market: parity on NYSE (queue ranks approximate, labelled by [`rules::PARITY_COUNTER`]),
//! price-time on the other four (NYSE American since its move to Pillar on 2017-07-24; its NYSE MKT days before are
//! parity too). [`synth`] writes synthetic files.
//!
//! Data terms: NYSE posts sample files of this product on its public server, but the only terms found for them are
//! the ICE / NYSE website terms (personal, non-commercial use; no adapting, extracting or derivative works without
//! written permission). queuetruth therefore ships no NYSE data, and its tests use hand-written and synthetic files
//! (`tests/fixtures/nyse/`, `docs/how-to/nyse-order-level.md`).

pub mod mapper;
pub mod message;
pub mod reader;
pub mod rules;
pub mod synth;

mod ingest;
#[cfg(test)]
mod tests;

pub(crate) use ingest::ingest;
pub use mapper::{Anomalies, FileCtx, InstrumentInfo, MapOutput, Mapper, MapperStats};
pub use message::{Message, ParseError, parse_line};
pub use reader::{FileKind, Reader, parse_file_name};
pub use rules::{AMERICAN_PILLAR_DATE, Allocation, Market, PARITY_COUNTER};

/// `IngestSpec.format` name.
pub const FORMAT: &str = "nyse_ibf";
