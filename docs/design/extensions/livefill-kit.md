# Extension: own-account live-fill kit (`tools/livefill/`)

Status: implemented in `tools/livefill/` (Python, standard library plus pyarrow, numpy and queuetruth; `websockets`
for live captures only). It sits outside the `queuetruth` package and talks to it only through public APIs:
`queuetruth.api.ingest`, `queuetruth.api.score`, `queuetruth.live.write_canonical_log`, `queuetruth.live.check`,
`queuetruth.stats.clustered_mcnemar`, `queuetruth.schemas.runid.canonical_json` and the `queuetruth` CLI
(`zoo --list-models`). User guide: `docs/how-to/own-account-live-fills.md`.

## Scope

- Venue: Bitstamp spot (public WebSocket API v2 channels `live_orders_<m>`, `live_trades_<m>`,
  `detail_order_book_<m>`; public REST `order_book/<m>/?group=2`, `markets/`; private REST v2 `account_balances`,
  `buy`, `sell`, `cancel_order`, `order_status`, `open_orders`).
- The IBKR route (limit orders on IEX, DEEP+ as market data) is a documented procedure only; no IBKR code.
- The kit never places an order unless the live guard below passes; tests never send a private request.

## Artifacts and schemas

| Artifact | Identifier | Content |
|---|---|---|
| capture directory | `livefill.capture_meta` (v1, `capture_meta.json`), `livefill.bitstamp_capture` (v1, Parquet key-value metadata `livefill.schema`) | `messages/part-NNNNN.parquet` with columns `seq` int64, `recv_ns` int64, `mono_ns` int64, `conn` int32, `segment` int32, `kind` string (`ws`, `rest_book`, `stale_rest_book`, `marker`, `duplicate`), `channel`, `market`, `event`, `event_id`, `pre_event_id` strings, `microtimestamp`, `order_id`, `trade_id`, `buy_order_id`, `sell_order_id` int64, `raw` large_string; `gaps.jsonl` |
| capture check | `livefill.capture_check` (v1) | verdict, per (segment, market) seed and snapshot counts (`compared`, `exact`, `explained`, `unexplained`), `unknown_deletes`, differences by kind, `max_skew_messages`, `unchecked_streams` |
| export | `livefill.export` (v1) | one `queuetruth ingest --format bitstamp` input directory per segment |
| harness log | `livefill.harness_log` (v1, JSON lines) | `run_start` (mode `dry_run`, `live`, `stand_in` or `synthetic`; configuration, plan, plan sha256, nonce, client order id prefix), then `market_info`, `preflight`, `touch`, `skip`, `submit`, `cancel`, `status`, `open_orders`, `error`, `run_end` records with `sent_ns` / `recv_ns` |
| match | `livefill.match` (v1), `livefill.own_orders`, `livefill.own_fills`, `livefill.own_queue` (Parquet, v1) | per own order (keyed by log and index): how it was found, entry stamp, queue position at entry, fills with fees, end state, consistency checks |
| pipeline | `livefill.pipeline` (v1) | per capture step results, `pooled` per arm, equivalent `queuetruth` commands, `run_id` (below) |

These are kit-local schemas; they are not registered in `queuetruth.schemas` and have no generated JSON Schema.

## Rules

1. **Gaps and segments.** Per market and connection, `pre_event_id` must equal the previous `event_id`. A gap is an
   `event_chain` break, a `restart` (null `pre_event_id` after earlier messages) or a `reconnect` (any new
   connection; a stall of `stall_s` without frames forces one). Each gap opens a new segment at the frame that
   revealed it. A chain break or restart resets only that market's chain; a reconnect resets every market's. A
   message without a `pre_event_id` member is counted (`no_pre_event_id`) and continues the chain. A repeated
   `event_id` is stored as `kind = duplicate` and never exported. A REST book is stamped with the (connection,
   segment) of its request; an answer arriving on another one is stored as `stale_rest_book`. A storage error stops
   the capture with `QT-INPUT-001`.
2. **Seed.** Per (segment, market), chosen by the book seed (`book_seed`, CLI `--book-seed`): with `auto` the first
   REST book, else the first `detail_order_book` frame; in both cases the first *unambiguous* one, after which no
   order or trade message arrives stamped earlier than the snapshot. In a segment after the first, a REST book must
   be stamped at or after the segment's first frame (venue stamp against the local receive clock). After the seed,
   every message stamped at or after the seed's stamp is applied in arrival order (the ingest's rule). A WebSocket
   seed side cut at 100 entries is known order by order only at prices strictly better than its worst price.
3. **Book.** Price-time; a change keeps its place only at the same price without a size increase; orders queued at one
   price in the same venue stamp rank by order id (`id_ties`; `--arrival-ties` ranks them by arrival, the ingest's
   rule). A created-then-deleted pair within 100 microseconds with nothing in between is a taker and never rests,
   unless a trade shows it as the maker.
4. **Check.** Each later `detail_order_book` frame is compared with the book at its own arrival position over the
   prices both know; a difference is explained when a message about the same order (for `queue_order`, about any
   order of the level) arrived within `window` messages (default 256). Inside the seed's known region, a delete of an
   order the book does not hold, that is not a taker and whose `order_created` the stream never carried, is an
   unknown delete (a lost create). Verdict, first match: `inconsistent` (an unexplained difference or an unknown
   delete), `unchecked`, `partial` (a stream without a seed or a later snapshot), `gaps`, `consistent`. Streams are
   loaded and checked one at a time, and snapshots are parsed only when compared.
5. **Matching.** Orders are keyed by (log, index); a client order id in two submits is `QT-INPUT-003`. By venue order
   id; without an id, or with an id the feed never carried (`signature_after_id_miss`, a warning), by signature
   (side, price, amount, created within 1 s before to 30 s after the send, unique). Consistency checks: trades equal
   the amount lost on `live_orders`, the sum of per-message `amount_traded`, and the final `order_status` executed
   amount; FIFO violations counted after allowing for trades printed before their order messages. A fill's fee is
   that of the final `order_status` transaction with the same trade id.
6. **Canonical log.** As in the guide's table; fills carry `fee` and `fee_ccy` when known; `validation_status =
   real` only for live harness runs. Excluded and counted: not in the feed, ambiguous, spanning a gap, not on the book
   at entry.
7. **Live guard.** `live=True`, `confirm == "place-real-orders-on-<market>"`, non-empty `BITSTAMP_API_KEY` and
   `BITSTAMP_API_SECRET` from the environment (not the dry-run placeholder), explicit `max_order_notional` and
   `max_total_notional` with `notional_max <= max_order_notional`, and maker-or-cancel orders. Only the resulting
   `LiveAuthorization` constructs the HTTP transport; a dry-run harness rejects it and a live harness requires it.
   A signed `account_balances` preflight whose server signature verifies must pass before the first order. Caps use
   the exact notional `price * amount`; `max_net_position` bounds the net filled base amount. Client order ids are
   `qtlf-<plan tag>-<run nonce>-<index>`. Fail closed: any transport exception, HTTP 408, 429 or 5xx, a non-JSON body
   or an unverified server signature is an `unknown` outcome; an unknown submit stops the run after one status lookup
   and the order stays possibly open. On every exit the harness cancels the orders it believes open, then every open
   order of the market whose client order id carries the run's prefix (or, when that listing fails, each unresolved
   order by client order id).
8. **Pipeline.** One or more captures; each gets the orders sent during its recording on its markets (an order in
   two captures of one market is `QT-USAGE-001`). `out` must be absent or empty (`QT-CONFIG-001`), as for export.
   `pooled`, per arm: summed 2x2, exact McNemar on the pooled discordant counts, fill-rate bias with a 95% bootstrap
   interval resampling segments (`clustered_mcnemar`, B 2000, seed 0). `run_id` = first 16 hex digits of
   sha256(canonical_json({engine_version, spec})) with `engine_version` naming the queuetruth and livefill versions
   and `spec` the sha256 of every capture part, each capture's market list and every harness log, plus the options
   (the shape of design section 7.3).

## Error codes

`QT-USAGE-001` (guard failures, bad options, an existing capture or log, overlapping captures), `QT-CONFIG-001`
(writer limits, a non-empty output directory), `QT-INPUT-001` (missing capture or log, a capture storage error),
`QT-INPUT-002` (malformed frames, snapshots or submit records, foreign Parquet, bad logs), `QT-INPUT-003` (the same
order id or client order id in two submits), `QT-INPUT-004` (inexact decimals), `QT-DATA-001`
(`--require-consistent` with an inconsistent capture).

## Findings for the core (fixed at integration in `qt_crypto::bitstamp`)

- It merged by (stamp, receive time), so same-millisecond orders at one price could be queued in the opposite order
  of the venue's (id order; measured: 64 of 5,407 snapshots of the RD-12 capture differed). New orders at one side
  and price within one timestamp are now ranked by order id (`id_ties_reranked`).
- Its taker heuristic dropped an order that rested and was filled within `taker_window_us`; a trade naming the order
  as the maker now exempts it (`maker_in_taker_window`). The matcher's `ingest_suppressed` flag still describes the
  old behaviour.
- It seeded from the first `detail_order_book` frame per market even when that frame was ambiguous (rule 2). It now
  seeds from the first snapshot that no later-arriving message is stamped before, and sets `seed_ambiguous` when
  none qualifies. On the RD-12 capture the seed moves to the 12th snapshot (`docs/RESULTS.md`, RD-12).
