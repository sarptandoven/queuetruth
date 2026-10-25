# Agent sandbox (extension of SPEC 1.1, 3.10, 4.3 and 4.6)

Status: built and tested (`python/queuetruth/sandbox/`, `python/queuetruth/mcp/sandbox_tools.py`,
`python/tests/sandbox/`). This note is the contract the code was built against, written to be merged into
[the design specification](../spec.md) as a new section; the user guide is
[Give an agent a realistic exchange](../../how-to/agent-sandbox.md).

## S.1 Scope

A simulated exchange over one instrument-day of an MBO dataset (`kind = mbo`, FIFO rule sets) in which an agent's
orders are shadow orders of the exact engine (SPEC 5.2, fill rule `exact`). Out of scope, as for shadows (SPEC 1.2):
market impact, interaction between the agent's own orders, hidden liquidity beyond the hidden-print policy, auctions.
One instrument per episode.

## S.2 Clock, visible cut and the tape

- The clock starts at `start_ns` (default: the instrument row's open) and only increases, by `advance(by | to)`,
  capped at `end_ns` (default: close). The visible cut is `visible_ns = clock_ns - feed_latency_ns`. The agent acts
  only at the times it advances to, so its reaction time to a market event is its step plus `latency_ns`.
- **Cut rule.** The cut at `t` is the longest prefix, in feed order, of complete events (maximal runs of records
  ending with `F_LAST`, SPEC 5.1.2) whose records all have `ts_event <= t`; once every record of the partition has
  been read and all remaining records have `ts_event <= t`, the trailing records go too (a final unterminated event,
  which every replay ends synthetically). Cuts nest: `t1 <= t2` implies `cut(t1)` is a prefix of `cut(t2)`. The
  cursor reads until a record past `t` or the end of the data however far that is (the first cut of an afternoon
  episode on a liquid instrument spans millions of records); while more than 4,000,000 records are pending, all at or
  before `t`, it releases their complete events early, so memory holds one buffer plus the cut
  (`test_a_cut_larger_than_the_pending_cap_is_released_whole`).
- **Tape.** The source partition is read forward only (`SourceCursor`); each cut's new records are appended as a new
  `part-NNNNNN.parquet` of a scratch dataset (manifest listing the parts in order, `dataset_id` by the SPEC 3.10 rule,
  compaction every 32 parts), with the source prints (`trades_source`, needed for OC-28 cross prints) of
  `ts_event <= t` and a `data_quality.json` whose counters are zero and whose `feed_gaps` are the instrument's gaps
  with `ts_start < t`, each with `ts_end` clamped to `t` (and a neutral reason while it is in progress). The engine
  uses only a gap's start: it censors the live shadows at the first event after `ts_start`
  (`Engine::set_feed_gaps`), and a gap that no event after its start has reached does nothing. A prefix replay
  therefore meets every gap at the same event as the full replay, including gaps with records of the instrument
  inside them, which ingest writes (crypto warmup gaps from the first event to the warm time, Hyperliquid partitions
  without an L4 snapshot, sequence gaps spanning non-monotone times); `test_equivalence.py` checks a fill inside a
  gap and an order working inside one against independent prefix replays. No time past the cut enters a replay
  input. The scratch dataset is the only input of every replay run during the episode. Records past the cut stay in
  the cursor's private buffer. The guarantee is about the interface and the replay inputs: in-process Python can
  always read private attributes or the dataset itself, so an agent that must not see the future is driven through
  the MCP tools from another process.
- **Opening.** Every dataset file and directory is resolved to its real path, and in MCP mode checked against the
  server root, before it is read or listed (manifest, instruments, data quality, trades directory, partition files);
  the partition's files are later opened, and linked by the certification view, only through the real paths resolved
  at open, so a link changed during the session is never followed.
- **Market view.** `BookView` applies exactly the released records: displayed levels (SPEC 5.1.1 record semantics for
  totals and counts), the best-quote history in `bbo = change` form (one row per event end where any of the six
  values changed), and prints with the engine's kind rule. It equals the engine's `bbo.parquet`, `depth.parquet` and
  `trades.parquet` on every fixture and, with the day-one data, on the TSLA partition (678,438 records released at
  16 cuts: 126,458 best-quote rows, every print, top-10 depth at five cuts; `test_book.py`, marked `realdata`).

## S.3 Orders as shadows

| Instruction | `qt.shadow_spec` row | Notes |
|---|---|---|
| limit, day | `price_mode = abs`, `price` | |
| pegged limit, day | `price_mode = touch`, `offset_ticks` | resolved at activation; `rejected_no_touch`, `rejected_would_cross` |
| limit IOC, market | `abs` at the limit or collar price, `ts_cancel = ts_submit + 1 ns` | only `crossed_at_live` fills count; the order is final at activation |
| cancel at `c0` | `ts_cancel = c0` | effective at `c0 + latency_ns` |
| partial cancel of `k` at `c0` | `qt.shadow_amend (ts_send = c0, new_qty = qty - k)` | priority kept; `new_qty <= filled` ends `amended_to_filled` |

`ts_submit` is the clock at the decision; `latency_ns = spec.latency_ns + (stable_seed(seed, ["sandbox_latency",
order_id]) mod (jitter_ns + 1))`, so submit and cancel delays of one order are equal and every arrival time is known to
the sandbox. Order ids are `o1, o2, ...` in submission order. Market orders are priced at decision time from the
visible opposite best: `best +- max(1, ceil(|best| * market_collar_bps / 10^4 / tick)) * tick` (a sell collar of a
positive price is floored at one tick); no visible opposite quote rejects the order.

Checks at submit (rejections are `QT-CONFIG-001` with `details.reason`, or `QT-INPUT-004` for a price finer than
`price_exp`, and are logged): side, type, tif, `qty > 0` and a multiple of `lot_size`, price on the tick grid, peg only
on day orders, `max_order_qty`, `max_open_orders`, `max_position` (worst case `position + open quantity on the order's
side + qty`), and self-trade prevention `reject_new`: the new order is rejected when its price (limit, collar, or the
peg estimate from the visible touch) would cross an open opposite order that can still be there when it arrives, i.e.
not an IOC arriving at or before it and not an order whose cancel is effective at or before it. A repeated
`client_order_id` with the same normalized request returns the original ack (`duplicate = true`); with another
request it is `QT-INPUT-003`. Arguments are first put in their logged JSON form (`wire`: integers, strings, `None` and
finite floats as they are, numpy numbers as `int` or `float`, a `Decimal` as its string, a non-finite float as
`"nan"` / `"inf"`), and that form is what is validated and logged, so a float size or price is refused as such and
re-executing the logged request meets the same rejection with the same message.

## S.4 Steps and status

At each step the sandbox extends the tape to the new visible cut and, when some order is open and the tape gained
records since the last window replay, replays the scratch dataset with every open order (`bbo none`, `trajectory
ahead`; ended orders keep their final state: shadows are independent). Without new records nothing can change: an
instruction sent since the last replay takes effect after its cut. Mapping of a replay's `end_state` (window replay / final replay):

| `end_state` | Status |
|---|---|
| `filled`, `amended_to_filled` | `filled` (reason `reduced_to_filled` for the latter) |
| `cancelled` | `cancelled` (`agent_cancel`, `finish_cancel`) |
| `rejected_would_cross`, `rejected_no_touch` | `rejected` |
| `censored_clear`, `censored_gap` | `censored` (`book_clear`, `feed_gap`) |
| `open_at_end`, `censored_session_end` | window replay: `pending` (not activated) or `working`; final replay: `expired` |

IOC and market orders are final once activated: `filled` when the arrival fills cover the quantity, else `cancelled`
(`ioc_remainder`, `no_liquidity`). Queue ahead and rank of a working order are the last `queue_trajectory` row's
`*_after`; `level_qty` is the displayed size at its price in the market view.

**Fill visibility.** A fill is knowable at its event (`ts_event`), except an arrival fill (`crossed_at_live`), which
is stamped `ts_live` but determined at the activation event (`ts_activated_event`). A step reports the fills of its
window replay not reported before; a reported fill that a later window replay changes or drops is `QT-ENGINE-001`
(prefix stability).

**End visibility.** An order's end is decided at an event: its last fill's event (or its activation event for an
arrival fill), the `R` record of a book clear, its activation event for a rejection or an IOC; and, for a cancel, a
reduction to the filled size or a feed gap, the first event after the effective time (SPEC 5.2.1 agenda). The
session shows the order ended at the first step whose cut covers that event.

**Equivalence.** Instructions made at a step take effect after its cut, and cuts nest, so the window replay of step
`k` equals the replay of `cut(visible_k)` with every order of the whole episode (`test_equivalence.py` checks this
against independently cut prefixes), and the fills and order states reported up to step `k` are exactly those of one
full replay whose knowable time is at or before `visible_k`, attributed to the first step whose cut covers it.
`finish` checks the second statement at run time (`Consistency`: every reported fill against the full replay's rows,
and every order's first working step, ended step and end against the full replay; exact when timestamps never
decrease and each event's records share one timestamp, which the tape counts).

**Wake-ups.** A step ends at the time the agent asks for. Waking at the next market event, or at the agent's own
fill, would be causal (the wake time depends only on data up to it, and real systems are woken by exactly those
messages), but while every step costs a replay of the whole prefix, event-driven steps would cost a replay per event.
A later `advance(until="event", by=cap)` can wake at the next released event cheaply once the engine accepts shadow
orders into a running replay.

## S.5 Finish and scorecard

`finish` sends a cancel at the clock for every open day order, builds a single-partition view of the original files
(symlinks), replays it once with every committed order (`bbo change`), and computes:

- **PnL**, exactly (integer prices and sizes, `Decimal` money): `cash = -sum(d q p) 10^(price_exp + size_exp) m`;
  `pnl_gross = cash + position * mark` with the mark the mid of the last best quote at or before `mark_ns =
  max(finish, the latest order end)`, else the last print; a flat position needs no mark. Fees per fill:
  `q_real * per_unit + q_real * p_real * m * bps / 10^4` by liquidity (positive = cost). `pnl_net = pnl_gross - fees`;
  `pnl_net_at_touch` marks a long position at the bid and a short one at the ask. During the session the position
  shown is a running account booked once per reported fill.
- **Markouts** with `queuetruth.score.markouts.compute_markouts` (mid, SPEC 5.7.5; null past the data's last event)
  on the final replay's quotes; per horizon and liquidity: counts, mean ticks and bps, total currency, adverse count.
  In-session markouts use the same formula with `last_event_ts = visible_ns` and are bitwise equal; they wait in a
  heap keyed by due time, so a step costs the markouts it reports.
- **Adverse selection** per horizon: `cost_ccy = -sum(maker mo_ccy)`, mean maker ticks, share of negative markouts.
- **Fill rates** per class (`passive_limit`, `marketable_limit` = a day limit with an arrival fill, `ioc_limit`,
  `market`, `all`) over the orders that reached the venue (activated, not rejected); median time to first fill of
  passive orders.
- **Execution cost**: `sum d (mid_decision - P) q` with the visible mid at submit, by liquidity.
- **Diagnostics**: same-price overlaps of working day orders, arrival fills of several orders in one event on one
  side, crossing working pairs (a sweep in start order, equal to comparing every pair), out-of-session orders,
  rejected instructions.

## S.6 Documents, identities and the log

Documents (Pydantic models in `queuetruth.sandbox.models`, JSON Schemas from `json_schemas()`):
`qt.sandbox_session`, `qt.sandbox_observation`, `qt.sandbox_ack`, `qt.sandbox_scorecard`, `qt.sandbox_order_log`,
`qt.sandbox_verification`, and for the MCP tools `qt.sandbox_finish` and `qt.sandbox_session_list`; spec
`qt.sandbox_spec`. Conventions of SPEC 0.1 and 3.10 (tag first, unknown fields rejected, integer ns, raw fixed-point
prices and sizes); money and `avg_fill_price` / `mark_price` / `vwap_price` are floats.

- `spec_sha256` = `sha256(canonical_json({engine_version, spec}))` with `spec.dataset` replaced by
  `{"sha256": dataset_id}` (SPEC 7.3); `spec_id` = its first 16 hex. `run_id` = the first 16 hex of the same digest
  with `log_root` added.
- **Log.** Entries `{seq, kind, clock_ns, visible_ns, rows_seen, data, prev, hash}`, `hash = sha256(canonical_json(entry
  without hash))`, `prev` of the first = `sha256("queuetruth.sandbox.order_log.v1")`; kinds `open` (engine version,
  dataset id, spec id, `spec_sha256`, instrument-day, window, first observation digest: the root commits to every
  setting of the episode), `submit` (request and derived order), `cancel` (`by` agent or finish), `reject` (request in
  its validated form, code, message), `advance` (`to_ns`, `observation_sha256`), `finish` (`final_sha256` of the final
  orders and fills).
- `certificate.scorecard_sha256` covers the scorecard without `certificate`, `timings` and `files`.
- `verify(log, dataset=, scorecard=)`: the chain; the header (`engine_version` is the running engine's, the header's
  engine version, dataset id and spec id are those the `open` entry commits to, and the logged spec gives the logged
  spec id on the dataset: `header_ok`); the dataset (`dataset_id`, and the sha256 of every partition file against its
  manifest: `dataset_ok`); then re-execution of every entry in a fresh sandbox (each must reproduce with the same
  hash), then the scorecard's recomputed digest, claimed digest, log root and run id.

## S.7 MCP tools

Registered on a `QtMcpServer` by `register_sandbox_tools(app, root=None, max_sessions=4, workdir=None, out_dir=None,
idle_timeout_s=3600)`, listed after the server's tools: `qt_sandbox_start` -> `qt.sandbox_session`,
`qt_sandbox_observe` -> `qt.sandbox_observation` (read-only, idempotent), `qt_sandbox_submit` and `qt_sandbox_cancel`
-> `qt.sandbox_ack`, `qt_sandbox_advance` -> `qt.sandbox_observation`, `qt_sandbox_finish` -> `qt.sandbox_finish`,
`qt_sandbox_list` -> `qt.sandbox_session_list` (read-only), `qt_sandbox_close` -> `qt.sandbox_session_list`
(destructive: no scorecard).

- `qt.sandbox_finish` is the scorecard without its `orders` and `fills` arrays (with their counts and the
  certificate), so its size does not grow with the episode; `detail = "full"` adds the whole scorecard, `include_log`
  (default false) the order log as an embedded `application/json` resource. With an output directory (`out_dir`,
  `--out`, which must lie outside the root), `finish` writes the scorecard, the order log, the markouts and the
  replay tables to `<out_dir>/<session_id>/`, lists them in `files` and links `scorecard.json` and `order_log.json` as
  `resource_link` items.
- Datasets resolve under the server root with the local tools' containment (S.2 Opening; else `QT-CONFIG-001`);
  scratch files go to a private temporary directory; sessions are bounded (`QT-USAGE-001` beyond `max_sessions`);
  when the limit is reached, a start first evicts the sessions idle for longer than `idle_timeout_s`; a `finish`
  that fails closes its session; every session is closed at exit. Duration and time arguments are strict: an integer
  is ns, a string needs a unit or `HH:MM`.

## S.8 Integration (hub edits left to the integrator)

- Done at integration: the eight documents are registered in `queuetruth.schemas.models.DOC_MODELS` and
  `qt.sandbox_spec` in `SPEC_MODELS`, with their names in `PYTHON_ONLY` in `crates/schema/src/tests.rs`.
- `queuetruth mcp serve --sandbox [--sandbox-out DIR]`: a flag in `crates/cli/src/grammar.rs` and
  `python/queuetruth/cli.py` that calls `register_sandbox_tools(app, out_dir=...)`; until then
  `python -m queuetruth.mcp.sandbox_tools` serves them. `QtMcpServer` could grow an extension point for extra tools
  with their own output schemas (registration currently overrides the instance's `tool_list`).
- `queuetruth sandbox verify ORDER_LOG [--dataset DIR] [--scorecard FILE]`: the certification entry point for third
  parties, printing `qt.sandbox_verification` as JSON and exiting non-zero when `ok` is false (grammar plus
  `cli.py`, calling `queuetruth.sandbox.verify`); optionally `queuetruth sandbox run --spec FILE` for scripted agents.
- A cross-link from `how-to/use-from-an-agent.md` to the sandbox how-to; add `how-to/agent-sandbox.md` to
  `docs/mkdocs.yml` and this note to the specification.

## S.9 Measurements

On the TSLA partition of Nasdaq 2019-12-30 (678,438 records), a touch-quoting agent from 10:00 to 10:30 ET with 5 s
steps: 33.2 s for the episode, of which 360 window replays took 24.1 s (67 ms each, over prefixes of 169,195 to
233,747 records), the tape 3.7 s and the market view 0.55 s; finish 0.4 s; `verify` 34.5 s (shared laptop, Low Power
Mode; an earlier run under heavier load measured 114 ms per window replay and 64.1 s for `verify`). Every one of the
537 reported fills and every one of the 511 orders equals the single full replay; `test_realdata_demo.py` asserts
the scorecard. The `qt.sandbox_finish` summary of that episode is 6,583 bytes of JSON; its full scorecard is 341,629
and its order log (1,152 entries) 471,554. The per-step cost is the replay of the prefix: linear in the prefix,
quadratic over an episode.

## S.10 What the guarantee does not cover

No look-ahead holds at the interface: nothing the session returns, and no replay it runs, depends on data past the
visible cut. It says nothing about what the agent knew before the episode. The session document names the symbol,
venue and date, and prices are absolute, so a language model whose training data covers that day may recall its
price path; earlier episodes on the same day, or tuning across episodes, leak the same way. The certificate and
`verify` prove that the scorecard follows from the logged instructions on the logged data, not that the instructions
were made without such knowledge. Claims about language-model agents need data after the model's training cutoff or
a held-out day. A blind mode would have to hide the instrument-day from the agent entirely: an operator-pinned
episode the agent starts without naming the dataset, symbol or date, times relative to the session, and prices
rebased; hiding the symbol and date alone would leave the absolute prices, and in MCP the agent itself names the
dataset path, symbol and date when it calls `qt_sandbox_start`.
