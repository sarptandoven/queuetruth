//! `prepare` / `ingest` on the committed synthetic capture fixtures: instruments, sessions, re-seeding, data quality
//! and errors. Tests that write a dataset need `qt_ingest`'s `DatasetWriter` / `Dataset` and are gate tests.

mod common;

use common::{fixture, lines, tmpdir};
use indexmap::IndexMap;
use qt_crypto::{bitstamp, prepare, units};
use qt_schema::json::Verdict;
use qt_schema::specs::{IngestFormat, IngestSpec};
use qt_types::{MboEvent, RuleSet, Venue, flags};
use serde_json::{Value, json};

fn spec(format: &str, venue: &str, inputs: &[String], extra: Value) -> IngestSpec {
    let mut v = json!({
        "format": format,
        "venue": venue,
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

const DAY: i64 = 86_400_000_000_000;

#[test]
fn ingest_prepare_bitstamp_capture_dir() {
    let p = prepare(&spec("bitstamp", "generic", &[dir("bitstamp")], json!({}))).unwrap();
    assert_eq!(
        (p.format, p.l2_only, p.venue, p.source_venue.as_str()),
        ("bitstamp", false, Venue::Generic, "bitstamp")
    );
    assert_eq!(p.rule_set, RuleSet::CryptoL3); // `generic` on an L3 capture means crypto_l3 (E38, M25)
    // the same records as the pure converter on the files in et's read order
    let files: Vec<_> = ["live_orders.jsonl", "live_trades.jsonl", "detail_order_book.jsonl"]
        .iter()
        .map(|f| fixture(&format!("bitstamp/{f}")))
        .collect();
    let all: Vec<String> = qt_crypto::input::lines(&files).unwrap().map(Result::unwrap).collect();
    let cfg = bitstamp::Config {
        instruments: IndexMap::from([("btcusd".to_string(), 1)]),
        ..Default::default()
    };
    let (events, trades, _) = bitstamp::convert(lines(&all), &cfg).unwrap();
    assert_eq!((p.events.clone(), p.trades.clone()), (events, trades));
    assert_eq!(p.inputs.len(), 4); // three data files + capture_meta.json
    let i = &p.instruments[..];
    assert_eq!(i.len(), 1);
    let date = units::session_date_of(units::SessionClock::Utc, p.events[0].ts_event);
    assert_eq!(
        (i[0].instrument_id, i[0].session_date, i[0].symbol.as_str()),
        (1, date, "btcusd")
    );
    assert_eq!(
        (i[0].midnight_utc_ns, i[0].open_s, i[0].close_s),
        (i64::from(date) * DAY, 0, 86_400)
    );
    assert_eq!(
        (i[0].tick_size, i[0].currency.as_str(), i[0].size_exp, i[0].price_exp),
        (5_000_000_000, "USD", -8, -9)
    );
    assert_eq!(
        (
            i[0].session_tz.as_str(),
            i[0].source.as_str(),
            i[0].source_venue.as_str()
        ),
        ("UTC", "bitstamp", "bitstamp")
    );
    let dq = &p.data_quality;
    assert_eq!(
        (dq.records, dq.snapshot_records, dq.pre_snapshot_rows_skipped),
        (p.events.len() as u64, 4, 1)
    );
    assert_eq!((dq.sequence_gaps, dq.verdict), (0, Verdict::Warnings));
    assert_eq!(
        (
            ss(&p, "taker_orders_suppressed"),
            ss(&p, "deleted_unknown"),
            ss(&p, "deleted_unknown_outside_seed_range")
        ),
        (1, 1, 1)
    );
    assert!(!dq.source_specific.contains_key("records")); // volumes stay out of the verdict
    assert_eq!(p.counters["seeded_orders"], 4);
    assert!(p.spec_echo.get("out").is_none() && p.spec_echo.get("format") == Some(&json!("bitstamp")));
}

#[test]
fn ingest_prepare_bitfinex_capture_dir() {
    let p = prepare(&spec("bitfinex", "bitfinex", &[dir("bitfinex")], json!({}))).unwrap();
    assert_eq!((p.venue, p.source_venue.as_str()), (Venue::Generic, "bitfinex"));
    assert_eq!((p.counters["seq_all_checked"], p.counters["seq_all_gaps"]), (13, 0)); // flags from capture_meta.json
    assert!(p.events.iter().any(|e| e.order_id == 104 && e.size == 5_000_000_000)); // u64 sizes: no chunking
    assert_eq!(
        (
            ss(&p, "no_execution_records"),
            ss(&p, "delete_unknown"),
            ss(&p, "chunked_orders")
        ),
        (1, 1, 0)
    );
    assert_eq!(
        (
            p.trades.len(),
            p.instruments[0].symbol.as_str(),
            p.instruments[0].currency.as_str()
        ),
        (1, "tBTCUSD", "USD")
    );
    // options override the capture flags: without SEQ_ALL nothing is checked
    let p = prepare(&spec(
        "bitfinex",
        "generic",
        &[dir("bitfinex")],
        json!({"options": {"flags": 32768}}),
    ))
    .unwrap();
    assert_eq!(p.counters["seq_all_checked"], 0);
    let p = prepare(&spec(
        "bitfinex",
        "generic",
        &[dir("bitfinex")],
        json!({"options": {"max_size": 4294967295u64}}),
    ))
    .unwrap();
    assert_eq!(ss(&p, "chunked_orders"), 1);
}

#[test]
fn ingest_prepare_coinbase_capture_dir() {
    let p = prepare(&spec("coinbase", "coinbase", &[dir("coinbase")], json!({}))).unwrap();
    assert_eq!(
        (p.venue, p.source_venue.as_str(), p.events.len(), p.trades.len()),
        (Venue::Coinbase, "coinbase", 11, 2)
    );
    assert_eq!(p.counters["duplicates_dropped"], 2); // matches.jsonl repeats the full channel's matches
    assert_eq!((ss(&p, "warmup_events"), p.data_quality.feed_gaps.len()), (9, 1));
    let g = &p.data_quality.feed_gaps[0];
    assert_eq!(
        (g.instrument_id, g.ts_start, g.ts_end),
        (1, p.events[0].ts_event, units::iso_ns("2026-10-11T00:00:04Z").unwrap())
    );
    assert_eq!(p.data_quality.verdict, Verdict::Warnings);
    assert_eq!(p.rule_set, RuleSet::Coinbase);
}

/// Schema 1.1 rule sets (E38, M25): the L3 formats default to `crypto_l3` instead of `generic`; other
/// names are kept; the manifest venue is the rule set's record-semantics venue.
#[test]
fn ingest_rule_set_crypto_l3_default() {
    use IngestFormat::{Bitfinex, Bitstamp, Coinbase, TardisL2};
    for (venue, format, want) in [
        ("generic", Bitstamp, RuleSet::CryptoL3),
        ("bitstamp", Bitstamp, RuleSet::CryptoL3),
        ("Generic", Bitfinex, RuleSet::CryptoL3),
        ("bitfinex", Bitfinex, RuleSet::CryptoL3),
        ("generic", Coinbase, RuleSet::CryptoL3),
        ("coinbase", Coinbase, RuleSet::Coinbase),
        ("crypto_l3", Bitstamp, RuleSet::CryptoL3),
        ("crypto-l3", Coinbase, RuleSet::CryptoL3),
        ("generic", TardisL2, RuleSet::Generic),
        ("crypto_l3", TardisL2, RuleSet::CryptoL3),
    ] {
        assert_eq!(
            qt_crypto::rule_set_of(venue, format).unwrap(),
            want,
            "{venue} {format:?}"
        );
    }
    assert_eq!(
        qt_crypto::rule_set_of("lse", Bitstamp).unwrap_err().code(),
        "QT-CONFIG-001"
    );
    for (format, venue) in [
        ("bitfinex", "bitfinex"),
        ("coinbase", "crypto_l3"),
        ("bitstamp", "crypto_l3"),
    ] {
        let p = prepare(&spec(format, venue, &[dir(format)], json!({}))).unwrap();
        assert_eq!((p.rule_set, p.venue), (RuleSet::CryptoL3, Venue::Generic), "{format}");
        assert!(p.instruments.iter().all(|i| i.venue == Venue::Generic));
        assert_eq!(p.data_quality.hidden_prints, 0);
    }
    let p = prepare(&spec("tardis_l2", "generic", &[dir("tardis")], json!({}))).unwrap();
    assert_eq!(p.rule_set, RuleSet::Generic);
}

/// Schema 1.1 `open_s` / `close_s` (3.11 #18, review m5): honoured on every instrument row, validated.
#[test]
fn ingest_open_close_fields() {
    let p = prepare(&spec(
        "bitstamp",
        "generic",
        &[dir("bitstamp")],
        json!({"open_s": 3600, "close_s": 79200}),
    ))
    .unwrap();
    assert!(p.instruments.iter().all(|i| (i.open_s, i.close_s) == (3600, 79_200)));
    assert_eq!(p.spec_echo.get("open_s"), Some(&json!(3600)));
    let p = prepare(&spec("bitstamp", "generic", &[dir("bitstamp")], json!({"open_s": 60}))).unwrap();
    assert_eq!((p.instruments[0].open_s, p.instruments[0].close_s), (60, 86_400));
    let code = |extra: Value| {
        prepare(&spec("bitstamp", "generic", &[dir("bitstamp")], extra))
            .unwrap_err()
            .code()
    };
    assert_eq!(code(json!({"open_s": 7200, "close_s": 3600})), "QT-CONFIG-001");
    assert_eq!(code(json!({"close_s": 172_801})), "QT-CONFIG-001");
    assert_eq!(code(json!({"open_s": 90_000})), "QT-CONFIG-001"); // after the default close (day length)
}

#[test]
fn ingest_prepare_tardis_plain_and_gzip() {
    let p = prepare(&spec("tardis_l2", "generic", &[dir("tardis")], json!({}))).unwrap();
    assert!(p.l2_only && p.events.is_empty());
    assert_eq!(
        (p.l2.len(), p.trades.len(), p.source_venue.as_str()),
        (13, 3, "binance-futures")
    );
    assert_eq!(
        (
            p.instruments.len(),
            p.instruments[0].symbol.as_str(),
            p.instruments[0].currency.as_str()
        ),
        (1, "BTCUSDT", "USDT")
    );
    assert_eq!(p.instruments[0].tick_size, 500_000_000); // gcd of 99, 99.5, 100, 100.5, 101, 102
    let dq = &p.data_quality;
    assert_eq!(
        (dq.records, dq.snapshot_records, dq.pre_snapshot_rows_skipped),
        (13, 6, 1)
    );
    assert_eq!((ss(&p, "remove_absent_level"), ss(&p, "unknown_side_trades")), (1, 1));
    // gzip inputs give identical rows
    let d = tmpdir("tardis_gz");
    for f in ["incremental_book_L2.csv", "trades.csv"] {
        let raw = std::fs::read(fixture(&format!("tardis/{f}"))).unwrap();
        let mut enc = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        std::io::Write::write_all(&mut enc, &raw).unwrap();
        std::fs::write(d.join(format!("{f}.gz")), enc.finish().unwrap()).unwrap();
    }
    let g = prepare(&spec(
        "tardis_l2",
        "generic",
        &[d.display().to_string()],
        json!({"source_venue": "tardis"}),
    ))
    .unwrap();
    assert_eq!((g.l2.clone(), g.source_venue.as_str()), (p.l2.clone(), "tardis"));
    std::fs::remove_dir_all(&d).unwrap();
}

fn order(ev: &str, oid: u64, side: u8, px: &str, amt: &str, us: i64) -> String {
    format!(
        r#"{{"event":"{ev}","channel":"live_orders_btcusd","data":{{"id":{oid},"order_type":{side},"price_str":"{px}","amount_str":"{amt}","microtimestamp":"{us}"}},"recv_ns":{}}}"#,
        us * 1000
    )
}

/// A Bitstamp capture crossing a UTC midnight: seeded 1 ms before, one event after.
fn midnight_capture(name: &str) -> (std::path::PathBuf, i64) {
    let d = tmpdir(name);
    let midnight_us = 20_738 * 86_400_000_000; // 2026-10-12T00:00:00Z
    let us0 = midnight_us - 1000;
    let seed = format!(
        r#"{{"event":"data","channel":"detail_order_book_btcusd","data":{{"bids":[["61200.00","1","1"]],"asks":[["61205.00","0.8","2"]],"microtimestamp":"{us0}"}},"recv_ns":{}}}"#,
        us0 * 1000
    );
    std::fs::write(d.join("detail_order_book.jsonl"), seed + "\n").unwrap();
    let orders = [
        order("order_created", 11, 0, "61200.00", "0.5", us0 + 10),
        order("order_changed", 1, 0, "61200.00", "0.4", us0 + 20), // size decrease keeps priority
        order("order_created", 12, 0, "61200.00", "0.3", us0 + 30),
        order("order_deleted", 12, 0, "61200.00", "0.3", midnight_us + 1000),
    ];
    std::fs::write(d.join("live_orders.jsonl"), orders.join("\n") + "\n").unwrap();
    (d, midnight_us * 1000)
}

#[test]
fn ingest_day_boundary_reseed() {
    let (d, midnight) = midnight_capture("reseed");
    let p = prepare(&spec("bitstamp", "generic", &[d.display().to_string()], json!({}))).unwrap();
    assert_eq!(p.instruments.len(), 2);
    assert_eq!(
        (
            p.instruments[1].session_date - p.instruments[0].session_date,
            p.instruments[1].midnight_utc_ns
        ),
        (1, midnight)
    );
    let after: Vec<&MboEvent> = p.events.iter().filter(|e| e.ts_event >= midnight).collect();
    let got: Vec<(char, u64, u64, u8)> = after
        .iter()
        .map(|e| (e.action as char, e.order_id, e.size, e.flags))
        .collect();
    let snap = flags::SNAPSHOT;
    assert_eq!(
        got,
        vec![
            ('R', 0, 0, 0),
            ('A', 1, 40_000_000, snap), // FIFO kept: 1 (reduced in place), 11, 12, then the ask
            ('A', 11, 50_000_000, snap),
            ('A', 12, 30_000_000, snap),
            ('A', 2, 80_000_000, snap | flags::LAST),
            ('C', 12, 30_000_000, flags::LAST),
        ]
    );
    assert_eq!(
        (
            p.counters["day_boundary_reseeds"],
            p.counters["day_boundary_reseeded_orders"]
        ),
        (1, 4)
    );
    // a fixed session date that does not hold every record is a config error
    let e = prepare(&spec(
        "bitstamp",
        "generic",
        &[d.display().to_string()],
        json!({"session_date": "2026-10-11"}),
    ))
    .unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001");
    // Eastern sessions: both records fall on 2026-10-11 local time, so no boundary
    let p = prepare(&spec(
        "bitstamp",
        "generic",
        &[d.display().to_string()],
        json!({"tz": "America/New_York"}),
    ))
    .unwrap();
    assert_eq!(
        (p.instruments.len(), p.instruments[0].session_tz.as_str()),
        (1, "America/New_York")
    );
    assert_eq!(p.counters["day_boundary_reseeds"], 0);
    std::fs::remove_dir_all(&d).unwrap();
}

/// Open conflict 5 (3.3): `midnight_utc_ns` without `session_date` derives the date as `qt_ingest` does (the local
/// date whose midnight it is), instead of being rejected.
#[test]
fn ingest_midnight_without_session_date_derives_the_date() {
    let (d, midnight) = midnight_capture("midnight_only");
    let input = [d.display().to_string()];
    // 2026-10-11 00:00 US Eastern (EDT, UTC-4) = 2026-10-11T04:00Z: both records fall in that local day
    let eastern = midnight - DAY + 4 * 3_600_000_000_000;
    let p = prepare(&spec(
        "bitstamp",
        "generic",
        &input,
        json!({"tz": "America/New_York", "midnight_utc_ns": eastern}),
    ))
    .unwrap();
    let date = 20_737; // 2026-10-11
    assert_eq!(
        p.instruments
            .iter()
            .map(|i| (i.session_date, i.midnight_utc_ns))
            .collect::<Vec<_>>(),
        vec![(date, eastern)]
    );
    assert_eq!(
        qt_ingest::time::session_date_of_midnight(eastern, qt_schema::specs::SessionTz::AmericaNewYork).unwrap(),
        date
    );
    // the same as giving the date explicitly
    let q = prepare(&spec(
        "bitstamp",
        "generic",
        &input,
        json!({"tz": "America/New_York", "session_date": "2026-10-11"}),
    ))
    .unwrap();
    assert_eq!(
        (q.instruments[0].session_date, q.instruments[0].midnight_utc_ns),
        (date, eastern)
    );
    // a UTC midnight that does not hold every record names the derived date in the error
    let e = prepare(&spec(
        "bitstamp",
        "generic",
        &input,
        json!({"midnight_utc_ns": midnight}),
    ))
    .unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001");
    assert!(e.to_string().contains("2026-10-12"), "{e}");
    let e = prepare(&spec("bitstamp", "generic", &input, json!({"midnight_utc_ns": -1}))).unwrap_err();
    assert!(e.to_string().contains("must be >= 0"), "{e}");
    std::fs::remove_dir_all(&d).unwrap();
}

#[test]
fn ingest_errors() {
    let code = |s: IngestSpec| prepare(&s).unwrap_err().code();
    assert_eq!(
        code(spec("etcsv", "generic", &[dir("bitstamp")], json!({}))),
        "QT-CONFIG-001"
    );
    assert_eq!(
        code(spec(
            "bitstamp",
            "generic",
            &[dir("bitstamp")],
            json!({"options": {"bogus": 1}})
        )),
        "QT-CONFIG-001"
    );
    assert_eq!(
        code(spec("bitstamp", "lse", &[dir("bitstamp")], json!({}))),
        "QT-CONFIG-001"
    );
    assert_eq!(
        code(spec(
            "bitstamp",
            "generic",
            &[dir("bitstamp")],
            json!({"midnight_utc_ns": 0})
        )),
        "QT-CONFIG-001"
    );
    assert_eq!(
        code(spec("bitstamp", "generic", &[dir("nope")], json!({}))),
        "QT-INPUT-001"
    );
    // an L2-only capture (order book snapshots, no live_orders) is not order-level data
    let only_book = fixture("bitstamp/detail_order_book.jsonl").display().to_string();
    assert_eq!(
        code(spec("bitstamp", "generic", &[only_book], json!({}))),
        "QT-INPUT-002"
    );
    let e = qt_crypto::ingest(&spec("bitstamp", "generic", &[dir("bitstamp")], json!({}))).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001"); // out is required
    // strict: a non-clean verdict fails before anything is written
    let out = std::env::temp_dir().join(format!("qt_crypto_strict_{}", std::process::id()));
    let s = spec(
        "bitstamp",
        "generic",
        &[dir("bitstamp")],
        json!({"strict": true, "out": out.display().to_string()}),
    );
    assert_eq!(qt_crypto::ingest(&s).unwrap_err().code(), "QT-DATA-002");
    assert!(!out.exists());
    // symbols select and order instruments
    let p = prepare(&spec(
        "coinbase",
        "coinbase",
        &[dir("coinbase")],
        json!({"symbols": ["ETH-USD", "BTC-USD"]}),
    ))
    .unwrap();
    assert!(p.events.iter().all(|e| e.instrument_id == 2));
    assert_eq!(qt_crypto::guess_currency("btcusdt"), "USDT");
    assert_eq!(qt_crypto::guess_currency("BTC-PERPETUAL"), "");
}

mod gate {
    use super::*;
    use qt_ingest::Dataset;

    fn ingest_to(
        name: &str,
        s: IngestSpec,
    ) -> (
        qt_crypto::Prepared,
        qt_schema::json::IngestSummary,
        Dataset,
        std::path::PathBuf,
    ) {
        let out = std::env::temp_dir().join(format!("qt_crypto_out_{name}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&out);
        let mut v = serde_json::to_value(&s).unwrap();
        v["out"] = json!(out.display().to_string());
        let s: IngestSpec = serde_json::from_value(v).unwrap();
        let p = prepare(&s).unwrap();
        let sum = qt_crypto::ingest(&s).unwrap();
        let ds = Dataset::open(&out).unwrap();
        (p, sum, ds, out)
    }

    fn check_mbo(p: &qt_crypto::Prepared, ds: &Dataset) {
        let mut got: Vec<MboEvent> = Vec::new();
        for part in ds.partitions() {
            let rows: Vec<MboEvent> = part.mbo().unwrap().map(Result::unwrap).collect();
            assert_eq!(rows.len() as u64, part.rows);
            got.extend(rows);
        }
        let mut want = p.events.clone();
        let date = |e: &MboEvent| {
            p.instruments
                .iter()
                .filter(|i| i.instrument_id == e.instrument_id && i.midnight_utc_ns <= e.ts_event)
                .map(|i| i.session_date)
                .max()
        };
        want.sort_by_key(|e| (date(e), e.instrument_id)); // stable: feed order inside a partition
        assert_eq!(got, want);
    }

    #[test]
    #[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the ingest crate")]
    fn gate_ingest_bitstamp_dataset() {
        let (p, sum, ds, out) = ingest_to("bitstamp", spec("bitstamp", "generic", &[dir("bitstamp")], json!({})));
        assert_eq!(
            (sum.manifest.source_format.as_str(), sum.manifest.source_venue.as_str()),
            ("bitstamp", "bitstamp")
        );
        assert_eq!(
            (sum.manifest.venue, sum.manifest.inputs.len(), ds.partitions().len()),
            (Venue::Generic, 4, 1)
        );
        assert!(sum.spec.get("out").is_none());
        assert_eq!(
            (sum.data_quality.verdict, sum.data_quality.records),
            (Verdict::Warnings, p.events.len() as u64)
        );
        assert_eq!(ds.instruments, p.instruments);
        check_mbo(&p, &ds);
        let st = ds.partitions()[0].source_trades().unwrap().unwrap();
        assert_eq!(st, p.trades);
        std::fs::remove_dir_all(&out).unwrap();
    }

    #[test]
    #[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the ingest crate")]
    fn gate_ingest_bitfinex_and_coinbase_datasets() {
        let (p, _, ds, out) = ingest_to("bitfinex", spec("bitfinex", "generic", &[dir("bitfinex")], json!({})));
        check_mbo(&p, &ds);
        std::fs::remove_dir_all(&out).unwrap();
        let (p, sum, ds, out) = ingest_to("coinbase", spec("coinbase", "coinbase", &[dir("coinbase")], json!({})));
        assert_eq!(
            (sum.manifest.venue, sum.data_quality.feed_gaps.len()),
            (Venue::Coinbase, 1)
        );
        check_mbo(&p, &ds);
        assert_eq!(ds.partitions()[0].source_trades().unwrap().unwrap(), p.trades);
        std::fs::remove_dir_all(&out).unwrap();
    }

    #[test]
    #[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the ingest crate")]
    fn gate_ingest_day_boundary_partitions() {
        let (d, midnight) = midnight_capture("gate_reseed");
        let (p, _, ds, out) = ingest_to(
            "reseed",
            spec("bitstamp", "generic", &[d.display().to_string()], json!({})),
        );
        assert_eq!(ds.partitions().len(), 2);
        let second: Vec<MboEvent> = ds.partitions()[1].mbo().unwrap().map(Result::unwrap).collect();
        assert_eq!((second[0].action, second[0].ts_event), (b'R', midnight + 1_000_000));
        check_mbo(&p, &ds);
        std::fs::remove_dir_all(&out).unwrap();
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    #[cfg_attr(not(feature = "gate"), ignore = "gate test: needs the ingest crate")]
    fn gate_ingest_tardis_l2_dataset() {
        let (p, sum, ds, out) = ingest_to("tardis", spec("tardis_l2", "generic", &[dir("tardis")], json!({})));
        assert_eq!(sum.manifest.kind, qt_schema::json::DatasetKindName::L2);
        assert_eq!(sum.manifest.source_venue, "binance-futures");
        let part = &ds.partitions()[0];
        let rows: Vec<_> = part.l2().unwrap().map(Result::unwrap).collect();
        assert_eq!(rows, p.l2);
        assert_eq!(part.source_trades().unwrap().unwrap(), p.trades);
        std::fs::remove_dir_all(&out).unwrap();
    }
}
