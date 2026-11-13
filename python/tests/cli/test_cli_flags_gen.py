"""The flags generated from the spec schemas (`crates/cli/gen_flags.py` -> `crates/cli/src/flags_gen.rs`): every field
of each generated command's spec is a flag or has a reason not to be one, each flag's help is its field's schema
description unless an override says otherwise, the committed file is current, and a new spec field becomes a flag
without a hand edit."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from queuetruth.schemas.specs import SPEC_MODELS


@pytest.fixture(scope="module")
def gen(repo_root: Path) -> ModuleType:
    path = repo_root / "crates" / "cli" / "gen_flags.py"
    spec = importlib.util.spec_from_file_location("gen_flags", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_flags"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_every_spec_field_is_a_flag_or_says_why_not(gen: ModuleType) -> None:
    for path, cmd in gen.COMMANDS.items():
        fields = set(SPEC_MODELS[cmd.schema].model_fields)
        flags = {f["to"] for f in gen.flags(path, cmd)}
        assert flags | set(cmd.not_flags) == fields, path
        assert not flags & set(cmd.not_flags), path
        assert all(reason.strip() for reason in cmd.not_flags.values()), path


def test_help_comes_from_the_schema(gen: ModuleType, repo_root: Path) -> None:
    for path, cmd in gen.COMMANDS.items():
        schema = json.loads((repo_root / "schemas" / "json" / f"{cmd.schema}.schema.json").read_text())
        for f in gen.flags(path, cmd):
            o = cmd.overrides.get(f["to"])
            if o is None or o.help is None:
                assert f["help"] == gen.phrase(schema["properties"][f["to"]]["description"]), (path, f)


def test_committed_file_is_current(gen: ModuleType) -> None:
    assert gen.OUT.read_text(encoding="utf-8") == gen.generate(), "run make generate"


def test_a_new_field_becomes_a_flag_and_a_field_without_a_rule_stops_the_generator(
    gen: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ("shadows", "mirror-real")
    cmd = gen.COMMANDS[path]
    schema = json.loads((gen.SCHEMAS / f"{cmd.schema}.schema.json").read_text())
    schema["properties"]["max_age_ns"] = {"type": "integer", "default": 0, "description": "Oldest real order."}
    (tmp_path / f"{cmd.schema}.schema.json").write_text(json.dumps(schema))
    monkeypatch.setattr(gen, "SCHEMAS", tmp_path)
    new = gen.flags(path, cmd)[-1]
    assert new == {
        "long": "max-age-ns",
        "kind": "Dur",
        "to": "max_age_ns",
        "required": False,
        "help": "oldest real order",
    }
    schema["properties"]["weights"] = {"type": "object", "description": "A map."}
    (tmp_path / f"{cmd.schema}.schema.json").write_text(json.dumps(schema))
    with pytest.raises(SystemExit, match=r"'weights'.*no flag rule"):
        gen.flags(path, cmd)
    del schema["properties"]["weights"]
    schema["properties"]["max_age_ns"].pop("description")
    (tmp_path / f"{cmd.schema}.schema.json").write_text(json.dumps(schema))
    with pytest.raises(SystemExit, match=r"'max_age_ns'.*no description"):
        gen.flags(path, cmd)


def test_rules(gen: ModuleType) -> None:
    defs = {"PathRef": {}, "DatasetIdRef": {}, "DatasetRef": {"anyOf": [{"$ref": "#/$defs/PathRef"}]}}
    assert gen.kind_of("x", {"anyOf": [{"$ref": "#/$defs/PathRef"}, {"type": "null"}]}, defs) == "Path"
    assert gen.kind_of("x_ns", {"type": "integer"}, defs) == "Dur"
    assert gen.kind_of("x", {"type": "integer"}, defs) == "Int"
    assert gen.kind_of("x", {"type": "number"}, defs) == "Num"
    assert gen.kind_of("x", {"type": "boolean", "default": True}, defs) == "NoFlag"
    assert gen.kind_of("x", {"type": "boolean", "default": False}, defs) == "Flag"
    assert gen.kind_of("x", {"enum": ["a_b", "c"], "type": "string"}, defs) == 'Enum(&["a-b", "c"])'
    assert gen.kind_of("x", {"type": "object"}, defs) is None
    assert gen.phrase("Number of shadows. More text.") == "number of shadows"
    assert gen.phrase("BBO output.") == "BBO output"
