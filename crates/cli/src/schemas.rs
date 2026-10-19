//! The published JSON Schemas (`schemas/json`, generated from the Pydantic models), embedded at build time by
//! `build.rs` so that `schema list|show` works in the standalone binary and in an installed wheel.

include!(concat!(env!("OUT_DIR"), "/json_schemas.rs"));
