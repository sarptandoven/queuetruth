//! Command-line grammar shared by the `queuetruth` console script and the `queuetruth-core` binary, and the native
//! runner (SPEC 0.2, 4.1 `qt_cli`, 4.4).
//!
//! `parse` turns argv into a `CliCommand` whose `args` are the spec object of SPEC 4.2 for the command (only the
//! fields given on the command line; spec defaults apply to the rest); options that are not spec fields sit under
//! the reserved key `_cli`. `run_native` runs the Rust commands and the native part of the hybrid ones. Global
//! `--format human|json` is told apart from `ingest --format <source format>` by its value.

#![forbid(unsafe_code)]

mod bench;
pub mod formats;
mod grammar;
mod human;
mod live_importers;
mod native;
pub mod operations;
pub mod ops;
mod python_commands;
pub mod run_op;
mod schemas;
mod spec_check;

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

/// Global flags that take a value (skipped when looking for the command word).
const VALUE_FLAGS: &[&str] = &["--format", "--output-format", "--log-level", "--threads", "--seed"];

/// The command line as the commands run it, and the deprecation line to print on stderr after the command.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Routed {
    /// argv (program name first) with `advanced` dropped before a command word: `advanced zoo ...` runs `zoo ...`,
    /// `help advanced zoo` is `help zoo`.
    pub argv: Vec<String>,
    /// For an advanced command spelled at the top level, one line naming the new spelling and the verb that does
    /// its job; None with `--quiet`, with `QT_NO_DEPRECATION=1`, for help (`--help`, `help CMD`), through
    /// `advanced`, and for `doctor`, `mcp` and `bench`.
    pub deprecation: Option<String>,
}

/// Index of the command word in argv (after the program name and any global flags).
fn command_index(argv: &[String]) -> Option<usize> {
    let mut i = 1;
    while i < argv.len() {
        let a = argv[i].as_str();
        if a == "--" {
            return None;
        }
        if !a.starts_with('-') {
            return Some(i);
        }
        i += if VALUE_FLAGS.contains(&a) { 2 } else { 1 };
    }
    None
}

fn word_at(argv: &[String], i: usize) -> Option<&str> {
    argv.get(i).map(String::as_str).filter(|w| !w.starts_with('-'))
}

/// The deprecation line of an advanced command path spelled at the top level.
fn deprecation_line(path: &[&str]) -> String {
    let typed = format!("queuetruth {}", path.join(" "));
    // the longest prefix names the verb; a verb this build does not have yet is not named
    let to = grammar::REPLACED_BY
        .iter()
        .filter(|(p, _)| path.starts_with(p))
        .max_by_key(|(p, _)| p.len())
        .map(|(_, to)| *to)
        .filter(|to| to.split(' ').next().is_some_and(grammar::is_command));
    let use_ = match to {
        Some(to) if grammar::VERBS.contains(&to.split(' ').next().unwrap_or("")) => {
            format!(
                "use `queuetruth {to}`, or `queuetruth advanced {}` for this command",
                path.join(" ")
            )
        }
        Some(to) => format!("use `queuetruth {to}`"),
        None => format!("use `queuetruth advanced {}`", path.join(" ")),
    };
    format!("warning: `{typed}` is deprecated; {use_} (QT_NO_DEPRECATION=1 hides this line)")
}

/// Routes a command line before it is parsed (both binaries): drops `advanced` before a command word and, for an
/// advanced command spelled at the top level, returns its deprecation line. Outputs and exit codes do not change.
pub fn route(argv: &[String]) -> Routed {
    let mut out = argv.to_vec();
    let Some(i) = command_index(argv) else {
        return Routed {
            argv: out,
            deprecation: None,
        };
    };
    let word = argv[i].as_str();
    if word == grammar::ADVANCED {
        if word_at(argv, i + 1).is_some() {
            out.remove(i);
        }
        return Routed {
            argv: out,
            deprecation: None,
        };
    }
    if word == "help" {
        if word_at(argv, i + 1) == Some(grammar::ADVANCED) && word_at(argv, i + 2).is_some() {
            out.remove(i + 1);
        }
        return Routed {
            argv: out,
            deprecation: None,
        };
    }
    // help is printed the same under both spellings, without the line
    let help = argv[i..].iter().any(|a| a == "--help" || a == "-h");
    let quiet =
        help || argv.iter().any(|a| a == "--quiet") || std::env::var("QT_NO_DEPRECATION").is_ok_and(|v| v == "1");
    if quiet || !grammar::is_advanced(word) || grammar::UNDEPRECATED.contains(&word) {
        return Routed {
            argv: out,
            deprecation: None,
        };
    }
    let mut path = vec![word];
    if let Some(sub) = word_at(argv, i + 1)
        && grammar::all_commands().any(|c| c.path.len() == 2 && c.path[0] == word && c.path[1] == sub)
    {
        path.push(sub);
    }
    Routed {
        argv: out,
        deprecation: Some(deprecation_line(&path)),
    }
}

/// Parses argv (including the program name). `advanced CMD ...` parses to the same command as `CMD ...`.
pub fn parse(argv: &[String]) -> Result<CliCommand, CliExit> {
    let json_out = wants_json(argv) || runs_operation_argv(argv);
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
    let (mut path, leaf) = grammar::leaf_matches(&m);
    if path.first().map(String::as_str) == Some(grammar::ADVANCED) {
        path.remove(0);
    }
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
        .map_err(|msg| usage_exit(&msg, None, json_out || global.format == OutputFormat::Json))?;
    Ok(CliCommand {
        path,
        global,
        args: Value::Object(args),
        implementation,
    })
}

/// `zoo --list-models`: the `all` set with one line per model, then the families that run only when named. Both
/// come from the zoo family registry (`qt_zoo::FAMILIES`).
fn list_models(json_out: bool) -> String {
    use qt_zoo::{FAMILIES, FamilyDef, families::short_id};
    // the ids as typed on the command line: mode, exch and particles come from --mode, --exchange, --particles
    let all: Vec<(String, &FamilyDef)> = FAMILIES
        .iter()
        .flat_map(|d| d.in_all.iter().map(move |p| (short_id(d.name, p), d)))
        .collect();
    let other: Vec<&FamilyDef> = FAMILIES
        .iter()
        .filter(|d| d.in_all.is_empty() || !d.param_keys.is_empty())
        .collect();
    let template = |d: &FamilyDef| {
        let keys: Vec<String> = d
            .param_keys
            .iter()
            .filter(|k| **k != "particles")
            .map(|k| format!("{k}={}", k.to_ascii_uppercase()))
            .collect();
        format!("{}[{}]", d.name, keys.join(";"))
    };
    if json_out {
        let row = |id: String, d: &FamilyDef| serde_json::json!({"id": id, "family": d.name, "summary": d.summary});
        let doc = serde_json::json!({
            "all": all.iter().map(|(id, d)| row(id.clone(), d)).collect::<Vec<_>>(),
            "other": other.iter().map(|d| row(template(d), d)).collect::<Vec<_>>(),
        });
        return doc.to_string();
    }
    let tpl: Vec<String> = other.iter().map(|d| template(d)).collect();
    let w = all
        .iter()
        .map(|(id, _)| id)
        .chain(&tpl)
        .map(String::len)
        .max()
        .unwrap_or(0);
    let mut out = format!(
        "--models all ({} models; mode from --mode, hftbacktest exchange from --exchange, particles from --particles):\n",
        all.len()
    );
    for (id, d) in &all {
        out.push_str(&format!("  {id:<w$}  {}\n", d.summary));
    }
    out.push_str("\nother parameters or families (name them in --models):\n");
    for (t, d) in tpl.iter().zip(&other) {
        out.push_str(&format!("  {t:<w$}  {}\n", d.summary));
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

/// Every command of the grammar with its flags (the Python-only groups last).
pub fn command_table() -> Vec<CommandInfo> {
    grammar::all_commands()
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
/// The deprecation line, if any, is the last line of stderr (the answer comes first).
pub fn main_with(argv: &[String]) -> (String, String, i32) {
    let routed = route(argv);
    let (out, mut err, code) = run_routed(&routed.argv);
    if let Some(line) = routed.deprecation {
        if !err.is_empty() && !err.ends_with('\n') {
            err.push('\n');
        }
        err.push_str(&line);
        err.push('\n');
    }
    (out, err, code)
}

/// argv runs `run <op>` (or `advanced run <op>`), not `run --list`: its usage errors are JSON too.
fn runs_operation_argv(argv: &[String]) -> bool {
    let Some(i) = command_index(argv) else {
        return false;
    };
    let at = |j: usize| argv.get(j).map(String::as_str);
    let run = at(i) == Some("run") || (at(i) == Some(grammar::ADVANCED) && at(i + 1) == Some("run"));
    run && !argv[i..].iter().any(|a| a == "--list" || a == "--help" || a == "-h")
}

/// `run <op> --spec FILE`: its document and errors are JSON whatever `--format` says.
pub fn runs_operation(cmd: &CliCommand) -> bool {
    cmd.path == ["run"] && !cmd.args.get("list").and_then(Value::as_bool).unwrap_or(false)
}

fn run_routed(argv: &[String]) -> (String, String, i32) {
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
    // `run <op>` prints the operation's document, and its errors, as JSON (`run --list` follows --format)
    let json_out = cmd.global.format == OutputFormat::Json || runs_operation(&cmd);
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
