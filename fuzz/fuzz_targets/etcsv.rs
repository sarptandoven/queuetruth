//! `cargo +nightly fuzz run etcsv`: see `qt_fuzz::etcsv` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::etcsv(data));
