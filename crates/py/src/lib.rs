//! PyO3 extension module `queuetruth._core` (abi3-py311, SPEC 4.3): one function per native operation (spec JSON in,
//! summary JSON out), the shared CLI grammar, Arrow interchange through `pyo3-arrow`, and the interactive `Session`.
//! Every call that does work releases the GIL; Rust errors raise `QtCoreError(code, message)` with `.code` and
//! `.message`, re-raised by `queuetruth.api` as `queuetruth.QtError`.

use std::path::Path;
use std::sync::Mutex;

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3_arrow::PyTable;
use qt_cli::ops;
use qt_schema::specs::{
    CalibrationSpec, ExportSpec, FeatureSpec, IngestSpec, MirrorSpec, ReplaySpec, ShadowSampleSpec, WhySpec, ZooSpec,
};
use qt_types::{BookSide, QtError, QtResult, Venue};
use serde_json::Value;

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
    let code = e.code().to_owned();
    let message = e.message();
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

/// The Arrow schema of a table as a `pyarrow.Schema`.
#[pyfunction(name = "arrow_schema")]
fn table_schema(py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
    let schema = qt_schema::schema(name).ok_or_else(|| PyKeyError::new_err(format!("unknown table {name:?}")))?;
    Ok(pyo3_arrow::PySchema::new(schema).into_pyarrow(py)?.unbind())
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
    Ok(PyTable::try_new(batches, schema)?.into_pyarrow(py)?.unbind())
}

/// Writes any Arrow stream object (`__arrow_c_stream__`) whose schema metadata names a queuetruth table, with the
/// Parquet settings of SPEC 0.1; returns the file entry JSON `{name, rows, bytes, sha256}`.
#[pyfunction]
fn write_table(py: Python<'_>, table: PyTable, path: &str) -> PyResult<String> {
    let (batches, schema) = table.into_inner();
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
}

/// The `queuetruth._core` module.
#[pymodule(name = "_core")]
fn qt_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("QtCoreError", py.get_type::<QtCoreError>())?;
    m.add("CliExit", py.get_type::<CliExit>())?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(schema_names, m)?)?;
    m.add_function(wrap_pyfunction!(table_schema, m)?)?;
    m.add_function(wrap_pyfunction!(cli_parse, m)?)?;
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
    m.add_function(wrap_pyfunction!(read_table, m)?)?;
    m.add_function(wrap_pyfunction!(fill_liquidity, m)?)?;
    m.add_function(wrap_pyfunction!(write_table, m)?)?;
    m.add_class::<Session>()?;
    Ok(())
}
