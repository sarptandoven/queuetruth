//! Embeds every `schemas/json/*.schema.json` (generated from the Pydantic models) into the binary: writes
//! `$OUT_DIR/json_schemas.rs`, a sorted `(name, text)` table, and reruns when the directory changes. A missing or
//! empty directory fails the build (it would otherwise embed nothing silently).

use std::fmt::Write as _;
use std::path::PathBuf;

fn main() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap_or_default());
    let dir = manifest.join("../../schemas/json");
    println!("cargo:rerun-if-changed={}", dir.display());
    let rd = match std::fs::read_dir(&dir) {
        Ok(rd) => rd,
        Err(e) => panic!(
            "queuetruth-cli build: cannot read the JSON Schemas directory {} ({e}); it is generated from the \
             Pydantic models (`make schemas`) and must exist before the CLI is built",
            dir.display()
        ),
    };
    let mut names: Vec<String> = rd
        .filter_map(Result::ok)
        .filter_map(|e| e.file_name().into_string().ok())
        .filter_map(|n| n.strip_suffix(".schema.json").map(str::to_owned))
        .collect();
    assert!(
        !names.is_empty(),
        "queuetruth-cli build: no *.schema.json in {}; `schema list|show` would embed nothing (regenerate them from \
         the Pydantic models with `make schemas`)",
        dir.display()
    );
    names.sort();
    let mut out = String::from("/// (name, JSON Schema text) of every JSON document and spec, sorted by name.\n");
    out.push_str("pub(crate) const JSON_SCHEMAS: &[(&str, &str)] = &[\n");
    for n in &names {
        let path = dir.join(format!("{n}.schema.json"));
        println!("cargo:rerun-if-changed={}", path.display());
        let _ = writeln!(out, "    ({n:?}, include_str!({:?})),", path.display().to_string());
    }
    out.push_str("];\n");
    let dest = PathBuf::from(std::env::var("OUT_DIR").unwrap_or_default()).join("json_schemas.rs");
    if let Err(e) = std::fs::write(&dest, out) {
        panic!("writing {}: {e}", dest.display());
    }
}
