"""Generates `crates/schema/src/specs_gen.rs`, the Rust serde structs of the spec objects, from the published JSON
Schemas (`schemas/json/qt.*_spec.schema.json`) and the spec samples (`schemas/samples/qt.*_spec.json`).

The JSON Schema is the contract. This file only adds what a JSON Schema cannot say about Rust: type names of inline
enums and unions, integer widths, map types and the few types defined elsewhere (`BINDING` below). Every binding is
checked against the schemas, so a stale entry or a new field without a Rust name fails the generator with the field
it is about.

Defaults come from the schema (`default`) or, for a Pydantic `default_factory` (which a JSON Schema does not
record), from the `defaults` document of the spec's sample, which is the spec validated from its required fields
only. Methods, validation and conversions stay in the hand-written `crates/schema/src/specs.rs`.

    uv run --frozen --no-sync python scripts/gen_rust_specs.py           # write the file
    uv run --frozen --no-sync python scripts/gen_rust_specs.py --check   # exit 1 when the committed file differs

Standard library only. Step `rust-specs` of `make generate`, after `schemas`.
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
VARIANTS = {"30min": "Min30", "1min": "Min1", "America/New_York": "AmericaNewYork", "UTC": "Utc"}


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

# ---- type model -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class T:
    """A Rust type: `kind` in prim, opt, vec, map, enum, struct, union, ext, value."""

    kind: str
    name: str = ""
    inner: T | None = None


@dataclass
class Field:
    name: str
    ty: T
    required: bool
    schema: dict[str, Any]
    default: Any = None
    has_default: bool = False
    deserialize_with: str | None = None


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
    def __init__(self, root: Path) -> None:
        self.root = root
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
        enum_of = {u: name for name, users in ENUMS.items() for u in users}
        self.enum_of = enum_of
        for spec in SPECS:
            sch = json.loads((self.root / f"schemas/json/{spec}.schema.json").read_text(encoding="utf-8"))
            sam = json.loads((self.root / f"schemas/samples/{spec}.json").read_text(encoding="utf-8"))
            self.schemas[spec] = sch
            self.samples[spec] = sam
            for k, v in sch.get("$defs", {}).items():
                if k in self.defs and self.defs[k] != v:
                    raise GenError(f"$defs.{k} differs between spec schemas ({spec})")
                self.defs[k] = v
        for spec in SPECS:
            name = camel(spec.removeprefix("qt."))
            self._struct(name, self.schemas[spec])
        for spec in SPECS:
            name = camel(spec.removeprefix("qt."))
            self._collect_candidates(name, self.samples[spec]["defaults"])
        stale = (set(enum_of) | set(EXTERNAL_ENUMS) | set(INTS) | set(TAGGED)) - self.used_bindings
        if stale:
            raise GenError(f"bindings that match no schema field (remove them): {sorted(stale)}")
        for spec in SPECS:
            self._resolve_defaults(camel(spec.removeprefix("qt.")), self.samples[spec]["defaults"])
        for s in self.structs.values():
            for f in s.fields:
                if not f.required and not f.has_default:
                    self._resolve_field_default(s, f, None)
        self._enum_defaults()

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
            ext = EXTERNAL_ENUMS.get(key)
            if ext and ext.deserialize_with:
                f.deserialize_with = ext.deserialize_with
            s.fields.append(f)

    def _type(self, key: str, p: dict[str, Any]) -> T:
        if key in TAGGED:
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
                name = UNIONS.get(refs)  # type: ignore[arg-type]
                if name is None:
                    raise GenError(f"{key}: no Rust union for {refs} (add it to UNIONS)")
                for r in refs:
                    self._struct(r, self.defs[r])  # type: ignore[index]
                if ("union", name) not in self.order:
                    self.order.append(("union", name))
                    self.unions[name] = refs  # type: ignore[assignment]
                return T("union", name)
            if [a.get("type") for a in alts] == ["string", "array"] and alts[1].get("items", {}).get("type") == (
                "string"
            ):
                return T("ext", ONE_OR_MANY)
            raise GenError(f"{key}: unsupported anyOf {alts}")
        r = _ref(p)
        if r:
            if r in EXTERNAL_STRUCTS:
                return T("ext", EXTERNAL_STRUCTS[r])
            self._struct(r, self.defs[r])
            return T("struct", r)
        if "enum" in p or "const" in p:
            values = p["enum"] if "enum" in p else [p["const"]]
            if key in EXTERNAL_ENUMS:
                self.used_bindings.add(key)
                ext = EXTERNAL_ENUMS[key]
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
            if key in INTS:
                self.used_bindings.add(key)
                return T("prim", INTS[key])
            fname = key.rsplit(".", 1)[-1].rstrip("[]{}")
            if fname.endswith("_ns"):
                return T("prim", "i64")
            if fname == "seed":
                return T("prim", "u64")
            if p.get("maximum") == 65535:
                return T("prim", "u16")
            if p.get("minimum", -1) >= 0:
                return T("prim", "u32")
            return T("prim", "i64")
        if t == "array":
            return T("vec", inner=self._type(key + "[]", p["items"]))
        if t == "object":
            ap = p.get("additionalProperties")
            if ap is True:
                return T("map", "IndexMap", T("value"))
            if isinstance(ap, dict):
                return T("map", "IndexMap", self._type(key + "{}", ap))
        raise GenError(f"{key}: unsupported schema {p}")

    def _tagged(self, key: str, p: dict[str, Any]) -> T:
        name = TAGGED[key]
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
        raise GenError(f"{s.name}.{f.name}: no default in the schema or the samples")

    def _enum_defaults(self) -> None:
        for e in self.enums.values():
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
            for d, path in EXTERNAL_STRUCTS.items():
                if path == t.name:
                    return {k: p["default"] for k, p in self.defs[d]["properties"].items()}
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
        if t.kind == "struct" and "Default" in EXTRA_DERIVES.get(t.name, ()):
            return False
        d = self.default_value(t)
        return d is not None and json.dumps(d) == json.dumps(value)

    def has_eq(self, t: T, seen: frozenset[str] = frozenset()) -> bool:
        if t.kind == "prim":
            return t.name != "f64"
        if t.kind in ("opt", "vec", "map"):
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
        if t.kind == "map":
            if v:
                raise GenError(f"{where}: a non-empty map default is not supported")
            return f"{t.name}::new()"
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
        out: list[str] = [
            "// @generated by `make generate` (scripts/gen_rust_specs.py) from schemas/json/qt.*_spec.schema.json and",
            "// schemas/samples/qt.*_spec.json. Do not edit: change the Pydantic model in",
            "// python/queuetruth/schemas/specs.py, or the Rust binding in the generator, and run `make generate`.",
            "// `make generate-check` and the `specs_gen_is_current` test fail when this file is edited by hand.",
            "",
            "#![allow(clippy::unreadable_literal)]",
            "",
            "use indexmap::IndexMap;",
            "use serde::{Deserialize, Serialize};",
            "use serde_json::Value;",
            "",
            "use crate::specs::OneOrMany;",
        ]
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
        out += ["", "// ---- defaults that are not the type's own -----------------------------------------------"]
        out += fns
        out += ["", *self._render_tests()]
        return "\n".join(out) + "\n"

    def _derives(self, s: Struct) -> list[str]:
        d = ["Clone", "Debug"]
        extra = EXTRA_DERIVES.get(s.name, ())
        if "Default" in extra or (self.defaultable(s) and self._derive_default(s)):
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
            if attrs:
                out.append(f"{indent}#[serde({', '.join(attrs)})]")
            out.append(f"{indent}{vis}{f.name}: {self.rust(f.ty)},")
        return out

    def _render_struct(self, s: Struct, fns: list[str]) -> list[str]:
        container_default = self.defaultable(s)
        out = self._doc(s.doc or f"`{s.name}`.")
        out.append(f"#[derive({', '.join(self._derives(s))})]")
        out.append(f"#[serde(deny_unknown_fields{', default' if container_default else ''})]")
        out.append(f"pub struct {s.name} {{")
        out += self._render_fields(s, fns, "    ", container_default, "pub ")
        out.append("}")
        if container_default and not self._derive_default(s) and "Default" not in EXTRA_DERIVES.get(s.name, ()):
            out += ["", f"impl Default for {s.name} {{", "    fn default() -> Self {"]
            out.append("        Self {")
            for f in s.fields:
                out.append(f"            {f.name}: {self.lit(f.ty, f.default, f'{s.name}.{f.name}')},")
            out += ["        }", "    }", "}"]
        return out

    def _render_enum(self, e: Enum) -> list[str]:
        users = ", ".join(f"`{u}`" for u in e.users)
        out = self._doc(f"Values of {users}.")
        derives = ["Clone", "Copy", "Debug"] + (["Default"] if e.default is not None else [])
        out.append(f"#[derive({', '.join([*derives, 'PartialEq', 'Eq', 'Hash', 'Serialize', 'Deserialize'])})]")
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


def generate(root: Path = ROOT) -> str:
    g = Gen(root)
    g.load()
    body = rustfmt(g.render())
    return HASH_LINE.format(hashlib.sha256(body.encode("utf-8")).hexdigest()) + "\n" + body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed file differs; write nothing")
    a = ap.parse_args(argv)
    try:
        text = generate()
    except GenError as e:
        print(f"gen_rust_specs: {e}", file=sys.stderr)
        return 2
    path = ROOT / OUT
    if a.check:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            print(f"differs: {OUT} (run `make generate`)", file=sys.stderr)
            return 1
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
