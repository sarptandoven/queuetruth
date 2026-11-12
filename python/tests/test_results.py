"""Every verb's result reads the same way (plan section 4.2; `queuetruth.results.Result`): `.summary`, `.table()`
(a pandas DataFrame in human units), `.to_html()`, `_repr_html_()`, `.doc` and `.save(path)`. Each verb runs on
synthetic fixtures: `tests/fixtures/synth_small` (check, fit), a synthetic bar backtest and atlas (check without
data, atlas lookup), synthetic ITCH and IEX days (data add, data fetch iex over a local server, demo)."""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import importlib.util
import inspect
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import queuetruth as qt
from queuetruth.errors import QtError
from queuetruth.results import Result

HERE = Path(__file__).resolve().parent


def _load(rel: str) -> Any:
    """A helper module of another test directory, by its path (the directory names are not unique packages)."""
    spec = importlib.util.spec_from_file_location(f"qt_results_{Path(rel).stem}", HERE / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up there
    spec.loader.exec_module(mod)
    return mod


F = _load("demo/demo_fixtures.py")
sb = _load("check/synth_backtests.py")
_write_api = _load("atlas/test_atlas_lookup.py")._write_api

pytestmark = pytest.mark.requires_core

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "fixtures" / "synth_small"
ATLAS = REPO / "tests" / "fixtures" / "atlas_synth"
SYMS = ["AAPL", "MSFT"]
IEX_DAY = dt.date(2026, 10, 9)


def assert_shape(res: Any, tmp: Path, *, columns: set[str], first_line: str | None = None) -> pd.DataFrame:
    """The shared shape, checked the same way for every verb."""
    assert isinstance(res, Result)
    assert isinstance(res.summary, str) and res.summary.strip()
    if first_line is not None:
        assert first_line.startswith(res.summary.splitlines()[0])
    doc = res.doc
    assert isinstance(doc, dict) and str(doc["schema"]).startswith("qt.") and res.to_dict() is doc
    json.dumps(doc, allow_nan=False)
    df = res.table()
    assert isinstance(df, pd.DataFrame) and len(df) > 0 and columns <= set(df.columns), list(df.columns)
    assert not [c for c in df.columns if str(c).endswith("_ns")], "human units: no nanosecond columns"
    page = res.to_html()
    assert page.startswith("<!doctype html>") and "</html>" in page
    view = res._repr_html_()
    assert view.startswith('<div class="qt-result">') and "<table>" in view and len(view) < len(page)
    assert res.summary.splitlines()[0][:40].replace("&", "&amp;").replace("'", "&#x27;") in view
    if res.interval is not None:
        assert "95%" in res.interval
    j = res.save(tmp / "result.json")
    assert json.loads(j.read_text(encoding="utf-8")) == json.loads(json.dumps(doc))
    assert res.save(tmp / "page.html").read_text(encoding="utf-8") == page
    csv = pd.read_csv(res.save(tmp / "table.csv"))
    assert list(csv.columns) == list(df.columns) and len(csv) == len(df)
    return df


# ---- check -------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backtest(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return sb.write_nautilus(tmp_path_factory.mktemp("bt") / "orders_report.csv", sb.orders(DATA))


def test_check_on_order_level_data(backtest: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    res = qt.check(backtest, data=DATA, horizon="1s", window="10s", out=tmp_path / "out")
    assert res.doc["pnl"]["markout"] == "1s" and res.doc["window"] == "10s"
    df = assert_shape(res, tmp_path, columns={"symbol", "price", "entry_local", "outcome"})
    assert df["symbol"].str.startswith("QT").all()
    assert res.interval is not None and res.interval.startswith("Backtest minus real fills")
    # the CLI prints the same answer first
    from queuetruth import cli

    assert cli.main(["check", str(backtest), "--data", str(DATA), "--window", "10s", "--out", str(tmp_path / "c")]) in (
        0,
        1,
    )
    assert (
        capsys.readouterr().out.splitlines()[0]
        == qt.check(backtest, data=DATA, window="10s", out=tmp_path / "again").summary.splitlines()[0]
    )


def test_check_durations_in_every_form(backtest: Path, tmp_path: Path) -> None:
    a = qt.check(backtest, data=DATA, horizon="1min", window="10s", out=tmp_path / "a")
    b = qt.check(backtest, data=DATA, horizon=60_000_000_000, window="10 s", out=tmp_path / "b")
    assert a.doc["pnl"]["markout_ns"] == b.doc["pnl"]["markout_ns"] == 60_000_000_000
    with pytest.warns(DeprecationWarning, match="now `horizon`"):
        c = qt.check(backtest, data=DATA, markout="1min", window="10s", out=tmp_path / "c")
    assert c.doc["pnl"]["markout_ns"] == 60_000_000_000
    for bad in ("5", "soon", -1, True):
        with pytest.raises(QtError) as e:
            qt.check(backtest, data=DATA, horizon=bad, out=tmp_path / "x")
        assert e.value.code == "QT-USAGE-001"


def test_check_without_data_from_the_atlas(tmp_path: Path) -> None:
    orders = sb.orders(DATA)
    bt = sb.write_hftbacktest(tmp_path / "hbt.jsonl", orders)
    res = qt.check(bt, bar="5m", tier="ALL", atlas=ATLAS, out=tmp_path / "o")
    assert res.doc["mode"] == "atlas"
    assert_shape(res, tmp_path, columns=set())
    assert res.interval is not None and "95% interval" in res.interval


# ---- fit ---------------------------------------------------------------------------------------------------------


def test_fit(tmp_path: Path) -> None:
    res = qt.fit(backtester="nautilus", data=DATA, split="70%", out=tmp_path / "fit")
    df = assert_shape(res, tmp_path, columns={"horizon", "fitted_bias_pp", "default_bias_pp", "result"})
    assert set(df["horizon"]) <= {"100ms", "1s", "10s", "1min", "5min"} and "1s" in set(df["horizon"])
    assert res.interval is not None and res.interval.startswith("p = ")
    assert (
        res.passed
        and res.snippet is not None
        and res.snippet in res.to_html().replace("&#x27;", "'").replace("&quot;", '"')
    )


# ---- atlas -------------------------------------------------------------------------------------------------------


def test_atlas_lookup(tmp_path: Path) -> None:
    site = _write_api(tmp_path / "site")
    res = qt.atlas.lookup(library="nautilus", tier="ALL", horizon="1min", source=site)
    df = assert_shape(res, tmp_path, columns={"library", "setting", "horizon", "bias_pp", "ci_lo_pp", "ci_hi_pp"})
    assert set(df["horizon"]) == {"1min"}
    assert res.interval is not None and "pp" in res.interval
    assert qt.atlas.lookup(library="nautilus", horizon=60_000_000_000, source=site).doc["horizon"] == "1min"


@pytest.mark.parametrize(("library", "tier"), [("nautilus", "T1"), ("hftbacktest", "ALL")])
def test_atlas_calibration(library: str, tier: str, tmp_path: Path) -> None:
    res = qt.atlas.calibration(library, tier=tier, source=ATLAS)
    df = assert_shape(res, tmp_path, columns={"setting", "tier", "horizon", "bias_pp", "ci_lo_pp", "ci_hi_pp"})
    assert len(df) == 1 and res.doc["source"] == "atlas"
    assert res.interval is not None and "in the atlas" in res.interval
    assert res.snippet is not None and res.settings


# ---- data --------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def itch_gz(tmp_path_factory: pytest.TempPathFactory) -> Path:
    events = F.book_events(SYMS, F.hms(9, 30), F.hms(9, 42))
    p = tmp_path_factory.mktemp("itch") / "12302019.NASDAQ_ITCH50.gz"
    F.write_gz(p, F.itch_day(SYMS, events), members=3)
    return p


def test_data_add(itch_gz: Path, tmp_path: Path) -> None:
    res = qt.data.add([itch_gz], tmp_path / "ds")
    df = assert_shape(res, tmp_path, columns={"date", "symbol"})
    assert set(df["symbol"]) == set(SYMS)


@pytest.fixture
def bucket(tmp_path: Path) -> Iterator[str]:
    ev = F.book_events(SYMS, F.hms(9, 30), F.hms(9, 34))
    root = tmp_path / "bucket"
    p = root / "20261009" / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    p.parent.mkdir(parents=True)
    F.write_gz(p, F.iex_day(IEX_DAY.isoformat(), SYMS, ev, extra_symbols=()))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_data_fetch_iex(bucket: str, tmp_path: Path) -> None:
    from queuetruth.data.iex import iex_url

    res = qt.data.fetch_iex("2026-10-09", SYMS, tmp_path / "iex", yes=True, url=iex_url(IEX_DAY, bucket))
    assert_shape(res, tmp_path, columns={"date", "symbol"})


# ---- demo --------------------------------------------------------------------------------------------------------


def test_demo(itch_gz: Path, tmp_path: Path) -> None:
    res = qt.demo(
        "hftbacktest",
        from_file=itch_gz,
        symbols="AAPL, MSFT",
        minutes="1min",
        n=200,
        threads=1,
        out=tmp_path / "demo",
        cache_dir=tmp_path / "cache",
    )
    df = assert_shape(res, tmp_path, columns={"horizon", "exact_fill_rate", "library_fill_rate", "bias_pp"})
    assert list(df["horizon"]) == ["100ms", "1s", "10s", "1min", "5min"]
    assert res.doc["schema"] == "qt.demo_summary" and res.doc["headline"]["backtester"] == "hftbacktest"
    assert res.to_html() == (res.out / "demo.html").read_text(encoding="utf-8")
    assert json.loads((res.out / "demo.json").read_text(encoding="utf-8"))["run_id"] == res.doc["run_id"]
    assert res.summary in res.text()
    with pytest.raises(QtError):
        qt.demo("zipline", from_file=itch_gz)
    with pytest.raises(QtError):
        qt.demo(from_file=itch_gz, minutes="90s")


# ---- the API surface ---------------------------------------------------------------------------------------------


def test_verbs_have_typed_signatures() -> None:
    check = inspect.signature(qt.check).parameters
    assert {"backtest", "data", "horizon", "window", "backtester"} <= set(check)
    fit = inspect.signature(qt.fit).parameters
    assert {"backtester", "data", "live", "split"} <= set(fit)
    assert {"backtester", "minutes", "symbols", "yes"} <= set(inspect.signature(qt.demo).parameters)
    assert {"library", "symbol", "tier", "horizon"} <= set(inspect.signature(qt.atlas.lookup).parameters)
    assert {"library", "tier", "symbol", "source"} <= set(inspect.signature(qt.atlas.calibration).parameters)
    assert {"files", "out"} <= set(inspect.signature(qt.data.add).parameters)
    assert {"date", "symbols", "out"} <= set(inspect.signature(qt.data.fetch_iex).parameters)
    assert callable(qt.demo) and qt.demo.DemoOptions.__name__ == "DemoOptions"
