"""`queuetruth check` on synthetic backtests over tests/fixtures/synth_small: the backtester read from the file, the
symbols mapped from the dataset, the backtest's own orders replayed in the exact queue, and the error paths."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import queuetruth as qt
from queuetruth import cli
from queuetruth.check import CheckResult, check, detect
from queuetruth.check.detect import detect_all
from queuetruth.check.symbols import Instrument, match, norm
from queuetruth.errors import QtError

from . import synth_backtests as sb

pytestmark = pytest.mark.requires_core

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "tests" / "fixtures" / "synth_small"
FIX = REPO / "tests" / "fixtures" / "live"
#: Short enough that the touch-fill backtest's 5 s fills are often not real (phantom fills appear).
WINDOW = "10s"


@pytest.fixture(scope="module")
def orders() -> list[sb.Order]:
    return sb.orders(DATA)


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory, orders: list[sb.Order]) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("bt")
    return {
        "hftbacktest": sb.write_hftbacktest(d / "hbt_snapshots.jsonl", orders),
        "nautilus": sb.write_nautilus(d / "orders_report.csv", orders),
        "backtrader": sb.write_backtrader(d / "bt_orders.jsonl", orders),
        "vectorbt": sb.write_vectorbt(d / "vbt_orders.csv", orders),
    }


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory, files: dict[str, Path]) -> dict[str, CheckResult]:
    out = tmp_path_factory.mktemp("check")
    return {k: check(p, data=DATA, out=out / k, window=WINDOW) for k, p in files.items()}


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        got = capsys.readouterr()
        return code, got.out, got.err

    return _run


# ---- which backtester wrote the file ----------------------------------------------------------------------------


def test_detects_the_synthetic_backtests(files: dict[str, Path]) -> None:
    for kind, p in files.items():
        assert detect(p) == kind, p


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("backtesters/bt_orders.jsonl", "backtrader"),
        ("backtesters/bt_transactions.json", "backtrader"),
        ("backtesters/vbt_orders.json", "vectorbt"),
        ("backtesters/vbt_trades.csv", "vectorbt"),
        ("backtesters/lean_result.json", "lean"),
        ("backtesters/lean_order_events.json", "lean"),
        ("backtesters/lean_order_events.csv", "lean"),
        ("freqtrade/backtest-result-2024-03-01_14-00-00.zip", "freqtrade"),
        ("freqtrade/backtest-result-2024-03-01_14-00-00.json", "freqtrade"),
    ],
)
def test_detects_the_importer_fixtures(path: str, kind: str) -> None:
    assert detect(FIX / path) == kind


def test_detect_refuses_unknown_and_mixed_files(tmp_path: Path, files: dict[str, Path]) -> None:
    odd = tmp_path / "odd.csv"
    odd.write_text("a,b\n1,2\n")
    with pytest.raises(QtError, match="cannot tell which backtester") as e:
        detect_all([str(odd)])
    assert e.value.code == "QT-USAGE-001" and "--backtester" in (e.value.hint or "")
    with pytest.raises(QtError, match="different backtesters"):
        detect_all([str(files["hftbacktest"]), str(files["nautilus"])])


# ---- symbols ----------------------------------------------------------------------------------------------------


def _inst(sym: str) -> Instrument:
    return Instrument(sym, "cme", -9, 0, 250_000_000, 1, "USD", "UTC", {20619: 1})


def test_symbol_matching() -> None:
    assert norm("btc/usdt") == "BTCUSDT" and norm("AAPL.XNAS") == "AAPL" and norm("ES-U6") == "ESU6"
    syms = {"QTAU6": _inst("QTAU6"), "BTCUSDT": _inst("BTCUSDT")}
    m = match(["QTAU6", "BTC/USDT", "qtau6.XCME", "asset0", "ETHUSDT"], syms)
    assert m.mapped == {"QTAU6": "QTAU6", "BTC/USDT": "BTCUSDT", "qtau6.XCME": "QTAU6"}
    assert "names no instrument" in m.unmapped["asset0"] and "2 symbols" in m.unmapped["asset0"]
    assert "is not a symbol of the dataset" in m.unmapped["ETHUSDT"]
    one = match(["asset0"], {"QTAU6": _inst("QTAU6")})
    assert one.mapped == {"asset0": "QTAU6"} and "one symbol" in one.notes[0]
    amb = match(["BTC-USDT"], {"BTCUSDT": _inst("BTCUSDT"), "BTC_USDT": _inst("BTC_USDT")})
    assert "several dataset symbols" in amb.unmapped["BTC-USDT"]


# ---- the exact replay of the backtest's orders ------------------------------------------------------------------


def test_hftbacktest_answer_and_counts(results: dict[str, CheckResult], orders: list[sb.Order]) -> None:
    r = results["hftbacktest"]
    d = r.doc
    assert d["schema"] == "qt.check" and list(d)[:3] == ["schema", "schema_version", "answer"]
    assert d["backtester"] == "hftbacktest" and d["n_orders"] == d["n_limit"] == d["n_checked"] == len(orders)
    t = d["two_by_two"]
    assert sum(t.values()) == len(orders)
    assert d["backtest_filled"] == t["both"] + t["phantom"] == sum(o.filled for o in orders)
    assert d["real_filled"] == t["both"] + t["real_only"]
    assert t["phantom"] > 0  # a touch-fill backtest over a 10 s window
    assert d["fill_gap"]["orders"] == d["backtest_filled"] - d["real_filled"]
    assert d["fill_gap"]["ci_lo"] <= d["fill_gap"]["orders"] <= d["fill_gap"]["ci_hi"]
    assert d["instruments"] == {"QTAU6": "QTAU6", "QTBU6": "QTBU6"}
    assert d["window"] == "10s" and d["pnl"]["markout"] == "1min" and d["pnl"]["currency"] == "USD"
    assert r.summary == d["answer"]
    assert r.summary.startswith(f"Your backtest filled {d['backtest_filled']} of {len(orders)} limit orders within 10s")
    assert f"{t['phantom']} fills are phantom" in r.summary
    assert "95% CI" in r.summary and "1min markout" in r.summary
    # human units: symbols, decimal prices, local times, seconds
    row = next(o for o in r.orders if o["outcome"] == "phantom")
    assert row["symbol"] in ("QTAU6", "QTBU6")
    assert float(row["price"]) == pytest.approx(next(o.price for o in orders if row["order"].endswith(f":{o.oid}")))
    assert row["entry"].endswith("+00:00") and row["backtest_fill_after_s"] == pytest.approx(5.0, abs=0.01)
    assert {s["symbol"] for s in d["symbols"]} == {"QTAU6", "QTBU6"}
    assert sum(s["phantom"] for s in d["symbols"]) == t["phantom"]


def test_formats_of_one_backtest_agree(results: dict[str, CheckResult]) -> None:
    """hftbacktest snapshots, the NautilusTrader orders report and Backtrader notifications of the same orders give
    the same order-by-order answer."""
    want = results["hftbacktest"].doc
    for k in ("nautilus", "backtrader"):
        d = results[k].doc
        assert d["backtester"] == k
        assert d["two_by_two"] == want["two_by_two"], k
        assert d["real_filled"] == want["real_filled"]
        assert d["pnl"]["per_order"] == pytest.approx(want["pnl"]["per_order"], abs=1e-9), k


def test_vectorbt_fills_are_checked_as_resting_limit_orders(results: dict[str, CheckResult]) -> None:
    d = results["vectorbt"].doc
    t = d["two_by_two"]
    assert d["n_checked"] == 20 and d["backtest_filled"] == 20
    assert t["real_only"] == 0 and t["neither"] == 0 and t["phantom"] == 20 - t["both"]
    assert any("checked as a limit order" in n for n in d["notes"])
    assert d["time_to_fill"]["backtest_median_s"] == 0


def test_files_and_why_hint(results: dict[str, CheckResult], run: Any) -> None:
    r = results["hftbacktest"]
    assert r.out is not None
    doc = json.loads((r.out / "check.json").read_text())
    assert doc["answer"] == r.summary and doc["files"]["orders"].endswith("orders.csv")
    assert (r.out / "orders.csv").read_text().count("\n") == r.doc["n_checked"] + 1
    assert (r.out / "instrument_map.csv").read_text().startswith("venue,instrument,instrument_id")
    ex = r.doc["examples"]
    assert ex and all(e["outcome"] == "phantom" for e in ex)
    argv = ex[0]["why"].split()
    assert argv[:2] == ["queuetruth", "why"]
    code, out, err = run(*argv[1:])
    assert code == 0, err
    assert out.startswith("why: live order " + ex[0]["order"])


def test_html_report(results: dict[str, CheckResult]) -> None:
    r = results["hftbacktest"]
    page = r.to_html()
    assert page.startswith("<!doctype html>") and "How wrong is this backtest?" in page
    assert "phantom" in page and "QTAU6" in page and "<script src" not in page


def test_table_is_a_dataframe(results: dict[str, CheckResult]) -> None:
    pd = pytest.importorskip("pandas")
    df = results["hftbacktest"].table()
    assert isinstance(df, pd.DataFrame) and len(df) == 40
    assert {"symbol", "price", "entry", "backtest_filled", "real_filled", "outcome", "pnl_gap"} <= set(df.columns)


def test_qt_check_is_the_api(tmp_path: Path, files: dict[str, Path], results: dict[str, CheckResult]) -> None:
    r = qt.check(files["hftbacktest"], data=DATA, out=tmp_path / "api", window=WINDOW)
    assert isinstance(r, CheckResult)
    assert r.doc["two_by_two"] == results["hftbacktest"].doc["two_by_two"]


# ---- instrument mapping and error paths -------------------------------------------------------------------------


def test_unnamed_instrument_needs_a_map(tmp_path: Path, orders: list[sb.Order]) -> None:
    one = [o for o in orders if o.instrument_id == 1]
    p = sb.write_hftbacktest(tmp_path / "hbt.jsonl", one, named=False)
    with pytest.raises(QtError) as e:
        check(p, data=DATA, out=tmp_path / "x")
    assert e.value.code == "QT-INPUT-002"
    assert "asset0" in str(e.value) and "names no instrument" in str(e.value) and "QTAU6, QTBU6" in str(e.value)
    assert "--instrument-map" in (e.value.hint or "")
    imap = tmp_path / "map.csv"
    imap.write_text(
        "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\ncme,asset0,1,,-9,0,0.25,1\n"
    )
    r = check(p, data=DATA, out=tmp_path / "y", window=WINDOW, instrument_map=imap)
    assert r.doc["n_checked"] == len(one) and r.doc["instrument_map"] == str(imap)


def test_partly_unmapped_instruments_are_named(tmp_path: Path, orders: list[sb.Order]) -> None:
    text = sb.write_backtrader(tmp_path / "bt.jsonl", orders).read_text().replace('"QTBU6"', '"ZZZ1"')
    p = tmp_path / "bt2.jsonl"
    p.write_text(text)
    r = check(p, data=DATA, out=tmp_path / "o", window=WINDOW)
    n_b = sum(o.instrument_id == 2 for o in orders)
    assert r.doc["n_checked"] == len(orders) - n_b
    assert any(n.startswith(f"{n_b} order(s) on ZZZ1 not checked: ZZZ1 is not a symbol") for n in r.doc["notes"])
    assert r.doc["n_limit"] == len(orders)
    assert f"{n_b} more of its {len(orders)} limit orders could not be checked" in r.summary


def test_unknown_symbols_fail_with_the_reason() -> None:
    with pytest.raises(QtError, match=r"BTCUSDT \(\d+ orders\) is not a symbol of the dataset") as e:
        check(FIX / "backtesters" / "bt_orders.jsonl", data=DATA)
    assert e.value.code == "QT-INPUT-002"


def test_orders_outside_the_dataset_days(tmp_path: Path, orders: list[sb.Order]) -> None:
    year = 365 * 86_400 * 10**9
    early = [sb.Order(o.oid, o.instrument_id, o.side, o.px, o.qty, o.ts - year, o.filled) for o in orders]
    p = sb.write_hftbacktest(tmp_path / "hbt.jsonl", early)
    with pytest.raises(QtError, match="no order falls inside it") as e:
        check(p, data=DATA, out=tmp_path / "x")
    assert "2025-06" in str(e.value) and "2026-06-15 to 2026-06-16" in str(e.value)


def test_market_orders_only(tmp_path: Path, orders: list[sb.Order]) -> None:
    text = sb.write_backtrader(tmp_path / "bt.jsonl", orders).read_text().replace('"Limit"', '"Market"')
    p = tmp_path / "mkt.jsonl"
    p.write_text(text)
    with pytest.raises(QtError, match="no resting limit order to replay") as e:
        check(p, data=DATA, out=tmp_path / "x")
    assert e.value.code == "QT-INPUT-002"


def test_l2_dataset_and_missing_dataset(tmp_path: Path, files: dict[str, Path]) -> None:
    with pytest.raises(QtError, match="not a queuetruth dataset"):
        check(files["hftbacktest"], data=tmp_path)
    l2 = tmp_path / "l2"
    l2.mkdir()
    (l2 / "manifest.json").write_text(json.dumps({"kind": "l2", "venue": "binance", "partitions": []}))
    with pytest.raises(QtError, match="L2-only dataset"):
        check(files["hftbacktest"], data=l2)


# ---- the command line -------------------------------------------------------------------------------------------


def test_cli_without_data_names_what_is_needed(run: Any, files: dict[str, Path]) -> None:
    code, out, err = run("check", str(files["hftbacktest"]))
    assert code == 2 and out == ""
    assert err.startswith("QT-USAGE-001: check: without --data") and "--data DIR" in err and "atlas" in err
    code, out, _ = run("check", str(files["hftbacktest"]), "--json")
    doc = json.loads(out)
    assert code == 2 and doc["schema"] == "qt.error" and doc["code"] == "QT-USAGE-001"


def test_cli_answer_first_json_and_html(tmp_path: Path, run: Any, files: dict[str, Path]) -> None:
    out_dir = tmp_path / "c"
    argv = ["check", str(files["nautilus"]), "--data", str(DATA), "--out", str(out_dir), "--window", WINDOW]
    code, out, err = run(*argv, "--html")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("Your backtest filled 20 of 40 limit orders within 10s")
    assert "backtest filled" in out and "phantom" in out and "queuetruth why --data" in out
    assert (out_dir / "check.html").read_text().count("Your backtest filled") >= 1
    assert "note(s) in" in err
    code, out, _ = run("--format", "json", *argv, "--backtester", "nautilus")
    doc = json.loads(out)
    assert code == 0 and doc["schema"] == "qt.check" and doc["backtester"] == "nautilus"
    code, out2, _ = run(*argv, "--json")
    assert code == 0 and json.loads(out2)["two_by_two"] == doc["two_by_two"]


def test_cli_help_lists_check(run: Any) -> None:
    code, out, _ = run("--help")
    assert code == 0 and any(line.split()[:1] == ["check"] for line in out.splitlines())
    a, b = run("help", "check"), run("check", "--help")
    assert a == b and a[0] == 0 and a[1].startswith("usage: queuetruth check")
