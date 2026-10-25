"""Every run kind certifies and re-executes bit for bit.

`test_calibrate_live_*` runs by default: `calibrate-live` adds `calibration_source` to its spec echo after computing
`run_id`, so `certify` must drop it before recomputing the run id and before re-executing. The `integration` test
runs the synthetic pipeline of the end-to-end tests (one run of each of the eight kinds) through the command line and
is the measurement quoted in docs/how-to/certify-a-result.md (`pytest -m integration python/tests/cert`).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cert.helpers import failed, schema_valid, status, write
from queuetruth.cert.certify import certify
from queuetruth.cert.runs import executable_spec
from queuetruth.cert.verify import verify_certificate

REPO = Path(__file__).resolve().parents[3]
SYNTH = REPO / "tests" / "fixtures" / "synth_small"
REFEREE = REPO / "tests" / "fixtures" / "referee" / "02_level_shrink_caps_queue"

pytestmark = pytest.mark.requires_core


def test_executable_spec_drops_only_echo_annotations() -> None:
    spec = {"dataset": {"path": "d"}, "calibration_source": "live", "seed": 0}
    assert executable_spec("calibrate_live", spec) == {"dataset": {"path": "d"}, "seed": 0}
    assert executable_spec("calibrate", spec) is spec
    assert spec["calibration_source"] == "live"  # not mutated


def test_calibrate_live_certifies_and_reruns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from queuetruth import api

    if not (SYNTH / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    shutil.copytree(SYNTH, tmp_path / "synth")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    with contextlib.chdir(tmp_path):
        api.calibrate_live(
            {"dataset": {"path": "synth"}, "live": {"path": "synth/own_orders/live"}, "family": "power3", "out": "live"}
        )
        doc = json.loads(Path("live/calibration.json").read_text())
        assert doc["kind"] == "live" and doc["spec"]["calibration_source"] == "live"
        cert = certify(["live"], claims=["fitted n = {live#/estimate}"])
        run = cert.runs[0]
        assert (run.kind, run.binding, run.run_id) == ("calibrate_live", "verified", doc["run_id"])
        assert run.spec == doc["spec"]  # the certificate keeps the echo as written
        assert "calibration_source" not in run.reproduce.python
        schema_valid(cert.model_dump(mode="json"))
        p = write(cert, tmp_path / "cert.json")
        ver = verify_certificate(p, rerun=True)
    assert ver.status == "pass", failed(ver)
    assert status(ver, "binding:live") == "pass"
    assert status(ver, "rerun:live") == "pass" and status(ver, "rerun_claim:c1") == "pass"
    assert ver.runs[0].rerun == "identical"


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    return subprocess.run(
        [sys.executable, "-m", "queuetruth.cli", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.integration
def test_every_kind_reruns_bit_for_bit(tmp_path: Path) -> None:
    """The synthetic pipeline of tests/e2e (same arguments, run from one directory with relative paths)."""
    if not (SYNTH / "manifest.json").exists():
        pytest.skip("tests/fixtures/synth_small is missing (make fixtures)")
    live = str(SYNTH / "own_orders" / "live")
    ref = {
        k: str(REFEREE / v) for k, v in (("d", "dataset"), ("s", "schedule.parquet"), ("f", "expected_fills.parquet"))
    }
    steps = [
        ["ingest", "--format", "parquet", "--in", str(SYNTH), "--venue", "cme", "--out", "ds"],
        "shadows sample --data ds --n 160 --out shadows.parquet --seed 3 --lifetime 60s".split(),
        "replay --data ds --out replay --shadows shadows.parquet --rank-oracle --parity-trades".split(),
        "zoo --data ds --shadows shadows.parquet --out zoo --models all --bootstrap-b 20 --h-max 1m".split(),
        (
            "calibrate alloc --family power3 --data ds --shadows shadows.parquet --out cal --train 2026-06-15 "
            "--test 2026-06-16 --bootstrap-b 10"
        ).split(),
        ["calibrate-live", "--data", "ds", "--live", live, "--family", "power3", "--out", "live"],
        "features --data ds --shadows shadows.parquet --out feat".split(),
        "fit-hazard --features feat --out model --bootstrap-b 20".split(),
        ["score", "--live", live, *"--sim exact --data ds --out score --bootstrap-b 50 --perm-b 50".split()],
        ["referee", "run", "--data", ref["d"], "--schedule", ref["s"], "--sim-fills", ref["f"], "--out", "referee"],
        "dq --data ds --out dq".split(),
    ]
    for argv in steps:
        p = _cli(tmp_path, "--format", "json", *argv)
        assert p.returncode in (0, 1), f"{argv[0]}: exit {p.returncode}\n{p.stdout[-1000:]}\n{p.stderr[-2000:]}"
    runs = ["replay", "zoo", "cal", "live", "model", "score", "referee", "dq"]
    p = _cli(tmp_path, "--format", "json", "certify", *runs, "--out", "cert.json", "--claim", "zoo {zoo#/run_id}")
    assert p.returncode == 0, p.stderr[-2000:]
    cert = json.loads((tmp_path / "cert.json").read_text())
    kinds = "replay zoo calibrate calibrate_live fit_hazard score referee dq".split()
    assert [r["kind"] for r in cert["runs"]] == kinds
    assert [r["binding"] for r in cert["runs"]] == ["verified"] * 6 + ["recomputed"] * 2
    p = _cli(tmp_path, "--format", "json", "verify-cert", "cert.json", "--rerun")
    ver = json.loads(p.stdout)
    assert p.returncode == 0 and ver["status"] == "pass", [c for c in ver["checks"] if c["status"] == "fail"]
    assert {r["label"]: r["rerun"] for r in ver["runs"]} == dict.fromkeys(runs, "identical")
    outputs = {r["label"]: len(r["outputs"]) for r in cert["runs"]}
    assert outputs == dict(zip(runs, [9, 6, 8, 2, 3, 7, 1, 2], strict=True))
