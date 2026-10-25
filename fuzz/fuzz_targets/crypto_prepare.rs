//! `cargo +nightly fuzz run crypto_prepare`: see `qt_fuzz::crypto_prepare` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::crypto_prepare(data));
