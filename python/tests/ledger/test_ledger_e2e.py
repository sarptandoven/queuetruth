"""End to end: real CLI runs on the synthetic fixture are recorded with their exact `run_id`s, data hashes and code
version; the ledger commands read them; tampering is caught; and recording changes no output file, no summary and
no `run_id` (the same pipeline with `QT_LEDGER=off` is byte-identical, JSON compared without `timings`)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from queuetruth import api, cli
from queuetruth import ledger as L
from queuetruth.schemas.arrow import ENGINE_VERSION
from queuetruth.schemas.runid import run_id

pytestmark = pytest.mark.requires_core

REPO = Path(__file__).resolve().parents[3]
SYNTH = REPO / "tests" / "fixtures" / "synth_small"
LIVE = SYNTH / "own_orders" / "live"
STAGES = ["shadows.parquet", "zoo", "cal", "feat", "model", "score", "clive"]


def _steps() -> list[tuple[str, list[str]]]:
    ds = str(SYNTH)
    return [
        ("shadows-sample", ["shadows", "sample", "--data", ds, "--n", "60", "--out", "shadows.parquet", "--seed", "3",
                            "--lifetime", "60s"]),
        ("zoo", ["zoo", "--data", ds, "--shadows", "shadows.parquet", "--out", "zoo", "--bootstrap-b", "20",
                 "--h-max", "1m"]),
        ("calibrate", ["calibrate", "alloc", "--family", "power3", "--data", ds, "--shadows", "shadows.parquet",
                       "--out", "cal", "--train", "2026-06-15", "--test", "2026-06-16", "--bootstrap-b", "10"]),
        ("features", ["features", "--data", ds, "--shadows", "shadows.parquet", "--out", "feat"]),
        ("fit-hazard", ["fit-hazard", "--features", "feat", "--out", "model", "--bootstrap-b", "20"]),
        ("score", ["score", "--live", str(LIVE), "--sim", "exact", "--data", ds, "--out", "score", "--bootstrap-b",
                   "50", "--perm-b", "50"]),
        ("calibrate-live", ["calibrate-live", "--data", ds, "--live", str(LIVE), "--family", "power3", "--out",
                            "clive", "--train", "2026-06-15", "--test", "2026-06-16", "--bootstrap-b", "10"]),
    ]  # fmt: skip


def _run_capture(argv: list[str]) -> tuple[int, str, str]:
    """`queuetruth.cli.main(argv)` in process: (exit code, stdout, stderr)."""
    o, e = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
        code = cli.main(argv)
    return code, o.getvalue(), e.getvalue()


def _strip(doc: Any) -> Any:
    if isinstance(doc, dict):
        return {k: _strip(v) for k, v in doc.items() if k != "timings"}
    if isinstance(doc, list):
        return [_strip(v) for v in doc]
    return doc


def _digests(root: Path) -> dict[str, str]:
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    out = {}
    for p in files:
        data = p.read_bytes()
        if p.suffix == ".json":
            data = json.dumps(_strip(json.loads(data)), sort_keys=True).encode()
        out[p.name if p == root else p.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    return out


def _pipeline(work: Path, mp: pytest.MonkeyPatch) -> dict[str, Any]:
    work.mkdir()
    mp.chdir(work)
    docs = {}
    for kind, argv in _steps():
        code, out, err = _run_capture(["--format", "json", *argv])
        assert code == 0, (argv, out, err[-2000:])
        assert "ledger" not in err, err  # recording is silent when it works
        docs[kind] = json.loads(out)
    return docs


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The pipeline with the ledger on (QT_LEDGER_DIR) and off (QT_LEDGER=off), in two directories."""
    base = tmp_path_factory.mktemp("ledger_e2e")
    out: dict[str, Any] = {"base": base}
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("QT_LEDGER", raising=False)
        mp.setenv("QT_LEDGER_DIR", str(base / "ledger"))
        mp.setenv("QT_LEDGER_STUDY", "e2e")
        out["on"] = _pipeline(base / "on", mp)
        mp.setenv("QT_LEDGER", "off")
        mp.setenv("QT_LEDGER_DIR", str(base / "ledger_off"))
        out["off"] = _pipeline(base / "off", mp)
    return out


def _ledger(runs: dict[str, Any]) -> L.Ledger:
    return L.Ledger(runs["base"] / "ledger")


def test_every_tracked_run_is_recorded_once(runs: dict[str, Any]) -> None:
    recs = _ledger(runs).records()
    assert recs[0].kind == "genesis"
    kinds = [r.kind for r in recs[1:]]
    # features is not a tracked entry point; the zoo runs inside calibrate-live and score are part of those trials
    assert kinds == ["shadows-sample", "zoo", "calibrate", "fit-hazard", "score", "calibrate-live"]
    assert all(r.status == "ok" and r.study == "e2e" and r.started_at_ns <= r.recorded_at_ns for r in recs[1:])
    assert not (runs["base"] / "ledger_off").exists()


def test_records_carry_the_runs_exact_identities(runs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    on = runs["on"]
    by = {r.kind: r for r in _ledger(runs).records()[1:]}
    for kind in ("zoo", "calibrate", "score", "calibrate-live"):
        assert by[kind].run_id == on[kind]["run_id"] == by[kind].trial_id, kind
    assert by["fit-hazard"].run_id == on["fit-hazard"]["model_id"].rsplit(":", 1)[1]
    # shadows sample reports no run_id: the ledger's equals the Rust core's for the echoed spec
    monkeypatch.chdir(runs["base"] / "on")
    assert by["shadows-sample"].run_id == api.run_id(on["shadows-sample"]["spec"])
    dataset_id = json.loads((SYNTH / "manifest.json").read_text())["dataset_id"]
    for kind in ("shadows-sample", "zoo", "calibrate", "score", "calibrate-live"):
        assert by[kind].inputs["dataset"] == dataset_id, kind
        assert by[kind].dataset_hash is not None and L.n_trials(dataset=dataset_id[:12], ledger=_ledger(runs)) == 5
    sh = hashlib.sha256((runs["base"] / "on" / "shadows.parquet").read_bytes()).hexdigest()
    assert by["zoo"].inputs["shadows"] == by["calibrate"].inputs["shadows"] == sh
    for kind, rec in by.items():  # the recorded inputs reproduce each run_id (SPEC 7.3)
        echo = {k: v for k, v in on[kind]["spec"].items() if k != "calibration_source"}  # calibrate-live's tag
        lookup = {json.dumps(v, sort_keys=True): rec.inputs[k] for k, v in _refs(echo)}
        assert run_id(echo, ENGINE_VERSION, lambda r, lk=lookup: lk[json.dumps(r, sort_keys=True)]) == rec.run_id
        assert rec.spec_hash == L.spec_hash(kind, echo)
        assert rec.code is not None and rec.code.engine == ENGINE_VERSION and rec.code.core == ENGINE_VERSION
    assert by["zoo"].metrics["n_shadows"] == on["zoo"]["n_shadows"] == 60
    assert by["calibrate"].name == "power3" and by["calibrate"].metrics["estimate"] == on["calibrate"]["estimate"]
    assert by["score"].metrics["n_matched"] == on["score"]["n_matched"]


def _refs(node: Any, path: tuple[str, ...] = ()) -> list[tuple[str, dict[str, Any]]]:
    from queuetruth.schemas.runid import is_ref

    if isinstance(node, dict):
        if is_ref(node):
            return [(".".join(path), node)]
        return [x for k in sorted(node) if k not in ("out", "threads") for x in _refs(node[k], (*path, k))]
    if isinstance(node, list):
        return [x for i, v in enumerate(node) for x in _refs(v, (*path, str(i)))]
    return []


def test_recording_changes_no_output_and_no_run_id(runs: dict[str, Any]) -> None:
    on, off = runs["on"], runs["off"]
    assert _strip(on) == _strip(off)  # every stdout summary, run_id included
    for stage in STAGES:
        a, b = _digests(runs["base"] / "on" / stage), _digests(runs["base"] / "off" / stage)
        assert a and a == b, stage
    for kind in ("zoo", "calibrate", "score", "calibrate-live"):
        assert on[kind]["run_id"] == off[kind]["run_id"]
    # nothing ledger-related lands next to the outputs
    names = {p.name for p in (runs["base"] / "on").rglob("*")}
    assert not any("ledger" in n or n == ".queuetruth" for n in names)


def test_ledger_commands_on_the_real_ledger(runs: dict[str, Any], tmp_path: Path) -> None:
    led = str(runs["base"] / "ledger")
    code, out, _ = _run_capture(["--format", "json", "ledger", "stats", "--ledger", led])
    st = json.loads(out)
    assert code == 0 and (st["n_trials"], st["n_records"], st["n_errors"]) == (6, 6, 0)
    assert set(st["by_kind"]) == {"zoo", "calibrate", "calibrate-live", "fit-hazard", "score", "shadows-sample"}
    code, out, _ = _run_capture(["--format", "json", "ledger", "list", "--ledger", led, "--kind", "zoo"])
    assert code == 0 and [r["run_id"] for r in json.loads(out)["records"]] == [runs["on"]["zoo"]["run_id"]]
    code, out, _ = _run_capture(["ledger", "verify", "--ledger", led])
    assert code == 0 and out.startswith("ledger OK: 7 record(s)")
    code, out, _ = _run_capture(["--format", "json", "ledger", "export", "--ledger", led, "--out",
                                 str(tmp_path / "l.parquet")])  # fmt: skip
    import pyarrow.parquet as pq

    t = pq.read_table(tmp_path / "l.parquet")
    assert code == 0 and t.num_rows == 6 and t.column("run_id").to_pylist()[1] == runs["on"]["zoo"]["run_id"]
    sel = L.selection_inputs(ledger=led, kinds=["zoo", "calibrate"], allow_mixed=True)
    assert sel["n_trials"] == 2 and sel["n_with_series"] == 0  # runs carry metrics, not series
    zoo = L.list_records(kinds=["zoo"], ledger=led)[0]
    assert zoo.metrics["best_model"] in runs["on"]["zoo"]["models"]  # the zoo's own selection is recorded
    assert sum(k.startswith("brier_mean:") for k in zoo.metrics) == len(runs["on"]["zoo"]["leaderboard"])
    # the installed entry point (a fresh interpreter) reads the same ledger
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "python"), os.environ.get("PYTHONPATH", "")])}
    p = subprocess.run(
        [sys.executable, "-m", "queuetruth.cli", "--format", "json", "ledger", "verify", "--ledger", led],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )  # fmt: skip
    assert p.returncode == 0 and json.loads(p.stdout)["ok"] is True, p.stderr


def test_tampering_with_a_real_record_is_caught(runs: dict[str, Any], tmp_path: Path) -> None:
    copy = tmp_path / "ledger.jsonl"
    shutil.copy(runs["base"] / "ledger" / "ledger.jsonl", copy)
    data = copy.read_bytes()
    assert data.count(b'"n_shadows":60') == 1
    copy.write_bytes(data.replace(b'"n_shadows":60', b'"n_shadows":61'))
    code, out, _ = _run_capture(["--format", "json", "ledger", "verify", "--ledger", str(copy)])
    doc = json.loads(out)
    assert code == 2 and doc["code"] == "QT-LEDGER-001"
    assert [(p["line"], p["reason"]) for p in doc["details"]["verification"]["problems"]] == [(3, "hash_mismatch")]
    code, out, _ = _run_capture(["--format", "json", "ledger", "stats", "--ledger", str(copy)])
    assert code == 2 and json.loads(out)["code"] == "QT-LEDGER-001"


def test_a_failed_cli_run_is_recorded(tmp_path: Path, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = _run_capture(["--format", "json", "zoo", "--data", "missing", "--shadows", "s.parquet", "--out",
                                 "z"])  # fmt: skip
    assert code == 2 and json.loads(out)["code"] == "QT-INPUT-001"
    code, out, _ = _run_capture(["--format", "json", "shadows", "sample", "--data", "missing", "--n", "5", "--out",
                                 "s.parquet"])  # fmt: skip
    assert code == 2
    recs = L.list_records()
    assert [(r.kind, r.status, r.error.code) for r in recs] == [
        ("zoo", "error", "QT-INPUT-001"),
        ("shadows-sample", "error", json.loads(out)["code"]),
    ]
    assert all(r.trial_id is None and r.spec_hash for r in recs) and L.n_trials() == 0


def _score_argv(out: str) -> list[str]:
    return ["--format", "json", "score", "--live", ".", "--sim", "exact", "--data", str(SYNTH), "--out",
            f"../{out}", "--bootstrap-b", "20", "--perm-b", "20"]  # fmt: skip


def test_a_ledger_never_lands_inside_a_runs_own_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from inside a live log without a manifest (identified by every file under it): the ledger must not be
    written there, or the next run's run_id, and so its trial, would change. Outside any project the ledger is
    the per-user one; a project root that is itself an input is recorded in the per-user ledger instead."""
    monkeypatch.delenv("QT_LEDGER_DIR", raising=False)
    monkeypatch.setenv("QT_LEDGER", "on")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    live = tmp_path / "work" / "live"
    shutil.copytree(LIVE, live)
    (live / "manifest.json").unlink()
    monkeypatch.chdir(live)
    if L.project_root() is not None:
        pytest.skip(f"a project marker above {tmp_path} ({L.project_root()})")

    n = iter(range(10))

    def rid() -> str:
        code, out, err = _run_capture(_score_argv(f"score{next(n)}"))
        assert code == 0, err[-2000:]
        return json.loads(out)["run_id"]

    on = [rid(), rid(), rid()]
    monkeypatch.setenv("QT_LEDGER", "off")
    off = rid()
    assert on == [off] * 3
    user = L.user_ledger()
    assert user.directory.is_relative_to(home) and len(L.list_records(ledger=user)) == 3
    assert L.n_trials(ledger=user) == 1  # three reruns of one spec on unchanged data: one trial
    assert sorted(p.name for p in live.iterdir()) == ["events.parquet", "orders.parquet"]
    # the live log is its own project root (a git checkout): the default ledger would be inside it
    (live / ".git").mkdir()
    monkeypatch.setenv("QT_LEDGER", "on")
    assert L.default_ledger().directory.is_relative_to(live.resolve())
    assert rid() == off
    assert not (live / ".queuetruth").exists() and len(L.list_records(ledger=user)) == 4
