"""Tests of the upstream compatibility checks (`compat.py`: version finding, the runs, verdict, pruning) and of the
page (`compat_site.py`). No network: PyPI is a fake fetcher and the nightly runner a fake that writes `latest.json`."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import compat as C  # noqa: E402
import compat_site as CS  # noqa: E402

LINUX = "manylinux_2_28_x86_64"


def _files(project: str, version: str, tags=("cp313",), plat=LINUX, yanked=False) -> list[dict]:
    return [
        {"filename": f"{project}-{version}-{t}-{t}-{plat}.whl", "yanked": yanked, "digests": {"sha256": "ab" * 32}}
        for t in tags
    ]


def _fake_pypi(projects: dict[str, dict[str, list[dict]]]):
    """fetch(url) for https://pypi.org/pypi/<name>/json; records the URLs asked for."""
    asked: list[str] = []

    def fetch(url: str) -> dict:
        asked.append(url)
        m = re.fullmatch(r"https://pypi\.org/pypi/([^/]+)/json", url)
        assert m, url
        return {"releases": projects[m.group(1)]}

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


PINNED = {"hftbacktest": "2.4.4", "nautilus_trader": "1.231.0"}


def _pypi():
    return _fake_pypi(
        {
            "hftbacktest": {
                "2.4.3": _files("hftbacktest", "2.4.3"),
                "2.4.4": _files("hftbacktest", "2.4.4"),
                "2.5.0": _files("hftbacktest", "2.5.0", tags=("cp311", "cp312")),  # no cp313 wheel: skipped
                "1.8.4": _files("hftbacktest", "1.8.4"),  # an older major line than the pin: not looked at
            },
            "nautilus-trader": {
                "1.230.0": _files("nautilus_trader", "1.230.0"),
                "1.231.0": _files("nautilus_trader", "1.231.0"),
                "1.232.0": _files("nautilus_trader", "1.232.0"),
                "2.0.0rc7": _files("nautilus_trader", "2.0.0rc7"),
                "2.0.0rc8": _files("nautilus_trader", "2.0.0rc8"),
                "2.0.0rc9": _files("nautilus_trader", "2.0.0rc9", yanked=True),
                "2.0.0.dev20261030": _files("nautilus_trader", "2.0.0.dev20261030"),
                "2.0.0rc10": _files("nautilus_trader", "2.0.0rc10", plat="macosx_11_0_arm64"),
            },
        }
    )


# ---------------------------------------------------------------------------------------------------------------
# version finding
# ---------------------------------------------------------------------------------------------------------------


def test_newest_per_major_skips_yanked_dev_and_releases_without_a_linux_wheel():
    releases = {
        "1.231.0": _files("nt", "1.231.0"),
        "1.232.0": _files("nt", "1.232.0"),
        "2.0.0rc8": _files("nt", "2.0.0rc8"),
        "2.0.0rc9": _files("nt", "2.0.0rc9", yanked=True),
        "2.0.0.dev1": _files("nt", "2.0.0.dev1"),
        "2.0.0rc10": _files("nt", "2.0.0rc10", plat="macosx_11_0_arm64"),
        "3.0.0": [],
        "0.9.0": _files("nt", "0.9.0"),
    }
    ok = C.host_installable("cp313", "Linux", "x86_64")
    assert C.newest_per_major(releases, 1, ok) == ["2.0.0rc8", "1.232.0"]
    assert C.newest_per_major(releases, 2, ok) == ["2.0.0rc8"]
    assert C.newest_per_major(releases, 0, C.host_installable("cp312", "Linux", "x86_64")) == []
    aarch = {"2.0.0": _files("nt", "2.0.0", plat="manylinux_2_28_aarch64")}
    assert C.newest_per_major(aarch, 2, C.host_installable("cp313", "Linux", "aarch64")) == ["2.0.0"]
    assert C.newest_per_major(aarch, 2, ok) == []


def test_plan_targets_pinned_first_then_the_newest_of_each_major_line():
    fetch = _pypi()
    targets = C.plan_targets(PINNED, fetch)
    assert fetch.asked == ["https://pypi.org/pypi/hftbacktest/json", "https://pypi.org/pypi/nautilus-trader/json"]
    assert targets == [
        {"library": "hftbacktest", "version": "2.4.4", "roles": ["pinned", "newest 2.x"], "check": "hbt",
         "args": ["--hbt", "2.4.4"]},
        {"library": "nautilus_trader", "version": "1.231.0", "roles": ["pinned"], "check": "nt-pinned", "args": []},
        {"library": "nautilus_trader", "version": "2.0.0rc8", "roles": ["newest 2.x"], "check": "nt-rc",
         "args": ["--nt-rc", "2.0.0rc8"]},
        {"library": "nautilus_trader", "version": "1.232.0", "roles": ["newest 1.x"], "check": "nt-rc",
         "args": ["--nt-rc", "1.232.0"]},
    ]  # fmt: skip
    out = C.github_output(targets)
    assert out == "count=4\nhftbacktest=2.4.4\nnautilus_trader=1.231.0,2.0.0rc8,1.232.0\n"


def test_plan_targets_a_new_hftbacktest_release_and_a_pin_newer_than_pypi():
    fetch = _fake_pypi(
        {
            "hftbacktest": {"2.4.4": _files("hftbacktest", "2.4.4"), "2.4.5": _files("hftbacktest", "2.4.5")},
            "nautilus-trader": {"1.230.0": _files("nautilus_trader", "1.230.0")},  # the pin is not on PyPI
        }
    )
    targets = C.plan_targets(PINNED, fetch)
    assert [(t["library"], t["version"], t["roles"], t["check"]) for t in targets] == [
        ("hftbacktest", "2.4.4", ["pinned"], "hbt"),
        ("hftbacktest", "2.4.5", ["newest 2.x"], "hbt"),
        ("nautilus_trader", "1.231.0", ["pinned"], "nt-pinned"),
    ]
    assert targets[1]["args"] == ["--hbt", "2.4.5"]


def test_plan_targets_rejects_a_bad_pin_and_reports_pypi_errors():
    with pytest.raises(ValueError, match="not a release version"):
        C.plan_targets({**PINNED, "hftbacktest": "main"}, _pypi())

    def down(url):
        raise C.E.EnvError(f"GET {url}: offline")

    with pytest.raises(C.E.EnvError, match="offline"):
        C.plan_targets(PINNED, down)


def test_cmd_plan_reads_the_pins_from_the_repository(tmp_path, capsys):
    out = tmp_path / "targets.json"
    a = C.build_parser().parse_args(
        ["plan", "--out", str(out), "--py-tag", "cp313", "--system", "Linux", "--machine", "x86_64"]
    )
    assert C.cmd_plan(a, fetch=_pypi()) == 0
    targets = json.loads(out.read_text())
    assert targets[0]["roles"][0] == "pinned" and targets[0]["version"] == C.N.pinned_hbt_version(C.N.REPO_DEFAULT)
    assert capsys.readouterr().out.startswith(f"count={len(targets)}\n")


def test_plan_defaults_to_this_host_and_reads_macos_wheels():
    a = C.build_parser().parse_args(["plan", "--out", "x"])
    assert a.py_tag == f"cp{sys.version_info[0]}{sys.version_info[1]}"
    assert (a.system, a.machine) == (C.platform.system(), C.platform.machine())
    mac = {"2.0.0": _files("nt", "2.0.0", plat="macosx_14_0_arm64")}
    assert C.newest_per_major(mac, 2, C.host_installable("cp313", "Darwin", "arm64", 15)) == ["2.0.0"]
    assert C.newest_per_major(mac, 2, C.host_installable("cp313", "Linux", "x86_64")) == []


def test_runner_option_selects_the_hbt_version():
    a = C.N.build_parser().parse_args(["run", "--hbt", "2.4.5"])
    assert a.hbt == "2.4.5"
    assert C.N.build_parser().parse_args(["run"]).hbt == "pinned"


# ---------------------------------------------------------------------------------------------------------------
# runs, verdict, prune
# ---------------------------------------------------------------------------------------------------------------


def _run_args(tmp_path, **kw):
    base = dict(
        repo=str(C.N.REPO_DEFAULT), out=str(tmp_path / "out"), python="/py/python3.13", qt_python="/qt/python",
        mode="reduced", date="2026-11-01", min_free_gib=2.0, threads=2, target_timeout=60.0, commit="abc", run_url="https://ci/run/1",
        log_base="https://github.com/o/r/releases/download/compat-results/", summary="",
    )  # fmt: skip
    base.update(kw)
    return argparse.Namespace(**base)


def _fake_runner(results: dict[str, object]):
    """runner(argv, log): writes the latest.json a nightly run of argv's --only check would write."""
    calls: list[list[str]] = []

    def run(argv, log, timeout_s):
        calls.append(argv)
        state = Path(argv[argv.index("--state") + 1])
        check = argv[argv.index("--only") + 1]
        log.write_text("runner says hello\n")
        version = argv[-1] if argv[-2] in ("--hbt", "--nt-rc") else "pinned"
        res = results.get(f"{check}:{version}")
        if res == "timeout":  # stopped at the time limit before it recorded anything
            return C.TIMEOUT_RC
        if res is None:  # the runner crashed before writing anything
            return 2
        nid = "20261101T041700Z"
        cdir = state / "runs" / nid / "checks" / check
        cdir.mkdir(parents=True)
        (cdir / "log.txt").write_text(f"{check} log\n")
        (state / "runs" / nid / "runner.log").write_text("runner log\n")
        run_doc = {"nightly_id": nid, "setup": {"qt": {"queuetruth": "0.2.0"}}, "checks": {check: res}}
        (state / "latest.json").write_text(json.dumps(run_doc))
        return 0 if res["status"] == "pass" else 1

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _gate(name, ok, detail=""):
    return {"name": name, "ok": ok, "detail": detail}


TARGETS = [
    {"library": "hftbacktest", "version": "2.4.4", "roles": ["pinned", "newest 2.x"], "check": "hbt",
     "args": ["--hbt", "2.4.4"]},
    {"library": "nautilus_trader", "version": "1.231.0", "roles": ["pinned"], "check": "nt-pinned", "args": []},
    {"library": "nautilus_trader", "version": "2.0.0rc8", "roles": ["newest 2.x"], "check": "nt-rc",
     "args": ["--nt-rc", "2.0.0rc8"]},
]  # fmt: skip


def test_run_targets_writes_the_result_and_one_log_per_target(tmp_path):
    runner = _fake_runner(
        {
            "hbt:2.4.4": {"status": "pass", "gates": [_gate("a", True), _gate("b", True)], "wall_s": 11.0},
            "nt-pinned:pinned": {"status": "fail", "gates": [_gate("touch", False, "3 disagree")], "wall_s": 9.5},
        }
    )
    a = _run_args(tmp_path)
    doc = C.run_targets(TARGETS, a, runner)
    up = tmp_path / "out" / "upload"
    assert json.loads((up / "compat-2026-11-01.json").read_text()) == doc
    assert doc["schema"] == C.SCHEMA_RESULT and doc["date"] == "2026-11-01" and doc["run_url"] == "https://ci/run/1"
    hbt, ntp, ntrc = doc["targets"]
    assert (hbt["status"], hbt["queuetruth"], len(hbt["gates"])) == ("pass", "0.2.0", 2)
    assert ntp["status"] == "fail" and ntp["gates"] == [_gate("touch", False, "3 disagree")]
    assert ntrc["status"] == "error" and "no result (exit 2)" in ntrc["reason"]
    assert hbt["log"] == "compat-2026-11-01-hftbacktest-2.4.4.log.txt"
    assert hbt["log_url"] == "https://github.com/o/r/releases/download/compat-results/" + hbt["log"]
    log = (up / hbt["log"]).read_text()
    assert "runner log" in log and "hbt log" in log and "runner says hello" in log
    assert "runner says hello" in (up / ntrc["log"]).read_text()
    argv = runner.calls[2]
    assert argv[argv.index("--only") + 1] == "nt-rc" and argv[-2:] == ["--nt-rc", "2.0.0rc8"]
    assert argv[argv.index("--qt-python") + 1] == "/qt/python" and argv[argv.index("--mode") + 1] == "reduced"
    assert {p.name for p in (tmp_path / "out" / "state").iterdir()} == {
        "hftbacktest-2.4.4", "nautilus_trader-1.231.0", "nautilus_trader-2.0.0rc8"}  # fmt: skip
    md = C.summary_markdown(doc)
    assert "| hftbacktest | 2.4.4 | pinned, newest 2.x | `hbt` | pass | 2 / 2 |" in md


def test_run_targets_writes_the_result_after_every_target_and_reports_a_timeout(tmp_path):
    a = _run_args(tmp_path)
    seen: list[int] = []
    inner = _fake_runner({"hbt:2.4.4": {"status": "pass", "gates": [_gate("a", True)]}, "nt-pinned:pinned": "timeout"})

    def runner(argv, log, timeout_s):
        assert timeout_s == 60.0
        result = tmp_path / "out" / "upload" / "compat-2026-11-01.json"
        seen.append(len(json.loads(result.read_text())["targets"]) if result.exists() else 0)
        return inner(argv, log, timeout_s)

    doc = C.run_targets(TARGETS[:2], a, runner)
    assert seen == [0, 1]  # the first version's result was on disk before the second one started
    assert doc["targets"][1]["status"] == "error"
    assert doc["targets"][1]["reason"].startswith("stopped at the 60 s time limit: ")


def test_run_with_timeout_stops_the_whole_process_group(tmp_path):
    log = tmp_path / "log.txt"
    assert C.run_with_timeout([sys.executable, "-c", "print('hi')"], log, 30) == 0
    assert "hi" in log.read_text()
    t0 = C.dt.datetime.now()
    rc = C.run_with_timeout(["/bin/sh", "-c", "sleep 60 & sleep 60; wait"], log, 0.5, grace_s=5)
    assert rc == C.TIMEOUT_RC and (C.dt.datetime.now() - t0).total_seconds() < 20
    assert "stopped at the 0.5 s time limit" in log.read_text()


def test_verdict_errors_only_for_a_pinned_version():
    doc = {"targets": [
        {"library": "hftbacktest", "version": "2.4.4", "roles": ["pinned"], "status": "pass", "gates": []},
        {"library": "nautilus_trader", "version": "2.0.0rc8", "roles": ["newest 2.x"], "status": "fail",
         "gates": [_gate("replica x", False)]},
    ]}  # fmt: skip
    rc, lines = C.verdict(doc)
    assert rc == 0
    assert lines == ["::warning title=compat::nautilus_trader 2.0.0rc8 (newest 2.x): fail: failed gates: replica x"]
    doc["targets"][0].update(status="error", reason="uv pip install failed")
    rc, lines = C.verdict(doc)
    assert rc == 1 and lines[0] == "::error title=compat::hftbacktest 2.4.4 (pinned): error: uv pip install failed"


def test_verdict_escapes_workflow_command_data():
    doc = {"targets": [{"library": "hftbacktest", "version": "2.4.5", "roles": ["newest 2.x"], "status": "error",
                        "gates": [], "reason": "50% done\r\n::error::injected"}]}  # fmt: skip
    rc, lines = C.verdict(doc)
    assert rc == 0 and len(lines) == 1 and "\n" not in lines[0] and "\r" not in lines[0]
    assert lines[0].endswith("error: 50%25 done%0D%0A::error::injected")


def _upload_dir(tmp_path, date="2026-11-01", **doc_kw):
    d = tmp_path / "artifact"
    d.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "library": "hftbacktest",
            "version": "2.4.4",
            "status": "pass",
            "log": f"compat-{date}-hftbacktest-2.4.4.log.txt",
        }
    ]
    doc = {"schema": C.SCHEMA_RESULT, "schema_version": 1, "date": date, "targets": rows, **doc_kw}
    (d / f"compat-{date}.json").write_text(json.dumps(doc))
    (d / f"compat-{date}-hftbacktest-2.4.4.log.txt").write_text("log\n")
    return d


def test_upload_files_takes_only_the_result_of_the_date_and_its_logs(tmp_path):
    d = _upload_dir(tmp_path)
    (d / "compat-2099-01-01.json").write_text(
        json.dumps({"schema": C.SCHEMA_RESULT, "date": "2099-01-01", "targets": []})
    )
    (d / "compat.html").write_text("<p>x</p>")
    (d / "compat-2026-11-01-extra.log.txt").write_text("not named by the result\n")
    assert [p.name for p in C.upload_files(d, "2026-11-01")] == [
        "compat-2026-11-01.json",
        "compat-2026-11-01-hftbacktest-2.4.4.log.txt",
    ]
    bad = _upload_dir(tmp_path / "c")
    doc = json.loads((bad / "compat-2026-11-01.json").read_text())
    (bad / "compat-2026-11-01.json").write_text(json.dumps({**doc, "date": "2099-01-01"}))
    with pytest.raises(ValueError, match="its date is '2099-01-01'"):
        C.upload_files(bad, "2026-11-01")
    (bad / "compat-2026-11-01.json").write_text(
        json.dumps({**doc, "targets": [{**doc["targets"][0], "log": "../x.log.txt"}]})
    )
    with pytest.raises(ValueError, match="is not a compat-2026-11-01"):
        C.upload_files(bad, "2026-11-01")
    (bad / "compat-2026-11-01.json").write_text(json.dumps({**doc, "targets": ["not an object"]}))
    with pytest.raises(ValueError, match="not a valid"):
        C.upload_files(bad, "2026-11-01")
    assert C.main(["select", str(tmp_path / "missing"), "--date", "2026-11-01"]) == 2


def test_stale_assets_only_dated_results_and_logs():
    names = [
        "compat-2026-07-01.json",
        "compat-2026-07-01-hftbacktest-2.4.4.log.txt",
        "compat-2026-08-03.json",
        "compat-2026-08-02.json",
        "compat.html",
        "day-2026-07-01.tar.gz",
        "compat-2026-07-01.json.bak",
    ]
    stale = C.stale_assets(names, dt.date(2026, 11, 1), 90)
    assert stale == ["compat-2026-07-01-hftbacktest-2.4.4.log.txt", "compat-2026-07-01.json", "compat-2026-08-02.json"]


# ---------------------------------------------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------------------------------------------


def _doc(date, rows, run_url="https://github.com/o/r/actions/runs/1"):
    return {"schema": C.SCHEMA_RESULT, "schema_version": 1, "date": date, "mode": "reduced", "run_url": run_url,
            "targets": rows}  # fmt: skip


def _t(lib, ver, status, roles=("pinned",), check="hbt", gates=None, **kw):
    gates = gates if gates is not None else [_gate("g1", True), _gate("g2", status == "pass", "1 of 53 differ")]
    row = {"library": lib, "version": ver, "roles": list(roles), "check": check, "status": status, "gates": gates}
    row.update(log=f"compat-x-{lib}-{ver}.log.txt", log_url=f"https://example.org/{lib}-{ver}.log.txt")
    return {**row, **kw}


@pytest.fixture
def results(tmp_path) -> Path:
    d = tmp_path / "results"
    d.mkdir()
    docs = {
        "compat-2026-10-31.json": _doc("2026-10-31", [
            _t("hftbacktest", "2.4.4", "pass", ("pinned", "newest 2.x")),
            _t("nautilus_trader", "2.0.0rc7", "fail", ("newest 2.x",), "nt-rc"),
        ]),
        "compat-2026-11-01.json": _doc("2026-11-01", [
            _t("hftbacktest", "2.4.4", "pass", ("pinned", "newest 2.x")),
            _t("nautilus_trader", "1.231.0", "pass", ("pinned",), "nt-pinned"),
            _t("nautilus_trader", "2.0.0rc10", "error", ("newest 2.x",), "nt-rc", gates=[],
               reason="no wheel <for> this & that"),
            _t("nautilus_trader", "2.0.0rc8", "pass", ("newest 2.x",), "nt-rc"),
        ]),
    }  # fmt: skip
    for name, doc in docs.items():
        (d / name).write_text(json.dumps(doc))
    (d / "compat-2026-10-30.json").write_text("{not json")
    (d / "compat-2026-10-29.json").write_text(json.dumps({"schema": "other"}))
    return d


def test_page_per_library_and_version_newest_first(results):
    docs, skipped = CS.load_results(results)
    assert [d["date"] for d in docs] == ["2026-10-31", "2026-11-01"]
    assert skipped == [
        "compat-2026-10-29.json: not a valid qt.compat.result document",
        "compat-2026-10-30.json: JSONDecodeError",
    ]
    page = CS.render(docs, skipped)
    assert page.startswith("<!doctype html>") and "<script" not in page.lower()
    assert "On 2026-11-01, 3 of 4 version(s) passed" in page
    nt = page.split("<h2>NautilusTrader</h2>", 1)[1].split("<h2>", 1)[0]
    order = [nt.index(f"<code>{v}</code>") for v in ("2.0.0rc10", "2.0.0rc8", "2.0.0rc7", "1.231.0")]
    assert order == sorted(order)  # version order (rc10 after rc8), not text order
    assert page.index("<h2>hftbacktest</h2>") < page.index("<h2>NautilusTrader</h2>")
    assert "no wheel &lt;for&gt; this &amp; that" in page and "<for>" not in page
    assert "g2: 1 of 53 differ" in page  # the failed gate of rc7, with its detail
    assert '<td class="fail">fail</td>' in page and '<td class="error">error</td>' in page
    assert '<a href="https://example.org/hftbacktest-2.4.4.log.txt">log</a>' in page
    assert '<a href="https://github.com/o/r/actions/runs/1">run</a>' in page
    every = page.split("<h2>Every result</h2>", 1)[1]
    assert every.count("<tr><td>2026-11-01</td>") == 4 and every.count("<tr><td>2026-10-31</td>") == 2
    assert every.index("2026-11-01") < every.index("2026-10-31")
    assert "Skipped: compat-2026-10-29.json" in page
    assert "var(--fg)" in page and "prefers-color-scheme: dark" in page  # the atlas CSS
    assert 'href="index.html"' not in page
    assert 'href="index.html"' in CS.render(docs, atlas_link=True)


def test_page_is_deterministic_and_handles_no_results(results, tmp_path):
    out1, out2 = tmp_path / "a" / "compat.html", tmp_path / "b" / "compat.html"
    assert CS.main(["--results", str(results), "--out", str(out1), "--atlas-link"]) == 0
    assert CS.main(["--results", str(results), "--out", str(out2), "--atlas-link"]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    empty = tmp_path / "c" / "compat.html"
    assert CS.main(["--results", str(tmp_path / "missing"), "--out", str(empty)]) == 0
    assert "No results yet." in empty.read_text()


def test_page_links_only_https_urls(tmp_path):
    row = _t("hftbacktest", "2.4.4", "pass", log_url="javascript:alert(1)")
    page = CS.render([_doc("2026-11-01", [row], run_url="")])
    assert "javascript:" not in page and ">log</a>" not in page


def test_page_skips_a_result_whose_targets_are_not_objects(results):
    (results / "compat-2026-11-02.json").write_text(json.dumps(_doc("2026-11-02", ["not an object", 3])))
    docs, skipped = CS.load_results(results)
    assert [d["date"] for d in docs] == ["2026-10-31", "2026-11-01"]
    assert "compat-2026-11-02.json: not a valid qt.compat.result document" in skipped
    assert "On 2026-11-01" in CS.render(docs, skipped)
