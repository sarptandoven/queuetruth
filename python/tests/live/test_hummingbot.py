"""Hummingbot importers, pairing and reconciliation (`queuetruth.live.importers.hummingbot`) on the fixtures of
tests/fixtures/live/hummingbot (written from Hummingbot's source at commit 9af100d6822d) and on malformed inputs."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.live import read_log, table_rows
from queuetruth.live.importers import BOT_IMPORTERS, SIM_IMPORTERS, ImportOptions
from queuetruth.live.importers import freqtrade as ft
from queuetruth.live.importers import hummingbot as hb

FIX = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "live" / "hummingbot"
CSV = FIX / "trades_conf_pure_mm_1.csv"
NS = 1_000_000_000
MS = 1_000_000
T10 = 1_709_287_200 * NS


def _db(tmp: Path, name: str = "conf_pure_mm_1") -> Path:
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


def _events(out: Path) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for e in table_rows(read_log(out)[1]):
        by.setdefault(e["order_key"], []).append(e)
    return by


def test_fixture_generator_is_current(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("hb_make_fixtures", FIX / "make_fixtures.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = tmp_path
    mod.main()
    for name in ("conf_pure_mm_1.sql", "conf_pure_mm_1_paper.sql", "trades_conf_pure_mm_1.csv"):
        assert (tmp_path / name).read_text() == (FIX / name).read_text(), name


def test_registered_in_importer_registries() -> None:
    assert SIM_IMPORTERS["hummingbot"] is hb.import_hummingbot_sim
    assert BOT_IMPORTERS["hummingbot"] is hb.import_hummingbot


def test_read_live_database(tmp_path: Path) -> None:
    run = hb.read_hb_run([str(_db(tmp_path))])
    assert (run.market, run.paper, run.venue) == ("binance", False, "binance")
    assert len(run.orders) == 9
    by = {o.exchange_order_id: o for o in run.orders}
    o = by["28457312045"]
    assert (o.side, o.px, o.qty, o.order_type) == ("buy", Decimal("62000.5"), Decimal("0.001"), "LIMIT")
    assert [s for _, s, _ in o.statuses] == ["BuyOrderCreated", "OrderCancelled"]
    two = by["28457312091"]
    assert [(f.qty, f.trade_id) for f in two.fills] == [(Decimal("0.0006"), "3051042"), (Decimal("0.0004"), "3051051")]
    assert two.filled == Decimal("0.001") and two.fills[0].fee == Decimal("0.0372") and two.fills[0].fee_ccy == "USDT"
    mkt = by["28457312170"]
    assert mkt.px is None and mkt.order_type == "MARKET" and mkt.side == "sell"


def test_paper_detection(tmp_path: Path) -> None:
    run = hb.read_hb_run([str(_db(tmp_path, "conf_pure_mm_1_paper"))])
    assert (run.market, run.paper, run.venue) == ("binance_PaperTrade", True, "binance")
    assert hb.is_paper("kucoin_paper_trade") and hb.is_paper("Binance_PaperTrade") and not hb.is_paper("binance")


def test_csv_matches_database_fills(tmp_path: Path) -> None:
    db = hb.read_hb_run([str(_db(tmp_path))])
    csv = hb.read_hb_run([str(CSV)])
    key = lambda r: sorted((o.cid, f.trade_id, f.qty, f.px, f.ts_ns, f.fee) for o in r.orders for f in o.fills)  # noqa: E731
    assert key(db) == key(csv)
    assert all(not o.record and o.created_ns is None for o in csv.orders)
    # both sources together: the CSV fills deduplicate onto the database orders
    both = hb.read_hb_run([str(_db(tmp_path)), str(CSV)])
    assert key(both) == key(db) and len(both.orders) == 9
    # the CSV copy of a fill wins, whatever the order of the inputs: the database truncates to 6 decimals
    text = CSV.read_text()
    assert ",61990.0,0.001," in text
    fine = tmp_path / "fine.csv"
    fine.write_text(text.replace(",61990.0,0.001,", ",61990.0000004,0.001,", 1))
    for paths in ([str(_db(tmp_path)), str(fine)], [str(fine), str(_db(tmp_path))]):
        r = hb.read_hb_run(paths)
        f = next(f for o in r.orders for f in o.fills if f.trade_id == "3051001")
        assert f.px == Decimal("61990.0000004") and f.from_csv


def test_import_live_log(tmp_path: Path) -> None:
    with pytest.warns(qlive.LiveImportWarning):
        s = hb.import_hummingbot_live(str(_db(tmp_path)), tmp_path / "live", instrument_map=None, venue="binance")
    assert s.n_orders == 9 and s.n_fills == 6 and s.validation_status == "fixture_only"
    assert any("*OrderCompleted status" in w for w in s.warnings)
    ev = _events(tmp_path / "live")
    k = "binance:main:28457312090"
    assert [e["event_type"] for e in ev[k]] == ["submit_sent", "fill", "cancel_ack"]
    sub, fill, cxl = ev[k]
    assert sub["ts_local_send"] == T10 + 60_115 * MS and sub["ts_exchange"] is None and sub["ts_precision"] == "ms"
    assert fill["ts_local_recv"] == T10 + 78_300 * MS and fill["qty"] == 40_000 and fill["fill_id"] == "3051088"
    assert fill["fee"] == pytest.approx(0.024792) and fill["fee_ccy"] == "USDT" and fill["liquidity"] == "unknown"
    assert cxl["ts_local_recv"] == T10 + 90_110 * MS and cxl["reason"] == "OrderCancelled"
    orders = {o["order_key"]: o for o in table_rows(read_log(tmp_path / "live")[0])}
    assert orders[k]["client_order_id"] == "buy-BTC-USDT-1709287260115005" and orders[k]["tif"] == "gtc"
    mkt = orders["binance:main:28457312170"]
    assert (mkt["order_type"], mkt["tif"], mkt["limit_px"]) == ("market", "other", None)
    assert ev["binance:main:28457312170"][-1]["liquidity"] == "taker"


def test_import_csv_is_fills_only(tmp_path: Path) -> None:
    with pytest.warns(qlive.LiveImportWarning):
        s = hb.import_hummingbot_live(str(CSV), tmp_path / "live")
    assert s.coverage is not None and s.coverage.fills_only and s.n_orders == 5 and s.n_fills == 6
    assert any("coverage=fills_only" in w for w in s.warnings)
    orders = table_rows(read_log(tmp_path / "live")[0])
    assert {o["order_key"] for o in orders} == {
        "binance:main:buy-BTC-USDT-1709287200120001",
        "binance:main:buy-BTC-USDT-1709287260115005",
        "binance:main:sell-BTC-USDT-1709287260116006",
        "binance:main:sell-BTC-USDT-1709287290121008",
        "binance:main:sell-BTC-USDT-1709287325000009",
    }


def test_paper_database_as_live_arm_is_simulated(tmp_path: Path) -> None:
    with pytest.warns(qlive.LiveImportWarning):
        s = hb.import_hummingbot_live(str(_db(tmp_path, "conf_pure_mm_1_paper")), tmp_path / "live")
    assert s.validation_status == "simulated" and s.venue == "binance"
    assert any("paper trade market binance_PaperTrade" in w for w in s.warnings)
    ev = _events(tmp_path / "live")
    rej = ev["binance:main:sell-BTC-USDT-1709287290301008"]
    assert [e["event_type"] for e in rej] == ["submit_sent", "reject"]


def test_fee_recomputed_when_stored_as_zero(tmp_path: Path) -> None:
    # MarketsRecorder stores trade_fee_in_quote = 0 when its fee conversion fails
    text = CSV.read_text()
    old = "'flat_fees': []}\",0.0619900,"
    assert old in text
    p = tmp_path / "zero.csv"
    p.write_text(text.replace(old, "'flat_fees': []}\",0,", 1))
    r = hb.read_hb_run([str(p)])
    f = next(f for o in r.orders for f in o.fills if f.trade_id == "3051001")
    assert (f.fee, f.fee_ccy) == (Decimal("0.001") * Decimal("61990.0") * Decimal("0.001"), "USDT")
    assert any("1 fill(s) with trade_fee_in_quote = 0" in w for w in r.warnings)
    # a percent fee charged in a third token cannot be converted: the stored 0 stays; a single flat fee is used
    q = tmp_path / "bnb.csv"
    q.write_text(
        text.replace(old, "'flat_fees': []}\",0,", 1).replace("'percent_token': 'USDT'", "'percent_token': 'BNB'", 1)
    )
    f2 = next(f for o in hb.read_hb_run([str(q)]).orders for f in o.fills if f.trade_id == "3051001")
    assert f2.fee == 0
    flat = "'flat_fees': [{'token': 'BNB', 'amount': '0.00011'}]}\",0,"
    w = tmp_path / "flat.csv"
    w.write_text(text.replace(old, flat, 1).replace("'percent': '0.001'", "'percent': '0'", 1))
    f3 = next(f for o in hb.read_hb_run([str(w)]).orders for f in o.fills if f.trade_id == "3051001")
    assert (f3.fee, f3.fee_ccy) == (Decimal("0.00011"), "BNB")


def test_real_columns_and_statuses(tmp_path: Path) -> None:
    # databases written before SqliteDecimal stored REAL values in real units
    sql = """
CREATE TABLE "Order" (id TEXT, config_file_path TEXT, strategy TEXT, market TEXT, symbol TEXT, base_asset TEXT,
    quote_asset TEXT, creation_timestamp BIGINT, order_type TEXT, amount REAL, leverage INTEGER, price REAL,
    last_status TEXT, last_update_timestamp BIGINT, exchange_order_id TEXT, position TEXT);
CREATE TABLE "OrderStatus" (id INTEGER, order_id TEXT, timestamp BIGINT, status TEXT);
INSERT INTO "Order" VALUES ('o1','c','s','binance','BTC-USDT','BTC','USDT',1709287200000,'LIMIT_MAKER',0.001,1,
    62000.5,'OrderExpired',1709287230000,'77',NULL);
INSERT INTO "Order" VALUES ('o2','c','s','binance','BTC-USDT','BTC','USDT',1709287200001,'LIMIT',0.002,1,61000,
    'SomethingNew',1709287230000,NULL,'CLOSE');
INSERT INTO "OrderStatus" VALUES (1,'o1',1709287200000,'BuyOrderCreated');
INSERT INTO "OrderStatus" VALUES (2,'o1',1709287230000,'OrderExpired');
INSERT INTO "OrderStatus" VALUES (3,'o2',1709287200001,'SellOrderCreated');
INSERT INTO "OrderStatus" VALUES (4,'o2',1709287230000,'SomethingNew');
"""
    db = _sql_db(tmp_path, "real.sqlite", sql)
    run = hb.read_hb_run([str(db)])
    o1 = next(x for x in run.orders if x.cid == "o1")
    assert o1.px == Decimal("62000.5") and o1.qty == Decimal("0.001") and o1.side == "buy"
    res = hb.import_hummingbot([str(db)], ImportOptions())
    by: dict[str, list[str]] = {}
    for e in res.events:
        by.setdefault(e["order_key"], []).append(e["event_type"])
    assert by == {"binance:main:77": ["submit_sent", "expire"], "binance:main:o2": ["submit_sent"]}
    assert any("1 record(s) without a canonical event: status SomethingNew" in w for w in res.warnings)
    orders = {o["order_key"]: o for o in res.orders}
    assert orders["binance:main:77"]["post_only"] is True and orders["binance:main:77"]["tif"] == "post_only"
    assert orders["binance:main:o2"]["reduce_only"] is True and orders["binance:main:o2"]["side"] == "sell"


def test_malformed_inputs(tmp_path: Path) -> None:
    other = _sql_db(tmp_path, "other.sqlite", "CREATE TABLE Foo (x INTEGER);")
    assert "not a Hummingbot database" in str(_err("QT-INPUT-002", hb.read_hb_run, [str(other)]))
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    _err("QT-INPUT-002", hb.read_hb_run, [str(junk)])
    _err("QT-INPUT-001", hb.read_hb_run, [str(tmp_path / "none.sqlite")])
    n = [0]

    def csv_with(old: str, new: str) -> Path:
        text = CSV.read_text()
        assert old in text
        n[0] += 1
        p = tmp_path / f"t{n[0]}.csv"
        p.write_text(text.replace(old, new, 1))
        return p

    _err("QT-INPUT-002", hb.read_hb_run, [str(csv_with(",BUY,LIMIT,61990.0,", ",HOLD,LIMIT,61990.0,"))])
    _err("QT-INPUT-002", hb.read_hb_run, [str(csv_with(",BUY,LIMIT,61990.0,", ",BUY,LIMIT,-1,"))])
    _err("QT-INPUT-002", hb.read_hb_run, [str(csv_with("\"{'fee_type'", "\"{'fee_type' oops"))])
    _err("QT-INPUT-002", hb.read_hb_run, [str(csv_with("1709287212402,", "yesterday,"))])
    _err("QT-INPUT-002", hb.read_hb_run, [str(csv_with("3051001,", ","))])
    nocol = tmp_path / "nocol.csv"
    nocol.write_text("a,b\n1,2\n")
    assert "missing column" in str(_err("QT-INPUT-002", hb.read_hb_run, [str(nocol)]))
    # a fill whose side contradicts its order's created status, and two fills of one order with different sides
    sql = (FIX / "conf_pure_mm_1.sql").read_text()
    old = "'buy-BTC-USDT-1709287200120001','BUY'"
    db = _sql_db(tmp_path, "side.sqlite", sql.replace(old, "'buy-BTC-USDT-1709287200120001','SELL'"))
    assert "with sell fills" in str(_err("QT-INPUT-002", hb.read_hb_run, [str(db)]))
    two = sql.replace(
        "'sell-BTC-USDT-1709287260116006','SELL','LIMIT',62000000000,400",
        "'sell-BTC-USDT-1709287260116006','BUY','LIMIT',62000000000,400",
    )
    assert two != sql
    assert "contradicts" in str(_err("QT-INPUT-002", hb.read_hb_run, [str(_sql_db(tmp_path, "two.sqlite", two))]))


def test_market_selection_and_options(tmp_path: Path) -> None:
    sql = (FIX / "conf_pure_mm_1.sql").read_text()
    sql += (
        "INSERT INTO \"Order\" VALUES ('x1','c','s','kucoin','BTC-USDT','BTC','USDT',1709287200500,"
        "'LIMIT',1000,1,61000000000,'BuyOrderCreated',1709287200500,NULL,'NIL');\n"
        "INSERT INTO \"OrderStatus\" VALUES (500,'x1',1709287200500,'BuyOrderCreated');\n"
    )
    db = _sql_db(tmp_path, "two.sqlite", sql)
    assert "markets" in str(_err("QT-CONFIG-001", hb.read_hb_run, [str(db)]))
    k = hb.read_hb_run([str(db)], market="kucoin")
    assert [o.cid for o in k.orders] == ["x1"] and any("9 order(s) on other markets" in w for w in k.warnings)
    _err("QT-CONFIG-001", hb.import_hummingbot, [str(_db(tmp_path))], ImportOptions(extra={"bogus": 1}))
    _err("QT-INPUT-001", hb.import_hummingbot_live, str(tmp_path / "none.sqlite"), tmp_path / "o")


def test_pair_orders(tmp_path: Path) -> None:
    live = hb.read_hb_run([str(_db(tmp_path))])
    paper = hb.read_hb_run([str(_db(tmp_path, "conf_pure_mm_1_paper"))])
    pairs = hb.pair_orders(live, paper)
    got = {lo.exchange_order_id: so.cid for lo, so in pairs if lo is not None and so is not None}
    assert len(got) == 8 and got["28457312001"] == "buy-BTC-USDT-1709287200118001"
    assert [lo.exchange_order_id for lo, so in pairs if so is None] == ["28457312170"]
    assert [so.cid for lo, so in pairs if lo is None] == ["buy-BTC-USDT-1709287320300009"]
    # a 100 ms window leaves the orders created 180 ms apart (cycle 4) unpaired
    narrow = hb.pair_orders(live, paper, window_ms=100)
    assert sum(1 for lo, so in narrow if lo is not None and so is not None) == 6
    assert sum(1 for lo, so in hb.pair_orders(live, paper, window_ms=0) if lo and so) == 0
    _err("QT-CONFIG-001", hb.pair_orders, live, paper, window_ms=-1)
    _err("QT-CONFIG-001", hb.pair_orders, live, paper, price_tol="-0.5")


def test_import_sim_pairs_paper_with_live(tmp_path: Path) -> None:
    db, paper = _db(tmp_path), _db(tmp_path, "conf_pure_mm_1_paper")
    with pytest.warns(qlive.LiveImportWarning):
        s = qlive.import_sim("hummingbot", [str(paper)], tmp_path / "sim", pair_with=str(db))
    assert s.venue == "sim:hummingbot" and s.validation_status == "simulated"
    assert any("8 of 9 simulator order(s) paired" in w for w in s.warnings)
    assert any("pair_with: keys taken from" in w and "as venue binance, account main" in w for w in s.warnings)
    # `market` names the live market in both entry points: the paper arm is <market>_PaperTrade
    with pytest.warns(qlive.LiveImportWarning):
        m = qlive.import_sim("hummingbot", [str(paper)], tmp_path / "sim3", pair_with=str(db), market="binance")
    assert m.n_orders == 9 and any("8 of 9 simulator order(s) paired" in w for w in m.warnings)
    # a trades CSV has no creation times: pairing refuses it and names the database
    e = _err("QT-CONFIG-001", qlive.import_sim, "hummingbot", [str(paper)], tmp_path / "sim4", pair_with=str(CSV))
    assert "data/<config>.sqlite" in str(e)
    keys = {o["order_key"] for o in table_rows(read_log(tmp_path / "sim")[0])}
    assert "binance:main:28457312001" in keys and "sim:hummingbot:main:buy-BTC-USDT-1709287320300009" in keys
    with pytest.warns(qlive.LiveImportWarning):
        s2 = qlive.import_sim("hummingbot", [str(paper)], tmp_path / "sim2")
    assert any("no pair_with" in w for w in s2.warnings)


@pytest.mark.requires_core
def test_reconcile_hummingbot_end_to_end(tmp_path: Path) -> None:
    db, paper = _db(tmp_path), _db(tmp_path, "conf_pure_mm_1_paper")
    rep = hb.reconcile_hummingbot(paper, db, tmp_path / "rec", window_ns=30 * NS)
    got = {(r.live_key or r.sim_key): r.category for r in rep.orders}
    assert got == {
        "binance:main:28457312001": "timing",
        "binance:main:28457312002": "missed_fill",
        "binance:main:28457312045": "missed_fill",
        "binance:main:28457312046": "both_unfilled",
        "binance:main:28457312090": "partial_fill",
        "binance:main:28457312091": "timing",
        "binance:main:28457312133": "both_unfilled",
        "binance:main:28457312134": "extra_live_fill",
        "binance:main:28457312170": "no_sim_twin",
        "sim:hummingbot:main:buy-BTC-USDT-1709287320300009": "no_live_twin",
    }
    assert (rep.n_live_orders, rep.n_sim_orders, rep.n_paired) == (9, 9, 8)
    assert rep.pairing_coverage == pytest.approx(8 / 9)
    sc = rep.score
    assert sc is not None and sc.two_by_two == {"both": 3, "neither": 2, "live_only": 1, "sim_only": 2}
    assert (sc.n_matched, sc.n_unmatched_live, sc.n_unmatched_sim) == (8, 0, 1)
    # 8 orders in one 30-minute cluster: the report does not present the p-value as a finding
    assert sc.n_clusters == 1 and sc.interpretable is False
    assert ft.render_reconcile(rep).splitlines()[1].startswith("score (not interpretable: 8 matched orders in 1")
    assert rep.missed_fill_notional == {"BTC-USDT": "124.0105"}
    vwap = next(r for r in rep.orders if r.live_key == "binance:main:28457312091")
    assert vwap.live_avg_px == "62000" and vwap.fill_delay_ns == 2_300 * MS and vwap.score_class == "both"
    assert "2.3s after the paper" in vwap.explanation
    doc = json.loads((tmp_path / "rec" / "reconcile.json").read_text())
    assert ft.ReconcileReport.model_validate(doc) == rep and doc["kind"] == "hummingbot"
    rep2 = hb.reconcile_hummingbot(paper, db, tmp_path / "x" / "rec2", window_ns=30 * NS)
    assert rep2.run_id == rep.run_id
    assert (tmp_path / "rec" / "reconcile.json").read_bytes() == (
        tmp_path / "x" / "rec2" / "reconcile.json"
    ).read_bytes()
    # the database with its trades CSV as the live arm: the CSV fills dedupe onto the database orders
    both = hb.reconcile_hummingbot(paper, [db, CSV], tmp_path / "both", window_ns=30 * NS)
    assert both.counts == rep.counts and both.run_id != rep.run_id


def test_reconcile_options(tmp_path: Path) -> None:
    db, paper = _db(tmp_path), _db(tmp_path, "conf_pure_mm_1_paper")
    rep = hb.reconcile_hummingbot(paper, db, tmp_path / "rec", score=False, market="binance", pair_window_ms=100)
    assert rep.score is None and rep.n_paired == 6
    _err("QT-CONFIG-001", hb.reconcile_hummingbot, paper, db, tmp_path / "x", pair_with="y")
    _err("QT-CONFIG-001", hb.reconcile_hummingbot, paper, db, tmp_path / "x", pair_window_ms="1s")
    _err("QT-INPUT-001", hb.reconcile_hummingbot, paper, tmp_path / "none.sqlite", tmp_path / "x")
    # swapped arguments (the live arm is the paper bot) fail unless asked for
    e = _err("QT-CONFIG-001", hb.reconcile_hummingbot, db, paper, tmp_path / "sw", score=False)
    assert "arms swapped?" in str(e)
    swapped = hb.reconcile_hummingbot(db, paper, tmp_path / "sw", score=False, arms_as_given=True)
    assert any(w.startswith("arms_as_given") for w in swapped.warnings)
    # a trades CSV alone (fills only) cannot be paired: either arm
    e = _err("QT-CONFIG-001", hb.reconcile_hummingbot, paper, CSV, tmp_path / "csv", score=False)
    assert "only the bot database" in str(e)
    # a pairing window too tight to pair most orders is reported
    lone = hb.reconcile_hummingbot(paper, db, tmp_path / "w0", score=False, pair_window_ms=0)
    assert lone.pairing_coverage == 0 and any(w.startswith("pairing: only 0 of 9") for w in lone.warnings)


def test_reconcile_run_id_and_tick_warning(tmp_path: Path) -> None:
    db, paper = _db(tmp_path), _db(tmp_path, "conf_pure_mm_1_paper")
    base = hb.reconcile_hummingbot(paper, db, tmp_path / "a", score=False)
    imap = tmp_path / "imap.csv"
    imap.write_text(
        "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n"
        "binance,BTC-USDT,1,,-9,-8,0.0000001,0.00001\n"
    )
    mapped = hb.reconcile_hummingbot(paper, db, tmp_path / "b", score=False, instrument_map=imap)
    assert len({base.run_id, mapped.run_id, hb.reconcile_hummingbot(paper, db, tmp_path / "c").run_id}) == 3
    # a tick of 1e-7 is below what Hummingbot's database keeps (6 decimals)
    assert any("tick below 1e-6 for ['BTC-USDT']" in w for w in mapped.warnings)


def test_pair_orders_scales_linearly() -> None:
    # 50,000 orders per arm (a month of a 1-level pure_market_making bot refreshing every 30 s, both sides)
    import time

    def run(market: str, shift_ms: int) -> hb.HbRun:
        orders = []
        for i in range(50_000):
            side = "buy" if i % 2 == 0 else "sell"
            t0 = T10 + (i // 2) * 30 * NS + shift_ms * MS
            rec = hb.Record({}, "", "x", 0, i)
            orders.append(
                hb.HbOrder(
                    cid=f"{market}{i}",
                    market=market,
                    symbol="BTC-USDT",
                    side=side,
                    order_type="LIMIT",
                    qty=Decimal("0.001"),
                    px=Decimal(62000 + (i % 7)),
                    created_ns=t0,
                    exchange_order_id=None,
                    position=None,
                    rec=rec,
                    record=True,
                )
            )
        return hb.HbRun(market=market, paper=market.endswith("PaperTrade"), orders=orders, paths=[])

    live, paper = run("binance", 0), run("binance_PaperTrade", 2)
    t = time.perf_counter()
    pairs = hb.pair_orders(live, paper)
    elapsed = time.perf_counter() - t
    assert sum(1 for lo, so in pairs if lo is not None and so is not None) == 50_000
    assert elapsed < 20, elapsed
