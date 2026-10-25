//! `cargo +nightly fuzz run crypto_kraken_l3`: see `qt_fuzz::crypto_kraken_l3` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::crypto_kraken_l3(data));
