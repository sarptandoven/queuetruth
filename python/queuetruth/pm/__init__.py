"""Prediction-market kit: Kalshi and Polymarket captures -> L2 books, queue calibration and reward-aware MM PnL.

Self-hosted use on your own data only. Kalshi's rulebook (Rule 11.1) and the Polymarket terms bar redistributing
their market data and derived outputs: the kit reads files you captured or exported yourself, writes its results
next to them, ships no venue data and has no network code (`queuetruth.pm.terms`).

Pipeline (`queuetruth pm ingest | calibrate | score`, or the functions below; `queuetruth pm fixtures`,
`queuetruth.pm.fixtures.write_fixtures`, writes invented example captures to try it on):

1. `queuetruth.pm.ingest.ingest(spec)`: Kalshi WebSocket captures (`orderbook_snapshot` / `orderbook_delta`,
   `trade`, `fill`, `user_order`) and REST exports (orderbook, fills, orders, queue positions, markets, series,
   incentive programs), or Polymarket CLOB WebSocket captures (market channel `book`, `price_change`,
   `last_trade_price`; user channel `order`, `trade`), Gamma market metadata and a Polygon log export of the CTF
   Exchange (`OrderFilled`, `OrdersMatched`) -> an L2-only queuetruth dataset of the book as published, the same
   book without your own resting orders, the canonical live log of your orders and fills, and the kit's tables
   (official queue positions, queue entry points, decoded chain fills, the queue ahead each own order had at its
   first on-chain fill).
2. `queuetruth.pm.calibrate.calibrate(spec)`: queue-position models (`queuetruth.pm.queue`, the corrected mode of
   the Rust zoo, and their one-order-per-change `atomic` variants) fitted by order-weighted RMSE against Kalshi's
   official queue positions or the queue truth at your own fills, with the `cancels_ahead` / `risk_adverse` queue
   bounds and a trust verdict in the form of `calibrate-live`'s.
3. `queuetruth.pm.score.score(spec)`: PnL = spread capture - adverse selection - fees + rewards (+ Polymarket maker
   rebates) of your live orders or hypothetical quotes, with fills from the calibrated model and the
   `trade_through_only` / `front_of_queue` fill bounds as a band, and your live fills against the simulation.

Formulas: `queuetruth.pm.fees` (Kalshi and Polymarket trading fees, Kalshi fee rounding, Polymarket maker
rebates) and `queuetruth.pm.rewards` (Kalshi Liquidity Incentive Program, Polymarket liquidity rewards), each with
its sources and the date they were read. Schemas: `queuetruth.pm.models`, `queuetruth.pm.tables` and the committed
files of `queuetruth.pm.schemas`. Walkthrough: `docs/how-to/prediction-market-making.md`.

This package imports nothing at load time, so the main CLI can route `queuetruth pm` without loading pyarrow.
"""

from __future__ import annotations

__all__: list[str] = []
