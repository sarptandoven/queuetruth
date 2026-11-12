//! Serde mirrors of the spec objects of SPEC 4.2 (source of truth: `python/queuetruth/schemas/specs.py`).
//!
//! The structs and enums are generated from the published JSON Schemas into `specs_gen.rs` (`make generate`, step
//! `rust-specs`, `scripts/gen_rust_specs.py`): field names, order, types, defaults and `deny_unknown_fields` come from
//! the schema, so they cannot drift from it. This file holds what cannot be generated: methods, the `calibrate` kind
//! check and the names shared with `crate::json`.
//!
//! Unknown fields are rejected; defaults equal the Pydantic defaults (tested against `schemas/samples/*.json`).
//! Range constraints are validated by the Python models and by the consuming engine (`QT-CONFIG-001`).
//!
//! Schema 1.1 (SPEC 3.11, E38): fields marked "1.1" in `specs.py` are additive and default to the v0.1 behaviour;
//! a 1.1 field holding its default does not enter `run_id` (`crate::V1_1_DEFAULTS`).

use serde::{Deserialize, Serialize};

pub use crate::json::{CalibrationKind, ExportTarget, Recalibration};
pub use crate::live_venue::{LIVE_VENUE_CLI_NAMES, LiveVenue};
pub use crate::specs_gen::*;

impl DatasetRef {
    /// `{"path": p}`.
    pub fn path(p: impl Into<String>) -> Self {
        Self::Path(PathRef { path: p.into() })
    }

    /// The local path, if this is a path reference.
    pub fn as_path(&self) -> Option<&str> {
        match self {
            Self::Path(p) => Some(&p.path),
            Self::DatasetId(_) => None,
        }
    }
}

impl FileRef {
    /// `{"path": p}`.
    pub fn path(p: impl Into<String>) -> Self {
        Self::Path(PathRef { path: p.into() })
    }

    /// The local path, if this is a path reference.
    pub fn as_path(&self) -> Option<&str> {
        match self {
            Self::Path(p) => Some(&p.path),
            _ => None,
        }
    }
}

/// Default zoo / calibration / scorer horizons (the schema default of `ZooSpec.horizons_ns`; tested).
pub const DEFAULT_HORIZONS_NS: [i64; 4] = [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000];

/// `CalibrationSpec.kind`: `live` is a `calibration_result` kind only and is rejected here, as in `specs.py`.
pub(crate) fn spec_calibration_kind<'de, D: serde::Deserializer<'de>>(d: D) -> Result<CalibrationKind, D::Error> {
    match CalibrationKind::deserialize(d)? {
        CalibrationKind::Live => Err(serde::de::Error::custom(
            "kind: `live` is a calibration_result kind; use LiveCalibrationSpec (calibrate-live)",
        )),
        k => Ok(k),
    }
}

/// One string or a list of strings (`str | list[str]` in `specs.py`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum OneOrMany {
    /// One value.
    One(String),
    /// Several values.
    Many(Vec<String>),
}

impl OneOrMany {
    /// The values as a list.
    pub fn to_vec(&self) -> Vec<String> {
        match self {
            Self::One(s) => vec![s.clone()],
            Self::Many(v) => v.clone(),
        }
    }
}
