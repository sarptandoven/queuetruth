# Replay a public IEX DEEP+ day

**Goal:** run queuetruth's exact replay on real, current, order-by-order US equities data that anyone can download
for free and that may be redistributed: IEX's DEEP+ feed from IEX HIST.

**You need:** about 10 GB of download bandwidth (the raw file is streamed and filtered, never stored), under 300 MB
of free disk for the subset, and queuetruth built from this repository.

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

## 1. Stream a subset of one day

Pick a date from the [HIST listing](https://iextrading.com/api/1.0/hist) (any `DPLS` entry) and a few symbols. The
script streams the gzipped capture once, keeps only the requested symbols' messages (and the System Events) and
writes a small pcap plus a manifest with the source URL, the sha256 of the stream, the full stream's gap and
duplicate counts and IEX's attribution line:

<!-- check-docs: skip (streams about 9 GB from IEX; takes about 30 minutes) -->
```sh
python3 -I scripts/iex/stream_subset.py --date 20261009 --symbols SPY,AAPL,NVDA,TSLA
```

Outputs land in `.realdata/iex/` (gitignored). The subset renumbers the kept messages contiguously and carries every
gap of the source stream over as a jump of the same size, so the ingest below reports exactly the source's gaps and
none caused by the filter. `--self-test` checks the filter on a synthetic capture without a download.

## 2. Ingest

<!-- check-docs: skip (needs the subset from step 1) -->
```sh
queuetruth ingest --format iex-deep-plus --venue iex \
  --in .realdata/iex/20261009_DPLS.subset.SPY-AAPL-NVDA-TSLA.pcap.gz --out iex-20261009
```

or from Python:

<!-- check-docs: skip (needs the subset from step 1) -->
```python
import queuetruth as qt

qt.ingest({
    "format": "iex_deep_plus",
    "venue": "iex",
    "inputs": [{"path": ".realdata/iex/20261009_DPLS.subset.SPY-AAPL-NVDA-TSLA.pcap.gz"}],
    "out": "iex-20261009",
})
```

Instrument ids are assigned per symbol in order of first appearance in the file. Records go to the US/Eastern date of
their timestamp; the tick defaults to 0.01 (pass `--tick-size` to change it). `data_quality.json` lists the IEX
counters: `iex_modify_maintain_priority` and `iex_modify_reset_priority` (volumes, information only), and anomalies
such as `iex_unknown_order_ref` or `iex_modify_maintain_unrepresentable` (a maintain-priority modify that also changed
the price or raised the size, which queuetruth records cannot express; written as a priority loss and counted).

## 3. Replay with the rank oracle

<!-- check-docs: skip (needs the dataset from step 2) -->
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

The `iex` rule set is labelled `real_data` on the strength of this run.

## Limits

- One venue: DEEP+ covers trading on IEX only (IEX's terms, clause 08), and IEX's share of US equity volume is small,
  so queue dynamics on IEX are not those of Nasdaq or NYSE.
- Hidden liquidity is invisible until it trades; hidden prints follow the replay's `hidden_print_policy`.
- The channelized variant (`DPLC`, since 2026-08-07) uses the same messages on 16 channels; the reader tracks
  sequences per channel, but no `DPLC` day has been run through it yet.
- A maintain-priority Order Modify that changes the price or raises the size cannot keep priority in queuetruth
  records; it is counted (`iex_modify_maintain_unrepresentable`) and replayed as a priority loss.
