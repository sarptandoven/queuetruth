//! Regenerates the committed seed corpora under `fuzz/seeds/<target>/`: `cargo run --example gen_seeds` from `fuzz/`.
//!
//! Every seed is synthetic (built with the crates' own encoders) or a copy of a test fixture already in the
//! repository (`crates/crypto/tests/fixtures`), so no market data with redistribution terms is committed. The first
//! byte of each seed is the harness selector (see `qt_fuzz`). Output is deterministic.

use std::ffi::c_char;
use std::io::Write;
use std::path::{Path, PathBuf};

use ::dbn::encode::EncodeRecord;
use ::dbn::encode::dbn::Encoder;
use ::dbn::{
    FlagSet, InstrumentDefMsg, MappingInterval, MboMsg, Metadata, RecordHeader, SType, Schema, SymbolMapping, TradeMsg,
    rtype,
};
use qt_ingest::iex::synth::{Segment, frames, message_bytes, pcap, pcapng};
use qt_ingest::iex::{Message as Deep, symbol};
use qt_ingest::itch::{Body, Header, Message, StockDirectory, encode, stock};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).to_path_buf()
}

fn write(target: &str, name: &str, sel: u8, body: &[u8]) {
    let dir = root().join("seeds").join(target);
    std::fs::create_dir_all(&dir).unwrap();
    let mut v = Vec::with_capacity(body.len() + 1);
    v.push(sel);
    v.extend_from_slice(body);
    std::fs::write(dir.join(name), v).unwrap();
}

fn gz(b: &[u8]) -> Vec<u8> {
    let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    e.write_all(b).unwrap();
    e.finish().unwrap()
}

fn fixture(rel: &str) -> Vec<u8> {
    std::fs::read(root().join("../crates/crypto/tests/fixtures").join(rel)).unwrap()
}

// ---- ITCH ----------------------------------------------------------------------------------------------------------

fn itch_day() -> Vec<u8> {
    let mut ts = 34_200_000_000_000u64;
    let mut msgs = Vec::new();
    let mut m = |kind: u8, locate: u16, body: Body| {
        ts += 1_000;
        msgs.push(Message {
            header: Header {
                kind,
                locate,
                tracking: 0,
                ts_ns: ts,
            },
            body,
        });
    };
    let dir = |s: &str| StockDirectory {
        stock: stock(s),
        market_category: b'Q',
        financial_status: b'N',
        round_lot_size: 100,
        round_lots_only: b'N',
        issue_classification: b'C',
        issue_sub_type: *b"Z ",
        authenticity: b'P',
        short_sale_threshold: b'N',
        ipo_flag: b' ',
        luld_tier: b'1',
        etp_flag: b'N',
        etp_leverage_factor: 0,
        inverse_indicator: b'N',
    };
    let (aapl, spy) = (stock("AAPL"), stock("SPY"));
    m(b'S', 0, Body::SystemEvent { event: b'O' });
    m(b'R', 1, Body::StockDirectory(dir("AAPL")));
    m(b'R', 2, Body::StockDirectory(dir("SPY")));
    m(
        b'H',
        1,
        Body::TradingAction {
            stock: aapl,
            state: b'T',
            reason: *b"    ",
        },
    );
    m(
        b'Y',
        1,
        Body::RegSho {
            stock: aapl,
            action: b'0',
        },
    );
    m(
        b'L',
        1,
        Body::MarketParticipant {
            mpid: *b"NSDQ",
            stock: aapl,
            primary: b'Y',
            mode: b'N',
            state: b'A',
        },
    );
    m(
        b'V',
        0,
        Body::MwcbLevels {
            level1: 1,
            level2: 2,
            level3: 3,
        },
    );
    m(b'W', 0, Body::MwcbStatus { level: b'1' });
    m(
        b'K',
        1,
        Body::IpoQuoting {
            stock: aapl,
            release_time_s: 1,
            qualifier: b'A',
            price: 1,
        },
    );
    m(
        b'J',
        1,
        Body::LuldCollar {
            stock: aapl,
            reference: 1,
            upper: 2,
            lower: 1,
            extension: 0,
        },
    );
    m(
        b'h',
        1,
        Body::OperationalHalt {
            stock: aapl,
            market: b'Q',
            action: b'T',
        },
    );
    let add = |order_ref, buy_sell, shares, price, mpid| Body::AddOrder {
        order_ref,
        buy_sell,
        shares,
        stock: aapl,
        price,
        mpid,
    };
    m(b'A', 1, add(1, b'B', 100, 1_852_500, None));
    m(b'F', 1, add(2, b'S', 200, 1_853_000, Some(*b"GSCO")));
    m(b'A', 1, add(3, b'X', 1, 1, None));
    m(
        b'A',
        2,
        Body::AddOrder {
            order_ref: 4,
            buy_sell: b'S',
            shares: 50,
            stock: spy,
            price: 3_200_000,
            mpid: None,
        },
    );
    m(
        b'E',
        1,
        Body::Executed {
            order_ref: 1,
            shares: 30,
            match_no: 1,
        },
    );
    m(
        b'C',
        1,
        Body::ExecutedWithPrice {
            order_ref: 2,
            shares: 20,
            match_no: 2,
            printable: b'Y',
            price: 1_852_900,
        },
    );
    m(
        b'C',
        1,
        Body::ExecutedWithPrice {
            order_ref: 2,
            shares: 20,
            match_no: 3,
            printable: b'N',
            price: 1_852_900,
        },
    );
    m(
        b'X',
        1,
        Body::Cancel {
            order_ref: 1,
            shares: 10,
        },
    );
    m(
        b'U',
        1,
        Body::Replace {
            old_ref: 1,
            new_ref: 5,
            shares: 70,
            price: 1_852_600,
        },
    );
    m(
        b'E',
        1,
        Body::Executed {
            order_ref: 5,
            shares: 1_000,
            match_no: 4,
        },
    );
    m(b'D', 1, Body::Delete { order_ref: 2 });
    m(b'D', 1, Body::Delete { order_ref: 99 });
    m(
        b'P',
        1,
        Body::Trade {
            order_ref: 0,
            buy_sell: b'B',
            shares: 5,
            stock: aapl,
            price: 1_852_700,
            match_no: 5,
        },
    );
    m(
        b'Q',
        1,
        Body::CrossTrade {
            shares: 1_000,
            stock: aapl,
            price: 1_852_500,
            match_no: 6,
            cross_type: b'O',
        },
    );
    m(
        b'Q',
        1,
        Body::CrossTrade {
            shares: 0,
            stock: aapl,
            price: 1_852_500,
            match_no: 7,
            cross_type: b'C',
        },
    );
    m(b'B', 1, Body::BrokenTrade { match_no: 5 });
    m(
        b'N',
        1,
        Body::Rpii {
            stock: aapl,
            interest: b'B',
        },
    );
    m(b'S', 0, Body::SystemEvent { event: b'C' });
    let mut out = Vec::new();
    for msg in &msgs {
        encode(msg, &mut out);
    }
    // A zero-length frame, an unknown type and a short known type.
    out.extend_from_slice(&[0, 0, 0, 3, b'z', 1, 2, 0, 4, b'A', 0, 1, 0]);
    out
}

fn itch_noii() -> Vec<u8> {
    let mut out = Vec::new();
    let mut raw = vec![b'I'];
    raw.extend_from_slice(&[0; 49]);
    out.extend_from_slice(&(raw.len() as u16).to_be_bytes());
    out.extend_from_slice(&raw);
    let mut raw = vec![b'O'];
    raw.extend_from_slice(&[7; 47]);
    out.extend_from_slice(&(raw.len() as u16).to_be_bytes());
    out.extend_from_slice(&raw);
    out
}

// ---- IEX DEEP+ -----------------------------------------------------------------------------------------------------

const T0: i64 = 1_791_552_600_000_000_000;

fn deep_segments() -> Vec<Segment> {
    let s = symbol("SPY");
    let z = symbol("ZVZZT");
    let mut ts = T0;
    let mut next = || {
        ts += 1_000;
        ts
    };
    let msgs: Vec<Vec<u8>> = vec![
        Deep::SystemEvent {
            event: b'O',
            ts: next(),
        },
        Deep::SecurityDirectory {
            flags: 0x80,
            ts: next(),
            symbol: z,
            round_lot: 100,
            adjusted_poc: 100_000,
            luld_tier: 1,
        },
        Deep::TradingStatus {
            status: b'T',
            ts: next(),
            symbol: s,
            reason: *b"    ",
        },
        Deep::RetailLiquidity {
            indicator: b' ',
            ts: next(),
            symbol: s,
        },
        Deep::OperationalHalt {
            status: b'N',
            ts: next(),
            symbol: s,
        },
        Deep::ShortSalePriceTest {
            status: 0,
            ts: next(),
            symbol: s,
            detail: b' ',
        },
        Deep::SecurityEvent {
            event: b'O',
            ts: next(),
            symbol: s,
        },
        Deep::AddOrder {
            side: b'8',
            ts: next(),
            symbol: s,
            order_id: 1,
            size: 100,
            price: 5_000_000,
        },
        Deep::AddOrder {
            side: b'5',
            ts: next(),
            symbol: s,
            order_id: 2,
            size: 300,
            price: 5_000_100,
        },
        Deep::AddOrder {
            side: b'8',
            ts: next(),
            symbol: z,
            order_id: 3,
            size: 10,
            price: 100_000,
        },
        Deep::OrderModify {
            flags: 1,
            ts: next(),
            symbol: s,
            order_id: 1,
            size: 50,
            price: 5_000_000,
        },
        Deep::OrderModify {
            flags: 0,
            ts: next(),
            symbol: s,
            order_id: 2,
            size: 400,
            price: 5_000_200,
        },
        Deep::OrderExecuted {
            sale_flags: 0,
            ts: next(),
            symbol: s,
            order_id: 1,
            size: 20,
            price: 5_000_000,
            trade_id: 1,
        },
        Deep::OrderExecuted {
            sale_flags: 0x08,
            ts: next(),
            symbol: s,
            order_id: 2,
            size: 900,
            price: 5_000_200,
            trade_id: 2,
        },
        Deep::Trade {
            sale_flags: 0x20,
            ts: next(),
            symbol: s,
            size: 7,
            price: 5_000_050,
            trade_id: 3,
        },
        Deep::TradeBreak {
            sale_flags: 0,
            ts: next(),
            symbol: s,
            size: 7,
            price: 5_000_050,
            trade_id: 3,
        },
        Deep::OrderDelete {
            ts: next(),
            symbol: s,
            order_id: 1,
        },
        Deep::ClearBook { ts: next(), symbol: z },
        Deep::SystemEvent {
            event: b'C',
            ts: next(),
        },
    ]
    .iter()
    .map(message_bytes)
    .collect();
    let mut segs: Vec<Segment> = msgs
        .chunks(4)
        .enumerate()
        .map(|(k, c)| Segment {
            channel: 1,
            session: 0x50ff_0000,
            first_seq: 1 + 4 * k as i64,
            send_time: T0 + 100_000 * k as i64,
            capture_ns: T0 + 100_000 * k as i64 + 10,
            messages: c.to_vec(),
        })
        .collect();
    // An A/B duplicate of the second segment, a gap, a heartbeat, a short message and an unknown type.
    let dup = segs[1].clone();
    segs.insert(2, dup);
    segs.push(Segment {
        channel: 1,
        session: 0x50ff_0000,
        first_seq: 40,
        send_time: T0 + 9_000_000,
        capture_ns: T0 + 9_000_010,
        messages: vec![vec![b'a', b'8', 0], vec![b'?', 1, 2, 3], Vec::new()],
    });
    segs.push(Segment {
        channel: 1,
        session: 0x50ff_0000,
        first_seq: 43,
        send_time: T0 + 9_500_000,
        capture_ns: T0 + 9_500_010,
        messages: Vec::new(),
    });
    segs
}

// ---- DBN -----------------------------------------------------------------------------------------------------------

const D: i64 = 86_400_000_000_000;

fn dbn_bytes<R: ::dbn::encode::DbnEncodable>(md: &Metadata, recs: &[R], zstd: bool) -> Vec<u8> {
    let mut out = Vec::new();
    if zstd {
        let mut enc = Encoder::with_zstd(&mut out, md).unwrap();
        for r in recs {
            enc.encode_record(r).unwrap();
        }
    } else {
        let mut enc = Encoder::new(&mut out, md).unwrap();
        for r in recs {
            enc.encode_record(r).unwrap();
        }
    }
    out
}

fn metadata(schema: Schema, start: u64, mappings: Vec<SymbolMapping>) -> Metadata {
    Metadata::builder()
        .dataset("GLBX.MDP3")
        .schema(Some(schema))
        .start(start)
        .stype_in(Some(SType::RawSymbol))
        .stype_out(SType::InstrumentId)
        .mappings(mappings)
        .build()
}

fn mbo_records() -> Vec<MboMsg> {
    let day = 19_000i64;
    (0..12u32)
        .map(|i| MboMsg {
            hd: RecordHeader::new::<MboMsg>(rtype::MBO, 1, 42, (day * D + 1_000 + i64::from(i)) as u64),
            order_id: 1 + u64::from(i / 4),
            price: if i == 0 {
                i64::MAX
            } else {
                4_500_250_000_000 + i64::from(i % 3) * 250_000_000
            },
            size: 1 + i % 5,
            flags: FlagSet::new(if i % 3 == 2 { 0x80 } else { 0 }),
            channel_id: 3,
            action: if i == 0 { b'R' } else { b"AMCTF"[(i % 5) as usize] } as c_char,
            side: if i % 2 == 0 { b'B' } else { b'A' } as c_char,
            ts_recv: (day * D + 1_005 + i64::from(i)) as u64,
            ts_in_delta: 0,
            sequence: 1 + i,
        })
        .collect()
}

fn mapping() -> Vec<SymbolMapping> {
    let date_of = |ns: i64| metadata(Schema::Mbo, ns as u64, vec![]).start().date();
    vec![SymbolMapping {
        raw_symbol: "ESH1".into(),
        intervals: vec![MappingInterval {
            start_date: date_of(19_000 * D),
            end_date: date_of(19_002 * D),
            symbol: "42".into(),
        }],
    }]
}

fn main() {
    let _ = std::fs::remove_dir_all(root().join("seeds"));

    // ITCH: in memory, through a raw file, through a gzip file, with the symbol filter.
    let day = itch_day();
    write("itch", "day_memory", 0, &day);
    write("itch", "day_file_raw", 8, &day);
    write("itch", "day_file_gzip", 8, &gz(&day));
    write("itch", "day_filter_exp4", 4 | 1, &day);
    write("itch", "noii_direct_listing", 0, &itch_noii());
    write("itch_parse", "day", 0, &day);
    write("itch_parse", "noii_direct_listing", 0, &itch_noii());
    write("ingest", "itch", 0, &day);
    write("ingest", "itch_filter", 3, &gz(&day));

    // IEX DEEP+: pcap and pcapng, raw and gzip, in memory and through files.
    let f = frames(&deep_segments());
    let (p, png) = (pcap(&f), pcapng(&f));
    write("iex", "pcap_memory", 0, &p);
    write("iex", "pcapng_memory", 0, &png);
    write("iex", "pcapng_filter", 4, &png);
    write("iex", "pcap_file_gzip", 8, &gz(&p));
    write("iex", "pcapng_file_gzip", 8, &gz(&png));
    write("iex_parse", "pcap", 0, &p);
    write("iex_parse", "pcapng", 0, &png);
    write("ingest", "iex_pcapng_gzip", 1, &gz(&png));
    write("ingest", "iex_pcap", 1, &p);

    // DBN: MBO plain and zstd, definitions, trades.
    let mbo = mbo_records();
    let md = metadata(Schema::Mbo, (19_000 * D) as u64, mapping());
    let plain = dbn_bytes(&md, &mbo, false);
    write("dbn", "mbo_plain", 0, &plain);
    write("dbn", "mbo_zstd", 0, &dbn_bytes(&md, &mbo, true));
    write("dbn", "mbo_exp0", 3, &plain);
    let def = InstrumentDefMsg {
        hd: RecordHeader::new::<InstrumentDefMsg>(rtype::INSTRUMENT_DEF, 1, 42, 0),
        min_price_increment: 250_000_000,
        ..Default::default()
    };
    write(
        "dbn",
        "definition",
        0,
        &dbn_bytes(&metadata(Schema::Definition, 0, vec![]), &[def], false),
    );
    let trade = TradeMsg {
        hd: RecordHeader::new::<TradeMsg>(rtype::MBP_0, 1, 42, (19_000 * D + 1_003) as u64),
        price: 4_500_250_000_000,
        size: 4,
        action: b'T' as c_char,
        side: b'N' as c_char,
        flags: FlagSet::new(0),
        depth: 0,
        ts_recv: (19_000 * D + 1_004) as u64,
        ts_in_delta: 0,
        sequence: 5,
    };
    write(
        "dbn",
        "trades",
        0,
        &dbn_bytes(&metadata(Schema::Trades, 0, vec![]), &[trade], false),
    );
    write("ingest", "dbn_plain", 2, &plain);

    // Crypto: the repository's test fixtures, one seed per file and selector family.
    let cat = |files: &[&str]| -> Vec<u8> {
        let mut v = Vec::new();
        for f in files {
            v.extend_from_slice(&fixture(f));
            if v.last() != Some(&b'\n') {
                v.push(b'\n');
            }
        }
        v
    };
    let bitstamp = cat(&[
        "bitstamp/live_orders.jsonl",
        "bitstamp/live_trades.jsonl",
        "bitstamp/detail_order_book.jsonl",
    ]);
    let bitfinex = cat(&[
        "bitfinex/book_R0.jsonl",
        "bitfinex/book_P0.jsonl",
        "bitfinex/trades.jsonl",
        "bitfinex/hb.jsonl",
    ]);
    let coinbase = cat(&["coinbase/full.jsonl", "coinbase/matches.jsonl", "coinbase/level2.jsonl"]);
    let cb3 = cat(&["coinbase_level3/book.jsonl", "coinbase_level3/level3.jsonl"]);
    let kraken = cat(&["kraken_l3/level3.jsonl"]);
    let hl_dir = "hyperliquid_node";
    let hl = cat(&[
        &format!("{hl_dir}/node_raw_book_diffs_by_block/hourly/20261011/0"),
        &format!("{hl_dir}/node_fills_by_block/hourly/20261011/0"),
        &format!("{hl_dir}/node_order_statuses_by_block/hourly/20261011/0"),
    ]);
    let mut hl_snap = hl.clone();
    hl_snap.pop();
    hl_snap.extend_from_slice(qt_fuzz::HL_SNAPSHOT_SEP);
    hl_snap.extend_from_slice(&fixture(&format!("{hl_dir}/l4_snapshot_1000.json")));
    let tardis = cat(&["tardis/incremental_book_L2.csv", "tardis/trades.csv"]);
    for (target, body) in [
        ("crypto_bitstamp", &bitstamp),
        ("crypto_bitfinex", &bitfinex),
        ("crypto_coinbase", &coinbase),
        ("crypto_coinbase_level3", &cb3),
        ("crypto_kraken_l3", &kraken),
        ("crypto_hyperliquid", &hl_snap),
        ("crypto_tardis", &tardis),
    ] {
        write(target, "fixture", 0, body);
        write(target, "fixture_instruments", 4, body);
        write(target, "fixture_exp12", 1, body);
    }
    write("crypto_hyperliquid", "fixture_no_snapshot", 8, &hl);
    for (sel, name, body) in [
        (0u8, "bitstamp", &bitstamp),
        (1, "bitfinex", &bitfinex),
        (2, "coinbase", &coinbase),
        (3, "coinbase_level3", &cb3),
        (4, "kraken_l3", &kraken),
        (5, "hyperliquid_node", &hl),
        (6, "tardis_book", &fixture("tardis/incremental_book_L2.csv")),
        (7, "tardis_trades", &fixture("tardis/trades.csv")),
    ] {
        write("crypto_prepare", name, sel, body);
        write(
            "crypto_prepare",
            &format!("{name}_eastern_gzip_write"),
            sel | 8 | 32,
            &gz(body),
        );
    }
    for (name, text) in [
        (
            "bitfinex_frame",
            &b"[17082,[[1234,61200.5,0.30000000000000004],[1235,61201,-1e-8]],5,1700000000000]"[..],
        ),
        (
            "nested",
            &b"{\"a\":[1,-2.50,3e2,\"4\",null,true,false,{\"b\":\"\\u00e9\\n\"}]}"[..],
        ),
        ("date", &b"2026-10-09"[..]),
        ("iso", &b"2026-10-09T13:30:00.123456789Z"[..]),
        ("int", &b"1791552600000"[..]),
    ] {
        write("crypto_json", name, 0, text);
    }
    // etcsv: the embedded day with each spec variant, and an MBO file alone.
    let sep = std::str::from_utf8(qt_fuzz::ETCSV_SEP).unwrap();
    let et_day = format!(
        "{}{sep}{}{sep}{}",
        qt_fuzz::ETCSV_MBO,
        qt_fuzz::ETCSV_TRADES,
        qt_fuzz::ETCSV_INSTRUMENTS
    );
    write("etcsv", "day", 1, et_day.as_bytes());
    write("etcsv", "day_derived_midnight", 0, et_day.as_bytes());
    write("etcsv", "day_instruments_in_spec", 3, et_day.as_bytes());
    write("etcsv", "day_eastern_exp4_filter", 1 | 4 | 8 | 16, et_day.as_bytes());
    write("etcsv", "mbo_only", 1, qt_fuzz::ETCSV_MBO.as_bytes());

    // Parquet: the files ingest writes for the etcsv day (MBO partition, instruments, source trades) in each role of
    // the `parquet` harness, plus an L2 partition from the Tardis fixture.
    let tmp = qt_fuzz::scratch_root().join("gen-seeds");
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).unwrap();
    let ds = tmp.join("etcsv-ds");
    let inputs: Vec<_> = [
        ("mbo.csv", qt_fuzz::ETCSV_MBO),
        ("trades.csv", qt_fuzz::ETCSV_TRADES),
        ("instruments.csv", qt_fuzz::ETCSV_INSTRUMENTS),
    ]
    .iter()
    .map(|(name, text)| {
        let p = tmp.join(name);
        std::fs::write(&p, text).unwrap();
        serde_json::json!({"path": p.to_str().unwrap()})
    })
    .collect();
    let spec = |v: serde_json::Value| -> qt_schema::specs::IngestSpec { serde_json::from_value(v).unwrap() };
    qt_ingest::ingest(&spec(serde_json::json!({
        "format": "etcsv", "inputs": inputs, "venue": "nasdaq", "midnight_utc_ns": 1_577_682_000_000_000_000i64,
        "out": ds.to_str().unwrap(),
    })))
    .unwrap();
    let read = |rel: &str| std::fs::read(ds.join(rel)).unwrap();
    let mbo_part = read("mbo/date=2019-12-30/instrument_id=13/part-00000.parquet");
    let inst = read("instruments.parquet");
    let trades_part = read("trades_source/date=2019-12-30/instrument_id=13/part-00000.parquet");
    write("parquet", "mbo_part_alone", 0, &mbo_part);
    write("parquet", "instruments_in_spec", 1, &inst);
    write("parquet", "dataset_mbo_part", 2, &mbo_part);
    write("parquet", "dataset_instruments", 3, &inst);
    write("parquet", "dataset_trades_part", 4, &trades_part);
    let tardis_csv = tmp.join("incremental_book_L2.csv");
    std::fs::write(&tardis_csv, fixture("tardis/incremental_book_L2.csv")).unwrap();
    let l2 = tmp.join("tardis-ds");
    qt_crypto::ingest(&spec(serde_json::json!({
        "format": "tardis_l2", "inputs": [{"path": tardis_csv.to_str().unwrap()}], "venue": "generic",
        "out": l2.to_str().unwrap(),
    })))
    .unwrap();
    let l2_ds = qt_ingest::Dataset::open(&l2).unwrap();
    write(
        "parquet",
        "l2_part_alone",
        0,
        &std::fs::read(&l2_ds.partitions()[0].files[0]).unwrap(),
    );
    std::fs::remove_dir_all(&tmp).unwrap();

    println!("seeds written to {}", root().join("seeds").display());
}
