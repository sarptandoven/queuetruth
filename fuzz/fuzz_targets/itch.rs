//! `cargo +nightly fuzz run itch`: see `qt_fuzz::itch` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::itch(data));
