- **Rust spec structs generated from the JSON Schemas.** `crates/schema/src/specs_gen.rs` is written by
  `scripts/gen_rust_specs.py` (step `rust-specs` of `make generate`) from `schemas/json/qt.*_spec.schema.json`, so the
  Rust mirrors of the specs can no longer drift from the published contract. `specs.rs` keeps only methods and checks.
