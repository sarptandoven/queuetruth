Prediction-market fixtures of `queuetruth pm`. Invented markets, accounts, order ids and values: no venue
data.

Written by `queuetruth pm fixtures` (queuetruth.pm.fixtures) from an exact simulated price-time FIFO book
(individual orders: other traders add near the touch, cancel random resting orders and send takers that sweep the
best levels; a small market maker, the "own" account, joins the best bid and ask, rests a few seconds and cancels),
in the message formats of the public API documentation (docs.kalshi.com and docs.polymarket.com, read 2026-10-24).
Every generation is byte for byte the same (MANIFEST.sha256). Write them with

    queuetruth pm fixtures --out DIR

kalshi/ws.jsonl          WebSocket capture (capture-wrapped): subscribe commands, orderbook_snapshot, orderbook_delta
                         (own changes carry client_order_id), trade, fill, user_order, market_lifecycle_v2
kalshi/rest.jsonl        REST responses: market (price_ranges), series (fee_type), incentive_programs, the
                         queue_positions request and response around every sample (local send and receive times),
                         the fills export
kalshi/truth.json        what no venue publishes: each own order's true queue ahead at entry, at every queue
                         sample and at every fill, and the final book
polymarket/market.jsonl  market channel: book (both tokens), price_change, last_trade_price, market_resolved
polymarket/user.jsonl    user channel: order (PLACEMENT, UPDATE, CANCELLATION) and trade (MATCHED, MINED, CONFIRMED)
polymarket/gamma.json    Gamma market object: clobTokenIds, feeSchedule, rewards settings and clobRewards
polymarket/logs.json     eth_getLogs result of the CTF Exchange V2: OrderFilled per maker in operator order,
                         FeeCharged, the taker's OrderFilled and OrdersMatched; plus one reorged (removed) log and
                         one unrelated Transfer
polymarket/truth.json    every match's makers in operator order with their exact queue ahead, and the own orders'
                         truth as for Kalshi
quotes.csv               hypothetical quotes for `pm score --quotes` on the Kalshi fixture
MANIFEST.sha256          sha256 of every file above (sha256sum format)
