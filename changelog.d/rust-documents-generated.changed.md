- **Rust document structs generated from the JSON Schemas.** `crates/schema/src/json_gen.rs` is written by
  `scripts/gen_rust_specs.py` (step `rust-specs` of `make generate`) from the document schemas, as the spec structs
  are; `json.rs` keeps only the tag types, aliases and methods.
