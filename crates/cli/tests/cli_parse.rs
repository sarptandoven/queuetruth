//! `cli_parse` golden test: every command and every flag of the grammar parsed into the expected `CliCommand` JSON
//! (`tests/golden/cli_parse.json`; regenerate with `QT_BLESS=1` and review the diff), plus usage-error cases.

use std::path::PathBuf;

use qt_cli::{CliCommand, Implementation, OutputFormat, command_table, parse};
use serde_json::Value;

fn golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/golden")
}

fn argv_cases() -> Vec<Vec<String>> {
    let text = std::fs::read_to_string(golden_dir().join("cli_argv.json")).expect("cli_argv.json");
    serde_json::from_str(&text).expect("argv list")
}

fn parse_ok(argv: &[String]) -> CliCommand {
    match parse(argv) {
        Ok(c) => c,
        Err(x) => panic!("{argv:?} -> exit {}: {}", x.code, x.text),
    }
}

#[test]
fn cli_parse_golden() {
    let got: Vec<Value> = argv_cases()
        .iter()
        .map(|a| {
            serde_json::json!({
                "argv": a,
                "command": serde_json::to_value(parse_ok(a)).expect("json"),
            })
        })
        .collect();
    let path = golden_dir().join("cli_parse.json");
    let text = serde_json::to_string_pretty(&got).expect("json") + "\n";
    if std::env::var_os("QT_BLESS").is_some() {
        std::fs::write(&path, &text).expect("write golden");
    }
    let want = std::fs::read_to_string(&path).expect("cli_parse.json (run once with QT_BLESS=1)");
    assert_eq!(text, want, "parsed commands differ from tests/golden/cli_parse.json");
}

#[test]
fn cli_parse_covers_every_command_and_flag() {
    let cases = argv_cases();
    for info in command_table() {
        let mine: Vec<&Vec<String>> = cases
            .iter()
            .filter(|a| {
                let words: Vec<&str> = a
                    .iter()
                    .skip(1)
                    .filter(|w| !w.starts_with('-'))
                    .map(String::as_str)
                    .collect();
                words.starts_with(&info.path)
            })
            .collect();
        assert!(!mine.is_empty(), "no golden case for {:?}", info.path);
        for f in &info.flags {
            if *f == "list-models" {
                continue; // prints and exits instead of parsing (`zoo_list_models_exits_zero`)
            }
            let flag = format!("--{f}");
            let alias = (*f == "format").then_some("--input-format");
            assert!(
                mine.iter()
                    .any(|a| a.iter().any(|w| *w == flag || Some(w.as_str()) == alias)),
                "no golden case for {:?} {flag}",
                info.path
            );
        }
    }
}

#[test]
fn zoo_list_models_exits_zero() {
    let a = |v: &[&str]| -> Vec<String> { v.iter().map(|s| (*s).to_owned()).collect() };
    let x = parse(&a(&["queuetruth", "zoo", "--list-models"])).expect_err("prints and exits");
    assert_eq!((x.code, x.to_stderr), (0, false));
    assert!(x.text.starts_with("--models all (16 models;"), "{}", x.text);
    assert!(x.text.contains("hbt_power3[n=2]") && !x.text.contains("  hbt_power[n=1] "));
    let j = parse(&a(&["queuetruth", "zoo", "--list-models", "--format", "json"])).expect_err("json");
    let v: Value = serde_json::from_str(&j.text).expect("one JSON document");
    assert_eq!(v["all"].as_array().map(Vec::len), Some(16));
    // without --list-models the zoo's required flags stay required
    let e = parse(&a(&["queuetruth", "zoo", "--data", "d"])).expect_err("missing flags");
    assert_eq!(e.code, 2);
    assert!(e.text.contains("--shadows") && e.text.contains("--out"), "{}", e.text);
}

#[test]
fn unit_less_durations_need_a_unit() {
    let a = |v: &[&str]| -> Vec<String> { v.iter().map(|s| (*s).to_owned()).collect() };
    let base = [
        "queuetruth",
        "shadows",
        "sample",
        "--data",
        "d",
        "--n",
        "5",
        "--out",
        "o",
    ];
    let e = parse(&a(&[&base[..], &["--lifetime", "5"]].concat())).expect_err("unit-less");
    assert!(
        e.text.contains(r#"--lifetime: "5" has no unit; write 5s"#),
        "{}",
        e.text
    );
    let ok = parse_ok(&a(&[&base[..], &["--lifetime", "0"]].concat()));
    assert_eq!(ok.args["lifetime_ns"], 0);
    let ok = parse_ok(&a(&[&base[..], &["--lifetime", "5s", "--latency-ns", "250"]].concat()));
    assert_eq!(
        (ok.args["lifetime_ns"].as_i64(), ok.args["latency_ns"].as_i64()),
        (Some(5_000_000_000), Some(250))
    );
}

#[test]
fn new_flags_map_to_spec_fields_and_cli_extras() {
    let a = |v: &[&str]| -> Vec<String> { v.iter().map(|s| (*s).to_owned()).collect() };
    // live import: FIX options and tz are LiveImportSpec fields (schema 1.1); simulator options are `_cli`.
    let c = parse_ok(&a(&[
        "queuetruth",
        "live",
        "import",
        "--venue",
        "fix44",
        "--in",
        "x.log",
        "--out",
        "l",
        "--fix-local-ts-regex",
        "^(\\S+)",
        "--fix-venue-name",
        "cb",
        "--tz",
        "UTC",
    ]));
    assert_eq!(c.args["fix"]["local_ts_regex"], "^(\\S+)");
    assert_eq!(c.args["fix"]["venue_name"], "cb");
    let c2 = parse_ok(&a(&[
        "queuetruth",
        "live",
        "import",
        "--venue",
        "fix44",
        "--in",
        "x.log",
        "--out",
        "l",
        "--fix-session-venue",
        "S1=123",
        "--fix-data-dictionary",
        "d.xml",
    ]));
    assert_eq!(c2.args["fix"]["session_venues"]["S1"], "123");
    assert_eq!(c2.args["fix"]["data_dictionary"][0], "d.xml");
    assert_eq!(c.args["tz"], "UTC");
    assert!(c.args.get("_cli").is_none());
    let c = parse_ok(&a(&[
        "queuetruth",
        "live",
        "import",
        "--venue",
        "sim-hftbacktest",
        "--in",
        "x.jsonl",
        "--out",
        "l",
        "--option",
        "size_exp=-8",
        "--option",
        "venue=binance",
    ]));
    assert_eq!(c.args["venue"], "sim_hftbacktest");
    assert_eq!(c.args["_cli"]["options"]["size_exp"], -8);
    assert_eq!(c.args["_cli"]["options"]["venue"], "binance");
    // why --model / --zoo: `_cli`, and the command becomes hybrid (the model column is merged in Python).
    let c = parse_ok(&a(&[
        "queuetruth",
        "why",
        "--data",
        "d",
        "--shadows",
        "s",
        "--shadow-id",
        "x",
        "--model",
        "front_of_queue",
    ]));
    assert_eq!(c.implementation, Implementation::Hybrid);
    assert_eq!(c.args["_cli"]["model"], "front_of_queue");
    let c = parse_ok(&a(&[
        "queuetruth",
        "why",
        "--data",
        "d",
        "--shadows",
        "s",
        "--shadow-id",
        "x",
    ]));
    assert_eq!(c.implementation, Implementation::Native);
    // export replay-schedule keeps --target; --force is `ExportSpec.force` (1.1); --verbose is a global flag (omitted
    // when off).
    let c = parse_ok(&a(&[
        "queuetruth",
        "export",
        "replay-schedule",
        "--target",
        "nautilus",
        "--live",
        "l",
        "--out",
        "s.py",
        "--verbose",
    ]));
    assert_eq!(c.args["target"], "nautilus");
    assert_eq!(c.args["live"]["path"], "l");
    assert!(c.global.verbose);
    let v = serde_json::to_value(parse_ok(&a(&["queuetruth", "schema", "list"]))).expect("json");
    assert!(v["global"].get("verbose").is_none());
    let c = parse_ok(&a(&[
        "queuetruth",
        "export",
        "nautilus",
        "--calibration",
        "c",
        "--force",
    ]));
    assert_eq!(c.args["force"], true);
    assert_eq!(c.args["target"], "nautilus");
}

/// Every `--fix-*` flag of `live import` lands on its schema-1.1 `FixOptions` field (deserialized by the Rust spec
/// mirror, so a renamed or dropped field fails here), and the 1.1 spec fields of `report`, `fit-hazard`,
/// `calibrate-live` and `features` are reachable from their flags.
#[test]
fn v1_1_flags_deserialize_into_the_spec_mirrors() {
    use qt_schema::specs::{
        FeatureSpec, FixChecksum, FixOptions, HazardFitSpec, LiveCalibrationSpec, LiveImportSpec, ReportSpec,
    };
    let a = |v: &[&str]| -> Vec<String> { v.iter().map(|s| (*s).to_owned()).collect() };
    let spec = |c: &CliCommand| -> serde_json::Value {
        let mut v = c.args.clone();
        v.as_object_mut().map(|o| o.remove("_cli"));
        v
    };
    let c = parse_ok(&a(&[
        "queuetruth",
        "live",
        "import",
        "--venue",
        "fix44",
        "--in",
        "x.log",
        "--out",
        "l",
        "--fix-delimiter",
        "|",
        "--fix-local-ts-regex",
        "^(\\S+)",
        "--fix-venue-name",
        "cme",
        "--fix-checksum",
        "keep",
        "--fix-data-dictionary",
        "a.xml",
        "--fix-data-dictionary",
        "b.xml",
        "--fix-session-venue",
        "FIX.4.4-ME-CME=cme",
        "--fix-instrument-tag",
        "107",
    ]));
    let li: LiveImportSpec = serde_json::from_value(spec(&c)).expect("LiveImportSpec");
    let fx = li.fix;
    assert_ne!(fx, FixOptions::default());
    assert_eq!(fx.delimiter, "|");
    assert_eq!(fx.local_ts_regex.as_deref(), Some("^(\\S+)"));
    assert_eq!(fx.venue_name.as_deref(), Some("cme"));
    assert_eq!(fx.checksum, FixChecksum::Keep);
    assert_eq!(
        fx.data_dictionary.map(|d| d.to_vec()),
        Some(vec!["a.xml".to_owned(), "b.xml".to_owned()])
    );
    assert_eq!(fx.session_venues.get("FIX.4.4-ME-CME").map(String::as_str), Some("cme"));
    assert_eq!(fx.instrument_tag, Some(107));

    let c = parse_ok(&a(&[
        "queuetruth",
        "report",
        "--in",
        "z",
        "--out",
        "r.html",
        "--sort-horizon",
        "10s",
    ]));
    let r: ReportSpec = serde_json::from_value(spec(&c)).expect("ReportSpec");
    assert_eq!(r.leaderboard_sort_horizon_ns, Some(10_000_000_000));
    let c = parse_ok(&a(&["queuetruth", "report", "--in", "z", "--out", "r.html"]));
    let r: ReportSpec = serde_json::from_value(spec(&c)).expect("ReportSpec");
    assert_eq!(r.leaderboard_sort_horizon_ns, None);

    let c = parse_ok(&a(&[
        "queuetruth",
        "fit-hazard",
        "--features",
        "f",
        "--out",
        "m",
        "--zoo-predictions",
        "z/zoo_predictions.parquet",
        "--zoo-shadows",
        "z/zoo_shadows.parquet",
    ]));
    let h: HazardFitSpec = serde_json::from_value(spec(&c)).expect("HazardFitSpec");
    assert_eq!(
        h.zoo_shadows.as_ref().and_then(|f| f.as_path()),
        Some("z/zoo_shadows.parquet")
    );

    let c = parse_ok(&a(&[
        "queuetruth",
        "calibrate-live",
        "--data",
        "d",
        "--live",
        "l",
        "--family",
        "power3",
        "--match-window-ns",
        "2ms",
        "--no-self-exclude",
    ]));
    let lc: LiveCalibrationSpec = serde_json::from_value(spec(&c)).expect("LiveCalibrationSpec");
    assert_eq!((lc.self_exclude, lc.match_window_ns), (false, 2_000_000));
    let c = parse_ok(&a(&[
        "queuetruth",
        "calibrate-live",
        "--data",
        "d",
        "--live",
        "l",
        "--family",
        "power",
    ]));
    let lc: LiveCalibrationSpec = serde_json::from_value(spec(&c)).expect("LiveCalibrationSpec");
    assert_eq!((lc.self_exclude, lc.match_window_ns), (true, 1_000_000));

    let c = parse_ok(&a(&[
        "queuetruth",
        "features",
        "--data",
        "d",
        "--out",
        "f",
        "--shadows",
        "s",
        "--cancel-away-ticks",
        "0",
    ]));
    let fs: FeatureSpec = serde_json::from_value(spec(&c)).expect("FeatureSpec");
    assert_eq!(fs.cancel_away_ticks, Some(0));
}

/// Schema 1.2 ingest formats: each CLI spelling deserializes into its `IngestFormat` (and options stay JSON).
#[test]
fn v1_2_ingest_formats_deserialize_into_the_spec_mirror() {
    use qt_schema::specs::{IngestFormat, IngestSpec};
    for (cli, want) in [
        ("coinbase-level3", IngestFormat::CoinbaseLevel3),
        ("kraken-l3", IngestFormat::KrakenL3),
        ("hyperliquid-node", IngestFormat::HyperliquidNode),
        ("iex-deep-plus", IngestFormat::IexDeepPlus),
    ] {
        let a: Vec<String> = [
            "queuetruth",
            "ingest",
            "--format",
            cli,
            "--in",
            "x",
            "--venue",
            "generic",
            "--out",
            "o",
            "--option",
            "checksum_precision={\"BTC/USD\":[1,8]}",
            "--option",
            "depth=100",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        let c = parse_ok(&a);
        assert_eq!(c.implementation, Implementation::Native);
        let mut v = c.args.clone();
        v.as_object_mut().map(|o| o.remove("_cli"));
        let spec: IngestSpec = serde_json::from_value(v).expect("IngestSpec");
        assert_eq!(spec.format, want, "{cli}");
        assert_eq!(spec.options["depth"], 100);
        assert_eq!(spec.options["checksum_precision"]["BTC/USD"][1], 8);
    }
}

#[test]
fn global_format_and_ingest_format_are_told_apart() {
    let a: Vec<String> = [
        "queuetruth",
        "ingest",
        "--format",
        "itch",
        "--in",
        "x",
        "--venue",
        "nasdaq",
        "--out",
        "o",
        "--format",
        "json",
    ]
    .iter()
    .map(|s| (*s).to_owned())
    .collect();
    let c = parse_ok(&a);
    assert_eq!(c.global.format, OutputFormat::Json);
    assert_eq!(c.args["format"], "itch");
    assert_eq!(c.implementation, Implementation::Native);
    let b: Vec<String> = ["queuetruth", "--format=json", "replay", "--data", "d", "--out", "o"]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
    assert_eq!(parse_ok(&b).global.format, OutputFormat::Json);
}

fn exit_of(args: &[&str]) -> qt_cli::CliExit {
    let a: Vec<String> = args.iter().map(|s| (*s).to_owned()).collect();
    match parse(&a) {
        Ok(c) => panic!("{args:?} parsed: {c:?}"),
        Err(x) => x,
    }
}

#[test]
fn usage_errors_and_help() {
    let help = exit_of(&["queuetruth", "--help"]);
    assert_eq!((help.code, help.to_stderr), (0, false));
    assert!(help.text.contains("Usage: queuetruth"));
    let sub = exit_of(&["queuetruth", "replay", "--help"]);
    assert_eq!((sub.code, sub.to_stderr), (0, false));
    let v = exit_of(&["queuetruth", "--version"]);
    assert_eq!(
        (v.code, v.text.trim()),
        (0, format!("queuetruth {}", env!("CARGO_PKG_VERSION")).as_str())
    );
    for bad in [
        vec!["queuetruth", "replay", "--out", "o"],
        vec![
            "queuetruth",
            "replay",
            "--data",
            "d",
            "--out",
            "o",
            "--bbo",
            "sometimes",
        ],
        vec!["queuetruth", "replay", "--data", "d", "--out", "o", "--latency", "1,2"],
        vec![
            "queuetruth",
            "replay",
            "--data",
            "d",
            "--out",
            "o",
            "--sweep-gap-ns",
            "2 hours",
        ],
        vec!["queuetruth", "score", "--live", "l", "--out", "o"],
        vec![
            "queuetruth",
            "score",
            "--live",
            "l",
            "--out",
            "o",
            "--sim",
            "exact",
            "--sim-log",
            "x",
        ],
        vec![
            "queuetruth",
            "score",
            "--live",
            "l",
            "--out",
            "o",
            "--sim",
            "exact",
            "--l2-depth",
            "3",
        ],
        vec!["queuetruth", "why", "--data", "d", "--shadows", "s"],
        vec!["queuetruth", "why", "--data", "d"],
        vec![
            "queuetruth",
            "calibrate",
            "alloc",
            "--data",
            "d",
            "--shadows",
            "s",
            "--out",
            "o",
        ],
        vec![
            "queuetruth",
            "calibrate",
            "alloc",
            "--family",
            "power3",
            "--data",
            "d",
            "--shadows",
            "s",
            "--out",
            "o",
            "--train",
            "blocks:4-6/5",
        ],
        vec![
            "queuetruth",
            "shadows",
            "sample",
            "--data",
            "d",
            "--n",
            "1",
            "--out",
            "o",
            "--qty",
            "half",
        ],
        vec!["queuetruth", "nonsense"],
        vec!["queuetruth"],
        // FIX options with another venue; importer options with a real venue.
        vec![
            "queuetruth",
            "live",
            "import",
            "--venue",
            "binance",
            "--in",
            "a.csv",
            "--out",
            "l",
            "--fix-delimiter",
            "|",
        ],
        vec![
            "queuetruth",
            "live",
            "import",
            "--venue",
            "fix44",
            "--in",
            "a.log",
            "--out",
            "l",
            "--option",
            "size_exp=-8",
        ],
        vec![
            "queuetruth",
            "live",
            "import",
            "--venue",
            "sim-zipline",
            "--in",
            "a",
            "--out",
            "l",
        ],
        // replay-schedule: exactly one of --schedule / --live; --amends only with --schedule.
        vec![
            "queuetruth",
            "export",
            "replay-schedule",
            "--target",
            "nautilus",
            "--out",
            "s.py",
        ],
        vec![
            "queuetruth",
            "export",
            "replay-schedule",
            "--target",
            "nautilus",
            "--schedule",
            "s.parquet",
            "--entry",
            "exchange-ack",
            "--out",
            "s.py",
        ],
        vec![
            "queuetruth",
            "export",
            "replay-schedule",
            "--target",
            "hftbacktest",
            "--live",
            "l",
            "--amends",
            "a.parquet",
            "--out",
            "s.py",
        ],
        vec![
            "queuetruth",
            "export",
            "replay-schedule",
            "--target",
            "backtrader",
            "--live",
            "l",
            "--out",
            "s.py",
        ],
        vec![
            "queuetruth",
            "zoo",
            "--data",
            "d",
            "--shadows",
            "s",
            "--out",
            "o",
            "--sort-horizon",
            "soon",
        ],
    ] {
        let x = exit_of(&bad);
        assert_eq!((x.code, x.to_stderr), (2, true), "{bad:?}");
        assert!(x.text.starts_with("QT-USAGE-001: "), "{bad:?}: {}", x.text);
    }
    let j = exit_of(&["queuetruth", "--format", "json", "replay", "--out", "o"]);
    assert_eq!((j.code, j.to_stderr), (2, false));
    let doc: Value = serde_json::from_str(&j.text).expect("qt.error JSON");
    assert_eq!(doc["schema"], "qt.error");
    assert_eq!(doc["schema_version"], 1);
    assert_eq!(doc["code"], "QT-USAGE-001");
    assert!(doc["message"].as_str().is_some_and(|m| m.contains("--data")));
}

/// `--fail-abs-bias` is a fraction (`0.02` = 2 pp) and also takes percentage points with a `pp` suffix; anything
/// else is a usage error, and the help states the unit.
#[test]
fn fail_abs_bias_is_a_fraction_or_pp() {
    let argv = |x: &str| -> Vec<String> {
        [
            "queuetruth",
            "score",
            "--live",
            "l",
            "--out",
            "o",
            "--sim-model",
            "front_of_queue",
            "--fail-abs-bias",
            x,
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect()
    };
    for (x, want) in [("0.02", 0.02), ("2pp", 0.02), ("2 pp", 0.02), ("0.5pp", 0.005)] {
        let c = parse_ok(&argv(x));
        let got = c.args["gates"]["max_abs_fill_rate_bias"].as_f64().expect("number");
        assert!((got - want).abs() < 1e-15, "{x}: {got}");
    }
    for bad in ["2%", "pp", "two"] {
        let e = parse(&argv(bad)).err().unwrap_or_else(|| panic!("{bad} accepted"));
        assert_eq!(e.code, 2, "{bad}: {}", e.text);
    }
    let help = parse(&["queuetruth", "score", "--help"].map(String::from)).expect_err("help");
    assert!(help.text.contains("e.g. 0.02 = 2 pp"), "{}", help.text);
}

/// The Python-only groups (run by `queuetruth.cli.main` before the grammar) are in the grammar for `--help`, the
/// command table and the reference docs; `queuetruth-core` refuses them like every Python command.
#[test]
fn python_only_groups_are_listed_and_documented() {
    let help = exit_of(&["queuetruth", "--help"]).text;
    for word in [
        "demo",
        "forecast",
        "ledger",
        "pm",
        "certify",
        "verify-cert",
        "commit",
        "reveal",
        "cert",
        "zoo-toxicity",
    ] {
        assert!(
            help.lines().any(|l| l.trim_start().starts_with(&format!("{word} "))),
            "{word} missing from --help:\n{help}"
        );
    }
    let table = command_table();
    let python = |p: &[&str]| {
        table
            .iter()
            .find(|c| c.path == p)
            .is_some_and(|c| c.implementation == Implementation::Python)
    };
    assert!(python(&["demo"]) && python(&["forecast", "score"]) && python(&["cert", "partial-opening"]));
    // `help X` and `X --help` print the same clap help, with the argparse flags, value names and descriptions
    let a = exit_of(&["queuetruth", "help", "forecast", "score"]);
    let b = exit_of(&["queuetruth", "forecast", "score", "--help"]);
    assert_eq!((a.code, &a.text), (0, &b.text));
    assert!(b.text.contains("--kelly-fraction <KELLY_FRACTION>"), "{}", b.text);
    let demo = exit_of(&["queuetruth", "demo", "--help"]).text;
    assert!(
        demo.contains("-y, --yes") && demo.contains("Writes <out>/demo.html"),
        "{demo}"
    );
    let certify = exit_of(&["queuetruth", "certify", "--help"]).text;
    assert!(certify.contains("<RUN_DIR>..."), "{certify}");
    let kinds = exit_of(&["queuetruth", "ledger", "list", "--kind", "nope"]);
    assert_eq!(kinds.code, 2);
    assert!(kinds.text.contains("possible values"), "{}", kinds.text);
    let argv: Vec<String> = ["queuetruth-core", "ledger", "verify"].map(String::from).to_vec();
    let (out, err, code) = qt_cli::main_with(&argv);
    assert_eq!((out.as_str(), code), ("", 2));
    assert!(err.contains("`ledger verify` requires the Python package"), "{err}");
}

/// `mcp serve --allow-run` is the flag form of `QT_MCP_ALLOW_RUN=1`; absent, the variable decides.
#[test]
fn mcp_serve_allow_run() {
    let on = parse_ok(&["queuetruth", "mcp", "serve", "--allow-run"].map(String::from));
    assert_eq!(on.args, serde_json::json!({"allow_run": true}));
    let off = parse_ok(&["queuetruth", "mcp", "serve"].map(String::from));
    assert_eq!(off.args, serde_json::json!({}));
}

/// Enum values are hyphenated on the command line: `--allocation per-partition` is `per_partition`.
#[test]
fn allocation_per_partition_is_hyphenated() {
    let argv = |x: &str| -> Vec<String> {
        [
            "queuetruth",
            "shadows",
            "sample",
            "--data",
            "d",
            "--n",
            "3",
            "--out",
            "o",
            "--allocation",
            x,
        ]
        .map(String::from)
        .to_vec()
    };
    assert_eq!(parse_ok(&argv("per-partition")).args["allocation"], "per_partition");
    assert_eq!(
        exit_of(&argv("per_partition").iter().map(String::as_str).collect::<Vec<_>>()).code,
        2
    );
}
