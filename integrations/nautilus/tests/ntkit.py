"""A minimal NautilusTrader backtest for engine-level tests (1.231.x and 2.0.x public API).

One equity on an L2_MBP book (bid 100.00 x `level_qty`, ask 100.01 x `level_qty`), and `n` independent episodes of
`step` ns (default 1 microsecond): at 10% of the episode a 1-share GTC buy limit is submitted at the bid, at 15% the
ask level is re-sent unchanged (an event at which an order delayed by a latency model is processed), from 20% the
episode's trade prints (price offset in ticks from the bid, size) arrive, and at 90% the order is cancelled if still
open. Episodes do not overlap, so with or without queue tracking each order sees only its own episode's prints
(every unfilled order is cancelled, which is checked). Returns the set of filled episode indices.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

T0 = 1_700_000_000_000_000_000
STEP = 1_000


def run_episodes(
    venue_options: dict[str, Any],
    n: int,
    prints: Sequence[tuple[int, int]],
    *,
    level_qty: int = 100,
    add_venue: Callable[[Any, Any], None] | None = None,
    step: int = STEP,
) -> set[int]:
    """`prints`: (price offset in ticks from the bid, size) per print, the same in every episode; offset 0 is a touch
    at the order's price, -1 a trade-through. Prints are seller-aggressed (they hit the bid). `add_venue(engine,
    venue)`, when given, adds the venue instead of the kit's own `add_venue` call (`venue_options` is then unused).
    `step` is the episode length in ns (submit at 10%, prints from 20%, cancel at 90% of it): raise it above twice
    any latency model's delays."""
    warnings.filterwarnings("ignore")
    import nautilus_trader

    nt2 = int(nautilus_trader.__version__.split(".")[0]) >= 2
    if nt2:
        from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.common import LoggerConfig
        from nautilus_trader.execution import FixedFeeModel
        from nautilus_trader.model import (
            AccountType,
            AggressorSide,
            BookAction,
            BookOrder,
            Currency,
            Equity,
            InstrumentId,
            Money,
            OmsType,
            OrderBookDelta,
            OrderBookDeltas,
            OrderSide,
            Price,
            Quantity,
            StrategyId,
            Symbol,
            TimeInForce,
            TradeId,
            TraderId,
            TradeTick,
            Venue,
        )
        from nautilus_trader.risk import RiskEngineConfig
        from nautilus_trader.trading import Strategy, StrategyConfig

        usd = Currency.from_str("USD")
        seller = AggressorSide.SELL
    else:
        from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.config import LoggingConfig, RiskEngineConfig, StrategyConfig
        from nautilus_trader.model.currencies import USD as usd
        from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas, TradeTick
        from nautilus_trader.model.enums import AccountType, AggressorSide, BookAction, OmsType, OrderSide, TimeInForce
        from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
        from nautilus_trader.model.instruments import Equity
        from nautilus_trader.model.objects import Money, Price, Quantity
        from nautilus_trader.trading.strategy import Strategy

        seller = AggressorSide.SELLER

    venue = Venue("XNAS")
    iid = InstrumentId(Symbol("QTX"), venue)

    def px(cents: int) -> Price:
        return Price.from_str(f"{cents / 100:.2f}")

    def qty(q: int) -> Quantity:
        return Quantity.from_int(q)

    bid, ask = 10_000, 10_001
    data: list[Any] = []
    snap = [OrderBookDelta.clear(iid, 0, T0, T0)]
    snap.append(
        OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.BUY, px(bid), qty(level_qty), 1), 32, 0, T0, T0)
    )
    snap.append(
        OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.SELL, px(ask), qty(level_qty), 2), 32 | 128, 0, T0, T0)
    )
    data.append(OrderBookDeltas(iid, snap))
    for k in range(n):
        t = T0 + step * (k + 1)
        # an unchanged ask level at 15%: an event between the order's arrival (after any latency) and the prints, so
        # the engine has a timestamp at which to process the inflight order
        tq = t + 3 * step // 20
        data.append(
            OrderBookDeltas(
                iid,
                [
                    OrderBookDelta(
                        iid, BookAction.UPDATE, BookOrder(OrderSide.SELL, px(ask), qty(level_qty), 2), 128, 0, tq, tq
                    )
                ],
            )
        )
        for j, (off, size) in enumerate(prints):
            ts = t + step // 5 + 10 * j
            data.append(TradeTick(iid, px(bid + off), qty(size), seller, TradeId(f"T{k}-{j}"), ts, ts))
    # a last book event after the last cancel alert, so the engine's clock reaches it
    t_end = T0 + step * (n + 1)
    data.append(
        OrderBookDeltas(
            iid,
            [
                OrderBookDelta(
                    iid, BookAction.UPDATE, BookOrder(OrderSide.SELL, px(ask), qty(level_qty), 2), 128, 0, t_end, t_end
                )
            ],
        )
    )

    class Episodes(Strategy):
        def __init__(self) -> None:
            if nt2:
                super().__init__(StrategyConfig(strategy_id=StrategyId("QTTEST-001"), order_id_tag="001"))
            else:
                super().__init__(StrategyConfig(strategy_id="QTTEST-001", order_id_tag="001"))
            self.orders: dict[int, Any] = {}
            self.coid: dict[str, int] = {}
            self.filled: set[int] = set()
            # 2.x runs a shallow copy of the strategy: keep state in mutable containers, never rebind attributes
            self.cancels_sent: list[int] = []

        def on_start(self) -> None:
            for k in range(n):
                t = T0 + step * (k + 1)
                self.clock.set_time_alert_ns(f"s{k}", t + step // 10, self._submit)
                self.clock.set_time_alert_ns(f"c{k}", t + 9 * step // 10, self._cancel)

        def _submit(self, ev: Any) -> None:
            k = int(ev.name[1:])
            o = self.order_factory.limit(
                instrument_id=iid,
                order_side=OrderSide.BUY,
                quantity=qty(1),
                price=px(bid),
                time_in_force=TimeInForce.GTC,
            )
            self.orders[k] = o
            self.coid[o.client_order_id.value] = k
            self.submit_order(o)

        def _cancel(self, ev: Any) -> None:
            o = self.orders.get(int(ev.name[1:]))
            # 2.x: the object order_factory returned is a snapshot that stays INITIALIZED, so read the cache's
            # order; and 2.x cancel_order takes the ClientOrderId (an exception in a timer callback is swallowed)
            c = None if o is None else self.cache.order(o.client_order_id)
            if c is not None and c.is_open:
                self.cancel_order(c.client_order_id if nt2 else c)
                self.cancels_sent.append(int(ev.name[1:]))

        def on_order_filled(self, e: Any) -> None:
            self.filled.add(self.coid[e.client_order_id.value])

    if nt2:
        cfg = BacktestEngineConfig(
            trader_id=TraderId("QT-001"),
            logging=LoggerConfig(bypass_logging=True),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    else:
        cfg = BacktestEngineConfig(
            trader_id="QT-001",
            logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
            risk_engine=RiskEngineConfig(bypass=True),
        )
    engine = BacktestEngine(config=cfg)
    if add_venue is not None:
        add_venue(engine, venue)
    else:
        engine.add_venue(
            venue=venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=usd,
            starting_balances=[Money(10**9, usd)],
            **({"fee_model": FixedFeeModel(Money(0, usd))} if nt2 else {}),
            **venue_options,
        )
    engine.add_instrument(
        Equity(
            instrument_id=iid,
            raw_symbol=Symbol("QTX"),
            currency=usd,
            price_precision=2,
            price_increment=px(1),
            lot_size=qty(1),
            ts_event=0,
            ts_init=0,
        )
    )
    engine.add_data(data, sort=True)
    strat = Episodes()
    engine.add_strategy(strat)
    engine.run()
    filled = set(strat.filled)
    engine.dispose()
    if len(strat.cancels_sent) != n - len(filled):
        raise AssertionError(f"{len(strat.cancels_sent)} cancels for {n - len(filled)} unfilled orders")
    return filled


def run_node(venue_config: Any, catalog_dir: str) -> Any:
    """Runs a `BacktestNode` with `venue_config` over a tiny catalog (an L2 book on XNAS.QTX and three trade prints)
    and no strategy: the high-level API builds the venue (fill, latency and fee models) and replays the data.
    Returns the node's results; raises whatever the engine raises."""
    warnings.filterwarnings("ignore")
    import nautilus_trader

    nt2 = int(nautilus_trader.__version__.split(".")[0]) >= 2
    if nt2:
        from nautilus_trader.backtest import BacktestDataConfig, BacktestEngineConfig, BacktestNode, BacktestRunConfig
        from nautilus_trader.common import LoggerConfig
        from nautilus_trader.model import (
            AggressorSide,
            BookAction,
            BookOrder,
            Currency,
            Equity,
            InstrumentId,
            OrderBookDelta,
            OrderSide,
            Price,
            Quantity,
            Symbol,
            TradeId,
            TraderId,
            TradeTick,
            Venue,
        )
        from nautilus_trader.persistence import ParquetDataCatalog

        usd = Currency.from_str("USD")
        seller = AggressorSide.SELL
    else:
        from nautilus_trader.backtest.node import BacktestNode
        from nautilus_trader.config import BacktestDataConfig, BacktestEngineConfig, BacktestRunConfig, LoggingConfig
        from nautilus_trader.model.currencies import USD as usd
        from nautilus_trader.model.data import BookOrder, OrderBookDelta, TradeTick
        from nautilus_trader.model.enums import AggressorSide, BookAction, OrderSide
        from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
        from nautilus_trader.model.instruments import Equity
        from nautilus_trader.model.objects import Price, Quantity
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        seller = AggressorSide.SELLER
    iid = InstrumentId(Symbol("QTX"), Venue("XNAS"))
    inst = Equity(
        instrument_id=iid,
        raw_symbol=Symbol("QTX"),
        currency=usd,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
    )
    # adds only: a CLEAR delta carries price precision 0, and rc4/rc5 catalogs refuse to write mixed precisions
    deltas = [
        OrderBookDelta(
            iid,
            BookAction.ADD,
            BookOrder(OrderSide.BUY, Price.from_str("100.00"), Quantity.from_int(100), 1),
            0,
            1,
            T0,
            T0,
        ),
        OrderBookDelta(
            iid,
            BookAction.ADD,
            BookOrder(OrderSide.SELL, Price.from_str("100.01"), Quantity.from_int(100), 2),
            128,
            2,
            T0,
            T0,
        ),
    ]
    trades = [
        TradeTick(
            iid,
            Price.from_str("100.00"),
            Quantity.from_int(5),
            seller,
            TradeId(f"T{i}"),
            T0 + STEP * (i + 1),
            T0 + STEP * (i + 1),
        )
        for i in range(3)
    ]
    Path(catalog_dir).mkdir(parents=True, exist_ok=True)
    cat = ParquetDataCatalog(catalog_dir)
    if nt2:
        cat.write_instruments([inst])
        cat.write_order_book_deltas(deltas)
        cat.write_trade_ticks(trades)
        try:  # 2.0.0rc7 takes the NautilusDataType enum; rc4 and rc5 its name
            from nautilus_trader.model import NautilusDataType

            kinds: list[Any] = [NautilusDataType.OrderBookDelta, NautilusDataType.TradeTick]
        except ImportError:
            kinds = ["OrderBookDelta", "TradeTick"]
        data = [BacktestDataConfig(data_type=k, catalog_path=catalog_dir, instrument_id=iid) for k in kinds]
        engine = BacktestEngineConfig(trader_id=TraderId("QT-001"), logging=LoggerConfig(bypass_logging=True))
    else:
        cat.write_data([inst])
        cat.write_data(deltas)
        cat.write_data(trades)
        data = [
            BacktestDataConfig(data_cls=OrderBookDelta, catalog_path=catalog_dir, instrument_id=iid),
            BacktestDataConfig(data_cls=TradeTick, catalog_path=catalog_dir, instrument_id=iid),
        ]
        engine = BacktestEngineConfig(trader_id="QT-001", logging=LoggingConfig(bypass_logging=True))
    run = BacktestRunConfig(venues=[venue_config], data=data, engine=engine, raise_exception=True)
    return BacktestNode([run]).run()
