//! `cargo +nightly fuzz run crypto_tardis`: see `qt_fuzz::crypto_tardis` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::crypto_tardis(data));
