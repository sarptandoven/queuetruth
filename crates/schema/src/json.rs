//! Serde mirrors of the JSON documents of SPEC 3.10 (source of truth: `python/queuetruth/schemas/models.py`).
//!
//! The structs and enums are generated from the published JSON Schemas into `json_gen.rs` (`make generate`, step
//! `rust-specs`, `scripts/gen_rust_specs.py`); this file holds what cannot be generated: the `schema` tag and
//! `schema_version` types, aliases, methods and the `dataset_id` rule.
//!
//! Field names and order equal the Pydantic models; `schema` / `schema_version` serialize first and are checked on
//! deserialization. Optional (`?`) fields serialize as `null`. Maps keep insertion order (`IndexMap`); producers
//! insert horizon keys in ascending numeric order. The round trip of every `schemas/samples/*.json` document through
//! these types is tested. Service-only documents of SPEC 4.5 (tenant, keys, uploads, ...) have no Rust mirror.
//!
//! Schema 1.1 (SPEC 3.11): fields marked "1.1" are additive `Option`s (null = not reported, the v0.1 behaviour) and
//! are deserialized as `None` when absent, so v0.1 documents stay readable; `schema_version` stays 1.

use std::marker::PhantomData;

use indexmap::IndexMap;
use qt_types::{ValidationStatus, Venue};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{Map, Value};

pub use crate::io::FileEntry;
pub use crate::json_gen::*;
pub use crate::specs::WindowSpec;

/// A top-level document type with its `qt.<name>` schema tag.
pub trait QtDoc {
    /// The `schema` value.
    const SCHEMA: &'static str;
}

/// Serializes as the document's schema name; deserialization rejects any other name.
pub struct Tag<T>(PhantomData<fn() -> T>);

impl<T> Default for Tag<T> {
    fn default() -> Self {
        Self(PhantomData)
    }
}
impl<T> Clone for Tag<T> {
    fn clone(&self) -> Self {
        *self
    }
}
impl<T> Copy for Tag<T> {}
impl<T> PartialEq for Tag<T> {
    fn eq(&self, _: &Self) -> bool {
        true
    }
}
impl<T> Eq for Tag<T> {}
impl<T: QtDoc> std::fmt::Debug for Tag<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(T::SCHEMA)
    }
}
impl<T: QtDoc> Serialize for Tag<T> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(T::SCHEMA)
    }
}
impl<'de, T: QtDoc> Deserialize<'de> for Tag<T> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let s = String::deserialize(d)?;
        if s == T::SCHEMA {
            Ok(Self(PhantomData))
        } else {
            Err(serde::de::Error::custom(format!(
                "expected schema {:?}, found {s:?}",
                T::SCHEMA
            )))
        }
    }
}

/// `schema_version`: serializes as `1`; deserialization rejects other values.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct V1;

impl Serialize for V1 {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_u32(crate::SCHEMA_VERSION)
    }
}
impl<'de> Deserialize<'de> for V1 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = u32::deserialize(d)?;
        if v == crate::SCHEMA_VERSION {
            Ok(V1)
        } else {
            Err(serde::de::Error::custom(format!("unsupported schema_version {v}")))
        }
    }
}

/// `{h: v}` map keyed by the horizon in ns as a decimal string; `None` = undefined.
pub type HMap = IndexMap<String, Option<f64>>;
/// Free-form JSON object.
pub type Object = Map<String, Value>;
/// Output files.
pub type Files = Vec<FileEntry>;

/// Horizon map key for a duration in ns.
pub fn hkey(h_ns: i64) -> String {
    h_ns.to_string()
}

impl ErrorDoc {
    /// The error document for a `QtError`.
    pub fn from_error(e: &qt_types::QtError) -> Self {
        Self {
            code: e.code().to_owned(),
            message: e.message(),
            ..Default::default()
        }
    }
}

// ---- datasets ---------------------------------------------------------------------------------------------------

impl DatasetManifest {
    /// The effective rule set: `rule_set`, else the venue's own.
    pub fn effective_rule_set(&self) -> qt_types::RuleSet {
        self.rule_set.unwrap_or_else(|| qt_types::RuleSet::from(self.venue))
    }
}

impl Default for DatasetManifest {
    fn default() -> Self {
        Self {
            schema: Tag::default(),
            schema_version: V1,
            dataset_id: String::new(),
            kind: DatasetKindName::Mbo,
            source_format: String::new(),
            venue: Venue::Generic,
            source_venue: String::new(),
            inputs: Vec::new(),
            partitions: Vec::new(),
            price_exp_by_instrument: IndexMap::new(),
            created_by: String::new(),
            validation_status: ValidationStatus::SyntheticOnly,
            rule_set: None,
            source_vintage: None,
            attribution: None,
        }
    }
}

/// `dataset_id` rule (SPEC 3.10): sha256 of the sorted partition-file sha256s joined by `\n`, then `\n` and
/// `source_format`.
pub fn dataset_id(file_sha256s: &[String], source_format: &str) -> String {
    let mut v: Vec<&str> = file_sha256s.iter().map(String::as_str).collect();
    v.sort_unstable();
    let text = format!("{}\n{source_format}", v.join("\n"));
    crate::io::sha256_bytes(text.as_bytes())
}
