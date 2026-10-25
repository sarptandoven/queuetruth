"""The Python-only command groups (demo, forecast, ledger, pm and the certificate commands) in the clap grammar:
the generated rows match their argparse parsers, `queuetruth --help` lists them, `help G [SUB]` equals
`G [SUB] --help`, and the commands still run through their own parsers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from queuetruth import cli

pytestmark = pytest.mark.requires_core

GROUPS = ("demo", "forecast", "ledger", "pm", "certify", "verify-cert", "commit", "reveal", "cert")


@pytest.fixture(scope="module")
def gen(repo_root: Path) -> ModuleType:
    path = repo_root / "crates" / "cli" / "gen_python_commands.py"
    spec = importlib.util.spec_from_file_location("gen_python_commands", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def test_generated_rows_are_current(gen: ModuleType) -> None:
    assert gen.generate() == gen.OUT.read_text(encoding="utf-8"), "rerun python crates/cli/gen_python_commands.py"


def test_every_argparse_option_is_in_the_clap_help(gen: ModuleType) -> None:
    import argparse

    from queuetruth import _core

    def leaves(p: argparse.ArgumentParser, path: list[str]) -> Any:
        sub = gen.subparsers(p)
        if sub is None:
            yield path, p
            return
        for name, child in sub.choices.items():
            yield from leaves(child, [*path, name])

    n = 0
    for prefix, parser in gen.parsers():
        for path, p in leaves(parser, prefix):
            with pytest.raises(_core.CliExit) as x:
                _core.cli_parse(["queuetruth", *path, "--help"])
            text = x.value.text
            for a in gen.positionals(p):
                shown = gen.positional_name(a).removesuffix("...")
                assert f"<{shown}>" in text or f"[{shown}]" in text, (path, a.dest)
                if a.help:
                    assert a.help in text, (path, a.dest)
            for a in p._actions:
                if not a.option_strings or a.help == argparse.SUPPRESS:
                    continue
                if set(a.option_strings) & gen.GLOBAL_OPTIONS:
                    assert all(o in text for o in a.option_strings if o != "--output-format"), (path, a)
                    continue
                for o in a.option_strings:
                    assert o in text, (path, o)
                n += 1
    assert n > 200


def test_root_help_lists_every_group(run: Any) -> None:
    code, out, _ = run("--help")
    assert code == 0
    listed = {line.split()[0] for line in out.splitlines() if line.startswith("  ") and line.split()}
    assert set(GROUPS) <= listed, set(GROUPS) - listed


@pytest.mark.parametrize(
    "words",
    [
        ["demo"],
        ["forecast"],
        ["forecast", "score"],
        ["ledger", "verify"],
        ["pm"],
        ["pm", "score"],
        ["verify-cert"],
        ["cert", "partial-opening"],
    ],
)
def test_help_word_equals_help_flag(run: Any, words: list[str]) -> None:
    a = run("help", *words)
    b = run(*words, "--help")
    assert a[0] == 0 and a == b
    assert a[1].startswith("usage: queuetruth"), a[1][:80]  # the group's own argparse help


def test_help_after_global_flags(run: Any) -> None:
    assert run("--quiet", "--seed", "3", "help", "forecast", "power") == run("forecast", "power", "--help")


def test_groups_still_run_through_their_parsers(run: Any) -> None:
    code, out, _ = run("--format", "json", "forecast", "power", "--edge", "0.02", "--sd", "0.2")
    assert code == 0 and json.loads(out)["n_required"] > 0
    code, _, err = run("ledger", "list", "--kind", "nope")
    assert code == 2 and "QT-USAGE-001" in err
