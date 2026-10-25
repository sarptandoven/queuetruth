"""Tests of the nightly driver (nightly.py) with the checks replaced by stubs, and of hbt_xcheck.py's pure parts."""

from __future__ import annotations

import datetime as dt
import fcntl
import gzip
import hashlib
import itertools
import json
import os
import plistlib
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import hbt_xcheck as X  # noqa: E402
import nightly as N  # noqa: E402
from qtnightly import EXIT_ADVISORY, EXIT_CHANGED, EXIT_ERROR, EXIT_FAIL, EXIT_OK  # noqa: E402

# ---------------------------------------------------------------------------------------------------------------
# helpers of nightly.py
# ---------------------------------------------------------------------------------------------------------------


def _args(*extra):
    return N.build_parser().parse_args(["run", *extra])


def test_selected_checks():
    assert N.selected_checks(_args()) == list(N.CHECKS)
    assert N.selected_checks(_args("--only", "bench,hbt")) == ["hbt", "bench"]
    assert N.selected_checks(_args("--skip", "nt-rc,repro")) == ["hbt", "nt-pinned", "bench"]
    with pytest.raises(ValueError, match="unknown checks"):
        N.selected_checks(_args("--only", "nope"))
    with pytest.raises(ValueError, match="no check"):
        N.selected_checks(_args("--skip", ",".join(N.CHECKS)))


def test_pinned_versions_come_from_the_repository_files(tmp_path):
    (tmp_path / "scripts" / "nt_crosscheck").mkdir(parents=True)
    req = tmp_path / "scripts" / "nt_crosscheck" / "requirements.txt"
    req.write_text("# comment\nnautilus_trader==1.231.0  # pinned\nnumpy==2.5.3\n\npandas==3.0.6\n")
    (tmp_path / "Makefile").write_text("x:\n\tuv pip install --no-deps hftbacktest==2.4.4  # pin\n")
    assert N.pinned_nt_version(tmp_path) == ("1.231.0", ["numpy==2.5.3", "pandas==3.0.6"])
    assert N.pinned_hbt_version(tmp_path) == "2.4.4"
    req.write_text("numpy==2\n")
    (tmp_path / "Makefile").write_text("nothing\n")
    with pytest.raises(N.CheckError):
        N.pinned_nt_version(tmp_path)
    with pytest.raises(N.CheckError):
        N.pinned_hbt_version(tmp_path)


def test_pins_of_this_checkout_parse():
    v, rest = N.pinned_nt_version(N.REPO_DEFAULT)
    assert N.E.version_key(v) is not None and rest
    assert N.E.version_key(N.pinned_hbt_version(N.REPO_DEFAULT)) is not None


def test_schedule_texts(tmp_path):
    plist = N.schedule_text("launchd", tmp_path / "repo", tmp_path / "state", 2, 30, "full", "x.nightly")
    doc = plistlib.loads(plist.encode())
    assert doc["Label"] == "x.nightly"
    assert doc["ProgramArguments"][:2] == ["/bin/bash", str(HERE / "nightly.sh")]
    assert doc["ProgramArguments"][2:] == ["run", "--mode", "full", "--state", str(tmp_path / "state")]
    assert doc["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert doc["StandardOutPath"] == str(tmp_path / "state" / "launchd.log") and doc["LowPriorityIO"] is True
    cron = N.schedule_text("cron", tmp_path, tmp_path / "s", 4, 5, "reduced", "x")
    assert cron.startswith("5 4 * * * mkdir -p ") and "--mode reduced" in cron and cron.endswith("2>&1\n")
    with pytest.raises(ValueError):
        N.schedule_text("cron", tmp_path, tmp_path, 24, 0, "full", "x")
    with pytest.raises(ValueError):
        N.schedule_text("systemd", tmp_path, tmp_path, 1, 0, "full", "x")


def test_schedule_texts_survive_awkward_paths_and_pass_run_options(tmp_path):
    state = tmp_path / "a b&c<d>%e"
    extra = ["--itch-dirs", "/data/it ch", "--repro-download"]
    doc = plistlib.loads(N.schedule_text("launchd", tmp_path, state, 2, 30, "full", "x", extra).encode())
    assert doc["ProgramArguments"][-5:] == ["--state", str(state), *extra]
    cron = N.schedule_text("cron", tmp_path, state, 2, 30, "full", "x", extra)
    cmd = cron.split(" ", 5)[5].rstrip("\n")
    assert "\\%" in cmd and "%e" not in cmd.replace("\\%", "")  # cron would turn a bare % into a newline
    words = shlex.split(cmd.replace("\\%", "%"))
    assert words[:3] == ["mkdir", "-p", str(state)]
    assert words[-6:-3] == extra and words[-3:] == [">>", str(state / "cron.log"), "2>&1"]


def test_schedule_command_validates_passed_through_options(tmp_path, capsys):
    base = ["schedule", "launchd", "--state", str(tmp_path / "s")]
    assert N.main([*base, "--", "--repro-download", "--itch-dirs", "/x"]) == EXIT_OK
    assert plistlib.loads(capsys.readouterr().out.encode())["ProgramArguments"][-3:] == [
        "--repro-download",
        "--itch-dirs",
        "/x",
    ]
    with pytest.raises(SystemExit):
        N.main([*base, "--", "--no-such-option"])
    assert N.main([*base, "--", "--mode", "reduced"]) == EXIT_ERROR  # schedule's own option
    assert N.main(["list", "--", "--offline"]) == EXIT_ERROR


def test_child_env_strips_interpreter_overrides(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/src")
    monkeypatch.setenv("VIRTUAL_ENV", "/v")
    monkeypatch.setenv("KEEP_ME", "1")
    env = N.child_env({"X": "y"})
    assert "PYTHONPATH" not in env and "VIRTUAL_ENV" not in env
    assert env["KEEP_ME"] == "1" and env["X"] == "y" and env["PYTHONNOUSERSITE"] == "1"


def test_run_logged_records_output_exit_and_timeout(tmp_path):
    log = tmp_path / "l.txt"
    assert N.run_logged([sys.executable, "-c", "print('hi'); raise SystemExit(3)"], log) == 3
    text = log.read_text()
    assert "hi" in text and "[nightly] exit 3" in text
    t0 = time.monotonic()
    rc = N.run_logged([sys.executable, "-c", "import time; time.sleep(30)"], log, timeout=1)
    assert rc == 124 and time.monotonic() - t0 < 25 and "timeout after 1" in log.read_text()
    assert N.run_logged(["/nonexistent/binary"], log) == 127


def test_run_logged_kills_the_child_group_when_the_runner_is_stopped(tmp_path):
    """SIGTERM to a runner blocked in run_logged stops the child's whole process group, then the runner exits."""
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "runner.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(HERE)!r})\n"
        "import nightly as N\n"
        "from pathlib import Path\n"
        "with N.stop_signals_raise():\n"
        "    try:\n"
        f"        argv = ['/bin/sh', '-c', 'echo $$ > {pidfile}; sleep 60 & wait']\n"
        f"        N.run_logged(argv, Path({str(tmp_path / 'l')!r}))\n"
        "    except N.Interrupted as e:\n"
        "        print('interrupted', e.signum)\n"
        "        raise SystemExit(2)\n"
    )
    runner = subprocess.Popen([sys.executable, "-I", str(script)], stdout=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 20
    while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text())
    runner.send_signal(signal.SIGTERM)
    out, _ = runner.communicate(timeout=30)
    assert runner.returncode == 2 and "interrupted 15" in out
    with pytest.raises(ProcessLookupError):  # the shell and its sleep (same group) are gone
        os.killpg(child, 0)
    assert "process group stopped" in (tmp_path / "l").read_text()


def _run_dir(state, nid, mode="reduced"):
    d = state / "runs" / nid
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"nightly_id": nid, "mode": mode}))


def test_prune_runs_keeps_the_newest_per_mode_the_current_and_protected(tmp_path):
    reduced = [f"2026102{i}T000000Z" for i in range(5)]
    for i in reduced:
        _run_dir(tmp_path, i)
    _run_dir(tmp_path, "20261019T000000Z", "full")  # older than every reduced run, but the only full one
    (tmp_path / "runs" / "20261030T000000Z").mkdir()  # the current run (no run.json yet)
    (tmp_path / "runs" / "notes").mkdir()
    removed = N.prune_runs(tmp_path, keep=2, current="20261030T000000Z", protect={reduced[0]})
    assert removed == reduced[1:3]
    left = sorted(p.name for p in (tmp_path / "runs").iterdir())
    assert left == ["20261019T000000Z", reduced[0], *reduced[3:], "20261030T000000Z", "notes"]


def test_bench_load_is_the_worse_of_start_and_end():
    assert N.bench_load({"env": {"load_avg_start": [211.8, 190.5, 157.8], "load_avg_end": [12.2, 9.0, 8.0]}}) == 211.8
    assert N.bench_load({"env": {}}) is None
    assert N.build_parser().parse_args(["run"]).bench_max_load == float(os.cpu_count() or 8)


def _bench_ctx(tmp_path, *extra):
    repo = tmp_path / "repo"
    (repo / "benches").mkdir(parents=True)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    return N.Ctx(
        a=_args("--mode", "full", *extra),
        repo=repo,
        state=tmp_path / "state",
        run_dir=tmp_path / "state" / "runs" / "r",
        mode=N.MODES["full"],
        nightly_id="r",
        today=N.utcnow().date(),
        qt_py=venv / "bin" / "python",
    )


def _fake_bench(monkeypatch, load, calls):
    def run_logged(argv, log, cwd=None, timeout=None, env=None):
        argv = [str(x) for x in argv]
        calls.append(argv)
        if "--out" in argv:
            doc = {"suite": "quick", "env": {"load_avg_start": [load], "load_avg_end": [1.0]}, "cases": []}
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(doc))
        return 0

    monkeypatch.setattr(N, "run_logged", run_logged)


def test_check_bench_errors_on_a_saturated_machine_and_passes_max_load(monkeypatch, tmp_path):
    ctx = _bench_ctx(tmp_path, "--bench-max-load", "8")
    cdir = tmp_path / "c"
    cdir.mkdir()
    calls: list = []
    _fake_bench(monkeypatch, 212.0, calls)
    with pytest.raises(N.CheckError, match=r"load 212\.0 > --bench-max-load 8"):
        N.check_bench(ctx, cdir, {"runs": []})
    bench_argv = next(c for c in calls if "--out" in c)
    assert bench_argv[bench_argv.index("--max-load") + 1] == "8.0"
    calls.clear()
    _fake_bench(monkeypatch, 3.0, calls)
    res = N.check_bench(ctx, cdir, {"runs": []})
    assert res["metrics"]["bench.vs_previous"] == "no previous run" and res["provenance"]["bench.baseline"] == "none"
    assert not any("check.py" in " ".join(c) for c in calls)


def test_check_bench_uses_the_pinned_baseline(monkeypatch, tmp_path):
    base = tmp_path / "baseline.json"
    base.write_text("{}")
    ctx = _bench_ctx(tmp_path, "--bench-baseline", str(base))
    cdir = tmp_path / "c"
    cdir.mkdir()
    calls: list = []
    _fake_bench(monkeypatch, 1.0, calls)
    res = N.check_bench(ctx, cdir, {"runs": []})
    chk = next(c for c in calls if any(x.endswith("check.py") for x in c))
    assert chk[chk.index("--baseline") + 1] == str(base)
    assert res["provenance"]["bench.baseline"] == f"--bench-baseline {base}"


def test_bench_baseline_skips_regressed_and_incomparable_nights(tmp_path):
    def run(nid, status, verdict):
        d = tmp_path / "runs" / nid / "checks" / "bench"
        d.mkdir(parents=True)
        (d / "bench.json").write_text("{}")
        bench = {
            "status": status,
            "metrics": {"bench.vs_previous": verdict},
            "artifacts": {"bench.json": "checks/bench/bench.json"},
        }
        return {"nightly_id": nid, "mode": "full", "checks": {"bench": bench}}

    hist = {
        "runs": [
            run("20261020T000000Z", "pass", "no previous run"),
            run("20261021T000000Z", "fail", "regression"),  # a 30% slowdown: fails once ...
            run("20261022T000000Z", "pass", "not comparable"),  # ... and must not become the baseline either
            {"nightly_id": "20261023T000000Z", "mode": "reduced", "checks": {}},
        ]
    }
    got = N.bench_baseline_run(tmp_path, hist, "full")
    assert got == ("20261020T000000Z", tmp_path / "runs/20261020T000000Z/checks/bench/bench.json")
    assert N.bench_baseline_run(tmp_path, hist, "reduced") is None


def test_bench_shim_links_the_checkout_and_the_venv(tmp_path):
    repo = tmp_path / "repo"
    for d in ("benches", "tests", ".venv", ".scratch", "target"):
        (repo / d).mkdir(parents=True)
    (repo / "Makefile").write_text("")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    a = _args("--state", str(tmp_path / "state"))
    ctx = N.Ctx(
        a=a,
        repo=repo,
        state=tmp_path / "state",
        run_dir=tmp_path / "r",
        mode=N.MODES["reduced"],
        nightly_id="x",
        today=N.utcnow().date(),
        qt_py=venv / "bin" / "python",
    )
    shim = N.bench_shim(ctx)
    assert sorted(p.name for p in shim.iterdir()) == [".venv", "Makefile", "benches", "tests"]
    assert (shim / ".venv").resolve() == venv.resolve()
    assert (shim / "benches").resolve() == (repo / "benches").resolve()


def _gz(path, body):
    with gzip.open(path, "wb") as f:
        f.write(body)


def test_verified_subset_skips_content_that_does_not_match_the_pin(tmp_path):
    good, stale = tmp_path / "good", tmp_path / "stale"
    for d, body in ((stale, b"old frames"), (good, b"pinned frames")):
        d.mkdir()
        _gz(d / "12302019.subset.T.itch50.gz", body)
        (d / "12302019.subset.T.manifest.json").write_text("{}")
    pin = {"framed_sha256": hashlib.sha256(b"pinned frames").hexdigest()}
    logs: list[str] = []
    assert N.verified_subset([stale, good], "12302019", "T", pin, logs.append) == good / "12302019.subset.T.itch50.gz"
    assert len(logs) == 1 and "stale" in logs[0]
    assert N.verified_subset([stale], "12302019", "T", pin, logs.append) is None
    assert N.verified_subset([stale], "12302019", "T", None, logs.append) == stale / "12302019.subset.T.itch50.gz"


def test_pinned_subsets_of_this_checkout_parse():
    pins = N.pinned_subsets(N.REPO_DEFAULT)
    assert ("12302019", "AAPL-MSFT-SPY-TSLA") in pins
    assert all(len(v["framed_sha256"]) == 64 for v in pins.values())


def test_repro_writes_inside_the_checkout_even_with_an_outside_state(tmp_path):
    ctx = N.Ctx(
        a=_args(),
        repo=tmp_path / "repo",
        state=tmp_path / "state",
        run_dir=tmp_path / "state" / "runs" / "x",
        mode=N.MODES["full"],
        nightly_id="20261024T000000Z",
        today=N.utcnow().date(),
    )
    (tmp_path / "repo").mkdir()
    assert N.repro_out_dir(ctx, tmp_path / "state" / "c") == tmp_path / "repo" / ".scratch" / "nightly-repro" / (
        "20261024T000000Z"
    )
    inside = tmp_path / "repo" / ".scratch" / "nightly" / "c"
    assert N.repro_out_dir(ctx, inside) == inside / "out"


def test_new_run_dir_waits_out_a_taken_second(monkeypatch, tmp_path):
    t = dt.datetime(2026, 10, 24, 2, 30, tzinfo=dt.UTC)
    times = iter([t, t + dt.timedelta(seconds=1)])
    monkeypatch.setattr(N, "utcnow", lambda: next(times))
    monkeypatch.setattr(N.time, "sleep", lambda s: None)
    (tmp_path / "runs" / "20261024T023000Z").mkdir(parents=True)
    _, nid, run_dir = N.new_run_dir(tmp_path)
    assert nid == "20261024T023001Z" and run_dir.is_dir()


def test_find_subset(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    (d1 / "12302019.subset.T.itch50.gz").write_bytes(b"")  # no manifest: not usable
    (d2 / "12302019.subset.T.itch50.gz").write_bytes(b"")
    (d2 / "12302019.subset.T.manifest.json").write_text("{}")
    assert N.find_subset([d1, d2], "12302019", "T") == d2 / "12302019.subset.T.itch50.gz"
    assert N.find_subset([d1], "12302019", "T") is None


# ---------------------------------------------------------------------------------------------------------------
# the driver end to end, with stub setup and checks
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """nightly.py with setup and every check replaced; `outcome[name]` is the body or exception a check returns."""
    outcome: dict = {}

    def setup_qt(ctx):
        ctx.qt_py = Path(sys.executable)
        return {"python": sys.executable}

    def setup_dataset(ctx):
        ctx.dataset = tmp_path / "ds"
        return {"dataset_id": "d"}

    def body(name):
        o = outcome.get(name, {"gates": [N.V.gate("g", True)], "metrics": {"m": 1}, "noisy": []})
        if isinstance(o, Exception):
            raise o
        return o

    monkeypatch.setattr(N, "setup_qt", setup_qt)
    monkeypatch.setattr(N, "setup_dataset", setup_dataset)
    monkeypatch.setattr(N, "check_hbt", lambda ctx, cdir: body("hbt"))
    monkeypatch.setattr(N, "check_nt", lambda ctx, cdir, which: body(which))
    monkeypatch.setattr(N, "check_repro", lambda ctx, cdir: body("repro"))
    monkeypatch.setattr(N, "check_bench", lambda ctx, cdir, hist: body("bench"))
    clock = itertools.count()
    t0 = dt.datetime(2026, 10, 24, tzinfo=dt.UTC)
    monkeypatch.setattr(N, "utcnow", lambda: t0 + dt.timedelta(seconds=next(clock)))
    return outcome


def _main(state, *extra):
    return N.main(["run", "--mode", "reduced", "--state", str(state), *extra])


def test_driver_first_run_then_unchanged_then_changed(stubbed, tmp_path):
    state = tmp_path / "state"
    assert _main(state) == EXIT_OK
    hist = json.loads((state / "history.json").read_text())
    assert len(hist["runs"]) == 1 and hist["runs"][0]["diff"]["vs"] is None
    latest = json.loads((state / "latest.json").read_text())
    assert latest["exit_code"] == EXIT_OK and set(latest["checks"]) == set(N.CHECKS)
    rid = latest["nightly_id"]
    assert (state / "runs" / rid / "run.json").exists() and (state / "runs" / rid / "index.html").exists()
    assert f'href="runs/{rid}/' not in (state / "runs" / rid / "index.html").read_text()
    assert _main(state) == EXIT_OK  # same numbers
    stubbed["hbt"] = {"gates": [N.V.gate("g", True)], "metrics": {"m": 2}, "noisy": []}
    assert _main(state) == EXIT_CHANGED
    latest = json.loads((state / "latest.json").read_text())
    assert latest["diff"]["changed"] == [{"check": "hbt", "metric": "m", "before": 1, "after": 2}]
    assert latest["flags"] == ["changed"]
    page = (state / "index.html").read_text()
    assert "1 tracked number(s) changed" in page


def test_driver_failure_error_advisory(stubbed, tmp_path):
    state = tmp_path / "state"
    stubbed["nt-rc"] = N.CheckError("PyPI unreachable")
    assert _main(state) == EXIT_ADVISORY
    stubbed["repro"] = {"gates": [N.V.gate("step x", False, "fail")], "metrics": {}, "noisy": []}
    assert _main(state) == EXIT_FAIL
    stubbed["bench"] = RuntimeError("bug in adapter")
    assert _main(state) == EXIT_ERROR
    run = json.loads((state / "latest.json").read_text())
    assert run["checks"]["bench"]["status"] == "error" and "RuntimeError" in run["checks"]["bench"]["reason"]
    assert "traceback" in run["checks"]["bench"]
    assert run["checks"]["nt-rc"]["status"] == "error" and run["checks"]["nt-rc"]["required"] is False
    stubbed["hbt"] = {"gates": [], "metrics": {}, "noisy": []}  # a check without gates cannot pass
    _main(state)
    assert json.loads((state / "latest.json").read_text())["checks"]["hbt"]["status"] == "fail"


def test_driver_setup_failure_errors_every_check(stubbed, monkeypatch, tmp_path):
    def bad(ctx):
        raise N.CheckError("maturin build failed")

    monkeypatch.setattr(N, "setup_qt", bad)
    assert _main(tmp_path / "a", "--only", "nt-rc") == EXIT_ERROR  # a setup failure is not "only advisory"
    assert _main(tmp_path / "s", "--only", "hbt,nt-rc") == EXIT_ERROR
    run = json.loads((tmp_path / "s" / "latest.json").read_text())
    assert run["setup"]["qt"]["status"] == "error" and "dataset" not in run["setup"]
    assert {c["status"] for c in run["checks"].values()} == {"error"}
    assert all("maturin" in c["reason"] for c in run["checks"].values())


def test_driver_low_disk(stubbed, monkeypatch, tmp_path):
    monkeypatch.setattr(N, "free_gib", lambda p: 1.0)
    assert _main(tmp_path / "s", "--only", "hbt") == EXIT_ERROR
    run = json.loads((tmp_path / "s" / "latest.json").read_text())
    assert "free disk" in run["checks"]["hbt"]["reason"]


def test_driver_refuses_a_held_lock(stubbed, tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    with (state / ".lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _main(state) == EXIT_ERROR
    assert not (state / "runs").exists()


def test_driver_keeps_an_unreadable_history_and_every_run_directory(stubbed, tmp_path):
    state = tmp_path / "s"
    for _ in range(3):
        assert _main(state) == EXIT_OK
    old = sorted(p.name for p in (state / "runs").iterdir())
    doc = json.loads((state / "history.json").read_text())
    (state / "history.json").write_text(json.dumps({**doc, "schema_version": 2}))
    assert _main(state, "--keep-runs", "1") == EXIT_ERROR
    assert json.loads((state / "history.json").read_text())["schema_version"] == 2
    assert set(old) < {p.name for p in (state / "runs").iterdir()}  # nothing pruned on an unloaded history
    run = json.loads((state / "latest.json").read_text())
    assert "history unreadable" in run["runner_error"]


def test_driver_diffs_each_check_against_its_last_result(stubbed, tmp_path):
    state = tmp_path / "s"
    assert _main(state) == EXIT_OK
    stubbed["nt-pinned"] = {"gates": [N.V.gate("g", True)], "metrics": {"m": 2}, "noisy": []}
    assert _main(state, "--only", "hbt") == EXIT_OK  # a partial run: nt-pinned not run, not a change
    stubbed["repro"] = N.CheckError("subset missing")
    assert _main(state, "--skip", "nt-pinned") == EXIT_ERROR
    del stubbed["repro"]
    assert _main(state) == EXIT_CHANGED
    d = json.loads((state / "latest.json").read_text())["diff"]
    assert [(e["check"], e["metric"], e["before"], e["after"]) for e in d["changed"]] == [("nt-pinned", "m", 1, 2)]
    assert [(s["check"], s["before"], s["after"]) for s in d["status_changes"]] == [("repro", "error", "pass")]


def test_driver_records_an_interrupted_run(stubbed, monkeypatch, tmp_path):
    state = tmp_path / "s"
    assert _main(state) == EXIT_OK

    def stopped(ctx, cdir, which):
        raise N.Interrupted(signal.SIGTERM)

    monkeypatch.setattr(N, "check_nt", stopped)
    assert _main(state, "--keep-runs", "1") == EXIT_ERROR
    run = json.loads((state / "latest.json").read_text())
    assert run["interrupted"] == {"signal": "SIGTERM", "during": "nt-pinned"} and "SIGTERM" in run["runner_error"]
    assert set(run["checks"]) == {"hbt"}
    assert len(json.loads((state / "history.json").read_text())["runs"]) == 1  # not a baseline for anything
    assert len(list((state / "runs").iterdir())) == 2  # nothing pruned
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_driver_unknown_or_empty_selection_is_a_usage_error(stubbed, tmp_path):
    assert _main(tmp_path / "s", "--only", "nope") == EXIT_ERROR
    assert _main(tmp_path / "s", "--skip", ",".join(N.CHECKS)) == EXIT_ERROR
    assert not (tmp_path / "s" / "runs").exists()


def test_render_and_list(stubbed, tmp_path, capsys):
    state = tmp_path / "s"
    assert N.main(["render", "--state", str(state)]) == EXIT_ERROR  # nothing yet
    _main(state)
    os.remove(state / "index.html")
    assert N.main(["render", "--state", str(state)]) == EXIT_OK
    assert (state / "index.html").exists()
    assert N.main(["list"]) == EXIT_OK
    assert "nt-rc" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------------------------
# hbt_xcheck.py pure parts
# ---------------------------------------------------------------------------------------------------------------

B, A = 66, 65


@pytest.mark.parametrize(
    ("side", "mode", "price", "off", "bb", "ba", "want"),
    [
        (B, "touch", None, 0, 100, 110, (100, None)),
        (B, "touch", None, -2, 100, 110, (80, None)),  # behind the touch
        (A, "touch", None, -1, 100, 110, (120, None)),
        (B, "touch", None, 1, 100, 110, (None, "improving_offset_crosses")),  # rejected, as in the engine
        (B, "touch", None, 0, None, 110, (None, "no_same_side_best")),
        (B, "abs", 115, 0, 100, 110, (115, "crosses_at_live")),  # an absolute price may cross: dropped
        (A, "abs", 95, 0, 100, 110, (95, "crosses_at_live")),
        (A, "abs", None, 0, 100, 110, (None, "no_abs_price")),
    ],
)
def test_resolve_price(side, mode, price, off, bb, ba, want):
    assert X.resolve_price(side, mode, price, off, 10, bb, ba, B) == want


def test_resolve_price_improving_inside_the_spread():
    assert X.resolve_price(B, "touch", None, 1, 10, 100, 130, B) == (110, None)
    assert X.resolve_price(A, "touch", None, 1, 10, 100, 130, B) == (120, None)
    assert set(X.DROP_REASONS) == {"no_abs_price", "no_same_side_best", "improving_offset_crosses", "crosses_at_live"}


def test_scale_matches_the_npz_writer():
    assert X.scale(150_000_000_000, -9) == 150.0
    assert X.scale(10_000_000, -9) == 0.01
    assert X.scale(3, 2) == 300.0
    assert X.scale(7, 0) == 7.0


def _runs(first_fill, events=(), error=None, scen="d_1", model="m"):
    return [
        {"scenario": scen, "model_id": model, "first_fill": first_fill, "order_events": list(events), "error": error}
    ]


def test_compare_runs_agreement_and_horizons():
    om = {"1": {"shadow_id": "s1", "ts_live": 0}, "2": {"shadow_id": "s2", "ts_live": 0}}
    zoo = {"d_1": {"m": {"1": 500_000_000, "2": None}}}
    res = X.compare_runs(_runs({"1": 500_000_000}), om, zoo)
    m = res["models"]["m"]
    assert res["ok"] and m["compared"] == 2 and m["agree_first_fill_ts"] == 2
    assert m["horizons"]["0.1s"] == {"agree": 2, "hbt_filled": 0, "zoo_filled": 0}
    assert m["horizons"]["1s"] == {"agree": 2, "hbt_filled": 1, "zoo_filled": 1}


def test_compare_runs_mismatch_not_resting_and_error_window():
    om = {str(i): {"shadow_id": f"s{i}", "ts_live": 10 * i} for i in range(1, 5)}
    zoo = {"d_1": {"m": {"1": 15, "2": None, "3": 100, "4": 45}}}
    runs = _runs(
        {"1": 16},  # order 1: one ns later in hftbacktest
        events=[{"order_id": 2, "status": "expired"}],  # order 2 expired on arrival
        error={"ts": 40, "error_code": 14, "step": 9},  # orders 4 (live at 40) and 3 (zoo fill after error) excluded
    )
    res = X.compare_runs(runs, om, zoo)
    m = res["models"]["m"]
    assert not res["ok"]
    assert (m["compared"], m["agree_first_fill_ts"], m["not_resting_in_hbt"], m["excluded_after_error"]) == (1, 0, 1, 2)
    assert m["mismatches"] == [
        {"scenario": "d_1", "order_id": 1, "shadow_id": "s1", "ts_live": 10, "hbt_first_fill": 16, "zoo_first_fill": 15}
    ]
    assert m["hbt_errors"] == [{"scenario": "d_1", "ts": 40, "error_code": 14, "step": 9}]


def test_compare_runs_nothing_compared_or_missing_zoo():
    om = {"1": {"shadow_id": "s1", "ts_live": 0}}
    res = X.compare_runs(_runs({}, events=[{"order_id": 1, "status": "rejected"}]), om, {"d_1": {"m": {"1": None}}})
    assert not res["ok"]
    with pytest.raises(ValueError, match="no zoo first fills"):
        X.compare_runs(_runs({}), om, {"d_1": {}})
    assert X.compare_runs([], om, {})["ok"] is False
