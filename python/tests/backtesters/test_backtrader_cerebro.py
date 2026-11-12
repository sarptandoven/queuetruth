"""A real Backtrader run with `qt.backtrader.QueueFiller` and `qt.backtrader.OrderLog` on synthetic minute bars.

Backtrader is not a dependency of queuetruth, so this runs only where it is installed (skipped otherwise). To run it:
make a separate venv with Backtrader that also sees the project's packages, then run pytest from it."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import pytest

import queuetruth as qt
from queuetruth.errors import QtError

from . import synth_touch

bt = pytest.importorskip("backtrader")

T0 = dt.datetime(2000, 1, 3, 9, 30)


def _bars(n: int = 240) -> list[tuple[dt.datetime, float, float, float, float, float]]:
    """A smooth synthetic price path: every limit placed half a point away is touched within a few bars."""
    rows = []
    for i in range(n):
        o, c = 100 + math.sin(i / 7) * 2, 100 + math.sin((i + 1) / 7) * 2
        rows.append((T0 + dt.timedelta(minutes=i), o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1000.0))
    return rows


class _Feed(bt.feeds.DataBase):  # type: ignore[misc]
    params = (("rows", None),)

    def start(self) -> None:
        super().start()
        self._i = 0

    def _load(self) -> bool:
        if self._i >= len(self.p.rows):
            return False
        t, o, h, lo, c, v = self.p.rows[self._i]
        self._i += 1
        self.lines.datetime[0] = bt.date2num(t)
        self.lines.open[0], self.lines.high[0], self.lines.low[0] = o, h, lo
        self.lines.close[0], self.lines.volume[0] = c, v
        return True


class _Quotes(bt.Strategy):  # type: ignore[misc]
    """Every 10 bars, a buy half a point under the close and a sell half a point over it, good for 3 bars."""

    def next(self) -> None:
        if len(self) % 10 == 1:
            valid = self.data.datetime.datetime(0) + dt.timedelta(minutes=3)
            px = self.data.close[0]
            self.buy(price=px - 0.5, exectype=bt.Order.Limit, size=1, valid=valid)
            self.sell(price=px + 0.5, exectype=bt.Order.Limit, size=1, valid=valid)


def _run(filler: Any, log: Path, compression: int = 1) -> None:
    c = bt.Cerebro()
    c.adddata(_Feed(rows=_bars(), timeframe=bt.TimeFrame.Minutes, compression=compression), name="QTAU6")
    c.addstrategy(_Quotes)
    if filler is not None:
        c.broker.set_filler(filler)
    c.addanalyzer(qt.backtrader.OrderLog, path=str(log))
    c.run()


def _without_refs(log: Path) -> list[dict[str, Any]]:
    """The log's rows without Backtrader's process-wide order and trade numbers."""
    return [{k: v for k, v in r.items() if k != "ref"} for r in map(json.loads, log.read_text().splitlines())]


def _statuses(log: Path) -> list[str]:
    return [r["status"] for r in map(json.loads, log.read_text().splitlines()) if r["event"] == "order"]


def test_cerebro_run_with_the_queue_filler(tmp_path: Path) -> None:
    table = tmp_path / "touch.json"
    table.write_text(synth_touch.tables().model_dump_json(by_alias=True))
    f1 = qt.backtrader.QueueFiller(table=table, tier="T1", seed=11)
    f2 = qt.backtrader.QueueFiller(table=table, tier="T1", seed=11)
    _run(f1, tmp_path / "a.jsonl")
    _run(f2, tmp_path / "b.jsonl")  # same process, new order refs: the same draws
    _run(None, tmp_path / "c.jsonl")

    assert f1.decisions and [d.to_dict() for d in f1.decisions] == [d.to_dict() for d in f2.decisions]
    assert _without_refs(tmp_path / "a.jsonl") == _without_refs(tmp_path / "b.jsonl")
    with_filler, without = _statuses(tmp_path / "a.jsonl"), _statuses(tmp_path / "c.jsonl")
    assert without.count("Completed") > with_filler.count("Completed") > 0
    assert with_filler.count("Completed") == sum(d.filled for d in f1.decisions)
    declined = without.count("Completed") - with_filler.count("Completed")
    assert with_filler.count("Expired") == without.count("Expired") + declined  # declined orders rest, then expire
    assert "Partial" not in with_filler
    assert {d.p for d in f1.decisions} == {0.25, 0.40}


def test_order_log_is_read_by_the_backtrader_importer(tmp_path: Path) -> None:
    from queuetruth import backtesters

    log = tmp_path / "bt_orders.jsonl"
    _run(None, log)
    assert backtesters.detect(log) == "backtrader"
    from queuetruth.live import import_sim

    res = import_sim("backtrader", [str(log)], tmp_path / "out")
    assert res.venue == "sim:backtrader"


def test_cerebro_refuses_a_bar_length_the_table_lacks(tmp_path: Path) -> None:
    f = qt.backtrader.QueueFiller(table=synth_touch.tables(), tier="T1")
    with pytest.raises(QtError, match="no 1h bars"):
        _run(f, tmp_path / "x.jsonl", compression=60)
