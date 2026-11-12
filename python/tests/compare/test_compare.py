"""`queuetruth compare` on the synthetic fixtures the reconcile and score tests use: the backtester and the kind of
live run read from the files, the path each pair takes (the reconciler for bots and bar backtesters, the scorer
for event-driven simulators, with `--data` also markouts, P&L per order and the exact arm), the answer lines, the
order table and the error paths."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import queuetruth as qt
from queuetruth import cli
from queuetruth.compare import CompareResult, compare, detect_backtest, detect_live
from queuetruth.errors import QtError
from queuetruth.live.importers import freqtrade as ft
from queuetruth.live.importers import hummingbot as hb
from queuetruth.live.importers import lean
from queuetruth.live.importers import vectorbt as vb

from . import synth_sims as ss

#: the reference imports below (`live import` without a map, as the reconcile tests do) warn about it on purpose
pytestmark = [pytest.mark.requires_core, pytest.mark.filterwarnings("ignore::queuetruth.live.LiveImportWarning")]

REPO = Path(__file__).resolve().parents[3]
FIX = REPO / "tests" / "fixtures" / "live"
BTF = FIX / "backtesters"
FT = FIX / "freqtrade"
HB = FIX / "hummingbot"
DATA = REPO / "tests" / "fixtures" / "synth_small"
LIVE = DATA / "own_orders" / "live"
STREAM = BTF / "binance_user_stream.jsonl"
FT_ZIP = FT / "backtest-result-2024-03-01_14-00-00.zip"


def _db(tmp: Path, fix: Path, name: str) -> Path:
    out = tmp / f"{name}.sqlite"
    if not out.exists():
        con = sqlite3.connect(out)
        con.executescript((fix / f"{name}.sql").read_text())
        con.close()
    return out


@pytest.fixture(scope="module")
def dbs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("db")
    return {
        "ft_live": _db(d, FT, "tradesv3.live"),
        "ft_dry": _db(d, FT, "tradesv3.dryrun"),
        "hb_live": _db(d, HB, "conf_pure_mm_1"),
        "hb_paper": _db(d, HB, "conf_pure_mm_1_paper"),
    }


@pytest.fixture(scope="module")
def sims(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[Path]]:
    d = tmp_path_factory.mktemp("sims")
    os_ = ss.sim_orders(LIVE)
    return {"nautilus": ss.write_nautilus(d / "nt", os_), "hftbacktest": [ss.write_hftbacktest(d / "hbt.jsonl", os_)]}


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        got = capsys.readouterr()
        return code, got.out, got.err

    return _run


# ---- detection --------------------------------------------------------------------------------------------------


def test_detects_every_backtester_and_live_kind(dbs: dict[str, Path], sims: dict[str, list[Path]]) -> None:
    assert detect_backtest([FT_ZIP]) == "freqtrade"
    assert detect_backtest([dbs["ft_dry"]]) == "freqtrade"
    assert detect_backtest([dbs["hb_paper"]]) == "hummingbot"
    assert detect_backtest([BTF / "bt_orders.jsonl"]) == "backtrader"
    assert detect_backtest([BTF / "vbt_orders.csv"]) == "vectorbt"
    assert detect_backtest([BTF / "lean_result.json", BTF / "lean_order_events.json"]) == "lean"
    assert detect_backtest(sims["hftbacktest"]) == "hftbacktest"
    assert detect_backtest(sims["nautilus"]) == "nautilus"
    assert detect_backtest([BTF / "instrument_map.csv"], "vectorbt") == "vectorbt"  # the flag wins
    k = detect_live([LIVE])
    assert (k.kind, k.name, k.detected) == ("canonical", None, True)
    assert (detect_live([STREAM]).kind, detect_live([STREAM]).name) == ("venue", "binance")
    k = detect_live([dbs["ft_live"], FT / "freqtrade.log"])
    assert (k.kind, k.name) == ("bot", "freqtrade")
    assert detect_live([dbs["hb_live"]]).name == "hummingbot"
    assert detect_live([HB / "trades_conf_pure_mm_1.csv"]).name == "hummingbot"
    k = detect_live([BTF / "instrument_map.csv"], venue="okx")
    assert (k.kind, k.name, k.detected) == ("venue", "okx", False)


def test_detection_errors_name_what_to_pass(dbs: dict[str, Path]) -> None:
    with pytest.raises(QtError) as e:
        detect_backtest([BTF / "instrument_map.csv"])
    assert e.value.code == "QT-INPUT-002" and "--backtester" in str(e.value.hint)
    with pytest.raises(QtError) as e:
        detect_live([BTF / "instrument_map.csv"])
    assert e.value.code == "QT-INPUT-002" and "--venue" in str(e.value.hint)
    with pytest.raises(QtError) as e:
        detect_live([STREAM, dbs["hb_live"]])
    assert "different sources" in e.value.message
    with pytest.raises(QtError) as e:
        detect_live([STREAM], venue="nasdaq")
    assert e.value.code == "QT-USAGE-001"
    with pytest.raises(QtError) as e:
        detect_backtest([FT_ZIP], "nonesuch")
    assert e.value.code == "QT-CONFIG-001"


# ---- the reconcile path -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sim", "fn"),
    [
        ([BTF / "vbt_orders.csv"], vb.reconcile_vectorbt),
        ([BTF / "lean_result.json", BTF / "lean_order_events.json"], lean.reconcile_lean),
    ],
)
def test_bar_backtesters_against_a_venue_export_match_reconcile(tmp_path: Path, sim: list[Path], fn: Any) -> None:
    res = compare(sim, STREAM, out=tmp_path / "c", options={"timeframe": "1h"})
    # the same pairing as `live import` then `reconcile`, which compare replaces
    from queuetruth import live as qlive

    qlive.import_venue({"venue": "binance", "inputs": [{"path": str(STREAM)}], "out": str(tmp_path / "lv")})
    rep = fn(sim if len(sim) > 1 else sim[0], tmp_path / "lv", tmp_path / "rec", timeframe="1h")
    doc = json.loads((tmp_path / "c" / "reconcile.json").read_text())
    assert doc["counts"] == rep.counts and doc["n_paired"] == rep.n_paired == 6
    assert res.doc["path"] == "reconcile" and res.doc["live"] == {
        "kind": "venue",
        "name": "binance",
        "inputs": [str(STREAM)],
        "log": str(tmp_path / "c" / "live"),
    }
    assert res.summary == (
        "5 of the backtest's 6 fills also filled live (83%, 95% CI 44% to 97%, orders counted as independent); live "
        "filled 5 order(s) in all."
    )
    lines = res.doc["lines"]
    assert lines[1] == "Fills only the backtest made: 1 (1 the live order rested and did not fill)."
    assert lines[2] == (
        "P&L gap BTCUSDT: on fills both made the backtest's prices were better by 0.19623456789 (quote); fills only "
        "the backtest made are 608 of notional (quote)."
    )
    assert lines[3].startswith("time to fill: still unfilled after 1s (Kaplan-Meier, cancels censored)")
    assert any(x.startswith("markouts: need --data") for x in lines)
    assert any(x.startswith("latency: not measured: no live order has both") for x in lines)
    assert res.doc["fills"]["backtest"] == 6 and res.doc["fills"]["both"] == 5


def test_freqtrade_and_hummingbot_against_their_bots_match_reconcile(tmp_path: Path, dbs: dict[str, Path]) -> None:
    live = [dbs["ft_live"], FT / "freqtrade.log"]
    res = compare(FT_ZIP, live, out=tmp_path / "ft")
    rep = ft.reconcile_freqtrade(FT_ZIP, live, tmp_path / "ft-api")
    assert res.doc["backtester"] == "freqtrade" and res.doc["live"]["kind"] == "bot"
    assert json.loads((tmp_path / "ft" / "reconcile.json").read_text())["counts"] == rep.counts
    assert res.summary.startswith("7 of the backtest's 10 fills also filled live (70%")
    assert res.doc["one_sided"] == {
        "backtest_only": {"missed_fill": 2, "no_live_twin": 1},
        "live_only": {"no_sim_twin": 3},
    }
    assert (
        res.doc["lines"][2]
        == "Fills only live made: 3 (3 no backtest order to pair it with (the backtest did not place it))."
    )
    res = compare(dbs["hb_paper"], dbs["hb_live"], out=tmp_path / "hb", window="30s")
    rep = hb.reconcile_hummingbot(dbs["hb_paper"], dbs["hb_live"], tmp_path / "hb-api", window_ns=30_000_000_000)
    assert json.loads((tmp_path / "hb" / "reconcile.json").read_text())["counts"] == rep.counts
    assert res.doc["backtester"] == "hummingbot" and res.doc["window_ns"] == 30_000_000_000
    assert res.doc["one_sided"]["backtest_only"] == {"missed_fill": 2}


def test_backtrader_against_a_canonical_log_and_the_order_table(tmp_path: Path) -> None:
    from queuetruth import live as qlive

    imap = BTF / "instrument_map.csv"
    qlive.import_venue(
        {
            "venue": "binance",
            "inputs": [{"path": str(STREAM)}],
            "out": str(tmp_path / "lv"),
            "instrument_map": {"path": str(imap)},
        }
    )
    res = compare(BTF / "bt_orders.jsonl", tmp_path / "lv", out=tmp_path / "c", options={"timeframe": "1h"})
    assert res.doc["live"]["kind"] == "canonical" and res.doc["backtester"] == "backtrader"
    with (tmp_path / "c" / "orders.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(res.orders) == 8
    first = rows[0]
    assert (first["symbol"], first["side"], first["live_price"], first["tz"]) == ("BTCUSDT", "buy", "61000", "UTC")
    assert first["placed"].startswith("2024-03-01 10:00:")
    missed = [r for r in rows if r["category"] == "missed_fill"]
    assert len(missed) == 1 and missed[0]["outcome"] == "backtest_only" and missed[0]["live_filled_qty"] == "0"
    assert res.table().shape[0] == 8


def test_freqtrade_backtest_needs_the_bots_records(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        compare(FT_ZIP, STREAM, out=tmp_path / "c")
    assert e.value.code == "QT-INPUT-002" and "binance export" in e.value.message
    assert "tradesv3.sqlite" in str(e.value.hint)


# ---- the score path ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["nautilus", "hftbacktest"])
def test_event_driven_simulators_are_scored_as_the_simulator_arm(
    tmp_path: Path, sims: dict[str, list[Path]], name: str
) -> None:
    res = compare(sims[name], LIVE, out=tmp_path / "c")
    assert res.doc["backtester"] == name and res.doc["path"] == "score"
    # the same scorecard as `live import --venue sim-*` then `score --sim-log`
    from queuetruth import live as qlive

    qlive.import_sim(name, [str(p) for p in sims[name]], tmp_path / "sim", venue="cme")
    sc = qt.score({"live": {"path": str(LIVE)}, "sim": {"kind": "external", "log": {"path": str(tmp_path / "sim")}}})
    assert res.doc["score"]["two_by_two"] == sc.model_dump(mode="json")["two_by_two"]
    tbt = res.doc["score"]["two_by_two"]
    f = res.doc["fills"]
    assert (f["backtest"], f["both"], f["live"]) == (
        tbt["both"] + tbt["sim_only"],
        tbt["both"],
        tbt["both"] + tbt["live_only"],
    )
    lines = res.doc["lines"]
    n_bt, n_live = tbt["sim_only"], tbt["live_only"]
    assert lines[1] == f"Fills only the backtest made: {n_bt} ({n_bt} the live order did not fill within the window)."
    assert lines[2] == f"Fills only live made: {n_live} ({n_live} the backtest order did not fill within the window)."
    assert "P&L gap: needs --data (each fill is marked to the dataset's mid)." in lines
    assert any(x.startswith("time to fill: still unfilled after 1s") and "log-rank p =" in x for x in lines)
    lat = next(x for x in lines if x.startswith("latency:"))
    assert lat.startswith("latency: median entry latency ") and "200 orders" in lat
    assert res.doc["latency"]["entry_ns"]["n"] == 200


def test_with_data_markouts_pnl_per_order_and_the_exact_arm(tmp_path: Path, sims: dict[str, list[Path]]) -> None:
    res = compare(sims["hftbacktest"], LIVE, data=DATA, out=tmp_path / "c")
    lines = res.doc["lines"]
    mk = next(x for x in lines if x.startswith("markouts:"))
    assert "100ms" in mk and "1s" in mk and "10s" in mk and "95% CI" in mk
    assert any(x.startswith("P&L gap per order (marked to the dataset's mid): ") for x in lines)
    assert lines[-1].startswith("exact queue: the live orders replayed in the dataset's real queue fill ")
    assert {r["horizon_ns"] for r in res.doc["markout_diff"]} <= {100_000_000, 1_000_000_000, 10_000_000_000}
    assert res.doc["exact"]["n_orders"] == 200 and (tmp_path / "c" / "exact" / "summary.json").is_file()
    gaps = [r["pnl_gap"] for r in res.orders if r.get("pnl_gap") is not None]
    assert len(gaps) > 100


def test_score_path_errors(tmp_path: Path, sims: dict[str, list[Path]]) -> None:
    rows = [json.loads(x) for x in sims["hftbacktest"][0].read_text().splitlines()]
    for r in rows:
        r.pop("order_key")
    nokey = tmp_path / "hbt_nokey.jsonl"
    nokey.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(QtError) as e:
        compare(nokey, LIVE, out=tmp_path / "a")
    assert e.value.code == "QT-INPUT-002" and "none of the backtest's 200 orders" in e.value.message
    assert "replay-schedule" in str(e.value.hint)
    # a live log imported without an instrument map cannot be scheduled: say which instruments and what to do
    from queuetruth import live as qlive

    qlive.import_venue({"venue": "binance", "inputs": [{"path": str(STREAM)}], "out": str(tmp_path / "lv")})
    with pytest.raises(QtError) as e:
        compare(sims["hftbacktest"], tmp_path / "lv", out=tmp_path / "b")
    assert "BTCUSDT (7 orders)" in e.value.message and "--instrument-map" in str(e.value.hint)
    # with --data, a live instrument that is not a dataset symbol is named
    with pytest.raises(QtError) as e:
        compare(BTF / "vbt_orders.csv", STREAM, data=DATA, out=tmp_path / "c", options={"timeframe": "1h"})
    assert "binance/BTCUSDT is not a symbol of the dataset" in e.value.message
    with pytest.raises(QtError) as e:
        compare(BTF / "vbt_orders.csv", STREAM, data=tmp_path, out=tmp_path / "d")
    assert e.value.code == "QT-INPUT-001" and "not a queuetruth dataset" in e.value.message


# ---- CLI and API ------------------------------------------------------------------------------------------------


def test_cli_answer_first_json_html_and_rerun(tmp_path: Path, run: Any) -> None:
    out = tmp_path / "c"
    argv = ("compare", "--backtest", str(BTF / "vbt_orders.csv"), "--live", str(STREAM), "--out", str(out),
            "--option", "timeframe=1h", "--max-rows", "2")  # fmt: skip
    code, text, err = run(*argv, "--html")
    assert code == 0, err
    lines = text.splitlines()
    assert lines[0].startswith("5 of the backtest's 6 fills also filled live (83%, 95% CI 44% to 97%")
    assert "orders that differ (5 of 7):" in text and f"... 3 more in {out / 'orders.csv'}" in text
    assert text.rstrip().endswith(f"Report: {out / 'compare.html'}")
    page = (out / "compare.html").read_text()
    assert page.startswith("<!doctype html>") and "5 of the backtest" in page and "<script src" not in page
    code, text, err = run("--format", "json", *argv)  # the same output directory again
    assert code == 0, err
    doc = json.loads(text)
    assert doc["schema"] == "qt.compare" and doc["answer"] == lines[0] and "html" not in doc["files"]
    assert not (out / "compare.html").exists()
    code, text, _ = run(*argv[:-2], "--json")
    assert json.loads(text)["fills"]["both"] == 5


def test_cli_errors_and_help(tmp_path: Path, run: Any) -> None:
    code, text, _ = run(
        "--format", "json", "compare", "--backtest", str(FT_ZIP), "--live", str(STREAM), "--out", str(tmp_path)
    )
    assert code == 2 and json.loads(text)["code"] == "QT-INPUT-002"
    code, _, err = run("compare", "--live", str(STREAM))
    assert code == 2 and "--backtest" in err
    code, text, _ = run("--format", "json", "compare", "--backtest", "x", "--live", str(STREAM), "--option", "nonsense")
    assert code == 2 and "KEY=VALUE" in json.loads(text)["message"]
    a, b = run("help", "compare"), run("compare", "--help")
    assert a[0] == b[0] == 0 and "--backtest" in a[1] and a[1] == b[1]


def test_python_api(tmp_path: Path) -> None:
    res = qt.compare(BTF / "vbt_orders.csv", STREAM, out=tmp_path / "c", options={"timeframe": "1h"})
    assert isinstance(res, CompareResult) and str(res) == res.summary
    assert res.to_dict() is res.doc and "<table" in res._repr_html_()
    assert qt.compare.compare is compare  # the subpackage is also the function
    res = qt.compare(BTF / "vbt_orders.csv", STREAM, options={"timeframe": "1h"})  # out: a new temporary directory
    assert res.out is not None and (res.out / "compare.json").is_file()


def test_venues_and_bots_come_from_the_importer_registry() -> None:
    from queuetruth.compare import detect as cd
    from queuetruth.live import importers as imp

    assert cd.VENUES == imp.names("venue", builtin=True) and cd.BOTS == imp.names("bot", builtin=True)

    def run(paths: list[str], opts: Any) -> Any:  # never called: detection only names the importer
        raise AssertionError

    imp.register(imp.ImporterDef(name="qt_test_venue", kind="venue", run=run))
    try:
        k = detect_live([STREAM], venue="qt-test-venue")
        assert (k.kind, k.name, k.detected) == ("venue", "qt_test_venue", False)
    finally:
        imp.unregister("qt_test_venue")
    with pytest.raises(QtError):
        detect_live([STREAM], venue="qt-test-venue")
