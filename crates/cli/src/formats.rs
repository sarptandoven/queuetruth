//! The source format registry (plan section 5.2): every `IngestSpec.format`, once, in CLI order. `ops::ingest`
//! dispatches through it, the CLI's `--format` choices come from it and `_core.formats()` returns it.
//! Adding a format: one module implementing it in `qt_ingest` or `qt_crypto`, its `SourceFormat` there, and one line
//! here.

use qt_crypto::formats::{BITFINEX, BITSTAMP, COINBASE, COINBASE_LEVEL3, HYPERLIQUID_NODE, KRAKEN_L3, TARDIS_L2};
use qt_ingest::DatasetKind;
pub use qt_ingest::formats::SourceFormat;
use qt_ingest::formats::{DBN, ETCSV, IEX_DEEP_PLUS, ITCH, NYSE_IBF, PARQUET};
use qt_schema::specs::IngestFormat;
use serde_json::{Value, json};

/// Every source format, in CLI order.
pub const REGISTRY: [SourceFormat; 13] = [
    ITCH,
    DBN,
    ETCSV,
    PARQUET,
    BITSTAMP,
    BITFINEX,
    COINBASE,
    COINBASE_LEVEL3,
    KRAKEN_L3,
    HYPERLIQUID_NODE,
    TARDIS_L2,
    IEX_DEEP_PLUS,
    NYSE_IBF,
];

/// The CLI spelling of every format (`--format` choices), in registry order.
pub const CLI_NAMES: [&str; REGISTRY.len()] = {
    let mut out = [""; REGISTRY.len()];
    let mut i = 0;
    while i < REGISTRY.len() {
        out[i] = REGISTRY[i].cli_name;
        i += 1;
    }
    out
};

/// The registry entry of a spec format.
pub fn get(format: IngestFormat) -> Option<&'static SourceFormat> {
    REGISTRY.iter().find(|f| f.format == format)
}

/// The registry as JSON (`_core.formats()`): one object per format, in registry order.
pub fn to_json() -> Value {
    Value::Array(
        REGISTRY
            .iter()
            .map(|f| {
                json!({
                    "name": f.name,
                    "cli_name": f.cli_name,
                    "kind": match f.kind {
                        DatasetKind::Mbo => "mbo",
                        DatasetKind::L2 => "l2",
                    },
                    "source_format": f.source_format,
                    "vendor": f.vendor,
                    "rule_set": f.rule_set.map(|r| r.name()),
                    "generic_rule_set": f.generic_rule_set.map(|r| r.name()),
                    "default_source_venue": f.default_source_venue,
                    "options": f.options,
                    "engine": f.engine,
                    "summary": f.summary,
                })
            })
            .collect(),
    )
}
