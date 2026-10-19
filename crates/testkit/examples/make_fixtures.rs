//! Writes the committed synthetic fixtures: `tests/fixtures/synth_small/` (CME), `tests/fixtures/synth_small_nasdaq/`
//! (Nasdaq, hidden prints; plus `own_orders/` and the censoring simulation for audit rulings A1/A2 in synth_small) and
//! `tests/fixtures/referee/<bug_class>/`, each directory with its own
//! `MANIFEST.sha256`. Usage: `cargo run --locked --release -p queuetruth-testkit --example make_fixtures [ROOT]`
//! (default ROOT = `<repo>/tests/fixtures`). Prints a `qt.fixtures_summary` document to stdout.

#![forbid(unsafe_code)]

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use qt_schema::io::FileEntry;
use qt_schema::json::{FixtureEntry, FixturesSummary};
use qt_testkit::calendar::{NS_PER_S, local_midnight};
use qt_testkit::fixtures::{fixture_config, partition_stats, readme, write_audit_inputs, write_synth_fixture};
use qt_testkit::{bugclasses, write_manifest_sha256};
use qt_types::{QtError, QtResult, Venue};

fn entries(dir: &Path, listed: Vec<(String, String)>) -> QtResult<Vec<FileEntry>> {
    listed
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
        .collect()
}

fn run() -> QtResult<FixturesSummary> {
    let root = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures"));
    std::fs::create_dir_all(&root)?;
    let mut fixtures = Vec::new();
    for (name, venue) in [("synth_small", Venue::Cme), ("synth_small_nasdaq", Venue::Nasdaq)] {
        let dir = root.join(name);
        let cfg = fixture_config(venue);
        let out = write_synth_fixture(&dir, &cfg, 2, "")?;
        let mut text = readme(name, &cfg, 2, &out);
        if venue == Venue::Cme {
            text.push_str(&write_audit_inputs(&dir, &out, cfg.seed)?);
        }
        std::fs::write(dir.join("README.txt"), text)?;
        for inst in &out.instruments {
            let close = local_midnight(venue, inst.session_date) + i64::from(inst.close_s) * NS_PER_S;
            let last = out
                .events
                .iter()
                .filter(|e| {
                    e.instrument_id == inst.instrument_id && e.ts_event >= local_midnight(venue, inst.session_date)
                })
                .filter(|e| e.ts_event < local_midnight(venue, inst.session_date + 1))
                .map(|e| e.ts_event)
                .max()
                .unwrap_or(0);
            if last < close {
                return Err(QtError::Engine(format!(
                    "{name}: instrument {} ends before the close",
                    inst.instrument_id
                )));
            }
        }
        for (d, i, r, e, _, _) in partition_stats(&out) {
            eprintln!("{name} {d} instrument {i}: {r} records, {e} events");
        }
        let listed = write_manifest_sha256(&dir)?;
        fixtures.push(FixtureEntry {
            name: name.to_owned(),
            path: format!("tests/fixtures/{name}"),
            files: entries(&dir, listed)?,
        });
    }
    let referee = root.join("referee");
    if referee.exists() {
        std::fs::remove_dir_all(&referee)?;
    }
    let summary = bugclasses::write_fixtures(&referee)?;
    let listed = write_manifest_sha256(&referee)?;
    fixtures.push(FixtureEntry {
        name: "referee".to_owned(),
        path: "tests/fixtures/referee".to_owned(),
        files: entries(&referee, listed)?,
    });
    eprintln!("referee: {} bug-class fixtures", summary.fixtures.len());
    Ok(FixturesSummary {
        out: root.display().to_string(),
        fixtures,
        ..FixturesSummary::default()
    })
}

fn main() -> ExitCode {
    match run() {
        Ok(s) => match serde_json::to_string(&s) {
            Ok(text) => {
                println!("{text}");
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("QT-ENGINE-001: {e}");
                ExitCode::from(2)
            }
        },
        Err(e) => {
            eprintln!("{e}");
            ExitCode::from(2)
        }
    }
}
