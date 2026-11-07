//! `FrameCheck` keeps the two faults of the pinned `dbn` 0.71.0 decoder that fuzzing found away from it
//! (`fuzz/regressions/dbn/`): a metadata block of 100 to 103 bytes (the decoder reads the schema-definition length past
//! its end and panics) and a block longer than the file can hold (the decoder allocates the announced length up
//! front). Both are input errors (`QT-INPUT-002`), raised before the decoder runs.

use std::path::{Path, PathBuf};

use qt_ingest::dbn::{DbnReader, MIN_METADATA_BYTES};

fn regressions() -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fuzz/regressions/dbn");
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().path()).collect();
    v.sort();
    v
}

#[test]
fn known_dbn_decoder_faults_are_input_errors() {
    let files = regressions();
    assert!(files.len() >= 6, "{files:?}");
    let tmp = std::env::temp_dir().join(format!("qt-dbn-guard-{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    for f in files {
        // Fuzz inputs are `[selector][file]`.
        let data = std::fs::read(&f).unwrap();
        let path = tmp.join("input.dbn");
        std::fs::write(&path, &data[1..]).unwrap();
        let err = match std::panic::catch_unwind(|| DbnReader::open(&path)) {
            Ok(Err(e)) => e,
            Ok(Ok(_)) => panic!("{}: opened", f.display()),
            Err(_) => panic!("{}: panicked", f.display()),
        };
        assert_eq!(err.code(), "QT-INPUT-002", "{}: {err}", f.display());
        let msg = err.to_string();
        assert!(
            msg.contains("shorter than its fixed fields") || msg.contains("cannot fit in this file"),
            "{}: {msg}",
            f.display()
        );
    }
    let _ = std::fs::remove_dir_all(&tmp);
    assert_eq!(MIN_METADATA_BYTES, 104);
}
