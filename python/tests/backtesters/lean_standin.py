"""A minimal stand-in of LEAN's Python `AlgorithmImports` for the tests of `queuetruth.lean` (LEAN is not
installable from PyPI). It mirrors only what `QueueFillModel` touches, with the names of LEAN's documented Python
API: `FillModel.limit_fill(asset, order) -> OrderEvent` (filled at the limit when the bar's low reaches a buy's
limit or its high a sell's), `OrderEvent.status / fill_quantity / fill_price`, `OrderStatus.NONE / FILLED`,
`OrderDirection.BUY / SELL`, `Resolution`, a security's `low`, `high`, `local_time` and `subscriptions`. It is not
LEAN and says nothing about LEAN's own fill logic."""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


class OrderStatus(enum.Enum):
    NONE = 0
    FILLED = 3


class OrderDirection(enum.Enum):
    BUY = 0
    SELL = 1


class Resolution(enum.Enum):
    TICK = 0
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAILY = 4


@dataclass
class OrderEvent:
    order_id: int
    status: OrderStatus = OrderStatus.NONE
    fill_quantity: float = 0
    fill_price: float = 0


@dataclass
class LimitOrder:
    id: int
    quantity: float
    limit_price: float

    @property
    def direction(self) -> OrderDirection:
        return OrderDirection.BUY if self.quantity > 0 else OrderDirection.SELL


@dataclass
class Security:
    low: float
    high: float
    resolution: Resolution | None = None
    local_time: dt.datetime = dt.datetime(2000, 1, 3, 9, 31)
    subscriptions: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.resolution is not None:
            self.subscriptions = [SimpleNamespace(resolution=self.resolution)]


class FillModel:
    def limit_fill(self, asset: Security, order: LimitOrder) -> OrderEvent:
        ev = OrderEvent(order.id)
        buy = order.direction == OrderDirection.BUY
        if (buy and asset.low <= order.limit_price) or (not buy and asset.high >= order.limit_price):
            ev.status, ev.fill_quantity, ev.fill_price = OrderStatus.FILLED, order.quantity, order.limit_price
        return ev

    def market_fill(self, asset: Any, order: Any) -> str:
        return "market"
