"""O-PARITY-1: the same spec through `qt.replay()`, `queuetruth replay --format json` and `queuetruth-core replay`
gives identical summaries (excluding `timings` and path fields) and identical output sha256s; the same for `zoo` and
`score` (Python API vs CLI). The HTTP arm is `test_service_gate.py`. Every document also validates (O-SCHEMA-3)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from e2e_support import SYNTH, core_binary, run_cli, schema_validate, strip_paths, tree_digests

pytestmark = [pytest.mark.integration, pytest.mark.requires_core]


def _canon(doc: Any) -> str:
    return json.dumps(strip_paths(doc), sort_keys=True)


@pytest.fixture(scope="module")
def work(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import queuetruth as qt

    w = tmp_path_factory.mktemp("parity")
    shutil.copytree(SYNTH, w / "ds", ignore=shutil.ignore_patterns("truth", "own_orders"))
    qt.sample_shadows({"dataset": {"path": str(w / "ds")}, "out": str(w / "sh.parquet"), "n": 120, "seed": 5})
    return w


def test_replay_api_cli_core_identical(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth as qt

    monkeypatch.chdir(work)
    flags = ["--data", "ds", "--shadows", "sh.parquet", "--rank-oracle", "--parity-trades", "--bbo", "change"]
    api = qt.replay(
        {
            "dataset": {"path": "ds"},
            "out": "r_api",
            "shadows": {"path": "sh.parquet"},
            "rank_oracle": True,
            "parity_trades": True,
            "bbo": "change",
        }
    ).model_dump(mode="json", by_alias=True)
    cli = run_cli("replay", *flags, "--out", "r_cli", cwd=work)
    docs = {"api": api, "cli": cli}
    core = core_binary()
    if core is not None:
        docs["core"] = run_cli("replay", *flags, "--out", "r_core", binary=core, cwd=work)
    for d in docs.values():
        schema_validate(d, "qt.replay_summary")
    assert len({_canon(d) for d in docs.values()}) == 1, {k: _canon(v)[:400] for k, v in docs.items()}
    assert len({d["run_id"] for d in docs.values()}) == 1
    digests = {k: tree_digests(work / f"r_{k}") for k in docs}
    assert len({json.dumps(d, sort_keys=True) for d in digests.values()}) == 1, digests
    if core is None:
        pytest.skip("queuetruth-core binary not built: API and CLI arms compared only")


def test_zoo_api_cli_identical(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth as qt

    monkeypatch.chdir(work)
    models = ["front_of_queue", "hbt_power3[n=2]", "hbt_l3_fifo"]
    api = qt.run_zoo(
        {
            "dataset": {"path": "ds"},
            "shadows": {"path": "sh.parquet"},
            "out": "z_api",
            "models": models,
            "bootstrap_b": 20,
        }
    ).model_dump(mode="json", by_alias=True)
    cli = run_cli(
        "zoo", "--data", "ds", "--shadows", "sh.parquet", "--out", "z_cli", "--models", ",".join(models),
        "--bootstrap-b", "20", cwd=work,
    )  # fmt: skip
    schema_validate(api, "qt.zoo_summary")
    schema_validate(cli, "qt.zoo_summary")
    assert _canon(api) == _canon(cli)
    assert tree_digests(work / "z_api") == tree_digests(work / "z_cli")


def test_score_api_cli_identical(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth as qt

    monkeypatch.chdir(work)
    live = str(SYNTH / "own_orders" / "live")
    api = qt.score(
        {
            "live": {"path": live},
            "sim": {"kind": "exact"},
            "dataset": {"path": "ds"},
            "out": "s_api",
            "bootstrap": {"b": 50},
            "perm_b": 50,
        }
    ).model_dump(mode="json", by_alias=True)
    cli = run_cli(
        "score", "--live", live, "--sim", "exact", "--data", "ds", "--out", "s_cli", "--bootstrap-b", "50",
        "--perm-b", "50", cwd=work,
    )  # fmt: skip
    schema_validate(api, "qt.scorecard_summary")
    schema_validate(cli, "qt.scorecard_summary")
    assert _canon(api) == _canon(cli)
    assert tree_digests(work / "s_api") == tree_digests(work / "s_cli")
