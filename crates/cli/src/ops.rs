//! Native operations shared by `run_native` and the PyO3 module: spec JSON in, typed summary out.

use std::path::{Path, PathBuf};

use qt_ingest::{Dataset, DatasetKind};
use qt_replay::{FillRule, HiddenPrintPolicy, ReplayOptions};
use qt_schema::io::FileEntry;
use qt_schema::json::{
    self, Doctor, ExportSummary, ExportTarget, FixtureEntry, FixturesSummary, IngestSummary, Verdict,
};
use qt_schema::specs::{DatasetRef, ExcludeOrderId, ExportSpec, IngestFormat, IngestSpec, L2Config, ReplaySpec};
use qt_types::{QtError, QtResult, Venue};
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use crate::schemas::JSON_SCHEMAS;

/// Deserializes a spec, mapping serde errors to `QT-CONFIG-001` (field named in the message).
pub fn spec<T: DeserializeOwned>(v: &Value, what: &str) -> QtResult<T> {
    T::deserialize(v).map_err(|e| QtError::Config(format!("{what}: {e}")))
}

/// Parses a spec JSON string.
pub fn spec_str<T: DeserializeOwned>(s: &str, what: &str) -> QtResult<T> {
    let v: Value = serde_json::from_str(s).map_err(|e| QtError::Config(format!("{what}: invalid JSON: {e}")))?;
    spec(&v, what)
}

/// Serializes a summary to a JSON value.
pub fn to_value<T: serde::Serialize>(v: &T) -> QtResult<Value> {
    serde_json::to_value(v).map_err(|e| QtError::Engine(format!("summary serialization: {e}")))
}

fn dataset_path(r: &DatasetRef) -> QtResult<&Path> {
    r.as_path().map(Path::new).ok_or_else(|| {
        QtError::Usage(
            "dataset must be a local {\"path\": ...} reference (dataset_id is resolved by the service)".into(),
        )
    })
}

/// `ingest` for every format: MBO formats through `qt_ingest`, crypto and Tardis through `qt_crypto`.
pub fn ingest(spec: &IngestSpec) -> QtResult<IngestSummary> {
    match spec.format {
        IngestFormat::Itch | IngestFormat::Dbn | IngestFormat::Etcsv | IngestFormat::Parquet => qt_ingest::ingest(spec),
        IngestFormat::Bitstamp | IngestFormat::Bitfinex | IngestFormat::Coinbase | IngestFormat::TardisL2 => {
            qt_crypto::ingest(spec)
        }
    }
}

/// `ingest` as a gate (CLI `--strict`): runs without the engine's strict error, echoes `strict = true` and reports
/// whether the verdict was not clean.
pub fn ingest_gate(spec: &IngestSpec) -> QtResult<(Value, bool)> {
    let mut relaxed = spec.clone();
    relaxed.strict = false;
    let summary = ingest(&relaxed)?;
    let failed = spec.strict && summary.data_quality.verdict != Verdict::Clean;
    let mut v = to_value(&summary)?;
    if spec.strict
        && let Some(Value::Object(echo)) = v.get_mut("spec")
    {
        echo.insert("strict".into(), Value::Bool(true));
    }
    Ok((v, failed))
}

/// `export l2-npz`: hftbacktest event file of one instrument-day (MBO datasets are synthesized with `spec.l2`; L2-only
/// datasets use their rows).
pub fn export_l2_npz(spec: &ExportSpec) -> QtResult<ExportSummary> {
    if spec.target != ExportTarget::L2Npz {
        return Err(QtError::Config(
            "target: only l2_npz is native (the other exports are Python: queuetruth.export)".into(),
        ));
    }
    let ds_ref = spec
        .dataset
        .as_ref()
        .ok_or_else(|| QtError::Config("dataset: required for l2_npz".into()))?;
    let iid = spec
        .instrument_id
        .ok_or_else(|| QtError::Config("instrument_id: required for l2_npz".into()))?;
    let date_s = spec
        .session_date
        .as_deref()
        .ok_or_else(|| QtError::Config("session_date: required for l2_npz".into()))?;
    let date = qt_ingest::time::parse_date(date_s).map_err(|e| QtError::Config(format!("session_date: {e}")))?;
    let out = spec
        .out
        .as_deref()
        .ok_or_else(|| QtError::Config("out: required for l2_npz".into()))?;
    if spec.feed_latency_ns < 0 {
        return Err(QtError::Config("feed_latency_ns: must be >= 0".into()));
    }
    let ds = Dataset::open(dataset_path(ds_ref)?)?;
    let part = ds
        .partitions()
        .iter()
        .find(|p| p.instrument_id == iid && p.session_date == date)
        .ok_or_else(|| QtError::Config(format!("no partition for instrument {iid} on {date_s}")))?;
    let inst = ds
        .instrument(iid, date)
        .ok_or_else(|| QtError::Config(format!("instrument {iid} on {date_s} is not in instruments.parquet")))?;
    let rows = if part.kind == DatasetKind::L2 {
        part.l2()?.collect::<QtResult<Vec<_>>>()?
    } else {
        let cfg: qt_l2::L2Config = (&spec.l2).into();
        qt_l2::synthesize_partition(part, &ds, &cfg)?
    };
    let out_path = Path::new(out);
    if let Some(parent) = out_path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    let entry = qt_l2::write_hbt_npz(&rows, inst, spec.feed_latency_ns, out_path)?;
    Ok(ExportSummary {
        target: ExportTarget::L2Npz,
        files: vec![entry],
        text: None,
        ..ExportSummary::default()
    })
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct L2Job {
    dataset: DatasetRef,
    out: String,
    #[serde(default = "one")]
    threads: u32,
    #[serde(default)]
    l2: Option<L2Config>,
}

fn one() -> u32 {
    1
}

fn files_doc(files: &[FileEntry]) -> QtResult<Value> {
    Ok(json!({ "files": to_value(&files)? }))
}

/// `synthesize_l2`: `{"dataset": {"path"}, "out", "threads"?, "l2"?}` -> `{"files": [...]}` (qt.l2 partitions).
pub fn synthesize_l2(v: &Value) -> QtResult<Value> {
    let job: L2Job = spec(v, "synthesize_l2")?;
    let ds = Dataset::open(dataset_path(&job.dataset)?)?;
    let cfg: qt_l2::L2Config = (&job.l2.unwrap_or_default()).into();
    let files = qt_l2::synthesize(&ds, &cfg, Path::new(&job.out), job.threads.max(1) as usize)?;
    files_doc(&files)
}

/// `bbo_from_l2`: `{"dataset": {"path"}, "out", "threads"?}` -> `{"files": [...]}` (`bbo.parquet`, `trades.parquet`).
pub fn bbo_from_l2(v: &Value) -> QtResult<Value> {
    let job: L2Job = spec(v, "bbo_from_l2")?;
    if job.l2.is_some() {
        return Err(QtError::Config("bbo_from_l2: l2 is not a field of this call".into()));
    }
    let ds = Dataset::open(dataset_path(&job.dataset)?)?;
    let out = Path::new(&job.out);
    std::fs::create_dir_all(out)?;
    let files = qt_l2::bbo_from_l2(&ds, out, job.threads.max(1) as usize)?;
    files_doc(&files)
}

/// `run_id` of a fully defaulted spec with local reference resolution (SPEC 7.3).
pub fn run_id(spec: &Value) -> QtResult<String> {
    qt_schema::run_id(spec, qt_schema::ENGINE_VERSION, &qt_calib::refs::resolve)
}

/// `referee fixtures`: the synthetic bug-class fixtures under `out`.
pub fn referee_fixtures(out: &str) -> QtResult<FixturesSummary> {
    qt_testkit::bugclasses::write_fixtures(Path::new(out))
}

fn is_empty_dir(p: &Path) -> bool {
    std::fs::read_dir(p).map(|mut d| d.next().is_none()).unwrap_or(false)
}

fn is_previous_fixture(p: &Path) -> bool {
    std::fs::read_to_string(p.join("manifest.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.get("source_format").and_then(Value::as_str).map(|s| s == "synthetic"))
        .unwrap_or(false)
}

/// `fixtures synth`: writes the synthetic quickstart dataset (`synth_small` generator settings; CME also gets the
/// own-order inputs) with truth tables and `MANIFEST.sha256`. Refuses to replace a directory that is not empty and
/// not a previous synthetic fixture.
pub fn fixtures_synth(out: &str, venue: Venue, sessions: u32) -> QtResult<FixturesSummary> {
    let dir = PathBuf::from(out);
    if dir.exists() && !(is_empty_dir(&dir) || is_previous_fixture(&dir)) {
        return Err(QtError::Config(format!(
            "out: {} exists and is not an empty directory or a previous synthetic fixture",
            dir.display()
        )));
    }
    if !(1..=5).contains(&sessions) {
        return Err(QtError::Config("sessions: must be 1..=5".into()));
    }
    let cfg = qt_testkit::fixtures::fixture_config(venue);
    let synth = qt_testkit::fixtures::write_synth_fixture(&dir, &cfg, sessions, "")?;
    let name = match venue {
        Venue::Nasdaq => "synth_small_nasdaq",
        _ => "synth_small",
    };
    let mut text = qt_testkit::fixtures::readme(name, &cfg, sessions, &synth);
    if venue == Venue::Cme {
        text.push_str(&qt_testkit::fixtures::write_audit_inputs(&dir, &synth, cfg.seed)?);
    }
    std::fs::write(dir.join("README.txt"), text)?;
    let listed = qt_testkit::write_manifest_sha256(&dir)?;
    let files = listed
        .into_iter()
        .map(|(name, sha256)| {
            let bytes = std::fs::metadata(dir.join(&name))?.len();
            Ok(FileEntry {
                name,
                rows: None,
                bytes,
                sha256,
            })
        })
        .collect::<QtResult<Vec<_>>>()?;
    Ok(FixturesSummary {
        out: out.to_owned(),
        fixtures: vec![FixtureEntry {
            name: name.to_owned(),
            path: out.to_owned(),
            files,
        }],
        ..FixturesSummary::default()
    })
}

/// `schema list`: JSON document schema names and Arrow table names.
pub fn schema_list() -> Value {
    json!({
        "json": JSON_SCHEMAS.iter().map(|(n, _)| *n).collect::<Vec<_>>(),
        "arrow": qt_schema::schema_names(),
    })
}

/// `schema show NAME`: the JSON Schema of a document/spec, or the Arrow schema dump of a table.
pub fn schema_show(name: &str) -> QtResult<Value> {
    if let Some((_, text)) = JSON_SCHEMAS.iter().find(|(n, _)| *n == name) {
        return serde_json::from_str(text).map_err(|e| QtError::Engine(format!("{name}: embedded schema: {e}")));
    }
    if let Some(text) = qt_schema::schema_json(name) {
        return serde_json::from_str(&text).map_err(|e| QtError::Engine(format!("{name}: arrow schema dump: {e}")));
    }
    Err(QtError::Config(format!(
        "name: unknown schema {name:?} (see `schema list`)"
    )))
}

/// Native part of `doctor`: core version and embedded-schema consistency.
pub fn doctor() -> Doctor {
    let mut problems = Vec::new();
    for (n, text) in JSON_SCHEMAS {
        match serde_json::from_str::<Value>(text) {
            Ok(v) if v.get("$schema").is_some() => {}
            _ => problems.push(format!("embedded JSON Schema {n} is invalid")),
        }
    }
    for n in qt_schema::schema_names() {
        if qt_schema::schema(n).is_none() || qt_schema::schema_json(n).is_none() {
            problems.push(format!("Arrow schema {n} is missing"));
        }
    }
    Doctor {
        python: "n/a (queuetruth-core; run `queuetruth doctor` for the Python checks)".into(),
        platform: std::env::consts::OS.into(),
        machine: std::env::consts::ARCH.into(),
        core_version: Some(qt_schema::ENGINE_VERSION.into()),
        schemas_ok: problems.is_empty(),
        postgres: None,
        docker: None,
        problems,
        ..Doctor::default()
    }
}

/// Adds a warning to a summary object's `warnings` list (created when absent).
pub fn add_warning(v: &mut Value, w: &str) {
    if let Value::Object(o) = v {
        let entry = o.entry("warnings").or_insert_with(|| Value::Array(Vec::new()));
        if let Value::Array(a) = entry {
            a.push(Value::String(w.to_owned()));
        }
    }
}

/// Replay options that are not `ReplaySpec` fields yet (SPEC 3.11): `{"fill_rule": "exact"|"et",
/// "hidden_print_policy": "ignore"|"behind"|"behind_and_through"|null, "exclude_order_ids": [...]}` (every key
/// optional). `exclude_order_ids` items are `{instrument_id, order_id, session_date?}` or a bare venue order id
/// (integer or decimal string), which applies to every instrument in `instruments`; an item with a `session_date`
/// goes to `exclude_order_ids_dated` (that date only); a `{"path": FILE}` value is read with `read_order_ids`.
pub fn replay_options(v: &Value, instruments: &[u32]) -> QtResult<ReplayOptions> {
    let mut o = ReplayOptions::default();
    let Value::Object(m) = v else {
        return if v.is_null() {
            Ok(o)
        } else {
            Err(QtError::Config("replay options: expected an object".into()))
        };
    };
    for (k, val) in m {
        match k.as_str() {
            "fill_rule" => {
                o.fill_rule = match val.as_str() {
                    Some("exact") => FillRule::Exact,
                    Some("et") => FillRule::Et,
                    _ => return Err(QtError::Config(format!("fill_rule: expected exact or et, got {val}"))),
                }
            }
            "hidden_print_policy" => {
                o.hidden_print_policy = match val.as_str() {
                    None if val.is_null() => None,
                    Some("ignore") => Some(HiddenPrintPolicy::Ignore),
                    Some("behind") => Some(HiddenPrintPolicy::Behind),
                    Some("behind_and_through") => Some(HiddenPrintPolicy::BehindAndThrough),
                    _ => {
                        return Err(QtError::Config(format!(
                            "hidden_print_policy: expected ignore, behind or behind_and_through, got {val}"
                        )));
                    }
                }
            }
            "exclude_order_ids" => {
                let list = match val.get("path").and_then(Value::as_str) {
                    Some(p) => read_order_ids(Path::new(p))?,
                    None => val.clone(),
                };
                let items = list
                    .as_array()
                    .ok_or_else(|| QtError::Config("exclude_order_ids: expected a list".into()))?;
                for it in items {
                    if let Some(id) = bare_id(it) {
                        o.exclude_order_ids.extend(instruments.iter().map(|&i| (i, id)));
                        continue;
                    }
                    let iid = it
                        .get("instrument_id")
                        .and_then(Value::as_u64)
                        .and_then(|x| u32::try_from(x).ok());
                    let oid = it.get("order_id").and_then(bare_id);
                    // `ExcludeOrderId.session_date` (1.1): the exclusion applies on that session date only.
                    let day = match it.get("session_date") {
                        None | Some(Value::Null) => None,
                        Some(Value::String(d)) => Some(
                            qt_ingest::time::parse_date(d)
                                .map_err(|e| QtError::Config(format!("exclude_order_ids: session_date {d:?}: {e}")))?,
                        ),
                        Some(other) => {
                            return Err(QtError::Config(format!(
                                "exclude_order_ids: session_date must be YYYY-MM-DD or null, got {other}"
                            )));
                        }
                    };
                    let targets: Vec<(u32, u64)> = match (iid, oid) {
                        (Some(i), Some(id)) => vec![(i, id)],
                        (None, Some(id)) => instruments.iter().map(|&i| (i, id)).collect(),
                        _ => {
                            return Err(QtError::Config(format!(
                                "exclude_order_ids: expected an order id or {{instrument_id, order_id, \
                                 session_date?}}, got {it}"
                            )));
                        }
                    };
                    match day {
                        None => o.exclude_order_ids.extend(targets),
                        Some(d) => o
                            .exclude_order_ids_dated
                            .extend(targets.into_iter().map(|(i, id)| (i, id, d))),
                    }
                }
                o.exclude_order_ids.sort_unstable();
                o.exclude_order_ids.dedup();
                o.exclude_order_ids_dated.sort_unstable();
                o.exclude_order_ids_dated.dedup();
            }
            other => return Err(QtError::Config(format!("replay options: unknown key {other:?}"))),
        }
    }
    Ok(o)
}

fn bare_id(v: &Value) -> Option<u64> {
    match v {
        Value::Number(n) => n.as_u64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

/// Reads an order-id list: `.json` (a list of ids or `{instrument_id, order_id}` objects), or text/CSV with one id
/// per line, or a CSV whose header has `order_id` (and optionally `instrument_id`).
pub fn read_order_ids(path: &Path) -> QtResult<Value> {
    let text = std::fs::read_to_string(path).map_err(|e| QtError::InputMissing {
        path: path.display().to_string(),
        msg: e.to_string(),
    })?;
    let bad = |msg: String| QtError::InputFormat {
        location: path.display().to_string(),
        msg,
    };
    if path.extension().is_some_and(|e| e.eq_ignore_ascii_case("json")) {
        return serde_json::from_str(&text).map_err(|e| bad(format!("invalid JSON: {e}")));
    }
    let mut lines = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'));
    let mut out = Vec::new();
    let first = lines.next();
    let header: Option<Vec<String>> = first
        .filter(|l| l.contains("order_id"))
        .map(|l| l.split(',').map(|c| c.trim().to_owned()).collect());
    let (oi, ii) = match &header {
        Some(h) => (
            h.iter().position(|c| c == "order_id").unwrap_or(0),
            h.iter().position(|c| c == "instrument_id"),
        ),
        None => (0, None),
    };
    let rest: Vec<&str> = if header.is_some() {
        lines.collect()
    } else {
        first.into_iter().chain(lines).collect()
    };
    for (n, l) in rest.iter().enumerate() {
        let cols: Vec<&str> = l.split(',').map(str::trim).collect();
        let id: u64 = cols
            .get(oi)
            .and_then(|c| c.parse().ok())
            .ok_or_else(|| bad(format!("line {}: expected an integer order id, got {l:?}", n + 1)))?;
        match ii {
            Some(i) => {
                let inst: u32 = cols
                    .get(i)
                    .and_then(|c| c.parse().ok())
                    .ok_or_else(|| bad(format!("line {}: expected an integer instrument_id", n + 1)))?;
                out.push(json!({"instrument_id": inst, "order_id": id}));
            }
            None => out.push(json!(id)),
        }
    }
    Ok(Value::Array(out))
}

/// Instrument ids of a dataset (for bare order ids in `exclude_order_ids`).
pub fn dataset_instruments(spec: &ReplaySpec) -> QtResult<Vec<u32>> {
    let ds = Dataset::open(dataset_path(&spec.dataset)?)?;
    let mut v: Vec<u32> = ds.partitions().iter().map(|p| p.instrument_id).collect();
    v.sort_unstable();
    v.dedup();
    Ok(v)
}

/// The items of an `exclude_order_ids` value: `{"path": FILE}` (the formats of `read_order_ids`) or a list of ids /
/// `{instrument_id, order_id, session_date?}` objects; an item without an instrument (a bare id) applies to every
/// instrument of the dataset (as the replay option). Sorted and deduplicated, as `ZooSpec.exclude_order_ids` items.
pub fn resolve_exclude_order_ids(v: &Value, dataset: &DatasetRef) -> QtResult<Vec<ExcludeOrderId>> {
    let list = match v.get("path").and_then(Value::as_str) {
        Some(p) => read_order_ids(Path::new(p))?,
        None => v.clone(),
    };
    let items = list
        .as_array()
        .ok_or_else(|| QtError::Config("exclude_order_ids: expected a list".into()))?;
    let mut instruments: Option<Vec<u32>> = None;
    let mut all = |out: &mut Vec<ExcludeOrderId>, id: u64, date: Option<String>| -> QtResult<()> {
        if instruments.is_none() {
            let ds = Dataset::open(dataset_path(dataset)?)?;
            let mut inst: Vec<u32> = ds.partitions().iter().map(|p| p.instrument_id).collect();
            inst.sort_unstable();
            inst.dedup();
            instruments = Some(inst);
        }
        for &i in instruments.as_deref().unwrap_or_default() {
            out.push(ExcludeOrderId {
                instrument_id: i,
                order_id: id,
                session_date: date.clone(),
            });
        }
        Ok(())
    };
    let mut out = Vec::new();
    for it in items {
        if let Some(id) = bare_id(it) {
            all(&mut out, id, None)?;
            continue;
        }
        let bad = || {
            QtError::Config(format!(
                "exclude_order_ids: expected an order id or {{instrument_id, order_id, session_date?}}, got {it}"
            ))
        };
        let o = it.as_object().ok_or_else(bad)?;
        if o.keys()
            .any(|k| !matches!(k.as_str(), "instrument_id" | "order_id" | "session_date"))
        {
            return Err(bad());
        }
        let oid = o.get("order_id").and_then(bare_id).ok_or_else(bad)?;
        let date = match o.get("session_date") {
            None | Some(Value::Null) => None,
            Some(Value::String(d)) => Some(d.clone()),
            Some(_) => return Err(bad()),
        };
        match o.get("instrument_id").filter(|x| !x.is_null()) {
            Some(x) => {
                let iid = x.as_u64().and_then(|x| u32::try_from(x).ok()).ok_or_else(bad)?;
                out.push(ExcludeOrderId {
                    instrument_id: iid,
                    order_id: oid,
                    session_date: date,
                });
            }
            None => all(&mut out, oid, date)?,
        }
    }
    out.sort();
    out.dedup();
    Ok(out)
}

/// `replay_options` for a spec (opens the dataset only when bare order ids need its instrument list).
pub fn replay_options_for(spec: &ReplaySpec, v: &Value) -> QtResult<ReplayOptions> {
    // A file may hold bare ids; a list needs the instruments only for items that name none.
    let needs = v.get("exclude_order_ids").is_some_and(|x| match x {
        Value::Null => false,
        Value::Array(items) => items
            .iter()
            .any(|it| bare_id(it).is_some() || it.get("instrument_id").is_none_or(Value::is_null)),
        _ => true,
    });
    let inst = if needs { dataset_instruments(spec)? } else { Vec::new() };
    replay_options(v, &inst)
}

/// Replay options from the spec's own `fill_rule` / `hidden_print_policy` / `exclude_order_ids` fields (schema
/// 1.1, read generically so older specs without them work) with `extra` (CLI `_cli` / Python keyword options)
/// overriding key by key.
pub fn merged_replay_options(spec: &ReplaySpec, extra: &Value) -> QtResult<ReplayOptions> {
    let mut m = serde_json::Map::new();
    let v = to_value(spec)?;
    for k in ["fill_rule", "hidden_print_policy", "exclude_order_ids"] {
        if let Some(x) = v.get(k).filter(|x| !x.is_null()) {
            m.insert(k.to_owned(), x.clone());
        }
    }
    if let Value::Object(e) = extra {
        for (k, x) in e {
            m.insert(k.clone(), x.clone());
        }
    }
    replay_options_for(spec, &Value::Object(m))
}

/// Folds `replay` options given outside the spec (CLI `--fill-rule` / `--hidden-print-policy` /
/// `--exclude-order-ids`, Python keyword arguments) into the spec's own schema 1.1 fields, each given option
/// replacing its field, so they enter `run_id` and the spec echo (E38 OC-34: output-affecting options are spec
/// fields). `exclude_order_ids` takes `{"path": FILE}` or a list (bare ids apply to every instrument of the dataset,
/// `resolve_exclude_order_ids`); `null` options are ignored; any other key is `QT-CONFIG-001`.
pub fn fold_replay_options(spec: &ReplaySpec, extra: &Value) -> QtResult<ReplaySpec> {
    let mut s = spec.clone();
    let Value::Object(m) = extra else {
        return Ok(s);
    };
    for (k, val) in m {
        if val.is_null() {
            continue;
        }
        match k.as_str() {
            "fill_rule" => {
                let v = val.as_str().map(|x| x.replace('-', "_"));
                s.fill_rule = v
                    .and_then(|x| serde_json::from_value(Value::String(x)).ok())
                    .ok_or_else(|| QtError::Config(format!("fill_rule: expected exact or et, got {val}")))?;
            }
            "hidden_print_policy" => {
                let v = val.as_str().map(|x| x.replace('-', "_"));
                s.hidden_print_policy = Some(
                    v.and_then(|x| serde_json::from_value(Value::String(x)).ok())
                        .ok_or_else(|| {
                            QtError::Config(format!(
                                "hidden_print_policy: expected ignore, behind or behind_and_through, got {val}"
                            ))
                        })?,
                );
            }
            "exclude_order_ids" => s.exclude_order_ids = resolve_exclude_order_ids(val, &s.dataset)?,
            other => return Err(QtError::Config(format!("replay options: unknown option {other:?}"))),
        }
    }
    Ok(s)
}

/// Liquidity (`maker` | `taker`) of a `qt.shadow_fills` reason (`qt_replay::fill_liquidity`: only `crossed_at_live`
/// is taker; a resting shadow crossed later is the maker, A5 as refined).
pub fn fill_liquidity(reason: &str) -> QtResult<&'static str> {
    let r: qt_schema::rows::FillReason = reason
        .parse()
        .map_err(|_| QtError::Config(format!("reason: unknown shadow fill reason {reason:?}")))?;
    Ok(qt_replay::fill_liquidity(r))
}

/// The `qt.replay_summary` with the strict-gate violations (`--strict`).
pub fn replay_gate(spec: &ReplaySpec, opts: &ReplayOptions) -> QtResult<(json::ReplaySummary, bool)> {
    let s = qt_replay::run_dataset_with(spec, opts)?;
    let failed = spec.strict && !qt_replay::strict_violations(&s).is_empty();
    Ok((s, failed))
}
