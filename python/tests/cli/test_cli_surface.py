"""The CLI surface of plan section 4.1: `queuetruth --help` lists the six verbs, the helpers and `advanced`, and
nothing else; every other command runs the same under `queuetruth advanced CMD` and under its old top-level
spelling, which adds one deprecation line on stderr (none with `--quiet` or `QT_NO_DEPRECATION=1`)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from queuetruth import _core, cli

pytestmark = pytest.mark.requires_core

VERBS = ["demo", "atlas", "check", "fit", "compare", "data"]
HELPERS = ["why", "report", "verify"]


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


def _commands(help_text: str) -> list[str]:
    block = help_text.split("Commands:\n", 1)[1].split("\n\n", 1)[0]
    return [m.group(1) for m in re.finditer(r"^  (\S+)", block, flags=re.M)]


def test_help_lists_the_verbs_the_helpers_and_advanced(run: Any) -> None:
    code, out, _ = run("--help")
    assert code == 0
    present = [v for v in VERBS if f"\n  {v} " in out]
    assert present == [v for v in VERBS if v != "compare" or "compare" in present]
    assert _commands(out) == [*present, *HELPERS, "advanced", "help"]
    assert "why       event-by-event trace" in out and "[alias: explain]" in out


def test_help_advanced_lists_the_rest(run: Any) -> None:
    code, out, _ = run("help", "advanced")
    assert code == 0
    listed = _commands(out)
    for word in ("ingest", "replay", "shadows", "zoo", "zoo-toxicity", "calibrate", "calibrate-live", "score",
                 "live", "dq", "referee", "fixtures", "export", "jobs", "schema", "doctor", "mcp", "forecast",
                 "ledger", "pm", "certify", "verify-cert", "commit", "reveal", "cert", "reconcile", "features",
                 "fit-hazard"):  # fmt: skip
        assert word in listed, word
    assert not set(listed) & {*VERBS, *HELPERS}
    code2, out2, _ = run("advanced", "--help")
    assert (code2, _commands(out2)) == (0, listed)


def test_help_verb_and_explain(run: Any) -> None:
    code, out, _ = run("help", "check")
    assert code == 0 and "usage: queuetruth check" in out
    code, out, _ = run("help", "explain")
    assert code == 0 and "event-by-event trace of one shadow or live order" in out
    code, out, _ = run("help", "advanced", "zoo")
    assert code == 0 and "--shadows" in out and "--models" in out
    # help is the same under both spellings, without a deprecation line
    assert run("zoo", "--help") == run("advanced", "zoo", "--help") == (0, out, "")


DEPRECATION = re.compile(r"^warning: `queuetruth [^`]+` is deprecated; .*\(QT_NO_DEPRECATION=1 hides this line\)$")


def _stable(out: str) -> str:
    """Output without the wall-clock timings (the only field that differs between two runs)."""
    out = re.sub(r'"timings":\{[^{}]*\}', '"timings":{}', out)
    return "\n".join(x for x in out.splitlines() if not x.startswith("timings."))


def _same(a: tuple[int, str, str], b: tuple[int, str, str], line: str | None) -> None:
    assert a[0] == b[0]
    assert _stable(a[1]) == _stable(b[1])
    old = a[2].splitlines()
    assert old[-1] == line if line else not any(DEPRECATION.match(x) for x in old)
    assert (old[:-1] if line else old) == b[2].splitlines()


def test_advanced_commands_run_the_same_under_both_spellings(
    synth: Path, tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QT_NO_DEPRECATION", raising=False)
    monkeypatch.chdir(tmp_path)
    ds = str(synth)
    cases: list[tuple[list[str], str | None]] = [
        (
            ["schema", "list"],
            "warning: `queuetruth schema list` is deprecated; use `queuetruth advanced schema list` "
            "(QT_NO_DEPRECATION=1 hides this line)",
        ),
        (
            ["--format", "json", "shadows", "sample", "--data", ds, "--n", "20", "--seed", "3", "--out", "sh.parquet"],
            "warning: `queuetruth shadows sample` is deprecated; use `queuetruth check`, or `queuetruth advanced "
            "shadows sample` for this command (QT_NO_DEPRECATION=1 hides this line)",
        ),
        (
            ["replay", "--data", ds, "--shadows", "sh.parquet", "--out", "replay"],
            "warning: `queuetruth replay` is deprecated; use `queuetruth advanced replay` "
            "(QT_NO_DEPRECATION=1 hides this line)",
        ),
        (
            [
                "--format",
                "json",
                "zoo",
                "--data",
                ds,
                "--shadows",
                "sh.parquet",
                "--out",
                "zoo",
                "--models",
                "hbt_power3",
                "--bootstrap-b",
                "50",
            ],
            "warning: `queuetruth zoo` is deprecated; use `queuetruth check`, or `queuetruth advanced zoo` for this "
            "command (QT_NO_DEPRECATION=1 hides this line)",
        ),
        (
            ["--format", "json", "dq", "--data", ds],
            "warning: `queuetruth dq` is deprecated; use `queuetruth data add`, or `queuetruth advanced dq` for this "
            "command (QT_NO_DEPRECATION=1 hides this line)",
        ),
        (
            ["live", "validate", "--live", "no-such-dir"],
            "warning: `queuetruth live validate` is deprecated; use `queuetruth advanced live validate` "
            "(QT_NO_DEPRECATION=1 hides this line)",
        ),
        (["--format", "json", "doctor"], None),
    ]
    for argv, line in cases:
        i = next(k for k, w in enumerate(argv) if not w.startswith("-") and (k == 0 or argv[k - 1] != "--format"))
        old = run(*argv)
        new = run(*argv[:i], "advanced", *argv[i:])
        _same(old, new, line)
        if line:
            assert DEPRECATION.match(line)


def test_deprecation_names_the_verb_that_replaces_it() -> None:
    def line(*argv: str) -> str | None:
        return _core.cli_route(["queuetruth", *argv])[1]

    assert "use `queuetruth fit`, or `queuetruth advanced calibrate alloc`" in (line("calibrate", "alloc") or "")
    assert "use `queuetruth fit`" in (line("calibrate-live") or "")
    assert "use `queuetruth fit`" in (line("export", "nautilus") or "")
    assert "use `queuetruth advanced export l2-npz`" in (line("export", "l2-npz") or "")
    assert "use `queuetruth data add`" in (line("--format", "json", "ingest", "--in", "x") or "")
    assert "use `queuetruth check`" in (line("zoo-toxicity") or "")
    assert "use `queuetruth verify cert`" in (line("verify-cert", "c.json") or "")
    compare = "use `queuetruth compare`" if _has("compare") else "use `queuetruth advanced"
    for argv in (["score"], ["reconcile"], ["live", "import"], ["shadows", "from-live"]):
        assert compare in (line(*argv) or ""), argv
    for argv in (["doctor"], ["mcp", "serve"], ["advanced", "zoo"], ["check", "x"], ["why"], ["explain"], ["help"]):
        assert line(*argv) is None, argv


def _has(word: str) -> bool:
    try:
        _core.cli_parse(["queuetruth", word, "--help"])
    except _core.CliExit as x:
        return int(x.code) == 0
    return True


def test_deprecation_line_is_suppressed(synth: Path, tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QT_NO_DEPRECATION", raising=False)
    argv = ["schema", "list"]
    code, out, err = run(*argv)
    assert code == 0 and DEPRECATION.match(err.splitlines()[-1])
    code, out_q, err_q = run("--quiet", *argv)
    assert (code, out_q) == (0, out) and "deprecated" not in err_q
    monkeypatch.setenv("QT_NO_DEPRECATION", "1")
    code, out_e, err_e = run(*argv)
    assert (code, out_e) == (0, out) and "deprecated" not in err_e
    # errors keep their exit code and JSON document; the line still follows on stderr
    monkeypatch.delenv("QT_NO_DEPRECATION")
    code, out_j, err_j = run("--format", "json", "replay", "--data", str(tmp_path / "missing"))
    assert code == 2 and '"schema": "qt.error"' in out_j.replace('":"', '": "')
    assert DEPRECATION.match(err_j.splitlines()[-1])
