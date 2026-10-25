//! `cargo +nightly fuzz run itch_parse`: see `qt_fuzz::itch_parse` for the input layout and the oracles.

#![no_main]

libfuzzer_sys::fuzz_target!(|data: &[u8]| qt_fuzz::itch_parse(data));
