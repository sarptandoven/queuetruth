//! Drift tests of the source format registry (`qt_cli::formats`): every `IngestSpec.format` of the committed JSON
//! Schema (generated from the Python model) is in it exactly once, its spellings agree, and the two engine crates'
//! own lists add up to it.

use std::collections::BTreeSet;
use std::path::Path;

use qt_cli::formats::{CLI_NAMES, REGISTRY, get};
use serde_json::Value;

fn schema_formats() -> Vec<String> {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../schemas/json/qt.ingest_spec.schema.json");
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    let v: Value = serde_json::from_str(&text).unwrap_or_else(|e| panic!("{e}"));
    v["properties"]["format"]["enum"]
        .as_array()
        .unwrap_or_else(|| panic!("format enum missing"))
        .iter()
        .map(|x| x.as_str().unwrap_or_default().to_owned())
        .collect()
}

#[test]
fn registry_holds_every_spec_format_once() {
    let names: Vec<&str> = REGISTRY.iter().map(|f| f.name).collect();
    let unique: BTreeSet<&str> = names.iter().copied().collect();
    assert_eq!(unique.len(), names.len(), "duplicate format in the registry: {names:?}");
    let spec: BTreeSet<String> = schema_formats().into_iter().collect();
    let ours: BTreeSet<String> = names.iter().map(|s| (*s).to_owned()).collect();
    assert_eq!(ours, spec, "registry and IngestSpec.format differ");
    for f in &REGISTRY {
        assert_eq!(
            serde_json::to_value(f.format).ok(),
            Some(Value::String(f.name.to_owned()))
        );
        assert_eq!(f.cli_name, f.name.replace('_', "-"));
        assert_eq!(get(f.format).map(|g| g.name), Some(f.name));
    }
    assert_eq!(
        CLI_NAMES.to_vec(),
        REGISTRY.iter().map(|f| f.cli_name).collect::<Vec<_>>()
    );
}

#[test]
fn engine_lists_add_up_to_the_registry() {
    let mut engines: Vec<&str> = qt_ingest::formats::FORMATS.iter().map(|f| f.name).collect();
    engines.extend(qt_crypto::formats::FORMATS.iter().map(|f| f.name));
    let a: BTreeSet<&str> = engines.iter().copied().collect();
    let b: BTreeSet<&str> = REGISTRY.iter().map(|f| f.name).collect();
    assert_eq!(engines.len(), REGISTRY.len());
    assert_eq!(a, b);
    for f in &qt_ingest::formats::FORMATS {
        assert_eq!(f.engine, "qt_ingest");
    }
    for f in &qt_crypto::formats::FORMATS {
        assert_eq!(f.engine, "qt_crypto");
    }
}

#[test]
fn every_format_choice_parses_into_its_spec_value() {
    for f in &REGISTRY {
        let argv: Vec<String> = [
            "queuetruth",
            "ingest",
            "--format",
            f.cli_name,
            "--in",
            "x",
            "--venue",
            "generic",
            "--out",
            "o",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        let c = qt_cli::parse(&argv).unwrap_or_else(|e| panic!("{}: {e:?}", f.cli_name));
        assert_eq!(c.args["format"], Value::String(f.name.to_owned()), "{}", f.cli_name);
    }
}

#[test]
fn to_json_lists_the_registry() {
    let v = qt_cli::formats::to_json();
    let names: Vec<&str> = v
        .as_array()
        .unwrap_or_else(|| panic!("array"))
        .iter()
        .filter_map(|o| o["name"].as_str())
        .collect();
    assert_eq!(names, REGISTRY.iter().map(|f| f.name).collect::<Vec<_>>());
    assert_eq!(v[11]["source_format"], "iex");
    assert_eq!(v[10]["kind"], "l2");
}
