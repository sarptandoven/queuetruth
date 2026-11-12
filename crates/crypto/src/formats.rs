//! The crypto and Tardis entries of the source format registry (`qt_ingest::formats::SourceFormat`), with what
//! `prepare` needs per format: the converter, the order-level counters and the exactness notes.

use indexmap::IndexMap;
use qt_ingest::DatasetKind;
use qt_ingest::formats::SourceFormat;
use qt_schema::specs::{IngestFormat, IngestSpec};
use qt_types::{QtResult, RuleSet};

use crate::{
    Counters, L3Output, bitfinex, bitstamp, cfg_err, checksum_precision, coinbase, coinbase_l3, hyperliquid, input,
    kraken_l3, opt_i64,
};

/// Lines of the resolved data files, in read order.
pub(crate) type Lines<'a> = Box<dyn Iterator<Item = QtResult<String>> + 'a>;

/// What an L3 converter reads besides the lines.
pub(crate) struct Args<'a> {
    pub(crate) instruments: IndexMap<String, u32>,
    pub(crate) price_exp: i8,
    pub(crate) size_exp: i8,
    pub(crate) spec: &'a IngestSpec,
    pub(crate) inp: &'a input::Inputs,
}

/// How a format's lines become records.
#[derive(Clone, Copy)]
pub(crate) enum Convert {
    /// Tardis L2 CSV (L2-only dataset).
    TardisL2,
    /// An order-level converter.
    L3(fn(Lines<'_>, Args<'_>) -> QtResult<L3Output>),
}

/// One crypto format: its registry entry and what `prepare` needs.
#[derive(Clone, Copy)]
pub(crate) struct Crypto {
    pub(crate) def: SourceFormat,
    pub(crate) convert: Convert,
    /// Counters of order-level records: a capture with none of them is L2 only and cannot become MBO.
    pub(crate) order_level_keys: &'static [&'static str],
    /// Exactness caveats of the source, from its counters (written as ingest warnings).
    pub(crate) notes: fn(&Counters) -> Vec<String>,
}

const fn def(
    format: IngestFormat,
    name: &'static str,
    cli_name: &'static str,
    default_source_venue: Option<&'static str>,
    options: &'static [&'static str],
    summary: &'static str,
) -> SourceFormat {
    SourceFormat {
        format,
        name,
        cli_name,
        kind: DatasetKind::Mbo,
        source_format: name,
        vendor: true,
        rule_set: None,
        generic_rule_set: Some(RuleSet::CryptoL3),
        default_source_venue,
        options,
        engine: "qt_crypto",
        summary,
        ingest: crate::ingest,
    }
}

/// Bitstamp capture.
pub const BITSTAMP: SourceFormat = def(
    IngestFormat::Bitstamp,
    "bitstamp",
    "bitstamp",
    Some("bitstamp"),
    &["taker_window_us", "currency"],
    "Bitstamp WebSocket capture (live_orders, live_trades), order by order",
);
/// Bitfinex capture.
pub const BITFINEX: SourceFormat = def(
    IngestFormat::Bitfinex,
    "bitfinex",
    "bitfinex",
    Some("bitfinex"),
    &["flags", "max_size", "currency"],
    "Bitfinex raw books (R0) capture, order by order",
);
/// Coinbase `full` channel capture.
pub const COINBASE: SourceFormat = def(
    IngestFormat::Coinbase,
    "coinbase",
    "coinbase",
    Some("coinbase"),
    &["warmup_depth", "currency"],
    "Coinbase full channel capture, order by order",
);
/// Coinbase Exchange `level3` capture.
pub const COINBASE_LEVEL3: SourceFormat = def(
    IngestFormat::CoinbaseLevel3,
    "coinbase_level3",
    "coinbase-level3",
    Some("coinbase"),
    &["currency"],
    "Coinbase Exchange level3 channel capture, order by order",
);
/// Kraken WebSocket v2 `level3` capture.
pub const KRAKEN_L3: SourceFormat = def(
    IngestFormat::KrakenL3,
    "kraken_l3",
    "kraken-l3",
    Some("kraken"),
    &["depth", "checksum_precision", "currency"],
    "Kraken WebSocket v2 level3 capture, order by order inside the subscribed depth",
);
/// Hyperliquid node outputs.
pub const HYPERLIQUID_NODE: SourceFormat = def(
    IngestFormat::HyperliquidNode,
    "hyperliquid_node",
    "hyperliquid-node",
    Some("hyperliquid"),
    &["snapshot_block", "max_block_gap_ms", "currency"],
    "Hyperliquid node outputs (raw book diffs, order statuses, fills, L4 snapshots), order by order",
);
/// Tardis L2 CSV.
pub const TARDIS_L2: SourceFormat = SourceFormat {
    kind: DatasetKind::L2,
    generic_rule_set: None,
    ..def(
        IngestFormat::TardisL2,
        "tardis_l2",
        "tardis-l2",
        None,
        &["currency"],
        "Tardis incremental_book_L2 and trades CSV, L2 only",
    )
};

/// The formats this crate reads, in registry order (from `CRYPTO`).
pub const FORMATS: [SourceFormat; CRYPTO.len()] = {
    let mut out = [BITSTAMP; CRYPTO.len()];
    let mut i = 0;
    while i < CRYPTO.len() {
        out[i] = CRYPTO[i].def;
        i += 1;
    }
    out
};

const CRYPTO: [Crypto; 7] = [
    Crypto {
        def: BITSTAMP,
        convert: Convert::L3(conv_bitstamp),
        order_level_keys: &["order_created", "order_changed", "order_deleted"],
        notes: no_notes,
    },
    Crypto {
        def: BITFINEX,
        convert: Convert::L3(conv_bitfinex),
        order_level_keys: &["snapshots", "adds", "updates", "deletes", "delete_unknown"],
        notes: no_notes,
    },
    Crypto {
        def: COINBASE,
        convert: Convert::L3(conv_coinbase),
        order_level_keys: &["received", "open", "change", "done_canceled", "done_filled", "match"],
        notes: no_notes,
    },
    Crypto {
        def: COINBASE_LEVEL3,
        convert: Convert::L3(conv_coinbase_level3),
        order_level_keys: &["snapshots", "open", "change", "done", "match", "noop"],
        notes: notes_coinbase_level3,
    },
    Crypto {
        def: KRAKEN_L3,
        convert: Convert::L3(conv_kraken_l3),
        order_level_keys: &["snapshots", "adds", "modifies", "deletes"],
        notes: notes_kraken_l3,
    },
    Crypto {
        def: HYPERLIQUID_NODE,
        convert: Convert::L3(conv_hyperliquid_node),
        order_level_keys: &["diffs"],
        notes: notes_hyperliquid_node,
    },
    Crypto {
        def: TARDIS_L2,
        convert: Convert::TardisL2,
        order_level_keys: &[],
        notes: no_notes,
    },
];

/// The crypto format with this spec value.
pub(crate) fn get(format: IngestFormat) -> Option<&'static Crypto> {
    CRYPTO.iter().find(|c| c.def.format == format)
}

fn conv_bitstamp(lines: Lines<'_>, a: Args<'_>) -> QtResult<L3Output> {
    let mut c = bitstamp::Config {
        instruments: a.instruments,
        price_exp: a.price_exp,
        size_exp: a.size_exp,
        ..Default::default()
    };
    if let Some(w) = opt_i64(a.spec, "taker_window_us")? {
        c.taker_window_us = w;
    }
    bitstamp::convert_full(lines, &c)
}

fn conv_bitfinex(lines: Lines<'_>, a: Args<'_>) -> QtResult<L3Output> {
    let mut c = bitfinex::Config {
        instruments: a.instruments,
        price_exp: a.price_exp,
        size_exp: a.size_exp,
        flags: a.inp.meta.flags.unwrap_or(0),
        ..Default::default()
    };
    if let Some(f) = opt_i64(a.spec, "flags")? {
        c.flags = f.and_then(|x| u64::try_from(x).ok()).unwrap_or(0);
    }
    if let Some(m) = opt_i64(a.spec, "max_size")? {
        c.max_size = m
            .and_then(|x| u64::try_from(x).ok())
            .filter(|x| *x > 0)
            .unwrap_or(u64::MAX);
    }
    bitfinex::convert_full(lines, &c)
}

fn conv_coinbase(lines: Lines<'_>, a: Args<'_>) -> QtResult<L3Output> {
    let mut c = coinbase::Config {
        instruments: a.instruments,
        price_exp: a.price_exp,
        size_exp: a.size_exp,
        ..Default::default()
    };
    if let Some(Some(d)) = opt_i64(a.spec, "warmup_depth")? {
        c.warmup_depth = usize::try_from(d).map_err(|_| cfg_err("options.warmup_depth: must be >= 0"))?;
    }
    coinbase::convert_full(lines, &c)
}

fn conv_coinbase_level3(lines: Lines<'_>, a: Args<'_>) -> QtResult<L3Output> {
    coinbase_l3::convert_full(
        lines,
        &coinbase_l3::Config {
            instruments: a.instruments,
            price_exp: a.price_exp,
            size_exp: a.size_exp,
        },
    )
}

fn conv_kraken_l3(lines: Lines<'_>, a: Args<'_>) -> QtResult<L3Output> {
    let mut c = kraken_l3::Config {
        instruments: a.instruments,
        price_exp: a.price_exp,
        size_exp: a.size_exp,
        ..Default::default()
    };
    if let Some(Some(d)) = opt_i64(a.spec, "depth")? {
        c.depth = Some(u32::try_from(d).map_err(|_| cfg_err("options.depth: must be 10, 100 or 1000"))?);
    }
    if let Some(v) = a.spec.options.get("checksum_precision") {
        c.precision = checksum_precision(v)?;
    }
    kraken_l3::convert_full(lines, &c)
}

fn conv_hyperliquid_node(lines: Lines<'_>, a: Args<'_>) -> QtResult<L3Output> {
    let mut c = hyperliquid::Config {
        instruments: a.instruments,
        price_exp: a.price_exp,
        size_exp: a.size_exp,
        ..Default::default()
    };
    if let Some(g) = opt_i64(a.spec, "max_block_gap_ms")? {
        c.max_block_gap_ms = g
            .map(|x| u64::try_from(x).map_err(|_| cfg_err("options.max_block_gap_ms: must be >= 0")))
            .transpose()?
            .unwrap_or(0);
    }
    let fixed = match opt_i64(a.spec, "snapshot_block")? {
        Some(Some(b)) => Some(u64::try_from(b).map_err(|_| cfg_err("options.snapshot_block: must be >= 0"))?),
        _ => a.inp.meta.snapshot_block,
    };
    if fixed.is_some() && a.inp.snapshots.len() > 1 {
        return Err(cfg_err(
            "options.snapshot_block: only valid with one L4 snapshot (name several by height)",
        ));
    }
    for f in &a.inp.snapshots {
        let h = fixed.or_else(|| input::snapshot_height_from_name(f)).ok_or_else(|| {
            cfg_err(format!(
                "L4 snapshot {}: block height unknown (options.snapshot_block, capture_meta.json snapshot_block, or \
                 digits in the file name)",
                f.display()
            ))
        })?;
        c.snapshots.push((h, input::read_whole(f)?));
    }
    hyperliquid::convert_full(lines, &c)
}

fn no_notes(_: &Counters) -> Vec<String> {
    Vec::new()
}

fn count(c: &Counters, k: &str) -> u64 {
    c.get(k).copied().unwrap_or(0)
}

fn notes_coinbase_level3(c: &Counters) -> Vec<String> {
    let mut n = Vec::new();
    if count(c, "unseeded_products") > 0 {
        n.push(format!(
            "coinbase_level3: {} product(s) without a REST level=3 snapshot are warm-up only (orders resting before \
             the capture are invisible)",
            count(c, "unseeded_products")
        ));
    }
    if count(c, "gaps") > 0 {
        n.push(format!(
            "coinbase_level3: {} sequence gap(s); the book is uncertain from each gap to the next snapshot",
            count(c, "gaps")
        ));
    }
    if count(c, "schema_assumed") > 0 {
        n.push("coinbase_level3: no schema frame in the capture; the documented level3 schema was used".into());
    }
    n
}

fn notes_kraken_l3(c: &Counters) -> Vec<String> {
    let mut n = vec![
        format!(
            "kraken_l3: exact only inside the subscribed depth of {} price levels per side; orders leaving the window \
             are kept frozen (out of view, not cancels) and levels beyond the window are not truth",
            count(c, "window_truncated_depth")
        ),
        "kraken_l3: the feed has no executions; modify / delete cannot be attributed to fills vs cancels".into(),
    ];
    if count(c, "checksums_verified") + count(c, "checksum_mismatches") == 0 {
        n.push("kraken_l3: no checksum was verified".into());
    }
    n
}

fn notes_hyperliquid_node(c: &Counters) -> Vec<String> {
    let mut n = Vec::new();
    if count(c, "unseeded_instruments") > 0 {
        n.push(format!(
            "hyperliquid_node: {} coin(s) without an L4 snapshot are warm-up only",
            count(c, "unseeded_instruments")
        ));
    }
    if count(c, "fills") == 0 {
        n.push("hyperliquid_node: no node_fills given; size decreases cannot be attributed to fills".into());
    }
    n.push("hyperliquid_node: user addresses are dropped; order ids are the on-chain oids".into());
    n
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;

    /// The options each format reads add up to the keys `prepare` accepts.
    #[test]
    fn format_options_add_up_to_the_accepted_keys() {
        let ours: BTreeSet<&str> = FORMATS.iter().flat_map(|f| f.options.iter().copied()).collect();
        let accepted: BTreeSet<&str> = crate::OPTION_KEYS.iter().copied().collect();
        assert_eq!(ours, accepted);
    }
}
