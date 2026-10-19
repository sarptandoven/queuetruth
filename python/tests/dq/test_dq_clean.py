"""`queuetruth dq` on clean data: every check passes or is skipped with a reason, the report validates against its
committed JSON Schema, the HTML is self-contained and deterministic, and the spec is validated."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from dq.helpers import FIXTURES, check
from queuetruth.dq import CHECK_IDS, HTML_NAME, JSON_NAME, json_schemas, run
from queuetruth.dq.model import gate, worst
from queuetruth.errors import QtError

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "json"


def test_committed_json_schemas_match_the_models() -> None:
    for name, text in json_schemas().items():
        assert (SCHEMA_DIR / name).read_text(encoding="utf-8") == text, f"{name} is stale: run make schemas"


@pytest.mark.requires_core
@pytest.mark.parametrize("name", ["synth_small_nasdaq", "synth_small"])
def test_clean_fixture_passes(name: str, tmp_path: Path) -> None:
    rep = run({"dataset": {"path": str(FIXTURES / name)}, "out": str(tmp_path / "dq")})
    assert rep.status == "pass", [(c.id, c.status, c.summary) for c in rep.checks if c.status != "pass"]
    assert not rep.gate_failed
    assert [c.id for c in rep.checks] == CHECK_IDS
    skipped = {c.id for c in rep.checks if c.status == "skip"}
    assert skipped == {"cross_source", "rate_drift"}
    assert all(c.summary.startswith("not run: ") for c in rep.checks if c.status == "skip")
    # the book checks really ran on the replay
    assert check(rep, "crossed_book").metrics["events"] > 100_000
    assert check(rep, "trade_parity").metrics["trades_compared"] > 10_000
    assert check(rep, "stale_book").metrics["prints_checked_against_book"] > 10_000
    assert check(rep, "vintage").metrics["dataset_id_ok"] is True
    if name == "synth_small":  # CME: snapshot runs begin with a clear, sequence resets at snapshots are no gaps
        snap = check(rep, "snapshots").metrics
        assert snap["snapshot_runs"] > 0 and snap["snapshot_runs"] == snap["clears"]
        assert check(rep, "sequence").metrics["gaps"] == 0


@pytest.mark.requires_core
def test_report_files_schema_and_determinism(tmp_path: Path) -> None:
    spec = {"dataset": {"path": str(FIXTURES / "synth_small_nasdaq")}, "out": str(tmp_path / "a")}
    rep = run(spec)
    doc = json.loads((tmp_path / "a" / JSON_NAME).read_text())
    jsonschema.validate(doc, json.loads(json_schemas()["qt.dq_report.schema.json"]))
    assert doc["schema"] == "qt.dq_report" and doc["schema_version"] == 1
    assert doc == rep.model_dump(mode="json", by_alias=True)
    html = (tmp_path / "a" / HTML_NAME).read_bytes()
    assert [f.name for f in rep.files] == [HTML_NAME]
    assert rep.files[0].sha256 == hashlib.sha256(html).hexdigest()
    text = html.decode()
    assert text.startswith("<!doctype html>") and "prefers-color-scheme" in text
    for ref in ("<link", 'src="http', "src='http", 'href="http', "url(http", "@import"):
        assert ref not in text  # no external requests
    for cid in CHECK_IDS:
        assert f'id="check-{cid}"' in text
    # same inputs, same bytes (only the output path differs, and it is not written into the page)
    run({**spec, "out": str(tmp_path / "b")})
    assert (tmp_path / "b" / HTML_NAME).read_bytes() == html


def test_without_replay_book_checks_skip(tmp_path: Path) -> None:
    rep = run({"dataset": {"path": str(FIXTURES / "synth_small_nasdaq")}, "replay": False})
    st = {c.id: c.status for c in rep.checks}
    assert st["crossed_book"] == "skip" and st["trade_parity"] == "skip"
    assert "--no-replay" in check(rep, "crossed_book").summary
    # record checks still run, the stale-book quiet-interval part included
    assert st["sequence"] == st["orphan_refs"] == st["stale_book"] == st["timestamps"] == "pass"
    assert check(rep, "dq_counters").metrics["origin"] == "ingest"
    assert rep.files == []  # no out: nothing written


def test_checks_subset_and_spec_errors(tmp_path: Path) -> None:
    rep = run({"dataset": {"path": str(FIXTURES / "synth_small_nasdaq")}, "checks": ["vintage", "symbology"]})
    assert [c.id for c in rep.checks] == ["symbology", "vintage"]  # report order, not request order
    with pytest.raises(QtError) as e:
        run({"dataset": {"path": str(FIXTURES / "synth_small_nasdaq")}, "checks": ["nope"]})
    assert e.value.code == "QT-CONFIG-001" and "nope" in e.value.message
    with pytest.raises(QtError) as e:
        run({"dataset": {"path": str(FIXTURES / "synth_small_nasdaq")}, "fail_on": "sometimes"})
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e:
        run({"dataset": {"path": str(tmp_path)}})
    assert e.value.code == "QT-INPUT-001"


def test_status_and_gate_rules() -> None:
    assert worst([]) == "pass"
    assert worst(["skip", "skip"]) == "pass"
    assert worst(["pass", "warn", "skip"]) == "warn"
    assert worst(["warn", "fail"]) == "fail"
    assert gate("warn", "warn") and gate("fail", "warn") and not gate("pass", "warn")
    assert not gate("warn", "fail") and gate("fail", "fail")
    assert not gate("fail", "never")
