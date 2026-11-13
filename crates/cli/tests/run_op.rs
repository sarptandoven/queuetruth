//! `queuetruth-core run <op> --spec FILE|- [--out DIR]` on `tests/fixtures/synth_small`: the native operations
//! from a file and from standard input, the operation table (`run --list`), and the errors (an invalid spec, an
//! unknown operation, an operation that needs Python, a spec that is not an object, a missing file), each a
//! `qt.error` document on stdout with exit code 2.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use qt_cli::operations::OPERATIONS;
use qt_cli::run_op::NATIVE;
use serde_json::{Value, json};

fn synth() -> String {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/synth_small")
        .display()
        .to_string()
}

fn scratch(name: &str) -> PathBuf {
    let p = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("qt-cli-run").join(name);
    if p.exists() {
        std::fs::remove_dir_all(&p).expect("clean scratch");
    }
    std::fs::create_dir_all(&p).expect("scratch");
    p
}

/// Runs the binary with `stdin`: (exit code, the one JSON document on stdout, stderr).
fn core(args: &[&str], stdin: Option<&str>) -> (i32, Value, String) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_queuetruth-core"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("run queuetruth-core");
    {
        let mut i = child.stdin.take().expect("stdin");
        if let Some(s) = stdin {
            i.write_all(s.as_bytes()).expect("write stdin");
        }
    }
    let o = child.wait_with_output().expect("wait");
    let out = String::from_utf8_lossy(&o.stdout).into_owned();
    let err = String::from_utf8_lossy(&o.stderr).into_owned();
    assert_eq!(
        out.trim().lines().count(),
        1,
        "{args:?}: one JSON document on stdout: {out}\n{err}"
    );
    let v = serde_json::from_str(out.trim()).unwrap_or_else(|e| panic!("{args:?}: not JSON ({e}): {out}"));
    (o.status.code().unwrap_or(-1), v, err)
}

fn write(dir: &Path, name: &str, v: &Value) -> String {
    let p = dir.join(name);
    std::fs::write(&p, v.to_string()).expect("write spec");
    p.display().to_string()
}

fn error_code(v: &Value) -> &str {
    assert_eq!(v["schema"], "qt.error", "{v}");
    v["code"].as_str().expect("code")
}

#[test]
fn shadows_then_replay_from_spec_files() {
    let dir = scratch("shadows_replay");
    let spec = write(
        &dir,
        "shadows.json",
        &json!({"dataset": {"path": synth()}, "n": 12, "seed": 7}),
    );
    let out = dir.join("out").display().to_string();
    let (code, doc, _) = core(&["run", "shadows", "--spec", &spec, "--out", &out], None);
    assert_eq!(code, 0, "{doc}");
    assert_eq!(doc["schema"], "qt.shadow_sample_summary");
    assert_eq!(doc["n"], 12);
    // --out DIR puts the operation's out_name inside the directory, as queuetruth.runner.run does
    let shadows = dir.join("out/shadows.parquet");
    assert!(shadows.is_file());
    assert!(doc["files"].to_string().contains("shadows.parquet"), "{doc}");

    let replay = write(
        &dir,
        "replay.json",
        &json!({"dataset": {"path": synth()}, "shadows": {"path": shadows.display().to_string()}}),
    );
    let rout = dir.join("replay").display().to_string();
    let (code, doc, _) = core(&["advanced", "run", "replay", "--spec", &replay, "--out", &rout], None);
    assert_eq!(code, 0, "{doc}");
    assert_eq!(doc["schema"], "qt.replay_summary");
    assert!(doc["run_id"].as_str().is_some_and(|r| !r.is_empty()));
    assert!(Path::new(&rout).is_dir());
}

#[test]
fn spec_from_standard_input_matches_the_file() {
    let dir = scratch("stdin");
    let spec =
        json!({"dataset": {"path": synth()}, "n": 5, "seed": 3, "out": dir.join("a.parquet").display().to_string()});
    let file = write(&dir, "spec.json", &spec);
    let (c1, from_file, _) = core(&["run", "shadows", "--spec", &file], None);
    let (c2, from_stdin, _) = core(&["run", "shadows", "--spec", "-"], Some(&spec.to_string()));
    assert_eq!((c1, c2), (0, 0));
    assert_eq!(from_file["spec"], from_stdin["spec"]);
    assert_eq!(from_file["by_instrument"], from_stdin["by_instrument"]);
}

#[test]
fn an_invalid_spec_is_config_001_with_the_model_and_field() {
    let (code, doc, _) = core(
        &["run", "shadows", "--spec", "-"],
        Some(r#"{"dataset": {"path": "ds"}, "n": -3, "bogus": 1}"#),
    );
    assert_eq!(code, 2);
    assert_eq!(error_code(&doc), "QT-CONFIG-001");
    let msg = doc["message"].as_str().expect("message");
    assert!(msg.starts_with("ShadowSampleSpec: n: must be >= 0"), "{msg}");
    assert!(msg.ends_with("(+1 more)"), "{msg}");
    // the same check for an operation that needs Python: the spec is refused before the operation is looked at
    let (code, doc, _) = core(&["run", "score", "--spec", "-"], Some("{}"));
    assert_eq!(code, 2);
    assert_eq!(error_code(&doc), "QT-CONFIG-001");
    assert!(doc["message"].as_str().expect("message").starts_with("ScorecardSpec: "));
}

#[test]
fn unknown_operation_python_only_operation_and_bad_input() {
    let (code, doc, _) = core(&["run", "nope", "--spec", "-"], Some("{}"));
    assert_eq!(code, 2);
    assert_eq!(error_code(&doc), "QT-USAGE-001");
    assert!(
        doc["message"]
            .as_str()
            .expect("message")
            .contains("unknown operation 'nope'; one of: atlas")
    );

    let (code, doc, _) = core(
        &["run", "dq", "--spec", "-"],
        Some(&json!({"dataset": {"path": synth()}}).to_string()),
    );
    assert_eq!(code, 2);
    assert_eq!(error_code(&doc), "QT-USAGE-001");
    assert!(
        doc["message"]
            .as_str()
            .expect("message")
            .contains("requires the Python package")
    );

    let (code, doc, _) = core(&["run", "shadows", "--spec", "-"], Some("[1, 2]"));
    assert_eq!((code, error_code(&doc)), (2, "QT-USAGE-001"));
    let (code, doc, _) = core(&["run", "shadows", "--spec", "-"], Some("{not json"));
    assert_eq!((code, error_code(&doc)), (2, "QT-USAGE-001"));
    let (code, doc, _) = core(&["run", "shadows", "--spec", "/no/such/spec.json"], None);
    assert_eq!((code, error_code(&doc)), (2, "QT-INPUT-001"));
    // a usage error of the command line is JSON too, without --format json
    let (code, doc, _) = core(&["run", "shadows"], None);
    assert_eq!((code, error_code(&doc)), (2, "QT-USAGE-001"));
    let (code, doc, _) = core(&["run", "atlas", "--spec", "-", "--out", "x"], Some("{}"));
    assert_eq!((code, error_code(&doc)), (2, "QT-USAGE-001"));
}

#[test]
fn list_prints_the_operation_table() {
    let (code, doc, _) = core(&["--format", "json", "run", "--list"], None);
    assert_eq!(code, 0);
    let rows = doc["operations"].as_array().expect("operations");
    assert_eq!(rows.len(), OPERATIONS.len());
    for (row, op) in rows.iter().zip(OPERATIONS) {
        assert_eq!(row["name"], op.name);
        assert_eq!(row["spec_schema"], json!(op.spec_schema));
        assert_eq!(row["native"], json!(NATIVE.contains(&op.name)));
    }
    let o = Command::new(env!("CARGO_BIN_EXE_queuetruth-core"))
        .args(["run", "--list"])
        .output()
        .expect("run");
    let text = String::from_utf8_lossy(&o.stdout);
    assert!(
        text.starts_with("operations (queuetruth advanced run NAME --spec FILE"),
        "{text}"
    );
    assert!(text.contains("replay*") && text.contains("qt.replay_spec"), "{text}");
}

#[test]
fn every_native_operation_is_in_the_table_with_a_published_schema() {
    for name in NATIVE {
        let op = OPERATIONS.iter().find(|o| o.name == *name);
        assert!(
            op.is_some_and(|o| o.spec_schema.is_some()),
            "{name}: not in the operation table, or without a published spec schema"
        );
    }
}
