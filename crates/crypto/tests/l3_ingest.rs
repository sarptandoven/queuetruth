//! `prepare` / `ingest` for the 1.2 exact order-level formats (`coinbase_level3`, `kraken_l3`,
//! `hyperliquid_node`) on the committed synthetic fixtures: dispatch, inputs, rule set, data quality and the
//! exactness notes. The dataset-writing test is a gate test.

mod common;

use common::{fixture, lines};
use qt_crypto::{coinbase_l3, prepare};
use qt_schema::json::Verdict;
use qt_schema::specs::IngestSpec;
use qt_types::{RuleSet, Venue};
use serde_json::{Value, json};

fn spec(format: &str, inputs: &[String], extra: Value) -> IngestSpec {
    let mut v = json!({
        "format": format,
        "venue": "generic",
        "inputs": inputs.iter().map(|p| json!({"path": p})).collect::<Vec<_>>(),
    });
    if let (Value::Object(m), Value::Object(e)) = (&mut v, extra) {
        m.extend(e);
    }
    serde_json::from_value(v).unwrap()
}

fn dir(name: &str) -> String {
    fixture(name).display().to_string()
}

fn ss(p: &qt_crypto::Prepared, k: &str) -> u64 {
    p.data_quality.source_specific.get(k).copied().unwrap_or(0)
}

fn kraken_opts() -> Value {
    json!({"options": {"checksum_precision": {"BTC/USD": [2, 8]}}})
}

#[test]
fn prepare_coinbase_level3_dir() {
    let p = prepare(&spec("coinbase_level3", &[dir("coinbase_level3")], json!({}))).unwrap();
    assert_eq!(
        (p.format, p.l2_only, p.venue, p.rule_set, p.source_venue.as_str()),
        ("coinbase_level3", false, Venue::Generic, RuleSet::CryptoL3, "coinbase")
    );
    // book (snapshots) first, then level3, then capture_meta.json
    let names: Vec<String> = p
        .inputs
        .iter()
        .map(|f| f.file_name().unwrap().to_string_lossy().into_owned())
        .collect();
    assert_eq!(names, ["book.jsonl", "level3.jsonl", "capture_meta.json"]);
    let files = [
        fixture("coinbase_level3/book.jsonl"),
        fixture("coinbase_level3/level3.jsonl"),
    ];
    let all: Vec<String> = qt_crypto::input::lines(&files).unwrap().map(Result::unwrap).collect();
    let cfg = coinbase_l3::Config {
        instruments: [("BTC-USD".to_string(), 1), ("ETH-USD".to_string(), 2)]
            .into_iter()
            .collect(),
        ..Default::default()
    };
    let (events, trades, _) = coinbase_l3::convert(lines(&all), &cfg).unwrap();
    assert_eq!((p.events.clone(), p.trades.clone()), (events, trades));
    assert_eq!(
        p.instruments
            .iter()
            .map(|i| (i.instrument_id, i.symbol.as_str(), i.currency.as_str()))
            .collect::<Vec<_>>(),
        vec![(1, "BTC-USD", "USD"), (2, "ETH-USD", "USD")]
    );
    let dq = &p.data_quality;
    assert_eq!(
        (dq.sequence_gaps, dq.feed_gaps.len(), dq.verdict),
        (1, 2, Verdict::Warnings)
    );
    assert_eq!(ss(&p, "unseeded_products"), 1);
    assert_eq!(dq.snapshot_records, 2 + 4 + 5);
    assert_eq!(p.notes.len(), 2);
    assert!(p.notes[0].contains("warm-up only") && p.notes[1].contains("sequence gap"));
}

#[test]
fn prepare_kraken_l3_dir() {
    let p = prepare(&spec("kraken_l3", &[dir("kraken_l3")], kraken_opts())).unwrap();
    assert_eq!(
        (p.format, p.rule_set, p.source_venue.as_str()),
        ("kraken_l3", RuleSet::CryptoL3, "kraken")
    );
    assert_eq!(p.inputs.len(), 2); // level3.jsonl + capture_meta.json (gen.py / expected_view.json are not data)
    let dq = &p.data_quality;
    assert_eq!(dq.verdict, Verdict::Warnings);
    assert_eq!(
        (
            ss(&p, "window_truncated_depth"),
            ss(&p, "no_execution_records"),
            ss(&p, "out_of_view_vanished"),
            ss(&p, "checksum_mismatches")
        ),
        (10, 1, 3, 0)
    );
    assert!(dq.reasons.iter().any(|r| r == "window_truncated_depth = 10"));
    assert!(dq.feed_gaps.is_empty());
    assert!(p.notes[0].contains("exact only inside the subscribed depth of 10"));
    assert_eq!(p.instruments[0].currency, "USD");
    // bad options
    for o in [
        json!({"options": {"checksum_precision": {"BTC/USD": [2]}}}),
        json!({"options": {"checksum_precision": [2, 8]}}),
        json!({"options": {"depth": 25}}),
        json!({"options": {"depth": -1}}),
        json!({"options": {"nope": 1}}),
    ] {
        let e = prepare(&spec("kraken_l3", &[dir("kraken_l3")], o.clone())).unwrap_err();
        assert_eq!(e.code(), "QT-CONFIG-001", "{o}");
    }
    // without the precision every float-quantity checksum mismatches: feed gaps and a warning counter
    let p = prepare(&spec("kraken_l3", &[dir("kraken_l3")], json!({}))).unwrap();
    assert!(ss(&p, "checksum_mismatches") > 0 && !p.data_quality.feed_gaps.is_empty());
}

#[test]
fn prepare_hyperliquid_node_dir() {
    let p = prepare(&spec("hyperliquid_node", &[dir("hyperliquid_node")], json!({}))).unwrap();
    assert_eq!(
        (p.format, p.rule_set, p.source_venue.as_str()),
        ("hyperliquid_node", RuleSet::CryptoL3, "hyperliquid")
    );
    // three hourly node files (no extension) plus the snapshot, found recursively
    assert_eq!(p.inputs.len(), 4);
    assert!(p.inputs.iter().any(|f| f.ends_with("l4_snapshot_1000.json")));
    let dq = &p.data_quality;
    assert_eq!(
        (dq.verdict, dq.feed_gaps.len(), dq.snapshot_records),
        (Verdict::Clean, 0, 4)
    );
    assert_eq!(p.trades.len(), 3);
    assert_eq!(p.instruments.len(), 1);
    assert!(p.notes.iter().any(|n| n.contains("user addresses are dropped")));
    // the snapshot height can come from the options (overriding the file name)
    let p2 = prepare(&spec(
        "hyperliquid_node",
        &[dir("hyperliquid_node")],
        json!({"options": {"snapshot_block": 999}}),
    ))
    .unwrap();
    // at 999 the block-1000 diff applies on top of the snapshot instead of being superseded
    assert_eq!(p2.events.len(), p.events.len() + 1);
    // diffs only (explicit file): unseeded warm-up
    let diffs = fixture("hyperliquid_node/node_raw_book_diffs_by_block/hourly/20261011/0");
    let p3 = prepare(&spec("hyperliquid_node", &[diffs.display().to_string()], json!({}))).unwrap();
    assert_eq!(p3.data_quality.verdict, Verdict::Warnings);
    assert_eq!(ss(&p3, "unseeded_instruments"), 1);
}

mod gate {
    use super::*;
    use qt_ingest::Dataset;

    #[test]
    #[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the ingest crate")]
    fn gate_ingest_l3_sources_write_datasets() {
        for (fmt, extra, verdict) in [
            ("coinbase_level3", json!({}), Verdict::Warnings),
            ("kraken_l3", kraken_opts(), Verdict::Warnings),
            ("hyperliquid_node", json!({}), Verdict::Clean),
        ] {
            let out = std::env::temp_dir().join(format!("qt_crypto_out_{fmt}_{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&out);
            let mut s = spec(fmt, &[dir(fmt)], extra);
            s.out = Some(out.display().to_string());
            let p = prepare(&s).unwrap();
            let sum = qt_crypto::ingest(&s).unwrap();
            assert_eq!(sum.manifest.source_format, fmt);
            assert_eq!(sum.manifest.rule_set, Some(RuleSet::CryptoL3));
            assert_eq!(sum.data_quality.verdict, verdict, "{fmt}");
            assert_eq!(sum.warnings, p.notes, "{fmt}");
            assert!(
                p.notes
                    .iter()
                    .all(|n| sum.data_quality.reasons.contains(&format!("warning: {n}"))),
                "{fmt}"
            );
            let ds = Dataset::open(&out).unwrap();
            let n: u64 = ds.partitions().iter().map(|x| x.rows).sum();
            assert_eq!(n, p.events.len() as u64);
            std::fs::remove_dir_all(&out).unwrap();
        }
    }
}
