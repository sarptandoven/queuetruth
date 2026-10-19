//! Local resolution of `DatasetRef` / `FileRef` objects: paths for execution, sha256 for the run identity
//! (SPEC 7.3: a dataset's `dataset_id`, a file's sha256, a directory by the sha256 of its `manifest.json`).

use std::path::Path;

use qt_schema::specs::{DatasetRef, FileRef};
use qt_types::{QtError, QtResult};
use serde_json::Value;

/// Local dataset path (`QT-CONFIG-001` for service-only references).
pub fn dataset_path<'a>(d: Option<&'a DatasetRef>, field: &str) -> QtResult<&'a Path> {
    match d {
        Some(d) => d.as_path().map(Path::new).ok_or_else(|| {
            QtError::Config(format!(
                "{field}: local runs need {{\"path\": ...}}, not a service dataset_id"
            ))
        }),
        None => Err(QtError::Config(format!("{field}: required"))),
    }
}

/// Local file path (`QT-CONFIG-001` for service-only references).
pub fn file_path<'a>(f: Option<&'a FileRef>, field: &str) -> QtResult<&'a Path> {
    match f {
        Some(f) => f
            .as_path()
            .map(Path::new)
            .ok_or_else(|| QtError::Config(format!("{field}: local runs need {{\"path\": ...}}"))),
        None => Err(QtError::Config(format!("{field}: required"))),
    }
}

/// `resolve` callback of `qt_schema::canon::run_id` for local runs.
pub fn resolve(v: &Value) -> QtResult<String> {
    if let Some(id) = v.get("dataset_id").and_then(Value::as_str) {
        return Ok(id.to_owned());
    }
    let Some(p) = v.get("path").and_then(Value::as_str) else {
        return Err(QtError::Config(format!("cannot resolve reference {v} locally")));
    };
    let path = Path::new(p);
    if path.is_dir() {
        let manifest = path.join("manifest.json");
        let text = std::fs::read_to_string(&manifest).map_err(|e| QtError::InputMissing {
            path: manifest.display().to_string(),
            msg: e.to_string(),
        })?;
        let doc: Value = serde_json::from_str(&text).map_err(|e| QtError::InputFormat {
            location: manifest.display().to_string(),
            msg: e.to_string(),
        })?;
        if doc.get("schema").and_then(Value::as_str) == Some("qt.dataset_manifest")
            && let Some(id) = doc.get("dataset_id").and_then(Value::as_str)
        {
            return Ok(id.to_owned());
        }
        return qt_schema::io::sha256_file(&manifest);
    }
    qt_schema::io::sha256_file(path)
}
