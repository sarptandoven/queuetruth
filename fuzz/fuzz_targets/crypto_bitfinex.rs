//! `cargo +nightly fuzz run crypto_bitfinex`: see `qt_fuzz::crypto_bitfinex` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::crypto_bitfinex(data));
