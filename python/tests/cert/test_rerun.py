"""`verify-cert --rerun`: bit-identical re-execution of certified runs (SPEC 7.2 determinism rules)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cert.helpers import failed, load, make_cert, reseal, status, verify, write

pytestmark = pytest.mark.requires_core


def test_rerun_reproduces_every_output(work: Path, tmp_path: Path) -> None:
    p = write(make_cert(), work.parent / "cert.json")
    keep = tmp_path / "rerun_out"
    ver = verify(p, rerun=True, work=keep)
    assert ver.status == "pass", [(c.id, c.summary, c.details) for c in ver.checks if c.status == "fail"]
    assert ver.rerun is True
    assert [(r.label, r.rerun) for r in ver.runs] == [
        ("replay", "identical"),
        ("referee", "identical"),
        ("dq", "identical"),
    ]
    for label in ("replay", "referee", "dq"):
        assert status(ver, f"rerun:{label}") == "pass"
    for cid in ("c1", "c2", "c3"):
        assert status(ver, f"rerun_claim:{cid}") == "pass"
    # the re-executed outputs are kept in --work, next to (never inside) the certified runs
    assert sorted(x.name for x in (keep / "replay").iterdir()) == sorted(x.name for x in (work / "replay").iterdir())
    assert (work / "replay").exists()


def test_rerun_catches_fabricated_results(work: Path) -> None:
    """Outputs edited before certification hash fine and bind to their inputs, but do not reproduce."""
    s = json.loads((work / "replay/summary.json").read_text())
    real = s["shadows"]["n_filled"]
    assert real < s["shadows"]["n"]
    s["shadows"]["n_filled"] = s["shadows"]["n"]
    (work / "replay/summary.json").write_text(json.dumps(s, indent=2))
    p = write(make_cert(("replay",), claims=["{replay#/shadows/n_filled} fills"]), work.parent / "cert.json")
    assert verify(p).status == "pass"  # nothing on disk changed since certification
    ver = verify(p, rerun=True)
    assert ver.status == "fail"
    assert sorted(failed(ver)) == ["rerun:replay", "rerun_claim:c1"]
    run = ver.runs[0]
    assert run.rerun == "differs" and run.differing == ["summary.json"]
    detail = next(c for c in ver.checks if c.id == "rerun_claim:c1").details[0]
    assert detail == f"replay#/shadows/n_filled: certified {s['shadows']['n']}, now {real}"


def test_rerun_ignores_timings_and_parquet_must_match(work: Path) -> None:
    s = json.loads((work / "replay/summary.json").read_text())
    s["timings"] = {"wall_s": 1234.5, "records_per_s": 1.0}
    (work / "replay/summary.json").write_text(json.dumps(s))
    p = write(make_cert(("replay",), claims=[]), work.parent / "cert.json")
    assert verify(p, rerun=True).status == "pass"
    # a byte appended to a Parquet output is a difference
    bbo = work / "replay" / "bbo.parquet"
    bbo.write_bytes(bbo.read_bytes() + b"\0")
    p = write(make_cert(("replay",), claims=[]), work.parent / "cert2.json")
    ver = verify(p, rerun=True)
    assert ver.runs[0].differing == ["bbo.parquet"]


def test_rerun_failure_is_reported_not_raised(work: Path) -> None:
    p = write(make_cert(("referee",), claims=[]), work.parent / "cert.json")
    shutil.copytree(work, work.parent / "broken")
    (work.parent / "broken" / "fx" / "schedule.parquet").unlink()
    ver = verify(p, rerun=True, base=work.parent / "broken")
    assert status(ver, "rerun:referee") == "fail"
    assert "re-execution failed" in next(c.summary for c in ver.checks if c.id == "rerun:referee")
    assert ver.runs[0].rerun == "failed"


def test_rerun_with_extra_output_files_warns(work: Path) -> None:
    """A file the run writes but the certificate does not list (deleted before certifying) is reported."""
    (work / "replay" / "trades.parquet").unlink()
    p = write(make_cert(("replay",), claims=[]), work.parent / "cert.json")
    ver = verify(p, rerun=True)
    assert ver.status == "pass" and status(ver, "rerun:replay") == "warn"


def test_rerun_warns_on_version_mismatch(work: Path) -> None:
    doc = make_cert(("referee",), claims=[]).model_dump(mode="json")
    doc["environment"]["queuetruth"] = "0.0.0"
    doc["environment"]["machine"] = "vax"
    doc["environment"]["packages"]["pyarrow"] = "1.0.0"
    p = write(reseal(doc), work.parent / "old.json")
    ver = verify(p, rerun=True)
    assert ver.status == "pass"
    assert any("re-executed with" in w for w in ver.warnings)
    assert any(" vax, re-executed on " in w and "4 ulp" in w for w in ver.warnings)
    assert any("pyarrow 1.0.0 -> " in w for w in ver.warnings)
    assert load(p)["environment"]["queuetruth"] == "0.0.0"
