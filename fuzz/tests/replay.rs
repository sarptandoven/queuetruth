//! Runs every committed seed (`seeds/<target>/`) and every minimized crash reproducer (`regressions/<target>/`)
//! through its harness on the stable toolchain: `cargo test` from `fuzz/`. A reproducer that panics again fails
//! here, so fixed fuzz findings stay fixed without nightly or libFuzzer. `known-crashes/<kind>/` holds reported
//! findings in code this crate does not fix; they must keep reproducing until fixed, then move to `regressions/`.
//!
//! Each test thread writes to its own scratch directory under `.scratch/fuzz-tmp` and removes it when it ends.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Removes the calling thread's harness scratch directory when dropped (also when an assertion fails).
struct Scratch;

impl Drop for Scratch {
    fn drop(&mut self) {
        qt_fuzz::remove_scratch();
    }
}

fn root() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
}

/// Sorted regular files of a directory (none when it is missing).
fn files(dir: &Path) -> Vec<PathBuf> {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut v: Vec<_> = rd
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.is_file())
        .collect();
    v.sort();
    v
}

/// Replays `<kind>/<target>/*` through each target's harness; returns the number of files per target.
fn replay(kind: &str) -> BTreeMap<&'static str, usize> {
    let _s = Scratch;
    let mut counts = BTreeMap::new();
    for &(name, harness) in qt_fuzz::TARGETS {
        let fs = files(&root().join(kind).join(name));
        for f in &fs {
            let data = std::fs::read(f).unwrap();
            let r = std::panic::catch_unwind(|| harness(&data));
            assert!(
                r.is_ok(),
                "{kind}/{name}/{} panicked",
                f.file_name().unwrap().to_string_lossy()
            );
        }
        counts.insert(name, fs.len());
    }
    counts
}

/// Subdirectory names of `<root>/<kind>/`.
fn subdirs(kind: &str) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(root().join(kind))
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    v.sort();
    v
}

#[test]
fn seeds_pass_every_harness() {
    let counts = replay("seeds");
    for (name, n) in counts {
        assert!(n > 0, "{name}: no seeds");
    }
}

#[test]
fn regressions_stay_fixed() {
    let counts = replay("regressions");
    // Every regressions/<dir> belongs to a target and replays at least one input, so a renamed or emptied directory
    // fails here instead of silently testing nothing.
    for d in subdirs("regressions") {
        let n = counts.get(d.as_str()).copied();
        assert!(n.is_some_and(|n| n > 0), "regressions/{d}: not a target or empty");
    }
    let total: usize = counts.values().sum();
    assert!(total >= 3, "{total} regression inputs (3 committed)");
}

#[test]
fn every_target_has_a_binary_and_seeds() {
    let manifest = std::fs::read_to_string(root().join("Cargo.toml")).unwrap();
    for (name, _) in qt_fuzz::TARGETS {
        assert!(
            root().join("fuzz_targets").join(format!("{name}.rs")).is_file(),
            "{name}: no fuzz_targets file"
        );
        assert!(
            manifest.contains(&format!("name = \"{name}\"")),
            "{name}: no [[bin]] in Cargo.toml"
        );
        assert!(root().join("seeds").join(name).is_dir(), "{name}: no seeds");
    }
    let bins = manifest.matches("[[bin]]").count();
    assert_eq!(bins, qt_fuzz::TARGETS.len(), "one [[bin]] per target");
}

/// Every `known-crashes/<kind>/` has a test below that keeps it honest.
#[test]
fn known_crash_directories_are_tested() {
    assert_eq!(subdirs("known-crashes"), ["parquet"]);
}

/// The Parquet decode panics (reported, not fixed here): every reproducer still panics inside the `parquet` / `arrow`
/// crates without the harness filter, and the filtered harness skips it. When this test fails because an input no
/// longer panics, the Parquet reader was hardened: move the inputs to `regressions/parquet/` and drop the filter.
#[test]
fn known_parquet_crashes_still_reproduce() {
    let _s = Scratch;
    let fs = files(&root().join("known-crashes/parquet"));
    assert!(fs.len() >= 6, "{} reproducers", fs.len());
    for f in fs {
        let data = std::fs::read(&f).unwrap();
        assert!(
            qt_fuzz::parquet_known_panic(&data),
            "{} no longer panics inside parquet / arrow",
            f.display()
        );
        qt_fuzz::parquet(&data);
    }
}
