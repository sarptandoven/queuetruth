//! PyO3 extension module `queuetruth._core` (abi3-py311, SPEC 4.3): one function per native operation (spec JSON in,
//! summary JSON out), the shared CLI grammar, Arrow interchange through the Arrow PyCapsule interface (`arrow_ffi`),
//! the interactive `Session`, and `IexSplit`, the incremental IEX DEEP+ splitter of `scripts/scale/iex_split.py`.
//! Every call that does work releases the GIL; Rust errors raise `QtCoreError(code, message)` with `.code` and
//! `.message`, re-raised by `queuetruth.api` as `queuetruth.QtError`.

use std::path::Path;
use std::sync::{Mutex, MutexGuard};

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use qt_cli::ops;
use qt_ingest::iex::split::{SplitError, Splitter};
use qt_schema::specs::{
    CalibrationSpec, ExportSpec, FeatureSpec, IngestSpec, MirrorSpec, ReplaySpec, ShadowSampleSpec, WhySpec, ZooSpec,
};
use qt_types::{BookSide, QtError, QtResult, Venue};
use serde_json::Value;

mod arrow_ffi;

create_exception!(
    _core,
    QtCoreError,
    PyException,
    "Error raised by the Rust core: args are (code, message); attributes .code and .message."
);
create_exception!(
    _core,
    CliExit,
    PyException,
    "Early CLI exit: args are (code, text, to_stderr); attributes .code, .text and .to_stderr."
);

fn core_err(e: &QtError) -> PyErr {
    coded_err(e.code(), e.message())
}

/// `QtCoreError(code, message)` with `.code` and `.message` set.
fn coded_err(code: &str, message: String) -> PyErr {
    let code = code.to_owned();
    let err = QtCoreError::new_err((code.clone(), message.clone()));
    Python::attach(|py| {
        let v = err.value(py);
        let _ = v.setattr("code", code);
        let _ = v.setattr("message", message);
    });
    err
}

fn to_py<T>(r: QtResult<T>) -> PyResult<T> {
    r.map_err(|e| core_err(&e))
}

fn json_out<T: serde::Serialize>(v: &T) -> QtResult<String> {
    serde_json::to_string(v).map_err(|e| QtError::Engine(format!("summary serialization: {e}")))
}

/// Engine version (the Rust workspace version).
#[pyfunction]
fn version() -> &'static str {
    qt_schema::ENGINE_VERSION
}

/// Every Arrow table name, in definition order.
#[pyfunction]
fn schema_names() -> Vec<&'static str> {
    qt_schema::schema_names().to_vec()
}

/// The source format registry as JSON: one object per `IngestSpec.format`, in CLI order (`qt_cli::formats`).
#[pyfunction]
fn formats() -> String {
    qt_cli::formats::to_json().to_string()
}

/// The Arrow schema of a table as a `pyarrow.Schema`.
#[pyfunction(name = "arrow_schema")]
fn table_schema(py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
    let schema = qt_schema::schema(name).ok_or_else(|| PyKeyError::new_err(format!("unknown table {name:?}")))?;
    Ok(arrow_ffi::schema_to_pyarrow(py, schema)?.unbind())
}

/// The zoo family registry (`qt_zoo::FAMILIES`) as a JSON list, one object per family in canonical order.
#[pyfunction]
fn zoo_families() -> String {
    qt_zoo::families_json().to_string()
}

/// The prediction-market family registry (`qt_zoo::pm::PM_FAMILIES`) as a JSON list, in canonical order.
#[pyfunction]
fn pm_families() -> String {
    qt_zoo::pm::pm_families_json().to_string()
}

/// One order of `pm_queue_run`, read from a `queuetruth.pm.queue.SimOrder` by attribute.
#[derive(FromPyObject)]
struct PmOrderArg {
    key: String,
    side: i64,
    price: i64,
    qty: f64,
    ts_live: Option<i64>,
    entry_seq: Option<i64>,
    inside: bool,
    ts_end: Option<i64>,
    end_seq: Option<i64>,
    end_inside: bool,
    post_only: bool,
    amends: Vec<(i64, f64)>,
    samples: Vec<i64>,
    probes: Vec<i64>,
}

/// `int64` values from native-endian bytes.
fn i64s(b: &[u8], what: &str) -> PyResult<Vec<i64>> {
    if !b.len().is_multiple_of(8) {
        return Err(core_err(&QtError::Config(format!(
            "{what}: {} bytes is not a whole number of int64",
            b.len()
        ))));
    }
    Ok(b.chunks_exact(8)
        .map(|c| i64::from_ne_bytes(c.try_into().expect("8 bytes")))
        .collect())
}

/// `f64` values from native-endian bytes (`numpy.ndarray.tobytes()` of a float64 array).
fn f64s(b: &[u8], what: &str) -> PyResult<Vec<f64>> {
    if !b.len().is_multiple_of(8) {
        return Err(core_err(&QtError::Config(format!(
            "{what}: {} bytes is not a whole number of f64",
            b.len()
        ))));
    }
    Ok(b.chunks_exact(8)
        .map(|c| f64::from_ne_bytes(c.try_into().expect("8 bytes")))
        .collect())
}

/// One per-order result of `pm_queue_run`: (activated_ts, activated_seq, visible_at_live, end, end_ts, fills per
/// model, samples, probes, samples_before_live, samples_after_end).
type PmResultTuple = (
    Option<i64>,
    Option<i64>,
    Option<f64>,
    &'static str,
    Option<i64>,
    Vec<Vec<qt_zoo::pm::Fill>>,
    Vec<(i64, Vec<f64>)>,
    Vec<(i64, Vec<f64>)>,
    usize,
    usize,
);

/// Runs the prediction-market queue engine (`qt_zoo::pm::run`): `models` as (family, n, atomic), `columns` the
/// int64 bytes of ts_event, ts_recv, event_id, sequence, kind, side, price, new_qty, trade_qty, flags, part;
/// `orders` the `SimOrder`s. Returns one `PmResultTuple` per order.
#[pyfunction]
#[pyo3(signature = (models, lot, columns, orders, gaps, end_ts=None))]
fn pm_queue_run(
    py: Python<'_>,
    models: Vec<(String, Option<f64>, bool)>,
    lot: f64,
    columns: Vec<Bound<'_, PyBytes>>,
    orders: Vec<PmOrderArg>,
    gaps: Vec<(i64, i64)>,
    end_ts: Option<i64>,
) -> PyResult<Vec<PmResultTuple>> {
    let models = to_py(
        models
            .iter()
            .map(|(f, n, a)| qt_zoo::pm::Model::new(f, *n, *a))
            .collect::<QtResult<Vec<_>>>(),
    )?;
    if columns.len() != 11 {
        return Err(core_err(&QtError::Config(format!(
            "pm_queue_run: 11 columns expected, got {}",
            columns.len()
        ))));
    }
    let mut cols = Vec::with_capacity(11);
    for c in &columns {
        cols.push(i64s(c.as_bytes(), "pm_queue_run")?);
    }
    let mut it = cols.into_iter();
    let mut next = || it.next().unwrap_or_default();
    let rows = qt_zoo::pm::Rows {
        ts_event: next(),
        ts_recv: next(),
        event_id: next(),
        sequence: next(),
        kind: next(),
        side: next(),
        price: next(),
        new_qty: next(),
        trade_qty: next(),
        flags: next(),
        part: next(),
    };
    let orders: Vec<qt_zoo::pm::SimOrder> = orders
        .into_iter()
        .map(|o| qt_zoo::pm::SimOrder {
            key: o.key,
            side: o.side,
            price: o.price,
            qty: o.qty,
            ts_live: o.ts_live,
            entry_seq: o.entry_seq,
            inside: o.inside,
            ts_end: o.ts_end,
            end_seq: o.end_seq,
            end_inside: o.end_inside,
            post_only: o.post_only,
            amends: o.amends,
            samples: o.samples,
            probes: o.probes,
        })
        .collect();
    let res = to_py(py.detach(|| qt_zoo::pm::run(&models, lot, &rows, &orders, &gaps, end_ts)))?;
    Ok(res
        .into_iter()
        .map(|r| {
            (
                r.activated_ts,
                r.activated_seq,
                r.visible_at_live,
                r.end,
                r.end_ts,
                r.fills,
                r.samples,
                r.probes,
                r.samples_before_live,
                r.samples_after_end,
            )
        })
        .collect())
}

/// C2 log-likelihood of one order under a model's fill curve (`qt_calib::live::interval_loglik`): `p_by_h` the
/// float64 bytes of `P(fill by tau_j)`, `k` the 1-based truth interval, `delta` whether it filled in it.
#[pyfunction]
fn calib_interval_loglik(p_by_h: &[u8], k: usize, delta: bool, n_smooth: u64) -> PyResult<f64> {
    let p = f64s(p_by_h, "calib_interval_loglik")?;
    to_py(qt_calib::live::interval_loglik(&p, k, delta, n_smooth))
}

/// The order-by-grid log-likelihood matrix of `calibrate-live` (`qt_calib::live::loglik_matrix`): `p` the float64
/// bytes of an `orders x grid x J` array, `present` one flag per (order, grid value); returns the float64 bytes of
/// the `orders x grid` matrix.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn calib_live_loglik_matrix<'py>(
    py: Python<'py>,
    p: &[u8],
    present: Vec<bool>,
    n_orders: usize,
    n_grid: usize,
    k: Vec<usize>,
    delta: Vec<bool>,
    n_smooth: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    let p = f64s(p, "calib_live_loglik_matrix")?;
    let m = to_py(py.detach(|| qt_calib::live::loglik_matrix(&p, &present, n_orders, n_grid, &k, &delta, n_smooth)))?;
    let bytes: Vec<u8> = m.iter().flat_map(|x| x.to_ne_bytes()).collect();
    Ok(PyBytes::new(py, &bytes))
}

/// Parses argv with the shared clap grammar; returns the `CliCommand` JSON or raises `CliExit`.
#[pyfunction]
fn cli_parse(py: Python<'_>, argv: Vec<String>) -> PyResult<String> {
    match qt_cli::parse(&argv) {
        Ok(cmd) => to_py(json_out(&cmd)),
        Err(x) => {
            let err = CliExit::new_err((x.code, x.text.clone(), x.to_stderr));
            let v = err.value(py);
            v.setattr("code", x.code)?;
            v.setattr("text", x.text)?;
            v.setattr("to_stderr", x.to_stderr)?;
            Err(err)
        }
    }
}

/// Routes a command line before it is parsed (`qt_cli::route`): (argv as the commands run it, deprecation line or
/// None).
#[pyfunction]
fn cli_route(argv: Vec<String>) -> (Vec<String>, Option<String>) {
    let r = qt_cli::route(&argv);
    (r.argv, r.deprecation)
}

/// Runs a native CLI command: (summary JSON, gate_failed).
#[pyfunction]
fn cli_run_native(py: Python<'_>, cmd_json: &str) -> PyResult<(String, bool)> {
    let cmd: qt_cli::CliCommand = serde_json::from_str(cmd_json)
        .map_err(|e| core_err(&QtError::Usage(format!("cli_run_native: invalid command JSON: {e}"))))?;
    to_py(py.detach(|| {
        let out = qt_cli::run_native(&cmd)?;
        Ok((out.summary.to_string(), out.gate_failed))
    }))
}

fn run_spec<S, T>(
    py: Python<'_>,
    spec_json: &str,
    what: &str,
    f: impl Fn(&S) -> QtResult<T> + Send + Sync,
) -> PyResult<String>
where
    S: serde::de::DeserializeOwned + Send,
    T: serde::Serialize,
{
    to_py(py.detach(|| {
        let s: S = ops::spec_str(spec_json, what)?;
        json_out(&f(&s)?)
    }))
}

/// `ingest` (IngestSpec JSON -> qt.ingest_summary JSON).
#[pyfunction]
fn ingest(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<IngestSpec, _>(py, spec_json, "ingest", ops::ingest)
}

/// `replay` (ReplaySpec JSON -> qt.replay_summary JSON); `options_json` (`fill_rule`, `hidden_print_policy`,
/// `exclude_order_ids`) replaces the spec's own fields of those names before the run, so they enter `run_id`
/// (E38 OC-34).
#[pyfunction]
#[pyo3(signature = (spec_json, options_json = None))]
fn replay(py: Python<'_>, spec_json: &str, options_json: Option<&str>) -> PyResult<String> {
    to_py(py.detach(|| {
        let s: ReplaySpec = ops::spec_str(spec_json, "replay")?;
        let extra = match options_json {
            Some(t) => serde_json::from_str::<Value>(t)
                .map_err(|e| QtError::Config(format!("replay options: invalid JSON: {e}")))?,
            None => Value::Null,
        };
        let s = ops::fold_replay_options(&s, &extra)?;
        let opts = ops::merged_replay_options(&s, &Value::Null)?;
        json_out(&qt_replay::run_dataset_with(&s, &opts)?)
    }))
}

/// `shadows sample` (ShadowSampleSpec JSON -> qt.shadow_sample_summary JSON).
#[pyfunction]
fn sample_shadows(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<ShadowSampleSpec, _>(py, spec_json, "sample_shadows", qt_replay::sample_shadows)
}

/// `shadows mirror-real` (MirrorSpec JSON -> qt.shadow_sample_summary JSON).
#[pyfunction]
fn mirror_real_orders(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<MirrorSpec, _>(py, spec_json, "mirror_real_orders", qt_replay::mirror_real_orders)
}

/// `zoo`, Rust part (ZooSpec JSON -> qt.zoo_summary JSON with an empty leaderboard).
#[pyfunction]
fn run_zoo(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<ZooSpec, _>(py, spec_json, "run_zoo", qt_zoo::run_zoo)
}

/// `calibrate`, Rust part (CalibrationSpec JSON -> fit.json qt.calibration_result JSON).
#[pyfunction]
fn calibrate(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<CalibrationSpec, _>(py, spec_json, "calibrate", qt_calib::calibrate)
}

/// `features` (FeatureSpec JSON -> qt.feature_summary JSON).
#[pyfunction]
fn extract_features(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<FeatureSpec, _>(py, spec_json, "extract_features", qt_features::extract)
}

/// `why` (WhySpec JSON with shadows + shadow_id -> qt.why_trace JSON); with `live` + `order_key` as well, that
/// live order's own MBO records are removed from the tape first (A1; `queuetruth.api.why` builds the schedule).
#[pyfunction]
fn why(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<WhySpec, _>(py, spec_json, "why", qt_replay::why)
}

/// `exclude_order_ids` of a dataset (`{"path": FILE}` or a list of ids / `{instrument_id, order_id}`; bare ids
/// apply to every instrument) -> JSON list of `{instrument_id, order_id}` (the `ZooSpec` / `ReplaySpec` item form).
#[pyfunction]
fn resolve_order_ids(py: Python<'_>, value_json: &str, dataset: &str) -> PyResult<String> {
    to_py(py.detach(|| {
        let v: Value = serde_json::from_str(value_json)
            .map_err(|e| QtError::Config(format!("exclude_order_ids: invalid JSON: {e}")))?;
        let ds = qt_schema::specs::DatasetRef::path(dataset);
        json_out(&ops::resolve_exclude_order_ids(&v, &ds)?)
    }))
}

/// `export l2-npz` (ExportSpec JSON -> qt.export_summary JSON).
#[pyfunction]
fn export_l2_npz(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_spec::<ExportSpec, _>(py, spec_json, "export_l2_npz", ops::export_l2_npz)
}

/// `referee fixtures` (output directory -> qt.fixtures_summary JSON).
#[pyfunction]
fn referee_fixtures(py: Python<'_>, out: &str) -> PyResult<String> {
    to_py(py.detach(|| json_out(&ops::referee_fixtures(out)?)))
}

/// `fixtures synth` (output directory, venue `cme`|`nasdaq`, sessions -> qt.fixtures_summary JSON).
#[pyfunction]
#[pyo3(signature = (out, venue = "cme", sessions = 2))]
fn fixtures_synth(py: Python<'_>, out: &str, venue: &str, sessions: u32) -> PyResult<String> {
    let v = match venue {
        "cme" => Venue::Cme,
        "nasdaq" => Venue::Nasdaq,
        other => {
            return Err(core_err(&QtError::Config(format!(
                "venue: expected cme or nasdaq, got {other:?}"
            ))));
        }
    };
    to_py(py.detach(|| json_out(&ops::fixtures_synth(out, v, sessions)?)))
}

fn run_value(py: Python<'_>, spec_json: &str, what: &str, f: fn(&Value) -> QtResult<Value>) -> PyResult<String> {
    to_py(py.detach(|| {
        let v: Value =
            serde_json::from_str(spec_json).map_err(|e| QtError::Config(format!("{what}: invalid JSON: {e}")))?;
        json_out(&f(&v)?)
    }))
}

/// Liquidity (`maker` | `taker`) of a `qt.shadow_fills` reason.
#[pyfunction]
fn fill_liquidity(reason: &str) -> PyResult<&'static str> {
    to_py(ops::fill_liquidity(reason))
}

/// L2 view synthesis (`{"dataset": {"path"}, "out", "threads"?, "l2"?}` -> `{"files": [...]}`).
#[pyfunction]
fn synthesize_l2(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_value(py, spec_json, "synthesize_l2", ops::synthesize_l2)
}

/// BBO and trades of an L2-only dataset (`{"dataset": {"path"}, "out", "threads"?}` -> `{"files": [...]}`).
#[pyfunction]
fn bbo_from_l2(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    run_value(py, spec_json, "bbo_from_l2", ops::bbo_from_l2)
}

/// `run_id` of a fully defaulted spec (JSON) with local reference resolution (SPEC 7.3).
#[pyfunction]
fn run_id(py: Python<'_>, spec_json: &str) -> PyResult<String> {
    to_py(py.detach(|| {
        let v: Value =
            serde_json::from_str(spec_json).map_err(|e| QtError::Config(format!("run_id: invalid JSON: {e}")))?;
        ops::run_id(&v)
    }))
}

fn parse_json(text: &str, what: &str) -> QtResult<Value> {
    serde_json::from_str(text).map_err(|e| QtError::Config(format!("{what}: invalid JSON: {e}")))
}

/// Resolver over refs resolved by the caller: `refs_json` is `[[ref, sha256], ...]`, matched by canonical JSON.
fn ref_table(refs_json: &str) -> QtResult<Vec<(String, String)>> {
    let refs: Vec<(Value, String)> = serde_json::from_str(refs_json)
        .map_err(|e| QtError::Config(format!("run_id: refs must be [[ref, sha256], ...]: {e}")))?;
    refs.into_iter()
        .map(|(r, h)| Ok((qt_schema::canonical_json(&r)?, h)))
        .collect()
}

fn with_refs<T>(
    spec_json: &str,
    refs_json: &str,
    f: impl Fn(&Value, &dyn Fn(&Value) -> QtResult<String>) -> QtResult<T>,
) -> QtResult<T> {
    let spec = parse_json(spec_json, "run_id")?;
    let table = ref_table(refs_json)?;
    let resolve = |r: &Value| -> QtResult<String> {
        let key = qt_schema::canonical_json(r)?;
        table
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, h)| h.clone())
            .ok_or_else(|| QtError::Config(format!("run_id: no resolved sha256 for ref {key}")))
    };
    f(&spec, &resolve)
}

/// Identity document of `run_id` (SPEC 7.3: execution-only fields and defaulted 1.1 fields removed, refs replaced by
/// `{"sha256": ...}`) with the refs resolved by the caller (`[[ref, sha256], ...]`).
#[pyfunction]
fn identity_document(spec_json: &str, engine_version: &str, refs_json: &str) -> PyResult<String> {
    to_py(with_refs(spec_json, refs_json, |s, r| {
        qt_schema::identity_document(s, engine_version, r)
    }))
}

/// `run_id` (first 16 hex chars of the sha256 of the identity document) with the refs resolved by the caller.
#[pyfunction]
fn run_id_resolved(spec_json: &str, engine_version: &str, refs_json: &str) -> PyResult<String> {
    to_py(with_refs(spec_json, refs_json, |s, r| {
        qt_schema::run_id(s, engine_version, r)
    }))
}

/// The spec without the 1.1 fields that hold their default (`qt_schema::V1_1_DEFAULTS`).
#[pyfunction]
fn drop_v1_1_defaults(spec_json: &str) -> PyResult<String> {
    to_py(parse_json(spec_json, "drop_v1_1_defaults").and_then(|v| json_out(&qt_schema::drop_v1_1_defaults(&v)?)))
}

/// `qt_schema::V1_1_DEFAULTS` as JSON `[[discriminating key, [path...], default], ...]`.
#[pyfunction]
fn v1_1_defaults() -> PyResult<String> {
    let rows: QtResult<Vec<Value>> = qt_schema::V1_1_DEFAULTS
        .iter()
        .map(|(k, path, d)| Ok(serde_json::json!([k, path, parse_json(d, "V1_1_DEFAULTS")?])))
        .collect();
    to_py(rows.and_then(|r| json_out(&r)))
}

/// `qt_types::stable_seed(run_seed, parts)`: FNV-1a 64 over the seed and the length-prefixed parts, then splitmix64.
#[pyfunction]
fn stable_seed(run_seed: u64, parts: Vec<Vec<u8>>) -> u64 {
    let refs: Vec<&[u8]> = parts.iter().map(Vec::as_slice).collect();
    qt_types::stable_seed(run_seed, &refs)
}

/// One `qt_types::splitmix64` step.
#[pyfunction]
fn splitmix64(x: u64) -> u64 {
    qt_types::splitmix64(x)
}

fn calibration(cal_json: &str) -> QtResult<qt_schema::json::CalibrationResult> {
    serde_json::from_str(cal_json).map_err(|e| QtError::Config(format!("trust: invalid calibration_result: {e}")))
}

/// The export rule of `qt_calib::trust::assess` on a `qt.calibration_result` (JSON): `{flags, horizons, fitted_id,
/// default_id, reasons}`, flags and reasons as `[token, text]` pairs.
#[pyfunction]
fn trust_assess(py: Python<'_>, cal_json: &str) -> PyResult<String> {
    to_py(py.detach(|| json_out(&qt_calib::trust::assess(&calibration(cal_json)?)?)))
}

/// `qt_calib::trust::annotate` on a `qt.calibration_result` (JSON): `{"warnings": [...], "trust": {...}}`.
#[pyfunction]
fn trust_annotate(py: Python<'_>, cal_json: &str) -> PyResult<String> {
    to_py(py.detach(|| {
        let r = qt_calib::trust::annotate(&calibration(cal_json)?)?;
        json_out(&serde_json::json!({"warnings": r.warnings, "trust": r.trust}))
    }))
}

/// Reads a queuetruth Parquet table (schema checked against `qt.schema`) as a `pyarrow.Table`.
#[pyfunction]
#[pyo3(signature = (path, expect = None))]
fn read_table(py: Python<'_>, path: &str, expect: Option<&str>) -> PyResult<Py<PyAny>> {
    let (batches, schema) = to_py(py.detach(|| {
        let reader = match expect {
            Some(name) => qt_schema::io::read_table_as(Path::new(path), name)?,
            None => qt_schema::io::read_table(Path::new(path))?,
        };
        let schema = reader.schema().clone();
        let batches = reader.collect::<QtResult<Vec<_>>>()?;
        Ok((batches, schema))
    }))?;
    Ok(arrow_ffi::table_to_pyarrow(py, batches, schema)?.unbind())
}

/// Writes any Arrow stream object (`__arrow_c_stream__`) whose schema metadata names a queuetruth table, with the
/// Parquet settings of SPEC 0.1; returns the file entry JSON `{name, rows, bytes, sha256}`.
#[pyfunction]
fn write_table(py: Python<'_>, table: Bound<'_, PyAny>, path: &str) -> PyResult<String> {
    let (batches, schema) = arrow_ffi::stream_from_python(&table)?;
    let name = schema
        .metadata()
        .get("qt.schema")
        .cloned()
        .ok_or_else(|| PyValueError::new_err("write_table: schema lacks qt.schema metadata"))?;
    to_py(py.detach(|| {
        let mut w = qt_schema::io::TableWriter::create(Path::new(path), &name)?;
        for b in &batches {
            w.write(b)?;
        }
        json_out(&w.finish()?)
    }))
}

/// Interactive replay session over a dataset: `advance_to(ts)`, `bbo(inst)`, `depth(inst, n)`,
/// `queue_ahead(inst, side, px)`.
#[pyclass(module = "queuetruth._core", frozen)]
struct Session {
    inner: Mutex<qt_replay::Session>,
}

fn book_side(side: &str) -> PyResult<BookSide> {
    match side {
        "B" | "b" | "bid" | "buy" => Ok(BookSide::Bid),
        "A" | "a" | "ask" | "sell" => Ok(BookSide::Ask),
        other => Err(PyValueError::new_err(format!(
            "side: expected 'B' or 'A', got {other:?}"
        ))),
    }
}

impl Session {
    fn with<T>(&self, f: impl FnOnce(&mut qt_replay::Session) -> T) -> PyResult<T> {
        let mut g = self
            .inner
            .lock()
            .map_err(|_| core_err(&QtError::Engine("Session: poisoned lock".into())))?;
        Ok(f(&mut g))
    }
}

type Level = (i64, u64, u32);

#[pymethods]
impl Session {
    #[new]
    #[pyo3(signature = (dataset, venue = None))]
    fn new(py: Python<'_>, dataset: &str, venue: Option<&str>) -> PyResult<Self> {
        let v = venue.map(str::parse::<Venue>).transpose().map_err(|e| core_err(&e))?;
        let s = to_py(py.detach(|| qt_replay::Session::open(Path::new(dataset), v)))?;
        Ok(Self { inner: Mutex::new(s) })
    }

    /// Replays every record with `ts_event <= ts`.
    fn advance_to(&self, py: Python<'_>, ts: i64) -> PyResult<()> {
        let r = py.detach(|| self.with(|s| s.advance_to(ts)))?;
        to_py(r)
    }

    /// Replays whole events up to `ts` (complete events whose records are all at or before `ts`; the end of the data
    /// also applies and ends an unterminated tail).
    fn advance_events_to(&self, py: Python<'_>, ts: i64) -> PyResult<()> {
        let r = py.detach(|| self.with(|s| s.advance_events_to(ts)))?;
        to_py(r)
    }

    /// `{"bid": (price, qty, count) | None, "ask": ...}` or None before the instrument's first record.
    fn bbo<'py>(&self, py: Python<'py>, instrument_id: u32) -> PyResult<Option<Bound<'py, PyDict>>> {
        let Some(b) = self.with(|s| s.bbo(instrument_id))? else {
            return Ok(None);
        };
        let d = PyDict::new(py);
        d.set_item("bid", b.bid.map(|l| (l.price, l.total, l.count)))?;
        d.set_item("ask", b.ask.map(|l| (l.price, l.total, l.count)))?;
        Ok(Some(d))
    }

    /// Top `n` levels `(bids, asks)`, each `[(price, qty, count)]` best first.
    fn depth(&self, instrument_id: u32, n: usize) -> PyResult<(Vec<Level>, Vec<Level>)> {
        let (b, a) = self.with(|s| s.depth(instrument_id, n))?;
        let conv = |v: Vec<qt_book::LevelView>| v.into_iter().map(|l| (l.price, l.total, l.count)).collect();
        Ok((conv(b), conv(a)))
    }

    /// Displayed `(qty, count)` at a price (queue ahead of a new order there), or None before data.
    fn queue_ahead(&self, instrument_id: u32, side: &str, price: i64) -> PyResult<Option<(u64, u32)>> {
        let side = book_side(side)?;
        self.with(|s| s.queue_ahead(instrument_id, side, price))
    }

    /// A session that also takes shadow orders, from a replay spec (JSON; its `shadows` and `amends` must be unset):
    /// every partition engine is configured as `replay` configures it.
    #[staticmethod]
    fn from_spec(py: Python<'_>, spec_json: &str) -> PyResult<Self> {
        let s = to_py(py.detach(|| {
            let spec: ReplaySpec =
                serde_json::from_str(spec_json).map_err(|e| QtError::Config(format!("session spec: {e}")))?;
            qt_replay::Session::open_replay(&spec)
        }))?;
        Ok(Self { inner: Mutex::new(s) })
    }

    /// Every record with `ts_event` at or before this was applied.
    #[getter]
    fn position(&self) -> PyResult<i64> {
        self.with(|s| s.position())
    }

    /// Sends the shadows of a `qt.shadow_spec` table (any `__arrow_c_stream__` object), each with `ts_submit` at or
    /// after the position, in row order.
    fn submit(&self, py: Python<'_>, shadows: Bound<'_, PyAny>) -> PyResult<()> {
        let (batches, _) = arrow_ffi::stream_from_python(&shadows)?;
        let r = py.detach(|| {
            // decode everything first, then submit all or none
            let mut specs = Vec::new();
            for b in &batches {
                match qt_schema::codec::batch_to_shadow_specs(b) {
                    Ok(v) => specs.extend(v),
                    Err(e) => return Ok(Err(e)),
                }
            }
            self.with(|s| s.submit_all(specs))
        })?;
        to_py(r)
    }

    /// Sends a cancel at `ts` (at or after the position) for a shadow without one.
    fn cancel(&self, py: Python<'_>, shadow_id: &str, ts: i64) -> PyResult<()> {
        let r = py.detach(|| self.with(|s| s.cancel(shadow_id, ts)))?;
        to_py(r)
    }

    /// Sends a priority-keeping reduction to `new_qty` at `ts` (at or after the position).
    fn amend(&self, py: Python<'_>, shadow_id: &str, ts: i64, new_qty: u64) -> PyResult<()> {
        let r = py.detach(|| self.with(|s| s.amend(shadow_id, ts, new_qty)))?;
        to_py(r)
    }

    /// Each shadow as a replay ending now would report it: a dict per id with `status` (`pending`, `live`,
    /// `done`), the `qt.shadow_results` columns the state determines (`end_state` None until done), `ts_end`,
    /// `queue_ahead` and `rank`.
    fn shadow_states<'py>(&self, py: Python<'py>, shadow_ids: Vec<String>) -> PyResult<Vec<Bound<'py, PyDict>>> {
        let peeks = to_py(self.with(|s| {
            shadow_ids
                .iter()
                .map(|id| s.shadow_peek(id))
                .collect::<QtResult<Vec<_>>>()
        })?)?;
        let mut out = Vec::with_capacity(peeks.len());
        for p in peeks {
            let r = &p.result;
            let d = PyDict::new(py);
            let status = match p.status {
                qt_replay::ShadowStatus::Pending => "pending",
                qt_replay::ShadowStatus::Live => "live",
                qt_replay::ShadowStatus::Done => "done",
            };
            d.set_item("shadow_id", &r.shadow_id)?;
            d.set_item("status", status)?;
            let done = p.status == qt_replay::ShadowStatus::Done;
            d.set_item("end_state", done.then(|| r.end_state.to_string()))?;
            d.set_item("price", r.price)?;
            d.set_item("qty", r.qty)?;
            d.set_item("ts_submit", r.ts_submit)?;
            d.set_item("ts_live", r.ts_live)?;
            d.set_item("ts_activated_event", r.ts_activated_event)?;
            d.set_item("ts_cancel_effective", r.ts_cancel_effective)?;
            d.set_item("ts_first_fill", r.ts_first_fill)?;
            d.set_item("ts_last_fill", r.ts_last_fill)?;
            d.set_item("ts_full_fill", r.ts_full_fill)?;
            d.set_item("filled_qty", r.filled_qty)?;
            d.set_item("n_fills", r.n_fills)?;
            d.set_item("out_of_session", r.out_of_session)?;
            d.set_item("ts_end", p.ts_end)?;
            d.set_item("queue_ahead", p.queue_ahead)?;
            d.set_item("rank", p.rank)?;
            out.push(d);
        }
        Ok(out)
    }

    /// The shadow fills since the last call, as a `qt.shadow_fills` pyarrow.Table.
    fn take_fills(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let (b, n) = to_py(self.with(|s| s.pending_fills())?)?;
        let schema = b.schema();
        let t = arrow_ffi::table_to_pyarrow(py, vec![b], schema)?.unbind();
        self.with(|s| s.consume_fills(n))?; // only once the table exists: a failed hand-off loses no fill
        Ok(t)
    }

    /// The results of the shadows that ended since the last call, as a `qt.shadow_results` pyarrow.Table.
    fn take_results(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let (b, n) = to_py(self.with(|s| s.pending_results())?)?;
        let schema = b.schema();
        let t = arrow_ffi::table_to_pyarrow(py, vec![b], schema)?.unbind();
        self.with(|s| s.consume_results(n))?;
        Ok(t)
    }

    /// Replays to the end of the data and ends the shadows still open (their results join `take_results`).
    fn finish(&self, py: Python<'_>) -> PyResult<()> {
        let r = py.detach(|| self.with(qt_replay::Session::finish))?;
        to_py(r)
    }
}

/// Incremental IEX DEEP+ symbol splitter, the fast engine of `scripts/scale/iex_split.py` (`qt_ingest::iex::split`,
/// a port of its Python engine): `IexSplit(k, midnight_utc_ns, symbols=None, patterns=None)`; `feed(bytes)` routes
/// the complete blocks received so far with the GIL released and returns `[(bucket, pcap records)]` to append after
/// the pcap file header; `stats()` returns the counters and maps of the Python `IexSplitter`. An input the Python
/// engine refuses raises `QtCoreError` `QT-INPUT-002` with its message, and so does every later `feed`.
#[pyclass(module = "queuetruth._core", frozen)]
struct IexSplit {
    inner: Mutex<Splitter>,
}

fn split_err(e: &SplitError) -> PyErr {
    coded_err("QT-INPUT-002", e.to_string())
}

impl IexSplit {
    fn lock(&self) -> Result<MutexGuard<'_, Splitter>, SplitError> {
        self.inner
            .lock()
            .map_err(|_| SplitError::Unsupported("IexSplit: poisoned lock".into()))
    }
}

/// (name, bucket, instrument id, messages, executions, test issue).
type SplitSymbol = (String, u32, u32, u64, u64, bool);
/// (name, [(ns since midnight, status, reason)]).
type TradingStatus = (String, Vec<(i128, String, String)>);

#[pymethods]
impl IexSplit {
    #[new]
    #[pyo3(signature = (k, midnight_utc_ns, symbols = None, patterns = None))]
    fn new(
        k: usize,
        midnight_utc_ns: i128,
        symbols: Option<Vec<String>>,
        patterns: Option<Vec<Vec<u8>>>,
    ) -> PyResult<Self> {
        let s =
            Splitter::new(k, midnight_utc_ns, symbols, patterns).map_err(|e| PyValueError::new_err(e.to_string()))?;
        Ok(Self { inner: Mutex::new(s) })
    }

    /// Routes the next bytes of the capture; returns `[(bucket, bytes)]`, buckets ascending, for the buckets with
    /// new records.
    fn feed<'py>(&self, py: Python<'py>, data: &[u8]) -> PyResult<Vec<(usize, Bound<'py, PyBytes>)>> {
        py.detach(|| self.lock()?.feed(data)).map_err(|e| split_err(&e))?;
        let mut g = self.lock().map_err(|e| split_err(&e))?;
        let mut parts = Vec::new();
        for (b, out) in g.outputs_mut().iter_mut().enumerate() {
            if !out.is_empty() {
                parts.push((b, PyBytes::new(py, out)));
                out.clear();
            }
        }
        Ok(parts)
    }

    /// `{"counters": {name: n}, "skipped_packets": [(reason, n)], "other_protocol": [(protocol id, n)],
    /// "unknown_types": [(type byte, n)], "sessions": [(channel, session, first_seq)], "first_send_ns",
    /// "last_send_ns", "system_events": [(code, ns since midnight)], "symbols": [(name, bucket, id, messages,
    /// executions, test_issue)], "trading_status": [(name, [(ns since midnight, status, reason)])],
    /// "bucket_messages", "bucket_segments", "capture_format", "pending", "blocks": [(block type, n)]}`, every list
    /// in order of first appearance.
    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let g = self.lock().map_err(|e| split_err(&e))?;
        let st = g.state();
        let c = &st.counters;
        let counters = PyDict::new(py);
        for (name, v) in [
            ("packets", c.packets),
            ("bad_segments", c.bad_segments),
            ("segments", c.segments),
            ("heartbeats", c.heartbeats),
            ("messages", c.messages),
            ("sequence_gaps", c.sequence_gaps),
            ("duplicate_messages", c.duplicate_messages),
            ("prefiltered", c.prefiltered),
            ("parsed", c.parsed),
            ("malformed", c.malformed),
            ("system", c.system),
            ("routed", c.routed),
            ("dropped", c.dropped),
            ("unknown_type", c.unknown_type),
        ] {
            counters.set_item(name, v)?;
        }
        counters.set_item("sequence_gap_messages", c.sequence_gap_messages)?;
        let symbols: Vec<SplitSymbol> = st
            .symbols
            .iter()
            .map(|s| (s.name.clone(), s.bucket, s.id, s.messages, s.executions, s.test_issue))
            .collect();
        let trading: Vec<TradingStatus> = st
            .trading_status_order
            .iter()
            .map(|&i| {
                let s = &st.symbols[i as usize];
                let acts = s
                    .trading_status
                    .iter()
                    .map(|(ts, status, reason)| (*ts, char::from(*status).to_string(), reason.clone()))
                    .collect();
                (s.name.clone(), acts)
            })
            .collect();
        let sessions: Vec<(u32, u32, i64)> = st
            .sessions
            .iter()
            .map(|s| (s.channel, s.session, s.first_seq))
            .collect();
        let events: Vec<(String, i128)> = st
            .system_events
            .iter()
            .map(|(k, v)| (char::from(*k).to_string(), *v))
            .collect();
        let d = PyDict::new(py);
        d.set_item("counters", counters)?;
        d.set_item(
            "skipped_packets",
            st.skipped_packets.iter().map(|(k, v)| (*k, *v)).collect::<Vec<_>>(),
        )?;
        d.set_item(
            "other_protocol",
            st.other_protocol.iter().map(|(k, v)| (*k, *v)).collect::<Vec<_>>(),
        )?;
        d.set_item(
            "unknown_types",
            st.unknown_types.iter().map(|(k, v)| (*k, *v)).collect::<Vec<_>>(),
        )?;
        d.set_item("sessions", sessions)?;
        d.set_item("first_send_ns", st.first_send_ns)?;
        d.set_item("last_send_ns", st.last_send_ns)?;
        d.set_item("system_events", events)?;
        d.set_item("symbols", symbols)?;
        d.set_item("trading_status", trading)?;
        d.set_item("bucket_messages", &st.bucket_messages)?;
        d.set_item("bucket_segments", &st.bucket_segments)?;
        d.set_item("capture_format", g.capture_kind().map(|k| k.name()))?;
        d.set_item("pending", g.pending())?;
        d.set_item("blocks", g.blocks().iter().map(|(k, v)| (*k, *v)).collect::<Vec<_>>())?;
        Ok(d)
    }
}

/// The `queuetruth._core` module.
#[pymodule(name = "_core")]
fn qt_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("QtCoreError", py.get_type::<QtCoreError>())?;
    m.add("CliExit", py.get_type::<CliExit>())?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(schema_names, m)?)?;
    m.add_function(wrap_pyfunction!(formats, m)?)?;
    m.add_function(wrap_pyfunction!(table_schema, m)?)?;
    m.add_function(wrap_pyfunction!(zoo_families, m)?)?;
    m.add_function(wrap_pyfunction!(pm_families, m)?)?;
    m.add_function(wrap_pyfunction!(pm_queue_run, m)?)?;
    m.add_function(wrap_pyfunction!(calib_interval_loglik, m)?)?;
    m.add_function(wrap_pyfunction!(calib_live_loglik_matrix, m)?)?;
    m.add_function(wrap_pyfunction!(cli_parse, m)?)?;
    m.add_function(wrap_pyfunction!(cli_route, m)?)?;
    m.add_function(wrap_pyfunction!(cli_run_native, m)?)?;
    m.add_function(wrap_pyfunction!(ingest, m)?)?;
    m.add_function(wrap_pyfunction!(replay, m)?)?;
    m.add_function(wrap_pyfunction!(sample_shadows, m)?)?;
    m.add_function(wrap_pyfunction!(mirror_real_orders, m)?)?;
    m.add_function(wrap_pyfunction!(run_zoo, m)?)?;
    m.add_function(wrap_pyfunction!(calibrate, m)?)?;
    m.add_function(wrap_pyfunction!(extract_features, m)?)?;
    m.add_function(wrap_pyfunction!(why, m)?)?;
    m.add_function(wrap_pyfunction!(export_l2_npz, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_order_ids, m)?)?;
    m.add_function(wrap_pyfunction!(referee_fixtures, m)?)?;
    m.add_function(wrap_pyfunction!(fixtures_synth, m)?)?;
    m.add_function(wrap_pyfunction!(synthesize_l2, m)?)?;
    m.add_function(wrap_pyfunction!(bbo_from_l2, m)?)?;
    m.add_function(wrap_pyfunction!(run_id, m)?)?;
    m.add_function(wrap_pyfunction!(run_id_resolved, m)?)?;
    m.add_function(wrap_pyfunction!(identity_document, m)?)?;
    m.add_function(wrap_pyfunction!(drop_v1_1_defaults, m)?)?;
    m.add_function(wrap_pyfunction!(v1_1_defaults, m)?)?;
    m.add_function(wrap_pyfunction!(stable_seed, m)?)?;
    m.add_function(wrap_pyfunction!(splitmix64, m)?)?;
    m.add_function(wrap_pyfunction!(trust_assess, m)?)?;
    m.add_function(wrap_pyfunction!(trust_annotate, m)?)?;
    m.add_function(wrap_pyfunction!(read_table, m)?)?;
    m.add_function(wrap_pyfunction!(fill_liquidity, m)?)?;
    m.add_function(wrap_pyfunction!(write_table, m)?)?;
    m.add_class::<Session>()?;
    m.add_class::<IexSplit>()?;
    Ok(())
}
