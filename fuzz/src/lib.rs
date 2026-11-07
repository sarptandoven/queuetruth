//! Fuzz harnesses for the parsers of untrusted market-data input (SPEC 8.10: the service accepts uploaded files):
//! ITCH 5.0, IEX DEEP+, DBN, the etcsv and Parquet ingest formats, and the crypto capture formats.
//!
//! Each `pub fn` takes the raw fuzz input and drives one reader the way `qt_ingest::ingest` / `qt_crypto::prepare`
//! drive it, then checks oracles that hold for every input, not only "no panic":
//! - an error caused by input bytes is an input, config or data class error (`QT-INPUT-*`, `QT-CONFIG-*`,
//!   `QT-DATA-*`), never `QT-ENGINE-001` (reserved for internal faults) or `QT-USAGE-001`;
//! - binary layouts round-trip (`parse(encode(parse(b))) == parse(b)` for ITCH and DEEP+ messages);
//! - mapped records carry the documented flags and sequence numbers, and DEEP+ delivers each `(channel, session)`
//!   in strictly increasing sequence order;
//! - a dataset that an ingest path wrote opens again and every row decodes.
//!
//! The first input byte is a selector (price exponent, symbol filter, in-memory vs. file, format); the rest is the
//! payload, so one corpus explores the configuration space as well as the bytes. The `fuzz_targets/` binaries are
//! one-line wrappers, and `tests/replay.rs` runs every committed seed and regression input through the same
//! functions on the stable toolchain.

#![forbid(unsafe_code)]

use std::cell::{Cell, RefCell};
use std::io::{Cursor, Read};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use indexmap::IndexMap;
use qt_types::{MboEvent, QtError, QtResult, RuleSet, flags, rules_for};

/// A harness: one fuzz input in, panics on a finding.
pub type Harness = fn(&[u8]);

/// The fuzz targets by name, with their harness (used by `tests/replay.rs`).
pub const TARGETS: &[(&str, Harness)] = &[
    ("itch", itch),
    ("itch_parse", itch_parse),
    ("iex", iex),
    ("iex_parse", iex_parse),
    ("dbn", dbn),
    ("ingest", ingest),
    ("etcsv", etcsv),
    ("parquet", parquet),
    ("crypto_bitstamp", crypto_bitstamp),
    ("crypto_bitfinex", crypto_bitfinex),
    ("crypto_coinbase", crypto_coinbase),
    ("crypto_coinbase_level3", crypto_coinbase_level3),
    ("crypto_kraken_l3", crypto_kraken_l3),
    ("crypto_hyperliquid", crypto_hyperliquid),
    ("crypto_tardis", crypto_tardis),
    ("crypto_prepare", crypto_prepare),
    ("crypto_json", crypto_json),
];

/// Price exponents a selector picks from (`-12..=-4` is the range the ITCH and DEEP+ mappers accept).
const PRICE_EXPS: [i8; 4] = [-9, -4, -12, -6];

/// Panics unless `e` is an error that input bytes may legitimately cause: `QT-INPUT-*` (missing, malformed,
/// duplicate, units), `QT-CONFIG-*` (input that contradicts the spec) or `QT-DATA-*` (unusable data, strict verdict).
/// `QT-ENGINE-001` is reserved for internal faults and `QT-USAGE-001` for command-line misuse.
pub fn assert_input_error(e: &QtError) {
    let code = e.code();
    assert!(
        ["QT-INPUT-", "QT-CONFIG-", "QT-DATA-"]
            .iter()
            .any(|p| code.starts_with(p)),
        "input bytes caused {code}: {e}"
    );
}

/// `Ok(())` or an input-class error.
fn check<T>(r: &QtResult<T>) {
    if let Err(e) = r {
        assert_input_error(e);
    }
}

/// Root of the files the harnesses write: `$QT_FUZZ_TMP`, else `<repo>/.scratch/fuzz-tmp` (git-ignored; fixed at
/// compile time so that a bare `cargo fuzz run`, `cargo fuzz tmin` or `cargo test` never writes to the system temp
/// directory).
pub fn scratch_root() -> PathBuf {
    std::env::var_os("QT_FUZZ_TMP").map_or_else(
        || PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/../.scratch/fuzz-tmp")),
        PathBuf::from,
    )
}

thread_local! {
    static SCRATCH: RefCell<Option<PathBuf>> = const { RefCell::new(None) };
}

/// Distinguishes the scratch directories of the threads of one process.
static SCRATCH_SEQ: AtomicUsize = AtomicUsize::new(0);

/// This thread's scratch directory, `<scratch_root>/qt-fuzz-<pid>-<n>`: every libFuzzer worker process and every
/// test thread gets its own, so parallel `cargo test` threads never share input or output paths.
fn scratch() -> PathBuf {
    SCRATCH.with(|s| {
        s.borrow_mut()
            .get_or_insert_with(|| {
                let n = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
                let d = scratch_root().join(format!("qt-fuzz-{}-{n}", std::process::id()));
                std::fs::create_dir_all(&d).expect("create the fuzz scratch directory");
                d
            })
            .clone()
    })
}

/// Removes this thread's scratch directory (the next harness call on the thread creates a fresh one).
pub fn remove_scratch() {
    if let Some(d) = SCRATCH.with(|s| s.borrow_mut().take()) {
        let _ = std::fs::remove_dir_all(d);
    }
}

/// Writes `bytes` to `<scratch>/<name>` (overwriting) and returns the path.
fn put(name: &str, bytes: &[u8]) -> PathBuf {
    let p = scratch().join(name);
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent).expect("create a scratch subdirectory");
    }
    std::fs::write(&p, bytes).expect("write a scratch input");
    p
}

/// An empty `<scratch>/<name>` directory.
fn fresh_dir(name: &str) -> PathBuf {
    let d = scratch().join(name);
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).expect("create a scratch directory");
    d
}

/// Every row of every partition of a dataset (MBO or L2 records, then the partition's source trades).
fn read_dataset(ds: &qt_ingest::Dataset) -> QtResult<()> {
    for p in ds.partitions() {
        match p.kind {
            qt_ingest::DatasetKind::Mbo => {
                for e in p.mbo()? {
                    e?;
                }
            }
            qt_ingest::DatasetKind::L2 => {
                for r in p.l2()? {
                    r?;
                }
            }
        }
        p.source_trades()?;
    }
    Ok(())
}

/// What an ingest path wrote must open again and decode completely.
fn read_back(out: &Path) {
    let ds = qt_ingest::Dataset::open(out).expect("an ingested dataset opens");
    read_dataset(&ds).expect("an ingested dataset reads back");
}

/// The `LAST` flag sits on the last record of a non-empty output.
fn check_last(mbo: &[MboEvent]) {
    if let Some(l) = mbo.last() {
        assert_ne!(l.flags & flags::LAST, 0, "last record of a message without LAST: {l:?}");
    }
}

// ---- ITCH ----------------------------------------------------------------------------------------------------------

/// ITCH 5.0: `[selector][framed stream]`. Selector bits: 0-1 price exponent, 2 symbol filter, 3 read through a file
/// (`Reader::open`: gzip detected by magic and inflated behind the decompression guard), 4 midnight 0.
pub fn itch(data: &[u8]) {
    use qt_ingest::itch::{Mapper, Reader};
    let Some((&sel, body)) = data.split_first() else { return };
    let price_exp = PRICE_EXPS[usize::from(sel & 3)];
    let symbols = ["AAPL".to_owned(), "spy ".to_owned()];
    let filter = (sel & 4 != 0).then_some(&symbols[..]);
    // 2019-12-30 05:00 UTC (US/Eastern midnight) or 0.
    let midnight = if sel & 16 != 0 { 0 } else { 1_577_682_000_000_000_000 };
    let mut mapper = Mapper::new(midnight, filter, price_exp).expect("valid mapper config");
    let res = if sel & 8 != 0 {
        let path = put("12302019.NASDAQ_ITCH50", body);
        match Reader::open(&path) {
            Ok(mut r) => itch_drive(&mut r, &mut mapper),
            Err(e) => Err(e),
        }
    } else {
        itch_drive(&mut Reader::new(Cursor::new(body)), &mut mapper)
    };
    check(&res);
    let live = mapper.live_orders() as u64;
    let stats = mapper.finish();
    assert_eq!(stats.orders_live, live);
    assert!(stats.anomalies.total() <= stats.messages);
}

/// Parses one ITCH frame and checks the encode / parse round trip and the short-message report.
fn itch_message(bytes: &[u8]) -> Result<qt_ingest::itch::Message, qt_ingest::itch::ParseError> {
    use qt_ingest::itch::{ParseError, encode, message_len, parse};
    assert!(!bytes.is_empty(), "zero-length frames are skipped by the reader");
    let r = parse(bytes);
    match &r {
        Ok(msg) => {
            let mut enc = Vec::with_capacity(64);
            encode(msg, &mut enc);
            let back = parse(&enc[2..]).expect("an encoded message parses");
            assert_eq!(&back, msg, "ITCH encode/parse round trip");
        }
        Err(ParseError::Short { kind, got, want }) => {
            assert_eq!(message_len(*kind), Some(*want));
            assert!(got < want);
        }
        Err(ParseError::Empty) => unreachable!("the reader never returns an empty frame"),
    }
    r
}

fn itch_drive<R: Read>(r: &mut qt_ingest::itch::Reader<R>, mapper: &mut qt_ingest::itch::Mapper) -> QtResult<()> {
    use qt_ingest::itch::MapOutput;
    let mut out = MapOutput::default();
    while let Some(bytes) = r.next_message()? {
        let Ok(msg) = itch_message(bytes) else {
            mapper.note_short_message();
            continue;
        };
        let before = mapper.sequence();
        mapper.apply(&msg, &mut out)?;
        check_last(&out.mbo);
        if !out.mbo.is_empty() {
            assert_eq!(mapper.sequence(), before + 1, "one sequence per message with records");
        }
        for e in &out.mbo {
            assert_eq!(e.sequence, mapper.sequence());
            assert!(e.ts_event >= 0 && e.ts_event == e.ts_recv);
        }
        for t in &out.trades {
            assert_eq!(t.sequence, mapper.sequence());
        }
    }
    // A clean end of stream stays clean.
    assert!(r.next_message()?.is_none());
    Ok(())
}

/// ITCH 5.0 framing and message layouts alone (no `Mapper`, in memory): `[selector][framed stream]`, the layout of
/// [`itch`] with the selector ignored, so the two targets share seeds. Runs at several thousand inputs per second
/// where [`itch`] spends most of each input setting up the mapper's 64 Ki-locate tables.
pub fn itch_parse(data: &[u8]) {
    use qt_ingest::itch::Reader;
    let Some((_, body)) = data.split_first() else { return };
    let mut r = Reader::new(Cursor::new(body));
    let res: QtResult<()> = (|| {
        let mut frames = 0u64;
        while let Some(bytes) = r.next_message()? {
            let _ = itch_message(bytes);
            frames += 1;
        }
        assert!(r.next_message()?.is_none());
        assert!(frames <= body.len() as u64 / 3, "every frame costs at least 3 bytes");
        Ok(())
    })();
    check(&res);
}

// ---- IEX DEEP+ -----------------------------------------------------------------------------------------------------

/// IEX DEEP+ capture: `[selector][pcap | pcapng, raw or gzip]`. Selector bits: 0-1 price exponent, 2 symbol filter,
/// 3 read through a file (`CaptureReader::open`, gzip detected by magic).
pub fn iex(data: &[u8]) {
    use qt_ingest::guard::GuardLimits;
    use qt_ingest::iex::{CaptureReader, FeedReader, Mapper};
    let Some((&sel, body)) = data.split_first() else { return };
    let price_exp = PRICE_EXPS[usize::from(sel & 3)];
    let symbols = ["SPY".to_owned(), "zvzzt".to_owned()];
    let filter = (sel & 4 != 0).then_some(&symbols[..]);
    let mut mapper = Mapper::new(filter, price_exp).expect("valid mapper config");
    let res = if sel & 8 != 0 {
        let path = put("data_feeds_20261009_DPLS.pcap.gz", body);
        match CaptureReader::open(&path, GuardLimits::default()) {
            Ok(cap) => iex_drive(&mut FeedReader::new(cap), Some(&mut mapper)),
            Err(e) => Err(e),
        }
    } else {
        iex_drive(
            &mut FeedReader::new(CaptureReader::new(Cursor::new(body))),
            Some(&mut mapper),
        )
    };
    check(&res);
    let live = mapper.live_orders() as u64;
    let stats = mapper.finish();
    assert_eq!(stats.orders_live, live);
}

/// IEX DEEP+ capture framing, IEX-TP sequencing and message layouts alone (no `Mapper`, in memory):
/// `[selector][pcap | pcapng]`, the layout of [`iex`] with the selector ignored (raw captures only; gzip goes through
/// the file path of [`iex`]).
pub fn iex_parse(data: &[u8]) {
    use qt_ingest::iex::{CaptureReader, FeedReader};
    let Some((_, body)) = data.split_first() else { return };
    check(&iex_drive(
        &mut FeedReader::new(CaptureReader::new(Cursor::new(body))),
        None,
    ));
}

fn iex_drive<R: Read>(
    feed: &mut qt_ingest::iex::FeedReader<R>,
    mut mapper: Option<&mut qt_ingest::iex::Mapper>,
) -> QtResult<()> {
    use qt_ingest::iex::message::encode;
    use qt_ingest::iex::{FeedEvent, MapOutput, Message, MsgCtx, ParseError, parse};
    use std::collections::HashMap;
    let mut out = MapOutput::default();
    let mut last: HashMap<(u32, u32), Option<u64>> = HashMap::new();
    let mut enc = Vec::with_capacity(64);
    let mut delivered = 0u64;
    let res = feed.for_each(|ev| {
        match ev {
            FeedEvent::NewSession { channel, session, .. } => {
                assert!(!last.contains_key(&(channel, session)), "a session opens once");
                last.insert((channel, session), None);
            }
            FeedEvent::Message(m) => {
                delivered += 1;
                let prev = last
                    .get_mut(&(m.channel, m.session))
                    .expect("message of an opened session");
                assert!(
                    prev.is_none_or(|p| m.seq > p),
                    "sequence {} after {prev:?} delivered",
                    m.seq
                );
                *prev = Some(m.seq);
                match parse(m.data) {
                    Ok(msg) => {
                        if !matches!(msg, Message::Unknown { .. }) {
                            enc.clear();
                            encode(&msg, &mut enc);
                            assert_eq!(parse(&enc), Ok(msg), "DEEP+ encode/parse round trip");
                        }
                        let Some(mapper) = mapper.as_deref_mut() else {
                            return Ok(());
                        };
                        let ctx = MsgCtx {
                            seq: m.seq,
                            channel: u16::try_from(m.channel).unwrap_or(u16::MAX),
                            send_time: m.send_time,
                        };
                        mapper.apply(ctx, &msg, &mut out)?;
                        check_last(&out.mbo);
                        for e in &out.mbo {
                            assert_eq!(e.sequence, m.seq);
                            assert!(e.ts_recv >= e.ts_event, "ts_recv is never before ts_event");
                        }
                    }
                    Err(e) => {
                        if let Some(mapper) = mapper.as_deref_mut() {
                            mapper.note_unparsed(matches!(e, ParseError::Short { .. }));
                        }
                    }
                }
            }
        }
        Ok(())
    });
    if res.is_ok() {
        assert_eq!(feed.stats.messages, delivered, "every counted message was delivered");
    }
    res
}

// ---- DBN -----------------------------------------------------------------------------------------------------------

/// DBN (plain or zstd, inferred from content): `[selector][file]`. Selector bits 0-1: price exponent. Reads the file
/// as `ingest_dbn` does for its schema (definitions, MBO or trades) and resolves the dated symbology.
///
/// The two faults of the pinned `dbn` 0.71.0 decoder this target found (a panic on 100 to 103 metadata bytes and an
/// up-front allocation of the announced metadata length) are kept out by `qt_ingest::dbn::FrameCheck` (fixed after
/// 0.2.0); their reproducers are in `regressions/dbn/`.
pub fn dbn(data: &[u8]) {
    use ::dbn::Schema;
    use qt_ingest::dbn::{DbnReader, Symbology, symbol_map, utc_day, vintage_of};
    let Some((&sel, body)) = data.split_first() else { return };
    let price_exp = [-9i8, -4, -12, 0][usize::from(sel & 3)];
    let path = put("input.dbn", body);
    let mut r = match DbnReader::open(&path) {
        Ok(r) => r,
        Err(e) => return assert_input_error(&e),
    };
    let md = r.metadata().clone();
    let _ = vintage_of(&md);
    let _ = symbol_map(&md);
    let mut sym = Symbology::new();
    sym.add_metadata(&md);
    let rules = rules_for(RuleSet::Generic);
    let mut ids: Vec<(u32, i64)> = Vec::new();
    let res: QtResult<()> = (|| {
        match md.schema {
            Some(Schema::Definition) => {
                while let Some((iid, s, mpi, ts_recv)) = r.next_definition()? {
                    sym.add_definition(iid, &s, mpi, ts_recv);
                    ids.push((iid, i64::try_from(ts_recv).unwrap_or(i64::MAX)));
                }
            }
            Some(Schema::Trades) => {
                while let Some(t) = r.next_trade(price_exp, &rules)? {
                    ids.push((t.instrument_id, t.ts_event));
                }
            }
            _ => {
                while let Some(e) = r.next_mbo(price_exp)? {
                    assert!(e.ts_event >= 0 && e.ts_recv >= 0);
                    ids.push((e.instrument_id, e.ts_recv));
                }
            }
        }
        Ok(())
    })();
    check(&res);
    for (iid, ts) in ids.into_iter().take(64) {
        let day = utc_day(ts);
        let name = sym.symbol_at(iid, day).map(str::to_owned);
        if let Some(n) = &name {
            let _ = sym.id_at(n, day);
        }
        let _ = sym.min_price_increment(iid, name.as_deref(), day);
    }
}

// ---- whole ingest --------------------------------------------------------------------------------------------------

/// `qt_ingest::ingest` end to end (reader thread, routing, data quality, Parquet writer):
/// `[selector][input file]`. Selector bits 0-1: format (ITCH, IEX DEEP+, DBN, ITCH with a symbol filter).
pub fn ingest(data: &[u8]) {
    let Some((&sel, body)) = data.split_first() else { return };
    let (format, venue, name, extra) = match sel & 3 {
        0 => ("itch", "nasdaq", "12302019.NASDAQ_ITCH50", serde_json::json!({})),
        1 => ("iex_deep_plus", "iex", "deep.pcap", serde_json::json!({})),
        2 => ("dbn", "generic", "input.dbn", serde_json::json!({})),
        _ => (
            "itch",
            "nasdaq",
            "12302019.NASDAQ_ITCH50",
            serde_json::json!({"symbols": ["AAPL"]}),
        ),
    };
    let input = put(&format!("ingest/{name}"), body);
    let out = fresh_dir("ingest-out");
    let mut v = serde_json::json!({
        "format": format,
        "inputs": [{"path": input.to_str().expect("utf-8 scratch path")}],
        "venue": venue,
        "out": out.to_str().expect("utf-8 scratch path"),
    });
    if let (Some(o), Some(e)) = (v.as_object_mut(), extra.as_object()) {
        o.extend(e.clone());
    }
    let spec: qt_schema::specs::IngestSpec = serde_json::from_value(v).expect("valid ingest spec");
    match qt_ingest::ingest(&spec) {
        Ok(_) => read_back(&out),
        Err(e) => assert_input_error(&e),
    }
}

// ---- etcsv --------------------------------------------------------------------------------------------------------

/// Separator between the files of an [`etcsv`] input.
pub const ETCSV_SEP: &[u8] = b"\n#--\n";

/// 2019-12-30 00:00 US/Eastern as UTC ns (the session of the embedded etcsv day).
const ETCSV_MIDNIGHT: i64 = 1_577_682_000_000_000_000;

/// A small etcsv day (two instruments, adds, a hidden print, a fill, a cancel), the layout `crates/ingest` tests use:
/// the seed of the [`etcsv`] target and the source of the [`parquet`] target's template dataset.
pub const ETCSV_MBO: &str = "\
ts_event,ts_recv,instrument_id,action,side,price,size,order_id,flags,sequence,channel_id
1577716200000000001,1577716200000000001,13,A,B,185000000000,100,1,128,1,0
1577716200000000002,1577716200000000002,5291,A,A,400000000000,5000000000,2,128,2,0
1577716200000000003,1577716200000000003,13,T,A,185000000000,10,0,0,3,0
1577716200000000003,1577716200000000003,13,F,B,185000000000,10,1,0,3,0
1577716200000000003,1577716200000000003,13,C,B,185000000000,10,1,128,3,0
1577716200000000004,1577716200000000004,5291,T,N,400010000000,70,0,128,4,0
";

/// Trade prints of [`ETCSV_MBO`]'s day.
pub const ETCSV_TRADES: &str = "\
ts_event,instrument_id,price,size,aggressor_side,sequence
1577716200000000003,13,185000000000,10,A,3
1577716200000000004,5291,400010000000,70,N,4
";

/// Instruments of [`ETCSV_MBO`]'s day (ITCH stock-directory columns).
pub const ETCSV_INSTRUMENTS: &str = "\
instrument_id,raw_symbol,market_category,financial_status,round_lot_size,round_lots_only,issue_classification,\
issue_sub_type,authenticity,short_sale_threshold,ipo_flag,luld_tier,etp_flag,etp_leverage_factor,inverse_indicator
13,AAPL,Q,N,100,N,C,Z,P,N,N,1,N,0,N
5291,MSFT,Q,N,100,N,C,Z,P,N,N,1,N,0,N
";

/// The etcsv ingest format end to end (`MboCsvReader`, trades and instruments CSV, routing, Parquet writer), then
/// `Dataset::open` and a full read: `[selector][mbo.csv][ETCSV_SEP][trades.csv][ETCSV_SEP][instruments.csv]` (the
/// last two optional). Selector bits: 0 fixed `midnight_utc_ns`, 1 instruments through the spec's `instruments`
/// instead of `inputs`, 2 `tz` America/New_York, 3 `price_exp` -4, 4 symbol filter.
pub fn etcsv(data: &[u8]) {
    let Some((&sel, body)) = data.split_first() else { return };
    let dir = fresh_dir("etcsv");
    let mut parts = split_on(body, ETCSV_SEP).into_iter();
    let mut inputs = Vec::new();
    let mut instruments = None;
    for (i, name) in ["mbo.csv", "trades.csv", "instruments.csv"].iter().enumerate() {
        let Some(bytes) = parts.next() else { break };
        let p = dir.join(name);
        std::fs::write(&p, bytes).expect("write a scratch input");
        let r = serde_json::json!({"path": p.to_str().expect("utf-8 scratch path")});
        if i == 2 && sel & 2 != 0 {
            instruments = Some(r);
        } else {
            inputs.push(r);
        }
    }
    let out = fresh_dir("etcsv-out");
    let mut v = serde_json::json!({
        "format": "etcsv",
        "inputs": inputs,
        "venue": "nasdaq",
        "out": out.to_str().expect("utf-8 scratch path"),
    });
    if let Some(r) = instruments {
        v["instruments"] = r;
    }
    if sel & 1 != 0 {
        v["midnight_utc_ns"] = ETCSV_MIDNIGHT.into();
    }
    if sel & 4 != 0 {
        v["tz"] = "America/New_York".into();
    }
    if sel & 8 != 0 {
        v["price_exp"] = (-4).into();
    }
    if sel & 16 != 0 {
        v["symbols"] = serde_json::json!(["aapl"]);
    }
    let spec: qt_schema::specs::IngestSpec = serde_json::from_value(v).expect("valid ingest spec");
    match qt_ingest::ingest(&spec) {
        Ok(_) => read_back(&out),
        Err(e) => assert_input_error(&e),
    }
}

/// `body` split on every occurrence of `sep`.
fn split_on<'a>(mut body: &'a [u8], sep: &[u8]) -> Vec<&'a [u8]> {
    let mut v = Vec::new();
    while let Some(i) = body.windows(sep.len()).position(|w| w == sep) {
        v.push(&body[..i]);
        body = &body[i + sep.len()..];
    }
    v.push(body);
    v
}

// ---- Parquet -------------------------------------------------------------------------------------------------------

/// The roles a [`parquet`] input plays (selector modulo 5).
const PARQUET_ROLES: u8 = 5;

/// The Parquet ingest format and the dataset reader on untrusted Parquet bytes: `[selector][file]`. The selector
/// (modulo 5) picks the file's role:
/// 0. the only input of `ingest` (format parquet);
/// 1. the spec's `instruments` file, beside a valid MBO partition file as input;
/// 2. the MBO partition file of an otherwise valid dataset (the template, ingested from [`ETCSV_MBO`]'s day);
/// 3. that dataset's `instruments.parquet`;
/// 4. that dataset's `trades_source` partition file.
///
/// Roles 2 to 4 first read the dataset directly (`Dataset::open`, every partition, source trades), then ingest it.
/// Every error must be input-class, and a dataset `ingest` wrote must read back.
///
/// Panics raised inside the `parquet` or `arrow` crates (corrupt footers, schemas or pages they do not bound) are a
/// reported finding (`known-crashes/parquet/`); this harness skips them so the target can look for other faults, and
/// [`parquet_unfiltered`] runs them. A panic anywhere else still fails.
pub fn parquet(data: &[u8]) {
    let _ = parquet_known_panic(data);
}

/// Runs [`parquet_unfiltered`] and returns whether it panicked inside the `parquet` or `arrow` crates (a known
/// finding, swallowed); any other panic propagates.
///
/// The first swallowed panic at each source location of a process is reported on stderr
/// (`qt-fuzz: known parquet panic at <file:line:col>: <message>`); with `$QT_FUZZ_KNOWN_DIR` set, its input is also
/// saved there as `parquet-<location>`, so a fuzzing run yields one reproducer per distinct panic site.
pub fn parquet_known_panic(data: &[u8]) -> bool {
    install_dependency_panic_filter();
    IN_FILTER.with(|f| f.set(true));
    DEPENDENCY_PANIC.with(|f| f.set(None));
    let r = std::panic::catch_unwind(|| parquet_unfiltered(data));
    IN_FILTER.with(|f| f.set(false));
    let Err(payload) = r else { return false };
    let Some(location) = DEPENDENCY_PANIC.with(Cell::take) else {
        std::panic::resume_unwind(payload)
    };
    static SEEN: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());
    let mut seen = SEEN.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    if !seen.contains(&location) {
        let msg = payload
            .downcast_ref::<&str>()
            .map(|s| (*s).to_owned())
            .or_else(|| payload.downcast_ref::<String>().cloned())
            .unwrap_or_default();
        eprintln!("qt-fuzz: known parquet panic at {location}: {msg}");
        if let Some(dir) = std::env::var_os("QT_FUZZ_KNOWN_DIR") {
            // `<crate>-<version>/src/<file>:<line>:<col>`, made a file name.
            let at = PARQUET_PANIC_CRATES
                .iter()
                .filter_map(|c| location.rfind(c))
                .max()
                .map_or(0, |i| i + 1);
            let name: String = location[at..]
                .chars()
                .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
                .collect();
            let dir = PathBuf::from(dir);
            let _ = std::fs::create_dir_all(&dir);
            let _ = std::fs::write(dir.join(format!("parquet-{name}")), data);
        }
        seen.push(location);
    }
    true
}

/// Source directories of the crates whose panics on corrupt Parquet are the known finding (cargo registry layout:
/// `.../parquet-<version>/src/...`, `.../arrow-<component>-<version>/src/...`).
const PARQUET_PANIC_CRATES: [&str; 2] = ["/parquet-", "/arrow-"];

thread_local! {
    /// Set while [`parquet_known_panic`] runs on this thread.
    static IN_FILTER: Cell<bool> = const { Cell::new(false) };
    /// The source location (`file:line:col`) of a panic inside [`PARQUET_PANIC_CRATES`] swallowed on this thread.
    static DEPENDENCY_PANIC: Cell<Option<String>> = const { Cell::new(None) };
}

/// Wraps the current panic hook (libFuzzer's abort hook under `cargo fuzz`, the default hook in tests) so that a
/// panic located in [`PARQUET_PANIC_CRATES`] while [`parquet_known_panic`] runs on that thread only sets
/// `DEPENDENCY_PANIC` and unwinds. Every other panic goes to the previous hook unchanged.
fn install_dependency_panic_filter() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let prev = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            let known = info
                .location()
                .filter(|l| IN_FILTER.with(Cell::get) && PARQUET_PANIC_CRATES.iter().any(|c| l.file().contains(c)));
            match known {
                Some(l) => DEPENDENCY_PANIC.with(|f| f.set(Some(l.to_string()))),
                None => prev(info),
            }
        }));
    });
}

/// [`parquet`] without the known-panic filter.
pub fn parquet_unfiltered(data: &[u8]) {
    let Some((&sel, body)) = data.split_first() else { return };
    let role = sel % PARQUET_ROLES;
    let t = parquet_template();
    let out = fresh_dir("parquet-out");
    let mut v = serde_json::json!({
        "format": "parquet",
        "venue": "nasdaq",
        "out": out.to_str().expect("utf-8 scratch path"),
    });
    let path = |p: &Path| serde_json::json!({"path": p.to_str().expect("utf-8 scratch path")});
    match role {
        0 => v["inputs"] = serde_json::json!([path(&put("parquet/in.parquet", body))]),
        1 => {
            v["inputs"] = serde_json::json!([path(&t.root.join(&t.mbo))]);
            v["instruments"] = path(&put("parquet/instruments.parquet", body));
        }
        _ => {
            let ds = fresh_dir("parquet-ds");
            copy_dir(&t.root, &ds);
            let target = match role {
                2 => &t.mbo,
                3 => &t.instruments,
                _ => &t.trades,
            };
            std::fs::write(ds.join(target), body).expect("write a scratch input");
            match qt_ingest::Dataset::open(&ds) {
                Ok(d) => check(&read_dataset(&d)),
                Err(e) => assert_input_error(&e),
            }
            v["inputs"] = serde_json::json!([path(&ds)]);
        }
    }
    let spec: qt_schema::specs::IngestSpec = serde_json::from_value(v).expect("valid ingest spec");
    match qt_ingest::ingest(&spec) {
        Ok(_) => read_back(&out),
        Err(e) => assert_input_error(&e),
    }
}

/// The template dataset of [`parquet`]: its root and the relative paths of its files.
struct Template {
    root: PathBuf,
    mbo: PathBuf,
    instruments: PathBuf,
    trades: PathBuf,
}

/// Ingests [`ETCSV_MBO`]'s day into `<scratch>/parquet-template` once per scratch directory.
fn parquet_template() -> Template {
    let root = scratch().join("parquet-template");
    let mbo = PathBuf::from("mbo/date=2019-12-30/instrument_id=13/part-00000.parquet");
    let trades = PathBuf::from("trades_source/date=2019-12-30/instrument_id=13/part-00000.parquet");
    let t = Template {
        instruments: PathBuf::from("instruments.parquet"),
        root,
        mbo,
        trades,
    };
    if !t.root.join("manifest.json").is_file() {
        let src = fresh_dir("parquet-template-src");
        let files = [
            ("mbo.csv", ETCSV_MBO),
            ("trades.csv", ETCSV_TRADES),
            ("instruments.csv", ETCSV_INSTRUMENTS),
        ];
        let inputs: Vec<_> = files
            .iter()
            .map(|(name, text)| {
                let p = src.join(name);
                std::fs::write(&p, text).expect("write a template input");
                serde_json::json!({"path": p.to_str().expect("utf-8 scratch path")})
            })
            .collect();
        let _ = std::fs::remove_dir_all(&t.root);
        let spec: qt_schema::specs::IngestSpec = serde_json::from_value(serde_json::json!({
            "format": "etcsv",
            "inputs": inputs,
            "venue": "nasdaq",
            "midnight_utc_ns": ETCSV_MIDNIGHT,
            "out": t.root.to_str().expect("utf-8 scratch path"),
        }))
        .expect("valid ingest spec");
        qt_ingest::ingest(&spec).expect("the template day ingests");
        for f in [&t.mbo, &t.instruments, &t.trades] {
            assert!(t.root.join(f).is_file(), "template file {}", f.display());
        }
    }
    t
}

/// Copies the directory tree `from` into the existing directory `to`.
fn copy_dir(from: &Path, to: &Path) {
    for e in std::fs::read_dir(from).expect("read a template directory") {
        let e = e.expect("read a template directory entry");
        let (src, dst) = (e.path(), to.join(e.file_name()));
        if src.is_dir() {
            std::fs::create_dir_all(&dst).expect("create a scratch directory");
            copy_dir(&src, &dst);
        } else {
            std::fs::copy(&src, &dst).expect("copy a template file");
        }
    }
}

// ---- crypto converters ---------------------------------------------------------------------------------------------

/// Lines of a capture as `qt_crypto::input::FileLines` yields them (`\n` split, `\r` stripped, invalid UTF-8 is an
/// error).
fn lines(body: &[u8]) -> impl Iterator<Item = QtResult<String>> + '_ {
    let body = body.strip_suffix(b"\n").unwrap_or(body);
    let mut done = body.is_empty();
    body.split(|&b| b == b'\n').map_while(move |l| {
        if done {
            return None;
        }
        let l = l.strip_suffix(b"\r").unwrap_or(l);
        Some(match String::from_utf8(l.to_vec()) {
            Ok(s) => Ok(s),
            Err(_) => {
                done = true;
                Err(QtError::InputFormat {
                    location: "fuzz input".into(),
                    msg: "invalid UTF-8".into(),
                })
            }
        })
    })
}

/// Instrument map variants: empty (every symbol seen) or a fixed list.
fn instruments(sel: u8, names: &[&str]) -> IndexMap<String, u32> {
    if sel & 4 == 0 {
        return IndexMap::new();
    }
    names
        .iter()
        .enumerate()
        .map(|(i, s)| ((*s).to_owned(), i as u32 + 1))
        .collect()
}

/// `(price_exp, size_exp)` pairs a selector picks from.
fn exps(sel: u8) -> (i8, i8) {
    [(-9, -8), (-12, -18), (0, 0), (-4, -2)][usize::from(sel & 3)]
}

/// Records of an L3 converter: feed order is kept per instrument, flags are valid.
fn check_l3(r: &QtResult<(Vec<MboEvent>, Vec<qt_schema::rows::TradeRow>, qt_crypto::Counters)>) {
    check(r);
    if let Ok((ev, _, _)) = r {
        for e in ev {
            assert!(
                matches!(e.action, b'A' | b'C' | b'M' | b'R' | b'T' | b'F' | b'N'),
                "{e:?}"
            );
            assert!(matches!(e.side, b'A' | b'B' | b'N'), "{e:?}");
        }
    }
}

/// Bitstamp `live_orders` / `live_trades` / `detail_order_book` lines. Selector: exponents, instrument map, taker
/// window (bits 3-4: default, none, 0, 1 s).
pub fn crypto_bitstamp(data: &[u8]) {
    use qt_crypto::bitstamp::{Config, convert};
    let Some((&sel, body)) = data.split_first() else { return };
    let (price_exp, size_exp) = exps(sel);
    let mut cfg = Config {
        instruments: instruments(sel, &["btcusd", "ethusd"]),
        price_exp,
        size_exp,
        ..Config::default()
    };
    cfg.taker_window_us = match (sel >> 3) & 3 {
        0 => cfg.taker_window_us,
        1 => None,
        2 => Some(0),
        _ => Some(1_000_000),
    };
    check_l3(&convert(lines(body), &cfg));
}

/// Bitfinex raw-book frames. Selector: exponents, instrument map, conf flags, `max_size` chunking.
pub fn crypto_bitfinex(data: &[u8]) {
    use qt_crypto::bitfinex::{Config, convert};
    let Some((&sel, body)) = data.split_first() else { return };
    let (price_exp, size_exp) = exps(sel);
    let cfg = Config {
        instruments: instruments(sel, &["tBTCUSD", "tETHUSD"]),
        price_exp,
        size_exp,
        flags: if sel & 8 != 0 { 0 } else { 65_536 | 32_768 },
        max_size: if sel & 16 != 0 { 3 } else { u64::MAX },
    };
    check_l3(&convert(lines(body), &cfg));
}

/// Coinbase `full` channel (with `level2` warm-up). Selector: exponents, instrument map, warm-up depth.
pub fn crypto_coinbase(data: &[u8]) {
    use qt_crypto::coinbase::{Config, convert};
    let Some((&sel, body)) = data.split_first() else { return };
    let (price_exp, size_exp) = exps(sel);
    let cfg = Config {
        instruments: instruments(sel, &["BTC-USD", "ETH-USD"]),
        price_exp,
        size_exp,
        warmup_depth: [10, 0, 1, 1000][usize::from((sel >> 3) & 3)],
    };
    check_l3(&convert(lines(body), &cfg));
}

/// Coinbase Exchange `level3` channel. Selector: exponents, instrument map.
pub fn crypto_coinbase_level3(data: &[u8]) {
    use qt_crypto::coinbase_l3::{Config, convert};
    let Some((&sel, body)) = data.split_first() else { return };
    let (price_exp, size_exp) = exps(sel);
    let cfg = Config {
        instruments: instruments(sel, &["BTC-USD", "ETH-USD"]),
        price_exp,
        size_exp,
    };
    check_l3(&convert(lines(body), &cfg));
}

/// Kraken WebSocket v2 `level3`. Selector: exponents, instrument map, depth override, checksum precision.
pub fn crypto_kraken_l3(data: &[u8]) {
    use qt_crypto::kraken_l3::{Config, convert};
    let Some((&sel, body)) = data.split_first() else { return };
    let (price_exp, size_exp) = exps(sel);
    let mut cfg = Config {
        instruments: instruments(sel, &["BTC/USD", "ETH/USD"]),
        price_exp,
        size_exp,
        depth: [None, Some(10), Some(1000), Some(7)][usize::from((sel >> 3) & 3)],
        ..Config::default()
    };
    if sel & 32 != 0 {
        cfg.precision.insert("BTC/USD".into(), (1, 8));
        cfg.precision.insert("ETH/USD".into(), (40, 40));
    }
    check_l3(&convert(lines(body), &cfg));
}

/// Separator between Hyperliquid node lines and an L4 snapshot in one fuzz input.
pub const HL_SNAPSHOT_SEP: &[u8] = b"\n#L4SNAPSHOT\n";

/// Hyperliquid node outputs: `[selector][lines][HL_SNAPSHOT_SEP][L4 snapshot at height 1000]`. Selector: exponents,
/// instrument map, block-gap check.
pub fn crypto_hyperliquid(data: &[u8]) {
    use qt_crypto::hyperliquid::{Config, convert};
    let Some((&sel, body)) = data.split_first() else { return };
    let (price_exp, size_exp) = exps(sel);
    let (lines_part, snap) = match body.windows(HL_SNAPSHOT_SEP.len()).position(|w| w == HL_SNAPSHOT_SEP) {
        Some(i) => (&body[..i], Some(&body[i + HL_SNAPSHOT_SEP.len()..])),
        None => (body, None),
    };
    let cfg = Config {
        instruments: instruments(sel, &["BTC", "@107"]),
        price_exp,
        size_exp,
        snapshots: snap
            .map(|s| vec![(1000, String::from_utf8_lossy(s).into_owned())])
            .unwrap_or_default(),
        max_block_gap_ms: if sel & 8 != 0 { 1 } else { 0 },
    };
    check_l3(&convert(lines(lines_part), &cfg));
}

/// Tardis `incremental_book_L2` / `trades` CSV. Selector: exponents, instrument map.
pub fn crypto_tardis(data: &[u8]) {
    use qt_crypto::tardis::{Config, convert};
    let Some((&sel, body)) = data.split_first() else { return };
    let (price_exp, size_exp) = exps(sel);
    let cfg = Config {
        instruments: instruments(sel, &["BTCUSDT", "ETHUSDT"]),
        price_exp,
        size_exp,
    };
    check(&convert(lines(body), &cfg));
}

/// `qt_crypto::prepare` on one capture file (input resolution, line reading with the gzip guard, conversion,
/// session-date cuts and re-seeding, instruments, data quality): `[selector][file]`. Selector bits 0-2: format;
/// bit 3: `tz` America/New_York instead of UTC; bit 4: strict; bit 5: also `qt_crypto::write` the prepared data
/// (the rest of `qt_crypto::ingest`, the path the service runs) and read the dataset back.
pub fn crypto_prepare(data: &[u8]) {
    let Some((&sel, body)) = data.split_first() else { return };
    let (format, file) = [
        ("bitstamp", "live_orders.jsonl"),
        ("bitfinex", "book_R0.jsonl"),
        ("coinbase", "full.jsonl"),
        ("coinbase_level3", "level3.jsonl"),
        ("kraken_l3", "level3.jsonl"),
        ("hyperliquid_node", "node_raw_book_diffs_by_block.jsonl"),
        ("tardis_l2", "incremental_book_L2.csv"),
        ("tardis_l2", "trades.csv"),
    ][usize::from(sel & 7)];
    let dir = fresh_dir("prepare");
    let input = dir.join(file);
    std::fs::write(&input, body).expect("write a scratch input");
    let mut v = serde_json::json!({
        "format": format,
        "inputs": [{"path": input.to_str().expect("utf-8 scratch path")}],
        "venue": "generic",
        "strict": sel & 16 != 0,
    });
    if sel & 8 != 0 {
        v["tz"] = "America/New_York".into();
    }
    let spec: qt_schema::specs::IngestSpec = serde_json::from_value(v).expect("valid ingest spec");
    match qt_crypto::prepare(&spec) {
        Ok(p) => {
            for w in p.instruments.windows(2) {
                assert!(
                    (w[0].session_date, w[0].instrument_id) < (w[1].session_date, w[1].instrument_id),
                    "instrument rows sorted and unique"
                );
            }
            if sel & 32 != 0 {
                let out = fresh_dir("prepare-out");
                match qt_crypto::write(&p, &out) {
                    Ok(_) => read_back(&out),
                    Err(e) => assert_input_error(&e),
                }
            }
        }
        Err(e) => assert_input_error(&e),
    }
}

/// The exact-decimal JSON reader and the unit / date / time parsers every crypto converter uses:
/// `[selector][text]`. Selector bits 0-1: exponents.
pub fn crypto_json(data: &[u8]) {
    use qt_crypto::{jsonx, units};
    let Some((&sel, body)) = data.split_first() else { return };
    let Ok(text) = std::str::from_utf8(body) else { return };
    let (price_exp, size_exp) = exps(sel);
    if let Ok(v) = jsonx::parse(text) {
        let mut stack = vec![&v];
        while let Some(j) = stack.pop() {
            let _ = (j.as_i128(), j.as_i64(), j.as_u64(), j.is_null(), j.get("type"));
            if let Some(t) = j.num_text() {
                let _ = units::price(t, price_exp);
                let _ = units::size(t, size_exp);
                let _ = units::signed_size(t, size_exp);
                let _ = units::is_zero(t);
                for exp in [-18, -9, 0, 18] {
                    let _ = units::dec_units(t, exp);
                }
            }
            match j {
                jsonx::J::Arr(a) => stack.extend(a.iter()),
                jsonx::J::Obj(m) => stack.extend(m.iter().map(|(_, x)| x)),
                _ => {}
            }
        }
    }
    let line = text.lines().next().unwrap_or("");
    if let Ok(d) = units::parse_date(line) {
        let (y, m, dd) = units::civil_from_days(i64::from(d));
        assert_eq!(units::days_from_civil(y, m, dd), i64::from(d), "civil date round trip");
    }
    if let Ok(ts) = units::iso_ns(line) {
        check_session_day(ts);
    }
    if let Ok(n) = line.trim().parse::<i64>() {
        check_session_day(n);
        for unit in [1, 1_000, 1_000_000, 1_000_000_000] {
            if let Ok(ts) = units::scaled_ns(n, unit, "fuzz") {
                check_session_day(ts);
            }
        }
    }
    let _ = qt_crypto::l3util::crc32(body);
}

/// `session_date_of` puts `ts` in the day whose (representable) midnight is at or before it and whose next midnight
/// (when representable) is after it, in both session clocks: compared exactly, over the whole `i64` range.
fn check_session_day(ts: i64) {
    use qt_crypto::units::{SessionClock, checked_midnight_ns, session_date_of};
    for clock in [SessionClock::Utc, SessionClock::Eastern] {
        let d = session_date_of(clock, ts);
        let next = d.checked_add(1).and_then(|n| checked_midnight_ns(clock, n));
        // A day is far shorter than the i64 range: at most one of its bounding midnights is unrepresentable.
        assert!(
            checked_midnight_ns(clock, d).is_some() || next.is_some(),
            "{clock:?}: day {d} of {ts} has no representable midnight"
        );
        assert!(
            checked_midnight_ns(clock, d).is_none_or(|m| m <= ts),
            "{clock:?}: {ts} before the midnight of its day {d}"
        );
        assert!(
            next.is_none_or(|m| ts < m),
            "{clock:?}: {ts} at or after the midnight after its day {d}"
        );
    }
}
