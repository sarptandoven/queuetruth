"""Writers for the committed schema artifacts (run by `make schemas` through `scripts/gen_schemas.py`).

- `schemas/json/<name>.schema.json`: JSON Schema (draft 2020-12) of every document and top-level spec.
- `schemas/arrow/<table>.json`: dump of every Arrow table schema (`queuetruth.schemas.arrow.schema_json`).
- `schemas/samples/<name>.json`: a fully populated sample of every document and spec (`sample`), and for specs the
  required-only input (`minimal`) with its fully defaulted form (`defaults`); the Rust mirrors round-trip these.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from queuetruth.schemas import arrow
from queuetruth.schemas import specs as sp
from queuetruth.schemas.models import DOC_MODELS
from queuetruth.schemas.runid import canonical_json, identity_document, is_ref, run_id, spec_echo
from queuetruth.schemas.specs import SPEC_MODELS

DRAFT = "https://json-schema.org/draft/2020-12/schema"

_PATTERN_SAMPLES = {
    r"^[0-9a-f]{16}$": "0123456789abcdef",
    r"^[0-9a-f]{64}$": "0123456789abcdef" * 4,
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$": "2020-01-02",
    r"^QT-[A-Z]+-[0-9]{3}$": "QT-INPUT-002",
    r"^[0-9]+$": "1000000000",
    r"^-?[0-9]+$": "7",
    r"^[0-9]{2}:[0-9]{2}$": "09:30",
    # queuetruth.cert.models: run label and relative path inside a run or input directory
    r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$": "replay",
    r"^(?:[^/.][^/]*|\.[^/.][^/]*|\.\.[^/]+)(?:/(?:[^/.][^/]*|\.[^/.][^/]*|\.\.[^/]+))*$": "summary.json",
    # queuetruth.ledger.models: HEX32 ids and `study` / `name` labels
    r"^[0-9a-f]{32}$": "0123456789abcdef" * 2,
    r"^[A-Za-z0-9_.:\-]{1,128}$": "study_1",
    # queuetruth.sandbox.models: order ids and client order ids
    r"^o[1-9][0-9]*$": "o1",
    r"^[A-Za-z0-9_.:\-]+$": "c1",
}


def json_schema(name: str, model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema of a model with `$schema` and `title` set to the qt name."""
    body = model.model_json_schema(mode="validation")
    return {"$schema": DRAFT, "title": name, **{k: v for k, v in body.items() if k != "title"}}


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


class _Sampler:
    def __init__(self, root: dict[str, Any], required_only: bool) -> None:
        self.defs = root.get("$defs", {})
        self.required_only = required_only

    def _resolve(self, node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node:
            node = self.defs[node["$ref"].rsplit("/", 1)[-1]]
        return node

    def sample(self, node: dict[str, Any]) -> Any:
        node = self._resolve(node)
        if "const" in node:
            return node["const"]
        if "enum" in node:
            return node["enum"][0]
        for key in ("anyOf", "oneOf"):
            if key in node:
                options = [self._resolve(o) for o in node[key]]
                non_null = [o for o in options if o.get("type") != "null"]
                return self.sample(non_null[0] if non_null else options[0])
        t = node.get("type")
        if t == "object" or "properties" in node:
            return self._object(node)
        if t == "array":
            if "prefixItems" in node:
                return [self.sample(i) for i in node["prefixItems"]]
            item = self.sample(node.get("items", {}))
            return [item] * max(1, int(node.get("minItems", 1)))
        if t == "string":
            pat = node.get("pattern")
            if pat is not None:
                return _PATTERN_SAMPLES[pat]
            return "x"
        if t == "integer":
            lo = node.get("minimum")
            if "exclusiveMinimum" in node:
                lo = node["exclusiveMinimum"] + 1
            hi = node.get("maximum")
            v = 1
            if lo is not None:
                v = max(v, int(lo))
            if hi is not None:
                v = min(v, int(hi))
            return v
        if t == "number":
            return 0.5
        if t == "boolean":
            return True
        return 1  # Any

    def _object(self, node: dict[str, Any]) -> dict[str, Any]:
        props: dict[str, Any] = node.get("properties", {})
        if props:
            req = set(node.get("required", []))
            return {k: self.sample(v) for k, v in props.items() if not self.required_only or k in req}
        if self.required_only:
            return {}
        pattern_props: dict[str, Any] = node.get("patternProperties", {})
        if pattern_props:
            pat, sub = next(iter(pattern_props.items()))
            return {_PATTERN_SAMPLES[pat]: self.sample(sub)}
        addl = node.get("additionalProperties")
        if addl is None or addl is False:
            return {}
        pn = node.get("propertyNames", {})
        if "enum" in pn:
            key = pn["enum"][0]
        elif "pattern" in pn:
            key = _PATTERN_SAMPLES[pn["pattern"]]
        else:
            key = "k"
        return {key: self.sample(addl if isinstance(addl, dict) else {})}


#: Top-level sample values that a model validator needs beyond the JSON Schema (the derived sample gives every integer
#: the same value): `qt.sandbox_spec` requires `end_ns > start_ns`.
_SAMPLE_OVERRIDES: dict[str, dict[str, Any]] = {"qt.sandbox_spec": {"end_ns": 2}}


def sample(name: str, model: type[BaseModel], required_only: bool = False) -> dict[str, Any]:
    """A sample instance of the model derived from its JSON Schema (every optional field populated)."""
    sch = model.model_json_schema(mode="validation")
    out = _Sampler(sch, required_only).sample(sch)
    out.update({k: v for k, v in _SAMPLE_OVERRIDES.get(name, {}).items() if k in out})
    return out


#: Number formatting cases shared by the Rust and Python canonical JSON tests: (JSON text, canonical text).
CANONICAL_NUMBERS = [
    ("1e-7", "1e-7"),
    ("1E21", "1e+21"),
    ("1e20", "100000000000000000000"),
    ("0.000001", "0.000001"),
    ("1.5e-6", "0.0000015"),
    ("-0.0", "0"),
    ("5e-324", "5e-324"),
    ("1.7976931348623157e308", "1.7976931348623157e+308"),
    ("333333333.3333333", "333333333.3333333"),
    ("9007199254740992.0", "9007199254740992"),
    ("123e-20", "1.23e-18"),
    ("2.0", "2"),
    ("-3.5", "-3.5"),
    ("0.1", "0.1"),
    ("100", "100"),
    ("18446744073709551615", "18446744073709551615"),
    ("-9223372036854775808", "-9223372036854775808"),
]


def _kat_specs() -> list[tuple[str, dict[str, Any]]]:
    def dump(model: sp.SpecModel) -> dict[str, Any]:
        return model.model_dump(mode="json")

    return [
        ("replay_defaults", dump(sp.ReplaySpec(dataset=sp.PathRef(path="data/ds"), out="out/r1", threads=4))),
        (
            "shadow_sample_floats",
            dump(sp.ShadowSampleSpec(dataset=sp.DatasetIdRef(dataset_id="ds_01J0000000000000000000000"), n=3000)),
        ),
        (
            "calibration_nested",
            dump(
                sp.CalibrationSpec(
                    kind="alloc",
                    family="power3",
                    dataset=sp.PathRef(path="data/ds"),
                    shadows=sp.PathRef(path="data/shadows.parquet"),
                    train=sp.WindowSpec(dates=["2019-12-30"]),
                    test=sp.WindowSpec(blocks=sp.BlockWindow(index=[3, 4])),
                    out="cal",
                )
            ),
        ),
        (
            "scorecard_refs",
            dump(
                sp.ScorecardSpec(
                    live=sp.PathRef(path="live"),
                    sim=sp.SimExternal(
                        kind="external", log=sp.JobFileRef(job_file=sp.JobFile(job_id="job_1", name="x"))
                    ),
                    dataset=sp.DatasetIdRef(dataset_id="ds_1"),
                )
            ),
        ),
        # Schema 1.1 (SPEC 3.11): defaulted 1.1 fields leave the identity document (replay_defaults and
        # scorecard_refs keep their v0.1 run_id); non-default values enter it.
        (
            "replay_v1_1_fields",
            dump(
                sp.ReplaySpec(
                    dataset=sp.PathRef(path="data/ds"),
                    fill_rule="et",
                    hidden_print_policy="behind",
                    exclude_order_ids=[sp.ExcludeOrderId(instrument_id=7, order_id=123456789)],
                )
            ),
        ),
        (
            "scorecard_v1_1_fields",
            dump(
                sp.ScorecardSpec(
                    live=sp.PathRef(path="live"),
                    sim=sp.SimExact(kind="exact"),
                    dataset=sp.PathRef(path="data/ds"),
                    window_ns=30_000_000_000,
                    self_exclude=False,
                    markout_past_end="last_quote",
                )
            ),
        ),
        (
            "live_import_v1_1_fields",
            dump(
                sp.LiveImportSpec(
                    venue="fix44",
                    inputs=[sp.PathRef(path="dropcopy.log")],
                    tz="America/Chicago",
                    fix=sp.FixOptions(
                        venue_name="cme",
                        checksum="keep",
                        data_dictionary=["FIX44.xml"],
                        session_venues={"FIX.4.4-ME-CME": "cme"},
                        instrument_tag=107,
                    ),
                )
            ),
        ),
        (
            "zoo_v1_1_fields",
            dump(
                sp.ZooSpec(
                    dataset=sp.PathRef(path="data/ds"),
                    shadows=sp.PathRef(path="data/shadows.parquet"),
                    amends=sp.PathRef(path="data/amends.parquet"),
                    exclude_order_ids=[sp.ExcludeOrderId(instrument_id=7, order_id=123456789)],
                )
            ),
        ),
        (
            "ingest_v1_1_session",
            dump(
                sp.IngestSpec(
                    format="itch", inputs=[sp.PathRef(path="raw.itch")], venue="nasdaq", open_s=34_200, close_s=57_600
                )
            ),
        ),
        (
            "why_v1_1_fields",
            dump(
                sp.WhySpec(
                    dataset=sp.PathRef(path="data/ds"),
                    live=sp.PathRef(path="live"),
                    order_key="binance:acct:1",
                    exclude_order_ids=[sp.ExcludeOrderId(instrument_id=7, order_id=123456789)],
                )
            ),
        ),
        (
            "export_v1_1_force",
            dump(sp.ExportSpec(target="hftbacktest", calibration=sp.PathRef(path="cal.json"), force=True)),
        ),
        ("export_v1_1_defaults", dump(sp.ExportSpec(target="hftbacktest", calibration=sp.PathRef(path="cal.json")))),
        # Defaults of every 1.1 field (identity documents without them).
        ("live_import_v1_1_defaults", dump(sp.LiveImportSpec(venue="binance", inputs=[sp.PathRef(path="t.csv")]))),
        (
            "zoo_v1_1_defaults",
            dump(sp.ZooSpec(dataset=sp.PathRef(path="data/ds"), shadows=sp.PathRef(path="data/shadows.parquet"))),
        ),
        (
            "ingest_v1_1_defaults",
            dump(sp.IngestSpec(format="itch", inputs=[sp.PathRef(path="raw.itch")], venue="nasdaq")),
        ),
        # Late 1.1 additions: report sort horizon, fit-hazard zoo_shadows, calibrate-live
        # self-exclusion (defaults keep the run_id; set values enter it) and the never-cancel features spec.
        ("report_v1_1_defaults", dump(sp.ReportSpec(inputs=["out/zoo"], out="r.html"))),
        (
            "report_v1_1_sort_horizon",
            dump(sp.ReportSpec(inputs=["out/zoo"], out="r.html", leaderboard_sort_horizon_ns=10_000_000_000)),
        ),
        (
            "hazard_fit_v1_1_defaults",
            dump(sp.HazardFitSpec(features=sp.PathRef(path="feat/features.parquet"), out="m")),
        ),
        (
            "hazard_fit_v1_1_zoo_shadows",
            dump(
                sp.HazardFitSpec(
                    features=sp.PathRef(path="feat/features.parquet"),
                    zoo_predictions=sp.PathRef(path="zoo/zoo_predictions.parquet"),
                    zoo_shadows=sp.PathRef(path="zoo/zoo_shadows.parquet"),
                )
            ),
        ),
        (
            "live_calibration_v1_1_defaults",
            dump(
                sp.LiveCalibrationSpec(
                    dataset=sp.PathRef(path="data/ds"), live=sp.PathRef(path="live"), family="power3"
                )
            ),
        ),
        (
            "live_calibration_v1_1_fields",
            dump(
                sp.LiveCalibrationSpec(
                    dataset=sp.PathRef(path="data/ds"),
                    live=sp.PathRef(path="live"),
                    family="power3",
                    self_exclude=False,
                    match_window_ns=2_000_000,
                )
            ),
        ),
        (
            "features_never_cancel",
            dump(
                sp.FeatureSpec(
                    dataset=sp.PathRef(path="data/ds"),
                    shadows=sp.PathRef(path="data/shadows.parquet"),
                    cancel_away_ticks=None,
                )
            ),
        ),
        (
            "raw_edge_cases",
            {
                "\ue000": 1,
                "\U0001f600": 2,
                "b": [1e-7, 1e21, 0.1, -0.0, 18446744073709551615, 'tab\tquote"nl\n\u0001'],
                "a": {"out": "x", "threads": 8, "keep": None, "inner": [{"out": 1, "z": True}, {"path": "p"}]},
                "A": False,
            },
        ),
    ]


def run_id_kat() -> dict[str, Any]:
    """Known answers for `run_id` / canonical JSON shared with `qt_schema` (`schemas/run_id_kat.json`)."""
    cases = []
    for name, spec in _kat_specs():
        refs: dict[str, str] = {}

        def collect(v: Any, refs: dict[str, str] = refs) -> None:
            if is_ref(v):
                key = canonical_json(v)
                refs[key] = hashlib.sha256(key.encode()).hexdigest()
            elif isinstance(v, dict):
                for x in v.values():
                    collect(x)
            elif isinstance(v, list):
                for x in v:
                    collect(x)

        collect(spec)

        def resolve(r: dict[str, Any], refs: dict[str, str] = refs) -> str:
            return refs[canonical_json(r)]

        cases.append(
            {
                "name": name,
                "engine_version": "0.1.0",
                "spec": spec,
                "refs": refs,
                "identity_document": identity_document(spec, "0.1.0", resolve),
                "run_id": run_id(spec, "0.1.0", resolve),
                "echo": spec_echo(spec),
            }
        )
    return {
        "description": "Shared known answers for qt_schema::run_id and queuetruth.schemas.run_id (SPEC 7.3); "
        "regenerate with make schemas",
        "cases": cases,
        "numbers": [list(c) for c in CANONICAL_NUMBERS],
    }


def artifacts() -> dict[str, str]:
    """Every generated file, keyed by path relative to the repository root."""
    out: dict[str, str] = {}
    for name, doc_model in DOC_MODELS.items():
        out[f"schemas/json/{name}.schema.json"] = _dump(json_schema(name, doc_model))
        out[f"schemas/samples/{name}.json"] = _dump({"sample": sample(name, doc_model)})
    for name, spec_model in SPEC_MODELS.items():
        out[f"schemas/json/{name}.schema.json"] = _dump(json_schema(name, spec_model))
        minimal = sample(name, spec_model, required_only=True)
        defaults = spec_model.model_validate(minimal).model_dump(mode="json")
        out[f"schemas/samples/{name}.json"] = _dump(
            {"sample": sample(name, spec_model), "minimal": minimal, "defaults": defaults}
        )
    for table in arrow.schema_names():
        out[f"schemas/arrow/{table}.json"] = arrow.schema_json(table)
    out["schemas/run_id_kat.json"] = _dump(run_id_kat())
    return out


def write_all(repo_root: Path) -> list[Path]:
    """Writes every artifact under `repo_root`, removing stale files in the generated directories."""
    files = artifacts()
    written: list[Path] = []
    for sub in ("schemas/json", "schemas/arrow", "schemas/samples"):
        d = repo_root / sub
        d.mkdir(parents=True, exist_ok=True)
        for p in sorted(d.glob("*.json")):
            if f"{sub}/{p.name}" not in files:
                p.unlink()
    for rel, text in sorted(files.items()):
        p = repo_root / rel
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8")
        written.append(p)
    return written
