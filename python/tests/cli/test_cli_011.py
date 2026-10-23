"""0.1.1 command-line fixes: `--help` shows defaults (the factory ones equal the specs'), unit-less durations are
rejected on flags whose name does not say ns, `zoo --list-models` and "did you mean", and the zoo's stderr collapses
the faithful hbt #312 abort lines into one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from queuetruth import cli
from queuetruth.schemas import specs as s

pytestmark = pytest.mark.requires_core


def _help(*path: str) -> str:
    from queuetruth import _core as core

    with pytest.raises(core.CliExit) as e:
        core.cli_parse(["queuetruth", *path, "--help"])
    return str(e.value.text)


def _dur(ns: int) -> str:
    for unit, size in (("m", 60_000_000_000), ("s", 1_000_000_000), ("ms", 1_000_000), ("us", 1_000)):
        if ns and ns % size == 0 and (unit != "m" or ns >= 2 * size):
            return f"{ns // size}{unit}"
    return str(ns)


def test_help_shows_defaults_equal_to_the_specs() -> None:
    zoo = _help("zoo")
    z = s.ZooSpec(dataset=s.PathRef(path="d"), shadows=s.PathRef(path="x"))
    assert f"[default: {','.join(z.models)}]" in zoo
    assert f"[default: {','.join(_dur(h) for h in z.horizons_ns)} (h_max is always added)]" in zoo
    assert f"[default: {_dur(z.h_max_ns)}]" in zoo and "[default: faithful]" in zoo
    sample = _help("shadows", "sample")
    sp = s.ShadowSampleSpec(dataset=s.PathRef(path="d"), n=1)
    assert "--lifetime <DURATION>" in sample and f"[default: {_dur(sp.lifetime_ns)}]" in sample
    assert f"[default: {','.join(sp.sides)}]" in sample
    assert "[default: " + ",".join(f"{p.offset_ticks}:{p.weight:g}" for p in sp.placements) + "]" in sample
    qty = ",".join(f"lots:{q.lots}:{q.weight:g}" if q.kind == "lots" else f"median:{q.weight:g}" for q in sp.qty)
    assert f"[default: {qty}]" in sample
    feat = _help("features")
    fs = s.FeatureSpec(dataset=s.PathRef(path="d"))
    assert f"[default: {','.join(_dur(x) for x in fs.landmarks_ns)}]" in feat
    assert f"[default: {','.join(fs.trackers)}]" in feat


@pytest.mark.parametrize(
    "argv",
    [
        ["shadows", "sample", "--data", "d", "--n", "5", "--out", "o", "--lifetime", "5"],
        ["zoo", "--data", "d", "--shadows", "s", "--out", "o", "--horizons", "1s,5"],
        ["zoo", "--data", "d", "--shadows", "s", "--out", "o", "--latency", "0,5,0"],
    ],
)
def test_unit_less_durations_are_rejected(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == 2
    assert '"5" has no unit; write 5s, 5ms, 5us or 5ns' in capsys.readouterr().err


def test_unit_less_zero_and_ns_flags_still_parse() -> None:
    from queuetruth import _core as core

    cmd = json.loads(
        core.cli_parse(["queuetruth", "shadows", "sample", "--data", "d", "--n", "5", "--out", "o", "--lifetime", "0"])
    )
    assert cmd["args"]["lifetime_ns"] == 0
    cmd = json.loads(
        core.cli_parse(["queuetruth", "zoo", "--data", "d", "--shadows", "s", "--out", "o", "--jitter-ns", "5"])
    )
    assert cmd["args"]["latency"]["jitter_ns"] == 5


def test_list_models(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["zoo", "--list-models"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("--models all (16 models;")
    assert "hbt_power3[n=2]" in out and "nt_queue_l2_prob_touch[p=P]" in out
    assert cli.main(["zoo", "--list-models", "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["all"]) == 16 and "hbt_power[n=1]" not in {m["id"] for m in doc["all"]}


def test_model_typo_suggests(repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    synth = repo_root / "tests" / "fixtures" / "synth_small"
    argv = ["zoo", "--data", str(synth), "--shadows", "nope.parquet", "--out", str(tmp_path / "z")]
    assert cli.main([*argv, "--models", "hbt_powr3[n=2]"]) == 2
    err = capsys.readouterr().err
    assert 'did you mean "hbt_power3"?' in err and "--list-models" in err


def _abort(model: str, n: int) -> str:
    return (
        f"hbt_312_abort: {model}: {n} of 1000 shadow pair(s) aborted at the #312 InvalidOrderStatus event "
        "(faithful hftbacktest stops there), first at shadow s1 ts 1; their predictions from the abort on carry "
        "status hbt_312_abort and are left out of the metrics"
    )


def test_stderr_collapses_hbt_312_and_hides_notes() -> None:
    doc: dict[str, Any] = {
        "warnings": [
            _abort("hbt_log[exch=partial;mode=faithful]", 79),
            "something real",
            _abort("hbt_power[exch=partial;mode=faithful;n=2]", 72),
            "info: a note",
            "ci_block=30min",
            "shadow diagnostics (fill rule Exact): hidden_print_fills=0 taker_fills=0",
        ],
        "n_excluded_hbt_312": 85,
    }
    lines = cli.stderr_lines(doc, verbose=False)
    assert lines == [
        "warning: hftbacktest bug #312 (https://github.com/nkaz001/hftbacktest/issues/312): 2 faithful hbt model(s) "
        "aborted on 72 to 79 of 1000 shadows; the 85 shadows involved are left out of every model (--verbose lists "
        "each model)",
        "warning: something real",
    ]
    loud = cli.stderr_lines(doc, verbose=True)
    assert (
        len(loud) == 6
        and "info: a note" in loud
        and f"warning: {_abort('hbt_log[exch=partial;mode=faithful]', 79)}" in loud
    )
    nonzero = {"warnings": ["shadow diagnostics (fill rule Exact): hidden_print_fills=3 taker_fills=0"]}
    assert cli.stderr_lines(nonzero, verbose=False) == [f"warning: {nonzero['warnings'][0]}"]
