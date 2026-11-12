//! Source format registry (plan section 5.2): one `SourceFormat` per `IngestSpec.format`.
//!
//! This crate defines the record and its own six formats; `qt_crypto` defines the crypto and Tardis ones, and
//! `qt_cli::formats::REGISTRY` lists all of them once, in CLI order. The CLI choices, `ops::ingest` and
//! `_core.formats()` read that registry; Python's hand-kept lists are checked against it by tests.

use qt_schema::json::IngestSummary;
use qt_schema::specs::{IngestFormat, IngestSpec};
use qt_types::{QtResult, RuleSet};

use crate::DatasetKind;

/// One source format.
#[derive(Clone, Copy, Debug)]
pub struct SourceFormat {
    /// `IngestSpec.format`.
    pub format: IngestFormat,
    /// Spec spelling (`coinbase_level3`).
    pub name: &'static str,
    /// CLI spelling (`coinbase-level3`): `name` with `-` for `_`.
    pub cli_name: &'static str,
    /// Dataset kind written: order-level or L2 only.
    pub kind: DatasetKind,
    /// `source_format` written to the manifest (`iex` for `iex_deep_plus`).
    pub source_format: &'static str,
    /// Read from a vendor's or venue's own files (not our Parquet): a `synthetic_only` validation status then
    /// describes the venue rules, not the data (`report.load.VENDOR_FORMATS`).
    pub vendor: bool,
    /// The only rule set the format accepts as `IngestSpec.venue`, if it is fixed (`iex` for `iex_deep_plus`).
    pub rule_set: Option<RuleSet>,
    /// What `venue: generic` (and its aliases) means for this format, when it is not `generic` itself.
    pub generic_rule_set: Option<RuleSet>,
    /// Source venue written when the spec gives none; `None`: the rule set's name, the market or the input.
    pub default_source_venue: Option<&'static str>,
    /// `IngestSpec.options` keys the format reads.
    pub options: &'static [&'static str],
    /// Engine crate that reads it.
    pub engine: &'static str,
    /// One line for help and `_core.formats()`.
    pub summary: &'static str,
    /// Runs the ingest of a spec whose `format` is this one.
    pub ingest: fn(&IngestSpec) -> QtResult<IngestSummary>,
}

const fn mbo(
    ingest: fn(&IngestSpec) -> QtResult<IngestSummary>,
    format: IngestFormat,
    name: &'static str,
    cli_name: &'static str,
    source_format: &'static str,
    summary: &'static str,
) -> SourceFormat {
    SourceFormat {
        format,
        name,
        cli_name,
        kind: DatasetKind::Mbo,
        source_format,
        vendor: true,
        rule_set: None,
        generic_rule_set: None,
        default_source_venue: None,
        options: &crate::ingest_impl::ALLOWED_OPTIONS,
        engine: "qt_ingest",
        summary,
        ingest,
    }
}

/// Nasdaq TotalView-ITCH 5.0.
pub const ITCH: SourceFormat = mbo(
    crate::ingest_impl::itch,
    IngestFormat::Itch,
    "itch",
    "itch",
    "itch",
    "Nasdaq TotalView-ITCH 5.0 (raw or gzip), order by order",
);
/// Databento DBN MBO.
pub const DBN: SourceFormat = mbo(
    crate::ingest_impl::dbn,
    IngestFormat::Dbn,
    "dbn",
    "dbn",
    "dbn",
    "Databento DBN MBO files (plain or zstd), order by order",
);
/// et `mbo.csv` layout.
pub const ETCSV: SourceFormat = mbo(
    crate::ingest_impl::etcsv,
    IngestFormat::Etcsv,
    "etcsv",
    "etcsv",
    "etcsv",
    "et CSV layout (mbo.csv, trades.csv, instruments.csv), order by order",
);
/// Our normalized Parquet (a dataset re-ingested).
pub const PARQUET: SourceFormat = SourceFormat {
    vendor: false,
    ..mbo(
        crate::ingest_impl::parquet,
        IngestFormat::Parquet,
        "parquet",
        "parquet",
        "parquet",
        "a queuetruth dataset (normalized Parquet), re-ingested",
    )
};
/// IEX HIST DEEP+.
pub const IEX_DEEP_PLUS: SourceFormat = SourceFormat {
    rule_set: Some(RuleSet::Iex),
    ..mbo(
        crate::ingest_impl::iex_deep_plus,
        IngestFormat::IexDeepPlus,
        crate::ingest_impl::IEX_FORMAT,
        "iex-deep-plus",
        "iex",
        "IEX HIST DEEP+ (IEX-TP over pcap or pcap.gz), order by order",
    )
};
/// NYSE Group TAQ Integrated Feed.
pub const NYSE_IBF: SourceFormat = SourceFormat {
    options: &crate::nyse::OPTIONS,
    ..mbo(
        crate::ingest_impl::nyse_ibf,
        IngestFormat::NyseIbf,
        crate::nyse::FORMAT,
        "nyse-ibf",
        crate::nyse::FORMAT,
        "NYSE Group TAQ Integrated Feed CSV files (rule set nyse), order by order",
    )
};

/// The formats this crate reads, in registry order.
pub const FORMATS: [SourceFormat; 6] = [ITCH, DBN, ETCSV, PARQUET, IEX_DEEP_PLUS, NYSE_IBF];

/// The format of this crate with this spec value.
pub fn get(format: IngestFormat) -> Option<&'static SourceFormat> {
    FORMATS.iter().find(|f| f.format == format)
}
