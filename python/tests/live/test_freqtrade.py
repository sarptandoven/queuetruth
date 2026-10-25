"""Freqtrade importers, pairing, reconciliation and the candle-fill audit (`queuetruth.live.importers.freqtrade`) on
the fixtures of tests/fixtures/live/freqtrade (written from Freqtrade's source at commit 1c0d709401b5) and on
malformed inputs."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.live import read_log, table_rows
from queuetruth.live.importers import BOT_IMPORTERS, SIM_IMPORTERS, ImportOptions
from queuetruth.live.importers import freqtrade as ft

FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "live" / "freqtrade"
STEM = "backtest-result-2024-03-01_14-00-00"
ZIP = FIX / f"{STEM}.zip"
IMAP = FIX / "instrument_map.csv"
LOG = FIX / "freqtrade.log"
NS = 1_000_000_000
T10 = 1_709_287_200 * NS  # 2024-03-01T10:00:00Z


def _db(tmp: Path, name: str = "tradesv3.dryrun") -> Path:
    out = tmp / f"{name}.sqlite"
    if not out.exists():
        con = sqlite3.connect(out)
        con.executescript((FIX / f"{name}.sql").read_text())
        con.close()
    return out


def _sql_db(tmp: Path, name: str, sql: str) -> Path:
    out = tmp / name
    con = sqlite3.connect(out)
    con.executescript(sql)
    con.close()
    return out


def _err(code: str, fn: Any, *a: Any, **kw: Any) -> QtError:
    with pytest.raises(QtError) as e:
        fn(*a, **kw)
    assert e.value.code == code, str(e.value)
    return e.value


def _bt_doc() -> dict[str, Any]:
    return json.loads((FIX / f"{STEM}.json").read_text())


def _write_bt(tmp: Path, doc: Any, name: str = "bt.json") -> Path:
    p = tmp / name
    p.write_text(json.dumps(doc))
    return p


def _events(out: Path) -> dict[str, list[dict[str, Any]]]:
    _, ev = read_log(out)
    by: dict[str, list[dict[str, Any]]] = {}
    for e in table_rows(ev):
        by.setdefault(e["order_key"], []).append(e)
    return by


# ---- fixtures and helpers --------------------------------------------------------------------------------------------


def test_fixture_generator_is_current() -> None:
    spec = importlib.util.spec_from_file_location("ft_make_fixtures", FIX / "make_fixtures.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (FIX / f"{STEM}.json").read_text() == mod._dump(mod.results())
    assert (FIX / f"{STEM}_config.json").read_text() == mod._dump(mod.config())
    with zipfile.ZipFile(ZIP) as zf:
        assert sorted(zf.namelist()) == [f"{STEM}.json", f"{STEM}_config.json"]
        assert zf.read(f"{STEM}.json").decode() == mod._dump(mod.results())


def test_registered_in_importer_registries() -> None:
    assert SIM_IMPORTERS["freqtrade"] is ft.import_freqtrade_sim
    assert BOT_IMPORTERS["freqtrade"] is ft.import_freqtrade
    assert "freqtrade" not in qlive.IMPORTERS  # not a LiveImportSpec venue (docs/design/extensions)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("1m", 60), ("5m", 300), (" 15m ", 900), ("1h", 3600), ("4h", 14_400), ("1d", 86_400), ("1w", 604_800), (5, 300)],
)
def test_timeframe_seconds(value: Any, seconds: int) -> None:
    assert ft.timeframe_seconds(value) == seconds


@pytest.mark.parametrize("value", ["1M", "0m", "5", "m5", "", True, 0, -5, "5x"])
def test_timeframe_seconds_rejects(value: Any) -> None:
    _err("QT-CONFIG-001", ft.timeframe_seconds, value)


def test_sig15_and_num() -> None:
    assert ft.num(0.30000000000000004, "x", "l") == Decimal("0.3")
    assert ft.num(60588.00000000001, "x", "l") == Decimal("60588")
    assert ft.num(3423.9000000000005, "x", "l") == Decimal("3423.9")
    assert ft.num(0.0016, "x", "l") == Decimal("0.0016")
    assert ft.num("61000.123456789", "x", "l") == Decimal("61000.123456789")
    assert ft.num(None, "x", "l") is None
    _err("QT-INPUT-002", ft.num, float("nan"), "x", "l")
    _err("QT-INPUT-002", ft.num, float("inf"), "x", "l")
    _err("QT-INPUT-002", ft.num, True, "x", "l")
    assert ft.dtext(Decimal("6.1E+4")) == "61000" and ft.dtext(Decimal("-0")) == "0"
    assert ft.duration_text(601 * NS) == "10m1s" and ft.duration_text(250_000_000) == "250ms"
    assert ft.duration_text(2_300_000_000) == "2.3s" and ft.duration_text(7_152_000_000) == "7.2s"
    assert ft.duration_text(4 * NS) == "4s" and ft.duration_text(59_970_000_000) == "1m"
    assert ft.duration_text(600_400_000_000) == "10m" and ft.duration_text(600_600_000_000) == "10m1s"
    assert ft.iso_ns(T10 + 481_000_000) == "2024-03-01T10:00:00.481Z"


# ---- backtest results ------------------------------------------------------------------------------------------------


def test_read_backtest_zip_json_and_last_result_agree() -> None:
    runs = [ft.read_backtest(p) for p in (ZIP, FIX / f"{STEM}.json", FIX / ".last_result.json")]
    shapes = [[(o.ref, o.side, o.qty, o.limit_px, o.filled_ns, o.placed_ns) for o in r.orders()] for r in runs]
    assert shapes[0] == shapes[1] == shapes[2]
    r = runs[0]
    assert r.kind == "backtest" and r.venue == "binance" and r.timeframe_s == 300 and r.strategy == "SampleStrategy"
    assert r.stake_currency == "USDT" and r.stats["timedout_entry_orders"] == 1
    assert [t.pair for t in r.trades] == ["BTC/USDT", "ETH/USDT", "BTC/USDT", "ETH/USDT", "BTC/USDT"]
    assert len(r.orders()) == 10
    # floating-point export artifacts come back as the decimals Freqtrade meant
    assert r.trades[2].orders[1].limit_px == Decimal("60588") and r.trades[3].orders[1].limit_px == Decimal("3423.9")
    t5 = r.trades[4]
    assert t5.orders[0].placed_ns == T10 + 3 * 3600 * NS and t5.orders[0].filled_ns == T10 + (3 * 3600 + 600) * NS
    assert t5.orders[1].placed_ns == t5.orders[1].filled_ns  # exit placement is not exported: the fill candle
    assert t5.orders[0].fee == Decimal("0.001") * Decimal("0.0016") * Decimal("61500")


def test_read_backtest_strategy_selection(tmp_path: Path) -> None:
    doc = _bt_doc()
    doc["strategy"]["Other"] = doc["strategy"]["SampleStrategy"]
    p = _write_bt(tmp_path, doc)
    _err("QT-CONFIG-001", ft.read_backtest, p)
    assert ft.read_backtest(p, strategy="Other").strategy == "Other"
    _err("QT-CONFIG-001", ft.read_backtest, p, strategy="Missing")


def test_read_backtest_without_orders_derives_two_orders(tmp_path: Path) -> None:
    doc = _bt_doc()
    for t in doc["strategy"]["SampleStrategy"]["trades"]:
        del t["orders"]
    r = ft.read_backtest(_write_bt(tmp_path, doc))
    assert len(r.orders()) == 10 and any("pre-2022 export" in w for w in r.warnings)
    o = r.trades[0].orders
    assert (o[0].role, o[0].limit_px, o[1].role, o[1].limit_px) == ("entry", Decimal(61000), "exit", Decimal(61610))


def test_read_backtest_open_orders_are_unfilled(tmp_path: Path) -> None:
    # `select_filled_or_open_orders` also exports orders still open at the end: no order_filled_timestamp
    doc = _bt_doc()
    t = doc["strategy"]["SampleStrategy"]["trades"][4]
    t["is_open"] = True
    t["orders"][1]["order_filled_timestamp"] = None
    r = ft.read_backtest(_write_bt(tmp_path, doc))
    o = r.trades[4].orders[1]
    assert (o.filled, o.filled_ns, o.avg_px, o.status, o.fee) == (Decimal(0), None, None, "open", None)
    assert o.placed_ns == r.trades[4].orders[0].filled_ns and o.limit_px == Decimal(61800)
    assert any("1 exported order(s) without order_filled_timestamp" in w for w in r.warnings)
    res = ft.import_freqtrade_sim([str(_write_bt(tmp_path, doc, "bt2.json"))], ImportOptions())
    ev = [e["event_type"] for e in res.events if e["order_key"] == "sim:freqtrade:main:bt4-BTC_USDT-exit1"]
    assert ev == ["ack"]


def test_read_backtest_order_types_from_config(tmp_path: Path) -> None:
    r = ft.read_backtest(ZIP)
    types = [(o.ref, o.order_type, o.limit_px) for o in r.orders()]
    # the stop_loss exit takes order_types.stoploss (limit: a stop-limit order); the others entry / exit (limit)
    assert ("bt2-BTC/USDT-exit1", "stop_limit", Decimal("60588")) in types
    assert sum(1 for _, t, _ in types if t == "limit") == 9
    cfg = json.loads((FIX / f"{STEM}_config.json").read_text())
    cfg["order_types"] = {"entry": "limit", "exit": "market", "stoploss": "market"}
    (tmp_path / "bt_config.json").write_text(json.dumps(cfg))
    r2 = ft.read_backtest(_write_bt(tmp_path, _bt_doc()))
    by = {o.ref: (o.order_type, o.limit_px) for o in r2.orders()}
    assert by["bt0-BTC/USDT-exit1"] == ("market", None) and by["bt2-BTC/USDT-exit1"] == ("stop", None)
    assert by["bt0-BTC/USDT-entry1"] == ("limit", Decimal(61000))
    (tmp_path / "bt_config.json").unlink()
    r3 = ft.read_backtest(_write_bt(tmp_path, _bt_doc()))
    assert {o.order_type for o in r3.orders()} == {"limit"} and any("no order_types" in w for w in r3.warnings)


def test_read_backtest_short_trade_sides(tmp_path: Path) -> None:
    doc = _bt_doc()
    t = doc["strategy"]["SampleStrategy"]["trades"][0]
    t["is_short"] = True
    t["orders"][0]["ft_order_side"], t["orders"][1]["ft_order_side"] = "sell", "buy"
    r = ft.read_backtest(_write_bt(tmp_path, doc))
    assert [(o.role, o.side) for o in r.trades[0].orders] == [("entry", "sell"), ("exit", "buy")]


def test_read_backtest_timeframe_option_and_missing_timeframe(tmp_path: Path) -> None:
    doc = _bt_doc()
    del doc["strategy"]["SampleStrategy"]["timeframe"]
    p = _write_bt(tmp_path, doc)
    r = ft.read_backtest(p)
    assert r.timeframe_s is None and any("no timeframe" in w for w in r.warnings)
    assert ft.read_backtest(p, timeframe="1h").timeframe_s == 3600


@pytest.mark.parametrize(
    ("mutate", "code", "text"),
    [
        (lambda d: d.pop("strategy"), "QT-INPUT-002", "no 'strategy'"),
        (lambda d: d.update(strategy={}), "QT-INPUT-002", "no 'strategy'"),
        (lambda d: d["strategy"]["SampleStrategy"].pop("trades"), "QT-INPUT-002", "without a 'trades' list"),
        (lambda d: d["strategy"]["SampleStrategy"]["trades"].append(5), "QT-INPUT-002", "not an object"),
        (lambda d: d["strategy"]["SampleStrategy"]["trades"][0].pop("pair"), "QT-INPUT-002", "without pair"),
        (lambda d: d["strategy"]["SampleStrategy"]["trades"][0].pop("open_timestamp"), "QT-INPUT-002", "open_times"),
        (
            lambda d: d["strategy"]["SampleStrategy"]["trades"][0]["orders"][0].update(amount=-1.0),
            "QT-INPUT-002",
            "positive",
        ),
        (
            lambda d: d["strategy"]["SampleStrategy"]["trades"][0]["orders"][0].update(safe_price=None),
            "QT-INPUT-002",
            "safe_price",
        ),
        (
            lambda d: d["strategy"]["SampleStrategy"]["trades"][0]["orders"][0].pop("ft_is_entry"),
            None,
            None,
        ),
        (
            lambda d: [
                d["strategy"]["SampleStrategy"]["trades"][0]["orders"][0].pop(k)
                for k in ("ft_is_entry", "ft_order_side")
            ],
            "QT-INPUT-002",
            "ft_is_entry",
        ),
        (
            lambda d: d["strategy"]["SampleStrategy"]["trades"][0]["orders"][0].update(order_filled_timestamp=1.5),
            "QT-INPUT-002",
            "integer epoch",
        ),
        (
            lambda d: d["strategy"]["SampleStrategy"]["trades"][0].update(open_timestamp=12),
            "QT-INPUT-002",
            "epoch",
        ),
        (lambda d: d["strategy"]["SampleStrategy"].update(timeframe="1M"), "QT-CONFIG-001", "fixed-length"),
    ],
)
def test_read_backtest_malformed(tmp_path: Path, mutate: Any, code: str | None, text: str | None) -> None:
    doc = _bt_doc()
    mutate(doc)
    p = _write_bt(tmp_path, doc)
    if code is None:  # ft_order_side alone still gives the role
        assert ft.read_backtest(p).trades[0].orders[0].role == "entry"
        return
    e = _err(code, ft.read_backtest, p)
    assert text in str(e)


def test_read_backtest_malformed_files(tmp_path: Path) -> None:
    _err("QT-INPUT-002", ft.read_backtest, FIX / f"{STEM}.meta.json")
    trunc = tmp_path / "cut.zip"
    trunc.write_bytes(ZIP.read_bytes()[:200])
    _err("QT-INPUT-002", ft.read_backtest, trunc)
    other = tmp_path / "backtest-result-x.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("readme.txt", "x")
        zf.writestr("a_config.json", "{}")
    assert "no backtest result member" in str(_err("QT-INPUT-002", ft.read_backtest, other))
    renamed = tmp_path / "renamed.zip"
    renamed.write_bytes(ZIP.read_bytes())  # member name differs from the zip name: the single result member is used
    assert len(ft.read_backtest(renamed).orders()) == 10
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    _err("QT-INPUT-002", ft.read_backtest, bad)
    nan = tmp_path / "nan.json"
    doc = _bt_doc()
    doc["strategy"]["SampleStrategy"]["trades"][0]["orders"][0]["amount"] = "NaN"
    nan.write_text(json.dumps(doc).replace('"NaN"', "NaN"))
    _err("QT-INPUT-002", ft.read_backtest, nan)
    lst = _write_bt(tmp_path, [{"pair": "BTC/USDT"}], "list.json")
    assert "list-only" in str(_err("QT-INPUT-002", ft.read_backtest, lst))
    last = _write_bt(tmp_path, {"latest_backtest": "gone.zip"}, ".last_result.json")
    _err("QT-INPUT-001", ft.read_backtest, last)
    # Freqtrade writes a bare file name: a self reference, a path or a chain fails QT-INPUT-002
    selfref = _write_bt(tmp_path, {"latest_backtest": ".last_result.json"}, ".last_result.json")
    assert "another .last_result.json" in str(_err("QT-INPUT-002", ft.read_backtest, selfref))
    for bad_name in ("../x.zip", "/etc/passwd", "sub/x.zip", "..", 5):
        p = _write_bt(tmp_path, {"latest_backtest": bad_name}, ".last_result.json")
        assert "bare file name" in str(_err("QT-INPUT-002", ft.read_backtest, p))
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    _err("QT-INPUT-002", ft.read_backtest, empty)
    log = tmp_path / "x.log"
    log.write_text("plain text\n")
    _err("QT-INPUT-002", ft.read_backtest, log)


# ---- trades database and bot log -------------------------------------------------------------------------------------


def test_read_dry_run_database(tmp_path: Path) -> None:
    r = ft.read_trades_db(_db(tmp_path))
    assert (r.kind, r.venue, r.timeframe_s) == ("dry_run", "binance", 300)
    assert [t.ref for t in r.trades] == ["t1", "t2", "t3", "t4"] and len(r.orders()) == 12
    assert not r.warnings
    by = {o.ref: o for o in r.orders()}
    sl = by["dry_run_sell_BTC/USDT_0b7f3e91-d5a2-4e8c-b6f0-3a9c2d71e5b8"]
    assert (sl.role, sl.side, sl.order_type, sl.limit_px, sl.avg_px) == (
        "stoploss",
        "sell",
        "stop_limit",
        Decimal("59982.12"),
        Decimal("60570"),
    )
    # dry-run orders fill all at once: every order is filled in full or not at all
    assert all(o.filled in (0, o.qty) for o in r.orders())
    stops = [o for o in r.orders() if o.role == "stoploss" and o.filled == 0]
    assert [(o.trade.ref, o.status, o.cancel_reason) for o in stops] == [
        ("t1", "canceled", None),
        ("t3", "canceled", None),
        ("t4", "canceled", None),
    ]
    # a trade's fee belongs to an order only when exactly one order of that role filled
    first = by["dry_run_buy_BTC/USDT_6f1c0c5e-2b7d-4a51-9c3e-0d8b1f4e7a21"]
    assert (first.fee, first.fee_ccy) == (Decimal("0.0976"), "USDT")
    assert sl.fee == Decimal("0.096912")
    t4_exits = [o for o in r.trades[3].orders if o.role == "exit"]
    assert [o.fee for o in t4_exits] == [None, Decimal("0.098864")]  # one filled exit order: it carries the fee
    # a partially filled dry-run order cannot come from Freqtrade's dry-run: warned
    sql = (FIX / "tradesv3.dryrun.sql").read_text()
    old = "'closed','BTC/USDT','limit','buy',61500.0,61500.0,0.0016,0.0016,0.0,"
    edited = _sql_db(
        tmp_path, "edited.sqlite", sql.replace(old, old.replace("0.0016,0.0016,0.0,", "0.0016,0.0008,0.0008,"))
    )
    assert any("partially filled" in w for w in ft.read_trades_db(edited).warnings)


def test_read_live_spot_database(tmp_path: Path) -> None:
    r = ft.read_trades_db(_db(tmp_path, "tradesv3.live"))
    assert (r.kind, r.venue, r.timeframe_s) == ("live", "binance", 300) and len(r.orders()) == 15 and not r.warnings
    by = {o.ref: o for o in r.orders()}
    part = by["24871100004"]
    assert (part.status, part.filled, part.qty, part.avg_px) == ("canceled", Decimal("0.012"), Decimal("0.03"), 3400)
    assert part.cancel_reason == "cancelled due to timeout, partially filled"
    assert (by["24871100006"].order_type, by["24871100006"].limit_px) == ("market", None)
    assert (by["24871100008"].role, by["24871100008"].avg_px) == ("stoploss", Decimal(60560))


def test_read_live_database_and_options(tmp_path: Path) -> None:
    p = _db(tmp_path, "tradesv3.futures")
    r = ft.read_trades_db(p)
    assert (r.kind, r.venue) == ("live", "binanceusdm") and r.trades[0].futures and r.trades[0].is_short
    assert ft.read_trades_db(p, dry_run=True).kind == "dry_run"
    assert ft.read_trades_db(p, venue="Binance").venue == "binance"
    entry = r.trades[0].orders[0]
    assert entry.avg_px == Decimal("3399.95") and entry.role == "entry" and entry.side == "sell"
    mkt = next(o for o in r.orders() if o.order_type == "market")
    assert mkt.limit_px is None and mkt.avg_px == Decimal("3375")


def test_database_malformed(tmp_path: Path) -> None:
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)
    _err("QT-INPUT-002", ft.read_trades_db, junk)
    _err("QT-INPUT-001", ft.read_trades_db, tmp_path / "missing.sqlite")
    no_orders = _sql_db(tmp_path, "a.sqlite", "CREATE TABLE trades (id INTEGER, pair TEXT, open_date TEXT);")
    assert "no 'orders' table" in str(_err("QT-INPUT-002", ft.read_trades_db, no_orders))
    sql = (FIX / "tradesv3.futures.sql").read_text()
    cols = _sql_db(tmp_path, "b.sqlite", sql.replace("order_date DATETIME,", "order_datex DATETIME,"))
    assert "lacks column" in str(_err("QT-INPUT-002", ft.read_trades_db, cols))

    def variant(name: str, old: str, new: str) -> Path:
        assert old in sql
        return _sql_db(tmp_path, name, sql.replace(old, new, 1))

    mixed = variant("c.sqlite", "'8389765612345678901'", "'dry_run_sell_ETH/USDT:USDT_1'")
    assert "mixed" in str(_err("QT-INPUT-002", ft.read_trades_db, mixed))
    two = variant("d.sqlite", "PRIMARY KEY (id)\n);", "PRIMARY KEY (id)\n);\n")
    con = sqlite3.connect(two)
    con.execute(
        "INSERT INTO trades VALUES "
        "(2,'kraken','BTC/USD',0,'2024-03-01 09:00:00',NULL,NULL,NULL,NULL,NULL,NULL,NULL,5,'SPOT',0)"
    )
    con.commit()
    con.close()
    _err("QT-CONFIG-001", ft.read_trades_db, two)
    assert ft.read_trades_db(two, venue="x").venue == "x"
    st = variant("e.sqlite", "'closed','ETH/USDT:USDT','limit','sell'", "'pending','ETH/USDT:USDT','limit','sell'")
    assert "ccxt order status" in str(_err("QT-INPUT-002", ft.read_trades_db, st))
    over = variant("f.sqlite", "3399.9500000000003,0.12,0.12", "3399.9500000000003,0.12,0.5")
    assert "outside" in str(_err("QT-INPUT-002", ft.read_trades_db, over))
    side = variant("g.sqlite", "(1,1,'sell','ETH/USDT:USDT'", "(1,1,'short','ETH/USDT:USDT'")
    _err("QT-INPUT-002", ft.read_trades_db, side)
    date = variant("h.sqlite", "'2024-03-01 09:00:09.877000'", "'09:00 yesterday'")
    _err("QT-INPUT-002", ft.read_trades_db, date)
    orphan = variant("i.sqlite", "(4,1,'stoploss'", "(4,9,'stoploss'")
    r = ft.read_trades_db(orphan)
    assert r.skipped == {"order row whose trade is not in the trades table": 1} and len(r.orders()) == 3
    # trades.timeframe is an integer number of minutes
    tf = variant("j.sqlite", "'exit_signal',5,'FUTURES'", "'exit_signal','5m','FUTURES'")
    assert "timeframe (minutes)" in str(_err("QT-INPUT-002", ft.read_trades_db, tf))


def test_bot_log_restores_deleted_entries(tmp_path: Path) -> None:
    run = ft.FtRun(kind="dry_run", venue="binance", timeframe_s=300, trades=[], paths=[])
    assert ft.read_bot_log(LOG, run=run) == 1
    (t,) = run.trades
    (o,) = t.orders
    assert (t.ref, t.pair, t.open_ns) == ("deleted-2-1709288103", "ETH/USDT", T10 + 903 * NS)
    assert (o.qty, o.limit_px, o.status, o.end_ns) == (
        Decimal("0.03"),
        Decimal("3400"),
        "canceled",
        T10 + 1504_117_000_000,
    )
    berlin = ft.FtRun(kind="dry_run", venue="binance", timeframe_s=300, trades=[], paths=[])
    ft.read_bot_log(LOG, tz="Europe/Berlin", run=berlin)
    assert berlin.trades[0].orders[0].end_ns == o.end_ns - 3600 * NS  # log asctime is the bot's local time


def test_bot_log_deletion_paths(tmp_path: Path) -> None:
    # the three ways Freqtrade deletes a trade (freqtradebot.py): handle_cancel_enter after a timeout (bot cancel)
    # or an exchange cancel, handle_onexchange_order and replace_order_failed
    tr = "Trade(id={i}, pair=ETH/USDT, amount=0.03, is_short=False, leverage=1, open_rate=3400, open_since={t})"
    a, b, c, d = (tr.format(i=i, t=f"2024-03-01 1{i}:00:00") for i in (1, 2, 3, 4))
    h = "2024-03-01 15:00:0{} - freqtrade.freqtradebot - {} - "
    log = tmp_path / "paths.log"
    log.write_text(
        "\n".join(
            [
                h.format(1, "INFO") + f"Buy order cancelled due to timeout for {a}.",
                h.format(2, "INFO") + f"Buy order fully cancelled. Removing {a} from database.",
                h.format(3, "INFO") + f"Buy order cancelled on exchange for {b}.",
                h.format(4, "INFO") + f"Buy order fully cancelled. Removing {b} from database.",
                h.format(5, "WARNING") + f"Trade only had fully canceled entry orders. Removing {c} from database.",
                h.format(6, "WARNING") + f"Could not fully cancel order for {d}, therefore not replacing.",
                h.format(7, "WARNING") + f"Removing {d} from database.",
                h.format(8, "WARNING") + "Removing Trade(id=9, pair=garbled) from database.",
            ]
        )
        + "\n"
    )
    run = ft.FtRun(kind="live", venue="binance", timeframe_s=300, trades=[], paths=[])
    assert ft.read_bot_log(log, run=run) == 4
    got = [(o.ref, o.bot_cancel, o.cancel_reason.split(";")[0]) for o in run.orders()]
    assert got == [
        ("deleted-1-1709290800", True, "cancelled due to timeout"),
        ("deleted-2-1709294400", False, "cancelled on exchange"),
        ("deleted-3-1709298000", False, "cancelled on exchange"),
        ("deleted-4-1709301600", True, "cancel to replace not confirmed, replacement abandoned"),
    ]
    assert any("1 'Removing Trade(...)' line(s)" in w for w in run.warnings)
    res = ft.import_freqtrade([str(log)], ImportOptions(extra={"venue": "binance", "dry_run": False}))
    by: dict[str, list[str]] = {}
    for e in res.events:
        by.setdefault(e["order_key"], []).append(e["event_type"])
    assert by["binance:main:deleted-1-1709290800"] == ["submit_sent", "cancel_sent", "cancel_ack"]
    assert by["binance:main:deleted-2-1709294400"] == ["submit_sent", "cancel_ack"]  # the exchange cancelled
    assert by["binance:main:deleted-3-1709298000"] == ["submit_sent", "cancel_ack"]
    assert by["binance:main:deleted-4-1709301600"] == ["submit_sent", "cancel_sent", "cancel_ack"]


def test_bot_log_malformed(tmp_path: Path) -> None:
    good = LOG.read_text().splitlines()[5]
    bad = tmp_path / "bad.log"
    bad.write_text(
        good.replace("is_short=False", "is_short=maybe") + "\n" + good.replace("open_rate=3400", "open_rate=N/A")
    )
    run = ft.FtRun(kind="dry_run", venue="binance", timeframe_s=300, trades=[], paths=[])
    assert ft.read_bot_log(bad, run=run) == 0
    assert any("2 'Removing Trade(...)' line(s) not in a recognised format" in w for w in run.warnings)
    contra = tmp_path / "contra.log"
    contra.write_text(good.replace("Buy order", "Sell order"))
    _err("QT-INPUT-002", ft.read_bot_log, contra, run=run)
    zero = tmp_path / "zero.log"
    zero.write_text(good.replace("amount=0.03", "amount=0"))
    _err("QT-INPUT-002", ft.read_bot_log, zero, run=run)
    _err("QT-CONFIG-001", ft.read_bot_log, LOG, tz="Mars/Base", run=run)


def test_bot_run_input_rules(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _err("QT-CONFIG-001", ft.read_bot_run, [str(db), str(ZIP)], {})
    db2 = tmp_path / "copy.sqlite"
    db2.write_bytes(db.read_bytes())
    _err("QT-CONFIG-001", ft.read_bot_run, [str(db), str(db2)], {})
    only_log = ft.read_bot_run([str(LOG)], {"venue": "binance"})
    assert len(only_log.orders()) == 1 and any("no trades database" in w for w in only_log.warnings)
    assert ft.read_bot_run([str(db)], {"timeframe": "15m"}).timeframe_s == 900
    _err("QT-CONFIG-001", ft.read_run, [str(ZIP), str(FIX / f"{STEM}.json")], {})


# ---- canonical logs --------------------------------------------------------------------------------------------------


def test_import_dry_run_live_log(tmp_path: Path) -> None:
    s = ft.import_freqtrade_live([str(_db(tmp_path)), str(LOG)], tmp_path / "live", instrument_map=str(IMAP))
    assert s.validation_status == "simulated" and s.n_orders == 13 and s.n_fills == 8
    assert any("dry_run: fills are Freqtrade dry-run fills" in w for w in s.warnings)
    assert any("tif: Freqtrade does not store" in w for w in s.warnings)
    assert qlive.validate(tmp_path / "live") is not None
    orders = {o["order_key"]: o for o in table_rows(read_log(tmp_path / "live")[0])}
    ev = _events(tmp_path / "live")
    deleted = "binance:main:deleted-2-1709288103"
    assert [e["event_type"] for e in ev[deleted]] == ["submit_sent", "cancel_sent", "cancel_ack"]
    assert orders[deleted]["instrument_id"] == 2 and orders[deleted]["tif"] == "gtc"
    t4 = "binance:main:dry_run_buy_BTC_USDT_e47b2c90-3d1f-4a85-b9e6-8f2a0c5d7b13"
    assert [e["event_type"] for e in ev[t4]] == ["submit_sent", "ack", "fill"]
    fill = ev[t4][2]
    assert fill["ts_exchange"] is None and fill["ts_local_recv"] == T10 + (3 * 3600 + 372) * NS + 880_000_000
    assert fill["qty"] == 160_000 and fill["px"] == 61_500 * 10**9 and fill["derived"] is True
    assert {e["ts_precision"] for e in ev[t4]} == {"ms"}  # Freqtrade's dry-run order times are milliseconds
    timeout = "binance:main:dry_run_sell_BTC_USDT_1c6e9a2f-8b4d-4f37-a0e5-6d2b9c8f1e40"
    assert [e["event_type"] for e in ev[timeout]] == ["submit_sent", "ack", "cancel_sent", "cancel_ack"]
    stop = "binance:main:dry_run_sell_BTC_USDT_2e8f4b61-0a9c-4d73-b5e2-c71d9f30a846"
    assert [e["event_type"] for e in ev[stop]] == ["submit_sent", "ack", "cancel_ack"]  # no ft_cancel_reason
    sl = "binance:main:dry_run_sell_BTC_USDT_0b7f3e91-d5a2-4e8c-b6f0-3a9c2d71e5b8"
    assert orders[sl]["order_type"] == "stop_limit" and orders[sl]["limit_px"] == 59_982_120_000_000
    assert next(e for e in ev[sl] if e["event_type"] == "fill")["liquidity"] == "unknown"


def test_import_live_database_uses_exchange_clock(tmp_path: Path) -> None:
    with pytest.warns(qlive.LiveImportWarning, match="unmapped_instruments"):
        s = ft.import_freqtrade_live(str(_db(tmp_path, "tradesv3.futures")), tmp_path / "live", account="acct")
    assert s.validation_status == "fixture_only" and s.venue == "binanceusdm"
    ev = _events(tmp_path / "live")
    entry = ev["binanceusdm:acct:8389765612345678901"]
    assert [(e["event_type"], e["ts_exchange_kind"]) for e in entry] == [
        ("ack", "gateway_time"),
        ("fill", "record_time"),
    ]
    assert entry[1]["px"] == 3_399_950_000_000 and entry[1]["fee"] == pytest.approx(0.0407994)
    assert {e["ts_precision"] for e in entry} == {"ms"}  # ccxt timestamps are epoch milliseconds
    mkt = ev["binanceusdm:acct:8389765612345679555"]
    assert mkt[-1]["liquidity"] == "taker"
    # cancelled by the exchange (or without a bot reason): no cancel_sent
    assert [e["event_type"] for e in ev["binanceusdm:acct:8389765612345679001"]] == ["ack", "cancel_ack"]
    assert [e["event_type"] for e in ev["binanceusdm:acct:8389765612345678950"]] == ["ack", "cancel_ack"]
    orders = {o["order_key"]: o for o in table_rows(read_log(tmp_path / "live")[0])}
    assert orders["binanceusdm:acct:8389765612345679555"]["reduce_only"] is True
    assert orders["binanceusdm:acct:8389765612345678901"]["reduce_only"] is None
    with pytest.warns(qlive.LiveImportWarning):
        loc = ft.import_freqtrade_live(
            str(_db(tmp_path, "tradesv3.futures")), tmp_path / "loc", order_date_clock="local", tif="po"
        )
    ev2 = _events(tmp_path / "loc")
    assert [e["event_type"] for e in ev2["binanceusdm:main:8389765612345678901"]][:2] == ["submit_sent", "ack"]
    assert loc.n_orders == 4
    assert {o["tif"] for o in table_rows(read_log(tmp_path / "loc")[0]) if o["order_type"] == "limit"} == {"post_only"}


def test_import_options_and_units(tmp_path: Path) -> None:
    db = str(_db(tmp_path))
    _err("QT-CONFIG-001", ft.import_freqtrade_live, db, tmp_path / "a", bogus=1)
    _err("QT-CONFIG-001", ft.import_freqtrade_live, db, tmp_path / "b", tif="day")
    _err("QT-CONFIG-001", ft.import_freqtrade_live, db, tmp_path / "c", order_date_clock="gps")
    _err("QT-INPUT-004", ft.import_freqtrade_live, db, tmp_path / "d", price_exp=-1)  # 59982.12 at 0.1
    _err("QT-INPUT-001", ft.import_freqtrade_live, str(tmp_path / "missing.sqlite"), tmp_path / "e")
    _err("QT-CONFIG-001", ft.import_freqtrade, [str(ZIP)], ImportOptions())
    # malformed integer options fail QT-CONFIG-001, not a Python ValueError
    for bad in ({"price_exp": "-x"}, {"size_exp": 1.5}, {"price_exp": True}):
        _err("QT-CONFIG-001", ft.import_freqtrade_live, db, tmp_path / "g", **bad)
    _err(
        "QT-CONFIG-001",
        ft.import_freqtrade_sim,
        [str(ZIP)],
        ImportOptions(extra={"pair_tolerance_candles": "x", "pair_with": db}),
    )
    _err("QT-CONFIG-001", ft.import_freqtrade_sim, [str(ZIP)], ImportOptions(extra={"live_venue": "x"}))
    _err("QT-CONFIG-001", ft.import_freqtrade_live, db, tmp_path / "h", live_dry_run=True)


# ---- pairing and the simulator log -----------------------------------------------------------------------------------


PAIRS = {
    "dry_run_buy_BTC/USDT_6f1c0c5e-2b7d-4a51-9c3e-0d8b1f4e7a21": "bt0-BTC/USDT-entry1",
    "dry_run_sell_BTC/USDT_9a0e5d14-7c3f-4f0b-8e61-52b9c6d0a3f4": "bt0-BTC/USDT-exit1",
    "deleted-2-1709288103": "bt1-ETH/USDT-entry1",
    "dry_run_buy_BTC/USDT_c3d9e2a7-41b8-4c6e-a2f5-7e0d93b1c845": "bt2-BTC/USDT-entry1",
    "dry_run_sell_BTC/USDT_0b7f3e91-d5a2-4e8c-b6f0-3a9c2d71e5b8": "bt2-BTC/USDT-exit1",
    "dry_run_buy_BTC/USDT_e47b2c90-3d1f-4a85-b9e6-8f2a0c5d7b13": "bt4-BTC/USDT-entry1",
    "dry_run_sell_BTC/USDT_1c6e9a2f-8b4d-4f37-a0e5-6d2b9c8f1e40": "bt4-BTC/USDT-exit1",
}


def test_pair_runs_scenario(tmp_path: Path) -> None:
    live = ft.read_bot_run([str(_db(tmp_path)), str(LOG)], {})
    bt = ft.read_backtest(ZIP)
    p = ft.pair_runs(live, bt)
    got = {lo.ref: so.ref for lo, so in p.pairs if lo is not None and so is not None}
    assert got == PAIRS
    assert sorted(lo.ref for lo, so in p.pairs if so is None) == [
        "dry_run_buy_ETH/USDT_5e2a8c47-9f1d-4b3a-8d27-c0e6f4a19b52",
        "dry_run_sell_BTC/USDT_2e8f4b61-0a9c-4d73-b5e2-c71d9f30a846",  # untriggered stop-losses: no backtest twin
        "dry_run_sell_BTC/USDT_7d3b0f8e-c2a6-4e19-b5d4-90f1a7e2c6b8",
        "dry_run_sell_BTC/USDT_f05b3d92-7e61-4a2c-8d4f-b19c6e07a253",
        "dry_run_sell_ETH/USDT_9c4a7e20-5b13-4f8d-a6c9-03e2b7d51f94",
        "dry_run_sell_ETH/USDT_a81f4d3c-62e9-4b70-95c8-1d7e0b2f6a93",
    ]
    assert sorted(so.ref for lo, so in p.pairs if lo is None) == [
        "bt1-ETH/USDT-exit1",
        "bt3-ETH/USDT-entry1",
        "bt3-ETH/USDT-exit1",
    ]
    assert len(p.trade_pairs) == 4 and p.timeframe_s == 300
    # tolerance 0 still pairs every trade (entries were placed in the backtest's candle); a 1h timeframe merges
    # candles, so the 12:00 backtest trade and the 12:35 dry-run trade pair as well
    assert len(ft.pair_runs(live, bt, tolerance=0).trade_pairs) == 4
    assert len(ft.pair_runs(live, bt, timeframe_s=3600).trade_pairs) == 5
    _err("QT-CONFIG-001", ft.pair_runs, live, bt, tolerance=-1)
    bt.timeframe_s = live.timeframe_s = None
    _err("QT-CONFIG-001", ft.pair_runs, live, bt)


def test_import_sim_pairs_backtest_with_live_keys(tmp_path: Path) -> None:
    db = str(_db(tmp_path))
    s = qlive.import_sim(
        "freqtrade", [str(ZIP)], tmp_path / "sim", instrument_map=str(IMAP), pair_with=[db, str(LOG)], account="main"
    )
    assert s.venue == "sim:freqtrade" and s.validation_status == "simulated" and s.n_orders == 10 and s.n_fills == 10
    assert any("7 of 10 simulator order(s) paired" in w for w in s.warnings)
    # the simulator manifest does not list the pair_with inputs: the warning records them and their hashes
    assert any("pair_with: keys taken from" in w and "tradesv3.dryrun.sqlite (sha256 " in w for w in s.warnings)
    assert any("4 backtest fill time(s) clamped" in w for w in s.warnings)
    orders = {o["order_key"]: o for o in table_rows(read_log(tmp_path / "sim")[0])}
    keys = {ft.make_order_key("binance", "main", k) for k in PAIRS}
    assert keys <= set(orders)
    assert all(o["venue"] == "sim:freqtrade" and o["instrument_id"] in (1, 2) for o in orders.values())
    assert "sim:freqtrade:main:bt3-ETH_USDT-entry1" in orders
    ev = _events(tmp_path / "sim")
    # the deleted dry-run entry (placed 10:15:03) pairs with the backtest fill of the 10:15 candle: clamped
    fill = next(e for e in ev["binance:main:deleted-2-1709288103"] if e["event_type"] == "fill")
    assert fill["ts_exchange"] == T10 + 903 * NS and "clamped" in fill["reason"] and fill["source"] == "sim"
    # a later candle needs no clamp
    late = next(
        e
        for e in ev[ft.make_order_key("binance", "main", "dry_run_buy_BTC/USDT_e47b2c90-3d1f-4a85-b9e6-8f2a0c5d7b13")]
        if e["event_type"] == "fill"
    )
    assert late["ts_exchange"] == T10 + 3 * 3600 * NS + 600 * NS and "clamped" not in late["reason"]
    with pytest.warns(qlive.LiveImportWarning):
        unpaired = qlive.import_sim("freqtrade", [str(ZIP)], tmp_path / "sim2")
    assert any("no pair_with" in w for w in unpaired.warnings) and unpaired.unmapped_instruments


def test_import_sim_rounds_backtest_values(tmp_path: Path) -> None:
    doc = _bt_doc()
    doc["strategy"]["SampleStrategy"]["trades"][0]["orders"][1]["safe_price"] = 61610.123456789123
    p = _write_bt(tmp_path, doc)
    res = ft.import_freqtrade_sim([str(p)], ImportOptions(extra={"price_exp": -2}))
    assert any("rounded half-even" in w for w in res.warnings)
    px = {o["order_key"]: o["limit_px"] for o in res.orders}
    assert px["sim:freqtrade:main:bt0-BTC_USDT-exit1"] == 6_161_012


# ---- explanation -----------------------------------------------------------------------------------------------------


def _v(**kw: Any) -> ft.OrderView:
    base: dict[str, Any] = {
        "key": "k",
        "instrument": "BTC/USDT",
        "side": "buy",
        "role": "entry",
        "order_type": "limit",
        "qty": Decimal(1),
        "limit_px": Decimal(100),
        "placed_ns": T10,
        "filled_qty": Decimal(0),
        "avg_px": None,
        "first_fill_ns": None,
        "end_ns": None,
        "status": "open",
    }
    base.update(kw)
    return ft.OrderView(**base)


def _x(live: ft.OrderView | None, sim: ft.OrderView | None, **kw: Any) -> ft.ReconcileOrder:
    args: dict[str, Any] = {
        "price_tol_bps": 0.5,
        "time_tol_ns": NS,
        "sim_name": "sim",
        "live_name": "live",
        "no_sim_hint": "h1",
        "no_live_hint": "h2",
    }
    args.update(kw)
    return ft.explain_pair(live, sim, **args)


def test_explain_pair_categories() -> None:
    f = {"filled_qty": Decimal(1), "avg_px": Decimal(100), "first_fill_ns": T10 + NS, "status": "closed"}
    assert _x(_v(**f), _v(key="s", **f)).category == "match"
    r = _x(_v(status="cancelled", end_ns=T10 + 60 * NS, note="cancelled due to timeout"), _v(key="s", **f))
    assert r.category == "missed_fill" and "rested 1m" in r.explanation and "cancelled due to timeout" in r.explanation
    assert _x(_v(**f), _v(key="s", status="rejected")).category == "extra_live_fill"
    assert _x(_v(status="cancelled"), _v(key="s", status="cancelled")).category == "both_unfilled"
    part = _x(_v(**{**f, "filled_qty": Decimal("0.5")}), _v(key="s", **f))
    assert part.category == "partial_fill" and part.live_status == "partial"
    late = _x(_v(**{**f, "first_fill_ns": T10 + 5 * NS}), _v(key="s", **f))
    assert late.category == "timing" and late.fill_delay_ns == 4 * NS and "4s after the sim" in late.explanation
    in_tol = _x(_v(**{**f, "first_fill_ns": T10 + NS + 999_000_000}), _v(key="s", **f))
    assert in_tol.category == "match"
    # buy: the simulator paid less -> positive advantage; sell: the simulator received more -> positive
    cheap = _x(_v(**{**f, "avg_px": Decimal(101)}), _v(key="s", **f))
    assert cheap.category == "price" and cheap.sim_price_advantage_bps == pytest.approx(99.00990099)
    assert cheap.sim_price_advantage_quote == "1"
    sell = _x(_v(side="sell", **{**f, "avg_px": Decimal(101)}), _v(key="s", side="sell", **f))
    assert sell.sim_price_advantage_bps == pytest.approx(-99.00990099) and "worse" in sell.explanation
    small = _x(_v(**{**f, "avg_px": Decimal("100.004")}), _v(key="s", **f))
    assert small.category == "match" and small.sim_price_advantage_bps == pytest.approx(0.39998400064)
    # backtest resolution: a live fill inside the fill candle is on time, outside it is a timing issue
    bt = {**f, "first_fill_ns": T10, "status": "closed"}
    inside = _x(_v(**{**f, "first_fill_ns": T10 + 299 * NS}), _v(key="s", resolution_ns=300 * NS, **bt), time_tol_ns=0)
    outside = _x(_v(**{**f, "first_fill_ns": T10 + 300 * NS}), _v(key="s", resolution_ns=300 * NS, **bt), time_tol_ns=0)
    assert inside.category == "match" and outside.category == "timing"
    assert _x(_v(), None).category == "no_sim_twin" and _x(None, _v(key="s", **f)).category == "no_live_twin"
    assert "h1" in _x(_v(), None).explanation and "h2" in _x(None, _v(key="s", **f)).explanation
    # an untriggered stop-loss on one side (or both) is agreement, not a missing twin
    stop = {"role": "stoploss", "side": "sell", "status": "cancelled"}
    one = _x(_v(**stop), None)
    assert one.category == "stop_not_triggered" and one.issues == [] and "never triggered" in one.explanation
    assert _x(None, _v(key="s", **stop)).category == "stop_not_triggered"
    assert _x(_v(**stop), _v(key="s", **stop)).category == "stop_not_triggered"
    # a triggered (filled) stop-loss without a twin is a missing twin
    assert _x(_v(**{**f, **stop, "status": "closed"}), None).category == "no_sim_twin"
    # a filled market order without an average price
    mkt = _x(_v(order_type="market", limit_px=None, filled_qty=Decimal(1), first_fill_ns=T10), None)
    assert "filled 1 at unknown price" in mkt.explanation


def test_explain_pairs_sorting_and_summary() -> None:
    f = {"filled_qty": Decimal(2), "avg_px": Decimal(100), "first_fill_ns": T10, "status": "closed"}
    rows = ft.explain_pairs(
        [
            (_v(key="b", placed_ns=T10 + 5), _v(key="sb", placed_ns=T10 + 5, **f)),
            (
                _v(key="a", placed_ns=T10 + 9, **{**f, "avg_px": Decimal(101), "first_fill_ns": T10 + 9}),
                _v(key="sa", placed_ns=T10 + 9, **{**f, "first_fill_ns": T10 + 9}),
            ),
        ],
        score_classes={"b": "sim_only"},
    )
    assert [r.live_key for r in rows] == ["b", "a"] and rows[0].score_class == "sim_only"
    counts, adv, missed = ft.summarize(rows)
    assert counts["missed_fill"] == 1 and counts["price"] == 1 and sum(counts.values()) == 2
    assert adv == {"BTC/USDT": "2"} and missed == {"BTC/USDT": "200"}


# ---- end to end ------------------------------------------------------------------------------------------------------


EXPECTED_CATEGORIES = {
    "binance:main:dry_run_buy_BTC_USDT_6f1c0c5e-2b7d-4a51-9c3e-0d8b1f4e7a21": "match",
    "binance:main:dry_run_sell_BTC_USDT_2e8f4b61-0a9c-4d73-b5e2-c71d9f30a846": "stop_not_triggered",
    "binance:main:dry_run_sell_BTC_USDT_9a0e5d14-7c3f-4f0b-8e61-52b9c6d0a3f4": "match",
    "binance:main:deleted-2-1709288103": "missed_fill",
    "binance:main:dry_run_buy_BTC_USDT_c3d9e2a7-41b8-4c6e-a2f5-7e0d93b1c845": "match",
    "binance:main:dry_run_sell_BTC_USDT_0b7f3e91-d5a2-4e8c-b6f0-3a9c2d71e5b8": "price",
    "binance:main:dry_run_buy_BTC_USDT_e47b2c90-3d1f-4a85-b9e6-8f2a0c5d7b13": "timing",
    "binance:main:dry_run_sell_BTC_USDT_f05b3d92-7e61-4a2c-8d4f-b19c6e07a253": "stop_not_triggered",
    "binance:main:dry_run_sell_BTC_USDT_1c6e9a2f-8b4d-4f37-a0e5-6d2b9c8f1e40": "missed_fill",
    "binance:main:dry_run_buy_ETH_USDT_5e2a8c47-9f1d-4b3a-8d27-c0e6f4a19b52": "no_sim_twin",
    "binance:main:dry_run_sell_ETH_USDT_9c4a7e20-5b13-4f8d-a6c9-03e2b7d51f94": "stop_not_triggered",
    "binance:main:dry_run_sell_ETH_USDT_a81f4d3c-62e9-4b70-95c8-1d7e0b2f6a93": "no_sim_twin",
    "binance:main:dry_run_sell_BTC_USDT_7d3b0f8e-c2a6-4e19-b5d4-90f1a7e2c6b8": "no_sim_twin",
    "sim:freqtrade:main:bt1-ETH_USDT-exit1": "no_live_twin",
    "sim:freqtrade:main:bt3-ETH_USDT-entry1": "no_live_twin",
    "sim:freqtrade:main:bt3-ETH_USDT-exit1": "no_live_twin",
}


def _counts(**kw: int) -> dict[str, int]:
    return {c: kw.get(c, 0) for c in ft.CATEGORIES}


@pytest.mark.requires_core
def test_reconcile_freqtrade_end_to_end(tmp_path: Path) -> None:
    db = _db(tmp_path)
    rep = ft.reconcile_freqtrade(ZIP, [db, LOG], tmp_path / "rec", instrument_map=IMAP)
    got = {(r.live_key or r.sim_key): r.category for r in rep.orders}
    assert got == EXPECTED_CATEGORIES
    assert rep.counts == _counts(
        match=3, missed_fill=2, price=1, timing=1, stop_not_triggered=3, no_sim_twin=3, no_live_twin=3
    )
    assert (rep.n_live_orders, rep.n_sim_orders, rep.n_paired) == (13, 10, 7)
    assert rep.pairing_coverage == pytest.approx(7 / 13)
    assert rep.sim_price_advantage_quote == {"BTC/USDT": "0.0288"}
    assert rep.missed_fill_notional == {"BTC/USDT": "98.88", "ETH/USDT": "102"}
    sc = rep.score
    assert sc is not None and rep.score_error is None
    assert sc.two_by_two == {"both": 4, "neither": 0, "live_only": 0, "sim_only": 2}
    assert (sc.n_orders, sc.n_matched, sc.n_unmatched_live, sc.n_unmatched_sim) == (13, 6, 3, 4)
    assert sc.fill_rate_bias == pytest.approx(2 / 6)
    # 6 matched orders: the significance statement is flagged as not interpretable
    assert sc.interpretable is False and sc.n_clusters == 5 and sc.path == "score"
    classes = {r.live_key: r.score_class for r in rep.orders if r.score_class}
    assert classes["binance:main:deleted-2-1709288103"] == "sim_only"
    assert classes["binance:main:dry_run_buy_BTC_USDT_6f1c0c5e-2b7d-4a51-9c3e-0d8b1f4e7a21"] == "both"
    price = next(r for r in rep.orders if r.category == "price")
    assert price.sim_price_advantage_bps == pytest.approx(18 / 60570 * 10_000, abs=1e-6)
    missed = next(r for r in rep.orders if r.live_key == "binance:main:deleted-2-1709288103")
    assert "candle rule" in missed.explanation and "rested 10m1s" in missed.explanation
    assert "cancelled due to timeout; trade removed" in missed.explanation
    timing = next(r for r in rep.orders if r.category == "timing")
    assert "outside the backtest fill candle [2024-03-01T13:10:00.000Z, +5m)" in timing.explanation
    assert any("the live arm is a dry-run bot" in w for w in rep.warnings)
    doc = json.loads((tmp_path / "rec" / "reconcile.json").read_text())
    assert ft.ReconcileReport.model_validate(doc) == rep and doc["live_log"] == "live"
    lines = (tmp_path / "rec" / "reconcile_orders.csv").read_text().splitlines()
    assert lines[0].startswith("category,live_key,sim_key") and len(lines) == 17
    assert (tmp_path / "rec" / "score" / "summary.json").exists()
    text = ft.render_reconcile(rep, max_rows=2)
    assert text.splitlines()[0] == (
        "reconcile (freqtrade): 13 live orders, 10 simulator orders, 7 paired (54% of live orders)"
    )
    assert text.splitlines()[1].startswith("score (not interpretable: 6 matched orders in 5 cluster(s)")
    assert "missed-fill notional ETH/USDT: 102 (quote)" in text
    # deterministic and independent of the output directory: a second run writes the same report, byte for byte
    rep2 = ft.reconcile_freqtrade(ZIP, [db, LOG], tmp_path / "elsewhere" / "rec2", instrument_map=IMAP)
    assert rep2.run_id == rep.run_id and rep2.score is not None and rep2.score.run_id == sc.run_id
    for name in ("reconcile.json", "reconcile_orders.csv"):
        assert (tmp_path / "rec" / name).read_bytes() == (tmp_path / "elsewhere" / "rec2" / name).read_bytes()


LIVE_CATEGORIES = {
    "24871100001": "match",
    "24871100002": "stop_not_triggered",
    "24871100003": "match",
    "24871100004": "partial_fill",
    "24871100005": "stop_not_triggered",
    "24871100006": "partial_fill",
    "24871100007": "match",
    "24871100008": "price",
    "24871100009": "match",
    "24871100010": "stop_not_triggered",
    "24871100011": "missed_fill",
    "24871100012": "no_sim_twin",
    "24871100013": "timing",
    "24871100014": "stop_not_triggered",
    "24871100015": "match",
}


@pytest.mark.requires_core
def test_reconcile_backtest_vs_live_end_to_end(tmp_path: Path) -> None:
    rep = ft.reconcile_freqtrade(ZIP, _db(tmp_path, "tradesv3.live"), tmp_path / "rec", instrument_map=IMAP)
    got = {(r.live_key or r.sim_key).removeprefix("binance:main:"): r.category for r in rep.orders}
    assert got == LIVE_CATEGORIES
    assert rep.counts == _counts(
        match=5, missed_fill=1, partial_fill=2, price=1, timing=1, stop_not_triggered=4, no_sim_twin=1
    )
    assert (rep.n_live_orders, rep.n_sim_orders, rep.n_paired) == (15, 10, 10)
    assert not any("dry-run" in w for w in rep.warnings)
    sc = rep.score
    assert sc is not None and sc.two_by_two == {"both": 7, "neither": 0, "live_only": 0, "sim_only": 1}
    assert (sc.n_orders, sc.n_matched, sc.n_unmatched_live, sc.n_unmatched_sim) == (15, 8, 1, 2)
    rows = {r.live_key: r for r in rep.orders}
    exit_mkt = rows["binance:main:24871100006"]
    # the market exit sold 0.012 of the backtest's 0.03, 1.4 USDT lower: both issues are listed
    assert exit_mkt.issues == ["partial_fill", "price"] and exit_mkt.sim_price_advantage_quote == "0.0168"
    assert rows["binance:main:24871100008"].sim_price_advantage_bps == pytest.approx(28 / 60560 * 10_000, abs=1e-6)
    # the paired backtest exit takes the live market type; the live log has exchange-clock acks and record_time fills
    sim_orders = {o["order_key"]: o for o in table_rows(read_log(tmp_path / "rec" / "sim")[0])}
    assert sim_orders["binance:main:24871100006"]["order_type"] == "market"
    assert sim_orders["binance:main:24871100006"]["limit_px"] is None
    assert sim_orders["binance:main:24871100008"]["order_type"] == "stop_limit"
    ev = _events(tmp_path / "rec" / "live")
    assert [(e["event_type"], e["ts_exchange_kind"]) for e in ev["binance:main:24871100004"]] == [
        ("ack", "gateway_time"),
        ("fill", "record_time"),
        ("cancel_sent", "unknown"),
        ("cancel_ack", "unknown"),
    ]


@pytest.mark.requires_core
def test_reconcile_dry_run_vs_live_end_to_end(tmp_path: Path) -> None:
    live = _db(tmp_path, "tradesv3.live")
    rep = ft.reconcile_freqtrade([_db(tmp_path), LOG], live, tmp_path / "rec", instrument_map=IMAP)
    assert rep.counts == _counts(
        extra_live_fill=2, price=1, timing=4, stop_not_triggered=5, no_sim_twin=4, no_live_twin=3
    )
    assert (rep.n_live_orders, rep.n_sim_orders, rep.n_paired) == (15, 13, 9)
    rows = {r.live_key: r for r in rep.orders if r.live_key}
    # two bots: fills within BOT_TIME_TOL_NS (5 s) are on time; the entry 28.6 s apart is a timing difference
    t1 = rows["binance:main:24871100001"]
    assert (
        t1.category == "timing" and t1.fill_delay_ns == 28_574_000_000 and "28.6s after the dry-run" in t1.explanation
    )
    assert rows["binance:main:24871100004"].category == "extra_live_fill"  # the dry-run entry timed out unfilled
    both_stops = rows["binance:main:24871100002"]
    assert both_stops.sim_key is not None and "neither stop-loss triggered" in both_stops.explanation
    sc = rep.score
    assert sc is not None and sc.two_by_two == {"both": 4, "neither": 0, "live_only": 2, "sim_only": 0}
    tight = ft.reconcile_freqtrade([_db(tmp_path), LOG], live, tmp_path / "t", score=False, time_tol_ns=60 * NS)
    assert tight.counts["timing"] == 2 and tight.run_id != rep.run_id


def test_reconcile_swapped_arms(tmp_path: Path) -> None:
    live, dry = _db(tmp_path, "tradesv3.live"), _db(tmp_path)
    e = _err("QT-CONFIG-001", ft.reconcile_freqtrade, live, [dry, LOG], tmp_path / "x", score=False)
    assert "arms swapped?" in str(e)
    rep = ft.reconcile_freqtrade(live, [dry, LOG], tmp_path / "y", score=False, arms_as_given=True)
    assert any(w.startswith("arms_as_given") for w in rep.warnings)


def test_reconcile_run_id_covers_every_input_and_option(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = ft.reconcile_freqtrade(ZIP, [db, LOG], tmp_path / "a", score=False)
    ids = {base.run_id}
    for i, kw in enumerate(
        (
            {"instrument_map": IMAP},
            {"score": True},
            {"window_ns": 3600 * NS},
            {"price_tol_bps": 2.0},
            {"time_tol_ns": NS},
            {"account": "acct"},
            {"pair_tolerance_candles": 2},
        )
    ):
        args: dict[str, Any] = {"score": False, **kw}
        ids.add(ft.reconcile_freqtrade(ZIP, [db, LOG], tmp_path / f"b{i}", **args).run_id)
    assert len(ids) == 8
    # an input's content, not its path, enters run_id (SPEC 7.3)
    moved = tmp_path / "moved.sqlite"
    moved.write_bytes(db.read_bytes())
    assert ft.reconcile_freqtrade(ZIP, [moved, LOG], tmp_path / "c", score=False).run_id == base.run_id
    spec = base.options
    assert spec["kind"] == "freqtrade_reconcile" and spec["instrument_map"] is None and spec["score"] is False
    assert spec["sim"] == [{"path": str(ZIP)}]
    from queuetruth.schemas.arrow import ENGINE_VERSION
    from queuetruth.schemas.runid import run_id

    assert base.run_id == run_id(spec, ENGINE_VERSION, lambda r: ft.sha256_file(r["path"]))


def test_reconcile_without_score_and_map(tmp_path: Path) -> None:
    rep = ft.reconcile_freqtrade(ZIP, [_db(tmp_path), LOG], tmp_path / "rec", score=False)
    assert rep.score is None and rep.score_error == "not requested"
    assert any(w.startswith("instrument_map: none given") for w in rep.warnings)
    orders = table_rows(read_log(tmp_path / "rec" / "live")[0])
    assert {(o["instrument"], o["instrument_id"]) for o in orders} == {("BTC/USDT", 1), ("ETH/USDT", 2)}
    _err("QT-CONFIG-001", ft.reconcile_freqtrade, ZIP, [LOG], tmp_path / "x", pair_with="y")
    _err("QT-INPUT-001", ft.reconcile_freqtrade, tmp_path / "none.zip", [LOG], tmp_path / "x")
    _err("QT-CONFIG-001", ft.reconcile_freqtrade, ZIP, [ZIP], tmp_path / "x")


def test_run_score_reports_scorer_errors(tmp_path: Path) -> None:
    def boom(spec: dict[str, Any]) -> Any:
        raise QtError("QT-INPUT-002", "NOT SCORED: 0 of 3 live orders could be scored")

    res, err, classes = ft.run_score(tmp_path / "l", tmp_path / "s", tmp_path / "o", window_ns=NS, scorer=boom)
    assert res is None and classes == {} and err == "QT-INPUT-002: NOT SCORED: 0 of 3 live orders could be scored"


# ---- candle-fill audit -----------------------------------------------------------------------------------------------


TS = pa.timestamp("ns", tz="UTC")
M = 60 * NS
P = 100 * 10**9  # price 100 at price_exp -9


def _audit_dir(
    tmp: Path, shadows: list[dict[str, Any]], trades: list[tuple[int, int]], fills: list[tuple[str, int, int]]
) -> Path:
    d = tmp / "replay"
    d.mkdir(exist_ok=True)
    cols: dict[str, list[Any]] = {k: [] for k in shadows[0]}
    for s in shadows:
        for k in cols:
            cols[k].append(s[k])
    tsc = ("ts_live", "ts_cancel_effective", "ts_first_fill", "ts_full_fill")
    pq.write_table(
        pa.table({k: pa.array(v, type=TS if k in tsc else None) for k, v in cols.items()}), d / "shadow_results.parquet"
    )
    pq.write_table(
        pa.table(
            {
                "ts_event": pa.array([t for t, _ in trades], type=TS),
                "instrument_id": pa.array([1] * len(trades), type=pa.uint32()),
                "price": pa.array([p for _, p in trades], type=pa.int64()),
            }
        ),
        d / "trades.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "shadow_id": [f[0] for f in fills],
                "ts_event": pa.array([f[1] for f in fills], type=TS),
                "fill_qty": pa.array([f[2] for f in fills], type=pa.uint64()),
            }
        ),
        d / "shadow_fills.parquet",
    )
    return d


def _sh(
    sid: str, live: int, side: int = 66, end: int | None = None, ff: int | None = None, **kw: Any
) -> dict[str, Any]:
    r = {
        "shadow_id": sid,
        "instrument_id": 1,
        "side": side,
        "price": P,
        "qty": 10,
        "ts_live": live,
        "ts_cancel_effective": end,
        "ts_first_fill": ff,
        "ts_full_fill": ff,
        "ambiguous_race": False,
        "out_of_session": False,
        "end_state": "filled" if ff else "cancelled",
        "first_fill_reason": "trade_through" if ff else "never",
    }
    r.update(kw)
    return r


def test_candle_fill_audit_cases(tmp_path: Path) -> None:
    t = T10
    # each order lives in its own hour so trades do not leak between cases
    h = 3600 * NS
    shadows = [
        _sh("touch", t, ff=None),  # low touches 100, FIFO does not fill
        _sh("through", t + h, ff=t + h + 2 * M),  # trades at 99: through, FIFO fills
        _sh("gap", t + 2 * h, ff=t + 2 * h + 6 * M),  # candle 1 above, candle 2 below: no candle contains 100
        _sh("late", t + 3 * h, end=t + 3 * h + 2 * M),  # cancelled at 2m, touch at 3m inside the 5m candle
        _sh("early", t + 4 * h + 2 * M),  # clock anchor: a print at 1m (before placement) touches
        _sh("sell", t + 5 * h, side=65, ff=t + 5 * h + M),  # sell: high 101 passes 100, FIFO fills
        _sh("race", t, ambiguous_race=True),
        _sh("pre", t, out_of_session=True),
        _sh("rej", t, end_state="rejected_would_cross"),
        _sh("dead", t + h, end=t + h),
        _sh("nopx", t, price=None),
    ]
    trades = [
        (t + M, 101 * 10**9),
        (t + 2 * M, P),  # touch
        (t + h + M, 101 * 10**9),
        (t + h + 2 * M, 99 * 10**9),  # through
        (t + 2 * h + M, 102 * 10**9),
        (t + 2 * h + 6 * M, 98 * 10**9),  # gap (5m candles)
        (t + 3 * h + M, 101 * 10**9),
        (t + 3 * h + 3 * M, P),  # late touch
        (t + 4 * h + M, P),
        (t + 4 * h + 3 * M, 101 * 10**9),  # early touch (before placement at 2m)
        (t + 5 * h + M, 101 * 10**9),
        (t + 5 * h + 2 * M, 99 * 10**9),  # sell through (range 99..101)
    ]
    d = _audit_dir(
        tmp_path,
        shadows,
        trades,
        [("through", t + h + 2 * M, 10), ("gap", t + 2 * h + 6 * M, 4), ("sell", t + 5 * h + M, 10)],
    )
    a = ft.candle_fill_audit(d, timeframe="5m", timeout=600, anchor="submit")
    assert a.n_input == 11 and a.n_excluded == {
        "ambiguous_race": 1,
        "out_of_session": 1,
        "rejected": 1,
        "not_live": 1,
        "no_price": 1,
    }
    al = a.all
    assert (al.n, al.candle_filled, al.exact_filled, al.both, al.candle_only, al.exact_only, al.neither) == (
        6,
        4,
        3,
        2,
        2,
        1,
        1,
    )
    assert (al.candle_only_queue, al.candle_only_outside_life, al.window_reached) == (1, 1, 3)
    # the gap order: prints passed the limit between candles, no single candle's range holds it
    assert al.exact_only_by_reason == {"trade_through": 1} and al.marketable_at_placement == 0
    assert al.overfill_share == pytest.approx(0.5) and al.overfill_ci95 is not None
    assert (a.touch.n, a.touch.candle_only) == (2, 2) and (a.through.n, a.through.both) == (2, 2)
    assert a.by_side["sell"].both == 1 and a.by_side["buy"].n == 5
    assert a.qty_candle == 40 and a.qty_exact == 24
    c = ft.candle_fill_audit(d, timeframe="5m", timeout=600, anchor="clock")
    assert c.all.candle_filled == 5 and c.all.candle_only_outside_life == 2  # "early" fills on the clock grid
    one = ft.candle_fill_audit(d, timeframe="1m", timeout=600)
    # 1m candles hold one print each here: a print through the limit is a candle whose range excludes the limit
    # (Freqtrade checks low <= price <= high per candle), so "through", "gap" and "sell" become exact-only
    assert (one.all.candle_filled, one.all.exact_only, one.all.candle_only_outside_life) == (1, 3, 0)


def test_candle_fill_audit_placement_clamp_lag_and_reasons(tmp_path: Path) -> None:
    t = T10
    h = 3600 * NS
    shadows = [
        # a buy priced above every print of its life: Freqtrade clamps it to the placing candle's high and fills it
        _sh("above", t, ff=t + M, first_fill_reason="trade_through"),
        # a sell below every print: clamped to the low, filled
        _sh("below", t + h, side=65, ff=t + h + M, first_fill_reason="trade_through"),
        # the quote crossed the limit, no print at or through it: FIFO fills, no candle range contains the limit
        _sh("crossed", t + 2 * h, ff=t + 2 * h + 30 * NS, first_fill_reason="crossed"),
        # a print touches the limit 20 s before the order: only a placement lag of 30 s brings it into the candle
        _sh("lagged", t + 3 * h),
    ]
    trades = [
        (t + M, 99 * 10**9),
        (t + 2 * M, 98 * 10**9),
        (t + h + M, 101 * 10**9),
        (t + h + 2 * M, 102 * 10**9),
        (t + 2 * h + M, 101 * 10**9),
        (t + 3 * h - 20 * NS, P),
        (t + 3 * h + M, 101 * 10**9),
    ]
    d = _audit_dir(tmp_path, shadows, trades, [])
    a = ft.candle_fill_audit(d, timeframe="5m", timeout=600)
    al = a.all
    assert (al.candle_filled, al.marketable_at_placement, al.both, al.exact_only) == (2, 2, 2, 1)
    assert al.exact_only_by_reason == {"crossed": 1} and a.through.marketable_at_placement == 2
    assert a.lag_ns == 0 and al.candle_only == 0
    lag = ft.candle_fill_audit(d, timeframe="5m", timeout=600, lag_ns=30 * NS)
    # the lagged candle also holds the 20 s pre-placement touch: a fill credited from outside the order's life
    assert (lag.all.candle_filled, lag.all.candle_only_outside_life, lag.lag_ns) == (3, 1, 30 * NS)
    _err("QT-CONFIG-001", ft.candle_fill_audit, d, lag_ns=300 * NS)
    _err("QT-CONFIG-001", ft.candle_fill_audit, d, lag_ns=-1)
    _err("QT-CONFIG-001", ft.candle_fill_audit, d, anchor="clock", lag_ns=NS)


def test_candle_fill_audit_cluster_interval() -> None:
    # 4 clusters of 10 candle fills, the 2 overfills in one: a draw holds that cluster 0 to 4 times
    c = np.ones(40, bool)
    e = np.array([True] * 18 + [False] * 2 + [True] * 20, bool)
    cl = np.array(["a"] * 10 + ["b"] * 10 + ["c"] * 10 + ["d"] * 10, dtype=object)
    z = np.zeros(40, bool)
    r = ft._counts(c, e, z, z, z, np.array(["x"] * 40, dtype=object), cl)
    assert r.overfill_share == pytest.approx(0.05) and r.n_clusters == 4
    assert r.overfill_ci95 is not None and r.overfill_ci95_cluster is not None
    lo, hi = r.overfill_ci95_cluster
    assert (lo, hi) == (0.0, pytest.approx(0.15))  # 0 and 3 copies of it in the 2.5% / 97.5% tails
    one = ft._counts(c[:10], e[:10], z[:10], z[:10], z[:10], np.array(["x"] * 10, dtype=object), cl[:10])
    assert one.overfill_ci95_cluster is None and one.n_clusters == 1


def test_candle_fill_audit_errors(tmp_path: Path) -> None:
    _err("QT-INPUT-001", ft.candle_fill_audit, tmp_path)
    d = _audit_dir(tmp_path, [_sh("a", T10)], [(T10 + M, P)], [])
    _err("QT-CONFIG-001", ft.candle_fill_audit, d, timeout=0)
    _err("QT-CONFIG-001", ft.candle_fill_audit, d, anchor="noon")
    _err("QT-CONFIG-001", ft.candle_fill_audit, d, timeframe="1M")
    (d / "shadow_fills.parquet").unlink()
    assert ft.candle_fill_audit(d).qty_exact is None


@pytest.mark.requires_core
def test_candle_fill_audit_on_an_exact_replay(repo_root: Path, tmp_path: Path) -> None:
    import queuetruth as qt

    synth = repo_root / "tests" / "fixtures" / "synth_small"
    sh = tmp_path / "s.parquet"
    qt.sample_shadows({"dataset": {"path": str(synth)}, "out": str(sh), "n": 300, "seed": 7, "lifetime_ns": 600 * NS})
    qt.replay({"dataset": {"path": str(synth)}, "out": str(tmp_path / "r"), "shadows": {"path": str(sh)}})
    a = ft.candle_fill_audit(tmp_path / "r", timeframe="5m", timeout=600)
    al = a.all
    assert al.n + sum(a.n_excluded.values()) == a.n_input == 300
    assert al.both + al.candle_only + al.exact_only + al.neither == al.n
    assert a.touch.n + a.through.n == al.candle_filled
    assert al.candle_only == al.candle_only_queue + al.candle_only_outside_life
    assert a.qty_exact is not None and a.qty_exact > 0 and a.qty_candle is not None


def test_dry_run_database_as_simulator_arm(tmp_path: Path) -> None:
    # the same run on both sides pairs every order with itself: the simulator log reuses every live key
    db = str(_db(tmp_path))
    live = ft.read_bot_run([db], {})
    sim = ft.read_bot_run([db], {})
    p = ft.pair_runs(live, sim, tolerance=0)
    assert all(lo is not None and so is not None and lo.ref == so.ref for lo, so in p.pairs) and len(p.pairs) == 12
    with pytest.warns(qlive.LiveImportWarning):
        s = qlive.import_sim("freqtrade", [db], tmp_path / "sim", pair_with=db)
    assert s.venue == "sim:freqtrade" and s.n_orders == 12
    keys = {o["order_key"] for o in table_rows(read_log(tmp_path / "sim")[0])}
    assert keys == {ft.make_order_key("binance", "main", o.ref) for o in live.orders()}


def test_pair_runs_scales_linearly() -> None:
    # 25,000 trades per arm (50,000 orders): the bucketed pairing takes about a second; the old all-pairs scan would
    # compare 6.25e8 trade pairs
    import time

    def run(kind: str, shift: int) -> ft.FtRun:
        r = ft.FtRun(kind=kind, venue="binance", timeframe_s=300, trades=[], paths=[])
        for i in range(25_000):
            pair = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT")[i % 4]
            t0 = T10 + i * 60 * NS + shift
            tr = ft.FtTrade(
                ref=f"{kind}{i}",
                pair=pair,
                is_short=False,
                open_ns=t0,
                is_open=False,
                enter_tag=None,
                exit_reason=None,
                source="backtest" if kind == "backtest" else "db",
            )
            for role, side, dt_ in (("entry", "buy", 0), ("exit", "sell", 600 * NS)):
                rec = ft.Record({}, "", "x", 0, i)
                tr.orders.append(
                    ft.FtOrder(
                        ref=f"{tr.ref}-{role}",
                        trade=tr,
                        role=role,
                        side=side,
                        order_type="limit",
                        qty=Decimal(1),
                        filled=Decimal(1),
                        limit_px=Decimal(100),
                        avg_px=Decimal(100),
                        placed_ns=t0 + dt_,
                        filled_ns=t0 + dt_,
                        end_ns=None,
                        status="closed",
                        cancel_reason=None,
                        tag=None,
                        rec=rec,
                    )
                )
            r.trades.append(tr)
        return r

    live, bt = run("live", 2 * NS), run("backtest", 0)
    t = time.perf_counter()
    p = ft.pair_runs(live, bt)
    elapsed = time.perf_counter() - t
    assert len(p.trade_pairs) == 25_000 and sum(1 for lo, so in p.pairs if lo and so) == 50_000
    assert elapsed < 20, elapsed
