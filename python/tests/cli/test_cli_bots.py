"""Freqtrade and Hummingbot from the CLI: `live import --venue freqtrade | hummingbot | sim-freqtrade |
sim-hummingbot` with `--option`, and `queuetruth reconcile` (docs/design/extensions/freqtrade-hummingbot.md). The
CLI gives the same logs and reports as the Python API on the committed fixtures."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from queuetruth import cli
from queuetruth.live import LiveImportWarning
from queuetruth.live.importers import freqtrade as ft
from queuetruth.live.importers import hummingbot as hb

pytestmark = pytest.mark.requires_core

ROOT = Path(__file__).resolve().parents[3]
FT = ROOT / "tests" / "fixtures" / "live" / "freqtrade"
HB = ROOT / "tests" / "fixtures" / "live" / "hummingbot"
ZIP = FT / "backtest-result-2024-03-01_14-00-00.zip"


def _db(tmp: Path, fix: Path, name: str) -> Path:
    out = tmp / f"{name}.sqlite"
    if not out.exists():
        con = sqlite3.connect(out)
        con.executescript((fix / f"{name}.sql").read_text())
        con.close()
    return out


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


def test_live_import_bot_venues_match_the_python_api(tmp_path: Path, run: Any) -> None:
    db = _db(tmp_path, FT, "tradesv3.dryrun")
    code, out, err = run(
        "--format", "json", "live", "import", "--venue", "freqtrade", "--in", str(db),
        "--in", str(FT / "freqtrade.log"), "--out", str(tmp_path / "ft-cli"),
        "--instrument-map", str(FT / "instrument_map.csv"),
    )  # fmt: skip
    assert code == 0, err
    doc = json.loads(out)
    api = ft.import_freqtrade_live(
        [str(db), str(FT / "freqtrade.log")], tmp_path / "ft-api", instrument_map=str(FT / "instrument_map.csv")
    )
    assert doc["n_orders"] == api.n_orders > 0 and doc["validation_status"] == api.validation_status == "simulated"
    # the simulator arm, with an importer option
    code, out, err = run(
        "--format", "json", "live", "import", "--venue", "sim-freqtrade", "--in", str(ZIP),
        "--out", str(tmp_path / "ft-sim"), "--instrument-map", str(FT / "instrument_map.csv"),
        "--option", "venue=binance",
    )  # fmt: skip
    assert code == 0, err
    assert json.loads(out)["venue"] == "sim:freqtrade"
    # Hummingbot live database
    hdb = _db(tmp_path, HB, "conf_pure_mm_1")
    code, out, err = run(
        "--format", "json", "live", "import", "--venue", "hummingbot", "--in", str(hdb), "--out", str(tmp_path / "hb")
    )  # fmt: skip
    assert code == 0, err
    with pytest.warns(LiveImportWarning, match="unmapped_instruments"):  # no instrument map, as on the CLI above
        api_hb = hb.import_hummingbot_live([str(hdb)], tmp_path / "hb-api")
    assert json.loads(out)["n_orders"] == api_hb.n_orders


def test_live_import_bot_refusals(tmp_path: Path, run: Any) -> None:
    db = _db(tmp_path, FT, "tradesv3.dryrun")
    code, out, _ = run(
        "--format", "json", "live", "import", "--venue", "freqtrade", "--in", str(db), "--out", str(tmp_path / "o"),
        "--tz", "Europe/Berlin",
    )  # fmt: skip
    assert code == 2 and json.loads(out)["code"] == "QT-CONFIG-001" and "log_tz" in json.loads(out)["message"]
    code, out, _ = run(
        "--format", "json", "live", "import", "--venue", "binance", "--in", str(db), "--out", str(tmp_path / "p"),
        "--option", "dry_run=true",
    )  # fmt: skip
    assert code == 2 and json.loads(out)["code"] == "QT-USAGE-001"


def test_reconcile_freqtrade_matches_the_python_api(tmp_path: Path, run: Any) -> None:
    db = _db(tmp_path, FT, "tradesv3.dryrun")
    code, out, err = run(
        "--format", "json", "reconcile", "--kind", "freqtrade", "--sim", str(ZIP), "--live", str(db),
        "--live", str(FT / "freqtrade.log"), "--out", str(tmp_path / "cli"),
        "--instrument-map", str(FT / "instrument_map.csv"),
    )  # fmt: skip
    assert code == 0, err
    doc = json.loads(out)
    assert doc["schema"] == "qt.bot_reconcile"
    api = ft.reconcile_freqtrade(
        ZIP, [db, FT / "freqtrade.log"], tmp_path / "api", instrument_map=FT / "instrument_map.csv"
    )
    assert doc["counts"] == api.counts and (doc["n_live_orders"], doc["n_paired"]) == (13, 7)
    assert json.loads((tmp_path / "cli" / "reconcile.json").read_text())["counts"] == api.counts
    # human output: the answer first, then one line per order
    code, out, err = run(
        "reconcile", "--kind", "freqtrade", "--sim", str(ZIP), "--live", str(db), "--live", str(FT / "freqtrade.log"),
        "--out", str(tmp_path / "cli2"), "--instrument-map", str(FT / "instrument_map.csv"), "--max-rows", "3",
    )  # fmt: skip
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("reconcile (freqtrade): 13 live orders, 10 simulator orders, 7 paired")
    assert out.rstrip().endswith("more orders in " + str(tmp_path / "cli2") + "/reconcile_orders.csv")


def test_reconcile_hummingbot_options_and_errors(tmp_path: Path, run: Any) -> None:
    db, paper = _db(tmp_path, HB, "conf_pure_mm_1"), _db(tmp_path, HB, "conf_pure_mm_1_paper")
    code, out, err = run(
        "--format", "json", "reconcile", "--kind", "hummingbot", "--sim", str(paper), "--live", str(db),
        "--out", str(tmp_path / "hb"), "--window", "30s",
    )  # fmt: skip
    assert code == 0, err
    api = hb.reconcile_hummingbot(paper, db, tmp_path / "hb-api", window_ns=30_000_000_000)
    assert json.loads(out)["counts"] == api.counts
    code, out, _ = run("--format", "json", "reconcile", "--kind", "hummingbot", "--sim", str(paper), "--live", str(db))
    assert code == 2 and json.loads(out)["code"] == "QT-USAGE-001"
    code, out, _ = run(
        "--format", "json", "reconcile", "--kind", "freqtrade", "--sim", str(ZIP), "--live", str(db),
        "--out", str(tmp_path / "x"), "--option", "nonsense",
    )  # fmt: skip
    assert code == 2 and "KEY=VALUE" in json.loads(out)["message"]
    code, out, _ = run("help", "reconcile")
    assert code == 0 and "--kind" in out
