# Give an agent a realistic exchange

**Goal:** let an AI agent (or your own code) trade over a replayed day of order-level data, with its orders filling
exactly as the real queue would have filled them, with order-entry latency and without being shown anything past its
clock; then get a scorecard (PnL after fees, fill rates, adverse selection) and a committed order log anyone can
re-execute. The sandbox controls what the agent is shown, not what it already knows: see
[What the guarantee does not cover](#what-the-guarantee-does-not-cover).

**You need:** an MBO dataset (the synthetic fixture `queuetruth fixtures synth` writes is enough to try it), and for
an MCP agent the MCP extra: `pip install "queuetruth[mcp]"`.

The sandbox is `queuetruth.sandbox.Sandbox`; the MCP tools are in `queuetruth.mcp.sandbox_tools`. The design,
including what is exact and what is not, is in [the sandbox design note](../design/extensions/agent-sandbox.md).

## How it works

- **Your orders are shadow orders.** Each order is placed in the recorded FIFO queue of its price level by the exact
  engine, with the fill rule described in [Shadow orders](../concepts/shadow-orders.md): it fills when the orders
  ahead of it are gone and volume trades at its price, or when the market trades through it; an order that is
  marketable on arrival takes the displayed liquidity at or through its price.
- **Your clock moves only forward.** It starts at the episode start and moves with `advance`. You see the market and
  your own orders up to `clock - feed_latency_ns` (the visible cut). Every order and cancel is stamped with your clock
  and takes effect `latency_ns` (plus a deterministic jitter draw) later.
- **You act only at the times you advance to.** Nothing wakes you in between, so your reaction time to a market event
  is your step plus `latency_ns`: about 5 s in the real-data episode below, not its 1 ms of latency.
- **No look-ahead, by construction.** The sandbox copies the recorded events up to the visible cut, and only up to
  it, into a private scratch dataset, and every replay it runs during the episode reads that copy. Records after the
  cut wait in the reader's private buffer and reach neither the copy nor anything you receive.
- **Decide, then replay.** The engine replays whole datasets and its interactive session carries no shadow orders,
  so each `advance` re-runs the exact replay of the episode's prefix with your open orders. Instructions take effect
  after the cut they were made at and the prefixes nest, so each step equals the same prefix of one full replay.
  `finish` runs that one full replay on the original files and checks, fill by fill and order by order, that it equals
  what you were told during the episode.

## Run an episode

On the synthetic fixture (instrument 1 on 2026-06-15, a CME-style book with a 0.25 tick and a multiplier of 50):

<!-- check-docs: run -->
```python
import os

from queuetruth.sandbox import Sandbox

spec = {
    "dataset": {"path": os.path.join(os.environ["QT_FIXTURES"], "synth_small")},
    "instrument_id": 1,
    "session_date": "2026-06-15",
    "start": "13:35",
    "end": "13:50",
    "latency": "1ms",
    "fees": {"maker_per_unit": -0.2, "taker_per_unit": 0.3},
}
with Sandbox.open(spec) as sb:
    obs = sb.observe()
    print(obs.time_local, "bid", obs.book.bid, "ask", obs.book.ask)
    sb.submit("buy", 2, price="5001.00")  # a day limit order at the best bid
    sb.submit("sell", 1, peg_offset_ticks=-1)  # one tick behind the best ask at arrival
    for _ in range(12):
        obs = sb.advance(by="10s")
        for f in obs.fills:
            print(obs.time_local, f.order_id, f.side, f.qty, sb.to_price(f.price), f.liquidity, f.reason)
    card = sb.finish(out="episode")
print("PnL net", card.pnl.pnl_net, card.pnl.currency, "| equal to one full replay:", card.consistency.equal)
```

`Sandbox.open` takes a `SandboxSpec` or a dict. Times are absolute ns or local times of the session day
(`"13:35"`, `"10:30:05.250"`); durations are ns or strings with a unit (`"250us"`, `"1ms"`, `"5s"`; a bare `"5"` is
refused). Prices in documents are integers in fixed-point units; `sb.to_price(raw)` gives the exact decimal price and
`sb.to_raw("5001.25")` the integer. The main settings:

| Setting | Default | Meaning |
|---|---|---|
| `instrument_id` or `symbol`, `session_date` | | the instrument-day; `session_date` only when the dataset has several days |
| `start`, `end` | session open, close | the episode window |
| `latency`, `jitter` | 0, 0 | order-entry delay of every submit and cancel, plus a uniform draw in `[0, jitter]` per order |
| `feed_latency` | 0 | you see the market and your orders this much late |
| `depth_levels`, `max_trades` | 5, 20 | levels per side and prints listed per observation |
| `fees` | zero | `maker_per_unit`, `taker_per_unit` (currency per unit) and `maker_bps`, `taker_bps`; positive = cost |
| `markout_horizons` | 1 s, 10 s, 60 s | markout horizons |
| `max_order_qty`, `max_position`, `max_open_orders` | none, none, 100 | risk checks at submit |
| `self_trade_prevention` | `reject_new` | reject an order that could trade with one of your own |
| `hidden_print_policy` | venue default | the engine's hidden-print rule (SPEC 5.2.4a) |

## What an observation holds

`observe()` and `advance()` return a `qt.sandbox_observation`. Prices and sizes are integers in the instrument's
fixed-point units (the session document gives `price_exp`, `size_exp` and the tick); money is in currency.

| Field | Content |
|---|---|
| `clock_ns`, `visible_ns`, `time_local`, `done` | your clock, the visible cut, the local time, whether the episode end is reached |
| `book` | best bid and ask and the top levels as `[price, size, order count]` at the visible cut |
| `trades`, `trades_since_last`, `session_trades` | the latest prints, and their statistics (count, volume, `vwap_price`, high, low, last) since the last step and since the open |
| `orders` | your open orders, and those that ended this step: status, filled and leaves quantity, queue ahead, rank and the displayed size at your price |
| `fills` | your fills reported this step: time, price, size, maker or taker, reason, queue ahead before, fee |
| `markouts` | markouts that became computable this step (their horizon is visible) |
| `position` | position, cash, fees, the mark (mid at the cut) and PnL gross and net |

An order is `pending` until the first market event after its arrival confirms it (the engine's strict activation),
even when it is marketable; then `working`, `filled`, `cancelled`, `rejected` (a pegged order without a touch or
that would cross), `expired` (open when the data ends) or `censored` (book clear or feed gap).

## Orders

| Instruction | How |
|---|---|
| Limit order (day) | `submit("buy", 2, price="5001.00")`: rests; marketable on arrival takes liquidity first |
| Pegged limit order | `submit("sell", 1, peg_offset_ticks=0)`: priced against the same-side best **at arrival** (0 joins it, -1 one tick behind, +1 one tick inside) |
| IOC limit order | `submit("buy", 5, price="5001.25", tif="ioc")`: only what is displayed at or through the price on arrival; the rest is cancelled |
| Market order | `submit("buy", 5, type="market")`: an IOC limit order at a collar `market_collar_bps` (default 100) through the opposite best you saw |
| Cancel | `cancel("o3")`: takes effect after the order's latency; fills until then stand |
| Partial cancel | `cancel("o3", qty=2)`: reduces the order and keeps its queue priority |
| Idempotent retry | `client_order_id="..."`: the same id with the same request returns the original ack |

A rejected order raises `QtError` and is logged: `QT-CONFIG-001` for an invalid order (a price off the tick grid, a
size off the lot size) or a failed risk check, with `details.reason` (`max_order_qty`, `max_position`,
`max_open_orders`, `self_trade`, `no_quote`), and `QT-INPUT-004` for a price or size finer than the instrument's
fixed-point units. A float price or size (`price=5000.0`, `qty=2.0`) is refused: give a decimal string or an integer.
Self-trade prevention compares arrival times: an IOC that arrives no later than the new order, or a cancel effective
by then, cannot trade with it.

## The scorecard

`finish()` cancels your open orders at the current clock, runs one exact replay of every order you sent on the
original files and returns a `qt.sandbox_scorecard` (also written to `out/scorecard.json`, with `order_log.json`,
`markouts.parquet` and the replay's tables under `out/replay/`):

- `pnl`: cash of every fill (also fills while the final cancels were in flight), the end position marked at the mid,
  fees by liquidity, `pnl_net`, and `pnl_net_at_touch` (a long position marked at the bid, a short one at the ask);
- `fill_rates` per order class (passive limit, marketable limit, IOC, market) over the orders that reached the venue,
  with the median time to first fill of passive orders;
- `markouts` (mid reference, the scorer's definition) by horizon and liquidity, and `adverse_selection` = minus the
  maker markouts, with the share of maker fills the price moved against;
- `execution_cost`: each fill against the mid you saw when you sent the order;
- `consistency`: every fill reported during the episode is a row of the single full replay (all columns), every
  fill of that replay knowable by the last visible cut was reported, at the right step, and every order was shown
  working and shown ended at the steps the full replay implies, with its end (`statuses_mismatched`); `equal` is the
  verdict;
- `diagnostics`: your orders never see each other, so the scorecard counts same-price overlaps, taker fills that
  took the same displayed size in one event, and crossing pairs;
- `certificate`: the dataset id, the engine version, the spec id, the root of the committed log, the run id and the
  scorecard's sha256.

## Certify an episode

The committed log hash-chains every instruction with the information it was made on (the visible cut and the number
of records seen) and the sha256 of every observation you received; its first entry commits to the engine version, the
dataset and every setting of the episode (latency, fees, seed), so a published root commits to all of them. `verify`
checks the chain, that the header is the one the first entry commits to, and the dataset (its id and the sha256 of
every partition file), then re-executes the log on the dataset: every entry must reproduce with the same hash,
rejected instructions included.

<!-- check-docs: run -->
```python
from queuetruth.sandbox import verify

v = verify("episode/order_log.json", scorecard="episode/scorecard.json")
print("verified:", v.ok, "run", v.run_id, "problems", v.problems)
```

Editing an entry breaks the chain; recomputing every hash after an edit (a backdated cancel, a faster latency, a
different observation, a cheaper fee schedule in the header) fails the re-execution at that entry; data files changed
under an unchanged manifest fail the dataset check; a scorecard whose numbers were changed fails the digest
(`python/tests/sandbox/test_log.py`). A verified log proves that the scorecard follows from the logged instructions
on that data. It does not prove that the instructions were made without knowing the day beforehand (see
[What the guarantee does not cover](#what-the-guarantee-does-not-cover)).

## From an MCP agent

`register_sandbox_tools(app)` adds eight tools to a `queuetruth.mcp.server` app: `qt_sandbox_start`,
`qt_sandbox_observe` (read-only), `qt_sandbox_submit`, `qt_sandbox_cancel`, `qt_sandbox_advance`,
`qt_sandbox_finish`, `qt_sandbox_list` (read-only: the open sessions, for an agent that lost its session id) and
`qt_sandbox_close` (ends a session without a scorecard). Datasets are resolved under `QT_MCP_ROOT` like the local
tools, every file is checked before it is read, symlinks leaving the root are refused, and the tools write nothing
under it. Each result carries the document as `structuredContent` and a plain-text rendering with decimal prices for
agents that read text.

`qt_sandbox_finish` returns a `qt.sandbox_finish` summary: every aggregate of the scorecard and its certificate,
without the per-order and per-fill arrays, so it stays a few kilobytes however long the episode (6.6 KB for the
real-data episode below, whose full scorecard is 342 KB and order log 472 KB). `detail="full"` adds the whole
scorecard and `include_log=true` the order log. Give the server an output directory (`--out`, outside the root) and
every finished session's scorecard, order log and replay tables are written to `<out>/<session_id>/` and linked from
the result, ready for `verify`. Four sessions may be open at once (`--max-sessions`); when they are, starting
another first closes those idle for more than an hour (`--idle-timeout`).

`queuetruth mcp serve --allow-run` also lists the sandbox tools (they run replays, so plain `mcp serve` stays
read-only and does not). For the sandbox's own options (`--out`, `--max-sessions`, `--idle-timeout`), configure your
MCP client (see [Use queuetruth from an agent](use-from-an-agent.md)) to start the sandbox server instead:

```json
{
  "mcpServers": {
    "queuetruth-sandbox": {
      "command": "python",
      "args": ["-m", "queuetruth.mcp.sandbox_tools", "--out", "/home/me/research/sandbox-runs"],
      "env": {"QT_MCP_ROOT": "/home/me/research/datasets"}
    }
  }
}
```

Use the Python of the environment queuetruth is installed in. From a shell:

```sh
QT_MCP_ROOT=/path/to/datasets python -m queuetruth.mcp.sandbox_tools --out /path/to/sandbox-runs
```

An agent's exchange, in-process:

<!-- check-docs: run -->
```python
import os

import anyio
from mcp import Client

from queuetruth.mcp.sandbox_tools import register_sandbox_tools
from queuetruth.mcp.server import build_server

app = build_server(local=True, root=os.environ["QT_FIXTURES"])
register_sandbox_tools(app)


async def main() -> None:
    async with Client(app.server) as c:
        args = {"dataset_path": "synth_small", "instrument_id": 1, "session_date": "2026-06-15"}
        start = await c.call_tool("qt_sandbox_start", {**args, "start": "13:35", "end": "13:40", "latency": "1ms"})
        sid = start.structured_content["session_id"]
        ack = await c.call_tool("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 2, "price": "5001.00"})
        print(ack.content[1].text)
        obs = await c.call_tool("qt_sandbox_advance", {"session_id": sid, "by": "30s"})
        print(obs.content[1].text)
        fin = await c.call_tool("qt_sandbox_finish", {"session_id": sid})
        print(fin.content[1].text.splitlines()[0])
        print("certificate", fin.structured_content["certificate"]["log_root"][:16])


anyio.run(main)
```

A rejected order or a bad argument is a tool result with `isError` true and a `qt.error` document
(`python/tests/sandbox/test_mcp_sandbox.py`).

## A real-data episode

A naive market maker on TSLA, Nasdaq 2019-12-30 (the public ITCH day of [Results](../RESULTS.md)), 10:00 to 10:30
ET: every 5 s it joins the best bid and the best ask with 100-share pegged day orders, cancels a quote more than two
ticks off the touch, and stops quoting the side that would take its position past 300 shares. Latency 1 ms plus up
to 0.5 ms of jitter; illustrative maker-taker fees of -0.0020 USD (rebate) and 0.0030 USD per share. The agent is
`quote_at_touch` in `python/tests/sandbox/test_realdata_demo.py`, which reruns this episode and asserts every
scorecard number below as printed (the scorecard is deterministic; the wall times are measurements).

| | |
|---|---|
| Steps, window replays | 360, 360 |
| Orders, orders with a fill | 511, 260 (50.9%); median time to first fill 2.46 s |
| Fills | 537, all maker; bought 11,903, sold 12,207 shares; end position -304 |
| Edge against the mid at decision | +2,087.045 USD (8.9 ticks per fill on average) |
| Adverse selection of maker fills | 1,084.98 USD at 1 s (64% of fills adverse), 1,459.055 USD at 10 s, 1,058.40 USD at 60 s |
| PnL gross, fees, net | -965.67 USD, -48.22 USD (rebates), -917.45 USD (-943.29 USD marking the short at the ask) |
| Consistency with one full replay | equal: 537 of 537 reported fills matched, none unreported or misattributed; 511 of 511 orders shown in the states and at the steps the full replay implies |
| Wall time (shared laptop, Low Power Mode) | 33.2 s for the episode, of which 24.1 s in the 360 window replays (67 ms each); finish 0.4 s; `verify` 34.5 s |

The agent's fills were 8.9 ticks better than the mid it saw on average, and by the end of the episode the mid had
moved against them by 3,052.715 USD, more than that edge. One 30-minute episode of a naive agent is a demonstration
of the mechanics, not evidence about any strategy. Day-one data is read-only under `.realdata/` and is never copied
into the repository; to rerun:

<!-- check-docs: skip (needs the day-one dataset under .realdata) -->
```sh
QT_REALDATA_DS=.realdata/ds/nasdaq-2019-12-30 python -m pytest -m realdata python/tests/sandbox/test_realdata_demo.py
```

## Limits

- **No market impact.** Your orders never change the recorded book and never see each other: two of your orders at
  one price both assume the whole queue ahead, and two marketable orders arriving together can take the same
  displayed size. The scorecard counts these cases (`diagnostics`).
- **You react at your step, not at the event.** Decisions happen only at the times you advance to, so a fill or a
  quote change is acted on up to one step later, plus `latency_ns`. Waking at the next market event or at your own
  fill would be causal, but while each step costs a replay of the whole prefix it would cost a replay per event; an
  `advance` that ends at the next event is left for when the engine accepts orders into a running replay.
- **Arrival confirmation waits for the next market event.** On a quiet book a marketable order can show `pending`
  for as long as no event arrives; its fills then carry the arrival time.
- **Hidden liquidity, auctions and halts** follow the engine: hidden prints per the venue policy, orders resting
  through a book clear or a feed gap end `censored`.
- **Cost grows with the episode.** Each step that has open orders replays the episode's prefix from the start of the
  data (67 ms per step on TSLA above, over prefixes of 169,195 to 233,747 records); a step without open orders, or
  without a new market event since the last replay, replays nothing. Hundreds of steps are fine. A full liquid day in
  one-second steps is 23,400 steps: at least 26 minutes at the cost of a mid-morning step, and more, since each step
  replays the prefix from the start of the data and the whole TSLA day holds 2.9 times the records of its prefix at
  10:30 (678,438 against 233,747).
- **One instrument per episode**, FIFO venues only (as the engine).
- **The guarantee is about the interface.** Nothing the session returns depends on data past the visible cut, and
  every replay it runs reads only the copy up to the cut. Python code running in the same process can still open the
  dataset or read private attributes; an agent that must not, should drive the sandbox through the MCP tools, from
  another process.

## What the guarantee does not cover

The sandbox controls what the agent is shown; it cannot control what the agent already knows. The session names the
symbol, venue and date, and prices are absolute, so a language model whose training data covers that day may recall
how the price moved, and so may an agent tuned on earlier episodes of the same day. A verified certificate proves the
scorecard follows from the logged instructions on that data, not that they were made without such knowledge. For
claims about language-model agents, use data after the model's training cutoff or a day held out from every earlier
run, and name the dataset directory so its path does not give the day away (the agent passes it to
`qt_sandbox_start`). A blind mode that hides the instrument-day from the agent is not built yet: it would need
operator-pinned episodes, times relative to the session and rebased prices, not only a hidden symbol and date
(the [design note](../design/extensions/agent-sandbox.md) has the details).
