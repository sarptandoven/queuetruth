"""Backtrader and VectorBT importers, pairing with a canonical live log and reconciliation
(`queuetruth.live.importers.backtrader`, `.vectorbt`, `.backtest`) on the fixtures of
tests/fixtures/live/backtesters (written from Backtrader 1.9.78.123's and VectorBT 0.28.1's source) and on malformed
inputs."""

from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from queuetruth import api
from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.live import read_log, table_rows
from queuetruth.live.importers import SIM_IMPORTERS, ImportOptions
from queuetruth.live.importers import backtest as bk
from queuetruth.live.importers import backtrader as bt
from queuetruth.live.importers import freqtrade as ft
from queuetruth.live.importers import vectorbt as vb

FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "live" / "backtesters"
BT = FIX / "bt_orders.jsonl"
TXN = FIX / "bt_transactions.json"
VCSV = FIX / "vbt_orders.csv"
VJSON = FIX / "vbt_orders.json"
VTR = FIX / "vbt_trades.csv"
IMAP = FIX / "instrument_map.csv"
NS = 1_000_000_000
H = 3600 * NS
T0 = 1_709_251_200 * NS  # 2024-03-01T00:00:00Z

#: the scenario's outcome, the same for both libraries (make_fixtures.py)
CATEGORIES = {
    "binance:main:7100001": "match",
    "binance:main:7100002": "match",
    "binance:main:7100003": "missed_fill",
    "binance:main:7100004": "price",
    "binance:main:7100005": "partial_fill",
    "binance:main:7100006": "timing",
    "binance:main:7100007": "no_sim_twin",
}


def _err(code: str, fn: Any, *a: Any, **kw: Any) -> QtError:
    with pytest.raises(QtError) as e:
        fn(*a, **kw)
    assert e.value.code == code, str(e.value)
    return e.value


@pytest.fixture
def live(tmp_path: Path) -> Path:
    """The live arm: the fixture's Binance user-data stream imported as a canonical live log."""
    out = tmp_path / "live"
    api.import_live(
        {
            "venue": "binance",
            "inputs": [{"path": str(FIX / "binance_user_stream.jsonl")}],
            "out": str(out),
            "account": "main",
            "instrument_map": {"path": str(IMAP)},
        }
    )
    return out


def _write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text)
    return str(p)


def _rows(out: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    o, e = read_log(out)
    strip = lambda rs: [{k: v for k, v in r.items() if k != "raw"} for r in table_rows(rs)]  # noqa: E731
    return strip(o), strip(e)


def test_fixture_generator_is_current(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("bk_make_fixtures", FIX / "make_fixtures.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = tmp_path
    mod.main()
    for name in (
        "binance_user_stream.jsonl", "instrument_map.csv", "bt_orders.jsonl", "bt_transactions.json",
        "vbt_orders.csv", "vbt_orders.json", "vbt_trades.csv",
    ):  # fmt: skip
        assert (tmp_path / name).read_text() == (FIX / name).read_text(), name


def test_registered_in_importer_registries() -> None:
    assert SIM_IMPORTERS["backtrader"] is bt.import_backtrader_sim
    assert SIM_IMPORTERS["vectorbt"] is vb.import_vectorbt_sim
    assert qlive.SIM_VENUES["sim_backtrader"] == "backtrader" and qlive.SIM_VENUES["sim_vectorbt"] == "vectorbt"


# ---- Backtrader ----------------------------------------------------------------------------------------------------


def test_read_backtrader_notifications() -> None:
    run = bt.read_backtrader([str(BT)], {})
    assert [o.ref for o in run.orders] == ["1", "2", "3", "4", "5", "6", "7"]
    first = run.orders[0]
    assert (first.side, first.order_type, first.tif, first.qty, first.limit_px) == (
        "buy", "limit", "gtc", Decimal("0.01"), Decimal("61000.0"),
    )  # fmt: skip
    # created on the 09:00 bar, filled by BackBroker on the next one
    assert first.submit_ns == T0 + 9 * H and first.placed_ns == T0 + 9 * H
    assert [(f.ts_ns, f.qty, f.px, f.fee, f.fill_id) for f in first.fills] == [
        (T0 + 10 * H, Decimal("0.01"), Decimal("61000.0"), Decimal("0.61"), "1:1")
    ]
    market = run.orders[3]
    assert (market.side, market.order_type, market.tif, market.limit_px) == ("sell", "market", "other", None)
    assert market.fills[0].px == Decimal("61210.123456789")
    expired = run.orders[6]
    assert expired.fills == [] and expired.tif == "gtd" and expired.end == (T0 + 23 * H, "s", "expire", "Expired")
    assert any(w.startswith("trade notifications: 4 (2 closed; pnlcomm BTCUSDT 6.6739012345678)") for w in run.warnings)


def test_import_backtrader_log(tmp_path: Path) -> None:
    s = qlive.import_sim("backtrader", [str(BT)], tmp_path / "s", instrument_map=str(IMAP), venue="binance")
    assert (s.venue, s.n_orders, s.n_fills, s.validation_status) == ("sim:backtrader", 7, 6, "simulated")
    assert any("1 backtest price / size value(s) rounded half-even" in w for w in s.warnings)
    orders, events = _rows(tmp_path / "s")
    assert {o["order_key"] for o in orders} == {f"sim:backtrader:main:{i}" for i in range(1, 8)}
    assert all(o["instrument_id"] == 1 and o["price_exp"] == -2 for o in orders)
    by = {(e["order_key"], e["event_type"]): e for e in events}
    f4 = by[("sim:backtrader:main:4", "fill")]
    assert f4["px"] == 6121012 and f4["liquidity"] == "taker" and f4["source"] == "sim"  # 61210.12 at tick 0.01
    assert by[("sim:backtrader:main:1", "fill")]["liquidity"] == "unknown"
    assert by[("sim:backtrader:main:7", "expire")]["reason"] == "Expired"
    assert by[("sim:backtrader:main:1", "submit_sent")]["ts_local_send"] == T0 + 9 * H


def test_backtrader_options(tmp_path: Path) -> None:
    run = bt.read_backtrader([str(BT)], {"data_tz": "Europe/Berlin", "instrument": "BTC-USD"})
    assert run.orders[0].submit_ns == T0 + 8 * H and {o.instrument for o in run.orders} == {"BTC-USD"}
    run = bt.read_backtrader([str(BT)], {"instruments": "BTCUSDT:XBTUSD", "timeframe": "1h", "fill_delay_bars": "1"})
    assert run.orders[0].instrument == "XBTUSD" and run.orders[0].fills[0].ts_ns == T0 + 11 * H
    assert run.timeframe_ns == H
    _err("QT-CONFIG-001", bt.read_backtrader, [str(BT)], {"fill_delay_bars": 1})  # needs timeframe
    _err("QT-CONFIG-001", bt.read_backtrader, [str(BT)], {"instrument": "A", "instruments": "B:C"})
    _err("QT-CONFIG-001", bt.read_backtrader, [str(BT)], {"instruments": "nocolon"})
    _err("QT-CONFIG-001", bt.read_backtrader, [str(BT)], {"timeframe": "1M"})
    e = _err("QT-CONFIG-001", qlive.import_sim, "backtrader", [str(BT)], tmp_path / "x", strategy="S")
    assert "unknown option(s) ['strategy']" in str(e)


def test_backtrader_transactions_are_fills_only(tmp_path: Path) -> None:
    s = qlive.import_sim("backtrader", [str(TXN)], tmp_path / "t", instrument_map=str(IMAP), venue="binance")
    assert (s.n_orders, s.n_fills) == (6, 6) and s.coverage.fills_only
    assert any(w.startswith("sim:backtrader: Transactions analyzer: 6 entr(ies)") for w in s.warnings)
    orders, _ = _rows(tmp_path / "t")
    assert {o["order_type"] for o in orders} == {"other"} and all(o["limit_px"] is None for o in orders)
    assert [o["side"] for o in sorted(orders, key=lambda o: o["order_key"])][:2] == ["buy", "sell"]
    # headers=True adds a "date" key with the column names: skipped
    doc = json.loads(TXN.read_text())
    doc["date"] = [["amount", "price", "sid", "symbol", "value"]]
    run = bt.read_backtrader([_write(tmp_path, "h.json", json.dumps(doc))], {})
    assert len(run.orders) == 6


def test_backtrader_order_key_from_order_info(tmp_path: Path, live: Path) -> None:
    lines = BT.read_text().splitlines()
    out = []
    for line in lines:
        d = json.loads(line)
        if d.get("ref") == 3 and d["event"] == "order":
            d["order_key"] = "binance:main:7100006"  # stored by the strategy: order.info["order_key"]
        out.append(json.dumps(d))
    p = _write(tmp_path, "keyed.jsonl", "\n".join(out) + "\n")
    run = bt.read_backtrader([p], {})
    assert next(o for o in run.orders if o.ref == "3").key_hint == "binance:main:7100006"
    s = qlive.import_sim("backtrader", [p], tmp_path / "k", instrument_map=str(IMAP), venue="binance")
    assert any("1 simulator order(s) carry a live order_key" in w for w in s.warnings)
    assert "binance:main:7100006" in {o["order_key"] for o in _rows(tmp_path / "k")[0]}
    # the key hint wins over the time pairing
    pairs = bk.pair_sim_live(bk.read_live_arm(live).orders, run.orders, window_ns=2 * H)
    hinted = next(lo for lo, so in pairs if so is not None and so.ref == "3")
    assert hinted is not None and hinted.key == "binance:main:7100006"


def test_backtrader_malformed(tmp_path: Path) -> None:
    good = json.loads(BT.read_text().splitlines()[2])  # ref 1 Completed
    cases = {
        "status": {**good, "status": "Done"},
        "ordtype": {**good, "ordtype": "Long"},
        "exectype": {**good, "exectype": "Iceberg"},
        "sign": {**good, "size": -0.01},
        "no bits": {k: v for k, v in good.items() if k != "exbits"},
        "bad bit": {**good, "exbits": [{"dt": good["created"], "size": 0.01, "price": 0}]},
        "bit sign": {**good, "exbits": [{"dt": good["created"], "size": -0.01, "price": 61000.0}]},
        "no ref": {k: v for k, v in good.items() if k != "ref"},
        "bad time": {**good, "created": "yesterday"},
        "no limit": {**good, "price": None},
        "event": {**good, "event": "fill"},
        "trade": {"event": "trade", "status": "Pending"},
    }
    for name, obj in cases.items():
        p = _write(tmp_path, f"{name.replace(' ', '_')}.jsonl", json.dumps(obj) + "\n")
        _err("QT-INPUT-002", bt.read_backtrader, [p], {})
    _err("QT-INPUT-002", bt.read_backtrader, [_write(tmp_path, "e.jsonl", "")], {})
    _err("QT-INPUT-002", bt.read_backtrader, [_write(tmp_path, "l.json", "[1, 2]")], {})
    _err(
        "QT-INPUT-002",
        bt.read_backtrader,
        [_write(tmp_path, "tx.json", '{"2024-03-01T10:00:00": [[0, 1, 0, "X"]]}')],
        {},
    )
    _err("QT-INPUT-001", bt.import_backtrader, [str(tmp_path / "missing.jsonl")], tmp_path / "o")
    # a Created notification is counted, not an event; a margin call is a reject
    margin = [{**good, "ref": 9, "status": "Created"}, {**good, "ref": 9, "status": "Margin", "exbits": []}]
    p = _write(tmp_path, "m.jsonl", "\n".join(json.dumps(x) for x in margin) + "\n")
    run = bt.read_backtrader([p], {})
    assert run.skipped == {"Created notification (no event)": 1}
    assert run.orders[0].end is not None and run.orders[0].end[2:] == ("reject", "Margin: not enough cash")


# ---- VectorBT ------------------------------------------------------------------------------------------------------


def test_read_vectorbt_orders() -> None:
    run = vb.read_vectorbt([str(VCSV)], {})
    assert [o.ref for o in run.orders] == [f"BTCUSDT-{i}" for i in range(6)]
    o = run.orders[3]
    assert (o.side, o.order_type, o.tif, o.limit_px, o.placed_ns) == ("sell", "market", "other", None, T0 + 16 * H)
    assert [(f.qty, f.px, f.fee) for f in o.fills] == [
        (Decimal("0.01"), Decimal("61210.123456789"), Decimal("0.61210123456789"))
    ]
    assert all(x.liquidity == "unknown" for x in run.orders)
    limit = vb.read_vectorbt([str(VCSV)], {"order_type": "limit"})
    assert limit.orders[0].order_type == "limit" and limit.orders[0].limit_px == Decimal("61000.0")


def test_vectorbt_csv_and_json_give_the_same_log(tmp_path: Path) -> None:
    a = qlive.import_sim("vectorbt", [str(VCSV)], tmp_path / "c", instrument_map=str(IMAP), venue="binance")
    b = qlive.import_sim("vectorbt", [str(VJSON)], tmp_path / "j", instrument_map=str(IMAP), venue="binance")
    assert (a.n_orders, a.n_fills) == (b.n_orders, b.n_fills) == (6, 6)
    (oc, ec), (oj, ej) = _rows(tmp_path / "c"), _rows(tmp_path / "j")
    assert oc == oj
    # the same events; ISO text to the second has precision s, pandas' epoch milliseconds ms
    assert {e["ts_precision"] for e in ec} == {"s"} and {e["ts_precision"] for e in ej} == {"ms"}
    assert [{**e, "ts_precision": None} for e in ec] == [{**e, "ts_precision": None} for e in ej]
    assert {e["liquidity"] for e in ec if e["event_type"] == "fill"} == {"unknown"}


def test_vectorbt_trades(tmp_path: Path) -> None:
    run = vb.read_vectorbt([str(VTR)], {})
    assert [o.ref for o in run.orders] == [
        "BTCUSDT-t0-entry", "BTCUSDT-t0-exit", "BTCUSDT-t1-entry", "BTCUSDT-t1-exit", "BTCUSDT-t2-entry",
    ]  # fmt: skip
    assert [o.side for o in run.orders] == ["buy", "sell", "buy", "sell", "buy"]
    assert any(w.startswith("orders derived from 3 trade record(s)") and "(1 open)" in w for w in run.warnings)
    both = vb.read_vectorbt([str(VCSV), str(VTR)], {})
    assert len(both.orders) == 6
    assert any(w.startswith("trade records: 3 (2 closed; PnL BTCUSDT 6.6639)") for w in both.warnings)


def test_vectorbt_columns(tmp_path: Path) -> None:
    text = VCSV.read_text()
    lines = text.splitlines()
    sweep = "\n".join([*lines, *(ln.replace(",BTCUSDT,", ",ETHUSDT,") for ln in lines[1:])]) + "\n"
    p = _write(tmp_path, "sweep.csv", sweep)
    e = _err("QT-CONFIG-001", vb.read_vectorbt, [p], {})
    assert "each column of a VectorBT portfolio is its own backtest" in str(e)
    assert len(vb.read_vectorbt([p], {"column": "ETHUSDT"}).orders) == 6
    named = vb.read_vectorbt([p], {"instruments": "BTCUSDT:BTCUSDT,ETHUSDT:ETH-USDT"})
    assert {o.instrument for o in named.orders} == {"BTCUSDT", "ETH-USDT"} and len(named.orders) == 12
    one = vb.read_vectorbt([p], {"instruments": "ETHUSDT:ETH-USDT"})
    assert len(one.orders) == 6 and any("columns ['BTCUSDT'] not named" in w for w in one.warnings)
    _err("QT-CONFIG-001", vb.read_vectorbt, [p], {"column": "SOLUSDT"})
    _err("QT-CONFIG-001", vb.read_vectorbt, [p], {"instruments": "SOLUSDT:SOL"})
    # the same Column and Order Id twice: dropped and counted
    dup = vb.read_vectorbt([_write(tmp_path, "d.csv", text + lines[1] + "\n")], {})
    assert len(dup.orders) == 6 and dup.skipped == {"duplicate order record (same Column and Order Id)": 1}


def test_vectorbt_malformed(tmp_path: Path) -> None:
    head = "Order Id,Column,Timestamp,Size,Price,Fees,Side\n"
    cases = {
        "bar_number.csv": head + "0,BTCUSDT,5,0.01,61000,0.6,Buy\n",
        "side.csv": head + "0,BTCUSDT,2024-03-01 10:00:00,0.01,61000,0.6,Long\n",
        "size.csv": head + "0,BTCUSDT,2024-03-01 10:00:00,-0.01,61000,0.6,Buy\n",
        "price.csv": head + "0,BTCUSDT,2024-03-01 10:00:00,0.01,,0.6,Buy\n",
        "id.csv": head + ",BTCUSDT,2024-03-01 10:00:00,0.01,61000,0.6,Buy\n",
        "raw.csv": "id,col,idx,size,price,fees,side\n0,0,5,0.01,61000,0.6,0\n",
        "other.csv": "a,b\n1,2\n",
        "empty.csv": head,
        "columns.json": '{"Order Id": {"0": 0}, "Column": {"0": "BTCUSDT"}}',
        "scalar.json": "[1]",
        "direction.csv": VTR.read_text().replace(",Long,", ",Up,", 1),
    }
    for name, text in cases.items():
        e = _err("QT-INPUT-002", vb.read_vectorbt, [_write(tmp_path, name, text)], {})
        if name == "raw.csv":
            assert "records_readable" in str(e)
        if name == "bar_number.csv":
            assert "bar number" in str(e)
    _err("QT-CONFIG-001", vb.read_vectorbt, [str(VCSV)], {"order_type": "stop"})
    _err("QT-CONFIG-001", qlive.import_sim, "vectorbt", [str(VCSV)], tmp_path / "x", tif="ioc")
    # naive timestamps take data_tz
    naive = vb.read_vectorbt([_write(tmp_path, "n.csv", head + "0,BTCUSDT,2024-03-01 10:00:00,0.01,61000,0.6,Buy\n")],
                             {"data_tz": "America/New_York"})  # fmt: skip
    assert naive.orders[0].placed_ns == T0 + 15 * H


# ---- pairing with a live log, reconcile ----------------------------------------------------------------------------


def test_import_sim_pairs_with_a_live_log(tmp_path: Path, live: Path) -> None:
    s = qlive.import_sim(
        "backtrader", [str(BT)], tmp_path / "s", instrument_map=str(IMAP), pair_with=str(live), timeframe="1h"
    )
    assert any("6 of 7 simulator order(s) paired with an order of the live log" in w for w in s.warnings)
    assert any("6 backtest fill time(s) before the paired live order's entry clamped" in w for w in s.warnings)
    orders, events = _rows(tmp_path / "s")
    keys = {o["order_key"] for o in orders}
    assert keys == {f"binance:main:{7100001 + i}" for i in range(6)} | {"sim:backtrader:main:7"}
    fill = next(e for e in events if e["order_key"] == "binance:main:7100001" and e["event_type"] == "fill")
    assert fill["ts_exchange"] == T0 + 10 * H + 5 * NS  # the bar time, clamped to the live entry 10:00:05
    assert "bar time 2024-03-01T10:00:00.000Z clamped" in fill["reason"]
    # without a timeframe the window is 60 s: the 09:00 decisions are an hour from the live orders
    alone = qlive.import_sim("backtrader", [str(BT)], tmp_path / "w", instrument_map=str(IMAP), pair_with=str(live))
    assert any("0 of 7 simulator order(s) paired" in w for w in alone.warnings)
    _err("QT-INPUT-001", qlive.import_sim, "vectorbt", [str(VCSV)], tmp_path / "y", pair_with=str(BT))
    _err("QT-CONFIG-001", qlive.import_sim, "vectorbt", [str(VCSV)], tmp_path / "y", pair_with=[str(live)] * 2)


@pytest.mark.parametrize(
    ("fn", "sim", "n_sim", "extra"),
    [
        (bt.reconcile_backtrader, BT, 7, {"sim:backtrader:main:7": "no_live_twin"}),
        (vb.reconcile_vectorbt, VCSV, 6, {}),
    ],
)
def test_reconcile_end_to_end(tmp_path: Path, live: Path, fn: Any, sim: Path, n_sim: int, extra: Any) -> None:
    rep = fn(sim, live, tmp_path / "rec", timeframe="1h")
    assert {(r.live_key or r.sim_key): r.category for r in rep.orders} == {**CATEGORIES, **extra}
    assert (rep.n_live_orders, rep.n_sim_orders, rep.n_paired) == (7, n_sim, 6)
    assert rep.kind == {"bt": "backtrader", "vbt": "vectorbt"}[sim.name.split("_")[0]]
    sc = rep.score
    assert sc is not None and sc.two_by_two == {"both": 4, "neither": 0, "live_only": 0, "sim_only": 1}
    assert rep.missed_fill_notional == {"BTCUSDT": "608"}
    price = next(r for r in rep.orders if r.category == "price")
    # the simulator log's price at the 0.01 tick: 61210.12 vs 61190.50
    assert price.sim_avg_px == "61210.12" and price.sim_price_advantage_bps == pytest.approx(3.20638, abs=1e-5)
    timing = next(r for r in rep.orders if r.category == "timing")
    assert "outside the backtest fill candle [2024-03-01T20:00:00.000Z, +1h)" in timing.explanation
    doc = json.loads((tmp_path / "rec" / "reconcile.json").read_text())
    assert ft.ReconcileReport.model_validate(doc) == rep and doc["schema"] == "qt.bot_reconcile"
    assert (tmp_path / "rec" / "reconcile_orders.csv").read_text().count("\n") == len(rep.orders) + 1
    # run_id over the inputs' content: the same for another output directory, and the report is byte-identical
    rep2 = fn(sim, live, tmp_path / "x" / "rec2", timeframe="1h")
    assert rep2.run_id == rep.run_id
    assert (tmp_path / "rec" / "reconcile.json").read_bytes() == (
        tmp_path / "x" / "rec2" / "reconcile.json"
    ).read_bytes()
    # without an instrument map the ids and scales come from the live log; with it, the same report
    mapped = fn(sim, live, tmp_path / "m", timeframe="1h", instrument_map=IMAP)
    assert mapped.counts == rep.counts and mapped.run_id != rep.run_id
    assert any(
        w.startswith("instrument_map: none given; instrument ids and scales taken from the live log")
        for w in rep.warnings
    )


def test_reconcile_options_and_refusals(tmp_path: Path, live: Path) -> None:
    # no timeframe: a 60 s pairing window and 60 s timing tolerance; the 09:00 decisions pair with nothing
    rep = bt.reconcile_backtrader(BT, live, tmp_path / "a", score=False)
    assert rep.n_paired == 0 and rep.score is None and rep.score_error == "not requested"
    assert any(w.startswith("pairing: only 0 of 7 live orders paired") for w in rep.warnings)
    assert rep.options["time_tol_ns"] == 60 * NS
    wide = bt.reconcile_backtrader(BT, live, tmp_path / "b", score=False, pair_window_ms=2 * 3_600_000)
    # live fills 20 to 90 minutes after the bar time, beyond 60 s (missed, partial and price rank before timing)
    assert wide.n_paired == 6 and wide.counts["timing"] == 3
    # fills one bar later (bars labelled by their open time, filled at the close): the 21:30 live fill is now inside
    # its bar [21:00, 22:00), the 10:20 and 12:35 ones before theirs
    late = vb.reconcile_vectorbt(VCSV, live, tmp_path / "c", score=False, timeframe="1h", fill_delay_bars=1)
    assert late.counts["timing"] == 2 and late.counts["match"] == 1
    named = vb.reconcile_vectorbt(VCSV, live, tmp_path / "d", score=False, timeframe="1h", instrument="ETHUSDT")
    assert named.n_paired == 0 and any(
        "simulator instruments ['ETHUSDT'] are not in the live log" in w for w in named.warnings
    )
    # the arms swapped: a simulator log as the live arm
    sim_log = tmp_path / "simlog"
    qlive.import_sim("vectorbt", [str(VCSV)], sim_log, instrument_map=str(IMAP), venue="binance")
    e = _err("QT-CONFIG-001", bt.reconcile_backtrader, BT, sim_log, tmp_path / "e", score=False)
    assert "arms swapped?" in str(e)
    given = bt.reconcile_backtrader(BT, sim_log, tmp_path / "e", score=False, arms_as_given=True)
    assert any(w.startswith("arms_as_given") for w in given.warnings)
    e = _err("QT-INPUT-001", vb.reconcile_vectorbt, VCSV, FIX / "binance_user_stream.jsonl", tmp_path / "f")
    assert "canonical live-log directory" in str(e)
    _err("QT-CONFIG-001", vb.reconcile_vectorbt, VCSV, [live, live], tmp_path / "g")
    _err("QT-CONFIG-001", vb.reconcile_vectorbt, VCSV, live, tmp_path / "g", pair_with=str(live))
    _err("QT-CONFIG-001", vb.reconcile_vectorbt, VCSV, live, tmp_path / "g", pair_window_ms=-1)
    _err("QT-INPUT-001", vb.reconcile_vectorbt, tmp_path / "none.csv", live, tmp_path / "g")


def test_import_live_api_and_spec(tmp_path: Path) -> None:
    with pytest.warns(qlive.LiveImportWarning, match="unmapped_instruments"):  # no instrument map
        s = api.import_live(
            {"venue": "sim_vectorbt", "inputs": [{"path": str(VCSV)}], "out": str(tmp_path / "v")}, order_type="limit"
        )
    assert s.venue == "sim:vectorbt" and s.validation_status == "simulated"
    with pytest.warns(qlive.LiveImportWarning, match="unmapped_instruments"):
        s = qlive.import_venue({"venue": "sim_backtrader", "inputs": [{"path": str(BT)}], "out": str(tmp_path / "b")})
    assert s.n_orders == 7
    _err("QT-CONFIG-001", qlive.import_venue, {"venue": "sim_backtrader", "inputs": [{"path": str(BT)}],
                                              "out": str(tmp_path / "c"), "tz": "UTC"})  # fmt: skip


def test_import_backtrader_and_vectorbt_helpers(tmp_path: Path) -> None:
    a = bt.import_backtrader(str(BT), tmp_path / "a", instrument_map=str(IMAP), venue="binance")
    b = qlive.import_sim("backtrader", [str(BT)], tmp_path / "b", instrument_map=str(IMAP), venue="binance")
    assert _rows(tmp_path / "a") == _rows(tmp_path / "b") and a.n_events == b.n_events
    with pytest.warns(qlive.LiveImportWarning, match="unmapped_instruments"):
        c = vb.import_vectorbt([str(VCSV)], tmp_path / "c", account="bt")
    assert c.n_orders == 6 and all(o["account"] == "bt" for o in _rows(tmp_path / "c")[0])
    res = vb.import_vectorbt_sim([str(VCSV)], ImportOptions(extra={"venue": "binance"}))
    assert res.venue == "sim:vectorbt" and len(res.orders) == 6


def test_pair_sim_live_scales_linearly() -> None:
    # 50,000 orders per arm: a year of hourly decisions on six instruments
    import time

    rec = bk.Record({}, "", "x", 0, 0)
    live, sim = [], []
    for i in range(50_000):
        side = "buy" if i % 2 == 0 else "sell"
        inst = f"I{i % 6}"
        t = T0 + (i // 6) * H
        live.append(
            bk.LiveOrder(f"v:a:{i}", "v", inst, side, "limit", Decimal(1), Decimal(100), t + 5 * NS, [], None, "open")
        )
        sim.append(bk.SimOrder(str(i), inst, side, "limit", "gtc", Decimal(1), Decimal(100), t, "s", rec))
    t0 = time.perf_counter()
    pairs = bk.pair_sim_live(live, sim, window_ns=2 * H)
    elapsed = time.perf_counter() - t0
    assert sum(1 for lo, so in pairs if lo is not None and so is not None) == 50_000
    assert elapsed < 20, elapsed
