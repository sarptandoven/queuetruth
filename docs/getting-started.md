# Getting started

The quickest way to see what queuetruth answers is `queuetruth demo`: it streams the first minutes of a public
order-level trading day, places simulated resting orders in the real queue, and prints how often NautilusTrader's
default fill model fills them compared with the exact replay of that queue. On our heavily loaded laptop a first
run took 22 to 53 s and downloaded about 315 MB; a rerun from the cache took 13 to 17 s (section 1). The rest of the page walks through the same steps by hand: install
(section 2), a leaderboard on synthetic data (sections 3 to 7) and a full public Nasdaq day (section 8). Run the
runnable command blocks in order in one empty directory: each block uses the files the previous ones wrote.
`docs/check_docs.py` runs them the same way, and CI runs it on every change, so a block that stops working fails the
docs check.

## 1. A real-data answer in under a minute

`queuetruth demo` is new after release 0.1.1: the 0.1.1 wheel on PyPI does not have it, so until the next release
install from a source checkout (section 2).

<!-- check-docs: skip (downloads about 315 MB from emi.nasdaq.com) -->
```sh
queuetruth demo --yes
```

On 2026-10-24 this printed:

```text
queuetruth demo: Nasdaq TotalView-ITCH 5.0, 2019-12-30, AAPL MSFT SPY TSLA
3,000 simulated resting limit orders placed 09:35:00 to 09:40:00 ET (2,789 scored*), each followed for 5 min in
the exact replay of the real queue

Within 1 s, NautilusTrader's default fill model fills 33.1% of these orders; the exact replay of the real queue
fills 29.4%: 3.6 pp (1.12x) too many (95% CI +2.6 to +4.8 pp).
(model: queuetruth's faithful replica nt_prob_touch[p=1]; on the Nasdaq days 2019-12-30 and 2020-01-30 it fills
exactly the same orders as nautilus_trader 1.231.0 (FillModel) and 2.0.0rc7 (DefaultFillModel) at 1 s, 10 s and
60 s (docs/RESULTS.md, section 4))

horizon  exact fill rate  NautilusTrader default  overfill, pp        95% CI
100 ms              4.3%                    5.9%          +1.6  [+1.1, +2.1]
1 s                29.4%                   33.1%          +3.6  [+2.6, +4.8]
10 s               69.0%                   70.6%          +1.5  [+0.9, +2.3]
60 s               81.9%                   83.0%          +1.0  [+0.4, +2.0]
5 min              85.5%                   86.4%          +0.9  [+0.3, +1.9]
(95% CI: cluster bootstrap over instrument x minute, 20 clusters)
published for this day, full session, 3,000 shadows, 30-minute block bootstrap (docs/RESULTS.md, section 4):
1 s +3.1 [+2.6, +3.6]; 10 s +3.3 [+2.5, +4.1]; 60 s +1.4 [+1.0, +1.9] pp
exact fill rate at 1 s / 10 s / 60 s: 29.4% / 69.0% / 81.9% in this window, 11.0% / 48.4% / 75.0% over the
published session; where more orders fill under any model the overfill has less room to show
* 211 orders are left out of every model so that all are scored on the same orders (the zoo's common
population): a faithful hftbacktest model stops on them (its issue #312)
```

followed by the source note, the bytes read, the timings, and the paths of `qt-demo/demo.html` (two charts and the
tables, one self-contained file with no script and no external request) and `qt-demo/demo.json` (the same numbers as
a `qt.demo_summary` document; `--format json` prints it). Read the table this way: of these small orders at or near
the touch, 29.4% really fill within one second; a backtest with NautilusTrader's default `FillModel` fills 33.1% of
them, so it overstates fills by 3.6 percentage points at that horizon. The model is queuetruth's replica of that
fill model, which fills exactly the same orders as nautilus_trader 1.231.0 and 2.0.0rc7 on the two published days
([Results](RESULTS.md), section 4).

Compared with the full-session numbers published in [Results](RESULTS.md) (orders placed from 09:35 to 15:50): at
1 s this window's +3.6 pp [+2.6, +4.8] agrees with the session's +3.13 pp [+2.63, +3.64]. At 10 s and 60 s it is
smaller (+1.5 and +1.0 against +3.3 and +1.4 pp), and the 10 s intervals do not overlap. Minutes 5 to 10 after the
open are busier than the day as a whole: 69.0% of these orders fill within 10 s in the exact replay, against 48.4%
over the session, and when most orders fill under any model, a model that fills too many has less room to show it.
The demo prints this comparison under the table.

What the command does:

- **Fetch.** It streams the public Nasdaq TotalView-ITCH 5.0 sample file for 2019-12-30 (3.52 GB) from
  [emi.nasdaq.com](https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/) and stops reading at 09:45:10 ET. The book starts the
  day empty, so this prefix is an exact order-level record of everything before the cut. It keeps the messages of
  four symbols and writes a 7.0 MB subset with a manifest (source URL, bytes read, sha256 of what was read and of the
  subset) to your user cache directory (`~/Library/Caches/queuetruth/demo` on macOS, `~/.cache/queuetruth/demo` on
  Linux, or `QT_CACHE_DIR`). The subset content of the default run is pinned by sha256; if the public file ever
  changes, the demo stops with `QT-INPUT-002` instead of printing a different answer. The next run reads the cache
  and downloads nothing.
- **Ingest, shadows, zoo.** It ingests the subset (654,098 records, data quality `clean`), samples 3,000
  never-cancel shadow orders placed uniformly between 09:35:00 and 09:40:00 ET (the default placements and sizes of
  `shadows sample`), and runs seven zoo models in faithful mode against the exact FIFO queue at horizons from
  100 ms to 5 min: NautilusTrader's default `nt_prob_touch[p=1]` and `nt_queue_l2`, `hbt_power3[n=2]`,
  `hbt_risk_adverse`, `databento_mc[k=-0.8;particles=100]` and the bounds `front_of_queue` and `trade_through_only`
  (`--models all` runs all 16; `--verbose` prints every model's bias). The data cut leaves 5 minutes and 10 s of
  replay after the last placement, so every order has its full 5-minute label. As in `queuetruth zoo`, an order on
  which a faithful hftbacktest model stops (its issue #312) is left out of every model, so all models are scored on
  the same orders (211 of 3,000 here; the footnote under the table says so). The NautilusTrader numbers therefore
  depend on which models run: `--models` changes that population.
- **Answer.** The rates and biases are the zoo leaderboard's. The zoo's own 95% interval resamples 30-minute buckets
  of placement time, and a 5-minute window has only one, so the demo recomputes the interval with a cluster
  bootstrap over instrument x minute of placement (20 clusters, 2,000 draws). Orders on the same symbol in
  neighbouring minutes are not independent, so treat it as an approximate interval; the published full-session
  numbers are the reference.

Nothing is downloaded without your consent: on a terminal the demo shows the source, the estimated size and the
cache path and asks before downloading (the question goes to stderr); elsewhere it stops with `QT-USAGE-001` unless
you pass `--yes`. The Python API (`queuetruth.demo.run_demo`) downloads only with `DemoOptions(yes=True)` or a
`confirm` callback that returns True. A download whose estimate exceeds `--max-download-mb` (default 4000) is
refused before it starts. A failed or interrupted fetch never replaces a good cached subset. `--dry-run` shows the
plan and downloads nothing:

<!-- check-docs: run -->
```sh
queuetruth demo --dry-run --cache-dir qt-cache
```

**Measured cost.** On an Apple M1 Pro laptop (8 cores, 16 GB, Low Power Mode) shared with many other jobs (1-minute
load average 390 to 440 during the first four runs and 225 to 265 during the last three), on 2026-10-24:

| Run | Downloaded | Wall time | Fetch | Zoo | Ingest, shadows, answer |
|---|---|---|---|---|---|
| default, first run | 312.5 MB | 52.6 s | 34.9 s | 12.5 s | 1.4 s |
| default, first run again (cache cleared) | 316.7 MB | 29.0 s | 13.7 s | 9.4 s | 1.2 s |
| default, from the cache | 0 MB | 16.5 s | 0.0 s | 11.9 s | 1.4 s |
| `--source iex`, first run | 906.0 MB | 119.0 s | 103.7 s | 10.1 s | 1.4 s |
| default, first run (later build) | 316.7 MB | 21.7 s | 10.4 s | 7.8 s | 1.1 s |
| default, from the cache (later build) | 0 MB | 13.4 s | 0.0 s | 9.4 s | 1.4 s |
| `--from-file` on the cached default subset (later build) | 7.0 MB read | 13.4 s | 0.9 s | 8.8 s | 1.2 s |

Wall time is the whole process (`/usr/bin/time`), including Python start-up; the step times come from
`demo.json`. The bytes read vary by a few MB with how far the read-ahead got when the cut was found. Every run
printed the same numbers. The `run_id` identifies the queuetruth version, the settings and the subset's content, not
where the subset came from: the three runs of the later build printed the same `run_id`, `d46813cf69253f4d`. Fetching is bounded by the download and by decompressing about
780 MB of ITCH on one core; `curl` alone read the same prefix at 43.6 MB/s here.

**Why these defaults.** Streaming each day from its first byte on 2026-10-24, the Nasdaq file needs 88 MB to reach
the 09:30 open and then about 14 MB per minute, while the IEX DEEP+ file needs 24 MB to the open and then about 60 MB
per minute, so the same window costs about three times more from IEX (906 MB against 312 to 317 MB). The default
placement window is 5 minutes, which gives the interval 4 symbols x 5 minutes = 20 clusters; `--minutes 10` read
385 to 402 MB. The window starts at 09:35 and each order is followed for 5 minutes because the published run does
the same (placements from 300 s after the open, planned life 300 s, [Results](RESULTS.md) section 4): the opening
cross and its first minutes stay out, and the 5-minute column is comparable. Both cost bytes. Interpolated from the
measured checkpoints (`queuetruth.demo.sources`), a 60 s horizon would cut at 09:41:10 after about 256 MB, and a
09:30 start would cut at 09:40:10 after about 242 MB, against 312 to 317 MB measured for the default: 55 to 75 MB,
or about 2 to 6 s of fetch at the rates measured here.

**Other sources and your own files.**

- `--source iex` uses IEX DEEP+ for 2026-10-09 (SPY, AAPL, NVDA, TSLA). It printed +1.1 pp at 1 s (95% CI +0.6 to
  +1.6) on these orders. That is a different venue, day and set of symbols; the published Nasdaq numbers do not
  apply to it. Every output built from it carries IEX's attribution, as IEX's terms require:

  > Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data
  > Terms of Use.

  ([IEX Historical Data Terms of Use](https://iextrading.com/iex-historical-data-terms/))
- `--from-file PATH` reads a day file (or any prefix or symbol subset of one) that you already have, gzip or raw, and
  `--url URL` streams one from another server, so a hosted subset can be used once one exists. The format and the
  session date come from the file name (`MMDDYYYY.NASDAQ_ITCH50.gz`, `YYYYMMDD_IEXTP1_DPLS1.0.pcap.gz`, also inside
  an IEX HIST download URL), or from `--input-format itch|iex` and `--date YYYY-MM-DD`; the demo never borrows them
  from a built-in source, and stops with `QT-USAGE-001` when it cannot tell. A file's first bytes must match the
  format, and an IEX capture must start on the session date (`QT-INPUT-002` otherwise). The input must cover every
  order's 5-minute label: it either reaches the data cut or ends after the end of the last label. The demo's own
  subset qualifies: `--from-file` on a cached `subset.itch50.gz` reads its format, date and symbols from the
  `manifest.json` next to it, and gives the same numbers and `run_id` as the run that cut it (the `run_id` depends
  on the content, not on where it came from). Content that matches a pinned default subset is reported as that
  source, with its note and published reference.
- `--symbols`, `--minutes`, `--n` and `--seed` change the sample; `--out` (default `qt-demo`), `--cache-dir` and
  `--refresh` change where files go. `--out` must be new, empty, or an earlier demo output (it holds a `.qt-demo`
  marker). `queuetruth demo --help` or `queuetruth help demo` lists every flag; `queuetruth --help` does not list
  `demo` yet.

The Nasdaq sample files carry no terms of use of their own and Nasdaq claims copyright in them (see
[Data and licensing](data-and-licensing.md)): the demo keeps the filtered subset in your cache, prints aggregate
statistics only, and `demo.html` holds no order-level rows. The full report of the zoo run
(`queuetruth report --in qt-demo/zoo`) does hold event-level traces; keep it to yourself for Nasdaq data.

## 2. Install

```sh
pip install queuetruth            # Python API, CLI, HTML report
pip install "queuetruth[mcp]"     # adds the MCP server for agents
pip install "queuetruth[client]"  # adds the HTTP client for a queuetruth service
```

Wheels are built for macOS (arm64, x86_64) and Linux manylinux (x86_64, aarch64), CPython 3.11 and later (one
abi3 wheel per platform). On other platforms, build from source. Runtime dependencies are pyarrow, numpy, scipy,
scikit-learn and pydantic. Nothing is downloaded after install unless you run `queuetruth demo` and confirm the
download, and nothing calls home, so the wheel can come from an internal mirror.

There is also a standalone binary, `queuetruth-core`, for machines without Python. It accepts the same command line
and runs the native commands (`ingest`, `replay`, `shadows sample`, `shadows mirror-real`, `features`, `why`,
`export l2-npz`, `referee fixtures`, `fixtures synth`) and the Rust part of the hybrid commands. Python-only commands
exit 2 there with `QT-USAGE-001`. `queuetruth-core` standalone binaries are attached to each GitHub release.

Check the install:

<!-- check-docs: run -->
```sh
queuetruth doctor
```

`doctor` prints the Python and platform details, the engine version, whether the bundled JSON Schemas load, and
whether PostgreSQL and Docker are present (only the self-hosted service needs them). It exits 1 if it finds a
problem.

To build from source (Rust 1.85 or later, Python 3.11 or later):

```sh
git clone <repository URL> queuetruth && cd queuetruth
make venv sync develop
```

## 3. Synthetic data

queuetruth ships no market data. `fixtures synth` writes a small synthetic order-level dataset generated by our own
code: CME-style rules, two instruments, two session dates (2026-06-15 and 2026-06-16), about 200,000 records. It is
the same generator, seed and layout as `tests/fixtures/synth_small` in the repository. The numbers it produces say
nothing about any real venue; it is only for learning the workflow.

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
(cd synth && shasum -a 256 -c MANIFEST.sha256 > /dev/null && echo "synth: all files verified")
```

`synth/` is a dataset directory (see [Schemas](reference/schemas.md#schemas-reference)): `manifest.json`,
`instruments.parquet`, `mbo/date=.../instrument_id=.../part-00000.parquet`, source trade prints for parity checks,
`data_quality.json`, plus `truth/` (expected outputs from an independent reference implementation) and
`own_orders/` (200 real orders of this tape packaged as "your own" live orders, used by the scorer examples).

## 4. A queue-model leaderboard

**Place shadow orders.** A shadow order is an order you did not send but want to evaluate: side, price, size and
time. `shadows sample` draws them from the dataset: by default 60% join the best bid or ask, 20% one tick behind,
10% two ticks behind and 10% one tick inside the spread.

<!-- check-docs: run -->
```sh
queuetruth shadows sample --data synth --n 1000 --out qt-shadows.parquet
```

**Run the zoo.** One pass feeds every record to the exact FIFO engine, which knows where each shadow sits in the
real queue, and to an L2 view built from the same data, which only shows total size per price level. The 16 models
of the default set run on the L2 view. For every model and shadow you get the predicted fill probability at 100 ms,
1 s, 10 s, 60 s and 300 s next to the exact outcome.

<!-- check-docs: run -->
```sh
queuetruth zoo --data synth --shadows qt-shadows.parquet --out qt-zoo
ls qt-zoo
```

The terminal shows the answer first: the exact fill rate at every horizon, then each model's fill-rate bias (model
minus exact truth, in percentage points, `*` where the 95% interval excludes 0), sorted by the absolute bias at 60 s
(`--sort-horizon` picks another horizon), then one plain sentence per model with both fill rates ("fills 56.2% of
orders vs 57.0% in the exact replay, 0.8 pp less often"). Every model is scored on the same shadows. Faithful
hftbacktest models stop on some orders where hftbacktest itself would have aborted the backtest (its issue #312);
those shadows are left out of every model and one warning line counts them (`--verbose` prints each model's count;
see [The L2 model zoo](concepts/l2-model-zoo.md)). `queuetruth zoo --list-models` lists the model ids.

**Read these numbers as a demonstration of the workflow, not as evidence.** The synthetic order flow is not built to
make queue position matter, so this leaderboard says nothing about real markets. The evidence about real markets is the leaderboard on two public
Nasdaq days in [Results](RESULTS.md): there `nt_prob_touch[p=1]` fills 14.17% of 3,000 small orders within 1 s on
2019-12-30 where the exact replay fills 11.03% (+3.13 pp; the same orders fill in nautilus_trader 1.231.0's own
`BacktestEngine` with `FillModel(prob_fill_on_limit=1)` on a delta-fed L2 book), while the hftbacktest power and
log models stay within about 1 pp of exact.

`qt-zoo/summary.json` holds the leaderboard. To print it sorted by the 10 s fill-rate bias:

<!-- check-docs: run -->
```sh
python - <<'EOF'
import json

s = json.load(open("qt-zoo/summary.json"))
h = str(10_000_000_000)  # horizon keys are nanoseconds as decimal strings
rows = sorted(s["leaderboard"], key=lambda r: r["fill_rate_bias"][h]["value"])
for r in rows:
    b = r["fill_rate_bias"][h]
    print(f'{r["model_id"]:52s} {b["value"]:+.4f}  [{b["ci_lo"]:+.4f}, {b["ci_hi"]:+.4f}]')
EOF
```

A positive `fill_rate_bias` means the model fills more often than the exact replay: the direction that makes a
backtest look better than live trading. `front_of_queue` and `trade_through_only` are the optimistic and pessimistic
bounds, and every run checks that the exact fill count lies between their fill counts (oracle O-ZOO-6). The bounds
are built to bracket the exact arm order by order, and the tests check that on scenario and synthetic tapes; they
bound the exact replay, not the other models (on real data the faithful NautilusTrader touch model reaches
`front_of_queue`, see [Results](RESULTS.md), section 4).

**Render the report.**

<!-- check-docs: run -->
```sh
queuetruth report --in qt-zoo --out qt-report.html
```

`qt-report.html` is one self-contained file with no external requests (`--sort-horizon` sets the horizon its
leaderboard is sorted by, 60 s by default). It opens with the answers: one plain sentence per question the inputs
answer (here, which L2 model is closest to the exact replay, which models tie with it, and whether any model
overstates passive fills), each with its base rate, 95% interval and a link to the evidence, followed by a compact
leaderboard. The details are collapsed under them and open with a click: the full leaderboard, reliability diagrams,
fill-rate bias by queue decile and liquidity bucket, the inputs with their sha256, the scope and limits, and
event-by-event "why" traces for the orders where models disagreed most with the exact answer. The inputs and the
scope and limits are always printed, also when collapsed on screen.

## 5. One order, event by event

Replay the dataset with the shadows to get each order's exact fill timeline and queue trajectory:

<!-- check-docs: run -->
```sh
queuetruth replay --data synth --shadows qt-shadows.parquet --out qt-replay
```

`qt-replay/` holds `summary.json`, `shadow_results.parquet` (one row per shadow: when it went live, queue ahead at
that moment, first and full fill times, how it ended), `shadow_fills.parquet` (one row per fill, with its reason),
`queue_trajectory.parquet` (queue ahead and rank after every event that changed them),
`shadow_planned_end.parquet`, `trades.parquet`, `bbo.parquet` and `data_quality.json`. The `invariants` block of the
summary has eight counters that are zero on clean data.

To trace one filled shadow through every book event at its price level:

<!-- check-docs: run -->
```sh
SID=$(python -c "
import pyarrow.compute as pc, pyarrow.parquet as pq
t = pq.read_table('qt-replay/shadow_results.parquet')
print(t.filter(pc.equal(t['end_state'], 'filled'))['shadow_id'][0])
")
queuetruth why --data synth --shadows qt-shadows.parquet --shadow-id "$SID" --format json > why.json
python -c "
import json
w = json.load(open('why.json'))
print(w['subject'], w['outcome'])
for e in w['events'][:10]:
    print(e['ts_event'], e['action'], e['effect'], 'ahead' if e['is_ahead'] else '', e['queue_ahead_after'], e['fill_qty'])
"
```

[Explain one fill](how-to/explain-one-fill.md) describes the trace columns.

## 6. The same from Python

Every CLI command has a Python function that takes the same spec (a dict or a Pydantic model) and returns the same
summary as a Pydantic model. Tables are `pyarrow.Table`; use `polars.from_arrow(...)` or `.to_pandas()` as you
prefer.

<!-- check-docs: run -->
```python
import queuetruth as qt

qt.sample_shadows({"dataset": {"path": "synth"}, "n": 1000, "out": "qt-shadows.parquet"})
zoo = qt.run_zoo(
    {
        "dataset": {"path": "synth"},
        "shadows": {"path": "qt-shadows.parquet"},
        "out": "qt-zoo",
        "models": ["hbt_power3[n=2]", "hbt_risk_adverse", "front_of_queue", "trade_through_only"],
    }
)
h10 = str(10_000_000_000)
for row in zoo.model_dump()["leaderboard"]:
    b = row["fill_rate_bias"][h10]
    print(f'{row["model_id"]:48s} {b["value"]:+.4f}  [{b["ci_lo"]:+.4f}, {b["ci_hi"]:+.4f}]')

fills = qt.read_table("qt-zoo/zoo_fills.parquet", expect="qt.zoo_fills")
print(fills.num_rows, "model fills")
```

For interactive questions about the book at a point in time, `qt.Session` steps through a dataset:

<!-- check-docs: run -->
```python
import json

import queuetruth as qt

part = json.load(open("synth/manifest.json"))["partitions"][0]
inst = part["instrument_id"]
s = qt.Session("synth")
s.advance_to((part["ts_min"] + part["ts_max"]) // 2)  # ns since the epoch, UTC
bbo = s.bbo(inst)  # {"bid": (price, qty, order count), "ask": (...)}
print(bbo)
print(s.depth(inst, 3))  # three levels per side
print(s.queue_ahead(inst, "B", bbo["bid"][0]))  # (qty, orders) a new bid at the best bid would join behind
```

Prices are fixed point: real price = `price * 10^price_exp` (`price_exp` is in `instruments.parquet`, `-9` here).

`qt.Session.with_orders(spec)` also takes your own orders while it runs, decided from what it has shown you so far.
Each order joins the real queue as in `replay` with the same spec (latency model, fill rule, feed gaps and halts),
the session reads the data once however many orders you send, and its fills equal one `replay` with every order in
its input:

<!-- check-docs: run -->
```python
import json

import queuetruth as qt

part = json.load(open("synth/manifest.json"))["partitions"][0]
inst, t = part["instrument_id"], (part["ts_min"] + part["ts_max"]) // 2
s = qt.Session.with_orders({"dataset": {"path": "synth"}, "latency": {"gateway_ns": 50_000}})
s.advance_to(t)
bid = s.bbo(inst)["bid"][0]
s.submit([{"shadow_id": "my-bid", "instrument_id": inst, "side": "B", "price": bid, "qty": 1, "ts_submit": t}])
s.advance_to(t + 60_000_000_000)  # one minute later
print(s.take_fills().to_pylist())  # this order's fills so far (qt.shadow_fills rows)
s.cancel("my-bid", s.position)
s.finish()
print(s.take_results().select(["shadow_id", "end_state", "filled_qty"]).to_pylist())
```

## 7. Scripting and CI

- `--format json` prints exactly one JSON document to stdout: the command's summary, or a `qt.error` document.
  Logs and warnings go to stderr.
- Exit codes: `0` success, `1` the command ran but a gate you asked for failed (`--strict`, `--fail-*`, referee
  tolerance), `2` any error. Errors carry a stable code such as `QT-INPUT-002`; see [Error codes](reference/errors.md).
- For the same inputs, spec and version, outputs are byte-identical whatever `--threads` is. Every summary carries
  a `run_id` derived from the engine version, the spec and the sha256 of every input.

A CI gate on the scorer: the synthetic own-orders log against the optimistic `front_of_queue` bound, failing when
the absolute fill-rate bias exceeds 2 pp. `--fail-abs-bias` takes a fraction (`0.02` is 2 pp), while the output
prints pp. `front_of_queue` overfills this log by several pp, so the gate fails with exit code 1:

<!-- check-docs: run -->
```sh
set +e
queuetruth score --live synth/own_orders/live --data synth --sim-model front_of_queue --out sc \
    --fail-abs-bias 0.02 --format json > sc.json
rc=$?
set -e
echo "exit code $rc"
python -c "import json; print(json.load(open('sc.json'))['verdict']['headline'])"
test "$rc" -eq 1
```

## 8. A full real day: Nasdaq TotalView-ITCH

Nasdaq publishes full-day TotalView-ITCH 5.0 sample files at
[emi.nasdaq.com/ITCH/Nasdaq ITCH/](https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/). They are order-level, so the exact
replay is ground truth for displayed liquidity. Each file is several GB compressed. queuetruth does not mirror them,
and apart from the prefix `queuetruth demo` streams after you confirm, it never downloads them for you: read Nasdaq's
terms (see [Data and licensing](data-and-licensing.md)) and download a file yourself.

<!-- check-docs: skip (needs a downloaded ITCH file) -->
```sh
queuetruth ingest --format itch --in 01302020.NASDAQ_ITCH50.gz --venue nasdaq --date 2020-01-30 \
    --symbols AAPL,MSFT,SPY,TSLA --out nasdaq-20200130
queuetruth replay --data nasdaq-20200130 --out replay-20200130 --rank-oracle --parity-trades
```

Read `replay-20200130/summary.json` before anything else:

- `invariants`: eight counters (cancels larger than the resting order, fills of unknown orders, books crossed after
  a complete event, and so on). On a clean day all are zero.
- `parity`: trades derived by the replay compared with the file's own execution prints. `pass` should be `true`.
- `rank_oracle.rank0_share_sweep_adjusted`: the share of real executions where the executed order was at the head
  of its price level in our book, after grouping multi-level sweeps. This is the direct check that the FIFO book
  matches the matching engine.
- `data_quality.verdict`: `clean`, `warnings` or `unusable`. Every later summary repeats it.

Then the same analysis as the synthetic walkthrough, on real orders:

<!-- check-docs: skip (needs a downloaded ITCH file) -->
```sh
queuetruth shadows sample --data nasdaq-20200130 --n 3000 --out shadows.parquet
queuetruth shadows mirror-real --data nasdaq-20200130 --n 3000 --out mirror.parquet
queuetruth zoo --data nasdaq-20200130 --shadows shadows.parquet --out zoo --threads 4
queuetruth calibrate alloc --family power3 --data nasdaq-20200130 --shadows shadows.parquet \
    --train blocks:0-3/5 --test blocks:4/5 --out cal-power3
queuetruth report --in zoo --in cal-power3 --out nasdaq-20200130.html
```

`shadows mirror-real` places each shadow directly behind a real order at the moment that order arrived, with the
real order's cancel time. It gives a population that follows real traders' placement; its results are reported
separately from never-cancel shadows (see [Rules from the independent audit](concepts/audit-rules.md)).

On an Apple M1 Pro (8 cores) the zoo step for the 16 models of `--models all` and 3,000 shadows took 11.4 to 11.7 s
(faithful) and 7.7 s (corrected) for 2019-12-30 and 15.2 s and 11.7 to 11.8 s for 2020-01-30 at 4 threads, measured
while other builds were running ([Results](RESULTS.md), section 4). To reproduce the published real-data numbers from the public Nasdaq files alone, run `make reproduce`
(`scripts/repro/reproduce.py`; it streams both day files, keeps the four symbols and reruns ingest, replay, the
leaderboard, calibration, the learned model and the oracle) and `make reproduce-breadth` (`scripts/repro/breadth.py`,
the 36-symbol breadth study); see [Results](RESULTS.md), section 8.

## Next

- [Exact FIFO truth](concepts/exact-truth.md): what "exact" means here and where it stops.
- [Calibrate my hftbacktest model](how-to/calibrate-hftbacktest.md)
- [Score my Nautilus backtest against my Binance fills](how-to/score-nautilus-vs-binance.md)
- [Referee my simulator](how-to/referee-your-simulator.md)
