# A real day from free IEX data

**Goal:** run queuetruth's exact replay on real, current, order-by-order US equities data that anyone can download
for free and that may be redistributed: IEX's DEEP+ feed from IEX HIST. The last section does the same on a public
Nasdaq TotalView-ITCH sample day.

**You need:** about 10 GB of download bandwidth (the raw file is streamed and filtered, never stored), 2 GB of free
disk (the dataset of a few symbols takes a few hundred MB), and queuetruth 0.4.0 or later.

> Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data
> Terms of Use.

That line is IEX's required attribution ([IEX Historical Data Terms of Use](https://iextrading.com/iex-historical-data-terms/),
clause 01). Every artifact you publish that is built from IEX HIST data (datasets, traces, reports, charts) must carry
it with the link. See [Data and licensing](../data-and-licensing.md) for how IEX's terms compare with the other
sources.

## What DEEP+ is and how queuetruth reads it

IEX publishes every trading day of its DEEP+ feed (HIST feed code `DPLS`, since 2024-10-01) on a T+1 basis at
[IEX HIST](https://iextrading.com/trading/market-data/), as one gzipped pcapng capture of the multicast stream (8 to
18 GB compressed). DEEP+ is order by order: every displayed order is added, modified, deleted or executed under its own
order id. queuetruth's reader (`IngestSpec.format = "iex_deep_plus"`) follows two official documents:

- IEX DEEP+ Specification v1.06 (updated 2026-10-07): the message layouts (Message Protocol ID 0x8005);
- IEX Transport Specification v1.26 (2026-06-02): the IEX-TP segment that carries them (40-byte header, sequence
  numbers, heartbeats).

It reads pcap or pcapng, gzip or raw, walks Ethernet / IPv4 / UDP to the IEX-TP segments, drops A/B duplicates and
counts sequence gaps per channel and session, and maps each message to queuetruth MBO records:

| DEEP+ message | queuetruth records |
|---|---|
| Add Order `a` | `A` |
| Order Modify `M`, Modify Flags bit 0 = 1 (maintain priority), same price, smaller size | `C` of the decrease: the order keeps its place |
| Order Modify `M`, bit 0 = 0 (reset priority) | `C` of the remaining size, then `A` with the same id at the back of the queue |
| Order Delete `R` | `C` of the remaining size |
| Order Executed `L` | `T` (aggressor = the opposite side) + `F` + `C` of the resting order |
| Trade `T` (non-displayed against non-displayed) | `T` with side `N`: a hidden print that consumes no displayed queue |
| Clear Book `C` | `R` |
| System Event, Security Directory, Trading Status, Operational Halt, Short Sale Price Test, Retail Liquidity, Security Event, Trade Break | no records (statistics) |

The explicit priority bit is what makes DEEP+ useful for queue truth: IEX says on every modify whether the order kept
its time priority, so queuetruth does not have to infer it. The rule set is `iex`
([Venue semantics](../venue-semantics.md)): price, then display, then time priority (IEX Rule 11.220), displayed
interest ahead of non-displayed interest at a price, aggressor side on displayed executions, and real IEX-TP sequence
numbers (a gap becomes a `feed_gaps` entry and censors the shadows of that instrument). The records use Nasdaq record
semantics, so datasets carry `venue = nasdaq`, `rule_set = iex` and `source_venue = iex`.

What DEEP+ does not show: non-displayed orders and the hidden parts of reserve orders. Their executions arrive as Trade
messages with no resting order and become hidden prints. Routed executions are not reported at all.

## 1. Fetch a subset of one day

Pick a date from the [HIST listing](https://iextrading.com/api/1.0/hist) (any `DPLS` entry) and a few symbols:

<!-- check-docs: skip (streams a whole IEX day, several GB) -->
```sh
queuetruth data fetch iex --date 2026-10-09 --symbols SPY,AAPL,NVDA,TSLA --out iex-20261009
```

It asks before downloading (or pass `--yes`), streams the gzipped capture once, keeps only the requested symbols'
messages (and the System Events), ingests them and checks the data quality ([Add order-level data](add-data.md)).
The kept messages are renumbered contiguously and every gap of the source stream is carried over as a jump of the
same size, so the dataset reports exactly the source's gaps and none caused by the filter. IEX's attribution line
and the URL read go into the dataset's `manifest.json`. From Python:

<!-- check-docs: skip (streams a whole IEX day, several GB) -->
```python
import queuetruth as qt

qt.data.fetch_iex("2026-10-09", ["SPY", "AAPL", "NVDA", "TSLA"], out="iex-20261009", yes=True)
```

The results below were made with the repository script `scripts/iex/stream_subset.py`, which writes the same kind of
filtered pcap and a manifest (source URL, sha256 of the stream, the full stream's gap and duplicate counts) without
ingesting it; `queuetruth data add` then reads that pcap.

## 2. What the dataset holds

Instrument ids are assigned per symbol in order of first appearance in the file. Records go to the US/Eastern date of
their timestamp; the tick defaults to 0.01 (`queuetruth ingest --tick-size` sets another). `data_quality.json` lists the IEX
counters: `iex_modify_maintain_priority` and `iex_modify_reset_priority` (volumes, information only), and anomalies
such as `iex_unknown_order_ref` or `iex_modify_maintain_unrepresentable` (a maintain-priority modify that also changed
the price or raised the size, which queuetruth records cannot express; written as a priority loss and counted).

## 3. Replay with the rank oracle

<!-- check-docs: skip (needs the dataset from step 1) -->
```sh
queuetruth replay --data iex-20261009 --out iex-20261009-replay --rank-oracle
```

The rank oracle checks, for every execution of a displayed order, where that order stood in its price level's FIFO
queue. On a correct priority mapping every execution hits the head of the queue.

## Results on 2026-10-09

Day `20261009_IEXTP1_DPLS1.0.pcap.gz` (9,383,598,955 bytes, sha256
`0fc06989540e36de6dfb8455e6ccda20fda6161ea2800631cb6128bef16a93af`), symbols SPY, AAPL, NVDA and TSLA, run on
2026-10-19 with the commands above:

| Step | Result |
|---|---|
| Full stream | 263,277,570 IEX-TP segments (3,646 heartbeats), 330,703,432 DEEP+ messages, one session, 0 sequence gaps, 0 duplicates; 42.05 GB inflated |
| Subset | 4,009,395 messages, 96.8 MB gzip pcap |
| Ingest | 4,435,281 records (NVDA 2,169,963; SPY 1,531,785; AAPL 604,915; TSLA 128,618); verdict `clean`: 0 unknown references, 0 duplicate adds, 0 `ts_event` regressions, 0 feed gaps |
| Order Modify | 383,580 reset priority, 817 maintain priority (all same-price size decreases; 0 unrepresentable) |
| Executions | 21,301 displayed (`L`), 63,766 hidden prints (`T`), no single-price crosses |
| Replay invariants | cancels or fills exceeding resting, fills of unknown orders, unknown references, duplicate adds, unexpected modifies, crossed books, negative spreads: all 0 |
| Rank oracle | 21,301 executions checked, every one at FIFO rank 0 of its price level, 0 price-priority violations |
| Control | replaying the same records as if the 1,742 same-price reset-priority modifies had kept priority puts 33 executions behind the head of their queue: the priority bit is what makes the ranks exact |
| Time | stream and filter 30 min (5.2 MB/s, CPU bound in the stdlib-only script); ingest 8.0 s and replay 10.2 s in debug builds |

The `iex` rule set is labelled `real_data` on the strength of this run and of the atlas days
([What is validated](../validated.md)).

## Limits

- One venue: DEEP+ covers trading on IEX only (IEX's terms, clause 08), and IEX's share of US equity volume is small,
  so queue dynamics on IEX are not those of Nasdaq or NYSE.
- Hidden liquidity is invisible until it trades; hidden prints follow the replay's `hidden_print_policy`.
- The channelized variant (`DPLC`, since 2026-08-07) uses the same messages on 16 channels; the reader tracks
  sequences per channel, but no `DPLC` day has been run through it yet.
- A maintain-priority Order Modify that changes the price or raises the size cannot keep priority in queuetruth
  records; it is counted (`iex_modify_maintain_unrepresentable`) and replayed as a priority loss.

## The same on a Nasdaq sample day

Nasdaq publishes full-day TotalView-ITCH 5.0 sample files at
[emi.nasdaq.com/ITCH/Nasdaq ITCH/](https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/). They are order-level, so the exact
replay is ground truth for displayed liquidity. Each file is several GB compressed. queuetruth does not mirror them,
and apart from the prefix `queuetruth demo` streams after you confirm, it never downloads them for you: read Nasdaq's
terms (see [Data and licensing](../data-and-licensing.md)) and download a file yourself.

<!-- check-docs: skip (needs a downloaded ITCH file) -->
```sh
queuetruth data add 01302020.NASDAQ_ITCH50.gz --symbols AAPL,MSFT,SPY,TSLA --out nasdaq-20200130
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

Then the same analysis as [Your first leaderboard](../getting-started.md), on real orders:

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
separately from never-cancel shadows (see [Rules from the independent audit](../concepts/audit-rules.md)).

On an Apple M1 Pro (8 cores) the zoo step for the 16 models of `--models all` and 3,000 shadows took 11.4 to 11.7 s
(faithful) and 7.7 s (corrected) for 2019-12-30 and 15.2 s and 11.7 to 11.8 s for 2020-01-30 at 4 threads, measured
while other builds were running ([Results](../RESULTS.md), section 4). To reproduce the published real-data numbers from the public Nasdaq files alone, run `make reproduce`
(`scripts/repro/reproduce.py`; it streams both day files, keeps the four symbols and reruns ingest, replay, the
leaderboard, calibration, the learned model and the oracle) and `make reproduce-breadth` (`scripts/repro/breadth.py`,
the 36-symbol breadth study); see [Results](../RESULTS.md), section 8.
