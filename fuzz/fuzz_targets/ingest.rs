//! `cargo +nightly fuzz run ingest`: see `qt_fuzz::ingest` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::ingest(data));
