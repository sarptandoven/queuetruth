"""SPEC 7.2 cross-platform tolerance of re-executed outputs (`queuetruth.cert.tolerance`)."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cert.helpers import failed, make_cert, status, verify, write
from queuetruth.cert.tolerance import equivalent, json_close, ulp_distance


def test_ulp_distance() -> None:
    assert ulp_distance(1.0, 1.0) == 0
    assert ulp_distance(1.0, math.nextafter(1.0, 2.0)) == 1
    x = 0.1
    for _ in range(4):
        x = math.nextafter(x, 1.0)
    assert ulp_distance(0.1, x) == 4
    assert ulp_distance(-0.0, 0.0) == 0
    assert ulp_distance(-5e-324, 5e-324) == 2  # across zero


def test_json_close() -> None:
    four = 0.1
    for _ in range(4):
        four = math.nextafter(four, 1.0)
    five = math.nextafter(four, 1.0)
    assert json_close({"a": [1, 0.1, "s", None, True]}, {"a": [1, four, "s", None, True]})
    assert not json_close({"a": 0.1}, {"a": five})
    assert not json_close({"a": 1}, {"a": 1.0})  # integers stay exact, types too
    assert not json_close({"a": 1}, {"a": 2})
    assert not json_close({"a": True}, {"a": 1})
    assert not json_close({"a": 0.1}, {"a": 0.1, "b": 1})
    assert json_close([float("nan")], [float("nan")])
    assert not json_close([float("inf")], [1.7976931348623157e308])


def _parquet(path: Path, ints: list[int], floats: list[float | None], f32: list[float]) -> Path:
    table = pa.table(
        {
            "i": pa.array(ints, pa.int64()),
            "f": pa.array(floats, pa.float64()),
            "g": pa.array(f32, pa.float32()),
        }
    ).replace_schema_metadata({b"qt.schema": b"x"})
    pq.write_table(table, path)
    return path


def test_parquet_equivalence(tmp_path: Path) -> None:
    a = _parquet(tmp_path / "a.parquet", [1, 2], [0.1, None], [1.5, 2.5])
    up = float(np.nextafter(np.float32(1.5), np.float32(2.0)))
    b = _parquet(tmp_path / "b.parquet", [1, 2], [math.nextafter(0.1, 1.0), None], [up, 2.5])
    assert a.read_bytes() != b.read_bytes()
    assert equivalent(a, b)
    assert not equivalent(a, _parquet(tmp_path / "c.parquet", [1, 3], [0.1, None], [1.5, 2.5]))  # integer column
    assert not equivalent(a, _parquet(tmp_path / "d.parquet", [1, 2], [0.1, 0.0], [1.5, 2.5]))  # null position
    assert not equivalent(a, _parquet(tmp_path / "e.parquet", [1, 2], [0.2, None], [1.5, 2.5]))  # far apart
    html = tmp_path / "a.html"
    html.write_text("x")
    assert not equivalent(html, html.with_name("b.html"))  # other files: bytes only


@pytest.mark.requires_core
def test_rerun_reports_equivalent_outputs(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-execution whose JSON differs by 1 ulp in one value is `equivalent` (a warning), never `identical`; one
    that differs in an integer fails."""
    s = json.loads((work / "replay/summary.json").read_text())
    s["probe"] = 0.1
    (work / "replay/summary.json").write_text(json.dumps(s, indent=2))
    p = write(make_cert(("replay",), claims=["probe {replay#/probe}"]), work.parent / "cert.json")
    from queuetruth.cert import verify as qverify

    def fake(value: Any) -> Any:
        def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            out = Path(json.loads(kw["input"])["out"])
            shutil.copytree(work / "replay", out)
            doc = json.loads((out / "summary.json").read_text())
            doc["probe"] = value
            (out / "summary.json").write_text(json.dumps(doc))
            return subprocess.CompletedProcess(cmd, 0, "{}", "")

        return run

    monkeypatch.setattr(qverify.subprocess, "run", fake(math.nextafter(0.1, 1.0)))
    ver = verify(p, rerun=True)
    assert ver.status == "pass", failed(ver)
    assert status(ver, "rerun:replay") == "warn" and status(ver, "rerun_claim:c1") == "warn"
    assert ver.runs[0].rerun == "equivalent" and ver.runs[0].within_tolerance == ["summary.json"]
    monkeypatch.setattr(qverify.subprocess, "run", fake(1))
    ver = verify(p, rerun=True)
    assert sorted(failed(ver)) == ["rerun:replay", "rerun_claim:c1"] and ver.runs[0].rerun == "differs"
