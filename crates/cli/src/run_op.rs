//! `run <op> --spec FILE|- [--out DIR]`: any operation of the table (`crate::operations`, generated from
//! `queuetruth.ops.table()`) from a JSON spec, without a hand-written command.
//!
//! It follows `queuetruth.runner.run` step by step for a caller on its own machine (no allowed roots): the spec must
//! be a JSON object; the operation's `prepare` runs (replay: bare order ids in `exclude_order_ids` resolved against
//! the dataset); `--out DIR` replaces the spec's output field (with the operation's `out_name` inside the
//! directory); the spec is checked against its published JSON Schema (`crate::spec_check`), so an invalid spec is
//! `QT-CONFIG-001`; then the operation runs. In `queuetruth-core` the operations in `NATIVE` run in Rust, through the
//! same functions `queuetruth._core` gives the Python runner; every other one needs the Python package
//! (`QT-USAGE-001`). `queuetruth advanced run` runs every operation through `queuetruth.runner.run` itself.

use std::io::Read;
use std::path::{Path, PathBuf};

use qt_schema::specs::{FeatureSpec, IngestSpec, MirrorSpec, ReplaySpec, ShadowSampleSpec};
use qt_types::{QtError, QtResult};
use serde_json::{Value, json};

use crate::operations::{OPERATIONS, OperationRow};
use crate::ops::{self, spec, to_value};
use crate::schemas::JSON_SCHEMAS;

/// The operations `queuetruth-core run` runs in Rust: their whole document comes from the Rust engine.
pub const NATIVE: &[&str] = &["features", "ingest", "replay", "shadows", "shadows_mirror"];

/// The operation `name` (`QT-USAGE-001` for an unknown one, naming the known ones).
pub fn operation(name: &str) -> QtResult<&'static OperationRow> {
    OPERATIONS.iter().find(|o| o.name == name).ok_or_else(|| {
        let known: Vec<&str> = OPERATIONS.iter().map(|o| o.name).collect();
        QtError::Usage(format!("unknown operation '{name}'; one of: {}", known.join(", ")))
    })
}

/// The table as a JSON document: `{"operations": [{name, description, spec_schema, native}]}`.
pub fn list() -> Value {
    let rows: Vec<Value> = OPERATIONS
        .iter()
        .map(|o| {
            json!({
                "name": o.name,
                "description": o.description,
                "spec_schema": o.spec_schema,
                "native": NATIVE.contains(&o.name),
            })
        })
        .collect();
    json!({ "operations": rows })
}

/// Reads a spec: `-` is standard input, anything else a file. It must be one JSON object (`QT-USAGE-001`
/// otherwise; an unreadable file is `QT-INPUT-001`).
pub fn read_spec(path: &str) -> QtResult<Value> {
    let text = if path == "-" {
        let mut s = String::new();
        std::io::stdin()
            .read_to_string(&mut s)
            .map_err(|e| QtError::Usage(format!("--spec -: cannot read standard input: {e}")))?;
        s
    } else {
        std::fs::read_to_string(path).map_err(|e| QtError::InputMissing {
            path: path.to_owned(),
            msg: format!("--spec: {e}"),
        })?
    };
    let v: Value =
        serde_json::from_str(&text).map_err(|e| QtError::Usage(format!("--spec {path}: invalid JSON: {e}")))?;
    if !v.is_object() {
        return Err(QtError::Usage("spec must be a JSON object".into()));
    }
    Ok(v)
}

/// The published JSON Schema `name` (`qt.replay_spec`).
fn schema(name: &str) -> QtResult<Value> {
    let text = JSON_SCHEMAS
        .iter()
        .find(|(k, _)| *k == name)
        .map(|(_, t)| *t)
        .ok_or_else(|| QtError::Engine(format!("schema {name} is not embedded in this build")))?;
    serde_json::from_str(text).map_err(|e| QtError::Engine(format!("schema {name}: {e}")))
}

/// `spec` checked against the operation's published schema; every violation is listed, the first in the message.
pub fn validate(op: &OperationRow, spec: &Value) -> QtResult<()> {
    let Some(name) = op.spec_schema else {
        return Ok(());
    };
    let root = schema(name)?;
    let errors = crate::spec_check::check(spec, &root);
    let Some(first) = errors.first() else {
        return Ok(());
    };
    // as the Python runner writes it: `ShadowSampleSpec: n: ...`
    let title = op.spec_model;
    let mut msg = if first.loc.is_empty() {
        format!("{title}: {}", first.msg)
    } else {
        format!("{title}: {}: {}", first.loc, first.msg)
    };
    if errors.len() > 1 {
        msg.push_str(&format!(" (+{} more)", errors.len() - 1));
    }
    Err(QtError::Config(msg))
}

/// Replay's `prepare`: bare venue order ids in `exclude_order_ids` resolved against the dataset (as the runner).
fn expand_order_ids(mut spec: Value) -> QtResult<Value> {
    let bare = |x: &Value| match x {
        Value::Number(_) => true,
        Value::String(s) => !s.trim().is_empty() && s.trim().bytes().all(|b| b.is_ascii_digit()),
        Value::Object(o) => o.get("instrument_id").is_none_or(Value::is_null) && o.contains_key("order_id"),
        _ => false,
    };
    let Some(items) = spec.get("exclude_order_ids").and_then(Value::as_array) else {
        return Ok(spec);
    };
    if !items.iter().any(bare) {
        return Ok(spec);
    }
    let items = Value::Array(items.clone());
    let dataset = spec
        .get("dataset")
        .filter(|d| d.get("path").is_some_and(Value::is_string))
        .cloned()
        .ok_or_else(|| {
            QtError::Config(
                "exclude_order_ids: bare order ids apply to every instrument of the dataset, which needs a local \
                 dataset path"
                    .into(),
            )
        })?;
    let ds = ops::spec::<qt_schema::specs::DatasetRef>(&dataset, "replay")?;
    let resolved = ops::resolve_exclude_order_ids(&items, &ds)?;
    if let Value::Object(o) = &mut spec {
        o.insert("exclude_order_ids".into(), to_value(&resolved)?);
    }
    Ok(spec)
}

/// Runs the operation `name` on `spec` (module docstring) and returns its document.
pub fn run(name: &str, spec_json: Value, out: Option<&str>) -> QtResult<Value> {
    let op = operation(name)?;
    if !spec_json.is_object() {
        return Err(QtError::Usage("spec must be a JSON object".into()));
    }
    let mut data = if name == "replay" {
        expand_order_ids(spec_json)?
    } else {
        spec_json
    };
    if let Some(dir) = out {
        let Some(field) = op.out_field else {
            return Err(QtError::Usage(format!(
                "{name}: this operation writes no files, so it takes no out"
            )));
        };
        let target: PathBuf = match op.out_name {
            Some(n) => Path::new(dir).join(n),
            None => PathBuf::from(dir),
        };
        if let Value::Object(o) = &mut data {
            o.insert(field.to_owned(), Value::String(target.display().to_string()));
        }
        if op.out_name.is_some()
            && let Some(parent) = target.parent()
        {
            std::fs::create_dir_all(parent).map_err(|e| QtError::InputMissing {
                path: parent.display().to_string(),
                msg: format!("--out: cannot create the directory: {e}"),
            })?;
        }
    }
    validate(op, &data)?;
    if !NATIVE.contains(&name) {
        return Err(QtError::Usage(format!(
            "operation {name:?} requires the Python package (pip install queuetruth; then queuetruth advanced run \
             {name} --spec FILE)"
        )));
    }
    let d = &data;
    match name {
        "ingest" => to_value(&ops::ingest(&spec::<IngestSpec>(d, "ingest")?)?),
        "replay" => {
            let s = ops::fold_replay_options(&spec::<ReplaySpec>(d, "replay")?, &Value::Null)?;
            let opts = ops::merged_replay_options(&s, &Value::Null)?;
            to_value(&qt_replay::run_dataset_with(&s, &opts)?)
        }
        "shadows" => to_value(&qt_replay::sample_shadows(&spec::<ShadowSampleSpec>(d, "shadows")?)?),
        "shadows_mirror" => to_value(&qt_replay::mirror_real_orders(&spec::<MirrorSpec>(
            d,
            "shadows_mirror",
        )?)?),
        "features" => to_value(&qt_features::extract(&spec::<FeatureSpec>(d, "features")?)?),
        _ => Err(QtError::Engine(format!("{name}: in NATIVE but not dispatched"))),
    }
}
