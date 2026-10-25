//! `cargo +nightly fuzz run crypto_coinbase`: see `qt_fuzz::crypto_coinbase` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::crypto_coinbase(data));
