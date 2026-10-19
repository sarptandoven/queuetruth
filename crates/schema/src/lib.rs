//! Data contracts (SPEC 3, 4.1 `qt_schema`): Arrow table schemas, typed rows and their record-batch codecs, Parquet
//! I/O, serde mirrors of the JSON documents and spec objects, duration parsing and run identity.

#![forbid(unsafe_code)]

mod canon;
pub mod codec;
pub mod duration;
pub mod io;
pub mod json;
pub mod rows;
pub mod specs;
mod tables;

pub use canon::{
    EXECUTION_ONLY, V1_1_DEFAULTS, canonical_json, drop_v1_1_defaults, identity_document, is_ref, run_id, spec_echo,
};
pub use qt_types::{QtError, QtResult};
pub use tables::{
    FEATURES_INSERT_AT, added_columns, dict_type, dict_values, features_columns, features_full_schema, features_schema,
    full_schema, schema, schema_dump, schema_json, schema_names, table_metadata, ts_type, type_name,
};

/// Integer major `schema_version` of every JSON document (unchanged by the additive 1.1 bump).
pub const SCHEMA_VERSION: u32 = 1;
/// `qt.schema_version` metadata of every Arrow table (SPEC 3.11: schema 1.1, additive).
pub const ARROW_SCHEMA_VERSION: &str = "1.1";
/// Engine version written into table metadata and summaries (the workspace version).
pub const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests;
