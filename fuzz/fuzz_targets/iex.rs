//! `cargo +nightly fuzz run iex`: see `qt_fuzz::iex` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::iex(data));
