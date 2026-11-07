"""Contract tests of `queuetruth.schemas` (SPEC 3, 4.2, 6.7, 7.3): O-SCHEMA-1, O-SCHEMA-2, samples, run_id."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

import queuetruth as qt
from queuetruth.errors import QtApiError, QtError
from queuetruth.schemas import (
    ADDED_COLUMNS,
    DICT_VALUES,
    DOC_MODELS,
    FULL_SCHEMAS,
    SCHEMAS,
    SPEC_MODELS,
    V1_1_DEFAULTS,
    arrow,
    canonical_json,
    drop_v1_1_defaults,
    export,
    features_schema,
    identity_document,
    parse_document,
    run_id,
    spec_echo,
)
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s
from queuetruth.schemas.io import write_table

ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str) -> Any:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


# ---- O-SCHEMA-1 / O-SCHEMA-2 -------------------------------------------------------------------------------------


@pytest.mark.requires_core
def test_o_schema_1_rust_and_pyarrow_schemas_identical() -> None:
    from queuetruth import _core

    assert list(_core.schema_names()) == list(SCHEMAS)
    for name, schema in SCHEMAS.items():
        rust = _core.arrow_schema(name)
        assert isinstance(rust, pa.Schema)
        assert rust.equals(schema, check_metadata=True), name
        assert [f.nullable for f in rust] == [f.nullable for f in schema], name
    with pytest.raises(KeyError):
        _core.arrow_schema("qt.nope")


@pytest.mark.requires_core
def test_o_schema_1_core_version() -> None:
    from queuetruth import _core

    assert _core.version() == qt.__version__
    assert issubclass(_core.QtCoreError, Exception)


def test_o_schema_1_pyarrow_dumps_match_committed() -> None:
    names = sorted(p.stem for p in (ROOT / "schemas" / "arrow").glob("*.json"))
    assert names == sorted(SCHEMAS)
    for name in SCHEMAS:
        assert arrow.schema_json(name) == (ROOT / "schemas" / "arrow" / f"{name}.json").read_text(), name


def test_o_schema_2_make_schemas_leaves_no_diff() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_schemas.py"), "--check"], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    files = export.artifacts()
    for rel, text in files.items():
        assert (ROOT / rel).read_text(encoding="utf-8") == text, rel


def test_schema_table_conventions() -> None:
    # 31 engine tables (SPEC 3.4, 3.8 and the impact, shortfall and hazard tables) and 6 written by Python only
    # (qt.zoo_toxicity_fills / _markouts / _shadows, qt.conformal_sets / _calibrator, qt.ledger).
    assert len(SCHEMAS) == 37
    assert arrow.TABLE_VERSIONS == {"qt.conformal_sets": "1", "qt.conformal_calibrator": "1", "qt.ledger": "1"}
    for name, schema in [*SCHEMAS.items(), *FULL_SCHEMAS.items()]:
        meta = {k.decode(): v.decode() for k, v in schema.metadata.items()}
        version = arrow.TABLE_VERSIONS.get(name, "1.1")
        assert meta == {"qt.schema": name, "qt.schema_version": version, "qt.engine_version": qt.__version__}
        assert arrow.table_version(name) == version
        for f in schema:
            if (name, f.name) == ("qt.conformal_sets", "stratum"):
                # open dictionary: the stratum labels of the run
                assert f.type == pa.dictionary(pa.int16(), pa.utf8()) and f.name not in DICT_VALUES[name]
            elif pa.types.is_dictionary(f.type):
                assert f.type == pa.dictionary(pa.int8(), pa.utf8()), (name, f.name)
                assert DICT_VALUES[name][f.name], (name, f.name)
            if pa.types.is_timestamp(f.type):
                assert f.type == pa.timestamp("ns", tz="UTC"), (name, f.name)
    assert SCHEMAS["qt.shadow_results"].field("end_state").nullable is False
    assert SCHEMAS["qt.bbo"].field("bid_px").nullable is True
    assert DICT_VALUES["qt.instruments"]["venue"] == ("nasdaq", "cme", "coinbase", "generic")


def test_python_table_writers_use_the_registered_layouts() -> None:
    """The tables written by Python only equal their registered schemas (metadata included), so the dumps in
    `schemas/arrow` and the Rust mirror describe the files as written."""
    from queuetruth.eval import toxicity
    from queuetruth.ledger import _query as ledger_query
    from queuetruth.model import conformal

    for name in toxicity.TABLES:
        assert toxicity.table_schema(name).equals(SCHEMAS[name], check_metadata=True), name
        for col, _, _, values in toxicity.TABLES[name]:
            assert DICT_VALUES[name].get(col) == values, (name, col)
    assert DICT_VALUES["qt.zoo_toxicity_fills"]["mechanism"] == toxicity.MECHANISMS
    assert DICT_VALUES["qt.zoo_toxicity_shadows"]["status"] == toxicity.STATUSES
    assert conformal.sets_schema().equals(SCHEMAS[conformal.SETS_SCHEMA_NAME], check_metadata=True)
    calibrator = SCHEMAS[conformal.CALIBRATOR_SCHEMA_NAME]
    assert conformal._calibrator_schema().equals(calibrator, check_metadata=True)
    assert ledger_query._table_schema().equals(SCHEMAS["qt.ledger"], check_metadata=True)
    assert ledger_query.LEDGER_TABLE.equals(SCHEMAS["qt.ledger"], check_metadata=False)


def test_extension_documents_are_registered() -> None:
    """The documents of the extension packages are in `DOC_MODELS` / `SPEC_MODELS` with their package models, and the
    models moved into `queuetruth.schemas` (conformal, demo, bot reconciliation) stay importable from their old
    modules."""
    from queuetruth.demo import summary as demo
    from queuetruth.ledger import models as ledger
    from queuetruth.live.importers import freqtrade, hummingbot
    from queuetruth.model import conformal
    from queuetruth.sandbox import models as sandbox
    from queuetruth.schemas import conformal as sch_conformal
    from queuetruth.schemas import demo as sch_demo
    from queuetruth.schemas import reconcile as sch_reconcile

    expected = {
        "qt.demo_summary": demo.DemoSummary,
        "qt.demo_plan": demo.DemoPlan,
        "qt.demo_sources": demo.DemoSources,
        "qt.conformal_report": sch_conformal.ConformalReport,
        "qt.bot_reconcile": sch_reconcile.ReconcileReport,
        **ledger.DOCUMENTS,
        **sandbox.SANDBOX_DOC_MODELS,
    }
    assert len(expected) == 20
    for name, model in expected.items():
        assert DOC_MODELS[name] is model, name
        assert model.SCHEMA == name
    assert SPEC_MODELS["qt.sandbox_spec"] is sandbox.SandboxSpec
    for name in ("ConformalReport", "ConformalSpec", "CoverageCell", "DriftCell", "DriftGroup", "FillTimeCell"):
        assert getattr(conformal, name) is getattr(sch_conformal, name), name
    assert conformal.REPORT_SCHEMA == "qt.conformal_report"
    for name in demo.__all__:
        assert getattr(demo, name) is getattr(sch_demo, name), name
    for name in ("ReconcileOrder", "ReconcileReport", "ReconcileScore"):
        assert getattr(freqtrade, name) is getattr(sch_reconcile, name), name
    assert hummingbot.ReconcileReport is sch_reconcile.ReconcileReport
    assert freqtrade.CATEGORIES == sch_reconcile.Category.__args__  # type: ignore[attr-defined]


def test_features_schema_dynamic_columns() -> None:
    sch = features_schema(["dist_ticks", "imb_l1"])
    assert sch.names[6:10] == ["side_sign", "dist_ticks", "imb_l1", "event_ns"]
    assert sch.field("imb_l1").type == pa.float64() and sch.field("imb_l1").nullable
    with pytest.raises(ValueError):
        features_schema(["cause"])
    with pytest.raises(ValueError):
        features_schema(["x", "x"])
    # 1.1: the full layout appends midnight_utc_ns (nullable int64); the name is reserved in both layouts.
    full = features_schema(["dist_ticks", "imb_l1"], full=True)
    assert full.names == [*sch.names, "midnight_utc_ns"]
    assert full.field("midnight_utc_ns").type == pa.int64() and full.field("midnight_utc_ns").nullable
    assert full.metadata == sch.metadata
    for f in (features_schema, lambda n: features_schema(n, full=True)):
        with pytest.raises(ValueError):
            f(["midnight_utc_ns"])
    assert ADDED_COLUMNS["qt.features"] == ("midnight_utc_ns",)


def _shadow_spec_table() -> pa.Table:
    rows = {
        "shadow_id": ["s1"],
        "instrument_id": [1],
        "side": [ord("B")],
        "price_mode": pa.array(["abs"]).dictionary_encode().cast(pa.dictionary(pa.int8(), pa.utf8())),
        "price": [100],
        "offset_ticks": [0],
        "qty": [10],
        "ts_submit": [15],
        "latency_ns": [0],
        "ts_cancel": [None],
        "group": [None],
        "source": pa.array(["user"]).dictionary_encode().cast(pa.dictionary(pa.int8(), pa.utf8())),
        "order_key": [None],
    }
    return pa.table(rows, schema=SCHEMAS["qt.shadow_spec"])


def test_pyarrow_round_trip_with_schema(tmp_path: Path) -> None:
    sch = SCHEMAS["qt.shadow_spec"]
    table = _shadow_spec_table()
    entry = write_table(table, tmp_path / "s.parquet")
    assert entry["rows"] == 1 and entry["name"] == "s.parquet" and len(entry["sha256"]) == 64
    md = pq.ParquetFile(tmp_path / "s.parquet").metadata
    assert md.row_group(0).column(0).compression == "ZSTD"
    assert write_table(table, tmp_path / "t.parquet")["sha256"] == entry["sha256"]
    back = pq.read_table(tmp_path / "s.parquet")
    assert back.schema.metadata[b"qt.schema"] == b"qt.shadow_spec"
    assert back.cast(sch).equals(table)


@pytest.mark.requires_core
def test_core_tables_cross_the_arrow_capsule_interface(tmp_path: Path) -> None:
    """`_core.write_table` takes any `__arrow_c_stream__` object and `_core.read_table` returns a pyarrow.Table with
    the schema metadata (crates/py/src/arrow_ffi.rs)."""
    from queuetruth import _core

    table = _shadow_spec_table()
    reader = pa.RecordBatchReader.from_batches(table.schema, table.to_batches())
    entry = json.loads(_core.write_table(reader, str(tmp_path / "s.parquet")))
    assert entry["rows"] == 1
    back = _core.read_table(str(tmp_path / "s.parquet"), "qt.shadow_spec")
    assert isinstance(back, pa.Table) and back.schema.metadata[b"qt.schema"] == b"qt.shadow_spec"
    assert back.equals(table)
    assert json.loads(_core.write_table(back, str(tmp_path / "t.parquet")))["sha256"] == entry["sha256"]
    with pytest.raises(TypeError, match="__arrow_c_stream__"):
        _core.write_table({"shadow_id": ["s1"]}, str(tmp_path / "u.parquet"))


# ---- JSON documents and specs ------------------------------------------------------------------------------------


def _validator(name: str) -> jsonschema.Draft202012Validator:
    schema = _load(f"schemas/json/{name}.schema.json")
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


ALL_NAMES = sorted([*DOC_MODELS, *SPEC_MODELS])


@pytest.mark.parametrize("name", ALL_NAMES)
def test_samples_validate_and_round_trip(name: str) -> None:
    doc = _load(f"schemas/samples/{name}.json")
    model = DOC_MODELS.get(name) or SPEC_MODELS[name]
    sample = doc["sample"]
    _validator(name).validate(sample)
    obj = model.model_validate(sample)
    assert obj.model_dump(mode="json") == sample
    if "minimal" in doc:
        _validator(name).validate(doc["minimal"])
        _validator(name).validate(doc["defaults"])
        assert model.model_validate(doc["minimal"]).model_dump(mode="json") == doc["defaults"]


@pytest.mark.parametrize("name", sorted(DOC_MODELS))
def test_documents_require_schema_tag(name: str) -> None:
    schema = _load(f"schemas/json/{name}.schema.json")
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"][:2] == ["schema", "schema_version"]
    assert list(schema["properties"])[:2] == ["schema", "schema_version"]
    assert schema["properties"]["schema"]["const"] == name
    assert schema["additionalProperties"] is False
    sample = _load(f"schemas/samples/{name}.json")["sample"]
    v = _validator(name)
    for bad in ({**sample, "schema": "qt.other"}, {**sample, "schema_version": 2}, {**sample, "extra": 1}):
        assert not v.is_valid(bad)
    missing = dict(sample)
    del missing["schema"]
    assert not v.is_valid(missing)
    assert type(parse_document(sample)) is DOC_MODELS[name]


def test_specs_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        s.ReplaySpec.model_validate({"dataset": {"path": "d"}, "bogus": 1})
    with pytest.raises(ValidationError):
        s.ReplaySpec.model_validate({"dataset": {"path": "d", "x": 1}})
    with pytest.raises(ValidationError):
        s.ScorecardSpec.model_validate({"live": {"path": "l"}, "sim": {"kind": "exact", "model": "m"}})
    sc = s.ScorecardSpec.model_validate({"live": {"path": "l"}, "sim": {"kind": "zoo", "model": "front_of_queue"}})
    assert isinstance(sc.sim, s.SimZoo) and sc.sim.l2 == s.L2Config()
    assert s.ReplaySpec.model_validate({"dataset": {"dataset_id": "ds_1"}}).dataset == s.DatasetIdRef(dataset_id="ds_1")


def test_spec_defaults_match_spec_4_2() -> None:
    r = s.ReplaySpec(dataset=s.PathRef(path="d"))
    got = (r.sweep_gap_ns, r.bbo, r.trajectory, r.depth_levels, r.threads, r.seed)
    assert got == (2_000_000, "change", "ahead", 0, 1, 0)
    z = s.ZooSpec(dataset=s.PathRef(path="d"), shadows=s.PathRef(path="x"))
    assert z.horizons_ns == [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000]
    got = (z.h_max_ns, z.particles, z.models, z.mode, z.exchange)
    assert got == (300_000_000_000, 100, ["all"], "faithful", "partial")
    c = s.CalibrationSpec(kind="alloc", family="power3")
    assert (c.grid.alloc.lo, c.grid.alloc.hi, c.grid.alloc.n) == (-3.0, 4.0, 141)
    assert (c.grid.databento.lo, c.grid.databento.hi, c.grid.databento.n) == (-0.99, 0.99, 199)
    assert c.bootstrap == s.BootstrapSpec(b=200, block="auto", seed=0)
    sc = s.ScorecardSpec(live=s.PathRef(path="l"), sim=s.SimExact(kind="exact"))
    assert sc.bootstrap.b == 1000 and sc.perm_b == 2000 and sc.fdr.q == 0.05
    assert sc.markout_horizons_ns[-1] == 300_000_000_000
    sh = s.ShadowSampleSpec(dataset=s.PathRef(path="d"), n=10)
    assert [(p.offset_ticks, p.weight) for p in sh.placements] == [(0, 0.6), (-1, 0.2), (-2, 0.1), (1, 0.1)]
    assert s.L2Config() == s.L2Config(depth_levels=10, cadence_ns=0, trade_position="before")
    h = s.HazardFitSpec(features=s.PathRef(path="f"))
    assert h.grid_ns[0] == 100_000_000 and h.grid_ns[-1] == 60_000_000_000 and h.gbdt.max_iter == 300
    assert s.FeatureSpec(dataset=s.PathRef(path="d")).trackers[1] == "hbt_power3[n=1]"


def test_aliased_fields_serialize_by_alias() -> None:
    p = m.Parity(
        trades_compared=0,
        trades_mismatches=0,
        trades_missing_derived=0,
        trades_extra_derived=0,
        bbo_compared=0,
        bbo_mismatches=0,
        first_mismatches=[],
        pass_=True,
    )
    assert p.model_dump()["pass"] is True
    assert json.loads(p.model_dump_json())["pass"] is True
    dq = m.Error(code="QT-USAGE-001", message="x", details={})
    assert dq.model_dump()["schema"] == "qt.error"
    assert (
        m.Error.model_validate(
            {"schema": "qt.error", "schema_version": 1, "code": "QT-USAGE-001", "message": "x", "details": {}}
        ).schema_
        == "qt.error"
    )


# ---- errors -----------------------------------------------------------------------------------------------------


def test_errors() -> None:
    e = QtError("QT-INPUT-003", "duplicate shadow_id: x", hint="dedupe")
    assert str(e) == "QT-INPUT-003: duplicate shadow_id: x"
    assert (e.code, e.message, e.hint, e.exit_code) == ("QT-INPUT-003", "duplicate shadow_id: x", "dedupe", 2)
    _validator("qt.error").validate(e.to_document())
    assert QtError("QT-GATE-001", "gate").exit_code == 1
    assert QtError.from_display("QT-CONFIG-001: venue: bad").code == "QT-CONFIG-001"
    assert QtError.from_display("boom").code == "QT-ENGINE-001"
    a = QtApiError(
        {
            "type": "https://queuetruth.dev/problems/not-found",
            "title": "Not Found",
            "status": 404,
            "code": "QT-INPUT-001",
            "detail": "no such job",
        }
    )
    assert isinstance(a, QtError) and a.code == "QT-REMOTE-001" and a.status == 404 and a.remote_code == "QT-INPUT-001"
    assert a.problem["status"] == 404
    assert qt.QtError is QtError and qt.QtApiError is QtApiError


# ---- run identity -----------------------------------------------------------------------------------------------

KAT = _load("schemas/run_id_kat.json")


@pytest.mark.parametrize("case", KAT["cases"], ids=lambda c: c["name"])
def test_run_id_known_answers(case: dict) -> None:
    refs = case["refs"]

    def resolve(r: dict) -> str:
        return refs[canonical_json(r)]

    assert identity_document(case["spec"], case["engine_version"], resolve) == case["identity_document"]
    assert run_id(case["spec"], case["engine_version"], resolve) == case["run_id"]
    assert spec_echo(case["spec"]) == case["echo"]


@pytest.mark.parametrize(("text", "expected"), KAT["numbers"])
def test_canonical_numbers(text: str, expected: str) -> None:
    assert canonical_json(json.loads(text)) == expected


def test_canonical_json_rules() -> None:
    assert canonical_json({"b": 1, "a": [True, None, "é\n"]}) == '{"a":[true,null,"é\\n"],"b":1}'
    assert canonical_json({"\U0001f600": 1, "": 2}) == '{"\U0001f600":1,"":2}'
    with pytest.raises(ValueError):
        canonical_json(float("nan"))


def test_run_id_ignores_execution_fields_and_resolves_refs() -> None:
    base = s.ReplaySpec(dataset=s.PathRef(path="a")).model_dump(mode="json")
    moved = s.ReplaySpec(dataset=s.PathRef(path="b"), out="elsewhere", threads=8).model_dump(mode="json")

    def same(_: dict) -> str:
        return "0" * 64

    assert run_id(base, "0.1.0", same) == run_id(moved, "0.1.0", same)
    assert run_id(base, "0.1.0", same) != run_id(base, "0.2.0", same)
    assert run_id(base, "0.1.0", same) != run_id(base, "0.1.0", lambda _: "1" * 64)
    assert len(run_id(base, "0.1.0", same)) == 16
    echo = spec_echo(moved)
    assert "out" not in echo and "threads" not in echo and echo["dataset"] == {"path": "b"}


# ---- schema 1.1 (SPEC 3.11) --------------------------------------------------------------------------------------

#: run_id of the v0.1 known answers (committed before the 1.1 bump); 1.1 must not change them.
V0_1_RUN_IDS = {
    "replay_defaults": "8fe5ca7372885231",
    "shadow_sample_floats": "b766b42bc44fcf6a",
    "calibration_nested": "0cd47ab5167fff23",
    "scorecard_refs": "ac555f359b65ad3e",
    "raw_edge_cases": "cf7ec15b8d295933",
}
#: run_id of default specs whose 1.1 fields arrived last (computed before those fields existed): unchanged.
V0_1_RUN_IDS_TAIL = {
    "report_v1_1_defaults": "d66c2a2ce04f8ea3",
    "hazard_fit_v1_1_defaults": "7e5fe3f069f57418",
    "live_calibration_v1_1_defaults": "7d64f9816022cbe8",
}


def test_v1_1_keeps_v0_1_run_ids() -> None:
    got = {c["name"]: c["run_id"] for c in KAT["cases"]}
    for name, rid in {**V0_1_RUN_IDS, **V0_1_RUN_IDS_TAIL}.items():
        assert got[name] == rid, name
    by_name = {c["name"]: c for c in KAT["cases"]}
    for name, key in (
        ("report_v1_1_sort_horizon", "leaderboard_sort_horizon_ns"),
        ("hazard_fit_v1_1_zoo_shadows", "zoo_shadows"),
        ("live_calibration_v1_1_fields", "self_exclude"),
        ("live_calibration_v1_1_fields", "match_window_ns"),
        ("features_never_cancel", '"cancel_away_ticks":null'),
    ):
        assert key in by_name[name]["identity_document"], name
    for name, key in (
        ("report_v1_1_defaults", "leaderboard_sort_horizon_ns"),
        ("hazard_fit_v1_1_defaults", "zoo_shadows"),
        ("live_calibration_v1_1_defaults", "self_exclude"),
        ("live_calibration_v1_1_defaults", "match_window_ns"),
    ):
        assert key not in by_name[name]["identity_document"], name
        assert key in by_name[name]["echo"], name
    assert by_name["report_v1_1_sort_horizon"]["run_id"] != by_name["report_v1_1_defaults"]["run_id"]
    assert by_name["live_calibration_v1_1_fields"]["run_id"] != by_name["live_calibration_v1_1_defaults"]["run_id"]
    assert "fill_rule" in by_name["replay_v1_1_fields"]["identity_document"]
    assert "fill_rule" not in by_name["replay_defaults"]["identity_document"]
    assert by_name["replay_v1_1_fields"]["run_id"] != by_name["replay_defaults"]["run_id"]


SAME = "0" * 64


def _id(spec: dict) -> str:
    return run_id(spec, "0.1.0", lambda _: SAME)


V1_1_SPEC_FIELDS = {
    s.ReplaySpec: {"fill_rule": "exact", "hidden_print_policy": None, "exclude_order_ids": []},
    s.ZooSpec: {"amends": None, "exclude_order_ids": []},
    s.ScorecardSpec: {
        "window_ns": 60_000_000_000,
        "match_window_ns": 1_000_000,
        "self_exclude": True,
        "markout_past_end": None,
    },
    s.LiveImportSpec: {"tz": None},
    s.IngestSpec: {"open_s": None, "close_s": None},
    s.WhySpec: {"exclude_order_ids": []},
    s.ExportSpec: {"force": False},
    s.HazardFitSpec: {"zoo_shadows": None},
    s.ReportSpec: {"leaderboard_sort_horizon_ns": None},
    s.LiveCalibrationSpec: {"self_exclude": True, "match_window_ns": 1_000_000},
}

MINIMAL = {
    s.ReplaySpec: {"dataset": {"path": "d"}},
    s.ZooSpec: {"dataset": {"path": "d"}, "shadows": {"path": "s"}},
    s.ScorecardSpec: {"live": {"path": "l"}, "sim": {"kind": "exact"}},
    s.LiveImportSpec: {"venue": "fix44", "inputs": [{"path": "x"}]},
    s.IngestSpec: {"format": "itch", "inputs": [{"path": "x"}], "venue": "nasdaq"},
    s.WhySpec: {"dataset": {"path": "d"}, "shadows": {"path": "s"}, "shadow_id": "x"},
    s.ExportSpec: {"target": "hftbacktest", "calibration": {"path": "c"}},
    s.HazardFitSpec: {"features": {"path": "f"}},
    s.ReportSpec: {"inputs": ["o"]},
    s.LiveCalibrationSpec: {"dataset": {"path": "d"}, "live": {"path": "l"}, "family": "power3"},
}


@pytest.mark.parametrize("model", list(V1_1_SPEC_FIELDS), ids=lambda m: m.__name__)
def test_v1_1_spec_defaults_do_not_change_run_id(model: type) -> None:
    full = model.model_validate(MINIMAL[model]).model_dump(mode="json")
    fields = V1_1_SPEC_FIELDS[model]
    for k, v in fields.items():
        assert full[k] == v, k
    v0_1 = {k: v for k, v in full.items() if k not in fields}
    if model is s.LiveImportSpec:
        fix_new = {"venue_name": None, "checksum": "exclude", "data_dictionary": None, "session_venues": {}}
        fix_new["instrument_tag"] = None
        assert {k: full["fix"][k] for k in fix_new} == fix_new
        v0_1["fix"] = {k: v for k, v in full["fix"].items() if k not in fix_new}
        for k, v in (("checksum", "keep"), ("session_venues", {"S": "cme"}), ("instrument_tag", 107)):
            assert _id({**full, "fix": {**full["fix"], k: v}}) != _id(full), k
    assert _id(full) == _id(v0_1)
    assert drop_v1_1_defaults(full) == v0_1
    assert all(k in spec_echo(full) for k in fields), "echoes keep the 1.1 fields"


def test_v1_1_non_defaults_change_run_id() -> None:
    base = s.ReplaySpec.model_validate(MINIMAL[s.ReplaySpec]).model_dump(mode="json")
    for k, v in (
        ("fill_rule", "et"),
        ("hidden_print_policy", "ignore"),
        ("exclude_order_ids", [{"instrument_id": 1, "order_id": 2}]),
    ):
        assert _id({**base, k: v}) != _id(base), k
    sc = s.ScorecardSpec.model_validate(MINIMAL[s.ScorecardSpec]).model_dump(mode="json")
    for k, v in (("window_ns", 1), ("match_window_ns", 0), ("self_exclude", False), ("markout_past_end", "last_quote")):
        assert _id({**sc, k: v}) != _id(sc), k
    zoo = s.ZooSpec.model_validate(MINIMAL[s.ZooSpec]).model_dump(mode="json")
    assert _id({**zoo, "exclude_order_ids": [{"instrument_id": 1, "order_id": 2}]}) != _id(zoo)
    # W1: per-partition quotas enter the run id only when set
    smp = s.ShadowSampleSpec.model_validate({"dataset": {"path": "x"}, "n": 10}).model_dump(mode="json")
    assert _id({**smp, "partition_quotas": {"7": 3}}) != _id(smp)
    assert _id(smp) == _id({k: v for k, v in smp.items() if k != "partition_quotas"})
    # float-typed defaults canonicalise like ints; the discriminator keeps other specs untouched.
    assert _id({**sc, "window_ns": 6e10}) == _id(sc)
    other = {"tz": None, "amends": None, "window_ns": 60_000_000_000}
    assert drop_v1_1_defaults(other) == other
    # ReplaySpec.amends is a v0.1 field and stays in the identity document.
    assert '"amends":null' in identity_document(base, "0.1.0", lambda _: SAME)
    assert {d[0] for d in V1_1_DEFAULTS} == {
        "sweep_gap_ns",
        "write_fills",
        "markout_horizons_ns",
        "fix",
        "format",
        "max_events",
        "target",
        "gbdt",
        "max_why",
        "family",
        "window_open_s",
    }
    # `family` is shared with CalibrationSpec, which has none of LiveCalibrationSpec's 1.1 fields.
    cal = s.CalibrationSpec.model_validate({"kind": "alloc", "family": "power"}).model_dump(mode="json")
    assert drop_v1_1_defaults(cal) == cal
    for model, k, v in (
        (s.HazardFitSpec, "zoo_shadows", {"path": "z"}),
        (s.ReportSpec, "leaderboard_sort_horizon_ns", 10_000_000_000),
        (s.LiveCalibrationSpec, "self_exclude", False),
        (s.LiveCalibrationSpec, "match_window_ns", 0),
    ):
        d = model.model_validate(MINIMAL[model]).model_dump(mode="json")
        assert _id({**d, k: v}) != _id(d), (model.__name__, k)
    # FeatureSpec.cancel_away_ticks (v0.1 field, default 2 unchanged) accepts null / 0 = never cancel.
    fs = s.FeatureSpec.model_validate({"dataset": {"path": "d"}}).model_dump(mode="json")
    assert fs["cancel_away_ticks"] == 2 and "cancel_away_ticks" in identity_document(fs, "0.1.0", lambda _: SAME)
    for v in (None, 0):
        assert s.FeatureSpec.model_validate({"dataset": {"path": "d"}, "cancel_away_ticks": v}).cancel_away_ticks == v
    with pytest.raises(ValidationError):
        s.FeatureSpec.model_validate({"dataset": {"path": "d"}, "cancel_away_ticks": -1})
    with pytest.raises(ValidationError):
        s.ReportSpec.model_validate({"inputs": ["o"], "leaderboard_sort_horizon_ns": 0})
    with pytest.raises(ValidationError):
        s.LiveCalibrationSpec.model_validate({**MINIMAL[s.LiveCalibrationSpec], "match_window_ns": -1})
    why = s.WhySpec.model_validate(MINIMAL[s.WhySpec]).model_dump(mode="json")
    assert _id({**why, "exclude_order_ids": [{"instrument_id": 1, "order_id": 2}]}) != _id(why)


def test_v1_1_spec_values() -> None:
    r = s.ReplaySpec.model_validate(
        {
            "dataset": {"path": "d"},
            "fill_rule": "et",
            "hidden_print_policy": "behind_and_through",
            "exclude_order_ids": [{"instrument_id": 3, "order_id": 9}],
        }
    )
    assert r.exclude_order_ids == [s.ExcludeOrderId(instrument_id=3, order_id=9)]
    with pytest.raises(ValidationError):
        s.ReplaySpec.model_validate({"dataset": {"path": "d"}, "fill_rule": "fast"})
    with pytest.raises(ValidationError):
        s.ReplaySpec.model_validate({"dataset": {"path": "d"}, "exclude_order_ids": [{"order_id": 9}]})
    assert s.LiveImportSpec.model_validate({"venue": "sim_hftbacktest", "inputs": [{"path": "x"}]}).tz is None
    with pytest.raises(ValidationError):
        s.CalibrationSpec.model_validate({"kind": "live", "family": "power"})
    assert SPEC_MODELS["qt.live_calibration_spec"] is s.LiveCalibrationSpec


def test_live_calibration_spec_matches_as_built() -> None:
    """The registered spec has exactly the fields and defaults of `queuetruth.score.calibrate_live`'s model."""
    built = pytest.importorskip("queuetruth.score.calibrate_live").LiveCalibrationSpec
    reg = s.LiveCalibrationSpec
    assert list(built.model_fields) == list(reg.model_fields)
    doc = {"dataset": {"path": "d"}, "live": {"path": "l"}, "family": "power3"}
    assert built.model_validate(doc).model_dump(mode="json") == reg.model_validate(doc).model_dump(mode="json")


def test_v1_1_documents_accept_v0_1() -> None:
    for name, drop in (
        ("qt.replay_summary", ["shadow_diagnostics"]),
        ("qt.live_manifest", ["coverage", "entry_fallback_share"]),
        (
            "qt.scorecard_summary",
            ["n_twin_unmatched", "twin_match_rate", "discordance_clustered", "n_taker_fills", "fills_only"],
        ),
        ("qt.dataset_manifest", ["rule_set", "source_vintage"]),
        (
            "qt.live_import_summary",
            ["coverage", "entry_fallback_share", "unmapped_instruments", "instrument_scales"],
        ),
        ("qt.calibration_result", ["held_out_discordance", "trust"]),
        ("qt.why_trace", ["model_id", "model_queue_ahead", "first_divergence", "conclusion", "warnings"]),
    ):
        sample = dict(_load(f"schemas/samples/{name}.json")["sample"])
        assert list(sample)[-len(drop) :] == drop, name
        for k in drop:
            del sample[k]
        _validator(name).validate(sample)
        dumped = DOC_MODELS[name].model_validate(sample).model_dump(mode="json")
        assert all(dumped[k] in (None, []) for k in drop), name
    card = dict(_load("schemas/samples/qt.hazard_model_card.json")["sample"])
    card["scores_test"] = {k: v for k, v in card["scores_test"].items() if k != "hl_p_et"}
    _validator("qt.hazard_model_card").validate(card)
    key = dict(_load("schemas/samples/qt.api_key_created.json")["sample"], secret=None)
    _validator("qt.api_key_created").validate(key)
    assert m.ApiKeyCreated.model_validate(key).secret is None
    cal = dict(_load("schemas/samples/qt.calibration_result.json")["sample"], kind="live")
    _validator("qt.calibration_result").validate(cal)


def test_v1_1_arrow_layouts(tmp_path: Path) -> None:
    assert ADDED_COLUMNS["qt.shadow_fills"] == ("liquidity",)
    assert ADDED_COLUMNS["qt.shadow_results"] == ("ts_planned_end", "planned_end_kind")
    assert ADDED_COLUMNS["qt.live_orders"] == ("replaces_order_key",)
    assert ADDED_COLUMNS["qt.zoo_fills"] == ("liquidity",)
    assert ADDED_COLUMNS["qt.zoo_predictions"] == ("liquidity", "status")  # status: faithful hbt #312 aborts
    assert sum(1 for v in ADDED_COLUMNS.values() if v) == 9
    for name in SCHEMAS:
        core, full = SCHEMAS[name], FULL_SCHEMAS[name]
        assert full.names == core.names + list(ADDED_COLUMNS[name]), name
        assert arrow.full_schema(name) is full
    assert DICT_VALUES["qt.live_orders"]["tif"][-1] == "gtd"
    assert DICT_VALUES["qt.live_orders"]["validation_status"] == ("real", "fixture_only", "simulated")
    assert DICT_VALUES["qt.live_events"]["source"][-1] == "sim"
    assert SCHEMAS["qt.zoo_shadows"].names[-1] == "never_cancel"
    dump = json.loads(arrow.schema_json("qt.shadow_fills"))
    assert dump["fields"][-1] == {
        "name": "liquidity",
        "type": "dictionary<int8, utf8>",
        "nullable": False,
        "values": ["maker", "taker"],
        "since": "1.1",
    }
    # upgrade: liquidity derived from reason; conform accepts both layouts.
    d = pa.dictionary(pa.int8(), pa.utf8())
    reasons = DICT_VALUES["qt.shadow_fills"]["reason"]
    cols: dict[str, Any] = {
        f.name: pa.array([0, 0], type=f.type) for f in SCHEMAS["qt.shadow_fills"] if pa.types.is_integer(f.type)
    }
    cols["shadow_id"] = pa.array(["a", "b"])
    cols["ts_event"] = pa.array([1, 2], type=arrow.TS)
    cols["reason"] = pa.DictionaryArray.from_arrays(
        pa.array([2, 3], type=pa.int8()), pa.array(list(reasons), type=pa.utf8())
    )
    cols = {f.name: cols[f.name] for f in SCHEMAS["qt.shadow_fills"]}
    core_t = pa.table(cols, schema=SCHEMAS["qt.shadow_fills"])
    full_t = arrow.upgrade(core_t, "qt.shadow_fills")
    assert full_t.schema.equals(FULL_SCHEMAS["qt.shadow_fills"], check_metadata=True)
    assert full_t.column("liquidity").to_pylist() == ["maker", "taker"]
    assert full_t.column("liquidity").type == d
    assert arrow.conform(full_t, "qt.shadow_fills").num_columns == core_t.num_columns + 1
    assert arrow.conform(core_t, "qt.shadow_fills").schema.equals(SCHEMAS["qt.shadow_fills"])
    with pytest.raises(ValueError):
        arrow.conform(core_t.drop_columns(["fill_px"]), "qt.shadow_fills")
    write_table(full_t, tmp_path / "f.parquet", FULL_SCHEMAS["qt.shadow_fills"])
    back = pq.read_table(tmp_path / "f.parquet")
    assert back.schema.metadata[b"qt.schema_version"] == b"1.1"
    lo = pa.table(
        {f.name: pa.nulls(0, type=f.type) for f in SCHEMAS["qt.live_orders"]}, schema=SCHEMAS["qt.live_orders"]
    )
    assert arrow.upgrade(lo, "qt.live_orders").schema.names[-1] == "replaces_order_key"
    sr = pa.table(
        {f.name: pa.nulls(0, type=f.type) for f in SCHEMAS["qt.shadow_results"]}, schema=SCHEMAS["qt.shadow_results"]
    )
    with pytest.raises(ValueError, match="planned_end_kind"):
        arrow.upgrade(sr, "qt.shadow_results")


def test_v1_1_trust_and_live_import_fields() -> None:
    cal = dict(_load("schemas/samples/qt.calibration_result.json")["sample"])
    cal["trust"] = {
        "verdict": "refused",
        "reasons": ["at_grid_edge", "not_beating_default"],
        "flags": ["at_grid_edge", "plateau"],
        "beats_default": {"100000000": "n/a", "1000000000": "worse"},
    }
    _validator("qt.calibration_result").validate(cal)
    assert m.CalibrationResult.model_validate(cal).trust.verdict == "refused"
    assert not _validator("qt.calibration_result").is_valid({**cal, "trust": {**cal["trust"], "verdict": "maybe"}})
    with pytest.raises(ValidationError):
        m.CalibrationTrust.model_validate({**cal["trust"], "beats_default": {"1s": "better"}})
    li = dict(_load("schemas/samples/qt.live_import_summary.json")["sample"])
    li["unmapped_instruments"] = [{"venue": "fix44", "instrument": "ESZ6", "n_orders": 3}]
    li["instrument_scales"] = [
        {
            "venue": "fix44",
            "instrument": "ESZ6",
            "instrument_id": None,
            "price_exp": -9,
            "size_exp": -8,
            "scale_source": "default",
            "n_orders": 3,
        }
    ]
    _validator("qt.live_import_summary").validate(li)
    fx = s.FixOptions()
    assert (fx.checksum, fx.data_dictionary, fx.session_venues, fx.instrument_tag) == ("exclude", None, {}, None)
    assert s.FixOptions(data_dictionary="FIX44.xml").data_dictionary == "FIX44.xml"
    with pytest.raises(ValidationError):
        s.FixOptions(checksum="strict")
    assert s.ExportSpec(target="hftbacktest").force is False


def test_v1_1_last_batch() -> None:
    # A5 as refined (E48): only crossed_at_live is taker when upgrading v0.1 fills.
    assert arrow.TAKER_REASONS == ("crossed_at_live",)
    # ExcludeOrderId.session_date: null keeps run_id, a date changes it (list-item wildcard rule).
    base = s.ReplaySpec.model_validate(
        {"dataset": {"path": "d"}, "exclude_order_ids": [{"instrument_id": 1, "order_id": 2}]}
    ).model_dump(mode="json")
    assert base["exclude_order_ids"][0]["session_date"] is None
    v0 = {**base, "exclude_order_ids": [{"instrument_id": 1, "order_id": 2}]}
    assert _id(base) == _id(v0)
    dated = {**base, "exclude_order_ids": [{"instrument_id": 1, "order_id": 2, "session_date": "2019-12-30"}]}
    assert _id(dated) != _id(base)
    with pytest.raises(ValidationError):
        s.ExcludeOrderId(instrument_id=1, order_id=2, session_date="20191230")
    # Score tables.
    assert SCHEMAS["qt.score_fills"].field("px").nullable
    assert ADDED_COLUMNS["qt.score_fills"] == ("side", "instrument_id", "fee", "fee_ccy", "price_exp", "size_exp")
    assert ADDED_COLUMNS["qt.score_markouts"] == ("mo_bps", "mo_net_ccy")
    assert ADDED_COLUMNS["qt.score_orders"] == ("price_exp", "size_exp")
    # Documents.
    ing = dict(_load("schemas/samples/qt.ingest_summary.json")["sample"])
    assert list(ing)[-1] == "warnings"
    del ing["warnings"]
    _validator("qt.ingest_summary").validate(ing)
    assert m.IngestSummary.model_validate(ing).warnings == []
    man = dict(_load("schemas/samples/qt.dataset_manifest.json")["sample"])
    man["source_vintage"] = [
        {"sha256": "0" * 64, "dbn_version": 3, "dataset": "XNAS.ITCH", "start_ns": 1, "end_ns": None},
    ]
    _validator("qt.dataset_manifest").validate(man)
    assert m.DatasetManifest.model_validate(man).source_vintage[0].publisher_id is None
    # Optional metadata keys are documented in the dumps.
    dump = json.loads(arrow.schema_json("qt.zoo_trajectory"))
    assert list(dump) == ["name", "metadata", "optional_metadata", "fields"]
    assert "qt.zoo_trajectory_scope" in dump["optional_metadata"]
    assert "optional_metadata" not in json.loads(arrow.schema_json("qt.mbo"))


def test_zoo_predictions_prefix_layout_reads_and_upgrades() -> None:
    """A `qt.zoo_predictions` file written before `status` existed (v0.1 columns + `liquidity`) conforms to that
    prefix and upgrades to the full layout with a null `status`."""
    full = FULL_SCHEMAS["qt.zoo_predictions"]
    prefix = pa.schema(list(full)[:-1])
    t = prefix.empty_table()
    assert arrow.conform(t, "qt.zoo_predictions").schema.names == prefix.names
    up = arrow.upgrade(t, "qt.zoo_predictions")
    assert up.schema.names == full.names and up.column("status").null_count == 0
    with pytest.raises(ValueError):
        arrow.conform(pa.schema(list(full)[:3]).empty_table(), "qt.zoo_predictions")
