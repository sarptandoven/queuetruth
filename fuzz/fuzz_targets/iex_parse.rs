//! `cargo +nightly fuzz run iex_parse`: see `qt_fuzz::iex_parse` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::iex_parse(data));
