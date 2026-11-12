"""QuantConnect LEAN in one line: a fill model from the atlas touch tables.

    # in a LEAN Python algorithm
    import queuetruth as qt

    security.set_fill_model(qt.lean.QueueFillModel(tier="T1"))   # the atlas's touch tables (or table="touch.json")

LEAN's default fill model fills a limit order as soon as a bar trades at or through its price. `QueueFillModel`
returns an instance of a `FillModel` subclass that keeps LEAN's own limit-fill logic and accepts each limit fill
with the probability the touch tables give for the order's tier, side and bar length (`P(real | touch)`); a fill it
declines leaves the order resting. Other order types fill as LEAN fills them. The draws are seeded and recorded
(`model.decisions`). The probability replaces a fill rule; it is not a queue model
(docs/how-to/realistic-fills-backtrader-lean.md).

LEAN's classes come from `AlgorithmImports`, imported only when `QueueFillModel` is called; queuetruth does not
depend on LEAN. The method names follow LEAN's documented Python API (`limit_fill(asset, order)`, `OrderEvent`
fields `status`, `fill_quantity`, `fill_price`); queuetruth's tests run against a stand-in of that API, since LEAN
is not installable from PyPI.
"""

from __future__ import annotations

from typing import Any

from queuetruth.backtesters.touchfill import FillDecision, TouchFill, bar_name
from queuetruth.errors import QtError

_CLASS: list[type] = []


def _tables(table: Any, atlas: Any, yes: bool) -> Any:
    """`table`, or the atlas's touch tables when none is given (`queuetruth.atlas.touch_tables`)."""
    if table is not None:
        return table
    from queuetruth.atlas import touch_tables

    return touch_tables(atlas, yes=yes)


def _lean() -> Any:
    try:
        import AlgorithmImports  # type: ignore[import-not-found]
    except ImportError as e:
        raise QtError(
            "QT-CONFIG-001",
            "queuetruth.lean runs inside a QuantConnect LEAN Python algorithm (no AlgorithmImports module here)",
            "use it in a LEAN algorithm: security.set_fill_model(qt.lean.QueueFillModel(table=..., tier=...))",
        ) from e
    return AlgorithmImports


def _resolution_seconds(lean: Any, asset: Any) -> float | None:
    """The bar length of a security's data in seconds, from its subscriptions' resolution (None when unknown)."""
    res = getattr(lean, "Resolution", None)
    if res is None:
        return None
    seconds = {
        getattr(res, n, None): s
        for n, s in (("SECOND", 1), ("MINUTE", 60), ("HOUR", 3600), ("Second", 1), ("Minute", 60), ("Hour", 3600))
    }
    seconds.pop(None, None)
    found = set()
    for cfg in getattr(asset, "subscriptions", None) or ():
        r = getattr(cfg, "resolution", None)
        if r in seconds:
            found.add(seconds[r])
    return found.pop() if len(found) == 1 else None


def _model_class() -> type:
    if _CLASS:
        return _CLASS[0]
    lean = _lean()

    class QueueFillModelImpl(lean.FillModel):  # type: ignore[misc, name-defined]
        """LEAN's fill model with each limit fill accepted with the touch tables' `P(real | rule)`."""

        def limit_fill(self, asset: Any, order: Any) -> Any:
            fill = super().limit_fill(asset, order)
            if fill.status != lean.OrderStatus.FILLED:
                return fill
            touch: TouchFill = self._qt_touch
            side = "buy" if order.direction == lean.OrderDirection.BUY else "sell"
            limit = float(order.limit_price)
            low, high = float(asset.low), float(asset.high)
            if not touch.penetrated(side, limit, low, high):
                return self._qt_decline(fill)
            bar = self._qt_bar or bar_name(_resolution_seconds(lean, asset))
            d = touch.decide(order=str(order.id), bar_time=str(asset.local_time), side=side, limit=limit, bar=bar)
            return fill if d.filled else self._qt_decline(fill)

        @staticmethod
        def _qt_decline(fill: Any) -> Any:
            fill.status = lean.OrderStatus.NONE
            fill.fill_quantity = 0
            fill.fill_price = 0
            return fill

        @property
        def decisions(self) -> list[FillDecision]:
            """Every draw so far."""
            return self._qt_touch.decisions

        def summary(self) -> dict[str, Any]:
            """Draws made, fills given and declined."""
            return self._qt_touch.summary()

    QueueFillModelImpl.__module__ = __name__
    _CLASS.append(QueueFillModelImpl)
    return QueueFillModelImpl


def QueueFillModel(
    table: Any = None,
    *,
    tier: str,
    rule: str = "touch",
    seed: int = 0,
    tick: float | None = None,
    bar: str | None = None,
    atlas: Any = None,
    yes: bool = False,
) -> Any:
    """A LEAN `FillModel` whose limit fills happen with the touch tables' `P(real | rule)` for the order's tier, side
    and bar length.

    `table`: a `touch.json` file (atlas/v1/touch.json), a `queuetruth.atlas.touch.TouchTables` or its dict; without it
    the atlas's own tables (`atlas`: a directory or URL of the data API, default the public atlas, read after consent:
    `yes=True`, or a cached copy). `tier`: the liquidity tier of the security (`ALL` pools the tiers). `rule`: `touch`
    (default) or `penetrate_<k>`, which also needs `tick`. `seed`: the draws' seed (default 0). `bar`: `1m`, `5m` or
    `1h`; by default read from the security's data resolution (minute or hour). Anything the table lacks raises
    `QT-CONFIG-001`; no probability is assumed. Draws are keyed by LEAN's order id and the bar time, so a rerun with the
    same seed fills the same."""
    touch = TouchFill(_tables(table, atlas, yes), tier=tier, rule=rule, seed=seed, tick=tick)
    if bar is not None:
        touch.probability(bar, "buy")
        touch.probability(bar, "sell")
    model = _model_class()()
    model._qt_touch = touch
    model._qt_bar = bar
    return model


__all__ = ["QueueFillModel"]
