"""backtesting.py in `queuetruth check` (plan section 4.3): a `Backtest.run()` result's `_trades`, saved as CSV or
pickle-free JSON, read by its importer through the `queuetruth.backtesters` registry and replayed in the exact queue
of tests/fixtures/synth_small. Synthetic trades only; the last test runs the real backtesting.py when it is installed
(it is not a dependency)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import queuetruth as qt
from queuetruth import backtesters, cli
from queuetruth.check import check, detect
from queuetruth.errors import QtError
from queuetruth.live.importers import backtesting_py as bp

from . import synth_backtests as sb

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "tests" / "fixtures" / "synth_small"
WINDOW = "10s"


@pytest.fixture(scope="module")
def trades(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("btpy")
    os_ = sb.orders(DATA, instruments=(1,))
    return {
        "csv": sb.write_backtesting_py(d / "trades.csv", os_),
        "json": sb.write_backtesting_py(d / "trades.json", os_, json_records=True),
    }


def test_detected_and_registered(trades: dict[str, Path]) -> None:
    assert detect(trades["csv"]) == detect(trades["json"]) == "backtesting_py"
    b = backtesters.get("backtesting_py")
    assert b.label == "backtesting.py" and b.import_sim.__name__ == "import_backtesting_py_sim"
    assert "backtesting_py" in backtesters.names(check=True)


def test_each_trade_is_an_entry_and_an_exit_order(trades: dict[str, Path]) -> None:
    run = bp.read_backtesting_py([str(trades["csv"])], {})
    rows = trades["csv"].read_text().splitlines()[1:]
    assert len(run.orders) == 2 * len(rows)
    first = rows[0].split(",")
    size, epx, xpx = Decimal(first[1]), Decimal(first[4]), Decimal(first[5])
    entry = next(o for o in run.orders if o.ref == "asset0-f0-t0-entry")
    exit_ = next(o for o in run.orders if o.ref == "asset0-f0-t0-exit")
    assert entry.side == ("buy" if size > 0 else "sell") and exit_.side != entry.side
    assert entry.qty == exit_.qty == abs(size) and entry.order_type == "market"
    assert (entry.fills[0].px, exit_.fills[0].px) == (epx, xpx)
    fees = entry.fills[0].fee + exit_.fills[0].fee  # type: ignore[operator]
    assert fees == Decimal("0.02") and entry.fills[0].fee == (Decimal("0.02") * epx / (epx + xpx)).quantize(
        Decimal("1e-12")
    )
    assert any("names no instrument" in w for w in run.warnings)
    assert any("Commission (entry and exit together) split" in w for w in run.warnings)
    named = bp.read_backtesting_py([str(trades["json"])], {"instrument": "QTAU6", "order_type": "limit"})
    assert {o.instrument for o in named.orders} == {"QTAU6"} and named.orders[0].limit_px is not None
    assert [(o.side, o.qty, o.placed_ns) for o in named.orders] == [(o.side, o.qty, o.placed_ns) for o in run.orders]


def test_bad_exports_are_refused(tmp_path: Path, trades: dict[str, Path]) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(",Size,EntryBar,ExitBar,EntryPrice,ExitPrice\n0,1,0,1,10,11\n")
    with pytest.raises(QtError, match="missing \\['EntryTime', 'ExitTime'\\]") as e:
        bp.read_backtesting_py([str(bad)], {})
    assert e.value.code == "QT-INPUT-002"
    bars = tmp_path / "bars.json"
    bars.write_text(json.dumps([{"Size": 1, "EntryBar": 3, "ExitBar": 5, "EntryPrice": 10.0, "ExitPrice": 11.0,
                                 "EntryTime": 3, "ExitTime": 5}]))  # fmt: skip
    with pytest.raises(QtError, match="bar number"):
        bp.read_backtesting_py([str(bars)], {})
    with pytest.raises(QtError, match="a run trades one instrument") as e:
        bp.read_backtesting_py([str(trades["csv"])], {"instruments": "asset0:QTAU6"})
    assert e.value.code == "QT-CONFIG-001"


@pytest.mark.requires_core
def test_check_replays_the_trades_in_the_exact_queue(trades: dict[str, Path], tmp_path: Path) -> None:
    opts = {"instrument": "QTAU6"}
    a = check(trades["csv"], data=DATA, out=tmp_path / "csv", window=WINDOW, options=opts)
    b = check(trades["json"], data=DATA, out=tmp_path / "json", window=WINDOW, options=opts)
    d = a.doc
    n = 2 * (len(trades["csv"].read_text().splitlines()) - 1)
    assert d["n_checked"] == n and d["backtest_filled"] == n
    assert d["two_by_two"]["phantom"] == n - d["two_by_two"]["both"] and d["two_by_two"]["real_only"] == 0
    assert any("backtesting.py trade records are fills only" in x for x in d["notes"])
    assert a.summary == b.summary and d["two_by_two"] == b.doc["two_by_two"]
    assert a.summary.startswith(f"Your backtest filled {n} of {n} limit orders within 10s; in the real queue ")


@pytest.mark.requires_core
def test_cli_check_reads_a_backtesting_py_file(trades: dict[str, Path], tmp_path: Path, capsys: Any) -> None:
    code = cli.main(["check", str(trades["csv"]), "--data", str(DATA), "--window", WINDOW, "--out",
                     str(tmp_path / "c"), "--option", "instrument=QTAU6", "--json"])  # fmt: skip
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["schema"] == "qt.check" and doc["backtester"] == "backtesting_py"


def test_live_import_venue(trades: dict[str, Path], tmp_path: Path) -> None:
    from queuetruth.live import LiveImportWarning

    with pytest.warns(LiveImportWarning, match="sim:backtesting_py/asset0"):  # no instrument map: not scorable
        s = qt.import_live({"venue": "sim_backtesting_py", "inputs": [{"path": str(trades["csv"])}],
                            "out": str(tmp_path / "log")})  # fmt: skip
    assert s.venue == "sim:backtesting_py" and s.validation_status == "simulated"
    assert s.n_orders == 2 * (len(trades["csv"].read_text().splitlines()) - 1)


def test_the_real_backtesting_py(tmp_path: Path) -> None:
    """A tiny backtesting.py run on a synthetic price series, its `_trades` saved as the docs say, read back."""
    pytest.importorskip("backtesting")
    import numpy as np
    import pandas as pd
    from backtesting import Backtest, Strategy

    idx = pd.date_range("2026-06-15 14:00", periods=120, freq="1min")
    close = 100 + np.sin(np.arange(120) / 6.0)
    ohlc = pd.DataFrame({"Open": close, "High": close + 0.05, "Low": close - 0.05, "Close": close, "Volume": 1000},
                        index=idx)  # fmt: skip

    class Flip(Strategy):
        def init(self) -> None:
            pass

        def next(self) -> None:
            if len(self.data) % 10 == 0:
                if self.position.is_long:
                    self.position.close()
                    self.sell(size=1)
                else:
                    self.position.close()
                    self.buy(size=1)

    stats = Backtest(ohlc, Flip, cash=10_000, commission=0.001, finalize_trades=True).run()
    t = stats._trades
    t.to_csv(tmp_path / "trades.csv")
    t.to_json(tmp_path / "trades.json", orient="records", date_format="iso")
    t.to_json(tmp_path / "trades_epoch.json", orient="records")
    for name in ("trades.csv", "trades.json", "trades_epoch.json"):
        p = tmp_path / name
        assert detect(p) == "backtesting_py", name
        run = bp.read_backtesting_py([str(p)], {"instrument": "TEST"})
        assert len(run.orders) == 2 * len(t)
        assert run.orders[0].placed_ns == int(t["EntryTime"].iloc[0].value)
        fees = sum((f.fee or 0) for o in run.orders for f in o.fills)
        assert abs(float(fees) - float(t["Commission"].sum())) < 1e-6
