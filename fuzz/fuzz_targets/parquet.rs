//! `cargo +nightly fuzz run parquet`: see `qt_fuzz::parquet` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::parquet(data));
