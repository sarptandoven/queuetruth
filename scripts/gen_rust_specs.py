"""Generates the Rust serde structs of the contracts from the published JSON Schemas:

- `crates/schema/src/specs_gen.rs`: the spec objects (`schemas/json/qt.*_spec.schema.json`, with the spec samples
  `schemas/samples/qt.*_spec.json` for defaults);
- `crates/schema/src/json_gen.rs`: the JSON documents (`schemas/json/qt.*.schema.json` of `DOCS`).

The JSON Schema is the contract. This file only adds what a JSON Schema cannot say about Rust: type names of inline
enums and unions, integer widths, the `#[default]` variant of document enums, fields left out when empty (so older
files keep their bytes) and the few types defined elsewhere (one `Config` per generated file below). Every binding is
checked against the schemas, so a stale entry or a new field without a Rust name fails the generator with the field
it is about.

Spec defaults come from the schema (`default`) or, for a Pydantic `default_factory` (which a JSON Schema does not
record), from the `defaults` document of the spec's sample, which is the spec validated from its required fields
only. Methods, validation and conversions stay in the hand-written `specs.rs` and `json.rs`.

    uv run --frozen --no-sync python scripts/gen_rust_specs.py           # write the files
    uv run --frozen --no-sync python scripts/gen_rust_specs.py --check   # exit 1 when a committed file differs

Standard library only (plus `rustfmt` from the toolchain). Step `rust-specs` of `make generate`, after `schemas`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = "crates/schema/src/specs_gen.rs"

#: The specs mirrored in Rust, in the order of the generated file (top-level struct name = CamelCase of the name).
SPECS = (
    "qt.ingest_spec",
    "qt.replay_spec",
    "qt.shadow_sample_spec",
    "qt.mirror_spec",
    "qt.zoo_spec",
    "qt.calibration_spec",
    "qt.feature_spec",
    "qt.hazard_fit_spec",
    "qt.live_import_spec",
    "qt.scorecard_spec",
    "qt.report_spec",
    "qt.referee_spec",
    "qt.why_spec",
    "qt.export_spec",
    "qt.live_calibration_spec",
)

# ---- the Rust binding: what a JSON Schema cannot say ------------------------------------------------------------

#: Rust name of each inline string enum, with every `Struct.field` that uses it (`[]` = the items of a list).
ENUMS: dict[str, tuple[str, ...]] = {
    "TradePositionName": ("L2Config.trade_position",),
    "BootstrapBlock": ("BootstrapSpec.block",),
    "QtyKind": ("QtyRule.kind",),
    "IngestFormat": ("IngestSpec.format",),
    "SessionTz": ("IngestSpec.tz",),
    "BboModeName": ("ReplaySpec.bbo",),
    "TrajectoryModeName": ("ReplaySpec.trajectory",),
    "FillRuleName": ("ReplaySpec.fill_rule",),
    "HiddenPrintPolicyName": ("ReplaySpec.hidden_print_policy",),
    "Allocation": ("ShadowSampleSpec.allocation",),
    "SideName": ("ShadowSampleSpec.sides[]",),
    "MirrorPlacement": ("MirrorSpec.placement",),
    "ModeName": ("ZooSpec.mode", "CalibrationSpec.mode", "LiveCalibrationSpec.mode"),
    "ExchangeName": ("ZooSpec.exchange", "CalibrationSpec.exchange", "LiveCalibrationSpec.exchange"),
    "ZooBootstrapBlock": ("ZooSpec.bootstrap_block",),
    "CalibrationFamily": ("CalibrationSpec.family",),
    "PopulationName": ("FeatureSpec.population",),
    "SplitScheme": ("SplitSpec.scheme",),
    "FixChecksum": ("FixOptions.checksum",),
    "EntryName": ("ScorecardSpec.entry", "LiveCalibrationSpec.entry"),
    "MarkoutPastEnd": ("ScorecardSpec.markout_past_end",),
    "LiveCalibrationFamily": ("LiveCalibrationSpec.family",),
}
#: Variant identifiers that are not the CamelCase of the value.
VARIANTS = {"30min": "Min30", "1min": "Min1", "America/New_York": "AmericaNewYork", "UTC": "Utc", "n/a": "NotAvailable"}


@dataclass(frozen=True)
class External:
    """A type defined outside this file: its Rust path, whether it is `Eq`, and its `Default` as JSON (None = no
    `Default`)."""

    path: str
    eq: bool = True
    default: Any = None
    #: `deserialize_with` function of the field.
    deserialize_with: str | None = None


#: Enum fields whose type lives in `crate::json` (shared with the documents) or is generated elsewhere.
EXTERNAL_ENUMS: dict[str, External] = {
    "CalibrationSpec.kind": External(
        "crate::json::CalibrationKind", default="alloc", deserialize_with="crate::specs::spec_calibration_kind"
    ),
    "HazardFitSpec.recalibration": External("crate::json::Recalibration", default="isotonic"),
    "FdrSpec.method": External("crate::json::FdrMethod", default="bh"),
    "ScorecardSpec.markout_refs[]": External("crate::json::MarkoutRef", default="mid"),
    "ExportSpec.target": External("crate::json::ExportTarget", default="hftbacktest"),
    "LiveImportSpec.venue": External("crate::live_venue::LiveVenue"),
}
#: `$defs` objects defined in another crate (their schema still gives their fields and defaults).
EXTERNAL_STRUCTS = {"LatencyModel": "qt_types::LatencyModel"}
#: `anyOf` of `$ref`s -> Rust untagged enum; a variant is named after its `$ref` without the `Ref` suffix.
UNIONS = {("PathRef", "DatasetIdRef"): "DatasetRef", ("PathRef", "UploadRef", "JobFileRef"): "FileRef"}
#: `string | array of string` -> this untagged enum.
ONE_OR_MANY = "OneOrMany"
#: Discriminated `oneOf` fields -> Rust internally tagged enum (variant = CamelCase of the tag).
TAGGED = {"ScorecardSpec.sim": "SimSpec"}
#: Integer widths that the default rule does not give. The rule: a field named `*_ns` is `i64`, `seed` is `u64`, a
#: maximum of 65535 is `u16`, another minimum >= 0 is `u32`, anything else `i64`.
INTS = {
    "IngestSpec.price_exp": "i8",
    "IngestSpec.size_exp": "i8",
    "ShadowSampleSpec.n": "u64",
    "ShadowSampleSpec.partition_quotas{}": "u64",
    "MirrorSpec.n": "u64",
    "ExcludeOrderId.order_id": "u64",
    "QtyRule.lots": "u64",
    "Placement.offset_ticks": "i32",
    "FeatureSpec.n_real_orders": "u64",
    "RefereeTolerance.max_discordant": "u64",
    "RefereeTolerance.max_qty_mismatch": "u64",
    "RefereeTolerance.max_abs_ttf_err_ns": "u64",
}
#: Derives beyond the computed ones (Clone, Debug, PartialEq, and Eq / Default when they apply).
EXTRA_DERIVES = {"ExcludeOrderId": ("Default", "Hash", "PartialOrd", "Ord")}


@dataclass
class Config:
    """One generated file: its roots and its Rust binding."""

    label: str
    out: str
    #: (schema name, Rust name of the top-level struct)
    roots: tuple[tuple[str, str], ...]
    header: tuple[str, ...]
    uses: tuple[str, ...]
    enums: dict[str, tuple[str, ...]]
    external_enums: dict[str, External]
    external_structs: dict[str, str]
    unions: dict[tuple[str, ...], str]
    tagged: dict[str, str]
    ints: dict[str, str]
    extra_derives: dict[str, tuple[str, ...]]
    #: take `default_factory` defaults from the samples' `defaults` documents (specs)
    sample_defaults: bool = True
    #: `#[serde(default)]` on the container when every field has a default (specs)
    container_default: bool = True
    #: Rust names of `$defs` whose Rust name differs
    def_names: dict[str, str] = field(default_factory=dict)
    #: `#[default]` variant of enums whose schema gives none
    enum_defaults: dict[str, str] = field(default_factory=dict)
    #: `skip_serializing_if` of fields that are left out when empty (keeps the bytes of older files)
    omit: dict[str, str] = field(default_factory=dict)
    #: derive `Default` on every struct whose field types all have one (documents)
    derive_default_all: bool = False
    #: Rust path of a free-form JSON object (`additionalProperties: true`)
    free_object: str = "IndexMap<String, Value>"
    #: Rust types of external paths that implement `Default`
    ext_default: frozenset[str] = frozenset()
    #: generated `impl QtDoc` per root (documents)
    qt_doc: bool = False
    #: integer rule: "spec" (see `INTS`) or "u64" (documents: counts are `u64`, `*_ns` and `ts*` fields `i64`)
    int_default: str = "spec"
    #: structs whose `Default` is written by hand next to the generated file
    hand_default: frozenset[str] = frozenset()
    #: `[T; n]` for an array with `minItems == maxItems` (documents)
    fixed_arrays: bool = False
    #: derives of enums beyond the usual ones
    enum_derives: dict[str, tuple[str, ...]] = field(default_factory=dict)


SPEC_CONFIG = Config(
    label="spec",
    out=OUT,
    roots=tuple((s, "") for s in SPECS),
    header=(
        "// @generated by `make generate` (scripts/gen_rust_specs.py) from schemas/json/qt.*_spec.schema.json and",
        "// schemas/samples/qt.*_spec.json. Do not edit: change the Pydantic model in",
        "// python/queuetruth/schemas/specs.py, or the Rust binding in the generator, and run `make generate`.",
        "// `make generate-check` and the `specs_gen_is_current` test fail when this file is edited by hand.",
    ),
    uses=(
        "use indexmap::IndexMap;",
        "use serde::{Deserialize, Serialize};",
        "use serde_json::Value;",
        "",
        "use crate::specs::OneOrMany;",
    ),
    enums=ENUMS,
    external_enums=EXTERNAL_ENUMS,
    external_structs=EXTERNAL_STRUCTS,
    unions=UNIONS,
    tagged=TAGGED,
    ints=INTS,
    extra_derives=EXTRA_DERIVES,
)

DOC_OUT = "crates/schema/src/json_gen.rs"
#: The documents mirrored in Rust (schema name, Rust name), in the order of the generated file.
DOCS = (
    ("qt.error", "ErrorDoc"),
    ("qt.dataset_manifest", "DatasetManifest"),
    ("qt.data_quality", "DataQuality"),
    ("qt.ingest_summary", "IngestSummary"),
    ("qt.replay_summary", "ReplaySummary"),
    ("qt.shadow_sample_summary", "ShadowSampleSummary"),
    ("qt.zoo_summary", "ZooSummary"),
    ("qt.calibration_result", "CalibrationResult"),
    ("qt.live_calibration_test", "LiveCalibrationTest"),
    ("qt.feature_summary", "FeatureSummary"),
    ("qt.hazard_model_card", "HazardModelCard"),
    ("qt.live_import_summary", "LiveImportSummary"),
    ("qt.live_manifest", "LiveManifest"),
    ("qt.live_validation", "LiveValidation"),
    ("qt.scorecard_summary", "ScorecardSummary"),
    ("qt.referee_report", "RefereeReport"),
    ("qt.why_trace", "WhyTrace"),
    ("qt.report_summary", "ReportSummary"),
    ("qt.export_summary", "ExportSummary"),
    ("qt.fixtures_summary", "FixturesSummary"),
    ("qt.doctor", "Doctor"),
    ("qt.bench", "Bench"),
    ("qt.problem", "Problem"),
    ("qt.job", "Job"),
    ("qt.file_manifest", "FileManifest"),
    ("qt.estimate", "Estimate"),
)
DOC_ENUMS: dict[str, tuple[str, ...]] = {
    "DatasetKindName": ("DatasetManifest.kind",),
    "Verdict": ("DataQuality.verdict",),
    "CalibrationKind": ("CalibrationResult.kind",),
    "TrustVerdict": ("CalibrationTrust.verdict",),
    "BeatsDefault": ("CalibrationTrust.beats_default{}",),
    "HazardCause": ("HazardModelCard.causes[]",),
    "Recalibration": ("HazardModelCard.recalibration",),
    "LiveValidationStatus": ("LiveImportSummary.validation_status",),
    "ScaleSource": ("InstrumentScale.scale_source",),
    "SimKind": ("SimSource.kind",),
    "Direction": ("Bias.direction",),
    "MarkoutRef": ("MarkoutRow.ref", "MarkoutDiffRow.ref"),
    "Arm": ("MarkoutRow.arm",),
    "FdrMethod": ("Fdr.method",),
    "PnlGapDirection": ("PnlGap.direction",),
    "PnlGapPart": ("PnlGap.main_part",),
    "ExportTarget": ("ExportSummary.target",),
    "JobKind": ("Job.kind",),
    "JobState": ("Job.state",),
}
#: The `#[default]` variant of the document enums (the schema gives none; `Default` fills documents in Rust).
DOC_ENUM_DEFAULTS: dict[str, str] = {
    "DatasetKindName": "mbo",
    "Verdict": "clean",
    "CalibrationKind": "alloc",
    "TrustVerdict": "refused",
    "Recalibration": "isotonic",
    "LiveValidationStatus": "fixture_only",
    "ScaleSource": "default",
    "SimKind": "external",
    "Direction": "no_significant_bias",
    "Arm": "live",
    "MarkoutRef": "mid",
    "FdrMethod": "bh",
    "PnlGapDirection": "no_significant_difference",
    "PnlGapPart": "granted",
    "ExportTarget": "hftbacktest",
    "JobKind": "replay",
    "JobState": "queued",
}
DOC_EXTERNAL_ENUMS: dict[str, External] = {
    "DatasetManifest.venue": External("qt_types::Venue", default="generic"),
    "DatasetManifest.rule_set": External("qt_types::RuleSet", default="generic"),
    "DatasetManifest.validation_status": External("qt_types::ValidationStatus"),
    "DataSource.validation_status": External("qt_types::ValidationStatus"),
    # Kept as text in Rust (the side and action letters of the tape).
    "WhyTrace.side": External("String", default=""),
    "WhyEvent.action": External("String", default=""),
    "WhyEvent.side": External("String", default=""),
}
DOC_EXTERNAL_STRUCTS: dict[str, str] = {
    "FileEntry": "crate::io::FileEntry",
    # The echo of a spec window is read into the spec type (`WindowEcho` requires `n_blocks`; the spec defaults it).
    "WindowEcho": "crate::specs::WindowSpec",
}
#: Integer widths the document rule (`int_default="u64"`) does not give.
DOC_INTS: dict[str, str] = {
    "DatasetManifest.price_exp_by_instrument{}": "i8",
    "DecileRow.decile": "u8",
    "HazardModelCard.format_version": "u32",
    "InstrumentScale.price_exp": "i8",
    "InstrumentScale.size_exp": "i8",
    "LiquidityRow.bucket": "u8",
    "PartitionEntry.session_date": "i32",
    "Problem.status": "u16",
    "SourceVintage.dbn_version": "u8",
    "SourceVintage.start_ns": "u64",
    "SourceVintage.end_ns": "u64",
    "SourceVintage.publisher_id": "u16",
    "ToleranceEcho.max_abs_ttf_err_ns": "u64",
    "WhyEvent.price": "i64",
    "WhyEvent.rank_after": "u32",
    "WhyTrace.price": "i64",
}
DOC_EXTRA_DERIVES: dict[str, tuple[str, ...]] = {}
DOC_ENUM_DERIVES: dict[str, tuple[str, ...]] = {"Verdict": ("PartialOrd", "Ord")}
DOC_DEF_NAMES: dict[str, str] = {"Verdict": "ScoreVerdict"}
#: Fields left out when empty, so the files Rust wrote before they existed keep their bytes.
DOC_OMIT: dict[str, str] = {
    "DatasetManifest.attribution": "Option::is_none",
    "DataQuality.trading_halts": "Vec::is_empty",
    "ShadowSampleSummary.warnings": "Vec::is_empty",
    "ZooSummary.ci_block": "Option::is_none",
    "ZooSummary.n_ci_blocks": "Option::is_none",
}

# ---- type model -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class T:
    """A Rust type: `kind` in prim, opt, vec, map, enum, struct, union, ext, value."""

    kind: str
    name: str = ""
    inner: T | None = None
    items: tuple[T, ...] = ()


@dataclass
class Field:
    name: str
    ty: T
    required: bool
    schema: dict[str, Any]
    default: Any = None
    has_default: bool = False
    deserialize_with: str | None = None
    omit: str | None = None


@dataclass
class Struct:
    name: str
    doc: str
    fields: list[Field] = field(default_factory=list)


@dataclass
class Enum:
    name: str
    values: list[str]
    users: list[str] = field(default_factory=list)
    default: str | None = None


@dataclass
class Tagged:
    name: str
    tag: str
    variants: list[tuple[str, str]]  # (tag value, struct name)


class GenError(Exception):
    pass


def camel(s: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[_.\s/]+", s) if w)


def snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def _ref(node: dict[str, Any]) -> str | None:
    r = node.get("$ref")
    return r.rsplit("/", 1)[-1] if isinstance(r, str) else None


class Gen:
    def __init__(self, root: Path, cfg: Config | None = None) -> None:
        self.root = root
        self.cfg = cfg or SPEC_CONFIG
        self.defs: dict[str, dict[str, Any]] = {}
        self.structs: dict[str, Struct] = {}
        self.enums: dict[str, Enum] = {}
        self.tagged: dict[str, Tagged] = {}
        self.unions: dict[str, tuple[str, ...]] = {}
        self.order: list[tuple[str, str]] = []  # (kind, name) in first-use order
        self.used_bindings: set[str] = set()
        self.ext_enums: dict[str, tuple[External, list[str]]] = {}
        self.candidates: dict[str, list[dict[str, Any]]] = {}
        self.schemas: dict[str, dict[str, Any]] = {}
        self.samples: dict[str, dict[str, Any]] = {}

    # -- reading ----------------------------------------------------------------------------------------------------

    def load(self) -> None:
        cfg = self.cfg
        self.enum_of = {u: name for name, users in cfg.enums.items() for u in users}
        names = []
        for spec, rust in cfg.roots:
            sch = json.loads((self.root / f"schemas/json/{spec}.schema.json").read_text(encoding="utf-8"))
            self.schemas[spec] = sch
            if cfg.sample_defaults:
                sam = json.loads((self.root / f"schemas/samples/{spec}.json").read_text(encoding="utf-8"))
                self.samples[spec] = sam
            for k, v in sch.get("$defs", {}).items():
                if k in self.defs and self.defs[k] != v:
                    raise GenError(f"$defs.{k} differs between {cfg.label} schemas ({spec})")
                self.defs[k] = v
            names.append((spec, rust or camel(spec.removeprefix("qt."))))
        self.root_names = names
        for spec, name in names:
            self.title = spec
            self._struct(name, self.schemas[spec])
        if cfg.sample_defaults:
            for spec, name in names:
                self._collect_candidates(name, self.samples[spec]["defaults"])
        bound = set(self.enum_of) | set(cfg.external_enums) | set(cfg.ints) | set(cfg.tagged) | set(cfg.omit)
        stale = bound - self.used_bindings
        if stale:
            raise GenError(f"bindings that match no schema field (remove them): {sorted(stale)}")
        if cfg.sample_defaults:
            for spec, name in names:
                self._resolve_defaults(name, self.samples[spec]["defaults"])
        self._enum_defaults()
        for s in self.structs.values():
            for f in s.fields:
                if not f.required and not f.has_default:
                    self._resolve_field_default(s, f, None)

    def _struct(self, name: str, node: dict[str, Any]) -> None:
        if name in self.structs:
            return
        if node.get("type") != "object" or node.get("additionalProperties") is not False:
            raise GenError(f"{name}: expected an object schema with additionalProperties false")
        s = Struct(name, node.get("description", ""), [])
        self.structs[name] = s
        self.order.append(("struct", name))
        req = set(node.get("required", []))
        for fname, p in node.get("properties", {}).items():
            key = f"{name}.{fname}"
            f = Field(fname, self._type(key, p), fname in req, p)
            if "default" in p:
                f.default, f.has_default = p["default"], True
            ext = self.cfg.external_enums.get(key)
            if ext and ext.deserialize_with:
                f.deserialize_with = ext.deserialize_with
            if key in self.cfg.omit:
                self.used_bindings.add(key)
                f.omit = self.cfg.omit[key]
            s.fields.append(f)

    def rname(self, defname: str) -> str:
        """Rust name of a `$defs` entry."""
        return self.cfg.def_names.get(defname, defname)

    def _type(self, key: str, p: dict[str, Any]) -> T:
        if self.cfg.qt_doc and key.count(".") == 1:
            owner, fname = key.split(".")
            if fname == "schema" and p.get("const") == self.title:
                return T("tag", owner)
            if fname == "schema_version" and p.get("const") == 1:
                return T("v1")
        if key in self.cfg.tagged:
            self.used_bindings.add(key)
            return self._tagged(key, p)
        if "anyOf" in p:
            alts = p["anyOf"]
            nonnull = [a for a in alts if a.get("type") != "null"]
            if len(nonnull) < len(alts):
                inner = dict(p)
                del inner["anyOf"]
                inner.pop("default", None)
                if len(nonnull) == 1:
                    inner.update(nonnull[0])
                else:
                    inner["anyOf"] = nonnull
                return T("opt", inner=self._type(key, inner))
            refs = tuple(_ref(a) for a in alts)
            if all(refs):
                name = self.cfg.unions.get(refs)  # type: ignore[arg-type]
                if name is None:
                    raise GenError(f"{key}: no Rust union for {refs} (add it to UNIONS)")
                for r in refs:
                    self._struct(self.rname(r), self.defs[r])  # type: ignore[arg-type,index]
                if ("union", name) not in self.order:
                    self.order.append(("union", name))
                    self.unions[name] = refs  # type: ignore[assignment]
                return T("union", name)
            if all(a.get("type") in ("string", "integer", "number", "boolean") and len(a) == 1 for a in alts) and (
                len({a["type"] for a in alts}) > 1
            ):
                return T("value")
            if [a.get("type") for a in alts] == ["string", "array"] and alts[1].get("items", {}).get("type") == (
                "string"
            ):
                return T("ext", ONE_OR_MANY)
            raise GenError(f"{key}: unsupported anyOf {alts}")
        r = _ref(p)
        if r:
            if r in self.cfg.external_structs:
                return T("ext", self.cfg.external_structs[r])
            self._struct(self.rname(r), self.defs[r])
            return T("struct", self.rname(r))
        if ("enum" in p or "const" in p) and p.get("type") == "integer":
            p = {k: v for k, v in p.items() if k not in ("enum", "const")}
        if "enum" in p or "const" in p:
            values = p["enum"] if "enum" in p else [p["const"]]
            if key in self.cfg.external_enums:
                self.used_bindings.add(key)
                ext = self.cfg.external_enums[key]
                self.ext_enums[key] = (ext, values)
                return T("ext", ext.path)
            name = self.enum_of.get(key)
            if name is None:
                raise GenError(f"{key}: an enum without a Rust name (add it to ENUMS)")
            self.used_bindings.add(key)
            e = self.enums.get(name)
            if e is None:
                e = self.enums[name] = Enum(name, list(values))
                self.order.append(("enum", name))
            elif e.values != list(values):
                raise GenError(f"{key}: values {values} differ from {name} {e.values}")
            e.users.append(key)
            return T("enum", name)
        t = p.get("type")
        if t == "string":
            return T("prim", "String")
        if t == "boolean":
            return T("prim", "bool")
        if t == "number":
            return T("prim", "f64")
        if t == "integer":
            if key in self.cfg.ints:
                self.used_bindings.add(key)
                return T("prim", self.cfg.ints[key])
            fname = key.rsplit(".", 1)[-1].rstrip("[]{}")
            if self.cfg.int_default == "u64":
                is_time = fname.endswith("_ns") or fname.startswith("ts_") or fname in ("ts", "ts_live")
                if fname == "instrument_id":
                    return T("prim", "u32")
                return T("prim", "i64" if is_time else "u64")
            if fname.endswith("_ns"):
                return T("prim", "i64")
            if fname == "seed":
                return T("prim", "u64")
            if p.get("maximum") == 65535:
                return T("prim", "u16")
            if p.get("minimum", -1) >= 0:
                return T("prim", "u32")
            return T("prim", "i64")
        if t == "array" and "prefixItems" in p:
            items = tuple(self._type(f"{key}[{i}]", x) for i, x in enumerate(p["prefixItems"]))
            return T("tuple", items=items)
        if t == "array" and self.cfg.fixed_arrays and p.get("minItems") == p.get("maxItems") is not None:
            return T("array", str(p["minItems"]), self._type(key + "[]", p["items"]))
        if t == "array":
            return T("vec", inner=self._type(key + "[]", p["items"]))
        if t == "object":
            ap = p.get("additionalProperties")
            if ap is True:
                return T("object", self.cfg.free_object)
            if isinstance(ap, dict):
                return T("map", "IndexMap", self._type(key + "{}", ap))
            pp = p.get("patternProperties")
            if isinstance(pp, dict) and len(pp) == 1 and "additionalProperties" not in p:
                return T("map", "IndexMap", self._type(key + "{}", next(iter(pp.values()))))
        if not ({"type", "$ref", "anyOf", "oneOf", "allOf", "enum", "const"} & set(p)):
            return T("value")
        raise GenError(f"{key}: unsupported schema {p}")

    def _tagged(self, key: str, p: dict[str, Any]) -> T:
        name = self.cfg.tagged[key]
        disc = p["discriminator"]
        by_ref = {v.rsplit("/", 1)[-1]: k for k, v in disc["mapping"].items()}
        variants = []
        for alt in p["oneOf"]:
            r = _ref(alt)
            node = self.defs[r]  # type: ignore[index]
            props = dict(node["properties"])
            tag = props.pop(disc["propertyName"])
            if tag.get("const") != by_ref[r]:  # type: ignore[index]
                raise GenError(f"{key}: {r} tag {tag} differs from the discriminator mapping")
            inner = dict(node, properties=props, required=[x for x in node["required"] if x != disc["propertyName"]])
            self._struct(r, inner)  # type: ignore[arg-type]
            self.order.remove(("struct", r))  # type: ignore[arg-type]
            variants.append((by_ref[r], r))  # type: ignore[index]
        self.tagged[name] = Tagged(name, disc["propertyName"], variants)  # type: ignore[arg-type]
        self.order.append(("tagged", name))
        return T("tagged", name)

    # -- defaults ---------------------------------------------------------------------------------------------------

    def _collect_candidates(self, sname: str, value: Any) -> None:
        """Records every struct value of a defaults document (the default of a `default_factory` struct)."""
        s = self.structs[sname]
        if not isinstance(value, dict):
            return
        self.candidates.setdefault(sname, []).append(value)
        for f in s.fields:
            t = f.ty.inner if f.ty.kind == "opt" else f.ty
            if t and t.kind == "struct" and f.name in value:
                self._collect_candidates(t.name, value[f.name])

    def _resolve_defaults(self, sname: str, defaults: dict[str, Any]) -> None:
        s = self.structs[sname]
        for f in s.fields:
            if not f.required and not f.has_default:
                self._resolve_field_default(s, f, defaults)

    def _resolve_field_default(self, s: Struct, f: Field, defaults: dict[str, Any] | None) -> None:
        if defaults is not None and f.name in defaults:
            f.default, f.has_default = defaults[f.name], True
            return
        if f.ty.kind in ("struct", "ext") and self.default_value(f.ty) is not None:
            f.default, f.has_default = self.default_value(f.ty), True
            return
        seen = [c[f.name] for c in self.candidates.get(s.name, []) if f.name in c]
        if seen:
            if any(json.dumps(x) != json.dumps(seen[0]) for x in seen):
                raise GenError(f"{s.name}.{f.name}: the defaults documents disagree: {seen}")
            f.default, f.has_default = seen[0], True
            return
        if not self.cfg.sample_defaults and self.has_default_impl(f.ty):
            f.default, f.has_default = self.default_value(f.ty), True
            return
        raise GenError(f"{s.name}.{f.name}: no default in the schema or the samples")

    def _enum_defaults(self) -> None:
        for name, v in self.cfg.enum_defaults.items():
            if name not in self.enums or v not in self.enums[name].values:
                raise GenError(f"enum_defaults: {name} has no value {v!r}")
        for e in self.enums.values():
            if e.name in self.cfg.enum_defaults:
                e.default = self.cfg.enum_defaults[e.name]
                continue
            ds = set()
            for s in self.structs.values():
                for f in s.fields:
                    if f.ty == T("enum", e.name) and f.has_default:
                        ds.add(f.default)
            if len(ds) == 1:
                e.default = ds.pop()

    def defaultable(self, s: Struct) -> bool:
        return all(not f.required for f in s.fields)

    def default_value(self, t: T) -> Any:
        """The JSON value of `T::default()`, or None when T has no `Default`."""
        if t.kind == "opt":
            return None
        if t.kind == "object":
            return {}
        if t.kind == "tuple":
            return [self.default_value(x) for x in t.items]
        if t.kind == "array":
            return [self.default_value(t.inner)] * int(t.name)  # type: ignore[arg-type]
        if t.kind == "prim":
            return {"String": "", "bool": False, "f64": 0.0}.get(t.name, 0)
        if t.kind == "vec":
            return []
        if t.kind == "map":
            return {}
        if t.kind == "enum":
            return self.enums[t.name].default
        if t.kind == "ext":
            for ext, _ in self.ext_enums.values():
                if ext.path == t.name:
                    return ext.default
            for d, path in self.cfg.external_structs.items():
                if path == t.name and d in self.defs:
                    props = self.defs[d]["properties"].items()
                    if all("default" in p for _, p in props):
                        return {k: p["default"] for k, p in props}
            return None
        if t.kind == "struct":
            s = self.structs[t.name]
            if not self.defaultable(s):
                return None
            out = {}
            for f in s.fields:
                if not f.has_default:
                    return None
                out[f.name] = f.default
            return out
        return None

    def is_type_default(self, t: T, value: Any) -> bool:
        if t.kind == "opt":
            return value is None
        if t.kind == "struct" and "Default" in self.cfg.extra_derives.get(t.name, ()):
            return False
        d = self.default_value(t)
        return d is not None and json.dumps(d) == json.dumps(value)

    def has_default_impl(self, t: T, seen: frozenset[str] = frozenset()) -> bool:
        """True when the Rust type implements `Default`."""
        if t.kind in ("prim", "opt", "vec", "map", "object", "value", "tag", "v1"):
            return True
        if t.kind == "array":
            return self.has_default_impl(t.inner, seen)  # type: ignore[arg-type]
        if t.kind == "tuple":
            return all(self.has_default_impl(x, seen) for x in t.items)
        if t.kind == "enum":
            return self.enums[t.name].default is not None
        if t.kind == "ext":
            return t.name in self.cfg.ext_default or self.default_value(t) is not None
        if t.kind == "struct":
            if t.name in seen or t.name in self.cfg.hand_default:
                return True
            return self._struct_derives_default(self.structs[t.name], seen | {t.name})
        return False

    def _struct_derives_default(self, s: Struct, seen: frozenset[str] = frozenset()) -> bool:
        if "Default" in self.cfg.extra_derives.get(s.name, ()):
            return True
        if self.cfg.derive_default_all:
            return all(self.has_default_impl(f.ty, seen | {s.name}) for f in s.fields) and all(
                f.required or self.is_type_default(f.ty, f.default) for f in s.fields
            )
        return self.defaultable(s) and self._derive_default(s)

    def has_eq(self, t: T, seen: frozenset[str] = frozenset()) -> bool:
        if t.kind in ("tag", "v1", "object"):
            return True
        if t.kind == "tuple":
            return all(self.has_eq(x, seen) for x in t.items)
        if t.kind == "prim":
            return t.name != "f64"
        if t.kind in ("opt", "vec", "map", "array"):
            return self.has_eq(t.inner, seen)  # type: ignore[arg-type]
        if t.kind == "struct":
            if t.name in seen:
                return True
            return all(self.has_eq(f.ty, seen | {t.name}) for f in self.structs[t.name].fields)
        if t.kind == "union":
            return all(self.has_eq(T("struct", r), seen) for r in self.unions[t.name])
        if t.kind == "tagged":
            return all(self.has_eq(T("struct", r), seen) for _, r in self.tagged[t.name].variants)
        return True  # enums, externals (all Eq), Value

    # -- rendering --------------------------------------------------------------------------------------------------

    def rust(self, t: T) -> str:
        if t.kind in ("prim", "enum", "struct", "union", "tagged"):
            return t.name
        if t.kind == "ext":
            return t.name
        if t.kind == "opt":
            return f"Option<{self.rust(t.inner)}>"  # type: ignore[arg-type]
        if t.kind == "vec":
            return f"Vec<{self.rust(t.inner)}>"  # type: ignore[arg-type]
        if t.kind == "map":
            return f"{t.name}<String, {self.rust(t.inner)}>"  # type: ignore[arg-type]
        if t.kind == "value":
            return "Value"
        if t.kind == "object":
            return t.name
        if t.kind == "tag":
            return f"Tag<{t.name}>"
        if t.kind == "v1":
            return "V1"
        if t.kind == "tuple":
            return "(" + ", ".join(self.rust(x) for x in t.items) + ")"
        if t.kind == "array":
            return f"[{self.rust(t.inner)}; {t.name}]"  # type: ignore[arg-type]
        raise GenError(f"type {t}")

    def lit(self, t: T, v: Any, where: str) -> str:
        if t.kind == "opt":
            return "None" if v is None else f"Some({self.lit(t.inner, v, where)})"  # type: ignore[arg-type]
        if t.kind == "prim":
            if t.name == "bool":
                return "true" if v else "false"
            if t.name == "String":
                return f"{json.dumps(v)}.into()"
            if t.name == "f64":
                return repr(float(v))
            return _int(int(v))
        if t.kind == "vec":
            if not v:
                return "Vec::new()"
            return "vec![" + ", ".join(self.lit(t.inner, x, where) for x in v) + "]"  # type: ignore[arg-type]
        if t.kind in ("map", "object"):
            if v:
                raise GenError(f"{where}: a non-empty map default is not supported")
            return f"{t.name.split('<')[0]}::new()"
        if t.kind == "enum":
            return f"{t.name}::{variant(v)}"
        if t.kind == "ext":
            for ext, _ in self.ext_enums.values():
                if ext.path == t.name:
                    return f"{t.name}::{variant(v)}"
            if self.default_value(t) == v:
                return f"{t.name}::default()"
            raise GenError(f"{where}: a non-default {t.name} default is not supported")
        if t.kind == "struct":
            s = self.structs[t.name]
            parts = [f"{f.name}: {self.lit(f.ty, v[f.name], where)}" for f in s.fields]
            return f"{t.name} {{ " + ", ".join(parts) + " }"
        raise GenError(f"{where}: no literal for {t}")

    def render(self) -> str:
        out: list[str] = [*self.cfg.header, "", "#![allow(clippy::unreadable_literal)]", "", *self.cfg.uses]
        fns: list[str] = []
        for kind, name in self.order:
            out.append("")
            if kind == "struct":
                out += self._render_struct(self.structs[name], fns)
            elif kind == "enum":
                out += self._render_enum(self.enums[name])
            elif kind == "union":
                out += self._render_union(name)
            elif kind == "tagged":
                out += self._render_tagged(self.tagged[name], fns)
        if self.cfg.qt_doc:
            out += ["", "// ---- schema names ----------------------------------------------------------------------"]
            for spec, name in self.root_names:
                out += ["", f"impl QtDoc for {name} {{", f'    const SCHEMA: &\'static str = "{spec}";', "}"]
        if fns:
            out += ["", "// ---- defaults that are not the type's own -----------------------------------------------"]
            out += fns
        if self.ext_enums:
            out += ["", *self._render_tests()]
        return "\n".join(out) + "\n"

    def _derives(self, s: Struct) -> list[str]:
        d = ["Clone", "Debug"]
        extra = self.cfg.extra_derives.get(s.name, ())
        if self._struct_derives_default(s):
            d.append("Default")
        d.append("PartialEq")
        if all(self.has_eq(f.ty) for f in s.fields):
            d.append("Eq")
        d += [x for x in extra if x != "Default"]
        return [*d, "Serialize", "Deserialize"]

    def _derive_default(self, s: Struct) -> bool:
        return all(self.is_type_default(f.ty, f.default) for f in s.fields)

    def _doc(self, text: str, indent: str = "") -> list[str]:
        text = " ".join(text.split())
        if not text:
            return []
        lines, cur = [], ""
        for w in text.split(" "):
            if cur and len(indent) + 4 + len(cur) + 1 + len(w) > 120:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}" if cur else w
        lines.append(cur)
        return [f"{indent}/// {x}" for x in lines]

    def _field_doc(self, f: Field) -> str:
        if f.required:
            return f"`{f.name}` (required)."
        return f"`{f.name}` (default `{json.dumps(f.default, separators=(', ', ': '))}`)."

    def _render_fields(self, s: Struct, fns: list[str], indent: str, container_default: bool, vis: str) -> list[str]:
        out = []
        for f in s.fields:
            out += self._doc(self._field_doc(f), indent)
            attrs = []
            if f.deserialize_with:
                attrs.append(f'deserialize_with = "{f.deserialize_with}"')
            if not f.required and not container_default:
                if self.is_type_default(f.ty, f.default):
                    attrs.append("default")
                else:
                    fn = f"default_{snake(s.name)}_{f.name}"
                    fns += [
                        "",
                        f"fn {fn}() -> {self.rust(f.ty)} {{",
                        f"    {self.lit(f.ty, f.default, f'{s.name}.{f.name}')}",
                        "}",
                    ]
                    attrs.append(f'default = "{fn}"')
            if f.omit:
                attrs.append(f'skip_serializing_if = "{f.omit}"')
            if attrs:
                out.append(f"{indent}#[serde({', '.join(attrs)})]")
            out.append(f"{indent}{vis}{ident(f.name)}: {self.rust(f.ty)},")
        return out

    def _render_struct(self, s: Struct, fns: list[str]) -> list[str]:
        container_default = self.cfg.container_default and self.defaultable(s)
        out = self._doc(s.doc or f"`{s.name}`.")
        out.append(f"#[derive({', '.join(self._derives(s))})]")
        out.append(f"#[serde(deny_unknown_fields{', default' if container_default else ''})]")
        out.append(f"pub struct {s.name} {{")
        out += self._render_fields(s, fns, "    ", container_default, "pub ")
        out.append("}")
        if (
            container_default
            and not self._derive_default(s)
            and "Default" not in self.cfg.extra_derives.get(s.name, ())
        ):
            out += ["", f"impl Default for {s.name} {{", "    fn default() -> Self {"]
            out.append("        Self {")
            for f in s.fields:
                out.append(f"            {ident(f.name)}: {self.lit(f.ty, f.default, f'{s.name}.{f.name}')},")
            out += ["        }", "    }", "}"]
        return out

    def _render_enum(self, e: Enum) -> list[str]:
        users = ", ".join(f"`{u}`" for u in e.users)
        out = self._doc(f"Values of {users}.")
        derives = ["Clone", "Copy", "Debug"] + (["Default"] if e.default is not None else [])
        derives += ["PartialEq", "Eq", *self.cfg.enum_derives.get(e.name, ()), "Hash", "Serialize", "Deserialize"]
        out.append(f"#[derive({', '.join(derives)})]")
        out.append(f"pub enum {e.name} {{")
        for v in e.values:
            out.append(f"    /// `{v}`.")
            if v == e.default:
                out.append("    #[default]")
            out.append(f'    #[serde(rename = "{v}")]')
            out.append(f"    {variant(v)},")
        out.append("}")
        return out

    def _render_union(self, name: str) -> list[str]:
        refs = self.unions[name]
        out = self._doc(f"`{name}`: one of {', '.join(f'`{r}`' for r in refs)} (an untagged union).")
        derive = ["Clone", "Debug", "PartialEq"] + (["Eq"] if self.has_eq(T("union", name)) else [])
        out.append(f"#[derive({', '.join([*derive, 'Serialize', 'Deserialize'])})]")
        out.append("#[serde(untagged)]")
        out.append(f"pub enum {name} {{")
        for r in refs:
            out.append(f"    /// `{r}`.")
            out.append(f"    {r.removesuffix('Ref')}({r}),")
        out.append("}")
        return out

    def _render_tagged(self, tg: Tagged, fns: list[str]) -> list[str]:
        out = self._doc(f"`{tg.name}`: tagged by `{tg.tag}`.")
        derive = ["Clone", "Debug", "PartialEq"] + (["Eq"] if self.has_eq(T("tagged", tg.name)) else [])
        out.append(f"#[derive({', '.join([*derive, 'Serialize', 'Deserialize'])})]")
        out.append(f'#[serde(tag = "{tg.tag}", deny_unknown_fields)]')
        out.append(f"pub enum {tg.name} {{")
        for tag, r in tg.variants:
            s = self.structs[r]
            out += self._doc(s.doc or f"`{tag}`.", "    ")
            out.append(f'    #[serde(rename = "{tag}")]')
            if not s.fields:
                out.append(f"    {variant(tag)} {{}},")
                continue
            out.append(f"    {variant(tag)} {{")
            out += self._render_fields(s, fns, "        ", False, "")
            out.append("    },")
        out.append("}")
        return out

    def _render_tests(self) -> list[str]:
        out = [
            "#[cfg(test)]",
            "mod tests {",
            "    /// The enums defined outside this file accept every value of their schema.",
            "    #[test]",
            "    fn external_enums_accept_the_schema_values() {",
        ]
        for key, (ext, values) in self.ext_enums.items():
            vals = ", ".join(json.dumps(v) for v in values)
            out += [
                f"        // {key}",
                f"        for v in [{vals}] {{",
                f"            let r = serde_json::from_value::<{ext.path}>(serde_json::json!(v));",
                f'            assert!(r.is_ok(), "{key}: {{v}}");',
                "        }",
            ]
        out += ["    }", "}"]
        return out


#: Rust keywords that a field name is written with `r#` for.
KEYWORDS = frozenset({"ref", "type", "match", "move", "fn", "impl", "loop", "mod", "self", "use", "where", "struct"})


def ident(name: str) -> str:
    return f"r#{name}" if name in KEYWORDS else name


def variant(v: str) -> str:
    return VARIANTS.get(v) or camel(v)


def _int(n: int) -> str:
    s = str(abs(n))
    if len(s) > 4:
        groups = []
        while s:
            groups.insert(0, s[-3:])
            s = s[:-3]
        s = "_".join(groups)
    return f"-{s}" if n < 0 else s


#: First line of the generated file; the Rust test `specs_gen_is_current` recomputes the hash of the lines after it.
HASH_LINE = "// sha256 of the lines below: {}"


def rustfmt(text: str) -> str:
    """`text` formatted as `cargo fmt` would (edition and width from rustfmt.toml)."""
    exe = shutil.which("rustfmt")
    if exe is None:
        raise GenError("rustfmt not found (install the toolchain of rust-toolchain.toml)")
    r = subprocess.run(
        [exe, "--edition", "2024", "--config-path", str(ROOT / "rustfmt.toml"), "--emit", "stdout", "--quiet"],
        input=text,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    if r.returncode != 0:
        raise GenError(f"rustfmt failed: {r.stderr}")
    return r.stdout


DOC_CONFIG = Config(
    label="document",
    out=DOC_OUT,
    roots=DOCS,
    header=(
        "// @generated by `make generate` (scripts/gen_rust_specs.py) from schemas/json/qt.*.schema.json. Do not",
        "// edit: change the Pydantic model in python/queuetruth/schemas/models.py, or the Rust binding in the",
        "// generator, and run `make generate`. `make generate-check` and the `json_gen_is_current` test fail",
        "// when this file is edited by hand.",
    ),
    uses=(
        "use indexmap::IndexMap;",
        "use serde::{Deserialize, Serialize};",
        "use serde_json::{Map, Value};",
        "",
        "use crate::json::{QtDoc, Tag, V1};",
    ),
    enums=DOC_ENUMS,
    external_enums=DOC_EXTERNAL_ENUMS,
    external_structs=DOC_EXTERNAL_STRUCTS,
    unions={},
    tagged={},
    ints=DOC_INTS,
    extra_derives=DOC_EXTRA_DERIVES,
    sample_defaults=False,
    container_default=False,
    def_names=DOC_DEF_NAMES,
    enum_defaults=DOC_ENUM_DEFAULTS,
    omit=DOC_OMIT,
    derive_default_all=True,
    free_object="Map<String, Value>",
    qt_doc=True,
    enum_derives=DOC_ENUM_DERIVES,
    int_default="u64",
    fixed_arrays=True,
    # `impl Default for DatasetManifest` in json.rs (its validation status has no default).
    hand_default=frozenset({"DatasetManifest"}),
    ext_default=frozenset({"crate::io::FileEntry", "crate::specs::WindowSpec"}),
)
CONFIGS = (SPEC_CONFIG, DOC_CONFIG)


def generate(root: Path = ROOT, cfg: Config = SPEC_CONFIG) -> str:
    g = Gen(root, cfg)
    g.load()
    body = rustfmt(g.render())
    return HASH_LINE.format(hashlib.sha256(body.encode("utf-8")).hexdigest()) + "\n" + body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed file differs; write nothing")
    a = ap.parse_args(argv)
    stale = 0
    for cfg in CONFIGS:
        try:
            text = generate(ROOT, cfg)
        except GenError as e:
            print(f"gen_rust_specs ({cfg.label}): {e}", file=sys.stderr)
            return 2
        path = ROOT / cfg.out
        if a.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print(f"differs: {cfg.out} (run `make generate`)", file=sys.stderr)
                stale += 1
            continue
        path.write_text(text, encoding="utf-8")
        print(f"wrote {cfg.out}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
