"""Backtrader in one line: an order log for queuetruth and a fill model from the atlas touch tables.

    import backtrader as bt
    import queuetruth as qt

    cerebro.addanalyzer(qt.backtrader.OrderLog)                       # writes bt_orders.jsonl
    cerebro.broker.set_filler(qt.backtrader.QueueFiller(table="touch.json", tier="T1"))

`OrderLog` writes one JSON line per `notify_order` and `notify_trade` call, the format of `queuetruth live import
--venue sim-backtrader`, `reconcile --kind backtrader` and `check` (docs/how-to/reconcile-backtrader-vectorbt.md).
Its `path` parameter names the file (`cerebro.addanalyzer(qt.backtrader.OrderLog, path="run1.jsonl")`).

`QueueFiller` keeps Backtrader's own limit-order logic (a limit fills when the bar reaches it) and decides, for each
bar it would fill on, whether the real queue would have: it fills with the probability the touch tables give for the
order's tier, side and bar length (`P(real | touch)`), else the order keeps resting. Market, close and stop orders
fill as Backtrader fills them. The draws are seeded and recorded (`filler.decisions`). The probability replaces a
fill rule; it is not a queue model (docs/how-to/realistic-fills-backtrader-lean.md).

Backtrader is imported only when these classes are used; queuetruth does not depend on it.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any

from queuetruth.backtesters.touchfill import FillDecision, TouchFill, bar_name

#: Backtrader execution types `QueueFiller` decides on (a stop-limit is a limit order once triggered).
LIMIT_TYPES = frozenset({"Limit", "StopLimit", "StopTrailLimit"})


def _bt() -> Any:
    try:
        import backtrader
    except ImportError as e:
        from queuetruth.errors import QtError

        raise QtError(
            "QT-CONFIG-001",
            "queuetruth.backtrader needs Backtrader, which queuetruth does not install",
            "pip install backtrader",
        ) from e
    return backtrader


#: `backtrader.TimeFrame.Seconds` and `.Minutes` (stable values since Backtrader 1.0), so the filler needs no import
_SECONDS, _MINUTES = 3, 4


def _bar_seconds(data: Any) -> float | None:
    """A Backtrader data feed's bar length in seconds (None for ticks or days and longer)."""
    unit = {_SECONDS: 1, _MINUTES: 60}.get(getattr(data, "_timeframe", None))
    return None if unit is None else unit * float(getattr(data, "_compression", 1) or 1)


def _num2date(num: float) -> dt.datetime:
    """Backtrader's `num2date` (days since 0001-01-01, as its own `date2num` writes them), without importing it."""
    day = int(num)
    us = round((num - day) * 86_400_000_000)
    return dt.datetime.fromordinal(day) + dt.timedelta(microseconds=us)


class QueueFiller:
    """A Backtrader filler (`cerebro.broker.set_filler`) that fills a resting limit order on a bar that reaches it
    with the probability the atlas touch tables give, `P(real | rule)` for its tier, side and bar length.

    `table`: a `touch.json` file (atlas/v1/touch.json), a `queuetruth.atlas.touch.TouchTables` or its dict. `tier`:
    the liquidity tier of the instruments (`ALL` pools the tiers). `rule`: `touch` (default) or `penetrate_<k>`,
    which also needs `tick` (the tick size): with it the bar must go k ticks through the limit. `seed`: the draws'
    seed (default 0). `bar`: the bar length (`1m`, `5m`, `1h`); by default read from each order's data feed.

    The table, tier and rule are checked when the filler is made, the bar length and side on the first order of
    each; anything the table lacks raises `QT-CONFIG-001`. A draw happens on every bar the rule fires on while the
    order rests, each with the same probability (the table measures the first such bar). `decisions` holds every
    draw, `summary()` the counts. Use a new filler for each run.
    """

    def __init__(
        self,
        table: Any = None,
        *,
        tier: str,
        rule: str = "touch",
        seed: int = 0,
        tick: float | None = None,
        bar: str | None = None,
    ) -> None:
        self.touch = TouchFill(table, tier=tier, rule=rule, seed=seed, tick=tick)
        self.bar = bar
        if bar is not None:
            self.touch.probability(bar, "buy")
            self.touch.probability(bar, "sell")
        self._ids: dict[int, str] = {}
        self._seen: dict[str, int] = {}

    @property
    def decisions(self) -> list[FillDecision]:
        """Every draw so far."""
        return self.touch.decisions

    def summary(self) -> dict[str, Any]:
        """Draws made, fills given and declined."""
        return self.touch.summary()

    def _order_id(self, order: Any) -> str:
        """An identity of the order from what it is (feed, creation time, side, price, size), not from Backtrader's
        process-wide `ref` counter, so a second run in the same process draws the same."""
        ref = int(order.ref)
        if ref not in self._ids:
            data = order.data
            created = order.created
            base = (
                f"{getattr(data, '_name', '') or ''}|{created.dt!r}|{'buy' if order.isbuy() else 'sell'}|"
                f"{created.price!r}|{created.size!r}"
            )
            n = self._seen.get(base, 0)
            self._seen[base] = n + 1
            self._ids[ref] = f"{base}|{n}"
        return self._ids[ref]

    def __call__(self, order: Any, price: float, ago: int) -> float:
        remaining = abs(order.executed.remsize)
        if order.getordername() not in LIMIT_TYPES:
            return remaining
        data = order.data
        side = "buy" if order.isbuy() else "sell"
        limit = order.created.pricelimit if order.getordername() != "Limit" else order.created.price
        if limit is None or (isinstance(limit, float) and math.isnan(limit)):
            limit = price
        if not self.touch.penetrated(side, float(limit), float(data.low[ago]), float(data.high[ago])):
            return 0.0
        bar = self.bar or bar_name(_bar_seconds(data))
        when = _num2date(float(data.datetime[ago])).isoformat()
        d = self.touch.decide(order=self._order_id(order), bar_time=when, side=side, limit=float(limit), bar=bar)
        return remaining if d.filled else 0.0


def _order_log_class() -> type:
    bt = _bt()

    class OrderLog(bt.Analyzer):  # type: ignore[misc, name-defined]
        """Every order and trade notification as one JSON line, for `queuetruth live import --venue sim-backtrader`,
        `reconcile --kind backtrader` and `check`. Parameter `path` (default `bt_orders.jsonl`)."""

        params = (("path", "bt_orders.jsonl"),)

        def start(self) -> None:
            self._f = open(self.p.path, "w", encoding="utf-8")
            self._n = 0

        def stop(self) -> None:
            self._f.close()

        def get_analysis(self) -> dict[str, Any]:
            return {"path": self.p.path, "lines": getattr(self, "_n", 0)}

        @staticmethod
        def _t(num: float | None) -> str | None:
            return bt.num2date(num).isoformat() if num else None

        @staticmethod
        def _num(v: float | None) -> float | None:
            return v if v is not None and math.isfinite(v) else None

        def _data(self, data: Any) -> str:
            return data._name or f"data{next(i for i, d in enumerate(self.strategy.datas) if d is data)}"

        def _write(self, row: dict[str, Any]) -> None:
            self._f.write(json.dumps(row) + "\n")
            self._n += 1

        def notify_order(self, order: Any) -> None:
            bits = [
                {"dt": self._t(b.dt), "size": b.size, "price": b.price, "comm": b.comm}
                for b in order.executed.iterpending()
            ]
            self._write(
                {
                    "event": "order",
                    "ref": order.ref,
                    "data": self._data(order.data),
                    "status": order.getstatusname(),
                    "ordtype": order.ordtypename(),
                    "exectype": order.getordername(),
                    "size": order.created.size,
                    "price": self._num(order.created.price),
                    "pricelimit": self._num(order.created.pricelimit),
                    "created": self._t(order.created.dt),
                    "dt": self._t(order.data.datetime[0]),
                    "valid": self._t(order.valid) if order.valid else None,
                    "exbits": bits,
                    "order_key": order.info.get("order_key"),
                }
            )

        def notify_trade(self, trade: Any) -> None:
            self._write(
                {
                    "event": "trade",
                    "ref": trade.ref,
                    "data": self._data(trade.data),
                    "status": trade.status_names[trade.status],
                    "dtopen": self._t(trade.dtopen),
                    "dtclose": self._t(trade.dtclose),
                    "size": trade.size,
                    "price": trade.price,
                    "pnl": trade.pnl,
                    "pnlcomm": trade.pnlcomm,
                }
            )

    OrderLog.__module__ = __name__
    OrderLog.__qualname__ = "OrderLog"
    return OrderLog


def __getattr__(name: str) -> Any:
    """`OrderLog` is a `backtrader.Analyzer` subclass, built when first used (Backtrader is optional)."""
    if name == "OrderLog":
        cls = _order_log_class()
        globals()[name] = cls
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["LIMIT_TYPES", "OrderLog", "QueueFiller"]  # noqa: F822 (OrderLog is built on first access)
