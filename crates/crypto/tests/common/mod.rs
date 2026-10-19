//! Shared helpers for the crypto converter tests.
#![allow(dead_code)]

use qt_types::{MboEvent, QtResult};

/// `(action, side, price, size, order_id, flags, sequence)` with action/side as chars.
pub type Act = (char, char, i64, u64, u64, u8, u64);

pub fn acts(rows: &[MboEvent]) -> Vec<Act> {
    rows.iter()
        .map(|r| {
            (
                r.action as char,
                r.side as char,
                r.price,
                r.size,
                r.order_id,
                r.flags,
                r.sequence,
            )
        })
        .collect()
}

pub fn lines(v: &[String]) -> impl Iterator<Item = QtResult<String>> + '_ {
    v.iter().map(|s| Ok(s.clone()))
}

pub fn fixture(rel: &str) -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(rel)
}

/// Fresh empty directory under the system temp dir.
pub fn tmpdir(name: &str) -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("qt_crypto_{name}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

pub fn get(c: &qt_crypto::Counters, k: &str) -> u64 {
    *c.get(k).unwrap_or_else(|| panic!("counter {k} missing: {c:?}"))
}
