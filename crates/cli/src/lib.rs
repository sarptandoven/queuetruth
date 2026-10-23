//! Command-line grammar shared by the `queuetruth` console script and the `queuetruth-core` binary, and the native
//! runner (SPEC 0.2, 4.1 `qt_cli`, 4.4).
//!
//! `parse` turns argv into a `CliCommand` whose `args` are the spec object of SPEC 4.2 for the command (only the
//! fields given on the command line; spec defaults apply to the rest); options that are not spec fields sit under
//! the reserved key `_cli`. `run_native` runs the Rust commands and the native part of the hybrid ones. Global
//! `--format human|json` is told apart from `ingest --format <source format>` by its value.

#![forbid(unsafe_code)]

mod bench;
mod grammar;
mod human;
mod native;
pub mod ops;
mod schemas;

use clap::error::ErrorKind;
use qt_schema::json::ErrorDoc;
use qt_types::{QtError, QtResult};
use serde_json::Value;

pub use human::{render_human, render_human_cmd};

/// Early exit of argument parsing (help/version: code 0 to stdout; usage errors: code 2 to stderr).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct CliExit {
    /// Exit code.
    pub code: i32,
    /// Text to print.
    pub text: String,
    /// Print to stderr instead of stdout.
    pub to_stderr: bool,
}

/// True when argv asks for JSON output (`--format json`, `--output-format json`, or the `=` forms).
fn wants_json(argv: &[String]) -> bool {
    argv.windows(2)
        .any(|w| (w[0] == "--format" || w[0] == "--output-format") && w[1] == "json")
        || argv.iter().any(|a| a == "--format=json" || a == "--output-format=json")
}

/// The error document (`qt.error`) of an error.
pub fn error_document(e: &QtError, hint: Option<&str>) -> Value {
    let mut doc = ErrorDoc::from_error(e);
    doc.hint = hint.map(str::to_owned);
    serde_json::to_value(&doc).unwrap_or(Value::Null)
}

/// Prints an error for the given output format: JSON error document on stdout, or `CODE: message` on stderr.
pub fn error_exit(e: &QtError, hint: Option<&str>, json_out: bool) -> CliExit {
    if json_out {
        CliExit {
            code: 2,
            text: error_document(e, hint).to_string(),
            to_stderr: false,
        }
    } else {
        let mut text = e.to_string();
        if let Some(h) = hint {
            text.push_str("\nhint: ");
            text.push_str(h);
        }
        CliExit {
            code: 2,
            text,
            to_stderr: true,
        }
    }
}

fn usage_exit(msg: &str, detail: Option<&str>, json_out: bool) -> CliExit {
    let e = QtError::Usage(msg.to_owned());
    if json_out {
        return error_exit(&e, Some("run with --help"), true);
    }
    let mut text = e.to_string();
    if let Some(d) = detail.filter(|d| !d.trim().is_empty()) {
        text.push_str("\n\n");
        text.push_str(d.trim_end());
    }
    CliExit {
        code: 2,
        text,
        to_stderr: true,
    }
}

/// Parses argv (including the program name).
pub fn parse(argv: &[String]) -> Result<CliCommand, CliExit> {
    let json_out = wants_json(argv);
    let bin: &'static str = match argv.first() {
        Some(p) if p.ends_with("queuetruth-core") => "queuetruth-core",
        _ => "queuetruth",
    };
    let rewritten = grammar::rewrite_format(argv);
    let m = match grammar::command(bin).try_get_matches_from(&rewritten) {
        Ok(m) => m,
        Err(e) => {
            let text = e.render().to_string();
            return Err(match e.kind() {
                ErrorKind::DisplayHelp | ErrorKind::DisplayVersion => CliExit {
                    code: 0,
                    text,
                    to_stderr: false,
                },
                ErrorKind::DisplayHelpOnMissingArgumentOrSubcommand => {
                    usage_exit("a subcommand is required", Some(&text), json_out)
                }
                _ => {
                    let (head, tail) = text.split_once("\nUsage:").unwrap_or((text.as_str(), ""));
                    let msg = head
                        .trim_start_matches("error: ")
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ");
                    let detail = if tail.is_empty() {
                        String::new()
                    } else {
                        format!("Usage:{tail}")
                    };
                    usage_exit(&msg, Some(&detail), json_out)
                }
            });
        }
    };
    let format = match m.get_one::<String>("output_format").map(String::as_str) {
        Some("json") => OutputFormat::Json,
        _ => OutputFormat::Human,
    };
    let global = GlobalOpts {
        format,
        log_level: m
            .get_one::<String>("log_level")
            .cloned()
            .unwrap_or_else(|| "warn".to_owned()),
        threads: m.get_one::<u32>("threads").copied().unwrap_or(1) as usize,
        seed: m.get_one::<u64>("seed").copied().unwrap_or(0),
        quiet: m.get_flag("quiet"),
        verbose: m.get_flag("verbose"),
    };
    let (path, leaf) = grammar::leaf_matches(&m);
    let Some(cmd) = grammar::find(&path) else {
        return Err(usage_exit(
            &format!("unknown command {:?}", path.join(" ")),
            None,
            json_out,
        ));
    };
    if grammar::lists_models(cmd, leaf) {
        return Err(CliExit {
            code: 0,
            text: list_models(global.format == OutputFormat::Json),
            to_stderr: false,
        });
    }
    let (args, implementation) = grammar::build_args(cmd, leaf, &m)
        .map_err(|msg| usage_exit(&msg, None, global.format == OutputFormat::Json))?;
    Ok(CliCommand {
        path,
        global,
        args: Value::Object(args),
        implementation,
    })
}

/// `zoo --list-models`: the `all` set with one line per model, then the families that run only when named.
fn list_models(json_out: bool) -> String {
    use qt_zoo::{Exchange, Family, Mode};
    let all = qt_zoo::default_models(Mode::Faithful, Exchange::Partial);
    // the id as typed on the command line: mode, exch and particles come from --mode, --exchange, --particles
    let short = |m: &qt_zoo::ModelId| {
        let ps: Vec<String> = m
            .params
            .iter()
            .filter(|(k, _)| k.as_str() != "particles")
            .map(|(k, v)| format!("{k}={v}"))
            .collect();
        if ps.is_empty() {
            m.family.name().to_owned()
        } else {
            format!("{}[{}]", m.family.name(), ps.join(";"))
        }
    };
    let other: Vec<Family> = Family::ALL
        .iter()
        .copied()
        .filter(|f| !all.iter().any(|m| m.family == *f) || !f.param_keys().is_empty())
        .collect();
    let template = |f: Family| {
        let keys: Vec<String> = f
            .param_keys()
            .iter()
            .filter(|k| **k != "particles")
            .map(|k| format!("{k}={}", k.to_ascii_uppercase()))
            .collect();
        format!("{}[{}]", f.name(), keys.join(";"))
    };
    if json_out {
        let row = |id: String, f: Family| serde_json::json!({"id": id, "family": f.name(), "summary": f.summary()});
        let doc = serde_json::json!({
            "all": all.iter().map(|m| row(short(m), m.family)).collect::<Vec<_>>(),
            "other": other.iter().map(|f| row(template(*f), *f)).collect::<Vec<_>>(),
        });
        return doc.to_string();
    }
    let ids: Vec<String> = all.iter().map(short).collect();
    let tpl: Vec<String> = other.iter().map(|f| template(*f)).collect();
    let w = ids.iter().chain(&tpl).map(String::len).max().unwrap_or(0);
    let mut out = format!(
        "--models all ({} models; mode from --mode, hftbacktest exchange from --exchange, particles from --particles):\n",
        all.len()
    );
    for (id, m) in ids.iter().zip(&all) {
        out.push_str(&format!("  {id:<w$}  {}\n", m.family.summary()));
    }
    out.push_str("\nother parameters or families (name them in --models):\n");
    for (t, f) in tpl.iter().zip(&other) {
        out.push_str(&format!("  {t:<w$}  {}\n", f.summary()));
    }
    out.push_str(
        "\nexample: --models \"hbt_power3[n=2],nt_prob_touch[p=0.3]\"\n\
         rules: https://github.com/sarptandoven/queuetruth/blob/main/docs/concepts/l2-model-zoo.md",
    );
    out
}

/// Output format.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputFormat {
    /// Human tables.
    #[default]
    Human,
    /// Exactly one JSON document on stdout.
    Json,
}

/// Global flags.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct GlobalOpts {
    /// `--format`.
    pub format: OutputFormat,
    /// `--log-level`.
    pub log_level: String,
    /// `--threads`.
    pub threads: usize,
    /// `--seed`.
    pub seed: u64,
    /// `--quiet`.
    pub quiet: bool,
    /// `--verbose`: human output lists every summary field after the answer (left out of the JSON when false).
    #[serde(default, skip_serializing_if = "is_false")]
    pub verbose: bool,
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_false(b: &bool) -> bool {
    !*b
}

/// Where a command runs.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Implementation {
    /// Rust only.
    Native,
    /// Python only.
    Python,
    /// Rust part plus Python part.
    Hybrid,
}

/// A parsed command.
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct CliCommand {
    /// Command path, e.g. `["shadows", "sample"]`.
    pub path: Vec<String>,
    /// Global flags.
    pub global: GlobalOpts,
    /// Spec-shaped arguments.
    pub args: serde_json::Value,
    /// Implementation.
    pub implementation: Implementation,
}

/// Output of a native command.
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct NativeOutput {
    /// Summary JSON document.
    pub summary: serde_json::Value,
    /// A requested gate failed (exit code 1).
    pub gate_failed: bool,
}

/// One row of the command table (for docs and tests).
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct CommandInfo {
    /// Command path.
    pub path: Vec<&'static str>,
    /// Where it runs.
    pub implementation: Implementation,
    /// Long flag names.
    pub flags: Vec<&'static str>,
    /// Positional argument names.
    pub positional: Vec<&'static str>,
}

/// Every command of the grammar with its flags.
pub fn command_table() -> Vec<CommandInfo> {
    grammar::COMMANDS
        .iter()
        .map(|c| CommandInfo {
            path: c.path.to_vec(),
            implementation: c.imp,
            flags: c.flags.iter().map(|f| f.long).collect(),
            positional: c.positional.iter().map(|p| p.0).collect(),
        })
        .collect()
}

/// Runs a native command (or the native part of a hybrid one); Python-only commands are `QT-USAGE-001`.
pub fn run_native(cmd: &CliCommand) -> QtResult<NativeOutput> {
    native::run(cmd)
}

/// Runs a full command line in the standalone binary: returns (stdout text, stderr text, exit code).
pub fn main_with(argv: &[String]) -> (String, String, i32) {
    let cmd = match parse(argv) {
        Ok(c) => c,
        Err(x) => {
            return if x.to_stderr {
                (String::new(), x.text, x.code)
            } else {
                (x.text, String::new(), x.code)
            };
        }
    };
    let json_out = cmd.global.format == OutputFormat::Json;
    if cmd.implementation == Implementation::Python {
        let e = QtError::Usage(format!(
            "`{}` requires the Python package (pip install queuetruth)",
            cmd.path.join(" ")
        ));
        let x = error_exit(&e, None, json_out);
        return (
            if x.to_stderr { String::new() } else { x.text.clone() },
            if x.to_stderr { x.text } else { String::new() },
            2,
        );
    }
    match run_native(&cmd) {
        Ok(out) => {
            let code = if out.gate_failed { 1 } else { 0 };
            let text = if json_out {
                out.summary.to_string()
            } else {
                render_human_cmd(&cmd, &out.summary)
            };
            let mut err = String::new();
            if !json_out && !cmd.global.quiet {
                for w in out
                    .summary
                    .get("warnings")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if let Some(s) = w.as_str() {
                        // Informational lines only at --log-level info|debug, and not labelled as warnings.
                        if s.starts_with("info: ") {
                            if matches!(cmd.global.log_level.as_str(), "info" | "debug") {
                                err.push_str(s);
                                err.push('\n');
                            }
                            continue;
                        }
                        err.push_str("warning: ");
                        err.push_str(s);
                        err.push('\n');
                    }
                }
                if out.gate_failed {
                    err.push_str("QT-GATE-001: a requested gate failed\n");
                }
            }
            (text, err, code)
        }
        Err(e) => {
            let x = error_exit(&e, None, json_out);
            if x.to_stderr {
                (String::new(), x.text, 2)
            } else {
                (x.text, String::new(), 2)
            }
        }
    }
}
