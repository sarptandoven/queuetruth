//! Row <-> Arrow `RecordBatch` codecs for every Rust-written table, plus the shadow input readers (SPEC 3.6).
//!
//! `*_to_batch` builds a batch with the canonical schema (dictionary columns always carry the full canonical
//! dictionary); `batch_to_*` checks the batch's fields against the canonical schema and decodes it, reporting
//! malformed values as `QT-INPUT-002`.
//!
//! Schema 1.1 (SPEC 3.11): decoders accept both table layouts (v0.1 columns, or v0.1 + 1.1 columns, `check_layout`)
//! and ignore the 1.1 columns; `upgrade_batch` adds the 1.1 columns to a v0.1 batch (derived or null),
//! `with_added_columns` appends caller-built 1.1 columns, and `batch_liquidity` / `batch_planned_ends` read them.

use std::path::Path;
use std::sync::Arc;

use arrow_array::builder::{Int8Builder, StringBuilder};
use arrow_array::types::Int8Type;
use arrow_array::{
    Array, ArrayRef, BooleanArray, Date32Array, DictionaryArray, Float64Array, Int8Array, Int32Array, Int64Array,
    RecordBatch, StringArray, TimestampNanosecondArray, UInt8Array, UInt16Array, UInt32Array, UInt64Array,
};
use arrow_schema::{DataType, SchemaRef};
use qt_types::{BookSide, Instrument, MboEvent, QtError, QtResult, Side, Ts, Venue};

use crate::rows::*;
use crate::tables;

// ---- building ---------------------------------------------------------------------------------------------------

fn a<T: Array + 'static>(x: T) -> ArrayRef {
    Arc::new(x)
}

fn ts<I: IntoIterator<Item = i64>>(it: I) -> ArrayRef {
    a(TimestampNanosecondArray::from_iter_values(it).with_timezone("UTC"))
}

fn ts_opt<I: IntoIterator<Item = Option<i64>>>(it: I) -> ArrayRef {
    a(TimestampNanosecondArray::from_iter(it).with_timezone("UTC"))
}

fn strs<'a, I: IntoIterator<Item = &'a str>>(it: I) -> ArrayRef {
    a(StringArray::from_iter_values(it))
}

fn strs_opt<'a, I: IntoIterator<Item = Option<&'a str>>>(it: I) -> ArrayRef {
    a(StringArray::from_iter(it))
}

fn dict_values_array(values: &[&str]) -> StringArray {
    let mut b = StringBuilder::new();
    for v in values {
        b.append_value(v);
    }
    b.finish()
}

/// Dictionary column with keys = positions in `values`; `None` = null.
fn dict_idx<I: IntoIterator<Item = Option<usize>>>(values: &[&str], it: I) -> ArrayRef {
    let mut keys = Int8Builder::new();
    for k in it {
        match k.and_then(|i| i8::try_from(i).ok()) {
            Some(i) => keys.append_value(i),
            None => keys.append_null(),
        }
    }
    let keys = keys.finish();
    let vals: ArrayRef = Arc::new(dict_values_array(values));
    match DictionaryArray::<Int8Type>::try_new(keys, vals) {
        Ok(d) => a(d),
        // Keys are positions in `values` by construction; an error here is a bug in this module.
        Err(e) => panic!("qt_schema::codec: invalid dictionary construction: {e}"),
    }
}

fn dict<E: DictEnum, I: IntoIterator<Item = E>>(it: I) -> ArrayRef {
    dict_idx(E::VALUES, it.into_iter().map(|e| Some(e.index())))
}

fn finish(name: &str, schema: SchemaRef, cols: Vec<ArrayRef>) -> RecordBatch {
    match RecordBatch::try_new(schema, cols) {
        Ok(b) => b,
        // Column types, lengths and nullability follow the canonical schema by construction.
        Err(e) => panic!("qt_schema::codec: {name}: batch does not match its schema: {e}"),
    }
}

fn canonical(name: &str) -> SchemaRef {
    match tables::schema(name) {
        Some(s) => s,
        None => panic!("qt_schema::codec: unknown table {name}"),
    }
}

fn side_byte(s: BookSide) -> u8 {
    s.as_byte()
}

// ---- reading ----------------------------------------------------------------------------------------------------

fn bad(table: &str, msg: impl std::fmt::Display) -> QtError {
    QtError::InputFormat {
        location: table.to_owned(),
        msg: msg.to_string(),
    }
}

/// Checks that a batch has the canonical fields (names, types, nullability, order) of `name`.
pub fn check_batch_schema(batch: &RecordBatch, expected: &SchemaRef, name: &str) -> QtResult<()> {
    let got = batch.schema();
    if got.fields().len() != expected.fields().len() {
        return Err(bad(
            name,
            format!(
                "expected {} columns, found {}",
                expected.fields().len(),
                got.fields().len()
            ),
        ));
    }
    for (g, e) in got.fields().iter().zip(expected.fields().iter()) {
        if g.name() != e.name() || g.data_type() != e.data_type() || g.is_nullable() != e.is_nullable() {
            return Err(bad(
                name,
                format!(
                    "column {:?}: expected {} {} {}, found {} {} {}",
                    e.name(),
                    e.name(),
                    e.data_type(),
                    if e.is_nullable() { "null" } else { "not null" },
                    g.name(),
                    g.data_type(),
                    if g.is_nullable() { "null" } else { "not null" },
                ),
            ));
        }
    }
    Ok(())
}

/// Checks that a batch has the v0.1 layout (`schema(name)`) or the full 1.1 layout (`full_schema(name)`) of `name`.
pub fn check_layout(batch: &RecordBatch, name: &str) -> QtResult<()> {
    let core = canonical(name);
    let full = tables::full_schema(name).unwrap_or_else(|| core.clone());
    let (n, lo, hi) = (batch.num_columns(), core.fields().len(), full.fields().len());
    if n > lo && n <= hi {
        // the core columns plus the first k 1.1 columns (a file written before a later 1.1 column was added,
        // e.g. `qt.zoo_predictions` with `liquidity` but without `status`)
        let prefix = Arc::new(arrow_schema::Schema::new_with_metadata(
            full.fields().iter().take(n).cloned().collect::<Vec<_>>(),
            full.metadata().clone(),
        ));
        check_batch_schema(batch, &prefix, name)
    } else {
        check_batch_schema(batch, &core, name)
    }
}

/// True when the batch carries the 1.1 columns of its table (and the table has any).
pub fn has_added_columns(batch: &RecordBatch, name: &str) -> bool {
    let added = tables::added_columns(name).unwrap_or_default();
    !added.is_empty() && added.iter().all(|c| batch.column_by_name(c).is_some())
}

struct Cols<'b> {
    batch: &'b RecordBatch,
    table: &'static str,
}

impl<'b> Cols<'b> {
    fn new(batch: &'b RecordBatch, table: &'static str) -> QtResult<Self> {
        check_layout(batch, table)?;
        Ok(Self { batch, table })
    }

    fn col<T: Array + 'static>(&self, name: &str) -> QtResult<&'b T> {
        let c = self
            .batch
            .column_by_name(name)
            .ok_or_else(|| bad(self.table, format!("missing column {name:?}")))?;
        c.as_any()
            .downcast_ref::<T>()
            .ok_or_else(|| bad(self.table, format!("column {name:?} has the wrong type")))
    }
}

macro_rules! getter {
    ($fn:ident, $opt:ident, $arr:ty, $t:ty) => {
        #[allow(dead_code)]
        fn $fn(c: &Cols<'_>, name: &str) -> QtResult<impl Fn(usize) -> QtResult<$t>> {
            let arr: &$arr = c.col(name)?;
            let table = c.table;
            let name = name.to_owned();
            Ok(move |i: usize| {
                if arr.is_null(i) {
                    Err(bad(table, format!("row {i}: column {name:?} is null")))
                } else {
                    Ok(arr.value(i))
                }
            })
        }
        #[allow(dead_code)]
        fn $opt(c: &Cols<'_>, name: &str) -> QtResult<impl Fn(usize) -> Option<$t>> {
            let arr: &$arr = c.col(name)?;
            Ok(move |i: usize| if arr.is_null(i) { None } else { Some(arr.value(i)) })
        }
    };
}

getter!(get_u8, opt_u8, UInt8Array, u8);
getter!(get_u16, opt_u16, UInt16Array, u16);
getter!(get_u32, opt_u32, UInt32Array, u32);
getter!(get_u64, opt_u64, UInt64Array, u64);
getter!(get_i8, opt_i8, Int8Array, i8);
getter!(get_i32, opt_i32, Int32Array, i32);
getter!(get_i64, opt_i64, Int64Array, i64);
getter!(get_f64, opt_f64, Float64Array, f64);
getter!(get_bool, opt_bool, BooleanArray, bool);
getter!(get_ts, opt_ts, TimestampNanosecondArray, i64);
getter!(get_date, opt_date, Date32Array, i32);

fn get_str<'b>(c: &Cols<'b>, name: &str) -> QtResult<impl Fn(usize) -> QtResult<&'b str>> {
    let arr: &'b StringArray = c.col(name)?;
    let table = c.table;
    let name = name.to_owned();
    Ok(move |i: usize| {
        if arr.is_null(i) {
            Err(bad(table, format!("row {i}: column {name:?} is null")))
        } else {
            Ok(arr.value(i))
        }
    })
}

fn opt_str<'b>(c: &Cols<'b>, name: &str) -> QtResult<impl Fn(usize) -> Option<&'b str>> {
    let arr: &'b StringArray = c.col(name)?;
    Ok(move |i: usize| if arr.is_null(i) { None } else { Some(arr.value(i)) })
}

/// Decodes a dictionary column by value string (files written by other tools may order the dictionary differently).
fn get_dict<'b>(c: &Cols<'b>, name: &str) -> QtResult<impl Fn(usize) -> QtResult<Option<&'b str>>> {
    let arr: &'b DictionaryArray<Int8Type> = c.col(name)?;
    let values = arr
        .values()
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(|| bad(c.table, format!("column {name:?}: dictionary values are not utf8")))?;
    let keys = arr.keys();
    let table = c.table;
    let name = name.to_owned();
    Ok(move |i: usize| {
        if keys.is_null(i) {
            return Ok(None);
        }
        let k = usize::try_from(keys.value(i))
            .map_err(|_| bad(table, format!("row {i}: column {name:?}: negative dictionary key")))?;
        if k >= values.len() {
            return Err(bad(
                table,
                format!("row {i}: column {name:?}: dictionary key out of range"),
            ));
        }
        Ok(Some(values.value(k)))
    })
}

fn get_enum<E: DictEnum>(c: &Cols<'_>, name: &str) -> QtResult<impl Fn(usize) -> QtResult<E>> {
    let f = get_dict(c, name)?;
    let table = c.table;
    let name = name.to_owned();
    Ok(move |i: usize| {
        let s = f(i)?.ok_or_else(|| bad(table, format!("row {i}: column {name:?} is null")))?;
        E::from_name(s).ok_or_else(|| bad(table, format!("row {i}: column {name:?}: unknown value {s:?}")))
    })
}

fn book_side(table: &str, row: usize, col: &str, b: u8) -> QtResult<BookSide> {
    BookSide::from_byte(b).ok_or_else(|| {
        bad(
            table,
            format!("row {row}: column {col:?}: side must be B or A, got {b}"),
        )
    })
}

fn side(table: &str, row: usize, col: &str, b: u8) -> QtResult<Side> {
    match b {
        b'B' | b'A' | b'N' => Ok(Side::from_byte(b)),
        _ => Err(bad(
            table,
            format!("row {row}: column {col:?}: side must be B, A or N, got {b}"),
        )),
    }
}

// ---- qt.mbo -----------------------------------------------------------------------------------------------------

/// `qt.mbo` batch from MBO events.
pub fn mbo_to_batch(rows: &[MboEvent]) -> RecordBatch {
    let n = "qt.mbo";
    finish(
        n,
        canonical(n),
        vec![
            ts(rows.iter().map(|r| r.ts_event)),
            ts(rows.iter().map(|r| r.ts_recv)),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.action))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.side))),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.price))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.size))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.order_id))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.flags))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence))),
            a(UInt16Array::from_iter_values(rows.iter().map(|r| r.channel_id))),
            a(UInt16Array::from_iter_values(rows.iter().map(|r| r.publisher_id))),
        ],
    )
}

/// MBO events from a `qt.mbo` batch.
pub fn batch_to_mbo(batch: &RecordBatch) -> QtResult<Vec<MboEvent>> {
    let c = Cols::new(batch, "qt.mbo")?;
    let (te, tr, iid) = (
        get_ts(&c, "ts_event")?,
        get_ts(&c, "ts_recv")?,
        get_u32(&c, "instrument_id")?,
    );
    let (act, sd, px, sz) = (
        get_u8(&c, "action")?,
        get_u8(&c, "side")?,
        get_i64(&c, "price")?,
        get_u64(&c, "size")?,
    );
    let (oid, fl, seq) = (get_u64(&c, "order_id")?, get_u8(&c, "flags")?, get_u64(&c, "sequence")?);
    let (ch, pb) = (get_u16(&c, "channel_id")?, get_u16(&c, "publisher_id")?);
    (0..batch.num_rows())
        .map(|i| {
            Ok(MboEvent {
                ts_event: te(i)?,
                ts_recv: tr(i)?,
                instrument_id: iid(i)?,
                action: act(i)?,
                side: sd(i)?,
                price: px(i)?,
                size: sz(i)?,
                order_id: oid(i)?,
                flags: fl(i)?,
                sequence: seq(i)?,
                channel_id: ch(i)?,
                publisher_id: pb(i)?,
            })
        })
        .collect()
}

// ---- qt.instruments ---------------------------------------------------------------------------------------------

impl DictEnum for Venue {
    const VALUES: &'static [&'static str] = &["nasdaq", "cme", "coinbase", "generic"];
    fn index(self) -> usize {
        match self {
            Venue::Nasdaq => 0,
            Venue::Cme => 1,
            Venue::Coinbase => 2,
            Venue::Generic => 3,
        }
    }
    fn from_index(i: usize) -> Option<Self> {
        [Venue::Nasdaq, Venue::Cme, Venue::Coinbase, Venue::Generic]
            .get(i)
            .copied()
    }
}

/// `qt.instruments` batch.
pub fn instruments_to_batch(rows: &[Instrument]) -> RecordBatch {
    let n = "qt.instruments";
    finish(
        n,
        canonical(n),
        vec![
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(Date32Array::from_iter_values(rows.iter().map(|r| r.session_date))),
            strs(rows.iter().map(|r| r.symbol.as_str())),
            dict(rows.iter().map(|r| r.venue)),
            strs(rows.iter().map(|r| r.source_venue.as_str())),
            a(Int8Array::from_iter_values(rows.iter().map(|r| r.price_exp))),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.tick_size))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.lot_size))),
            a(Int8Array::from_iter_values(rows.iter().map(|r| r.size_exp))),
            a(Float64Array::from_iter_values(
                rows.iter().map(|r| r.contract_multiplier),
            )),
            strs(rows.iter().map(|r| r.currency.as_str())),
            strs(rows.iter().map(|r| r.session_tz.as_str())),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.midnight_utc_ns))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.open_s))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.close_s))),
            strs(rows.iter().map(|r| r.source.as_str())),
        ],
    )
}

/// Instruments from a `qt.instruments` batch.
pub fn batch_to_instruments(batch: &RecordBatch) -> QtResult<Vec<Instrument>> {
    let c = Cols::new(batch, "qt.instruments")?;
    let (iid, sd, sym, ven) = (
        get_u32(&c, "instrument_id")?,
        get_date(&c, "session_date")?,
        get_str(&c, "symbol")?,
        get_enum(&c, "venue")?,
    );
    let (sv, pe, ts_, lot) = (
        get_str(&c, "source_venue")?,
        get_i8(&c, "price_exp")?,
        get_i64(&c, "tick_size")?,
        get_u64(&c, "lot_size")?,
    );
    let (se, cm, cur, tz) = (
        get_i8(&c, "size_exp")?,
        get_f64(&c, "contract_multiplier")?,
        get_str(&c, "currency")?,
        get_str(&c, "session_tz")?,
    );
    let (mid, op, cl, src) = (
        get_i64(&c, "midnight_utc_ns")?,
        get_u32(&c, "open_s")?,
        get_u32(&c, "close_s")?,
        get_str(&c, "source")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(Instrument {
                instrument_id: iid(i)?,
                session_date: sd(i)?,
                symbol: sym(i)?.to_owned(),
                venue: ven(i)?,
                source_venue: sv(i)?.to_owned(),
                price_exp: pe(i)?,
                tick_size: ts_(i)?,
                lot_size: lot(i)?,
                size_exp: se(i)?,
                contract_multiplier: cm(i)?,
                currency: cur(i)?.to_owned(),
                session_tz: tz(i)?.to_owned(),
                midnight_utc_ns: mid(i)?,
                open_s: op(i)?,
                close_s: cl(i)?,
                source: src(i)?.to_owned(),
            })
        })
        .collect()
}

// ---- qt.bbo / qt.depth / qt.trades / qt.rank_trace --------------------------------------------------------------

/// `qt.bbo` batch.
pub fn bbo_to_batch(rows: &[BboRow]) -> RecordBatch {
    let n = "qt.bbo";
    finish(
        n,
        canonical(n),
        vec![
            ts(rows.iter().map(|r| r.ts_event)),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence))),
            a(Int64Array::from_iter(rows.iter().map(|r| r.bid_px))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.bid_sz))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.bid_ct))),
            a(Int64Array::from_iter(rows.iter().map(|r| r.ask_px))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.ask_sz))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.ask_ct))),
        ],
    )
}

/// BBO rows from a `qt.bbo` batch.
pub fn batch_to_bbo(batch: &RecordBatch) -> QtResult<Vec<BboRow>> {
    let c = Cols::new(batch, "qt.bbo")?;
    let (te, iid, seq) = (
        get_ts(&c, "ts_event")?,
        get_u32(&c, "instrument_id")?,
        get_u64(&c, "sequence")?,
    );
    let (bp, bs, bc) = (opt_i64(&c, "bid_px")?, get_u64(&c, "bid_sz")?, get_u32(&c, "bid_ct")?);
    let (ap, as_, ac) = (opt_i64(&c, "ask_px")?, get_u64(&c, "ask_sz")?, get_u32(&c, "ask_ct")?);
    (0..batch.num_rows())
        .map(|i| {
            Ok(BboRow {
                ts_event: te(i)?,
                instrument_id: iid(i)?,
                sequence: seq(i)?,
                bid_px: bp(i),
                bid_sz: bs(i)?,
                bid_ct: bc(i)?,
                ask_px: ap(i),
                ask_sz: as_(i)?,
                ask_ct: ac(i)?,
            })
        })
        .collect()
}

/// `qt.depth` batch.
pub fn depth_to_batch(rows: &[DepthRow]) -> RecordBatch {
    let n = "qt.depth";
    finish(
        n,
        canonical(n),
        vec![
            ts(rows.iter().map(|r| r.ts_event)),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| side_byte(r.side)))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.level))),
            a(Int64Array::from_iter(rows.iter().map(|r| r.price))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.size))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.count))),
        ],
    )
}

/// Depth rows from a `qt.depth` batch.
pub fn batch_to_depth(batch: &RecordBatch) -> QtResult<Vec<DepthRow>> {
    let c = Cols::new(batch, "qt.depth")?;
    let (te, iid, seq, sd) = (
        get_ts(&c, "ts_event")?,
        get_u32(&c, "instrument_id")?,
        get_u64(&c, "sequence")?,
        get_u8(&c, "side")?,
    );
    let (lv, px, sz, ct) = (
        get_u8(&c, "level")?,
        opt_i64(&c, "price")?,
        get_u64(&c, "size")?,
        get_u32(&c, "count")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(DepthRow {
                ts_event: te(i)?,
                instrument_id: iid(i)?,
                sequence: seq(i)?,
                side: book_side(c.table, i, "side", sd(i)?)?,
                level: lv(i)?,
                price: px(i),
                size: sz(i)?,
                count: ct(i)?,
            })
        })
        .collect()
}

/// `qt.trades` batch.
pub fn trades_to_batch(rows: &[TradeRow]) -> RecordBatch {
    let n = "qt.trades";
    finish(
        n,
        canonical(n),
        vec![
            ts(rows.iter().map(|r| r.ts_event)),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence))),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.price))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.size))),
            a(UInt8Array::from_iter_values(
                rows.iter().map(|r| r.aggressor_side.as_byte()),
            )),
            dict(rows.iter().map(|r| r.kind)),
            a(UInt64Array::from_iter(rows.iter().map(|r| r.maker_order_id))),
        ],
    )
}

/// Trade rows from a `qt.trades` batch.
pub fn batch_to_trades(batch: &RecordBatch) -> QtResult<Vec<TradeRow>> {
    let c = Cols::new(batch, "qt.trades")?;
    let (te, iid, seq, px) = (
        get_ts(&c, "ts_event")?,
        get_u32(&c, "instrument_id")?,
        get_u64(&c, "sequence")?,
        get_i64(&c, "price")?,
    );
    let (sz, ag, kd, mk) = (
        get_u64(&c, "size")?,
        get_u8(&c, "aggressor_side")?,
        get_enum(&c, "kind")?,
        opt_u64(&c, "maker_order_id")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(TradeRow {
                ts_event: te(i)?,
                instrument_id: iid(i)?,
                sequence: seq(i)?,
                price: px(i)?,
                size: sz(i)?,
                aggressor_side: side(c.table, i, "aggressor_side", ag(i)?)?,
                kind: kd(i)?,
                maker_order_id: mk(i),
            })
        })
        .collect()
}

/// `qt.rank_trace` batch.
pub fn rank_trace_to_batch(rows: &[RankTraceRow]) -> RecordBatch {
    let n = "qt.rank_trace";
    finish(
        n,
        canonical(n),
        vec![
            ts(rows.iter().map(|r| r.ts_event)),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.order_id))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| side_byte(r.side)))),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.price))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.rank))),
            a(UInt32Array::from_iter_values(
                rows.iter().map(|r| r.rank_sweep_adjusted),
            )),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.level_depth))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sweep_id))),
            dict(rows.iter().map(|r| r.exec_kind)),
        ],
    )
}

/// Rank-trace rows from a `qt.rank_trace` batch.
pub fn batch_to_rank_trace(batch: &RecordBatch) -> QtResult<Vec<RankTraceRow>> {
    let c = Cols::new(batch, "qt.rank_trace")?;
    let (te, iid, seq, oid) = (
        get_ts(&c, "ts_event")?,
        get_u32(&c, "instrument_id")?,
        get_u64(&c, "sequence")?,
        get_u64(&c, "order_id")?,
    );
    let (sd, px, rk, rka) = (
        get_u8(&c, "side")?,
        get_i64(&c, "price")?,
        get_u32(&c, "rank")?,
        get_u32(&c, "rank_sweep_adjusted")?,
    );
    let (ld, sw, ek) = (
        get_u32(&c, "level_depth")?,
        get_u64(&c, "sweep_id")?,
        get_enum(&c, "exec_kind")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(RankTraceRow {
                ts_event: te(i)?,
                instrument_id: iid(i)?,
                sequence: seq(i)?,
                order_id: oid(i)?,
                side: book_side(c.table, i, "side", sd(i)?)?,
                price: px(i)?,
                rank: rk(i)?,
                rank_sweep_adjusted: rka(i)?,
                level_depth: ld(i)?,
                sweep_id: sw(i)?,
                exec_kind: ek(i)?,
            })
        })
        .collect()
}

// ---- qt.real_orders ---------------------------------------------------------------------------------------------

/// `qt.real_orders` batch.
pub fn real_orders_to_batch(rows: &[RealOrderRow]) -> RecordBatch {
    let n = "qt.real_orders";
    finish(
        n,
        canonical(n),
        vec![
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(Date32Array::from_iter_values(rows.iter().map(|r| r.session_date))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.order_id))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| side_byte(r.side)))),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.price))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.size_at_add))),
            ts(rows.iter().map(|r| r.ts_add)),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence_add))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.queue_ahead_at_add))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.rank_at_add))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.level_qty_at_add))),
            ts_opt(rows.iter().map(|r| r.ts_first_fill)),
            ts_opt(rows.iter().map(|r| r.ts_last_fill)),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.filled_qty))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.n_fills))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.n_reduces))),
            ts_opt(rows.iter().map(|r| r.ts_end)),
            dict(rows.iter().map(|r| r.end_reason)),
            a(BooleanArray::from_iter(rows.iter().map(|r| Some(r.in_snapshot)))),
        ],
    )
}

/// Real-order rows from a `qt.real_orders` batch.
pub fn batch_to_real_orders(batch: &RecordBatch) -> QtResult<Vec<RealOrderRow>> {
    let c = Cols::new(batch, "qt.real_orders")?;
    let (iid, sdt, oid, sd) = (
        get_u32(&c, "instrument_id")?,
        get_date(&c, "session_date")?,
        get_u64(&c, "order_id")?,
        get_u8(&c, "side")?,
    );
    let (px, sz, ta, sqa) = (
        get_i64(&c, "price")?,
        get_u64(&c, "size_at_add")?,
        get_ts(&c, "ts_add")?,
        get_u64(&c, "sequence_add")?,
    );
    let (qa, ra, lq) = (
        get_u64(&c, "queue_ahead_at_add")?,
        get_u32(&c, "rank_at_add")?,
        get_u64(&c, "level_qty_at_add")?,
    );
    let (tff, tlf, fq, nf) = (
        opt_ts(&c, "ts_first_fill")?,
        opt_ts(&c, "ts_last_fill")?,
        get_u64(&c, "filled_qty")?,
        get_u32(&c, "n_fills")?,
    );
    let (nr, te, er, sn) = (
        get_u32(&c, "n_reduces")?,
        opt_ts(&c, "ts_end")?,
        get_enum(&c, "end_reason")?,
        get_bool(&c, "in_snapshot")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(RealOrderRow {
                instrument_id: iid(i)?,
                session_date: sdt(i)?,
                order_id: oid(i)?,
                side: book_side(c.table, i, "side", sd(i)?)?,
                price: px(i)?,
                size_at_add: sz(i)?,
                ts_add: ta(i)?,
                sequence_add: sqa(i)?,
                queue_ahead_at_add: qa(i)?,
                rank_at_add: ra(i)?,
                level_qty_at_add: lq(i)?,
                ts_first_fill: tff(i),
                ts_last_fill: tlf(i),
                filled_qty: fq(i)?,
                n_fills: nf(i)?,
                n_reduces: nr(i)?,
                ts_end: te(i),
                end_reason: er(i)?,
                in_snapshot: sn(i)?,
            })
        })
        .collect()
}

// ---- shadows ----------------------------------------------------------------------------------------------------

/// `qt.shadow_spec` batch.
pub fn shadow_specs_to_batch(rows: &[ShadowSpec]) -> RecordBatch {
    let n = "qt.shadow_spec";
    let mode = |p: &PriceSpec| match p {
        PriceSpec::Abs(_) => PriceMode::Abs,
        PriceSpec::Touch { .. } => PriceMode::Touch,
    };
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| side_byte(r.side)))),
            dict(rows.iter().map(|r| mode(&r.price))),
            a(Int64Array::from_iter(rows.iter().map(|r| match r.price {
                PriceSpec::Abs(p) => Some(p),
                PriceSpec::Touch { .. } => None,
            }))),
            a(Int32Array::from_iter_values(rows.iter().map(|r| match r.price {
                PriceSpec::Abs(_) => 0,
                PriceSpec::Touch { offset_ticks } => offset_ticks,
            }))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.qty))),
            ts(rows.iter().map(|r| r.ts_submit)),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.latency_ns))),
            ts_opt(rows.iter().map(|r| r.ts_cancel)),
            strs_opt(rows.iter().map(|r| r.group.as_deref())),
            dict(rows.iter().map(|r| r.source)),
            strs_opt(rows.iter().map(|r| r.order_key.as_deref())),
        ],
    )
}

/// Validates an id from a file: UTF-8, 1..=128 chars of `[A-Za-z0-9_.:-]` (SPEC 0.1).
pub fn validate_id(what: &str, id: &str, location: &str) -> QtResult<()> {
    let ok = !id.is_empty()
        && id.chars().count() <= 128
        && id
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | ':' | '-'));
    if ok {
        Ok(())
    } else {
        Err(QtError::InputFormat {
            location: location.to_owned(),
            msg: format!("invalid {what} {id:?} (1..=128 chars of [A-Za-z0-9_.:-])"),
        })
    }
}

/// Shadow specs from a `qt.shadow_spec` batch (ids validated; duplicates are checked by `read_shadow_specs`).
pub fn batch_to_shadow_specs(batch: &RecordBatch) -> QtResult<Vec<ShadowSpec>> {
    let t = "qt.shadow_spec";
    let c = Cols::new(batch, t)?;
    let (sid, iid, sd, pm) = (
        get_str(&c, "shadow_id")?,
        get_u32(&c, "instrument_id")?,
        get_u8(&c, "side")?,
        get_enum(&c, "price_mode")?,
    );
    let (px, off, qty, tsub) = (
        opt_i64(&c, "price")?,
        get_i32(&c, "offset_ticks")?,
        get_u64(&c, "qty")?,
        get_ts(&c, "ts_submit")?,
    );
    let (lat, tc, grp, src, ok) = (
        get_i64(&c, "latency_ns")?,
        opt_ts(&c, "ts_cancel")?,
        opt_str(&c, "group")?,
        get_enum(&c, "source")?,
        opt_str(&c, "order_key")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            let id = sid(i)?;
            validate_id("shadow_id", id, &format!("{t} row {i}"))?;
            let price = match pm(i)? {
                PriceMode::Abs => PriceSpec::Abs(px(i).ok_or_else(|| bad(t, format!("row {i}: abs price is null")))?),
                PriceMode::Touch => PriceSpec::Touch { offset_ticks: off(i)? },
            };
            let latency_ns = lat(i)?;
            if latency_ns < 0 {
                return Err(bad(t, format!("row {i}: latency_ns must be >= 0")));
            }
            Ok(ShadowSpec {
                shadow_id: id.to_owned(),
                instrument_id: iid(i)?,
                side: book_side(t, i, "side", sd(i)?)?,
                price,
                qty: qty(i)?,
                ts_submit: tsub(i)?,
                latency_ns,
                ts_cancel: tc(i),
                group: grp(i).map(str::to_owned),
                source: src(i)?,
                order_key: ok(i).map(str::to_owned),
            })
        })
        .collect()
}

/// `qt.shadow_amend` batch.
pub fn shadow_amends_to_batch(rows: &[ShadowAmend]) -> RecordBatch {
    let n = "qt.shadow_amend";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            ts(rows.iter().map(|r| r.ts_send)),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.new_qty))),
        ],
    )
}

/// Shadow amends from a `qt.shadow_amend` batch.
pub fn batch_to_shadow_amends(batch: &RecordBatch) -> QtResult<Vec<ShadowAmend>> {
    let t = "qt.shadow_amend";
    let c = Cols::new(batch, t)?;
    let (sid, tsd, nq) = (
        get_str(&c, "shadow_id")?,
        get_ts(&c, "ts_send")?,
        get_u64(&c, "new_qty")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            let id = sid(i)?;
            validate_id("shadow_id", id, &format!("{t} row {i}"))?;
            Ok(ShadowAmend {
                shadow_id: id.to_owned(),
                ts_send: tsd(i)?,
                new_qty: nq(i)?,
            })
        })
        .collect()
}

/// `qt.shadow_results` batch.
pub fn shadow_results_to_batch(rows: &[ShadowResultRow]) -> RecordBatch {
    let n = "qt.shadow_results";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| side_byte(r.side)))),
            a(Int64Array::from_iter(rows.iter().map(|r| r.price))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.qty))),
            ts(rows.iter().map(|r| r.ts_submit)),
            ts(rows.iter().map(|r| r.ts_live)),
            ts_opt(rows.iter().map(|r| r.ts_activated_event)),
            ts_opt(rows.iter().map(|r| r.ts_cancel_effective)),
            a(UInt64Array::from_iter(rows.iter().map(|r| r.queue_ahead_at_live))),
            a(UInt32Array::from_iter(rows.iter().map(|r| r.rank_at_live))),
            a(UInt64Array::from_iter(rows.iter().map(|r| r.level_qty_at_live))),
            ts_opt(rows.iter().map(|r| r.ts_first_fill)),
            ts_opt(rows.iter().map(|r| r.ts_last_fill)),
            ts_opt(rows.iter().map(|r| r.ts_full_fill)),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.filled_qty))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.n_fills))),
            a(UInt64Array::from_iter(rows.iter().map(|r| r.queue_ahead_at_first_fill))),
            dict(rows.iter().map(|r| r.first_fill_reason)),
            dict(rows.iter().map(|r| r.end_state)),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.latency_applied_ns))),
            a(BooleanArray::from_iter(rows.iter().map(|r| Some(r.ambiguous_race)))),
            a(BooleanArray::from_iter(rows.iter().map(|r| Some(r.out_of_session)))),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.dq_flags))),
        ],
    )
}

/// Shadow results from a `qt.shadow_results` batch.
pub fn batch_to_shadow_results(batch: &RecordBatch) -> QtResult<Vec<ShadowResultRow>> {
    let c = Cols::new(batch, "qt.shadow_results")?;
    let (sid, iid, sd, px) = (
        get_str(&c, "shadow_id")?,
        get_u32(&c, "instrument_id")?,
        get_u8(&c, "side")?,
        opt_i64(&c, "price")?,
    );
    let (qty, tsub, tl, tae) = (
        get_u64(&c, "qty")?,
        get_ts(&c, "ts_submit")?,
        get_ts(&c, "ts_live")?,
        opt_ts(&c, "ts_activated_event")?,
    );
    let (tce, qal, ral, lql) = (
        opt_ts(&c, "ts_cancel_effective")?,
        opt_u64(&c, "queue_ahead_at_live")?,
        opt_u32(&c, "rank_at_live")?,
        opt_u64(&c, "level_qty_at_live")?,
    );
    let (tff, tlf, tfu, fq) = (
        opt_ts(&c, "ts_first_fill")?,
        opt_ts(&c, "ts_last_fill")?,
        opt_ts(&c, "ts_full_fill")?,
        get_u64(&c, "filled_qty")?,
    );
    let (nf, qaff, ffr, es) = (
        get_u32(&c, "n_fills")?,
        opt_u64(&c, "queue_ahead_at_first_fill")?,
        get_enum(&c, "first_fill_reason")?,
        get_enum(&c, "end_state")?,
    );
    let (lat, amb, oos, dq) = (
        get_i64(&c, "latency_applied_ns")?,
        get_bool(&c, "ambiguous_race")?,
        get_bool(&c, "out_of_session")?,
        get_u32(&c, "dq_flags")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(ShadowResultRow {
                shadow_id: sid(i)?.to_owned(),
                instrument_id: iid(i)?,
                side: book_side(c.table, i, "side", sd(i)?)?,
                price: px(i),
                qty: qty(i)?,
                ts_submit: tsub(i)?,
                ts_live: tl(i)?,
                ts_activated_event: tae(i),
                ts_cancel_effective: tce(i),
                queue_ahead_at_live: qal(i),
                rank_at_live: ral(i),
                level_qty_at_live: lql(i),
                ts_first_fill: tff(i),
                ts_last_fill: tlf(i),
                ts_full_fill: tfu(i),
                filled_qty: fq(i)?,
                n_fills: nf(i)?,
                queue_ahead_at_first_fill: qaff(i),
                first_fill_reason: ffr(i)?,
                end_state: es(i)?,
                latency_applied_ns: lat(i)?,
                ambiguous_race: amb(i)?,
                out_of_session: oos(i)?,
                dq_flags: dq(i)?,
            })
        })
        .collect()
}

/// `qt.shadow_fills` batch.
pub fn shadow_fills_to_batch(rows: &[ShadowFillRow]) -> RecordBatch {
    let n = "qt.shadow_fills";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.fill_seq))),
            ts(rows.iter().map(|r| r.ts_event)),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.fill_qty))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.cum_qty))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.leaves_qty))),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.fill_px))),
            dict(rows.iter().map(|r| r.reason)),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.queue_ahead_before))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.traded_at_px_event))),
            a(UInt64Array::from_iter_values(
                rows.iter().map(|r| r.trade_through_event),
            )),
            a(UInt64Array::from_iter_values(
                rows.iter().map(|r| r.behind_volume_executed),
            )),
        ],
    )
}

/// Shadow fills from a `qt.shadow_fills` batch.
pub fn batch_to_shadow_fills(batch: &RecordBatch) -> QtResult<Vec<ShadowFillRow>> {
    let c = Cols::new(batch, "qt.shadow_fills")?;
    let (sid, fs, te, seq) = (
        get_str(&c, "shadow_id")?,
        get_u32(&c, "fill_seq")?,
        get_ts(&c, "ts_event")?,
        get_u64(&c, "sequence")?,
    );
    let (fq, cq, lq, fp) = (
        get_u64(&c, "fill_qty")?,
        get_u64(&c, "cum_qty")?,
        get_u64(&c, "leaves_qty")?,
        get_i64(&c, "fill_px")?,
    );
    let (rs, qab, tap, ttr, bve) = (
        get_enum(&c, "reason")?,
        get_u64(&c, "queue_ahead_before")?,
        get_u64(&c, "traded_at_px_event")?,
        get_u64(&c, "trade_through_event")?,
        get_u64(&c, "behind_volume_executed")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(ShadowFillRow {
                shadow_id: sid(i)?.to_owned(),
                fill_seq: fs(i)?,
                ts_event: te(i)?,
                sequence: seq(i)?,
                fill_qty: fq(i)?,
                cum_qty: cq(i)?,
                leaves_qty: lq(i)?,
                fill_px: fp(i)?,
                reason: rs(i)?,
                queue_ahead_before: qab(i)?,
                traded_at_px_event: tap(i)?,
                trade_through_event: ttr(i)?,
                behind_volume_executed: bve(i)?,
            })
        })
        .collect()
}

/// `qt.queue_trajectory` batch.
pub fn trajectory_to_batch(rows: &[TrajectoryRow]) -> RecordBatch {
    let n = "qt.queue_trajectory";
    let u64s = |f: fn(&TrajectoryRow) -> u64| a(UInt64Array::from_iter_values(rows.iter().map(f)));
    let u32s = |f: fn(&TrajectoryRow) -> u32| a(UInt32Array::from_iter_values(rows.iter().map(f)));
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            ts(rows.iter().map(|r| r.ts_event)),
            u64s(|r| r.sequence),
            dict(rows.iter().map(|r| r.kind)),
            u64s(|r| r.queue_ahead_before),
            u64s(|r| r.queue_ahead_after),
            u32s(|r| r.rank_before),
            u32s(|r| r.rank_after),
            u64s(|r| r.level_qty_before),
            u64s(|r| r.level_qty_after),
            u64s(|r| r.cancel_ahead_qty),
            u64s(|r| r.cancel_behind_qty),
            u64s(|r| r.add_behind_qty),
            u64s(|r| r.traded_at_px_qty),
            u64s(|r| r.trade_through_qty),
            u64s(|r| r.filled_qty),
        ],
    )
}

/// Trajectory rows from a `qt.queue_trajectory` batch.
pub fn batch_to_trajectory(batch: &RecordBatch) -> QtResult<Vec<TrajectoryRow>> {
    let c = Cols::new(batch, "qt.queue_trajectory")?;
    let (sid, te, seq, kd) = (
        get_str(&c, "shadow_id")?,
        get_ts(&c, "ts_event")?,
        get_u64(&c, "sequence")?,
        get_enum(&c, "kind")?,
    );
    let (qb, qa, rb, ra) = (
        get_u64(&c, "queue_ahead_before")?,
        get_u64(&c, "queue_ahead_after")?,
        get_u32(&c, "rank_before")?,
        get_u32(&c, "rank_after")?,
    );
    let (lb, la, ca, cb) = (
        get_u64(&c, "level_qty_before")?,
        get_u64(&c, "level_qty_after")?,
        get_u64(&c, "cancel_ahead_qty")?,
        get_u64(&c, "cancel_behind_qty")?,
    );
    let (ab, tp, tt, fq) = (
        get_u64(&c, "add_behind_qty")?,
        get_u64(&c, "traded_at_px_qty")?,
        get_u64(&c, "trade_through_qty")?,
        get_u64(&c, "filled_qty")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(TrajectoryRow {
                shadow_id: sid(i)?.to_owned(),
                ts_event: te(i)?,
                sequence: seq(i)?,
                kind: kd(i)?,
                queue_ahead_before: qb(i)?,
                queue_ahead_after: qa(i)?,
                rank_before: rb(i)?,
                rank_after: ra(i)?,
                level_qty_before: lb(i)?,
                level_qty_after: la(i)?,
                cancel_ahead_qty: ca(i)?,
                cancel_behind_qty: cb(i)?,
                add_behind_qty: ab(i)?,
                traded_at_px_qty: tp(i)?,
                trade_through_qty: tt(i)?,
                filled_qty: fq(i)?,
            })
        })
        .collect()
}

// ---- qt.l2 ------------------------------------------------------------------------------------------------------

/// `qt.l2` batch.
pub fn l2_to_batch(rows: &[L2Row]) -> RecordBatch {
    let n = "qt.l2";
    finish(
        n,
        canonical(n),
        vec![
            ts(rows.iter().map(|r| r.ts_event)),
            ts(rows.iter().map(|r| r.ts_recv)),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.event_id))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.sequence))),
            dict(rows.iter().map(|r| r.kind)),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.side.as_byte()))),
            a(Int64Array::from_iter(rows.iter().map(|r| r.price))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.prev_qty))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.new_qty))),
            a(UInt32Array::from_iter(rows.iter().map(|r| r.prev_ct))),
            a(UInt32Array::from_iter(rows.iter().map(|r| r.new_ct))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.trade_qty))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.aggressor.as_byte()))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.level))),
            a(UInt8Array::from_iter_values(rows.iter().map(|r| r.flags))),
        ],
    )
}

/// L2 rows from a `qt.l2` batch.
pub fn batch_to_l2(batch: &RecordBatch) -> QtResult<Vec<L2Row>> {
    let c = Cols::new(batch, "qt.l2")?;
    let (te, tr, iid, eid) = (
        get_ts(&c, "ts_event")?,
        get_ts(&c, "ts_recv")?,
        get_u32(&c, "instrument_id")?,
        get_u64(&c, "event_id")?,
    );
    let (seq, kd, sd, px) = (
        get_u64(&c, "sequence")?,
        get_enum(&c, "kind")?,
        get_u8(&c, "side")?,
        opt_i64(&c, "price")?,
    );
    let (pq, nq, pc, nc) = (
        get_u64(&c, "prev_qty")?,
        get_u64(&c, "new_qty")?,
        opt_u32(&c, "prev_ct")?,
        opt_u32(&c, "new_ct")?,
    );
    let (tq, ag, lv, fl) = (
        get_u64(&c, "trade_qty")?,
        get_u8(&c, "aggressor")?,
        get_u8(&c, "level")?,
        get_u8(&c, "flags")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(L2Row {
                ts_event: te(i)?,
                ts_recv: tr(i)?,
                instrument_id: iid(i)?,
                event_id: eid(i)?,
                sequence: seq(i)?,
                kind: kd(i)?,
                side: side(c.table, i, "side", sd(i)?)?,
                price: px(i),
                prev_qty: pq(i)?,
                new_qty: nq(i)?,
                prev_ct: pc(i),
                new_ct: nc(i),
                trade_qty: tq(i)?,
                aggressor: side(c.table, i, "aggressor", ag(i)?)?,
                level: lv(i)?,
                flags: fl(i)?,
            })
        })
        .collect()
}

// ---- zoo --------------------------------------------------------------------------------------------------------

/// `qt.zoo_predictions` batch.
pub fn zoo_predictions_to_batch(rows: &[ZooPredictionRow]) -> RecordBatch {
    let n = "qt.zoo_predictions";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.model_id.as_str())),
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.qty))),
            a(Int64Array::from_iter_values(rows.iter().map(|r| r.horizon_ns))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.p_fill))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.e_fill_qty))),
            ts_opt(rows.iter().map(|r| r.model_ts_first_fill)),
            a(BooleanArray::from_iter(rows.iter().map(|r| r.truth_filled))),
            ts_opt(rows.iter().map(|r| r.truth_ts_first_fill)),
            a(UInt64Array::from_iter(rows.iter().map(|r| r.truth_filled_qty))),
            a(BooleanArray::from_iter(rows.iter().map(|r| Some(r.censored)))),
            a(UInt64Array::from_iter(rows.iter().map(|r| r.queue_ahead_at_live))),
            a(UInt64Array::from_iter(rows.iter().map(|r| r.level_qty_at_live))),
            a(UInt64Array::from_iter_values(
                rows.iter().map(|r| r.visible_level_qty_at_live),
            )),
            a(BooleanArray::from_iter(
                rows.iter().map(|r| Some(r.out_of_view_at_live)),
            )),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(Date32Array::from_iter_values(rows.iter().map(|r| r.session_date))),
            ts(rows.iter().map(|r| r.ts_live)),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.trade_vol_60s))),
        ],
    )
}

/// Zoo predictions from a `qt.zoo_predictions` batch.
pub fn batch_to_zoo_predictions(batch: &RecordBatch) -> QtResult<Vec<ZooPredictionRow>> {
    let c = Cols::new(batch, "qt.zoo_predictions")?;
    let (mid, sid, qty, h) = (
        get_str(&c, "model_id")?,
        get_str(&c, "shadow_id")?,
        get_u64(&c, "qty")?,
        get_i64(&c, "horizon_ns")?,
    );
    let (pf, ef, mtf, tf) = (
        get_f64(&c, "p_fill")?,
        get_f64(&c, "e_fill_qty")?,
        opt_ts(&c, "model_ts_first_fill")?,
        opt_bool(&c, "truth_filled")?,
    );
    let (ttf, tfq, cen, qal) = (
        opt_ts(&c, "truth_ts_first_fill")?,
        opt_u64(&c, "truth_filled_qty")?,
        get_bool(&c, "censored")?,
        opt_u64(&c, "queue_ahead_at_live")?,
    );
    let (lql, vl, oov, iid) = (
        opt_u64(&c, "level_qty_at_live")?,
        get_u64(&c, "visible_level_qty_at_live")?,
        get_bool(&c, "out_of_view_at_live")?,
        get_u32(&c, "instrument_id")?,
    );
    let (sd, tl, tv) = (
        get_date(&c, "session_date")?,
        get_ts(&c, "ts_live")?,
        get_u64(&c, "trade_vol_60s")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(ZooPredictionRow {
                model_id: mid(i)?.to_owned(),
                shadow_id: sid(i)?.to_owned(),
                qty: qty(i)?,
                horizon_ns: h(i)?,
                p_fill: pf(i)?,
                e_fill_qty: ef(i)?,
                model_ts_first_fill: mtf(i),
                truth_filled: tf(i),
                truth_ts_first_fill: ttf(i),
                truth_filled_qty: tfq(i),
                censored: cen(i)?,
                queue_ahead_at_live: qal(i),
                level_qty_at_live: lql(i),
                visible_level_qty_at_live: vl(i)?,
                out_of_view_at_live: oov(i)?,
                instrument_id: iid(i)?,
                session_date: sd(i)?,
                ts_live: tl(i)?,
                trade_vol_60s: tv(i)?,
            })
        })
        .collect()
}

/// `qt.zoo_fills` batch.
pub fn zoo_fills_to_batch(rows: &[ZooFillRow]) -> RecordBatch {
    let n = "qt.zoo_fills";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.model_id.as_str())),
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.fill_seq))),
            ts(rows.iter().map(|r| r.ts_event)),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.fill_qty))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.cum_qty))),
            a(Float64Array::from_iter_values(
                rows.iter().map(|r| r.queue_ahead_est_before),
            )),
            dict(rows.iter().map(|r| r.reason)),
            dict(rows.iter().map(|r| r.quirk)),
        ],
    )
}

/// Zoo fills from a `qt.zoo_fills` batch.
pub fn batch_to_zoo_fills(batch: &RecordBatch) -> QtResult<Vec<ZooFillRow>> {
    let c = Cols::new(batch, "qt.zoo_fills")?;
    let (mid, sid, fs, te) = (
        get_str(&c, "model_id")?,
        get_str(&c, "shadow_id")?,
        get_u32(&c, "fill_seq")?,
        get_ts(&c, "ts_event")?,
    );
    let (fq, cq, qe, rs, qk) = (
        get_f64(&c, "fill_qty")?,
        get_f64(&c, "cum_qty")?,
        get_f64(&c, "queue_ahead_est_before")?,
        get_enum(&c, "reason")?,
        get_enum(&c, "quirk")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(ZooFillRow {
                model_id: mid(i)?.to_owned(),
                shadow_id: sid(i)?.to_owned(),
                fill_seq: fs(i)?,
                ts_event: te(i)?,
                fill_qty: fq(i)?,
                cum_qty: cq(i)?,
                queue_ahead_est_before: qe(i)?,
                reason: rs(i)?,
                quirk: qk(i)?,
            })
        })
        .collect()
}

/// `qt.zoo_trajectory` batch.
pub fn zoo_trajectory_to_batch(rows: &[ZooTrajectoryRow]) -> RecordBatch {
    let n = "qt.zoo_trajectory";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.model_id.as_str())),
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            ts(rows.iter().map(|r| r.ts_event)),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.queue_ahead_est))),
            a(UInt64Array::from_iter_values(rows.iter().map(|r| r.queue_ahead_exact))),
        ],
    )
}

/// Zoo trajectory rows from a `qt.zoo_trajectory` batch.
pub fn batch_to_zoo_trajectory(batch: &RecordBatch) -> QtResult<Vec<ZooTrajectoryRow>> {
    let c = Cols::new(batch, "qt.zoo_trajectory")?;
    let (mid, sid, te) = (
        get_str(&c, "model_id")?,
        get_str(&c, "shadow_id")?,
        get_ts(&c, "ts_event")?,
    );
    let (qe, qx) = (get_f64(&c, "queue_ahead_est")?, get_u64(&c, "queue_ahead_exact")?);
    (0..batch.num_rows())
        .map(|i| {
            Ok(ZooTrajectoryRow {
                model_id: mid(i)?.to_owned(),
                shadow_id: sid(i)?.to_owned(),
                ts_event: te(i)?,
                queue_ahead_est: qe(i)?,
                queue_ahead_exact: qx(i)?,
            })
        })
        .collect()
}

// ---- calibration ------------------------------------------------------------------------------------------------

/// `qt.alloc_events` batch.
pub fn alloc_events_to_batch(rows: &[AllocEventRow]) -> RecordBatch {
    let n = "qt.alloc_events";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
            a(Date32Array::from_iter_values(rows.iter().map(|r| r.session_date))),
            ts(rows.iter().map(|r| r.ts_event)),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.front))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.back))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.ahead_cancel))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.behind_cancel))),
            a(Float64Array::from_iter(rows.iter().map(|r| r.l2_prev))),
            a(Float64Array::from_iter(rows.iter().map(|r| r.l2_new))),
            a(Float64Array::from_iter(rows.iter().map(|r| r.l2_trades))),
        ],
    )
}

/// Allocation events from a `qt.alloc_events` batch.
pub fn batch_to_alloc_events(batch: &RecordBatch) -> QtResult<Vec<AllocEventRow>> {
    let c = Cols::new(batch, "qt.alloc_events")?;
    let (sid, iid, sd, te) = (
        get_str(&c, "shadow_id")?,
        get_u32(&c, "instrument_id")?,
        get_date(&c, "session_date")?,
        get_ts(&c, "ts_event")?,
    );
    let (fr, bk, ac, bc) = (
        get_f64(&c, "front")?,
        get_f64(&c, "back")?,
        get_f64(&c, "ahead_cancel")?,
        get_f64(&c, "behind_cancel")?,
    );
    let (lp, ln, lt) = (
        opt_f64(&c, "l2_prev")?,
        opt_f64(&c, "l2_new")?,
        opt_f64(&c, "l2_trades")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(AllocEventRow {
                shadow_id: sid(i)?.to_owned(),
                instrument_id: iid(i)?,
                session_date: sd(i)?,
                ts_event: te(i)?,
                front: fr(i)?,
                back: bk(i)?,
                ahead_cancel: ac(i)?,
                behind_cancel: bc(i)?,
                l2_prev: lp(i),
                l2_new: ln(i),
                l2_trades: lt(i),
            })
        })
        .collect()
}

/// `qt.touch_records` batch.
pub fn touch_records_to_batch(rows: &[TouchRecordRow]) -> RecordBatch {
    let n = "qt.touch_records";
    finish(
        n,
        canonical(n),
        vec![
            strs(rows.iter().map(|r| r.shadow_id.as_str())),
            ts(rows.iter().map(|r| r.ts_event)),
            a(UInt32Array::from_iter_values(rows.iter().map(|r| r.touch_index))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.nt_front_before))),
            a(Float64Array::from_iter_values(rows.iter().map(|r| r.nt_excess))),
            a(BooleanArray::from_iter(rows.iter().map(|r| Some(r.truth_filled_now)))),
            a(BooleanArray::from_iter(
                rows.iter().map(|r| Some(r.truth_first_fill_here)),
            )),
        ],
    )
}

/// Touch records from a `qt.touch_records` batch.
pub fn batch_to_touch_records(batch: &RecordBatch) -> QtResult<Vec<TouchRecordRow>> {
    let c = Cols::new(batch, "qt.touch_records")?;
    let (sid, te, ti) = (
        get_str(&c, "shadow_id")?,
        get_ts(&c, "ts_event")?,
        get_u32(&c, "touch_index")?,
    );
    let (nf, nx, tfn, tfh) = (
        get_f64(&c, "nt_front_before")?,
        get_f64(&c, "nt_excess")?,
        get_bool(&c, "truth_filled_now")?,
        get_bool(&c, "truth_first_fill_here")?,
    );
    (0..batch.num_rows())
        .map(|i| {
            Ok(TouchRecordRow {
                shadow_id: sid(i)?.to_owned(),
                ts_event: te(i)?,
                touch_index: ti(i)?,
                nt_front_before: nf(i)?,
                nt_excess: nx(i)?,
                truth_filled_now: tfn(i)?,
                truth_first_fill_here: tfh(i)?,
            })
        })
        .collect()
}

// ---- qt.features (dynamic) --------------------------------------------------------------------------------------

/// `qt.features` batch in the full 1.1 layout (`features_full_schema`) with one float64 column per name in
/// `feature_names` (each row's `values` must align).
pub fn features_to_batch(feature_names: &[&str], rows: &[FeatureRow]) -> QtResult<RecordBatch> {
    let schema = tables::features_full_schema(feature_names)?;
    if let Some((i, r)) = rows
        .iter()
        .enumerate()
        .find(|(_, r)| r.values.len() != feature_names.len())
    {
        return Err(QtError::Engine(format!(
            "features_to_batch: row {i} ({}) has {} values for {} feature names",
            r.order_ref,
            r.values.len(),
            feature_names.len()
        )));
    }
    let mut cols: Vec<ArrayRef> = vec![
        strs(rows.iter().map(|r| r.order_ref.as_str())),
        dict(rows.iter().map(|r| r.population)),
        a(UInt32Array::from_iter_values(rows.iter().map(|r| r.instrument_id))),
        a(Date32Array::from_iter_values(rows.iter().map(|r| r.session_date))),
        ts(rows.iter().map(|r| r.ts_landmark)),
        a(Int64Array::from_iter_values(rows.iter().map(|r| r.landmark_ns))),
        a(Int8Array::from_iter_values(rows.iter().map(|r| r.side_sign))),
    ];
    for j in 0..feature_names.len() {
        cols.push(a(Float64Array::from_iter(
            rows.iter().map(|r| r.values.get(j).copied().flatten()),
        )));
    }
    cols.push(a(Int64Array::from_iter_values(rows.iter().map(|r| r.event_ns))));
    cols.push(dict(rows.iter().map(|r| r.cause)));
    cols.push(a(Int64Array::from_iter(rows.iter().map(|r| r.midnight_utc_ns))));
    RecordBatch::try_new(schema, cols)
        .map_err(|e| QtError::Engine(format!("features_to_batch: batch does not match its schema: {e}")))
}

/// Feature names and rows from a `qt.features` batch in the v0.1 or the 1.1 layout (feature columns = every column
/// between `side_sign` and `event_ns`; `midnight_utc_ns` is `None` in a v0.1 batch).
pub fn batch_to_features(batch: &RecordBatch) -> QtResult<(Vec<String>, Vec<FeatureRow>)> {
    let t = "qt.features";
    let sch = batch.schema();
    let n = sch.fields().len();
    let (names, full) = tables::features_columns(&sch).ok_or_else(|| {
        let fixed = tables::columns(t).map_or(0, |c| c.iter().filter(|c| c.since.is_none()).count());
        bad(t, format!("expected at least {fixed} columns, found {n}"))
    })?;
    let refs: Vec<&str> = names.iter().map(String::as_str).collect();
    let expected = if full {
        tables::features_full_schema(&refs)?
    } else {
        tables::features_schema(&refs)?
    };
    check_batch_schema(batch, &expected, t)?;
    for f in &sch.fields()[tables::FEATURES_INSERT_AT..tables::FEATURES_INSERT_AT + names.len()] {
        if f.data_type() != &DataType::Float64 {
            return Err(bad(t, format!("feature column {:?} must be float64", f.name())));
        }
    }
    let c = Cols { batch, table: t };
    let (oref, pop, iid, sd) = (
        get_str(&c, "order_ref")?,
        get_enum::<Population>(&c, "population")?,
        get_u32(&c, "instrument_id")?,
        get_date(&c, "session_date")?,
    );
    let (tl, lns, ss, ens, cause) = (
        get_ts(&c, "ts_landmark")?,
        get_i64(&c, "landmark_ns")?,
        get_i8(&c, "side_sign")?,
        get_i64(&c, "event_ns")?,
        get_enum::<Cause>(&c, "cause")?,
    );
    let mid = if full {
        Some(opt_i64(&c, "midnight_utc_ns")?)
    } else {
        None
    };
    let mut value_cols: Vec<&Float64Array> = Vec::with_capacity(names.len());
    for name in &names {
        value_cols.push(c.col::<Float64Array>(name)?);
    }
    let rows = (0..batch.num_rows())
        .map(|i| {
            Ok(FeatureRow {
                order_ref: oref(i)?.to_owned(),
                population: pop(i)?,
                instrument_id: iid(i)?,
                session_date: sd(i)?,
                ts_landmark: tl(i)?,
                landmark_ns: lns(i)?,
                side_sign: ss(i)?,
                values: value_cols
                    .iter()
                    .map(|a| if a.is_null(i) { None } else { Some(a.value(i)) })
                    .collect(),
                event_ns: ens(i)?,
                cause: cause(i)?,
                midnight_utc_ns: mid.as_ref().and_then(|m| m(i)),
            })
        })
        .collect::<QtResult<Vec<_>>>()?;
    Ok((names, rows))
}

// ---- shadow input files -----------------------------------------------------------------------------------------

#[derive(serde::Deserialize)]
struct EtShadowCsvRow {
    shadow_id: String,
    instrument_id: u32,
    side: String,
    price: i64,
    qty: u64,
    ts_submit: i64,
    latency_ns: i64,
    #[serde(default)]
    ts_cancel: Option<i64>,
}

fn is_csv(path: &Path) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("csv"))
}

fn check_duplicates<'a>(ids: impl Iterator<Item = &'a str>) -> QtResult<()> {
    let mut seen = std::collections::HashSet::new();
    for id in ids {
        if !seen.insert(id) {
            return Err(QtError::Duplicate {
                what: "shadow_id".into(),
                id: id.to_owned(),
            });
        }
    }
    Ok(())
}

fn read_et_shadows_csv(path: &Path) -> QtResult<Vec<ShadowSpec>> {
    let loc = path.display().to_string();
    let mut rdr = csv::ReaderBuilder::new()
        .trim(csv::Trim::All)
        .from_path(path)
        .map_err(|e| QtError::InputMissing {
            path: loc.clone(),
            msg: e.to_string(),
        })?;
    let mut out = Vec::new();
    for (i, row) in rdr.deserialize::<EtShadowCsvRow>().enumerate() {
        let r = row.map_err(|e| QtError::InputFormat {
            location: format!("{loc} row {}", i + 1),
            msg: e.to_string(),
        })?;
        let rloc = format!("{loc} row {}", i + 1);
        validate_id("shadow_id", &r.shadow_id, &rloc)?;
        let side = match r.side.to_ascii_lowercase().as_str() {
            "b" | "bid" | "buy" => BookSide::Bid,
            "a" | "ask" | "sell" => BookSide::Ask,
            other => {
                return Err(QtError::InputFormat {
                    location: rloc,
                    msg: format!("unknown side {other:?}"),
                });
            }
        };
        if r.latency_ns < 0 {
            return Err(QtError::InputFormat {
                location: rloc,
                msg: "latency_ns must be >= 0".into(),
            });
        }
        out.push(ShadowSpec {
            shadow_id: r.shadow_id,
            instrument_id: r.instrument_id,
            side,
            price: PriceSpec::Abs(r.price),
            qty: r.qty,
            ts_submit: r.ts_submit,
            latency_ns: r.latency_ns,
            ts_cancel: r.ts_cancel,
            group: None,
            source: ShadowSource::User,
            order_key: None,
        });
    }
    Ok(out)
}

/// Reads shadow specs from a `qt.shadow_spec` Parquet file, or from a CSV with et's `shadows.csv` header
/// (`shadow_id,instrument_id,side,price,qty,ts_submit,latency_ns,ts_cancel`, any column order, `ts_cancel` optional,
/// extra columns ignored; price mode `abs`, source `user`). Duplicate ids are `QT-INPUT-003`.
pub fn read_shadow_specs(path: &Path) -> QtResult<Vec<ShadowSpec>> {
    let specs = if is_csv(path) {
        read_et_shadows_csv(path)?
    } else {
        let mut out = Vec::new();
        for batch in crate::io::read_table_as(path, "qt.shadow_spec")? {
            out.extend(batch_to_shadow_specs(&batch?)?);
        }
        out
    };
    check_duplicates(specs.iter().map(|s| s.shadow_id.as_str()))?;
    Ok(specs)
}

/// Reads shadow amends from a `qt.shadow_amend` Parquet file (ids validated; ordering rules are checked by the
/// engine against the specs).
pub fn read_shadow_amends(path: &Path) -> QtResult<Vec<ShadowAmend>> {
    let mut out = Vec::new();
    for batch in crate::io::read_table_as(path, "qt.shadow_amend")? {
        out.extend(batch_to_shadow_amends(&batch?)?);
    }
    Ok(out)
}

// ---- schema 1.1 columns -----------------------------------------------------------------------------------------

fn liquidity_from_reasons(table: &'static str, batch: &RecordBatch) -> QtResult<ArrayRef> {
    let c = Cols { batch, table };
    let reason = get_dict(&c, "reason")?;
    let mut out = Vec::with_capacity(batch.num_rows());
    for i in 0..batch.num_rows() {
        let liq = match reason(i)? {
            Some(r) => match table {
                "qt.shadow_fills" => FillReason::from_name(r).map(FillReason::liquidity),
                _ => ZooFillReason::from_name(r).map(ZooFillReason::liquidity),
            }
            .ok_or_else(|| bad(table, format!("row {i}: column \"reason\": unknown value {r:?}")))?,
            None => return Err(bad(table, format!("row {i}: column \"reason\" is null"))),
        };
        out.push(Some(liq.index()));
    }
    Ok(dict_idx(Liquidity::VALUES, out))
}

/// A batch in the v0.1 layout of `name` -> the full 1.1 layout (a full batch is returned unchanged). Added columns:
/// `liquidity` of `qt.shadow_fills` / `qt.zoo_fills` derived from `reason` (`FillReason::liquidity`); other nullable
/// columns null; a non-nullable column that is not derivable (`qt.shadow_results.planned_end_kind`,
/// `qt.score_fills.side`) is `QT-ENGINE-001` (use `with_added_columns`).
pub fn upgrade_batch(name: &str, batch: &RecordBatch) -> QtResult<RecordBatch> {
    check_layout(batch, name)?;
    let full = tables::full_schema(name).ok_or_else(|| QtError::Engine(format!("unknown table {name}")))?;
    if batch.num_columns() == full.fields().len() {
        return RecordBatch::try_new(full, batch.columns().to_vec())
            .map_err(|e| QtError::Engine(format!("{name}: {e}")));
    }
    let mut cols = batch.columns().to_vec();
    for f in &full.fields()[batch.num_columns()..] {
        let col = match (name, f.name().as_str()) {
            ("qt.shadow_fills", "liquidity") => liquidity_from_reasons("qt.shadow_fills", batch)?,
            ("qt.zoo_fills", "liquidity") => liquidity_from_reasons("qt.zoo_fills", batch)?,
            _ if f.is_nullable() => arrow_array::new_null_array(f.data_type(), batch.num_rows()),
            (_, col) => {
                return Err(QtError::Engine(format!(
                    "{name}.{col}: not derivable from the v0.1 columns; build it with with_added_columns"
                )));
            }
        };
        cols.push(col);
    }
    RecordBatch::try_new(full, cols).map_err(|e| QtError::Engine(format!("{name}: {e}")))
}

/// A v0.1 batch of `name` plus caller-built 1.1 columns (in `added_columns(name)` order) -> the full layout.
pub fn with_added_columns(name: &str, batch: &RecordBatch, added: Vec<ArrayRef>) -> QtResult<RecordBatch> {
    check_batch_schema(batch, &canonical(name), name)?;
    let full = tables::full_schema(name).ok_or_else(|| QtError::Engine(format!("unknown table {name}")))?;
    let mut cols = batch.columns().to_vec();
    cols.extend(added);
    RecordBatch::try_new(full, cols).map_err(|e| QtError::Engine(format!("{name}: {e}")))
}

/// The 1.1 `qt.zoo_predictions.status` column: `hbt_312_abort` where `aborted`, else null.
pub fn zoo_prediction_status_column<I: IntoIterator<Item = bool>>(aborted: I) -> ArrayRef {
    dict_idx(
        tables::ZOO_PREDICTION_STATUS,
        aborted.into_iter().map(|a| a.then_some(0)),
    )
}

/// A 1.1 `liquidity` column (`maker`/`taker`; `None` = null, allowed only in `qt.zoo_predictions`).
pub fn liquidity_column<I: IntoIterator<Item = Option<Liquidity>>>(values: I) -> ArrayRef {
    dict_idx(Liquidity::VALUES, values.into_iter().map(|v| v.map(Liquidity::index)))
}

/// `(ts_planned_end, planned_end_kind)` of one shadow (1.1 `qt.shadow_results` columns).
pub type PlannedEndCols = (Option<Ts>, PlannedEndKind);

/// The 1.1 `qt.shadow_results` columns `ts_planned_end`, `planned_end_kind` (for `with_added_columns`).
pub fn planned_end_columns(planned: &[PlannedEndCols]) -> Vec<ArrayRef> {
    vec![ts_opt(planned.iter().map(|p| p.0)), dict(planned.iter().map(|p| p.1))]
}

/// The `liquidity` column of a `qt.shadow_fills`, `qt.zoo_fills` or `qt.zoo_predictions` batch; `None` for a v0.1
/// batch (for fills: derive it with `FillReason::liquidity`).
pub fn batch_liquidity(batch: &RecordBatch, name: &'static str) -> QtResult<Option<Vec<Option<Liquidity>>>> {
    check_layout(batch, name)?;
    if !has_added_columns(batch, name) || batch.column_by_name("liquidity").is_none() {
        return Ok(None);
    }
    let c = Cols { batch, table: name };
    let f = get_dict(&c, "liquidity")?;
    (0..batch.num_rows())
        .map(|i| {
            f(i)?
                .map(|s| {
                    Liquidity::from_name(s)
                        .ok_or_else(|| bad(name, format!("row {i}: column \"liquidity\": unknown value {s:?}")))
                })
                .transpose()
        })
        .collect::<QtResult<Vec<_>>>()
        .map(Some)
}

/// `(ts_planned_end, planned_end_kind)` of a `qt.shadow_results` batch; `None` for a v0.1 batch.
pub fn batch_planned_ends(batch: &RecordBatch) -> QtResult<Option<Vec<PlannedEndCols>>> {
    let t = "qt.shadow_results";
    check_layout(batch, t)?;
    if !has_added_columns(batch, t) {
        return Ok(None);
    }
    let c = Cols { batch, table: t };
    let (pe, kind) = (
        opt_ts(&c, "ts_planned_end")?,
        get_enum::<PlannedEndKind>(&c, "planned_end_kind")?,
    );
    (0..batch.num_rows())
        .map(|i| Ok((pe(i), kind(i)?)))
        .collect::<QtResult<Vec<_>>>()
        .map(Some)
}

/// A row type with a fixed table and a codec pair.
pub trait TableRow: Sized {
    /// Table name (`qt.<name>`).
    const TABLE: &'static str;
    /// Encodes rows as a record batch with the canonical schema.
    fn to_batch(rows: &[Self]) -> RecordBatch;
    /// Decodes a record batch with the canonical schema.
    fn from_batch(batch: &RecordBatch) -> QtResult<Vec<Self>>;
}

macro_rules! table_row {
    ($t:ty, $name:literal, $to:ident, $from:ident) => {
        impl TableRow for $t {
            const TABLE: &'static str = $name;
            fn to_batch(rows: &[Self]) -> RecordBatch {
                $to(rows)
            }
            fn from_batch(batch: &RecordBatch) -> QtResult<Vec<Self>> {
                $from(batch)
            }
        }
    };
}

table_row!(MboEvent, "qt.mbo", mbo_to_batch, batch_to_mbo);
table_row!(Instrument, "qt.instruments", instruments_to_batch, batch_to_instruments);
table_row!(BboRow, "qt.bbo", bbo_to_batch, batch_to_bbo);
table_row!(DepthRow, "qt.depth", depth_to_batch, batch_to_depth);
table_row!(TradeRow, "qt.trades", trades_to_batch, batch_to_trades);
table_row!(RankTraceRow, "qt.rank_trace", rank_trace_to_batch, batch_to_rank_trace);
table_row!(
    RealOrderRow,
    "qt.real_orders",
    real_orders_to_batch,
    batch_to_real_orders
);
table_row!(
    ShadowSpec,
    "qt.shadow_spec",
    shadow_specs_to_batch,
    batch_to_shadow_specs
);
table_row!(
    ShadowAmend,
    "qt.shadow_amend",
    shadow_amends_to_batch,
    batch_to_shadow_amends
);
table_row!(
    ShadowResultRow,
    "qt.shadow_results",
    shadow_results_to_batch,
    batch_to_shadow_results
);
table_row!(
    ShadowFillRow,
    "qt.shadow_fills",
    shadow_fills_to_batch,
    batch_to_shadow_fills
);
table_row!(
    TrajectoryRow,
    "qt.queue_trajectory",
    trajectory_to_batch,
    batch_to_trajectory
);
table_row!(L2Row, "qt.l2", l2_to_batch, batch_to_l2);
table_row!(
    ZooPredictionRow,
    "qt.zoo_predictions",
    zoo_predictions_to_batch,
    batch_to_zoo_predictions
);
table_row!(ZooFillRow, "qt.zoo_fills", zoo_fills_to_batch, batch_to_zoo_fills);
table_row!(
    ZooTrajectoryRow,
    "qt.zoo_trajectory",
    zoo_trajectory_to_batch,
    batch_to_zoo_trajectory
);
table_row!(
    AllocEventRow,
    "qt.alloc_events",
    alloc_events_to_batch,
    batch_to_alloc_events
);
table_row!(
    TouchRecordRow,
    "qt.touch_records",
    touch_records_to_batch,
    batch_to_touch_records
);
