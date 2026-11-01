"""Backtrader and VectorBT from the CLI: `live import --venue sim-backtrader | sim-vectorbt` with `--option`, and
`queuetruth reconcile --kind backtrader | vectorbt` against a canonical live log. The CLI gives the same logs and
reports as the Python API on the committed fixtures (tests/fixtures/live/backtesters)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from queuetruth import cli
from queuetruth.live import read_log, table_rows
from queuetruth.live.importers import backtrader as bt
from queuetruth.live.importers import vectorbt as vb

pytestmark = pytest.mark.requires_core

FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "live" / "backtesters"
IMAP = FIX / "instrument_map.csv"


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return _run


@pytest.fixture
def live(tmp_path: Path, run: Any) -> Path:
    out = tmp_path / "live"
    code, _, err = run(
        "live", "import", "--venue", "binance", "--account", "main", "--in", str(FIX / "binance_user_stream.jsonl"),
        "--instrument-map", str(IMAP), "--out", str(out),
    )  # fmt: skip
    assert code == 0, err
    return out


def _rows(out: Path) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k != "raw"} for r in table_rows(read_log(out)[1])]


def test_live_import_backtester_venues_match_the_python_api(tmp_path: Path, run: Any, live: Path) -> None:
    code, out, err = run(
        "--format", "json", "live", "import", "--venue", "sim-backtrader", "--in", str(FIX / "bt_orders.jsonl"),
        "--out", str(tmp_path / "bt-cli"), "--instrument-map", str(IMAP), "--option", f"pair_with={live}",
        "--option", "timeframe=1h",
    )  # fmt: skip
    assert code == 0, err
    doc = json.loads(out)
    api = bt.import_backtrader(
        [str(FIX / "bt_orders.jsonl")], tmp_path / "bt-api", instrument_map=str(IMAP), pair_with=str(live),
        timeframe="1h",
    )  # fmt: skip
    assert (doc["venue"], doc["n_orders"], doc["validation_status"]) == ("sim:backtrader", api.n_orders, "simulated")
    assert _rows(tmp_path / "bt-cli") == _rows(tmp_path / "bt-api")
    code, out, err = run(
        "--format", "json", "live", "import", "--venue", "sim-vectorbt", "--in", str(FIX / "vbt_orders.csv"),
        "--in", str(FIX / "vbt_trades.csv"), "--out", str(tmp_path / "vbt"), "--instrument-map", str(IMAP),
        "--option", "venue=binance", "--option", "order_type=limit",
    )  # fmt: skip
    assert code == 0, err
    doc = json.loads(out)
    assert (doc["venue"], doc["n_orders"], doc["n_fills"]) == ("sim:vectorbt", 6, 6)
    assert any(w.startswith("sim:vectorbt: trade records: 3") for w in doc["warnings"])


def test_live_import_backtester_refusals(tmp_path: Path, run: Any) -> None:
    code, out, _ = run(
        "--format", "json", "live", "import", "--venue", "sim-vectorbt", "--in", str(FIX / "vbt_orders.csv"),
        "--out", str(tmp_path / "o"), "--option", "order_type=stop",
    )  # fmt: skip
    assert code == 2 and json.loads(out)["code"] == "QT-CONFIG-001"
    code, out, _ = run(
        "--format", "json", "live", "import", "--venue", "sim-backtrader", "--in", str(FIX / "vbt_orders.csv"),
        "--out", str(tmp_path / "p"),
    )  # fmt: skip
    assert code == 2 and json.loads(out)["code"] == "QT-INPUT-002"


def test_reconcile_backtesters_match_the_python_api(tmp_path: Path, run: Any, live: Path) -> None:
    for kind, sim, fn in (
        ("backtrader", FIX / "bt_orders.jsonl", bt.reconcile_backtrader),
        ("vectorbt", FIX / "vbt_orders.csv", vb.reconcile_vectorbt),
    ):
        code, out, err = run(
            "--format", "json", "reconcile", "--kind", kind, "--sim", str(sim), "--live", str(live),
            "--out", str(tmp_path / f"{kind}-cli"), "--option", "timeframe=1h",
        )  # fmt: skip
        assert code == 0, err
        doc = json.loads(out)
        api = fn(sim, live, tmp_path / f"{kind}-api", timeframe="1h")
        assert doc["schema"] == "qt.bot_reconcile" and doc["kind"] == kind
        assert doc["counts"] == api.counts and doc["run_id"] == api.run_id and doc["n_paired"] == 6
    code, out, err = run(
        "reconcile", "--kind", "vectorbt", "--sim", str(FIX / "vbt_orders.csv"), "--live", str(live),
        "--out", str(tmp_path / "human"), "--option", "timeframe=1h", "--max-rows", "2",
    )  # fmt: skip
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0] == "reconcile (vectorbt): 7 live orders, 6 simulator orders, 6 paired (86% of live orders)"
    assert out.rstrip().endswith(f"5 more orders in {tmp_path / 'human'}/reconcile_orders.csv")


def test_reconcile_backtester_errors(tmp_path: Path, run: Any, live: Path) -> None:
    code, out, _ = run(
        "--format", "json", "reconcile", "--kind", "backtrader", "--sim", str(FIX / "bt_orders.jsonl"),
        "--live", str(FIX / "binance_user_stream.jsonl"), "--out", str(tmp_path / "a"),
    )  # fmt: skip
    assert code == 2 and json.loads(out)["code"] == "QT-INPUT-001"
    code, out, _ = run(
        "--format", "json", "reconcile", "--kind", "vectorbt", "--sim", str(FIX / "vbt_orders.csv"),
        "--live", str(live), "--out", str(tmp_path / "b"), "--option", "strategy=S",
    )  # fmt: skip
    assert code == 2 and json.loads(out)["code"] == "QT-CONFIG-001"
    code, out, _ = run("help", "reconcile")
    assert code == 0 and "backtrader" in out and "vectorbt" in out


def test_backtest_against_exact_queue_shadows(tmp_path: Path, run: Any) -> None:
    """The guide's exact-queue section: a VectorBT export that fills every order of the synthetic live log at its
    limit price, scored next to the exact FIFO arm on the synthetic dataset."""
    import csv
    import datetime as dt

    from queuetruth import api

    api.fixtures_synth(str(tmp_path / "synth"))
    live = tmp_path / "synth" / "own_orders" / "live"
    orders, events = (table_rows(t) for t in read_log(live))
    ack = {e["order_key"]: e["ts_exchange"] for e in events if e["event_type"] == "ack"}
    vbt = tmp_path / "vbt_orders.csv"
    with vbt.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Order Id", "Column", "Timestamp", "Size", "Price", "Fees", "Side"])
        for i, o in enumerate(sorted(orders, key=lambda o: ack[o["order_key"]])):
            t = ack[o["order_key"]]
            stamp = dt.datetime.fromtimestamp(t // 10**9, dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
            w.writerow(
                [i, o["instrument"], f"{stamp}.{t % 10**9:09d}+00:00", o["qty_str"], o["px_str"], 0,
                 o["side"].capitalize()]
            )  # fmt: skip
    code, out, err = run(
        "--format", "json", "live", "import", "--venue", "sim-vectorbt", "--in", str(vbt),
        "--out", str(tmp_path / "vbt-sim"), "--option", f"pair_with={live}", "--option", "order_type=limit",
        "--option", "instruments=QTAU6:QTAU6,QTBU6:QTBU6", "--option", "size_exp=0",
    )  # fmt: skip
    assert code == 0, err
    assert any("200 of 200 simulator order(s) paired" in w for w in json.loads(out)["warnings"])
    code, out, err = run(
        "--format", "json", "score", "--live", str(live), "--sim-log", str(tmp_path / "vbt-sim"),
        "--data", str(tmp_path / "synth"), "--out", str(tmp_path / "sc-vbt"),
    )  # fmt: skip
    assert code == 0, err
    doc = json.loads(out)
    assert doc["two_by_two"] == {"both": 112, "neither": 0, "live_only": 0, "sim_only": 88}
    assert doc["verdict"]["headline"].startswith("sim overfills live by 44.0 pp")
    code, out, err = run(
        "--format", "json", "score", "--live", str(live), "--sim", "exact", "--data", str(tmp_path / "synth"),
        "--out", str(tmp_path / "sc-exact"),
    )  # fmt: skip
    assert code == 0, err
    assert json.loads(out)["two_by_two"] == {"both": 112, "neither": 88, "live_only": 0, "sim_only": 0}
