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
- **Tape.** The source partition is read forward only (`SourceCursor`); with `replay` stepping (S.4) each cut's new
  records are appended as a new
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
  input. With `replay` stepping the scratch dataset is the only input of every replay run during the episode. Records
  past the cut stay in the cursor's private buffer.
- **The guarantee, for both steppings.** Nothing the session returns depends on data past the visible cut. With
  `replay` stepping this holds physically: every replay during the episode reads the scratch dataset. With `session`
  stepping (S.4) there is no scratch dataset: the one-pass session opens the original files of the partition through
  the certification view and holds the day's feed gaps and trading halts, but its engine applies only the whole events
  of the cut (`Session::advance_events_to`, the cut rule above), acts on a gap or halt only at an event it applied, and
  every field the sandbox reads from it (`Session.shadow_states`, `take_fills`) is fixed when the order is sent or
  comes from those events; the one rule that reads a halt without an event (a pending order arriving in a halt) uses
  the halts as the tape lists them at the cut. `test_stepping.py` checks the session engine's book against the
  released records at every step, and the look-ahead tests run on both steppings. Either way the guarantee is about
  the interface: in-process Python can always read private attributes or the dataset itself, so an agent that must
  not see the future is driven through the MCP tools from another process.
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

**Session stepping (the default).** The window replays above are what `stepping="replay"` does, and what defines
every step. `stepping="session"` computes the same thing in one pass: a `qt.Session.with_orders` opened on the
certification view (the episode's partition alone, its feed gaps and trading halts, the seed and hidden-print policy
of every replay) receives each submit (its `qt.shadow_spec` row, an IOC's cancel at `ts_submit + 1` included), cancel
and partial cancel (an amend) at the agent's clock, and is advanced at every step with `Session.advance_events_to`
(the cut rule above, implemented in the engine: whole events, the unterminated tail ended once the data ends) while
the tape releases the same records to the market view without writing a scratch dataset. Under the same condition
as a window replay (an order open and records new since the last one), `Session.shadow_states` gives each open order's
state; `_window_view` turns it into the `shadow_results` row and last `queue_trajectory` row the window replay would
give (an order not ended is `open_at_end` with its queue ahead and rank, except a pending order whose `ts_live` falls in
a trading halt as the tape lists them at the cut, which the end of a prefix replay ends `censored_gap` at `ts_live`),
and the same `absorb` maps them. `window_replays` counts these reads. The finish cancels are not sent to the session;
the certification replay decides every end as before. `test_stepping.py` runs seeded random agents and scripted edge
cases (a halt, an event straddling the cut, an unterminated tail, orders after the data, a book clear, a feed gap)
through both steppings and compares every document, the log and the scorecard; `crates/replay/tests/incremental.rs`
checks `advance_events_to` and `shadow_peek` against one batch run.

**Wake-ups.** A step ends at the time the agent asks for. Waking at the next market event, or at the agent's own
fill, would be causal (the wake time depends only on data up to it, and real systems are woken by exactly those
messages). With the session stepping a step no longer costs a replay of the prefix, so an
`advance(until="event", by=cap)` that wakes at the next released event is cheap; it is not built yet.

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
`qt.sandbox_verification`, and for the MCP tools `qt.sandbox_finish` and `qt.sandbox_session_list`; for blind
episodes (S.11) `qt.sandbox_blind_session`, `qt.sandbox_blind_finish`, `qt.sandbox_blind_session_list`,
`qt.sandbox_blind_order_log` and the operator's `qt.sandbox_episodes`; spec `qt.sandbox_spec`. Conventions of SPEC 0.1 and 3.10 (tag first, unknown fields rejected, integer ns, raw fixed-point
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
steps: 33.2 s for the episode, of which 360 window replays took 24.1 s (67 ms each, over prefixes of 169,195 to 233,747
records), the tape 3.7 s and the market view 0.55 s; finish 0.4 s; `verify` 34.5 s (shared laptop, Low Power Mode; an
earlier run under heavier load measured 114 ms per window replay and 64.1 s for `verify`). Every one of the 537 reported
fills and every one of the 511 orders equals the single full replay; `test_realdata_demo.py` asserts the scorecard. The
`qt.sandbox_finish` summary of that episode is 6,583 bytes of JSON; its full scorecard is 341,629 and its order log
(1,152 entries) 471,554. These numbers are for `replay` stepping, whose per-step cost is the replay of the prefix:
linear in the prefix, quadratic over an episode. Session stepping on the synthetic fixture day: a quoting agent in 5 s
steps took 1.7 to 1.9 ms per step at each episode length measured (250 to 4,000 steps, median of 3 runs; 7.7 s for
4,000 steps with 2,478 orders), against 30 to 35 ms per step with `replay` stepping (139.1 s), with the same log root
and scorecard (`benches/sandbox_stepping.py`).

## S.10 What the guarantee does not cover

No look-ahead holds at the interface: nothing the session returns, and no replay it runs, depends on data past the
visible cut. It says nothing about what the agent knew before the episode. The session document names the symbol,
venue and date, and prices are absolute, so a language model whose training data covers that day may recall its
price path; earlier episodes on the same day, or tuning across episodes, leak the same way. The certificate and
`verify` prove that the scorecard follows from the logged instructions on the logged data, not that the instructions
were made without such knowledge. Claims about language-model agents need data after the model's training cutoff or
a held-out day, or a blind episode (S.11), which hides the instrument-day from the agent.

## S.11 Blind episodes

`queuetruth.sandbox.blind`; tests in `python/tests/sandbox/test_blind.py`.

- **Pinning.** `EpisodeBook.pin(spec)` validates an ordinary `qt.sandbox_spec` against the dataset, stores it with
  absolute times and a fresh 32-byte secret, and returns an opaque id `ep_<16 hex>`. A spec whose fees have a
  basis-point part is refused (`QT-CONFIG-001`): a fee proportional to the notional shows the price level. The book
  is a `qt.sandbox_episodes` document written with mode 0600. `start(episode_id)` opens the episode; an unknown id
  is `QT-USAGE-001` and names no pinned episode.
- **Transform.** `time_origin_ns` = the instrument row's open. `price_offset_ticks = base - floor(reference / tick)`,
  where `reference` is the best bid in view at the start (else the best ask, else the last print; none of them is
  `QT-CONFIG-001` at start) and `base = 1000 + (HMAC-SHA256(secret, "price_base") mod 9001)`. Both depend only on the
  secret and the data up to the first cut, so no look-ahead enters the transform. `salt = HMAC-SHA256(secret,
  "salt")`. `transform_sha256 = sha256(canonical_json({kind, time_origin_ns, price_offset_ticks, tick_size,
  price_exp, salt}))` commits to the transform and reveals nothing without the salt.
- **Agent-visible values.** A `BlindSandbox` wraps a `Sandbox` and maps its documents: times `t - time_origin_ns`,
  `time_local` as `+HH:MM:SS.ffffff`; raw prices `p + price_offset_ticks * tick`; float prices
  (`avg_fill_price`, `mark_price`, `vwap_price`) by the same amount in decimals; `cash` and `notional_traded`
  recomputed at the rebased prices; `mo_bps` and `mean_bps` null; a market order's collar price null. Sizes, money
  that does not depend on the price level and every count are unchanged. `qt.sandbox_blind_session` replaces the
  instrument by `BlindInstrument` (tick, lot, exponents, multiplier, currency) and the spec by its simulation
  settings. Acks keep their `log_seq` and name the entry of the agent's own log (`qt.sandbox_blind_order_log`: one
  entry per entry of the operator's log, same `seq`, blind times, prices and requests, `rows_seen` counted from the
  start, its own genesis `sha256("queuetruth.sandbox.blind_order_log.v1")`).
- **Instructions back.** A price the agent sends (decimal string or raw integer) is parsed at `price_exp` and moved
  back by the offset; text that does not parse is passed on as it is, so the sandbox meets and logs the same
  rejection. `advance(to=)` takes ns or `HH:MM[:SS]` since the open. The operator's sandbox therefore receives
  exactly what a non-blind agent sending the true values would send: its log entries equal theirs except the `open`
  entry, and its scorecard equals theirs except `run_id`, `certificate`, `timings` and `files` (tested).
- **Errors.** A `QtError` of the operator's sandbox has the true prices the call passed replaced by the agent's;
  then, if its message or hint still contains a hidden string (symbol, venue, date, dataset path or id, spec id), a
  time of day or an integer of 15 or more digits, it is replaced by a generic message. Only `details.reason` and
  `details.order_id` are kept. A blind MCP server reports an internal error by its code alone.
- **Scoring and certificate.** `finish` returns `qt.sandbox_blind_finish` to the agent and keeps the operator's
  `qt.sandbox_scorecard` (true values) in `scorecard`; with `out` it writes the usual files and `blind.json` (the
  episode id and the revealed transform). `Sandbox.open(..., blind=hook)` puts `{episode_id, transform_sha256}` in
  the `open` entry, so the log root commits to it, and `certify` copies it to `certificate.blind`. `verify` passes
  the logged commitment to the re-execution and checks the certificate's `blind` against it.
- **MCP.** `register_sandbox_tools(app, episodes=book)` (`--episodes FILE`) removes the server's own tools, replaces
  its instructions, and serves the eight sandbox tools with blind arguments (`qt_sandbox_start {episode_id}`,
  `qt_sandbox_advance` with `to` since the open, `qt_sandbox_finish {session_id, include_log}`) and blind outputs.
  The operator's files under `out_dir` are not linked; `include_log` embeds the blind log.
- **Not hidden.** Tick size, lot size, multiplier, currency, sizes, volumes, the session length and the shape of
  the price path in ticks. They narrow the instrument class and may let a very distinctive day be recognised. A
  market sweep that stops at the collar bounds the price level.
