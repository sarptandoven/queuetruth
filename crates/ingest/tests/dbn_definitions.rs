//! DBN instrument definitions and symbology (all DBN files are encoded in-process with the pinned `dbn` crate; the
//! opt-in test at the end reads Databento's own definition test files):
//! (a) definition files of DBN v1, v2 and v3 supply the tick, so a CME ingest without `tick_size` takes no default
//!     tick (no "can effectively hang" warning, no `instruments_default_tick_size`);
//! (b) definition and symbol-mapping records inside a mixed-schema file (a live capture) supply tick and symbol;
//! (c) parent and continuous symbology: metadata keyed by `ES.FUT` / `ES.c.0` names no instrument, definitions do,
//!     and the symbol filter accepts both the raw symbol and the alias;
//! (d) a definition whose match algorithm is not FIFO is rejected at ingest (QT-CONFIG-001); deletes, undefined
//!     ticks and undated records are counted, not applied;
//! (e) Databento's 2026-10-31 XNAS.ITCH re-issue: each vintage (stock-locate ids, or new ids with the locate in
//!     `raw_instrument_id`, midnight snapshots and after-hours to 01:00 UTC) ingests to the same symbols and ticks;
//!     a mix of the two vintages, or of two datasets, is QT-INPUT-002.

use std::ffi::c_char;
use std::num::NonZeroU64;
use std::path::{Path, PathBuf};

use ::dbn::encode::EncodeRecord;
use ::dbn::encode::dbn::Encoder;
use ::dbn::{
    ErrorMsg, FlagSet, InstrumentDefMsg, MappingInterval, MboMsg, Metadata, RecordHeader, SType, Schema, SymbolMapping,
    SymbolMappingMsg, SystemCode, SystemMsg, rtype,
};
use qt_ingest::dbn::{DbnDefinition, DbnReader, Symbology};
use qt_ingest::{Dataset, ingest};
use qt_schema::json::{IngestSummary, Verdict};
use qt_schema::specs::IngestSpec;
use qt_types::{MboEvent, QtError, UNDEF_PRICE, flags};
use serde_json::json;

const D: i64 = 86_400_000_000_000;
const H: i64 = 3_600_000_000_000;
const S: i64 = 1_000_000_000;
const ES_TICK: i64 = 250_000_000; // 0.25 at 1e-9
const PX: i64 = 4_500_250_000_000; // 4500.25

fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("qt-dbn-defs-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn spec(v: serde_json::Value) -> IngestSpec {
    serde_json::from_value(v).unwrap()
}

fn set_str(dst: &mut [c_char], s: &str) {
    assert!(s.len() < dst.len(), "{s} too long");
    for (i, b) in s.bytes().enumerate() {
        dst[i] = b as c_char;
    }
}

fn ev(iid: u32, ts: i64, action: u8, side: u8, oid: u64, seq: u64) -> MboEvent {
    MboEvent {
        ts_event: ts,
        ts_recv: ts,
        instrument_id: iid,
        action,
        side,
        price: PX,
        size: 2,
        order_id: oid,
        flags: flags::LAST,
        sequence: seq,
        channel_id: 0,
        publisher_id: 1,
    }
}

/// Add, add, cancel: a clean book of one instrument at `t`.
fn book(iid: u32, t: i64, seq0: u64) -> Vec<MboEvent> {
    vec![
        ev(iid, t, b'A', b'B', 1, seq0),
        ev(iid, t + S, b'A', b'A', 2, seq0 + 1),
        ev(iid, t + 2 * S, b'C', b'B', 1, seq0 + 2),
    ]
}

fn mbo_msg(e: &MboEvent) -> MboMsg {
    MboMsg {
        hd: RecordHeader::new::<MboMsg>(rtype::MBO, e.publisher_id, e.instrument_id, e.ts_event as u64),
        order_id: e.order_id,
        price: e.price,
        size: e.size as u32,
        flags: FlagSet::new(e.flags),
        channel_id: e.channel_id as u8,
        action: e.action as c_char,
        side: e.side as c_char,
        ts_recv: e.ts_recv as u64,
        ts_in_delta: 0,
        sequence: e.sequence as u32,
    }
}

struct Md {
    dataset: &'static str,
    schema: Option<Schema>,
    day0: i32,
    day1: i32,
    stype_in: Option<SType>,
    mappings: Vec<SymbolMapping>,
    version: u8,
    span_ns: Option<(i64, i64)>,
}

impl Md {
    fn new(dataset: &'static str, schema: Option<Schema>, day0: i32, day1: i32) -> Self {
        Self {
            dataset,
            schema,
            day0,
            day1,
            stype_in: Some(SType::RawSymbol),
            mappings: vec![],
            version: ::dbn::DBN_VERSION,
            span_ns: None,
        }
    }

    /// Request window `[start, end)` in UTC ns instead of whole UTC dates.
    fn span_ns(mut self, start: i64, end: i64) -> Self {
        self.span_ns = Some((start, end));
        self
    }

    fn map(mut self, m: Vec<SymbolMapping>) -> Self {
        self.mappings = m;
        self
    }

    fn stype_in(mut self, s: Option<SType>) -> Self {
        self.stype_in = s;
        self
    }

    fn version(mut self, v: u8) -> Self {
        self.version = v;
        self
    }

    fn build(&self) -> Metadata {
        Metadata::builder()
            .version(self.version)
            .dataset(self.dataset)
            .schema(self.schema)
            .start(self.span_ns.map_or(i64::from(self.day0) * D, |s| s.0) as u64)
            .end(NonZeroU64::new(
                self.span_ns.map_or(i64::from(self.day1) * D, |s| s.1) as u64
            ))
            .stype_in(self.stype_in)
            .stype_out(SType::InstrumentId)
            .mappings(self.mappings.clone())
            .build()
    }
}

/// Symbology of one input symbol: `[d0, d1)` UTC dates -> instrument id.
fn mapping(key: &str, spans: &[(i32, i32, u32)]) -> SymbolMapping {
    let date = |day: i32| Md::new("X", Some(Schema::Mbo), day, day + 1).build().start().date();
    SymbolMapping {
        raw_symbol: key.into(),
        intervals: spans
            .iter()
            .map(|&(a, b, id)| MappingInterval {
                start_date: date(a),
                end_date: date(b),
                symbol: id.to_string(),
            })
            .collect(),
    }
}

/// Writes records of possibly different types (each encoded as given).
struct Writer(Encoder<std::fs::File>);

impl Writer {
    fn create(path: &Path, md: &Md) -> Self {
        Self(Encoder::new(std::fs::File::create(path).unwrap(), &md.build()).unwrap())
    }

    fn rec<R: ::dbn::encode::DbnEncodable>(&mut self, r: &R) -> &mut Self {
        self.0.encode_record(r).unwrap();
        self
    }

    fn mbo(&mut self, events: &[MboEvent]) -> &mut Self {
        for e in events {
            self.rec(&mbo_msg(e));
        }
        self
    }
}

#[derive(Clone)]
struct Def {
    iid: u32,
    symbol: &'static str,
    mpi: i64,
    ts_recv: i64,
    algo: u8,
    action: u8,
    raw_id: u64,
    amount: i64,
    currency: &'static str,
}

fn def(iid: u32, symbol: &'static str, mpi: i64, ts_recv: i64) -> Def {
    Def {
        iid,
        symbol,
        mpi,
        ts_recv,
        algo: b'F',
        action: b'A',
        raw_id: 0,
        amount: i64::MAX,
        currency: "USD",
    }
}

impl Def {
    fn algo(mut self, a: u8) -> Self {
        self.algo = a;
        self
    }

    fn action(mut self, a: u8) -> Self {
        self.action = a;
        self
    }

    fn raw_id(mut self, r: u64) -> Self {
        self.raw_id = r;
        self
    }

    /// `min_price_increment_amount` (1e-9) and currency.
    fn value(mut self, amount: i64, currency: &'static str) -> Self {
        self.amount = amount;
        self.currency = currency;
        self
    }

    fn v3(&self) -> InstrumentDefMsg {
        let mut d = InstrumentDefMsg {
            hd: RecordHeader::new::<InstrumentDefMsg>(rtype::INSTRUMENT_DEF, 1, self.iid, self.ts_recv as u64),
            ts_recv: self.ts_recv as u64,
            min_price_increment: self.mpi,
            min_price_increment_amount: self.amount,
            raw_instrument_id: self.raw_id,
            match_algorithm: self.algo as c_char,
            security_update_action: self.action as c_char,
            instrument_class: b'F' as c_char,
            min_lot_size_round_lot: 1,
            ..Default::default()
        };
        set_str(&mut d.raw_symbol, self.symbol);
        set_str(&mut d.exchange, "XCME");
        set_str(&mut d.currency, self.currency);
        set_str(&mut d.security_type, "FUT");
        d
    }

    fn v2(&self) -> ::dbn::v2::InstrumentDefMsg {
        let mut d = ::dbn::v2::InstrumentDefMsg {
            hd: RecordHeader::new::<::dbn::v2::InstrumentDefMsg>(
                rtype::INSTRUMENT_DEF,
                1,
                self.iid,
                self.ts_recv as u64,
            ),
            ts_recv: self.ts_recv as u64,
            min_price_increment: self.mpi,
            min_price_increment_amount: self.amount,
            raw_instrument_id: self.raw_id as u32,
            match_algorithm: self.algo as c_char,
            security_update_action: self.action as c_char,
            instrument_class: b'F' as c_char,
            ..Default::default()
        };
        set_str(&mut d.raw_symbol, self.symbol);
        d
    }

    fn v1(&self) -> ::dbn::v1::InstrumentDefMsg {
        let mut d = ::dbn::v1::InstrumentDefMsg {
            hd: RecordHeader::new::<::dbn::v1::InstrumentDefMsg>(
                rtype::INSTRUMENT_DEF,
                1,
                self.iid,
                self.ts_recv as u64,
            ),
            ts_recv: self.ts_recv as u64,
            min_price_increment: self.mpi,
            min_price_increment_amount: self.amount,
            raw_instrument_id: self.raw_id as u32,
            match_algorithm: self.algo as c_char,
            instrument_class: b'F' as c_char,
            ..Default::default()
        };
        set_str(&mut d.raw_symbol, self.symbol);
        d
    }
}

fn run(spec_v: serde_json::Value) -> Result<IngestSummary, QtError> {
    ingest(&spec(spec_v))
}

fn dbn_spec(out: &Path, files: &[&Path], venue: &str, extra: serde_json::Value) -> serde_json::Value {
    let mut v = json!({"format": "dbn", "venue": venue, "out": out.to_str().unwrap(),
        "inputs": files.iter().map(|f| json!({"path": f.to_str().unwrap()})).collect::<Vec<_>>()});
    for (k, x) in extra.as_object().unwrap() {
        v.as_object_mut().unwrap().insert(k.clone(), x.clone());
    }
    v
}

/// `(session date, id, symbol, tick)` of every instrument row.
fn rows(out: &Path) -> Vec<(i32, u32, String, i64)> {
    let ds = Dataset::open(out).unwrap();
    ds.partitions()
        .iter()
        .map(|p| {
            let i = ds.instrument(p.instrument_id, p.session_date).unwrap();
            (p.session_date, p.instrument_id, i.symbol.clone(), i.tick_size)
        })
        .collect()
}

fn records(out: &Path) -> Vec<(i32, u32, Vec<MboEvent>)> {
    let ds = Dataset::open(out).unwrap();
    ds.partitions()
        .iter()
        .map(|p| {
            (
                p.session_date,
                p.instrument_id,
                p.mbo().unwrap().map(Result::unwrap).collect(),
            )
        })
        .collect()
}

fn counter(s: &IngestSummary, k: &str) -> Option<u64> {
    s.data_quality.source_specific.get(k).copied()
}

/// Ingest warnings (carried in `data_quality.reasons` with a `warning: ` prefix).
fn warns(s: &IngestSummary) -> Vec<String> {
    s.data_quality
        .reasons
        .iter()
        .filter_map(|r| r.strip_prefix("warning: ").map(str::to_owned))
        .collect()
}

fn defaulted_tick(s: &IngestSummary) -> bool {
    counter(s, "instruments_default_tick_size").is_some() || warns(s).iter().any(|w| w.contains("tick_size defaulted"))
}

// ---- (a) definition files of every DBN version supply the tick --------------------------------------------------

#[test]
fn a_definition_files_v1_v2_v3_supply_the_tick_and_avoid_the_default_tick_path() {
    let dir = scratch("versions");
    let d0: i32 = 20_000;
    let t0 = i64::from(d0) * D;
    let mbo = dir.join("es.mbo.dbn");
    let md = Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1).map(vec![mapping("ESZ4", &[(d0, d0 + 1, 42)])]);
    Writer::create(&mbo, &md).mbo(&book(42, t0 + 14 * H, 1));
    // Baseline: no tick from the spec or the source is the warning path.
    let s = run(dbn_spec(&dir.join("none"), &[&mbo], "cme", json!({}))).unwrap();
    assert!(defaulted_tick(&s), "{:?}", warns(&s));
    assert!(warns(&s)[0].contains("can effectively hang"), "{:?}", warns(&s));
    assert_eq!(rows(&dir.join("none")), vec![(d0, 42, "ESZ4".into(), 1)]);
    let d = def(42, "ESZ4", ES_TICK, t0);
    for v in [1u8, 2, 3] {
        let f = dir.join(format!("defs.v{v}.dbn"));
        let dmd = Md::new("GLBX.MDP3", Some(Schema::Definition), d0, d0 + 1).version(v);
        let mut w = Writer::create(&f, &dmd);
        match v {
            1 => w.rec(&d.v1()),
            2 => w.rec(&d.v2()),
            _ => w.rec(&d.v3()),
        };
        drop(w);
        let mut r = DbnReader::open(&f).unwrap();
        assert_eq!(r.metadata().version, v);
        let got = r.next_definition_record().unwrap().unwrap();
        assert_eq!(
            (
                got.instrument_id,
                got.raw_symbol.as_str(),
                got.min_price_increment,
                got.match_algorithm,
                got.instrument_class,
                got.security_update_action,
                got.valid_from()
            ),
            (42, "ESZ4", Some(ES_TICK), b'F', b'F', b'A', Some(t0 as u64)),
            "v{v}"
        );
        assert!(r.next_definition_record().unwrap().is_none());
        // The four-field form keeps its contract.
        let mut r = DbnReader::open(&f).unwrap();
        assert_eq!(
            r.next_definition().unwrap(),
            Some((42, "ESZ4".into(), ES_TICK, t0 as u64))
        );
        let out = dir.join(format!("ds-v{v}"));
        let s = run(dbn_spec(&out, &[&mbo, &f], "cme", json!({}))).unwrap();
        assert!(!defaulted_tick(&s), "v{v}: {:?}", warns(&s));
        assert!(warns(&s).is_empty(), "v{v}: {:?}", warns(&s));
        assert_eq!(
            s.data_quality.verdict,
            Verdict::Clean,
            "v{v}: {:?}",
            s.data_quality.reasons
        );
        assert_eq!(rows(&out), vec![(d0, 42, "ESZ4".into(), ES_TICK)], "v{v}");
    }
    // A tick at a coarser price exponent is rescaled exactly (0.25 at 1e-2 is 25).
    let out = dir.join("exp2");
    run(dbn_spec(
        &out,
        &[&mbo, &dir.join("defs.v3.dbn")],
        "cme",
        json!({"price_exp": -2}),
    ))
    .unwrap();
    assert_eq!(rows(&out)[0].3, 25);
    // The spec's tick_size overrides the definition.
    let out = dir.join("spec");
    run(dbn_spec(
        &out,
        &[&mbo, &dir.join("defs.v3.dbn")],
        "cme",
        json!({"tick_size": "0.5"}),
    ))
    .unwrap();
    assert_eq!(rows(&out)[0].3, 500_000_000);
    // Text fields the record leaves unset read as empty; an undefined raw_instrument_id (0) is none.
    let mut r = DbnReader::open(&dir.join("defs.v1.dbn")).unwrap();
    let got = r.next_definition_record().unwrap().unwrap();
    assert_eq!((got.exchange.as_str(), got.raw_instrument_id), ("", None));
    let mut r = DbnReader::open(&dir.join("defs.v3.dbn")).unwrap();
    let got = r.next_definition_record().unwrap().unwrap();
    assert_eq!(
        (
            got.exchange.as_str(),
            got.currency.as_str(),
            got.security_type.as_str(),
            got.min_lot_size_round_lot
        ),
        ("XCME", "USD", "FUT", Some(1))
    );
    let _ = std::fs::remove_dir_all(&dir);
}

// ---- (b) mixed-schema files (live captures) -----------------------------------------------------------------------

#[test]
fn b_definitions_and_symbol_mappings_inside_a_mixed_schema_file() {
    let dir = scratch("mixed");
    let d0: i32 = 20_001;
    let t0 = i64::from(d0) * D;
    let events = book(42, t0 + 14 * H, 10);
    // A live capture: no metadata mappings; a symbol-mapping record and a definition precede the MBO records.
    for v in [1u8, 3] {
        let f = dir.join(format!("live.v{v}.dbn"));
        let md = Md::new("GLBX.MDP3", None, d0, d0 + 1)
            .stype_in(Some(SType::Parent))
            .version(v);
        let mut w = Writer::create(&f, &md);
        let d = def(42, "ESZ4", ES_TICK, t0 + 13 * H);
        if v == 1 {
            let mut m = ::dbn::v1::SymbolMappingMsg {
                hd: RecordHeader::new::<::dbn::v1::SymbolMappingMsg>(
                    rtype::SYMBOL_MAPPING,
                    1,
                    42,
                    (t0 + 13 * H) as u64,
                ),
                start_ts: t0 as u64,
                end_ts: (t0 + D) as u64,
                ..Default::default()
            };
            set_str(&mut m.stype_in_symbol, "ES.FUT");
            set_str(&mut m.stype_out_symbol, "ESZ4");
            w.rec(&m).rec(&d.v1());
        } else {
            let mut m = SymbolMappingMsg {
                hd: RecordHeader::new::<SymbolMappingMsg>(rtype::SYMBOL_MAPPING, 1, 42, (t0 + 13 * H) as u64),
                stype_in: SType::Parent as u8,
                stype_out: SType::RawSymbol as u8,
                start_ts: t0 as u64,
                end_ts: (t0 + D) as u64,
                ..Default::default()
            };
            set_str(&mut m.stype_in_symbol, "ES.FUT");
            set_str(&mut m.stype_out_symbol, "ESZ4");
            w.rec(&m).rec(&d.v3());
        }
        w.mbo(&events);
        drop(w);
        let out = dir.join(format!("ds-v{v}"));
        let s = run(dbn_spec(&out, &[&f], "cme", json!({}))).unwrap();
        assert!(warns(&s).is_empty(), "v{v}: {:?}", warns(&s));
        assert_eq!(rows(&out), vec![(d0, 42, "ESZ4".into(), ES_TICK)], "v{v}");
        assert_eq!(records(&out), vec![(d0, 42, events.clone())], "v{v}");
        assert_eq!(s.data_quality.records, 3);
        assert_eq!(counter(&s, "dbn_mixed_schema_other_records"), Some(2));
        assert_eq!(counter(&s, "dbn_other_records_skipped"), None);
        assert_eq!(counter(&s, "dbn_symbol_mapping_records"), Some(1));
        // Skipped definition and mapping records of a mixed file are volumes, not anomalies.
        assert_eq!(
            s.data_quality.verdict,
            Verdict::Clean,
            "v{v}: {:?}",
            s.data_quality.reasons
        );
        // The symbol filter accepts the raw symbol and the requested parent symbol, case-insensitively.
        for (sel, n) in [("esz4", 1), ("ES.FUT", 1), ("NQZ4", 0)] {
            let o = dir.join(format!("f-{v}-{sel}"));
            run(dbn_spec(&o, &[&f], "cme", json!({"symbols": [sel]}))).unwrap();
            assert_eq!(rows(&o).len(), n, "v{v} {sel}");
        }
        // The scan reports what it added and leaves MBO records to the MBO pass.
        let mut sym = Symbology::new();
        let mut r = DbnReader::open(&f).unwrap();
        assert_eq!(r.scan_symbology(&mut sym).unwrap(), 2);
        assert_eq!((r.records, r.skipped), (5, 3));
        assert_eq!(sym.symbol_at(42, d0), Some("ESZ4"));
        assert_eq!(sym.aliases_at(42, d0), vec!["ES.FUT"]);
        assert_eq!(sym.id_at("ESZ4", d0), Some(42));
        assert_eq!(sym.id_at("ES.FUT", d0), None);
    }
    let _ = std::fs::remove_dir_all(&dir);
}

// ---- (c) parent and continuous symbology ------------------------------------------------------------------------

#[test]
fn c_parent_and_continuous_metadata_keys_are_aliases_not_raw_symbols() {
    let dir = scratch("parent");
    let d0: i32 = 20_002;
    let t0 = i64::from(d0) * D;
    let mut events = book(42, t0 + 14 * H, 1);
    events.extend(book(43, t0 + 15 * H, 4));
    let parent = vec![mapping("ES.FUT", &[(d0, d0 + 1, 42), (d0, d0 + 1, 43)])];
    let f = dir.join("parent.mbo.dbn");
    Writer::create(
        &f,
        &Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1)
            .stype_in(Some(SType::Parent))
            .map(parent.clone()),
    )
    .mbo(&events);
    let defs = dir.join("parent.definition.dbn");
    Writer::create(
        &defs,
        &Md::new("GLBX.MDP3", Some(Schema::Definition), d0, d0 + 1)
            .stype_in(Some(SType::Parent))
            .map(parent),
    )
    .rec(&def(42, "ESZ4", ES_TICK, t0).v3())
    .rec(&def(43, "ESH5", ES_TICK, t0).v3());
    // With definitions: raw symbols per contract, never the parent key.
    let out = dir.join("ds");
    let s = run(dbn_spec(&out, &[&f, &defs], "cme", json!({}))).unwrap();
    assert!(warns(&s).is_empty(), "{:?}", warns(&s));
    assert_eq!(s.data_quality.verdict, Verdict::Clean, "{:?}", s.data_quality.reasons);
    assert_eq!(
        rows(&out),
        vec![(d0, 42, "ESZ4".into(), ES_TICK), (d0, 43, "ESH5".into(), ES_TICK)]
    );
    // The same intervals in both files count once.
    assert_eq!(counter(&s, "dbn_alias_mapping_intervals"), Some(2));
    for (k, (sel, want)) in [
        (json!(["ESH5"]), vec![43]),
        (json!(["ES.FUT"]), vec![42, 43]),
        (json!(["esz4", "esh5"]), vec![42, 43]),
    ]
    .into_iter()
    .enumerate()
    {
        let o = dir.join(format!("sel-{k}"));
        run(dbn_spec(&o, &[&f, &defs], "cme", json!({"symbols": sel}))).unwrap();
        assert_eq!(rows(&o).iter().map(|r| r.1).collect::<Vec<_>>(), want, "{sel}");
    }
    // Without definitions a parent key with two children names neither: the id is the label, never "ES.FUT".
    let out = dir.join("nodefs");
    let s = run(dbn_spec(&out, &[&f], "cme", json!({"tick_size": "0.25"}))).unwrap();
    assert_eq!(
        rows(&out),
        vec![(d0, 42, "42".into(), ES_TICK), (d0, 43, "43".into(), ES_TICK)]
    );
    assert_eq!(counter(&s, "instruments_without_symbol"), Some(2));
    // A continuous symbol resolves to one contract per date and names it when nothing better does; a mixed
    // stype_in classifies keys by shape (ES.c.0 is continuous, BRK.B is a raw symbol).
    let cont = dir.join("cont.mbo.dbn");
    Writer::create(
        &cont,
        &Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1)
            .stype_in(None)
            .map(vec![
                mapping("ES.c.0", &[(d0, d0 + 1, 42)]),
                mapping("BRK.B", &[(d0, d0 + 1, 43)]),
            ]),
    )
    .mbo(&events);
    let out = dir.join("cont");
    let s = run(dbn_spec(&out, &[&cont], "cme", json!({"tick_size": "0.25"}))).unwrap();
    assert_eq!(
        rows(&out),
        vec![(d0, 42, "ES.c.0".into(), ES_TICK), (d0, 43, "BRK.B".into(), ES_TICK)]
    );
    assert_eq!(counter(&s, "dbn_alias_mapping_intervals"), Some(1));
    // BRK.B is a raw symbol of id 43 in one file and ESH5 is in the other: refused.
    let e = run(dbn_spec(&dir.join("cont-defs"), &[&cont, &defs], "cme", json!({}))).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002", "{e}");
    assert!(
        e.to_string().contains("instrument id 43 on ") && e.to_string().contains("BRK.B"),
        "{e}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

// ---- (d) match algorithm, deletes, undefined ticks, undated records ----------------------------------------------

#[test]
fn d_non_fifo_match_algorithm_is_rejected_and_odd_definitions_are_counted() {
    let dir = scratch("algo");
    let d0: i32 = 20_003;
    let t0 = i64::from(d0) * D;
    let mut events = book(42, t0 + 14 * H, 1);
    events.extend(book(77, t0 + 15 * H, 4));
    events.extend(book(78, t0 + 16 * H, 7));
    let f = dir.join("mbo.dbn");
    Writer::create(
        &f,
        &Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1).map(vec![
            mapping("ESZ4", &[(d0, d0 + 1, 42)]),
            mapping("SR3Z4", &[(d0, d0 + 1, 77)]),
            mapping("GEZ4", &[(d0, d0 + 1, 78)]),
        ]),
    )
    .mbo(&events);
    let defs = dir.join("defs.dbn");
    let mut undated = def(99, "ZZZ", ES_TICK, 0).v3();
    undated.ts_recv = u64::MAX;
    undated.hd.ts_event = u64::MAX;
    Writer::create(&defs, &Md::new("GLBX.MDP3", Some(Schema::Definition), d0, d0 + 1))
        .rec(&def(42, "ESZ4", ES_TICK, t0).v3())
        // A later delete with an undefined tick neither removes nor replaces the tick.
        .rec(&def(42, "ESZ4", i64::MAX, t0 + 20 * H).action(b'D').v3())
        .rec(&def(77, "SR3Z4", 5_000_000, t0).algo(b'K').v3())
        // An undefined match algorithm (a space) says nothing; a later modify defines it.
        .rec(&def(78, "GEZ4", 5_000_000, t0).algo(b' ').v3())
        .rec(&def(78, "GEZ4", 5_000_000, t0 + H).algo(b'C').action(b'M').v3())
        .rec(&undated);
    // Pro-rata and allocation books are rejected at ingest (spec 1.2 non-goals) at the first record of such a
    // partition, naming it and the other selected non-FIFO instruments of the definitions.
    let e = run(dbn_spec(&dir.join("all"), &[&f, &defs], "cme", json!({}))).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001", "{e}");
    let m = e.to_string();
    assert!(
        m.contains("SR3Z4 (id 77, 2024-10-07: configurable 'K') is not matched by price-time priority"),
        "{m}"
    );
    assert!(
        m.contains("1 other selected instrument(s) a non-FIFO algorithm too: GEZ4 (pro-rata)"),
        "{m}"
    );
    assert!(m.contains("--symbols") && !m.contains("ESZ4"), "{m}");
    assert!(!dir.join("all").exists(), "a refused ingest removes what it wrote");
    // The batch form names every non-FIFO partition.
    let mut sym = Symbology::new();
    DbnReader::open(&defs).unwrap().scan_symbology(&mut sym).unwrap();
    let e = sym
        .check_fifo([(d0, 42, Some("ESZ4")), (d0, 77, Some("SR3Z4")), (d0, 78, Some("GEZ4"))])
        .unwrap_err();
    let m = e.to_string();
    assert!(
        m.contains("2 instrument partition(s)") && m.contains("GEZ4 (id 78, 2024-10-07: pro-rata 'C')"),
        "{m}"
    );
    // Leaving them out with the symbol filter ingests the FIFO product cleanly; the later delete with an undefined
    // tick neither removed nor replaced the tick, and the odd records are counted.
    let out = dir.join("es");
    let s = run(dbn_spec(&out, &[&f, &defs], "cme", json!({"symbols": ["ESZ4"]}))).unwrap();
    assert_eq!(rows(&out), vec![(d0, 42, "ESZ4".into(), ES_TICK)]);
    assert!(warns(&s).is_empty(), "{:?}", warns(&s));
    assert_eq!(counter(&s, "dbn_definition_deletes"), Some(1));
    assert_eq!(counter(&s, "dbn_definitions_undated"), Some(1));
    assert_eq!(counter(&s, "dbn_definitions_without_tick"), None);
    // An undated definition is not an INFO counter: it raises the verdict to warnings; deletes do not.
    assert_eq!(s.data_quality.verdict, Verdict::Warnings);
    assert_eq!(s.data_quality.reasons, vec!["dbn_definitions_undated = 1".to_string()]);
    // The unit-level check: an undefined algorithm (78 before its modify) passes.
    assert!(sym.check_fifo([(d0, 42, Some("ESZ4"))]).is_ok());
    assert_eq!(sym.match_algorithm(78, Some("GEZ4"), d0), Some(b'C'));
    let mut early = Symbology::new();
    early.add_definition_record(
        &DbnReader::open(&defs)
            .unwrap()
            .next_definition_record()
            .unwrap()
            .map(|mut d| {
                d.instrument_id = 78;
                d.raw_symbol = "GEZ4".into();
                d.match_algorithm = b' ';
                d
            })
            .unwrap(),
        "x",
    );
    assert!(early.check_fifo([(d0, 78, Some("GEZ4"))]).is_ok());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn d_symbology_unit_semantics() {
    let d0: i32 = 20_010;
    let t0 = i64::from(d0) * D;
    let mut sym = Symbology::new();
    let mk = |iid: u32, s: &str, mpi: Option<i64>, ts: i64| DbnDefinition {
        instrument_id: iid,
        ts_recv: ts as u64,
        ts_event: ts as u64,
        raw_symbol: s.into(),
        raw_instrument_id: None,
        min_price_increment: mpi,
        min_price_increment_amount: None,
        display_factor: None,
        min_lot_size_round_lot: None,
        instrument_class: b'F',
        match_algorithm: b'F',
        security_update_action: b'A',
        exchange: String::new(),
        asset: String::new(),
        security_type: String::new(),
        currency: String::new(),
    };
    // Midnight snapshots repeat a definition every day: kept once, every date recorded.
    for k in 0..5 {
        sym.add_definition_record(&mk(42, "ESZ4", Some(ES_TICK), t0 + i64::from(k) * D), "f");
    }
    // A later change of tick supersedes from its own date.
    sym.add_definition_record(&mk(42, "ESZ4", Some(2 * ES_TICK), t0 + 5 * D + H), "f");
    let st = sym.stats();
    assert_eq!((st.definitions, st.definitions_repeated), (6, 4));
    assert_eq!(sym.min_price_increment(42, Some("ESZ4"), d0 + 4), Some(ES_TICK));
    assert_eq!(sym.min_price_increment(42, Some("ESZ4"), d0 + 5), Some(2 * ES_TICK));
    // Before the first definition: the latest of any date.
    assert_eq!(sym.min_price_increment(42, Some("ESZ4"), d0 - 3), Some(2 * ES_TICK));
    // A symbol mismatch never borrows another instrument's tick; the symbol's own definition under another id does.
    assert_eq!(sym.min_price_increment(42, Some("NQZ4"), d0), None);
    sym.add_definition_record(&mk(7, "NQZ4", Some(ES_TICK), t0), "f");
    assert_eq!(sym.min_price_increment(42, Some("NQZ4"), d0), Some(ES_TICK));
    // A definition without a tick is counted and leaves the earlier tick in force.
    sym.add_definition_record(&mk(42, "ESZ4", None, t0 + 6 * D), "f");
    assert_eq!(sym.min_price_increment(42, Some("ESZ4"), d0 + 6), Some(2 * ES_TICK));
    assert_eq!(sym.stats().definitions_without_tick, 1);
    assert!(sym.check().is_ok());
    // The legacy four-field form maps an undefined tick to none.
    let mut s2 = Symbology::new();
    s2.add_definition(5, "X", i64::MAX, t0 as u64);
    s2.add_definition(5, "X", 0, (t0 + 1) as u64);
    assert_eq!(s2.min_price_increment(5, Some("X"), d0), None);
    assert_eq!(s2.symbol_at(5, d0), Some("X"));
    assert_eq!(s2.stats().definitions_without_tick, 2);
}

#[test]
fn d_definition_in_the_after_hours_hour_may_carry_the_previous_dates_id() {
    let d1: i32 = 20_467; // 2026-01-14 (EST)
    let (u1, u2) = (i64::from(d1) * D, i64::from(d1 + 1) * D);
    let md = Md::new("XNAS.ITCH", Some(Schema::Mbo), d1, d1 + 2)
        .map(vec![mapping("AAPL", &[(d1, d1 + 1, 7), (d1 + 1, d1 + 2, 8)])])
        .build();
    let with = |ts: i64| {
        let mut sym = Symbology::new();
        sym.add_metadata_from(&md, Path::new("mbo.dbn"));
        sym.add_definition(7, "AAPL", 10_000_000, u1 as u64);
        sym.add_definition(7, "AAPL", 10_000_000, ts as u64);
        sym.check()
    };
    // 00:30 UTC on d1 + 1 is still the d1 session's after-hours: d1's id is accepted.
    assert!(with(u2 + 30 * 60 * S).is_ok());
    // From 01:00 UTC the date's own symbology applies.
    let e = with(u2 + H).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002");
    assert!(
        e.to_string()
            .contains("AAPL on 2026-01-15: instrument id 8 (mbo.dbn) and 7 (definition in definition)"),
        "{e}"
    );
}

// ---- (e) the 2026-10-31 XNAS.ITCH re-issue: both vintages, and a mix of them ----------------------------------

/// One XNAS.ITCH day, `day` an EST date (2023-01-03, before 2023-11-28), in either vintage. Returns the MBO and
/// definition files and the session records with the vintage's ids. Old vintage: AAPL is stock locate 13, no
/// midnight snapshots, after-hours stop at 00:00 UTC. New vintage: AAPL is id 40 (the locate in
/// `raw_instrument_id`), MSFT is id 13 (an old id re-used for another symbol), the next UTC date starts with the last
/// after-hours hour and a midnight snapshot of the book and the definitions.
fn xnas_vintage(dir: &Path, day: i32, new: bool) -> (PathBuf, PathBuf, Vec<MboEvent>) {
    let u = i64::from(day) * D;
    let u2 = u + D;
    let (aapl, msft) = if new { (40, 13) } else { (13, 21) };
    let tag = if new { "new" } else { "old" };
    let mut session = book(aapl, u + 15 * H, 1);
    session.extend(book(msft, u + 15 * H + 10 * S, 4));
    session.push(ev(aapl, u + 23 * H, b'A', b'B', 9, 7));
    let mut file = session.clone();
    let mut map = vec![
        mapping("AAPL", &[(day, day + 1, aapl)]),
        mapping("MSFT", &[(day, day + 1, msft)]),
    ];
    let mut defs = vec![
        def(aapl, "AAPL", i64::MAX, u + 8 * H).raw_id(13),
        def(msft, "MSFT", i64::MAX, u + 8 * H).raw_id(21),
    ];
    if new {
        // UTC date day + 1: the definitions snapshot and the book snapshot at 00:00, then after-hours to 00:59.
        let snap = [
            MboEvent {
                action: b'R',
                side: b'N',
                price: UNDEF_PRICE,
                size: 0,
                order_id: 0,
                flags: 0,
                sequence: 0,
                ..ev(aapl, u2, b'R', b'N', 0, 0)
            },
            MboEvent {
                ts_recv: u2,
                flags: flags::SNAPSHOT,
                sequence: 0,
                ..ev(aapl, u + 15 * H + S, b'A', b'A', 2, 0)
            },
            MboEvent {
                ts_recv: u2,
                flags: flags::SNAPSHOT | flags::LAST,
                sequence: 0,
                ..ev(aapl, u + 23 * H, b'A', b'B', 9, 0)
            },
        ];
        let after = [
            ev(aapl, u2 + 30 * 60 * S, b'A', b'A', 10, 8),
            ev(aapl, u2 + 59 * 60 * S, b'C', b'A', 10, 9),
        ];
        session.extend(snap.iter().chain(&after).copied());
        file.extend(snap.iter().chain(&after).copied());
        map = vec![
            mapping("AAPL", &[(day, day + 2, aapl)]),
            mapping("MSFT", &[(day, day + 2, msft)]),
        ];
        defs.push(def(aapl, "AAPL", i64::MAX, u2).raw_id(13));
        defs.push(def(msft, "MSFT", i64::MAX, u2).raw_id(21));
    }
    for d in &mut defs {
        if !new {
            // The old vintage leaves raw_instrument_id undefined: i32::MAX, as in Databento's 2021 test file.
            d.raw_id = i32::MAX as u64;
        }
    }
    let days = if new { 2 } else { 1 };
    let mbo = dir.join(format!("{tag}.mbo.dbn"));
    Writer::create(
        &mbo,
        &Md::new("XNAS.ITCH", Some(Schema::Mbo), day, day + days).map(map.clone()),
    )
    .mbo(&file);
    let dfile = dir.join(format!("{tag}.definition.dbn"));
    let mut w = Writer::create(
        &dfile,
        &Md::new("XNAS.ITCH", Some(Schema::Definition), day, day + days).map(map),
    );
    for d in &defs {
        let mut m = d.v3();
        m.instrument_class = b'K' as c_char;
        m.min_lot_size_round_lot = 100;
        set_str(&mut m.exchange, "XNAS");
        w.rec(&m);
    }
    drop(w);
    (mbo, dfile, session)
}

#[test]
fn e_both_xnas_vintages_ingest_to_the_same_symbols_and_ticks() {
    let dir = scratch("vintages");
    let day: i32 = 19_360; // 2023-01-03 (EST), before the 2023-11-28 id cut-over
    let (old_mbo, old_defs, old_session) = xnas_vintage(&dir, day, false);
    let (new_mbo, new_defs, new_session) = xnas_vintage(&dir, day, true);
    let (o_out, n_out) = (dir.join("old"), dir.join("new"));
    let so = run(dbn_spec(&o_out, &[&old_mbo, &old_defs], "nasdaq", json!({}))).unwrap();
    let sn = run(dbn_spec(&n_out, &[&new_mbo, &new_defs], "nasdaq", json!({}))).unwrap();
    // XNAS.ITCH definitions carry no min_price_increment: the Nasdaq venue's 0.01 applies, without the warning.
    for s in [&so, &sn] {
        assert_eq!(s.data_quality.verdict, Verdict::Clean, "{:?}", s.data_quality.reasons);
        assert!(warns(s).is_empty(), "{:?}", warns(s));
        assert_eq!(counter(s, "instruments_default_tick_size"), None);
        assert_eq!(s.data_quality.sequence_gaps, 0);
    }
    assert_eq!(counter(&so, "dbn_definitions_without_tick"), Some(2));
    assert_eq!(counter(&sn, "dbn_definitions_without_tick"), Some(4));
    let by_symbol = |out: &Path| {
        let mut v: Vec<(i32, String, i64)> = rows(out).into_iter().map(|(d, _, s, t)| (d, s, t)).collect();
        v.sort();
        v
    };
    let want = vec![
        (day, "AAPL".to_string(), 10_000_000),
        (day, "MSFT".to_string(), 10_000_000),
    ];
    assert_eq!(by_symbol(&o_out), want);
    assert_eq!(by_symbol(&n_out), want);
    // Same session date and symbols; the new vintage has the after-hours hour and the midnight snapshot.
    let ids = |out: &Path| rows(out).into_iter().map(|r| (r.2, r.1)).collect::<Vec<_>>();
    assert_eq!(ids(&o_out), vec![("AAPL".into(), 13), ("MSFT".into(), 21)]);
    assert_eq!(ids(&n_out), vec![("MSFT".into(), 13), ("AAPL".into(), 40)]);
    let flat = |out: &Path| records(out).into_iter().flat_map(|(_, _, v)| v).collect::<Vec<_>>();
    let sorted = |mut v: Vec<MboEvent>| {
        v.sort_by_key(|e| (e.instrument_id, e.ts_recv, e.sequence));
        v
    };
    assert_eq!(sorted(flat(&o_out)), sorted(old_session));
    assert_eq!(sorted(flat(&n_out)), sorted(new_session));
    assert_eq!(sn.data_quality.snapshot_records, 2);
    // Definition records alone (without any metadata mapping) also resolve the new ids per date.
    let mut sym = Symbology::new();
    let mut r = DbnReader::open(&new_defs).unwrap();
    while let Some(d) = r.next_definition_record().unwrap() {
        sym.add_definition_record(&d, "new");
    }
    assert_eq!(sym.stats().definitions_repeated, 2);
    assert_eq!(
        (sym.symbol_at(40, day), sym.symbol_at(13, day)),
        (Some("AAPL"), Some("MSFT"))
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn e_mixed_vintages_and_mixed_datasets_are_refused() {
    let dir = scratch("mix");
    let day: i32 = 19_360;
    let (old_mbo, old_defs, _) = xnas_vintage(&dir, day, false);
    let (new_mbo, new_defs, _) = xnas_vintage(&dir, day, true);
    // Old MBO with the re-issued definitions: AAPL is 13 in one and 40 in the other on the same date.
    let e = run(dbn_spec(&dir.join("a"), &[&old_mbo, &new_defs], "nasdaq", json!({}))).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002", "{e}");
    let m = e.to_string();
    for want in [
        "inputs disagree on the instrument symbology",
        "AAPL on 2023-01-03: instrument id 13",
        "old.mbo.dbn",
        "new.definition.dbn",
        "re-issue",
        "raw_instrument_id",
    ] {
        assert!(m.contains(want), "{want}: {m}");
    }
    // Re-issued MBO with the old definitions, and two MBO files of the two vintages.
    let pairs: [[&Path; 2]; 2] = [[&new_mbo, &old_defs], [&old_mbo, &new_mbo]];
    for files in pairs {
        let e = run(dbn_spec(&dir.join("b"), &files, "nasdaq", json!({}))).unwrap_err();
        assert_eq!(e.code(), "QT-INPUT-002", "{e}");
        assert!(e.to_string().contains("disagree"), "{e}");
    }
    // Definitions of another dataset.
    let glbx = dir.join("glbx.definition.dbn");
    Writer::create(&glbx, &Md::new("GLBX.MDP3", Some(Schema::Definition), day, day + 1))
        .rec(&def(42, "ESZ4", ES_TICK, i64::from(day) * D).v3());
    let e = run(dbn_spec(&dir.join("c"), &[&old_mbo, &glbx], "nasdaq", json!({}))).unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002", "{e}");
    let m = e.to_string();
    assert!(
        m.contains("2 Databento datasets") && m.contains("GLBX.MDP3") && m.contains("XNAS.ITCH"),
        "{m}"
    );
    // Nothing was written for a refused ingest.
    assert!(!dir.join("a").join("manifest.json").exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn e_vintages_without_mbo_symbology_and_across_dates() {
    let dir = scratch("nomap");
    let day: i32 = 19_360; // 2023-01-03 (EST)
    let (old_mbo, old_defs, old_session) = xnas_vintage(&dir, day, false);
    let (_, new_defs, _) = xnas_vintage(&dir, day, true);
    // An MBO file whose metadata maps no raw symbol (an ALL_SYMBOLS or instrument_id request): the old vintage's
    // records, AAPL 13 and MSFT 21.
    let bare = dir.join("old.bare.mbo.dbn");
    Writer::create(
        &bare,
        &Md::new("XNAS.ITCH", Some(Schema::Mbo), day, day + 1).stype_in(Some(SType::InstrumentId)),
    )
    .mbo(&old_session);
    // With the re-issued definitions, id 13 is MSFT: the ingest cannot tell, so it says so (verdict warnings).
    let out = dir.join("bare-new");
    let s = run(dbn_spec(&out, &[&bare, &new_defs], "nasdaq", json!({}))).unwrap();
    assert_eq!(
        rows(&out).into_iter().map(|r| (r.1, r.2)).collect::<Vec<_>>(),
        vec![(13, "MSFT".to_string()), (21, "21".to_string())]
    );
    assert_eq!(counter(&s, "dbn_xnas_vintage_unverified"), Some(1));
    assert_eq!(s.data_quality.verdict, Verdict::Warnings);
    let w = warns(&s).join("\n");
    assert!(
        w.contains(
            "1 XNAS.ITCH partition(s) before 2023-11-28 take their symbol from definitions that no MBO file's \
                    metadata confirms: MSFT (id 13, 2023-01-03)"
        ) && w.contains("stype_in=\"raw_symbol\""),
        "{w}"
    );
    // The same warning with the right definitions: the vintage cannot be checked either way.
    let s = run(dbn_spec(
        &dir.join("bare-old"),
        &[&bare, &old_defs],
        "nasdaq",
        json!({}),
    ))
    .unwrap();
    assert_eq!(counter(&s, "dbn_xnas_vintage_unverified"), Some(2));
    // MBO metadata that maps the raw symbols confirms them: no warning (e_both_xnas_vintages_...).
    let s = run(dbn_spec(
        &dir.join("mapped"),
        &[&old_mbo, &old_defs],
        "nasdaq",
        json!({}),
    ))
    .unwrap();
    assert_eq!(counter(&s, "dbn_xnas_vintage_unverified"), None);
    // From 2023-11-28 the re-issue keeps the ids: nothing to verify.
    let late = scratch("nomap-late");
    let lday = 19_698; // 2023-12-07
    let (_, late_defs, late_session) = xnas_vintage(&late, lday, true);
    let late_bare = late.join("bare.mbo.dbn");
    Writer::create(
        &late_bare,
        &Md::new("XNAS.ITCH", Some(Schema::Mbo), lday, lday + 2).stype_in(Some(SType::InstrumentId)),
    )
    .mbo(&late_session);
    let s = run(dbn_spec(
        &late.join("ds"),
        &[&late_bare, &late_defs],
        "nasdaq",
        json!({}),
    ))
    .unwrap();
    assert_eq!(counter(&s, "dbn_xnas_vintage_unverified"), None);
    assert!(warns(&s).is_empty(), "{:?}", warns(&s));
    // Definitions of both vintages, on different dates (old 2023-01-03, re-issued 2023-01-04), are refused even
    // though no date has two ids for one symbol; the message names an example of each.
    let next = scratch("nomap-next");
    let (new_mbo2, new_defs2, _) = xnas_vintage(&next, day + 1, true);
    let e = run(dbn_spec(
        &dir.join("cross"),
        &[&old_mbo, &old_defs, &new_mbo2, &new_defs2],
        "nasdaq",
        json!({}),
    ))
    .unwrap_err();
    assert_eq!(e.code(), "QT-INPUT-002", "{e}");
    let m = e.to_string();
    for want in [
        "definitions before 2023-11-28 come from both vintages",
        "AAPL id 13 on 2023-01-03 (raw_instrument_id undefined: the vintage before the re-issue",
        "MSFT id 13 on 2023-01-04 (raw_instrument_id 21: the re-issued vintage",
        "old.definition.dbn",
        "own folder",
    ] {
        assert!(m.contains(want), "{want}: {m}");
    }
    assert!(!dir.join("cross").exists());
    for d in [&dir, &late, &next] {
        let _ = std::fs::remove_dir_all(d);
    }
}

#[test]
fn e_nasdaq_request_window_keeps_one_eastern_session_whole() {
    let dir = scratch("window");
    let day: i32 = 19_360; // 2023-01-03, an EST date: its after-hours run to 01:00 UTC on 2023-01-04
    let (u, u2) = (i64::from(day) * D, i64::from(day + 1) * D);
    let m = 60 * S;
    // Re-issued AAPL (id 40 on every date). Session D-1's last after-hours hour, session D (regular hours and the
    // after-hours up to 00:59 UTC), then session D+1.
    let prev = vec![
        ev(40, u + 30 * m, b'A', b'B', 1, 1),
        ev(40, u + 40 * m, b'C', b'B', 1, 2),
    ];
    let mut session = book(40, u + 15 * H, 3);
    session.push(ev(40, u2 + 30 * m, b'A', b'A', 4, 6));
    session.push(ev(40, u2 + 59 * m, b'C', b'A', 4, 7));
    let next = book(40, u2 + 15 * H, 8);
    let all: Vec<MboEvent> = prev.iter().chain(&session).chain(&next).copied().collect();
    let within = |a: i64, b: i64| {
        all.iter()
            .filter(|e| a <= e.ts_recv && e.ts_recv < b)
            .copied()
            .collect::<Vec<_>>()
    };
    let aapl = |d0: i32, d1: i32| vec![mapping("AAPL", &[(d0, d1, 40)])];
    // The guide's Nasdaq window for an EST date D: [D 05:00 UTC, D+1 01:00 UTC).
    let window = dir.join("window.mbo.dbn");
    Writer::create(
        &window,
        &Md::new("XNAS.ITCH", Some(Schema::Mbo), day, day + 2)
            .span_ns(u + 5 * H, u2 + H)
            .map(aapl(day, day + 2)),
    )
    .mbo(&within(u + 5 * H, u2 + H));
    let out = dir.join("window");
    run(dbn_spec(&out, &[&window], "nasdaq", json!({}))).unwrap();
    assert_eq!(records(&out), vec![(day, 40, session.clone())]);
    // A whole UTC day D: a stub of session D-1, and session D without its last after-hours hour.
    let whole = dir.join("whole.mbo.dbn");
    Writer::create(
        &whole,
        &Md::new("XNAS.ITCH", Some(Schema::Mbo), day, day + 1).map(aapl(day, day + 1)),
    )
    .mbo(&within(u, u2));
    let out = dir.join("whole");
    run(dbn_spec(&out, &[&whole], "nasdaq", json!({}))).unwrap();
    assert_eq!(
        records(&out),
        vec![(day - 1, 40, prev), (day, 40, session[..3].to_vec())]
    );
    let _ = std::fs::remove_dir_all(&dir);
}

// ---- (f) input order, contract value, gateway records, odd bytes --------------------------------------------------

/// Every permutation of `items`.
fn permutations<T: Clone>(items: &[T]) -> Vec<Vec<T>> {
    if items.len() <= 1 {
        return vec![items.to_vec()];
    }
    let mut out = Vec::new();
    for i in 0..items.len() {
        let mut rest = items.to_vec();
        let first = rest.remove(i);
        for mut p in permutations(&rest) {
            p.insert(0, first.clone());
            out.push(p);
        }
    }
    out
}

#[test]
fn f_inputs_in_any_order_give_the_same_dataset() {
    let dir = scratch("order");
    let d0: i32 = 20_020;
    let t = |k: i32| i64::from(d0 + k) * D;
    const ZN_TICK: i64 = 15_625_000; // 1/64 at 1e-9
    // Id 7 is ESZ4 on d0, ZNZ4 on d0 + 1 and ESZ4 again on d0 + 2 (an id re-used across dates), with one
    // definition file per date (a 00:00 snapshot and an 08:00 repeat) and no raw metadata (a parent request).
    let mut events = Vec::new();
    for k in 0..3 {
        events.extend(book(7, t(k) + 14 * H, 1 + 3 * k as u64));
    }
    let mbo = dir.join("mbo.dbn");
    Writer::create(
        &mbo,
        &Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 3)
            .stype_in(Some(SType::Parent))
            .map(vec![
                mapping("ES.FUT", &[(d0, d0 + 1, 7), (d0 + 2, d0 + 3, 7)]),
                mapping("ZN.FUT", &[(d0 + 1, d0 + 2, 7)]),
            ]),
    )
    .mbo(&events);
    let mut files = vec![mbo.clone()];
    for (k, (sym, tick)) in [("ESZ4", ES_TICK), ("ZNZ4", ZN_TICK), ("ESZ4", ES_TICK)]
        .into_iter()
        .enumerate()
    {
        let k = k as i32;
        let f = dir.join(format!("def-{k}.dbn"));
        Writer::create(&f, &Md::new("GLBX.MDP3", Some(Schema::Definition), d0 + k, d0 + k + 1))
            .rec(&def(7, sym, tick, t(k)).v3())
            .rec(&def(7, sym, tick, t(k) + 8 * H).v3());
        files.push(f);
    }
    let want = vec![
        (d0, 7, "ESZ4".to_string(), ES_TICK),
        (d0 + 1, 7, "ZNZ4".to_string(), ZN_TICK),
        (d0 + 2, 7, "ESZ4".to_string(), ES_TICK),
    ];
    let mut first: Option<(Vec<u8>, Vec<u8>)> = None;
    for (k, order) in permutations(&files).into_iter().enumerate() {
        let out = dir.join(format!("ds-{k}"));
        let refs: Vec<&Path> = order.iter().map(PathBuf::as_path).collect();
        let s = run(dbn_spec(&out, &refs, "cme", json!({}))).unwrap();
        assert_eq!(rows(&out), want, "{order:?}");
        assert_eq!(counter(&s, "dbn_definition_repeats"), Some(3), "{order:?}");
        let got = (
            std::fs::read(out.join("data_quality.json")).unwrap(),
            std::fs::read(out.join("instruments.parquet")).unwrap(),
        );
        match &first {
            None => first = Some(got),
            Some(f) => assert!(*f == got, "{order:?}: data_quality.json or instruments.parquet differs"),
        }
    }
    // Unit level: the same records in two orders give the same lookups and counters, including repeats of one
    // contract split over two files (days 0-2 and 3-5).
    let recs: Vec<(u32, &str, i64)> = (0..6).map(|k| (42, "ESZ4", t(k))).collect();
    let mut fwd = Symbology::new();
    let mut rev = Symbology::new();
    for (i, (id, s, ts)) in recs.iter().enumerate() {
        fwd.add_definition_record(&definition(*id, s, *ts), if i < 3 { "a" } else { "b" });
    }
    for (i, (id, s, ts)) in recs.iter().enumerate().rev() {
        rev.add_definition_record(&definition(*id, s, *ts), if i < 3 { "a" } else { "b" });
    }
    assert_eq!(fwd.stats(), rev.stats());
    assert_eq!(fwd.stats().definitions_repeated, 5);
    let mut a = Symbology::new();
    let mut b = Symbology::new();
    let reused = [(7, "AAPL", t(0)), (7, "MSFT", t(1)), (7, "AAPL", t(2))];
    for (id, s, ts) in reused {
        a.add_definition_record(&definition(id, s, ts), "x");
    }
    for k in [0, 2, 1] {
        let (id, s, ts) = reused[k];
        b.add_definition_record(&definition(id, s, ts), "x");
    }
    for sym in [&a, &b] {
        assert_eq!(
            (sym.symbol_at(7, d0), sym.symbol_at(7, d0 + 1), sym.symbol_at(7, d0 + 2)),
            (Some("AAPL"), Some("MSFT"), Some("AAPL"))
        );
        assert_eq!(sym.stats().definitions_repeated, 0);
        assert_eq!(sym.id_at("MSFT", d0 + 2), Some(7));
    }
    let _ = std::fs::remove_dir_all(&dir);
}

/// A FIFO definition with a tick, received at `ts`.
fn definition(iid: u32, symbol: &str, ts: i64) -> DbnDefinition {
    DbnDefinition {
        instrument_id: iid,
        ts_recv: ts as u64,
        ts_event: ts as u64,
        raw_symbol: symbol.into(),
        raw_instrument_id: None,
        min_price_increment: Some(ES_TICK),
        min_price_increment_amount: None,
        display_factor: None,
        min_lot_size_round_lot: None,
        instrument_class: b'F',
        match_algorithm: b'F',
        security_update_action: b'A',
        exchange: String::new(),
        asset: String::new(),
        security_type: String::new(),
        currency: String::new(),
    }
}

#[test]
fn f_contract_multiplier_and_currency_come_from_definitions() {
    let dir = scratch("value");
    let d0: i32 = 20_030;
    let t0 = i64::from(d0) * D;
    let mut events = book(42, t0 + 14 * H, 1);
    events.extend(book(44, t0 + 15 * H, 4));
    events.extend(book(45, t0 + 16 * H, 7));
    events.extend(book(46, t0 + 17 * H, 10));
    let f = dir.join("mbo.dbn");
    Writer::create(
        &f,
        &Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1).map(vec![
            mapping("ESZ4", &[(d0, d0 + 1, 42)]),
            mapping("ZNZ4", &[(d0, d0 + 1, 44)]),
            mapping("XX1", &[(d0, d0 + 1, 45)]),
            mapping("XX2", &[(d0, d0 + 1, 46)]),
        ]),
    )
    .mbo(&events);
    let defs = dir.join("defs.dbn");
    Writer::create(&defs, &Md::new("GLBX.MDP3", Some(Schema::Definition), d0, d0 + 1))
        // ES: a 0.25 tick worth $12.50; ZN: a 1/64 tick worth $15.625.
        .rec(&def(42, "ESZ4", ES_TICK, t0).value(12_500_000_000, "USD").v3())
        .rec(&def(44, "ZNZ4", 15_625_000, t0).value(15_625_000_000, "USD").v3())
        // A synthetic contract priced in euros: a 0.0001 tick worth EUR 1.25.
        .rec(&def(45, "XX1", 100_000, t0).value(1_250_000_000, "EUR").v3())
        // No tick value: the multiplier stays 1.
        .rec(&def(46, "XX2", ES_TICK, t0).v3());
    let out = dir.join("ds");
    run(dbn_spec(&out, &[&f, &defs], "cme", json!({}))).unwrap();
    let ds = Dataset::open(&out).unwrap();
    let got: Vec<(String, f64, String)> = [42u32, 44, 45, 46]
        .iter()
        .map(|id| {
            let i = ds.instrument(*id, d0).unwrap();
            (i.symbol.clone(), i.contract_multiplier, i.currency.clone())
        })
        .collect();
    assert_eq!(
        got,
        vec![
            ("ESZ4".into(), 50.0, "USD".into()),
            ("ZNZ4".into(), 1000.0, "USD".into()),
            ("XX1".into(), 12_500.0, "EUR".into()),
            ("XX2".into(), 1.0, "USD".into()),
        ]
    );
    let d = DbnReader::open(&defs)
        .unwrap()
        .next_definition_record()
        .unwrap()
        .unwrap();
    assert_eq!(
        (d.min_price_increment_amount, d.contract_multiplier()),
        (Some(12_500_000_000), Some(50.0))
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn f_non_fifo_rejection_removes_what_it_wrote_so_out_can_be_reused() {
    let dir = scratch("refit");
    let d0: i32 = 20_031;
    let t0 = i64::from(d0) * D;
    // Many ESZ4 records first (streamed to disk with a 1-row memory budget), then a pro-rata contract.
    let mut events = Vec::new();
    for k in 0..200u64 {
        events.extend(book(42, t0 + 10 * H + (k as i64) * 10 * S, 1 + 3 * k));
    }
    events.extend(book(77, t0 + 16 * H, 10_000));
    let f = dir.join("mbo.dbn");
    Writer::create(
        &f,
        &Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1).map(vec![
            mapping("ESZ4", &[(d0, d0 + 1, 42)]),
            mapping("SR3Z4", &[(d0, d0 + 1, 77)]),
        ]),
    )
    .mbo(&events);
    let defs = dir.join("defs.dbn");
    Writer::create(&defs, &Md::new("GLBX.MDP3", Some(Schema::Definition), d0, d0 + 1))
        .rec(&def(42, "ESZ4", ES_TICK, t0).v3())
        .rec(&def(77, "SR3Z4", 5_000_000, t0).algo(b'A').v3());
    let opts = json!({"options": {"memory_rows": 1}});
    let out = dir.join("ds");
    let e = run(dbn_spec(&out, &[&f, &defs], "cme", opts.clone())).unwrap_err();
    assert_eq!(e.code(), "QT-CONFIG-001", "{e}");
    assert!(
        e.to_string()
            .contains("SR3Z4 (id 77, 2024-11-04: allocation (pro-rata with a top-order allocation) 'A')"),
        "{e}"
    );
    assert!(!out.exists(), "the refused ingest removed its output");
    // An existing empty `out` is emptied again, not removed.
    let empty = dir.join("empty");
    std::fs::create_dir_all(&empty).unwrap();
    run(dbn_spec(&empty, &[&f, &defs], "cme", opts.clone())).unwrap_err();
    assert_eq!(std::fs::read_dir(&empty).unwrap().count(), 0);
    // The guide's remedy works with the same `out`.
    let mut again = opts;
    again["symbols"] = json!(["ESZ4"]);
    let s = run(dbn_spec(&out, &[&f, &defs], "cme", again)).unwrap();
    assert_eq!(s.data_quality.records, 600);
    assert_eq!(rows(&out), vec![(d0, 42, "ESZ4".into(), ES_TICK)]);
    // A non-empty `out` of another dataset is never touched.
    let e = run(dbn_spec(&out, &[&f, &defs], "cme", json!({}))).unwrap_err();
    assert!(e.to_string().contains("exists and is not empty"), "{e}");
    assert!(out.join("manifest.json").exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn f_skipped_records_are_counted_by_kind() {
    let dir = scratch("skips");
    let d0: i32 = 20_032;
    let t0 = i64::from(d0) * D;
    let events = book(42, t0 + 14 * H, 1);
    // A live capture: a symbol mapping, a definition, a subscription acknowledgement, a heartbeat (all expected),
    // then a symbol that did not resolve and a slow-reader warning (gateway errors).
    let live = dir.join("live.dbn");
    let mut m = SymbolMappingMsg {
        hd: RecordHeader::new::<SymbolMappingMsg>(rtype::SYMBOL_MAPPING, 1, 42, (t0 + 13 * H) as u64),
        stype_in: SType::RawSymbol as u8,
        stype_out: SType::RawSymbol as u8,
        start_ts: t0 as u64,
        end_ts: (t0 + D) as u64,
        ..Default::default()
    };
    set_str(&mut m.stype_in_symbol, "ESZ4");
    set_str(&mut m.stype_out_symbol, "ESZ4");
    let ts = (t0 + 13 * H) as u64;
    Writer::create(&live, &Md::new("GLBX.MDP3", None, d0, d0 + 1))
        .rec(&m)
        .rec(&def(42, "ESZ4", ES_TICK, t0 + 13 * H).v3())
        .rec(
            &SystemMsg::new(
                ts,
                Some(SystemCode::SubscriptionAck),
                "Subscription request 1 succeeded",
            )
            .unwrap(),
        )
        .rec(&SystemMsg::heartbeat(ts))
        .rec(&ErrorMsg::new(ts, None, "Failed to resolve symbol NQZ9", true))
        .rec(&SystemMsg::new(ts, Some(SystemCode::SlowReaderWarning), "Warning: slow reading").unwrap())
        .mbo(&events);
    let s = run(dbn_spec(&dir.join("live"), &[&live], "cme", json!({}))).unwrap();
    assert_eq!(counter(&s, "dbn_mixed_schema_other_records"), Some(4));
    assert_eq!(counter(&s, "dbn_gateway_error_records"), Some(2));
    assert_eq!(s.data_quality.verdict, Verdict::Warnings);
    assert_eq!(
        s.data_quality.reasons,
        vec!["dbn_gateway_error_records = 2".to_string()]
    );
    // In an mbo-schema file any other record is unexpected and raises the verdict.
    let mbo = dir.join("mbo.dbn");
    Writer::create(
        &mbo,
        &Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1).map(vec![mapping("ESZ4", &[(d0, d0 + 1, 42)])]),
    )
    .rec(&def(42, "ESZ4", ES_TICK, t0).v3())
    .mbo(&events);
    let s = run(dbn_spec(&dir.join("mbo"), &[&mbo], "cme", json!({"tick_size": "0.25"}))).unwrap();
    assert_eq!(counter(&s, "dbn_other_records_skipped"), Some(1));
    assert_eq!(counter(&s, "dbn_mixed_schema_other_records"), None);
    assert_eq!(s.data_quality.verdict, Verdict::Warnings);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn f_v1_definition_with_an_invalid_update_action_byte_is_read_safely() {
    let dir = scratch("v1action");
    let d0: i32 = 20_033;
    let t0 = i64::from(d0) * D;
    let f = dir.join("defs.v1.dbn");
    Writer::create(
        &f,
        &Md::new("GLBX.MDP3", Some(Schema::Definition), d0, d0 + 1).version(1),
    )
    .rec(&def(42, "ESZ4", ES_TICK, t0).v1());
    // DBN v1 declares security_update_action as a Rust enum; patch the stored byte to a value it does not define.
    let mut bytes = std::fs::read(&f).unwrap();
    let rec = bytes.len() - std::mem::size_of::<::dbn::v1::InstrumentDefMsg>();
    let at = rec + std::mem::offset_of!(::dbn::v1::InstrumentDefMsg, security_update_action);
    assert_eq!(bytes[at], b'A');
    bytes[at] = b'Z';
    std::fs::write(&f, &bytes).unwrap();
    let d = DbnReader::open(&f).unwrap().next_definition_record().unwrap().unwrap();
    assert_eq!(
        (d.security_update_action, d.raw_symbol.as_str(), d.min_price_increment),
        (b'?', "ESZ4", Some(ES_TICK))
    );
    // Not a delete: it applies.
    let mut sym = Symbology::new();
    sym.add_definition_record(&d, "v1");
    assert_eq!(sym.min_price_increment(42, Some("ESZ4"), d0), Some(ES_TICK));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn f_aliases_of_live_subscriptions_and_single_child_parents() {
    let d0: i32 = 20_034;
    let t0 = i64::from(d0) * D;
    // A capture subscribed to ES.FUT and ES.c.0 maps the front contract twice: both aliases select it, in any
    // order of the records; after the id maps to another contract, the earlier aliases no longer apply.
    let maps = [
        (42, "ESZ4", "ES.FUT", t0),
        (42, "ESZ4", "ES.c.0", t0 + S),
        (42, "ESH5", "ES.c.1", t0 + D),
    ];
    for order in [[0, 1, 2], [2, 1, 0]] {
        let mut sym = Symbology::new();
        for k in order {
            let (id, raw, alias, at) = maps[k];
            sym.add_symbol_mapping(id, Some(raw), Some(alias), at as u64, "live");
        }
        assert_eq!(sym.aliases_at(42, d0), vec!["ES.FUT", "ES.c.0"]);
        assert_eq!(sym.aliases_at(42, d0 + 1), vec!["ES.c.1"]);
        assert_eq!(sym.symbol_at(42, d0 + 1), Some("ESH5"));
    }
    // A parent key that maps to one contract on a date names nothing (it is a product, not a contract); a
    // continuous key with one contract does.
    let md = Md::new("GLBX.MDP3", Some(Schema::Mbo), d0, d0 + 1)
        .stype_in(None)
        .map(vec![
            mapping("ES.FUT", &[(d0, d0 + 1, 42)]),
            mapping("NQ.c.0", &[(d0, d0 + 1, 43)]),
        ])
        .build();
    let mut sym = Symbology::new();
    sym.add_metadata_from(&md, Path::new("mbo.dbn"));
    assert_eq!((sym.symbol_at(42, d0), sym.symbol_at(43, d0)), (None, Some("NQ.c.0")));
    assert_eq!(sym.aliases_at(42, d0), vec!["ES.FUT"]);
}

// ---- Databento's own definition test files (opt-in) --------------------------------------------------------------

/// Reads the definition test files of Databento's `dbn` repository (commit d368005a, DBN 0.71.0; Apache-2.0,
/// Copyright Databento, Inc.; not committed here) from `QT_DBN_DEFINITION_SAMPLES`:
///
///     QT_DBN_DEFINITION_SAMPLES=.scratch/dbn-defs \
///     cargo test -p queuetruth-ingest --test dbn_definitions -- --ignored --nocapture
///
/// Fetch them with `curl -O https://raw.githubusercontent.com/databento/dbn/<commit>/tests/data/<file>` for
/// `test_data.definition.dbn` and `test_data.definition.v{1,2,3}.dbn.zst`.
#[test]
#[ignore = "needs Databento's definition test files (QT_DBN_DEFINITION_SAMPLES)"]
fn databento_definition_test_files() {
    let Ok(dir) = std::env::var("QT_DBN_DEFINITION_SAMPLES") else {
        panic!("set QT_DBN_DEFINITION_SAMPLES to the directory of Databento's definition test files");
    };
    let dir = PathBuf::from(dir);
    let mut seen = Vec::new();
    for f in [
        "test_data.definition.dbn",
        "test_data.definition.v1.dbn.zst",
        "test_data.definition.v2.dbn.zst",
        "test_data.definition.v3.dbn.zst",
    ] {
        let p = dir.join(f);
        let mut r = DbnReader::open(&p).unwrap();
        let version = r.metadata().version;
        let mut got = Vec::new();
        while let Some(d) = r.next_definition_record().unwrap() {
            assert_eq!(d.raw_instrument_id, None, "{f}: the SBE Int32 null is undefined");
            got.push((
                d.instrument_id,
                d.raw_symbol.clone(),
                d.min_price_increment,
                d.min_lot_size_round_lot,
                d.instrument_class,
                d.match_algorithm,
                d.security_update_action,
                d.exchange.clone(),
                d.ts_recv,
            ));
        }
        eprintln!("{f}: DBN v{version}, {} definitions", got.len());
        seen.push(got);
        let mut sym = Symbology::new();
        DbnReader::open(&p).unwrap().scan_symbology(&mut sym).unwrap();
        sym.check().unwrap();
        // 2021-10-04 and 2021-10-05 (UTC dates 18904, 18905): MSFT is 6819, then 6830.
        assert_eq!(
            (sym.symbol_at(6819, 18_904), sym.symbol_at(6830, 18_905)),
            (Some("MSFT"), Some("MSFT"))
        );
        assert_eq!(
            (sym.id_at("MSFT", 18_904), sym.id_at("MSFT", 18_905)),
            (Some(6819), Some(6830))
        );
        assert_eq!(sym.min_price_increment(6819, Some("MSFT"), 18_904), None);
        assert_eq!(sym.stats().definitions_without_tick, 2);
    }
    // Every encoding holds the same two XNAS.ITCH records: no tick (UNDEF), round lot 100, stock, FIFO, add.
    for g in &seen {
        assert_eq!(g, &seen[0]);
    }
    for (g, id) in seen[0].iter().zip([6819, 6830]) {
        assert_eq!(
            (g.0, g.1.as_str(), g.2, g.3, g.4, g.5, g.6, g.7.as_str()),
            (id, "MSFT", None, Some(100), b'K', b'F', b'A', "XNAS")
        );
    }
    assert_eq!(seen[0].len(), 2);
    // An MBO day of MSFT under 6819 ingests with these definitions: symbol MSFT, the Nasdaq 0.01 tick, no warning.
    let out_dir = scratch("dbsamples");
    let t0 = 18_904 * D;
    let mbo = out_dir.join("msft.mbo.dbn");
    Writer::create(
        &mbo,
        &Md::new("XNAS.ITCH", Some(Schema::Mbo), 18_904, 18_905).map(vec![mapping("MSFT", &[(18_904, 18_905, 6819)])]),
    )
    .mbo(&book(6819, t0 + 15 * H, 1));
    let out = out_dir.join("ds");
    let s = run(dbn_spec(
        &out,
        &[&mbo, &dir.join("test_data.definition.dbn")],
        "nasdaq",
        json!({}),
    ))
    .unwrap();
    assert!(warns(&s).is_empty(), "{:?}", warns(&s));
    assert_eq!(s.data_quality.verdict, Verdict::Clean, "{:?}", s.data_quality.reasons);
    assert_eq!(rows(&out), vec![(18_904, 6819, "MSFT".into(), 10_000_000)]);
    let _ = std::fs::remove_dir_all(&out_dir);
}

/// Cost of the symbology checks at full-market scale: 10,000 symbols with a new instrument id every UTC date for
/// 252 dates (XNAS.ITCH-style metadata from two files) and, per symbol and date, a midnight-snapshot definition and
/// its pre-open repeat. Run with
/// `cargo test --release -p queuetruth-ingest --test dbn_definitions bench_ -- --ignored --nocapture`.
#[test]
#[ignore = "benchmark"]
fn bench_symbology_check_full_market_year() {
    let (nsym, ndays, d0) = (10_000u32, 252i32, 19_360i32);
    let names: Vec<String> = (0..nsym).map(|i| format!("S{i:05}")).collect();
    let t = std::time::Instant::now();
    let mut sym = Symbology::new();
    for file in 0..2 {
        let maps = names
            .iter()
            .enumerate()
            .map(|(i, s)| {
                let spans: Vec<(i32, i32, u32)> = (0..ndays)
                    .map(|k| (d0 + k, d0 + k + 1, (k as u32) * nsym + i as u32 + 1))
                    .collect();
                mapping(s, &spans)
            })
            .collect();
        let md = Md::new("XNAS.ITCH", Some(Schema::Mbo), d0, d0 + ndays)
            .map(maps)
            .build();
        sym.add_metadata_from(&md, Path::new(if file == 0 { "mbo.dbn" } else { "definition.dbn" }));
    }
    for k in 0..ndays {
        for (i, s) in names.iter().enumerate() {
            let iid = (k as u32) * nsym + i as u32 + 1;
            let day = i64::from(d0 + k) * D;
            for ts in [day, day + 8 * H] {
                let d = DbnDefinition {
                    instrument_id: iid,
                    ts_recv: ts as u64,
                    ts_event: ts as u64,
                    raw_symbol: s.clone(),
                    raw_instrument_id: Some(u64::from(i as u32 + 1)),
                    min_price_increment: None,
                    min_price_increment_amount: None,
                    display_factor: None,
                    min_lot_size_round_lot: Some(100),
                    instrument_class: b'K',
                    match_algorithm: b'F',
                    security_update_action: b'A',
                    exchange: "XNAS".into(),
                    asset: String::new(),
                    security_type: String::new(),
                    currency: String::new(),
                };
                sym.add_definition_record(&d, "definition.dbn");
            }
        }
    }
    let built = t.elapsed();
    let t = std::time::Instant::now();
    sym.check().unwrap();
    let checked = t.elapsed();
    eprintln!(
        "symbology: {} intervals, {} definitions ({} repeats): build {:.2} s, check {:.2} s",
        u64::from(nsym) * ndays as u64,
        sym.stats().definitions,
        sym.stats().definitions_repeated,
        built.as_secs_f64(),
        checked.as_secs_f64()
    );
}
