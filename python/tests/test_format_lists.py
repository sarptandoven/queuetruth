"""Drift tests: every hand-kept list of source formats or live venues equals its source.

The source format registry is Rust (`qt_cli::formats`, `_core.formats()`); the live venues come from the Python model
(`LiveImportSpec.venue`), and the Rust copy and CLI choices are generated from it by `make schemas`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from queuetruth.report.load import VENDOR_FORMATS
from queuetruth.schemas import specs as sp

ROOT = Path(__file__).resolve().parents[2]


def _formats() -> list[dict]:
    from queuetruth import _core

    return json.loads(_core.formats())


@pytest.mark.requires_core
def test_ingest_spec_formats_match_the_registry() -> None:
    literal = get_args(sp.IngestSpec.model_fields["format"].annotation)
    assert sorted(literal) == sorted(f["name"] for f in _formats())
    assert len(set(literal)) == len(literal)


@pytest.mark.requires_core
def test_vendor_formats_match_the_registry() -> None:
    assert {f["source_format"] for f in _formats() if f["vendor"]} == VENDOR_FORMATS


@pytest.mark.requires_core
def test_cli_format_choices_match_the_registry() -> None:
    from queuetruth import _core

    names = [f["cli_name"] for f in _formats()]
    assert names == [f["name"].replace("_", "-") for f in _formats()]
    for name in names:
        argv = ["queuetruth", "ingest", "--format", name, "--in", "x", "--venue", "generic", "--out", "o"]
        cmd = json.loads(_core.cli_parse(argv))
        assert cmd["args"]["format"] == name.replace("-", "_")


@pytest.mark.requires_core
def test_registry_metadata_is_complete() -> None:
    keys = {
        "name",
        "cli_name",
        "kind",
        "source_format",
        "vendor",
        "rule_set",
        "generic_rule_set",
        "default_source_venue",
        "options",
        "engine",
        "summary",
    }
    for f in _formats():
        assert set(f) == keys, f["name"]
        assert f["kind"] in {"mbo", "l2"}
        assert f["engine"] in {"qt_ingest", "qt_crypto"}
        assert f["summary"]


def test_live_venue_rust_copy_is_generated_from_the_model() -> None:
    from queuetruth.schemas import export

    venues = get_args(sp.LiveImportSpec.model_fields["venue"].annotation)
    rs = (ROOT / export.LIVE_VENUE_RS).read_text(encoding="utf-8")
    assert rs == export.live_venue_rs(), "run `make schemas`"
    for v in venues:
        assert f'#[serde(rename = "{v}")]' in rs
        assert f'"{v.replace("_", "-")}",' in rs
    schema = json.loads((ROOT / "schemas/json/qt.live_import_spec.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["venue"]["enum"] == list(venues)
