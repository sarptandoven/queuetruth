"""The `queuetruth` console script in process: `--format json` outputs validated against `schemas/json`, exit codes
0/1/2 with `qt.error` documents (O-CLI-1), human output, jobs through the client (fake API), `mcp serve` dispatch.
Hybrid commands end to end are gate tests (marker `integration`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator

from queuetruth import cli
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table

pytestmark = pytest.mark.requires_core


@pytest.fixture(scope="module")
def synth(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures" / "synth_small"


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


@pytest.fixture
def run_json(run: Any, repo_root: Path) -> Any:
    """Runs with `--format json`; asserts exactly one JSON document on stdout, valid against its JSON Schema."""

    def _run(*argv: str) -> tuple[int, dict[str, Any]]:
        code, out, err = run("--format", "json", *argv)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1, f"{argv}: expected one JSON document, got {out!r} (stderr {err!r})"
        doc = json.loads(lines[0])
        if isinstance(doc.get("schema"), str) and doc["schema"].startswith("qt."):
            schema = json.loads((repo_root / "schemas" / "json" / f"{doc['schema']}.schema.json").read_text())
            Draft202012Validator(schema).validate(doc)
        return code, doc

    return _run


def test_native_commands_json(synth: Path, tmp_path: Path, run_json: Any) -> None:
    ds = str(synth)
    sh = str(tmp_path / "sh.parquet")
    code, doc = run_json("shadows", "sample", "--data", ds, "--n", "30", "--out", sh, "--seed", "2")
    assert (code, doc["schema"], doc["n"]) == (0, "qt.shadow_sample_summary", 30)
    code, doc = run_json("replay", "--data", ds, "--out", str(tmp_path / "r"), "--shadows", sh, "--rank-oracle")
    assert (code, doc["schema"]) == (0, "qt.replay_summary")
    assert json.loads((tmp_path / "r" / "summary.json").read_text())["run_id"] == doc["run_id"]
    first = pq.read_table(sh).column("shadow_id")[0].as_py()
    code, doc = run_json("why", "--data", ds, "--shadows", sh, "--shadow-id", first)
    assert (code, doc["schema"]) == (0, "qt.why_trace")
    code, doc = run_json(
        "export",
        "l2-npz",
        "--data",
        ds,
        "--instrument-id",
        "1",
        "--date",
        "2026-06-15",
        "--out",
        str(tmp_path / "x.npz"),
    )
    assert (code, doc["schema"]) == (0, "qt.export_summary")
    code, doc = run_json("referee", "fixtures", "--out", str(tmp_path / "ref"))
    assert (code, doc["schema"]) == (0, "qt.fixtures_summary")
    code, doc = run_json("schema", "list")
    assert code == 0 and "qt.scorecard_summary" in doc["json"] and "qt.zoo_predictions" in doc["arrow"]
    code, doc = run_json("schema", "show", "qt.why_trace")
    assert code == 0 and doc["$schema"].startswith("https://json-schema.org/")
    code, doc = run_json("doctor")
    assert doc["schema"] == "qt.doctor" and doc["schemas_ok"] is True and doc["problems"] == []
    assert code == 0


def test_python_commands_json(synth: Path, tmp_path: Path, run_json: Any) -> None:
    live = str(synth / "own_orders" / "live")
    code, doc = run_json("shadows", "from-live", "--live", live, "--out", str(tmp_path / "s.parquet"))
    assert (code, doc["schema"]) == (0, "qt.shadow_sample_summary") and doc["n"] > 0
    code, doc = run_json("live", "validate", "--live", live)
    assert (code, doc["schema"], doc["errors"]) == (0, "qt.live_validation", [])


def test_live_validate_errors_exit_2(synth: Path, tmp_path: Path, run_json: Any) -> None:
    from queuetruth import live as qlive

    orders, events = qlive.read_log(synth / "own_orders" / "live")
    qlive.write_log(tmp_path / "bad", pa.concat_tables([orders, orders.slice(0, 1)]), events)
    code, doc = run_json("live", "validate", "--live", str(tmp_path / "bad"))
    assert code == 2 and doc["schema"] == "qt.error" and doc["code"] in ("QT-INPUT-002", "QT-INPUT-003")
    assert doc["details"]["validation"]["errors"]


def test_exit_codes_and_error_documents(synth: Path, repo_root: Path, tmp_path: Path, run: Any, run_json: Any) -> None:
    code, out, _ = run("--help")
    assert code == 0 and "Usage: queuetruth" in out
    code, out, _ = run("--version")
    assert (code, out.strip()) == (0, "queuetruth 0.1.1")
    code, doc = run_json("replay", "--out", "x")
    assert (code, doc["code"]) == (2, "QT-USAGE-001")
    code, doc = run_json("replay", "--data", str(tmp_path / "missing"), "--out", str(tmp_path / "o"))
    assert (code, doc["code"]) == (2, "QT-INPUT-001")
    code, doc = run_json(
        "why", "--data", str(synth), "--shadows", str(synth / "truth" / "shadows.parquet"), "--shadow-id", "nope"
    )
    assert (code, doc["code"]) == (2, "QT-CONFIG-001")
    code, doc = run_json("calibrate", "latency", "--live", str(tmp_path / "nolive"), "--out", str(tmp_path / "c"))
    assert (code, doc["code"]) == (2, "QT-INPUT-001")
    wrong = repo_root / "tests" / "fixtures" / "synth_small_nasdaq" / "truth" / "bbo.parquet"
    strict = ["replay", "--data", str(synth), "--strict", "--bbo", "none", "--parity-bbo", str(wrong)]
    code, doc = run_json(*strict, "--out", str(tmp_path / "n"))
    assert (code, doc["schema"], doc["parity"]["pass"]) == (1, "qt.replay_summary", False)
    # Human mode: summary on stdout, `CODE: message` errors and warnings on stderr.
    code, out, err = run("replay", "--data", str(synth), "--out", str(tmp_path / "h"), "--bbo", "none")
    assert code == 0 and out.startswith("replay (qt.replay_summary)") and "files (" in out
    code, out, err = run("replay", "--data", str(tmp_path / "missing"), "--out", "x")
    assert code == 2 and out == "" and err.startswith("QT-INPUT-001: ")
    code, out, err = run("--quiet", *strict, "--out", str(tmp_path / "q"))
    assert code == 1 and err == ""


def test_jobs_through_the_client(tmp_path: Path, run_json: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth.client as qclient
    from queuetruth.client.testing import FakeService

    fake = FakeService(auto_advance=True)
    fake.datasets["ds_00000000000000000000000001"] = {"id": "ds_00000000000000000000000001"}
    monkeypatch.setattr(qclient, "Client", lambda *a, **k: fake.client())
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"dataset": {"dataset_id": "ds_00000000000000000000000001"}}))
    code, job = run_json("jobs", "submit", "replay", "--spec", str(spec), "--wait", "--timeout", "30")
    assert (code, job["schema"], job["state"]) == (0, "qt.job", "succeeded")
    fake.add_job_file(job["id"], "summary.json", b'{"ok": true}\n')
    code, man = run_json("jobs", "files", job["id"])
    assert (code, man["schema"]) == (0, "qt.file_manifest") and [f["name"] for f in man["files"]] == ["summary.json"]
    code, man = run_json("jobs", "download", job["id"], "--out", str(tmp_path / "dl"))
    assert code == 0 and (tmp_path / "dl" / "summary.json").read_bytes() == b'{"ok": true}\n'
    code, _ = run_json("jobs", "submit", "replay", "--spec", str(spec))
    jid = next(j for j in fake.jobs if j != job["id"])
    fake.finish_job(jid, state="failed")
    code, doc = run_json("jobs", "status", jid)
    assert (code, doc["schema"], doc["code"]) == (2, "qt.error", "QT-REMOTE-001")
    code, doc = run_json("jobs", "submit", "nonsense", "--spec", str(spec))
    assert code == 2 and doc["schema"] == "qt.error"


def test_mcp_serve_dispatch(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth.mcp.server as srv

    seen: dict[str, Any] = {}
    monkeypatch.setattr(srv, "serve", lambda **kw: seen.update(kw))
    code, out, _ = run("mcp", "serve", "--local")
    assert (code, out, seen) == (0, "", {"allow_submit": False, "local": True})


# ---- hybrid and Python commands end to end (gate-2) -------------------------------------------------------------


@pytest.mark.integration
def test_hybrid_commands_end_to_end(synth: Path, repo_root: Path, tmp_path: Path, run_json: Any) -> None:
    ds = str(synth)
    sh = str(tmp_path / "sh.parquet")
    assert run_json("shadows", "sample", "--data", ds, "--n", "120", "--out", sh)[0] == 0
    code, z = run_json(
        "zoo", "--data", ds, "--shadows", sh, "--out", str(tmp_path / "zoo"), "--models",
        "front_of_queue,hbt_power3[n=2]", "--bootstrap-b", "30", "--h-max", "1m",
    )  # fmt: skip
    assert code == 0 and len(z["leaderboard"]) == 2
    code, c = run_json(
        "calibrate", "alloc", "--family", "power3", "--data", ds, "--shadows", sh, "--out", str(tmp_path / "cal"),
        "--train", "2026-06-15", "--test", "2026-06-16", "--bootstrap-b", "10",
    )  # fmt: skip
    assert code == 0 and c["held_out"] and c["exports"]["hftbacktest"]
    verdict = next(w for w in c["warnings"] if w.startswith("trust: verdict: "))
    cal = str(tmp_path / "cal" / "calibration.json")
    if verdict == "trust: verdict: exportable":
        code, e = run_json("export", "hftbacktest", "--calibration", cal)
        assert code == 0 and e["text"] == c["exports"]["hftbacktest"]["python"]
    # A fit that does not beat the default out of sample (the same result with worse held-out scores): the export
    # refuses it (exit 1, one qt.error document with the reasons on stdout) and writes nothing; --force writes it
    # with a warning header.
    doc = json.loads(Path(cal).read_text())
    (fid,), (bid,) = doc["held_out"], doc["baseline_held_out"]
    base = doc["baseline_held_out"][bid]
    doc["held_out"][fid] = {
        "brier": {h: (v or 0.0) + 0.05 for h, v in base["brier"].items()},
        "logloss": {h: (v or 0.0) + 0.1 for h, v in base["logloss"].items()},
        "fill_rate_bias": base["fill_rate_bias"],
    }
    doc["warnings"] = [w for w in doc["warnings"] if not w.startswith("trust: ")]
    doc["trust"] = None  # the structured record (schema 1.1) would otherwise decide
    losing = tmp_path / "losing.json"
    losing.write_text(json.dumps(doc))
    for target, name in (("hftbacktest", "h.py"), ("nautilus", "n.py")):
        code, e = run_json("export", target, "--calibration", str(losing), "--out", str(tmp_path / name))
        assert (code, e["schema"], e["code"]) == (1, "qt.error", "QT-CALIB-001")
        assert "not_beating_default" in e["details"]["reasons"]
        assert "DOES NOT BEAT the default" in e["details"]["reason_texts"]["not_beating_default"]
        assert not (tmp_path / name).exists()
        code, e = run_json("export", target, "--calibration", str(losing), "--out", str(tmp_path / name), "--force")
        assert code == 0 and e["schema"] == "qt.export_summary" and "NOT VALIDATED" in e["text"]
        assert (tmp_path / name).read_text() == e["text"]
    live = str(synth / "own_orders" / "live")
    code, lat = run_json("calibrate", "latency", "--live", live, "--out", str(tmp_path / "lat"))
    assert code == 0 and lat["kind"] == "latency"
    code, sc = run_json(
        "score", "--live", live, "--sim", "exact", "--data", ds, "--out", str(tmp_path / "score"),
        "--bootstrap-b", "50", "--perm-b", "50",
    )  # fmt: skip
    assert code == 0 and sc["verdict"]["gates_passed"] is True
    code, sc = run_json(
        "score", "--live", live, "--sim-model", "front_of_queue", "--data", ds, "--out", str(tmp_path / "score2"),
        "--bootstrap-b", "50", "--perm-b", "50", "--fail-abs-bias", "0",
    )  # fmt: skip
    assert code == 1 and sc["verdict"]["gates_passed"] is False
    _, _ = run_json("shadows", "from-live", "--live", live, "--out", str(tmp_path / "own.parquet"))
    key = pq.read_table(tmp_path / "own.parquet").column("order_key")[0].as_py()
    code, w = run_json("why", "--data", ds, "--live", live, "--order-key", key)
    assert code == 0 and w["subject"]["order_key"] == key
    code, _ = run_json(
        "report", "--in", str(tmp_path / "zoo"), "--in", str(tmp_path / "score"), "--out", str(tmp_path / "r.html")
    )
    assert code == 0 and (tmp_path / "r.html").exists()
    fx = repo_root / "tests" / "fixtures" / "referee" / "02_level_shrink_caps_queue"
    base = ["referee", "run", "--data", str(fx / "dataset"), "--schedule", str(fx / "schedule.parquet")]
    code, rep = run_json(*base, "--sim-fills", str(fx / "expected_fills.parquet"), "--out", str(tmp_path / "ref1"))
    assert code == 0 and rep["pass"] is True
    empty = tmp_path / "empty.parquet"
    write_table(SCHEMAS["qt.shadow_fills"].empty_table(), empty)
    code, rep = run_json(*base, "--sim-fills", str(empty), "--out", str(tmp_path / "ref2"), "--simulator", "mine@0.1")
    assert code == 1 and rep["pass"] is False and rep["simulator"] == {"name": "mine", "version": "0.1"}
    code, f = run_json("features", "--data", ds, "--shadows", sh, "--out", str(tmp_path / "feat"))
    assert code == 0 and f["n_orders"] > 0
    code, cl = run_json(
        "calibrate-live", "--data", ds, "--live", live, "--family", "power3", "--out", str(tmp_path / "clive"),
        "--train", "2026-06-15", "--test", "2026-06-16", "--bootstrap-b", "10",
    )  # fmt: skip
    assert code == 0 and cl["schema"] == "qt.calibration_result"
    assert cl["kind"] == "live" and cl["held_out_discordance"] is not None
    assert any(w.startswith("trust: verdict: ") for w in cl["warnings"])
    clive = str(tmp_path / "clive")
    code, e = run_json("export", "hftbacktest", "--calibration", clive, "--out", str(tmp_path / "cl.py"))
    if any(w.startswith("trust: verdict: refused") for w in cl["warnings"]):
        assert (code, e["code"]) == (1, "QT-CALIB-001") and e["details"]["reasons"]
        code, e = run_json("export", "hftbacktest", "--calibration", clive, "--out", str(tmp_path / "cl.py"), "--force")
        assert code == 0 and "NOT VALIDATED" in e["text"]
    else:
        assert code == 0 and (tmp_path / "cl.py").exists()
