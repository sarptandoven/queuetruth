//! `queuetruth-core`: standalone binary for the native command subset (SPEC 4.4). Python-only commands exit 2 with
//! `QT-USAGE-001`; hybrid commands run their native part and say what is missing in `warnings`.

#![forbid(unsafe_code)]

use std::io::Write;

fn main() {
    let argv: Vec<String> = std::env::args().collect();
    let (out, err, code) = qt_cli::main_with(&argv);
    if !out.is_empty() {
        let mut o = std::io::stdout().lock();
        let _ = writeln!(o, "{}", out.trim_end());
        let _ = o.flush();
    }
    if !err.is_empty() {
        let mut e = std::io::stderr().lock();
        let _ = write!(e, "{err}");
        if !err.ends_with('\n') {
            let _ = writeln!(e);
        }
    }
    std::process::exit(code);
}
