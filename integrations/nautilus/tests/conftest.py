"""Shared fixtures. `cal_touch.json` and `cal_queue_refused.json` are the real RD-9 calibrations of
docs/RESULTS.md (fitted on 2019-12-30 blocks 0-3/5, held out on 2020-01-30) with local paths replaced;
`*.export.txt` are their `queuetruth export nautilus` texts. `cal_touch_latency.json` is the touch calibration with
a synthetic latency fit added (run id `000000000000a7e1`), its export text written by queuetruth 0.1.1."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
if importlib.util.find_spec("queuetruth_nautilus") is None:  # run from a checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def load_doc(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def touch_doc() -> dict[str, Any]:
    return load_doc("cal_touch.json")


@pytest.fixture
def queue_doc() -> dict[str, Any]:
    return load_doc("cal_queue_refused.json")


@pytest.fixture
def latency_doc() -> dict[str, Any]:
    return load_doc("cal_touch_latency.json")


def variant(doc: dict[str, Any], **changes: Any) -> dict[str, Any]:
    d = copy.deepcopy(doc)
    d.update(changes)
    return d


def write(tmp_path: Path, doc: dict[str, Any], name: str = "calibration.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def exportable(doc: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A variant with an `exportable` structured verdict (for engine tests of crafted parameters)."""
    d = variant(doc, **changes)
    d["trust"] = {"verdict": "exportable", "reasons": [], "flags": [], "beats_default": {}}
    return d
