//! `cargo +nightly fuzz run dbn`: see `qt_fuzz::dbn` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::dbn(data));
