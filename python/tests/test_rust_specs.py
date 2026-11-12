"""`scripts/gen_rust_specs.py`: the Rust spec structs are generated from the JSON Schemas; the binding is checked."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gen_rust_specs", ROOT / "scripts" / "gen_rust_specs.py")
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
sys.modules["gen_rust_specs"] = gen
_spec.loader.exec_module(gen)

needs_rustfmt = pytest.mark.skipif(shutil.which("rustfmt") is None, reason="rustfmt not installed")


def _copy_inputs(tmp_path: Path) -> Path:
    for sub in ("schemas/json", "schemas/samples"):
        (tmp_path / sub).mkdir(parents=True)
        for name in gen.SPECS:
            src = ROOT / sub / (f"{name}.schema.json" if sub == "schemas/json" else f"{name}.json")
            shutil.copy(src, tmp_path / sub / src.name)
    return tmp_path


def _load(root: Path) -> gen.Gen:
    g = gen.Gen(root)
    g.load()
    return g


@needs_rustfmt
def test_the_committed_file_is_current() -> None:
    assert (ROOT / gen.OUT).read_text(encoding="utf-8") == gen.generate(), "run `make generate`"


def test_every_spec_struct_and_field_comes_from_the_schema() -> None:
    g = _load(ROOT)
    for name in gen.SPECS:
        schema = json.loads((ROOT / f"schemas/json/{name}.schema.json").read_text(encoding="utf-8"))
        s = g.structs[gen.camel(name.removeprefix("qt."))]
        assert [f.name for f in s.fields] == list(schema["properties"])
        assert {f.name for f in s.fields if f.required} == set(schema.get("required", []))


def test_a_new_schema_field_and_enum_value_need_no_hand_edit(tmp_path: Path) -> None:
    root = _copy_inputs(tmp_path)
    p = root / "schemas/json/qt.replay_spec.schema.json"
    schema = json.loads(p.read_text(encoding="utf-8"))
    schema["properties"]["bbo"]["enum"].append("sampled")
    schema["properties"]["note"] = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    p.write_text(json.dumps(schema), encoding="utf-8")
    text = _load(root).render()
    assert '#[serde(rename = "sampled")]\n    Sampled,' in text
    assert "pub note: Option<String>," in text


def test_a_stale_or_missing_binding_fails_with_the_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(gen.INTS, "ReplaySpec.gone", "u64")
    with pytest.raises(gen.GenError, match=r"ReplaySpec\.gone"):
        _load(ROOT)
    monkeypatch.delitem(gen.INTS, "ReplaySpec.gone")
    monkeypatch.setitem(gen.ENUMS, "BboModeName", ())
    with pytest.raises(gen.GenError, match=r"ReplaySpec\.bbo: an enum without a Rust name"):
        _load(ROOT)


def test_defaults_without_a_schema_default_come_from_the_samples() -> None:
    g = _load(ROOT)
    calib = {f.name: f for f in g.structs["CalibrationSpec"].fields}
    assert calib["bootstrap"].default == {"b": 200, "block": "auto", "seed": 0}
    score = {f.name: f for f in g.structs["ScorecardSpec"].fields}
    assert score["bootstrap"].default["b"] == 1000
    grid = {f.name: f.default for f in g.structs["CalibrationGrid"].fields}
    assert grid["alloc"] == {"lo": -3.0, "hi": 4.0, "n": 141}
