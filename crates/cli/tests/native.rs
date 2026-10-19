//! Native runs on `tests/fixtures/synth_small` (replay, shadows sample, shadows mirror-real, why, schema,
//! export l2-npz, referee fixtures) and O-CLI-1: exit codes 0/1/2 and the `qt.error` JSON per error class, through
//! the `queuetruth-core` binary.

use std::path::{Path, PathBuf};
use std::process::Command;

use qt_schema::codec::{read_shadow_specs, shadow_specs_to_batch};
use qt_schema::io::TableWriter;
use serde_json::{Value, json};

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn synth() -> String {
    repo().join("tests/fixtures/synth_small").display().to_string()
}

fn scratch(name: &str) -> PathBuf {
    let p = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join("qt-cli").join(name);
    if p.exists() {
        std::fs::remove_dir_all(&p).expect("clean scratch");
    }
    std::fs::create_dir_all(&p).expect("scratch");
    p
}

/// Runs the binary: (exit code, stdout, stderr).
fn core(args: &[&str]) -> (i32, String, String) {
    let o = Command::new(env!("CARGO_BIN_EXE_queuetruth-core"))
        .args(args)
        .output()
        .expect("run queuetruth-core");
    (
        o.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&o.stdout).into_owned(),
        String::from_utf8_lossy(&o.stderr).into_owned(),
    )
}

/// Runs with `--format json` and parses the single stdout document.
fn core_json(args: &[&str]) -> (i32, Value) {
    let mut a = vec!["--format", "json"];
    a.extend_from_slice(args);
    let (code, out, err) = core(&a);
    let v: Value =
        serde_json::from_str(out.trim()).unwrap_or_else(|e| panic!("{args:?}: stdout not JSON ({e}): {out}\n{err}"));
    assert_eq!(out.trim().lines().count(), 1, "exactly one JSON document on stdout");
    (code, v)
}

fn s(p: &Path) -> String {
    p.display().to_string()
}

#[test]
fn native_runs_on_synth_small() {
    let dir = scratch("native");
    let ds = synth();
    let sh = s(&dir.join("sh.parquet"));
    let (code, v) = core_json(&[
        "shadows", "sample", "--data", &ds, "--n", "40", "--out", &sh, "--seed", "5",
    ]);
    assert_eq!(code, 0, "{v}");
    assert_eq!(v["schema"], "qt.shadow_sample_summary");
    assert_eq!(v["n"], 40);

    let rep = s(&dir.join("replay"));
    let (code, v) = core_json(&[
        "replay",
        "--data",
        &ds,
        "--out",
        &rep,
        "--shadows",
        &sh,
        "--rank-oracle",
    ]);
    assert_eq!(code, 0, "{v}");
    assert_eq!(v["schema"], "qt.replay_summary");
    assert_eq!(v["shadows"]["n"], 40);
    assert_eq!(v["rank_oracle"]["price_priority_violations"], 0);
    assert!(dir.join("replay/shadow_results.parquet").exists());
    assert!(dir.join("replay/summary.json").exists());

    // --threads must not change outputs (summary minus timings).
    let rep4 = s(&dir.join("replay4"));
    let (_, v4) = core_json(&[
        "replay",
        "--data",
        &ds,
        "--out",
        &rep4,
        "--shadows",
        &sh,
        "--rank-oracle",
        "--threads",
        "4",
    ]);
    let strip = |mut x: Value| {
        x.as_object_mut().map(|o| o.remove("timings"));
        x["files"].clone()
    };
    assert_eq!(strip(v.clone()), strip(v4.clone()));
    assert_eq!(v["run_id"], v4["run_id"]);

    let mirror = s(&dir.join("mirror.parquet"));
    let (code, v) = core_json(&["shadows", "mirror-real", "--data", &ds, "--out", &mirror, "--n", "30"]);
    assert_eq!(code, 0, "{v}");
    assert!(v["n"].as_u64().is_some_and(|n| n > 0));

    let first = read_shadow_specs(Path::new(&sh)).expect("shadows")[0].shadow_id.clone();
    let (code, v) = core_json(&[
        "why",
        "--data",
        &ds,
        "--shadows",
        &sh,
        "--shadow-id",
        &first,
        "--max-events",
        "50",
    ]);
    assert_eq!(code, 0, "{v}");
    assert_eq!(v["schema"], "qt.why_trace");
    assert_eq!(v["subject"]["shadow_id"], first.as_str());
    assert!(v["events"].as_array().is_some_and(|e| e.len() <= 50));

    let (code, v) = core_json(&["schema", "list"]);
    assert_eq!(code, 0);
    assert!(
        v["json"]
            .as_array()
            .is_some_and(|a| a.iter().any(|n| n == "qt.replay_summary"))
    );
    assert!(v["arrow"].as_array().is_some_and(|a| a.iter().any(|n| n == "qt.mbo")));
    let (code, v) = core_json(&["schema", "show", "qt.error"]);
    assert_eq!(code, 0);
    assert_eq!(v["$schema"], "https://json-schema.org/draft/2020-12/schema");
    let (_, v) = core_json(&["schema", "show", "qt.mbo"]);
    assert_eq!(v["name"], "qt.mbo");

    let npz = dir.join("l2.npz");
    let (code, v) = core_json(&[
        "export",
        "l2-npz",
        "--data",
        &ds,
        "--instrument-id",
        "1",
        "--date",
        "2026-06-15",
        "--out",
        &s(&npz),
        "--l2-depth",
        "5",
    ]);
    assert_eq!(code, 0, "{v}");
    assert_eq!(v["schema"], "qt.export_summary");
    assert_eq!(v["target"], "l2_npz");
    let head = std::fs::read(&npz).expect("npz");
    assert_eq!(&head[..2], b"PK");

    let fx = dir.join("referee");
    let (code, v) = core_json(&["referee", "fixtures", "--out", &s(&fx)]);
    assert_eq!(code, 0, "{v}");
    assert!(v["fixtures"].as_array().is_some_and(|f| f.len() >= 11));
    assert!(fx.join("MANIFEST.sha256").exists());
}

#[test]
fn hybrid_native_parts_say_what_is_missing() {
    let dir = scratch("hybrid");
    let ds = synth();
    let sh = s(&dir.join("sh.parquet"));
    assert_eq!(
        core_json(&["shadows", "sample", "--data", &ds, "--n", "20", "--out", &sh]).0,
        0
    );
    let (code, v) = core_json(&[
        "zoo",
        "--data",
        &ds,
        "--shadows",
        &sh,
        "--out",
        &s(&dir.join("zoo")),
        "--models",
        "front_of_queue",
    ]);
    assert_eq!(code, 0, "{v}");
    assert_eq!(v["schema"], "qt.zoo_summary");
    assert_eq!(v["leaderboard"].as_array().map(Vec::len), Some(0));
    let w = v["warnings"].as_array().expect("warnings");
    assert!(
        w.iter()
            .any(|x| x.as_str().is_some_and(|t| t.contains("Python package")))
    );
}

#[test]
fn o_cli_1_exit_codes_and_error_documents() {
    let dir = scratch("ocli1");
    let ds = synth();
    // --help and --version: 0.
    let (code, out, _) = core(&["--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("Usage: queuetruth-core"));
    assert_eq!(core(&["replay", "--help"]).0, 0);
    assert_eq!(core(&["--version"]).0, 0);

    let check = |args: &[&str], want_code: &str| {
        let (code, v) = core_json(args);
        assert_eq!(code, 2, "{args:?}: {v}");
        assert_eq!(v["schema"], "qt.error", "{v}");
        assert_eq!(v["schema_version"], 1);
        assert_eq!(v["code"], want_code, "{args:?}: {v}");
        assert!(v["message"].as_str().is_some_and(|m| !m.is_empty()));
        assert!(v["details"].is_object());
    };
    // QT-USAGE-001: missing flag, bad value, Python-only command in the standalone binary.
    check(&["replay", "--out", "x"], "QT-USAGE-001");
    check(
        &["replay", "--data", &ds, "--out", "x", "--bbo", "maybe"],
        "QT-USAGE-001",
    );
    check(
        &["score", "--live", "l", "--out", "o", "--sim", "exact"],
        "QT-USAGE-001",
    );
    let (_, v) = core_json(&["score", "--live", "l", "--out", "o", "--sim", "exact"]);
    assert!(
        v["message"]
            .as_str()
            .is_some_and(|m| m.contains("requires the Python package"))
    );
    // QT-INPUT-001: dataset missing.
    check(
        &["replay", "--data", &s(&dir.join("nope")), "--out", &s(&dir.join("o1"))],
        "QT-INPUT-001",
    );
    // QT-INPUT-002: shadow file is not Parquet.
    let bad = dir.join("bad.parquet");
    std::fs::write(&bad, b"not parquet").expect("write");
    check(
        &[
            "replay",
            "--data",
            &ds,
            "--out",
            &s(&dir.join("o2")),
            "--shadows",
            &s(&bad),
        ],
        "QT-INPUT-002",
    );
    // QT-INPUT-003: duplicate shadow ids.
    let sh = dir.join("sh.parquet");
    assert_eq!(
        core_json(&["shadows", "sample", "--data", &ds, "--n", "5", "--out", &s(&sh)]).0,
        0
    );
    let mut rows = read_shadow_specs(&sh).expect("read");
    rows.push(rows[0].clone());
    let dup = dir.join("dup.parquet");
    let mut w = TableWriter::create(&dup, "qt.shadow_spec").expect("writer");
    w.write(&shadow_specs_to_batch(&rows)).expect("write");
    w.finish().expect("finish");
    check(
        &[
            "replay",
            "--data",
            &ds,
            "--out",
            &s(&dir.join("o3")),
            "--shadows",
            &s(&dup),
        ],
        "QT-INPUT-003",
    );
    // QT-CONFIG-001: unknown shadow id; unknown schema name.
    check(
        &["why", "--data", &ds, "--shadows", &s(&sh), "--shadow-id", "no-such-id"],
        "QT-CONFIG-001",
    );
    check(&["schema", "show", "qt.nothing"], "QT-CONFIG-001");
    // Exit 1: --strict gate (BBO parity against another dataset's reference fails), summary still printed.
    let wrong = repo()
        .join("tests/fixtures/synth_small_nasdaq/truth/bbo.parquet")
        .display()
        .to_string();
    let right = repo()
        .join("tests/fixtures/synth_small/truth/bbo.parquet")
        .display()
        .to_string();
    let base = [
        "replay",
        "--data",
        ds.as_str(),
        "--strict",
        "--bbo",
        "none",
        "--parity-bbo",
    ];
    let o4 = s(&dir.join("o4"));
    let (code, v) = core_json(&[&base[..], &[wrong.as_str(), "--out", o4.as_str()]].concat());
    assert_eq!(code, 1, "{v}");
    assert_eq!(v["schema"], "qt.replay_summary");
    assert_eq!(v["parity"]["pass"], false);
    assert!(
        v["warnings"]
            .as_array()
            .is_some_and(|w| w.iter().any(|x| x.as_str().is_some_and(|t| t.starts_with("strict: "))))
    );
    // Exit 0 against the dataset's own reference.
    let o5 = s(&dir.join("o5"));
    assert_eq!(
        core_json(&[&base[..], &[right.as_str(), "--out", o5.as_str()]].concat()).0,
        0
    );
    // Human errors go to stderr as `CODE: message`, nothing on stdout.
    let (code, out, err) = core(&["replay", "--data", &s(&dir.join("nope")), "--out", "x"]);
    assert_eq!(code, 2);
    assert!(out.is_empty());
    assert!(err.starts_with("QT-INPUT-001: "), "{err}");
}

fn own_ids_file(dir: &Path) -> PathBuf {
    let sched = repo().join("tests/fixtures/synth_small/own_orders/schedule.parquet");
    let ids: Vec<String> = read_shadow_specs(&sched)
        .expect("schedule")
        .iter()
        .filter_map(|s| {
            s.order_key
                .as_deref()
                .and_then(|k| k.rsplit(':').next())
                .map(str::to_owned)
        })
        .collect();
    let p = dir.join("own_ids.txt");
    std::fs::write(&p, ids.join("\n") + "\n").expect("ids");
    p
}

/// Audit A1 oracle (gate: needs the committed fixture regenerated with the current testkit and engine): the own
/// orders removed from the tape reproduce the reference replay `expected_results.parquet` byte for byte.
#[test]
#[cfg_attr(
    not(feature = "gate"),
    ignore = "gate test: engine and testkit reference must agree on the committed fixture"
)]
fn o_a1_exclusion_reproduces_reference() {
    let dir = scratch("a1");
    let own = repo().join("tests/fixtures/synth_small/own_orders");
    let out = dir.join("r");
    let (code, v) = core_json(&[
        "replay",
        "--data",
        &synth(),
        "--out",
        &s(&out),
        "--shadows",
        &s(&own.join("schedule.parquet")),
        "--bbo",
        "none",
        "--exclude-order-ids",
        &s(&own_ids_file(&dir)),
    ]);
    assert_eq!(code, 0, "{v}");
    let got = qt_schema::io::sha256_file(&out.join("shadow_results.parquet")).expect("sha");
    let want = qt_schema::io::sha256_file(&own.join("expected_results.parquet")).expect("sha");
    assert_eq!(got, want);
}

#[test]
fn replay_exclude_order_ids_and_fill_rule() {
    let dir = scratch("exclude");
    let ds = synth();
    let own = repo().join("tests/fixtures/synth_small/own_orders");
    let sched = own.join("schedule.parquet");
    let ids: Vec<String> = read_shadow_specs(&sched)
        .expect("schedule")
        .iter()
        .filter_map(|s| {
            s.order_key
                .as_deref()
                .and_then(|k| k.rsplit(':').next())
                .map(str::to_owned)
        })
        .collect();
    assert_eq!(ids.len(), 200);
    let ids_file = dir.join("own_ids.txt");
    std::fs::write(&ids_file, ids.join("\n") + "\n").expect("ids");
    let out = dir.join("r");
    let (code, v) = core_json(&[
        "replay",
        "--data",
        &ds,
        "--out",
        &s(&out),
        "--shadows",
        &s(&sched),
        "--bbo",
        "none",
        "--exclude-order-ids",
        &s(&ids_file),
    ]);
    assert_eq!(code, 0, "{v}");
    assert!(v["warnings"].as_array().is_some_and(|w| w.iter().any(|x| {
        x.as_str()
            .is_some_and(|t| t.starts_with("exclude_order_ids:") && t.contains(" matched=200 "))
    })));
    let plain = dir.join("plain");
    assert_eq!(
        core_json(&[
            "replay",
            "--data",
            &ds,
            "--out",
            &s(&plain),
            "--shadows",
            &s(&sched),
            "--bbo",
            "none"
        ])
        .0,
        0
    );
    let got = qt_schema::io::sha256_file(&out.join("shadow_results.parquet")).expect("sha");
    assert_ne!(
        got,
        qt_schema::io::sha256_file(&plain.join("shadow_results.parquet")).expect("sha")
    );
    let (code, v) = core_json(&[
        "replay",
        "--data",
        &ds,
        "--out",
        &s(&dir.join("et")),
        "--shadows",
        &s(&sched),
        "--bbo",
        "none",
        "--fill-rule",
        "et",
    ]);
    assert_eq!(code, 0, "{v}");
    let w = v["warnings"].as_array().expect("warnings");
    assert!(
        w.iter().any(|x| x.as_str().is_some_and(|t| t.contains("fill rule Et"))),
        "{w:?}"
    );
    let bad = dir.join("bad_ids.txt");
    std::fs::write(&bad, "12x\n").expect("write");
    let (code, v) = core_json(&[
        "replay",
        "--data",
        &ds,
        "--out",
        &s(&dir.join("b")),
        "--exclude-order-ids",
        &s(&bad),
    ]);
    assert_eq!((code, v["code"].as_str()), (2, Some("QT-INPUT-002")));
}

/// Open conflict 8 (E38 OC-34): `--fill-rule`, `--hidden-print-policy` and `--exclude-order-ids` are spec fields,
/// so they change `run_id` and appear in the spec echo; the same options written into the spec give the same run.
#[test]
fn replay_option_flags_enter_the_spec_and_run_id() {
    let dir = scratch("oc34");
    let ds = synth();
    let sched = repo().join("tests/fixtures/synth_small/own_orders/schedule.parquet");
    let ids = own_ids_file(&dir);
    let run = |name: &str, extra: &[&str]| -> Value {
        let out = dir.join(name);
        let mut argv = vec![
            "replay".to_owned(),
            "--data".to_owned(),
            ds.clone(),
            "--out".to_owned(),
            s(&out),
            "--shadows".to_owned(),
            s(&sched),
            "--bbo".to_owned(),
            "none".to_owned(),
        ];
        argv.extend(extra.iter().map(|x| (*x).to_owned()));
        let refs: Vec<&str> = argv.iter().map(String::as_str).collect();
        let (code, v) = core_json(&refs);
        assert_eq!(code, 0, "{v}");
        v
    };
    let plain = run("plain", &[]);
    let et = run("et", &["--fill-rule", "et"]);
    let hidden = run("hidden", &["--hidden-print-policy", "ignore"]);
    let excl = run("excl", &["--exclude-order-ids", &s(&ids)]);
    let rid = |v: &Value| v["run_id"].as_str().expect("run_id").to_owned();
    let all = [rid(&plain), rid(&et), rid(&hidden), rid(&excl)];
    let distinct: std::collections::BTreeSet<&String> = all.iter().collect();
    assert_eq!(distinct.len(), 4, "{all:?}");
    assert_eq!(et["spec"]["fill_rule"], "et");
    assert_eq!(hidden["spec"]["hidden_print_policy"], "ignore");
    let items = excl["spec"]["exclude_order_ids"]
        .as_array()
        .expect("exclude_order_ids in the spec echo");
    assert!(!items.is_empty() && items.len() % 200 == 0, "{}", items.len()); // bare ids: every instrument
    // twin-linking counters in the structured diagnostics (not only in the warning line)
    let d = &excl["shadow_diagnostics"];
    assert_eq!(d["twins_linked"], 200, "{d}");
    assert!(
        d["twin_fills_with_queue_ahead"].is_u64() && d["twin_fills_off_display"].is_u64(),
        "{d}"
    );
    assert_eq!(plain["shadow_diagnostics"]["twins_linked"], 0);
    // the same exclusion written into the spec itself is the same run
    let doc = json!({
        "dataset": {"path": ds},
        "out": s(&dir.join("from_spec")),
        "shadows": {"path": s(&sched)},
        "bbo": "none",
        "exclude_order_ids": items,
    });
    let spec: qt_schema::specs::ReplaySpec = serde_json::from_value(doc).expect("spec");
    let opts = qt_cli::ops::merged_replay_options(&spec, &Value::Null).expect("options");
    let (summary, _) = qt_cli::ops::replay_gate(&spec, &opts).expect("replay");
    assert_eq!(summary.run_id, rid(&excl));
    // and folding the CLI's option into a plain spec gives exactly that spec
    let plain_spec: qt_schema::specs::ReplaySpec = serde_json::from_value(json!({
        "dataset": {"path": ds},
        "out": s(&dir.join("from_spec")),
        "shadows": {"path": s(&sched)},
        "bbo": "none",
    }))
    .expect("spec");
    let folded =
        qt_cli::ops::fold_replay_options(&plain_spec, &json!({"exclude_order_ids": {"path": s(&ids)}})).expect("fold");
    assert_eq!(folded, spec);
}

#[test]
fn bench_subcommands_emit_qt_bench() {
    let dir = scratch("bench");
    let ds = synth();
    for args in [
        vec![
            "bench",
            "replay",
            "--data",
            ds.as_str(),
            "--repeat",
            "2",
            "--warmup",
            "0",
        ],
        vec![
            "bench",
            "replay",
            "--data",
            ds.as_str(),
            "--repeat",
            "1",
            "--warmup",
            "0",
            "--mode",
            "decode",
            "--source",
            "memory",
        ],
        vec![
            "bench",
            "l2",
            "--data",
            ds.as_str(),
            "--repeat",
            "1",
            "--warmup",
            "0",
            "--l2-depth",
            "5",
        ],
        vec![
            "bench",
            "itch",
            "--synth-messages",
            "20000",
            "--repeat",
            "1",
            "--warmup",
            "0",
        ],
    ] {
        let (code, v) = core_json(&args);
        assert_eq!(code, 0, "{args:?}: {v}");
        assert_eq!(v["schema"], "qt.bench");
        assert!(v["records"].as_u64().is_some_and(|r| r > 0));
        assert!(v["records_per_s_median"].as_f64().is_some_and(|r| r > 0.0));
    }
    let out = dir.join("sinks");
    let (code, v) = core_json(&[
        "bench",
        "replay",
        "--data",
        &ds,
        "--mode",
        "sinks",
        "--out",
        &s(&out),
        "--repeat",
        "1",
        "--warmup",
        "0",
    ]);
    assert_eq!((code, v["wall_s"].as_array().map(Vec::len)), (0, Some(1)));
    assert!(!out.exists(), "bench output is removed after the run");
    let (code, v) = core_json(&["bench", "replay", "--mode", "sinks"]);
    assert_eq!((code, v["code"].as_str()), (2, Some("QT-USAGE-001")));
}

/// Trader review item 3/5 (B2, B4): human `why` prints the event table with UTC times, decimal prices, bid/ask, the
/// fill row marked and a conclusion; human `calibrate` leads with the fitted value, flags and a verdict.
#[test]
fn human_views_answer_the_question() {
    let dir = scratch("human");
    let ds = synth();
    let sh = s(&dir.join("sh.parquet"));
    assert_eq!(
        core_json(&[
            "shadows", "sample", "--data", &ds, "--n", "40", "--out", &sh, "--seed", "5"
        ])
        .0,
        0
    );
    // A filled shadow: the trace has a fill row.
    let rep = s(&dir.join("replay"));
    assert_eq!(
        core_json(&["replay", "--data", &ds, "--out", &rep, "--shadows", &sh]).0,
        0
    );
    let results =
        qt_schema::io::read_table_as(&dir.join("replay/shadow_results.parquet"), "qt.shadow_results").expect("results");
    let mut filled = None;
    for b in results {
        let b = b.expect("batch");
        let ids = b
            .column_by_name("shadow_id")
            .and_then(|c| c.as_any().downcast_ref::<arrow_array::StringArray>().cloned())
            .expect("shadow_id");
        let q = b
            .column_by_name("filled_qty")
            .and_then(|c| c.as_any().downcast_ref::<arrow_array::UInt64Array>().cloned())
            .expect("filled_qty");
        if let Some(i) = (0..b.num_rows()).find(|&i| q.value(i) > 0) {
            filled = Some(ids.value(i).to_owned());
            break;
        }
    }
    let id = filled.expect("a filled shadow");
    let (code, out, err) = core(&["why", "--data", &ds, "--shadows", &sh, "--shadow-id", &id]);
    assert_eq!(code, 0, "{err}");
    assert!(out.starts_with(&format!("why: shadow {id} | QT")), "{out}");
    assert!(out.contains("time (UTC)") && out.contains("q.ahead"), "{out}");
    assert!(out.contains("<< FILL"), "{out}");
    assert!(out.contains("conclusion: filled"), "{out}");
    // Decimal prices (price_exp -9, ticks of 0.25), never the raw fixed-point integer.
    assert!(!out.contains("000000000 "), "{out}");
    assert!(out.contains(" bid ") || out.contains(" ask "), "{out}");
    // --verbose appends the full dump; --model needs the Python package.
    let (_, verbose, _) = core(&["why", "--data", &ds, "--shadows", &sh, "--shadow-id", &id, "--verbose"]);
    assert!(verbose.contains("all fields:"), "{verbose}");
    let (code, v) = core_json(&[
        "why",
        "--data",
        &ds,
        "--shadows",
        &sh,
        "--shadow-id",
        &id,
        "--model",
        "x",
    ]);
    assert_eq!(code, 2);
    assert!(
        v["message"]
            .as_str()
            .is_some_and(|m| m.contains("requires the Python package"))
    );

    let (code, out, err) = core(&[
        "calibrate",
        "alloc",
        "--family",
        "power3",
        "--data",
        &ds,
        "--shadows",
        &sh,
        "--out",
        &s(&dir.join("cal")),
        "--bootstrap-b",
        "5",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(out.starts_with("calibrate: power3"), "{out}");
    assert!(out.contains("\nfitted: n = "), "{out}");
    assert!(out.contains("\nflags: ") && out.contains("\nverdict: "), "{out}");
    assert!(out.contains("needs the Python package"), "{out}");
    // Informational lines are not warnings: shown only at --log-level info.
    assert!(!err.contains("warning: info:"), "{err}");
    let (_, _, err_info) = core(&[
        "calibrate",
        "alloc",
        "--family",
        "power3",
        "--data",
        &ds,
        "--shadows",
        &sh,
        "--out",
        &s(&dir.join("cal2")),
        "--bootstrap-b",
        "5",
        "--log-level",
        "info",
    ]);
    assert!(!err_info.contains("warning: info:"), "{err_info}");
    if err_info.contains("info: ") {
        assert!(!err.contains("info: "), "{err}");
    }
}

/// `zoo --amends FILE` (`ZooSpec.amends`) and `zoo --exclude-order-ids FILE` (`ZooSpec.exclude_order_ids`, file formats
/// of replay's flag; bare ids expand to every instrument of the dataset), both echoed in the summary's spec.
#[test]
fn zoo_amends_and_exclude_order_ids() {
    let dir = scratch("zoo_flags");
    let ds = synth();
    let truth = repo().join("tests/fixtures/synth_small/truth");
    let ids = own_ids_file(&dir);
    let n_ids = std::fs::read_to_string(&ids).expect("ids").lines().count();
    let (code, v) = core_json(&[
        "zoo",
        "--data",
        &ds,
        "--shadows",
        &s(&truth.join("shadows.parquet")),
        "--amends",
        &s(&truth.join("amends.parquet")),
        "--exclude-order-ids",
        &s(&ids),
        "--out",
        &s(&dir.join("zoo")),
        "--models",
        "front_of_queue",
        "--no-fills",
    ]);
    assert_eq!(code, 0, "{v}");
    assert_eq!(v["spec"]["amends"]["path"], s(&truth.join("amends.parquet")).as_str());
    let excl = v["spec"]["exclude_order_ids"]
        .as_array()
        .expect("exclude_order_ids echoed");
    // Two instruments in synth_small: every bare id applies to both.
    assert_eq!(excl.len(), 2 * n_ids, "{excl:?}");
    assert!(
        excl.iter()
            .all(|x| x["instrument_id"].is_u64() && x["order_id"].is_u64())
    );
    // A missing id file is QT-INPUT-001.
    let (code, v) = core_json(&[
        "zoo",
        "--data",
        &ds,
        "--shadows",
        &s(&truth.join("shadows.parquet")),
        "--exclude-order-ids",
        &s(&dir.join("nope.txt")),
        "--out",
        &s(&dir.join("zoo2")),
    ]);
    assert_eq!((code, v["code"].as_str()), (2, Some("QT-INPUT-001")), "{v}");
}
