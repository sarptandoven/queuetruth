# Results

Aggregate numbers from real-data and performance runs on one Mac (Apple M1 Pro, 8 cores, 16 GB, macOS 15). Every
number below was recomputed from a run artifact named in its section; nothing is copied from the
README of the author's earlier private prototype. No event-level rows, prices or per-order tables are published here; the artifacts stay in the gitignored
`.realdata/` directory.

**Data and licensing.** The real days are the public Nasdaq TotalView-ITCH 5.0 sample files for 2019-12-30 and
2020-01-30 ([Nasdaq EMI](https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/)), restricted to AAPL, MSFT, SPY and TSLA. The
crypto captures are 10-minute public WebSocket recordings of Bitstamp and Bitfinex BTC-USD (2026-10-12). No copy of
either is in this repository.

**Which input.** The leaderboard (section 4), the breadth run (4b), the RD-9 calibration and the RD-10 learned model
are computed **from the raw public ITCH bytes** with queuetruth's own ITCH reader (`scripts/repro/reproduce.py`,
section 2), with no artifact of the author's earlier private prototype. On the same build they are identical to the
same runs on the prototype's converted CSV (section 2). Sections 1 and 3 (CSV rows), RD-8's mirror-shadow checks and
RD-11 compare against the prototype's own outputs and therefore use its converted CSV of the ITCH files ("et" in
script and format names: its `mbo.csv`, `trades.csv`, `instruments.csv`, ingested with `queuetruth ingest --format
etcsv`). The CSV merges hidden and cross prints into one side-`N` print, so the hidden-print rule (audit A3) is not
verified by the CSV runs.

## Provenance

| Artifact | sha256 (first 16 hex) | Written by | When (UTC) |
|---|---|---|---|
| `.realdata/results.json` (RD-1 .. RD-13) | `163dcb1bcccca1ed` | `make realdata` (`scripts/realdata/validate_et.py`, threads 4), 1,016 s | 2026-10-19 06:02 |
| `.realdata/rd9/results.json` (RD-9 rerun) | `3a75d56082db5500` | `validate_et.py --only RD-9` after the driver learned to record export refusals | 2026-10-19 01:46 |
| `.realdata/rd13/results.json` (RD-13 rerun) | `84414da9bc3c69ce` | `validate_et.py --only RD-13` with a rebuilt release `queuetruth-core` | 2026-10-19 01:47 |
| `scripts/realdata/expected.json` (targets) | `eceab0235b71a997` | separate expectation scripts, recomputed from et artifacts | 2026-10-18 |
| `.realdata/headline/headline.json` (L2 leaderboard) | `7fb312eb7116aa97` | `scripts/realdata/headline.py --threads 4` | 2026-10-19 01:36 |
| `.realdata/oracle/2019-12-30/discrepancy_report.json` | `aa332eb45846fa85` | `scripts/oracle/run_oracle.py --run-qt` | 2026-10-19 01:40 |
| `.realdata/oracle/2020-01-30/discrepancy_report.json` | `f39e6326869baba9` | `scripts/oracle/run_oracle.py --run-qt` | 2026-10-19 01:44 |
| `.realdata/itch/realbytes-12302019.json` | `d122666c1cb3e305` | `scripts/realdata/itch_realbytes.py` | 2026-10-19 00:35 |
| `.realdata/itch/realbytes-01302020.json` | `7cf822595e993f7d` | `scripts/realdata/itch_realbytes.py` | 2026-10-19 00:35 |
| `.realdata/dbn-samples/results.json` (Databento DBN test files) | `c985f384672b87bb` | `scripts/realdata/dbn_samples.py` | 2026-10-19 01:27 |
| `.realdata/bench.json` (qtbench quick suite) | `1f422f444c4e9801` | `make bench BENCH_SUITE=quick` | 2026-10-19 00:05 |
| `.realdata/repro/phase1/results.json` (public raw-bytes reproduction) | `dc74725450766f49` | `scripts/repro/reproduce.py`, threads 4, 1,387 s | 2026-10-19 07:38 |
| `.realdata/repro/phase1/headline/headline-2019-12-30.json` / `-2020-01-30.json` | `fb9703bb8fa6bfa4` / `2ab595da18c8a29a` | `reproduce.py` (headline step) | 2026-10-19 07:16 / 07:26 |
| `.realdata/repro/phase1/decompose.json` (raw bytes vs CSV vs published) | `a750ce7076b874ec` | `scripts/repro/compare_published.py` functions, CSV control run | 2026-10-19 07:40 |
| `.realdata/repro/results.json` (raw-bytes reproduction on commit `4be5240`) | `446c6b09ba71dbd8` | `make reproduce` (`scripts/repro/reproduce.py`), threads 4, 1,162 s | 2026-10-19 08:25 |
| `.realdata/repro/breadth/breadth.json` (36 more symbols by tier) | `3771d1dd179fc32e` | `scripts/repro/breadth.py`, threads 4 | 2026-10-19 07:48 |

`scripts/realdata/summarize.py --results .realdata/results.json --headline .realdata/headline/headline.json --bench
.realdata/bench.json` prints every table below from those files. The engine is queuetruth 0.1.0 as built in the
working tree on 2026-10-18 (before the 0.1.0 commit `4be5240`), all builds after the zoo, calibration and feature engines stopped
detecting sequence gaps per partition (section 6). The latest full `make realdata` (`results.json` above, finished
2026-10-19 02:02 local time, UTC-4) ran after the self-exclusion fix of the scorer; it reproduces every RD-1 .. RD-12
number of the earlier 01:35 UTC run that this page also quotes, except where a section says otherwise. The headline,
oracle, RD-9 and RD-13 rerun files loaded the extension built at 21:30 on 2026-10-18, and the RD-13 rerun used
`queuetruth-core` built at 21:46. The engine was still changing between these runs; the raw-bytes results were since rerun on the 0.1.0 commit
(below), the et-artifact runs (RD-1 .. RD-13) were not. The qtbench quick suite (section 7) ran on the 20:03 build.

The raw-bytes runs (sections 2, 3 raw rows, 4, 4b, RD-9, RD-10) are newer than everything above and ran on one
build: `_core.abi3.so` sha256 `812f1e881489aec8...` (built 2026-10-19 03:10 local, after the per-shadow #312 ruling
and the NautilusTrader-verified faithful NT models), source-tree hash `fa118807fb672aa7...` (sha256 over the Rust and
Python sources and lock files, recorded by `reproduce.py`; these runs predate the first commit). Two pieces loaded
the next build (`4671359193252d96...`, 03:20, which changed only the crypto readers and Python files outside the zoo
and replay): the day-two oracle subprocess and the day-two half of the breadth run.

Release-commit rerun: `make reproduce` on commit `4be5240` (queuetruth 0.1.0; git HEAD recorded before and after the
run) with the installed extension `4671359193252d96...`, unchanged during the run, wrote `.realdata/repro/results.json`
(sha256 `446c6b09ba71dbd8cc8ca1d6889584a156639ac5c90fe050264e28bb229263cc`; 2026-10-19 08:05 to 08:25 UTC, 1,162 s at
4 threads). All 12 steps completed (6 `pass`: subset, ingest and replay on both days; 6 `recorded`: leaderboard and
oracle on both days, calibration, learned model). Compared with the published run
(`.realdata/repro/phase1/results.json`), the leaderboard on both days, the four calibrations (power3, Databento,
`nt_prob_touch`, `nt_queue_l2_prob_touch`), the learned model and the oracle are identical; only wall times differ.
(`reproduce.py` flags `build_changed_during_run` because the source-tree hash changed: unrelated files were added to
the working tree during the run; the extension and git HEAD did not change.) RD-13 is still pending a re-measurement
on a quiet machine.

## 1. Exactness against the earlier prototype (RD-1 .. RD-7)

Replays use `fill_rule = et` and `hidden_print_policy = ignore` (SPEC 6.8). Expected values come from
`expected.json`; "got" is the queuetruth run.

| RD | Check | Expected | Got | Pass |
|---|---|---|---|---|
| RD-1 | day one ingest: partitions, records | 4 (ids 13, 5291, 7451, 7992), 6,103,262 | 4 (same ids), 6,103,262 | yes |
| RD-1 | records by instrument | 1,737,379 / 1,346,429 / 2,341,016 / 678,438 | identical | yes |
| RD-1 | session open/close, tick, price exponent | 34,200 s / 57,600 s, 10,000,000, -9 | identical | yes |
| RD-1 | source prints, side-N prints | 211,867, 35,860 | 211,867, 35,860 (`hidden_prints`, verdict `clean`) | yes |
| RD-1b | day two ingest: records by instrument | 2,270,580 / 2,146,891 / 4,924,758 / 1,309,071 (10,651,300) | identical | yes |
| RD-2 | day one: records / events / trades | 6,103,262 / 5,423,833 / 211,867 | identical | yes |
| RD-2 | 8 book invariants | all 0 | all 0 | yes |
| RD-2 | executions checked, price-priority violations | 176,103, 0 | 176,103, 0 | yes |
| RD-2 | FIFO rank histogram (raw; sweep-adjusted) | 171,833 / 493 / 377 / 397 / 274 / 2,729; 173,854 / 253 / 267 / 225 / 199 / 1,305 | identical | yes |
| RD-2 | rank-0 share (raw, sweep-adjusted) | 0.9757528264708721, 0.9872290648086631 | identical (abs diff < 1e-12) | yes |
| RD-2 | sweeps, multi-fill sweeps | 87,901, 37,990 | identical | yes |
| RD-2 | first 60 s after open: checked / violations raw / adjusted | 2,157 / 617 / 203 | identical | yes |
| RD-2 | rank-0 counts in all 32 half-hour buckets | et `invariants.json` | identical | yes |
| RD-2 | trade parity: compared, mismatches | 211,867, 0 | 211,867, 0 | yes |
| RD-3 | BBO stream: engine parity rows, mismatches; per-instrument sha256 of rendered rows | 5,423,833, 0; 4 digests | 5,423,833, 0; 4 identical digests | yes |
| RD-4 | rank trace: rows; per-instrument sha256 (sweep ids relabelled) | 176,103; 4 digests | identical | yes |
| RD-5 | 3,000 et mirror shadows: filled (any fill), partial | 235, 2 | 235, 2 | yes |
| RD-5 | per-shadow mismatches vs et `shadow_fills.csv`; shadow digest | 0; `85646860...` | 0; identical | yes |
| RD-5 | sum filled qty; queue-trace rows and digest | 37,794; 11,680, `5fbd042f...` | identical | yes |
| RD-5 | SPEC-reference fills: rows, digest, end states, reasons | 366; `4d4048f5...`; cancelled 2,765, filled 233, censored 2; queue_depleted 114, trade_through 53, crossed 68 | identical | yes |
| RD-6 | day two: records / events / trades; invariants | 10,651,300 / 9,361,483 / 421,420; all 0 | identical | yes |
| RD-6 | executions checked; rank-0 share raw / adjusted | 314,369; 0.9867925908725097 / 0.9908865059850048 | identical | yes |
| RD-6 | sweeps, multi-fill; trade parity; BBO parity; BBO and rank digests | 161,037, 65,081; 421,420 / 0; 9,361,483 / 0; 8 digests | identical | yes |
| RD-6 | 3,000 et mirror shadows: filled, partial; mismatches; trace rows; fills rows | 195, 0; 0; 12,210; 317 | identical | yes |
| RD-7 | `qt.depth` and the `l2` visible book, rendered as et rows after every event: top 3 (both days), top 5 (day two) | 4 digests per day and depth | all identical, 0 unconsumed rows, 5,423,833 + 9,361,483 events | yes |

Totals: 16,754,562 records, 14,785,316 events, 633,287 source prints compared with 0 mismatches, 490,472 executions
checked with 0 price-priority violations.

## 2. Raw ITCH bytes (public reproduction)

`scripts/repro/reproduce.py` reproduces sections 3 (raw rows), 4, RD-9 and RD-10 from nothing but the public Nasdaq
URLs and a built queuetruth: it streams each day file once and keeps the 4 symbols (`scripts/realdata/itch_subset.py`;
the raw day is never stored), or reuses a subset already on disk when its content hashes to the pinned value, ingests
it with `qt.ingest(format="itch")`, and runs the replay checks, the leaderboard, the calibrations, the learned model
and the oracle. It writes one `results.json` with the sha256 of every input and output, the extension sha256 and a
source-tree hash. Wall time 1,387 s at 4 threads with the subsets on disk; streaming a day adds 4 to 16 min
(observed 4 to 16 MB/s from the Nasdaq server). Peak free-disk need about 0.5 GB.

| Check (raw ITCH bytes, `.realdata/repro/phase1/results.json`) | 2019-12-30 | 2020-01-30 |
|---|---|---|
| public file: bytes, sha256 (pinned in `itch_subset.py`) | 3,524,013,057, `ef03df46a27e6bda...` | 5,597,158,940, `8f5399155242f8d8...` |
| a second independent stream (the breadth run, section 4b): compressed and decompressed sha256, messages by type equal the pinned values | yes | yes |
| subset (AAPL, MSFT, SPY, TSLA): messages, sha256 of the ITCH frames | 5,425,849, `e85775e21d3a4cc4...` | 9,363,498, `8707219a75f2d78c...` |
| ingest: records (= RD-1 / RD-1b), data-quality verdict, wall time | 6,103,262, `clean`, 3.0 s | 10,651,300, `clean`, 5.6 s |
| hidden (`P`) + cross (`Q`, shares > 0) prints, each equal to the raw-stream count; sum = the CSV's side-`N` prints | 35,854 + 6 = 35,860 | 107,103 + 6 = 107,109 |
| replay: 8 invariants; executions checked, price-priority violations; rank-0 share raw / sweep-adjusted; trade parity | all 0; 176,103, 0; 0.97575 / 0.98723; 211,867 compared, 0 mismatches | all 0; 314,369, 0; 0.98679 / 0.99089; 421,420, 0 |
| leaderboard (section 4) vs the same run on the CSV dataset, same build: overall cells (17 models x 2 modes x 3 horizons), decile and bucket cells | 102 of 102 and 442 of 442 identical (bias, CI and n) | 102 of 102 and 442 of 442 identical |
| RD-9 (4 families) vs the CSV dataset, same build; RD-10 vs the earlier CSV run | RD-9: identical estimates, CIs, event counts and held-out scores; RD-10: identical BSS and CIs | (two-day runs) |

So on these two days the raw-bytes and CSV pipelines give the same leaderboard and calibration to the last digit; the separate hidden /
cross prints of the raw bytes do not move any model or calibration on these orders. Every
difference between section 4 below and the earlier published leaderboard comes from engine changes between the two
builds (`.realdata/repro/phase1/decompose.json`): mainly the bounds (`trade_through_only` -1.2 .. -3.7 pp before,
-6.0 .. -15.8 pp now; `front_of_queue` +0.6 .. +3.0 before, +0.9 .. +3.3 now), the faithful NautilusTrader models
(verified against nautilus_trader 1.231.0, section 4), and faithful hftbacktest shadows that reach #312 now being left
out per shadow (section 4); 19 of 204 overall cells moved by more than 0.5 pp, all of them bound or faithful
`nt_prob_touch` cells.

Checks against the prototype's artifacts (`scripts/realdata/itch_realbytes.py --et-root`, build of about 2026-10-19
00:35 UTC; optional, they need the prototype's CSVs and shadows):

| Check | 2019-12-30 | 2020-01-30 |
|---|---|---|
| ITCH-1: ingest from raw bytes; MBO rows and trades rendered as et CSV rows hash identically to et's `mbo.csv` / `trades.csv` per instrument; hidden (`P`) and cross (`Q`) prints counted separately and summing to et's side-N prints | pass | pass |
| ITCH-2: the RD-2/RD-3/RD-4 checks on the ITCH-ingested dataset | pass | pass |
| ITCH-5: the RD-5/RD-6 shadow checks on the ITCH-ingested dataset | pass | pass |

So the RD-1 .. RD-6 claims above also hold from raw ITCH bytes read by queuetruth's own reader. RD-7 has not been
repeated on the ITCH-ingested datasets.

## 3. Shadow-rule truth: independent matching-engine oracle (O-SH-5 on real data)

`scripts/oracle/run_oracle.py --run-qt` inserts each of the prototype's 3,000 mirror shadows per day as a real order
into an independent price-time matching engine fed with the reconstructed taker flow, and compares fills with
queuetruth's default `exact` rule. The result depends on the dataset and the shadow population, so each row names
both:

| Dataset | Shadows | Build | Day | Agree | Disagree (by category) | Filled qty, oracle vs queuetruth |
|---|---|---|---|---|---|---|
| prototype CSV (`.realdata/ds/nasdaq-<date>`) | prototype's 3,000 mirror shadows | 2026-10-18 21:30 | 2019-12-30 | 2,997 / 3,000 | 3 (hidden prints 3) | 37,994 vs 37,994 |
| prototype CSV | prototype's 3,000 mirror shadows | 2026-10-18 21:30 | 2020-01-30 | 2,997 / 3,000 | 3 (oracle divergence 3) | 22,237 vs 22,237 |
| raw ITCH bytes, real cross prints (`--cross-window-s 0`) | prototype's 3,000 mirror shadows | about 2026-10-19 00:35 UTC (older) | 2019-12-30 | 2,942 / 3,000 | 58 (crosses 57, off-price 1) | |
| raw ITCH bytes, real cross prints | prototype's 3,000 mirror shadows | about 2026-10-19 00:35 UTC (older) | 2020-01-30 | 2,958 / 3,000 | 42 (crosses 39, oracle divergence 3) | |
| raw ITCH bytes, real cross prints (`reproduce.py`) | `qt.mirror_real_orders`, 3,000, seed 0 | 2026-10-19 03:10 | 2019-12-30 | 2,992 / 3,000 | 8 (oracle divergence 6, crosses 2) | 169,123 vs 169,123 |
| raw ITCH bytes, real cross prints (`reproduce.py`) | `qt.mirror_real_orders`, 3,000, seed 0 | 2026-10-19 03:20 | 2020-01-30 | 2,985 / 3,000 | 15 (oracle divergence 12, crosses 2, hidden prints 1) | 171,093 vs 171,093 |

The public rows use queuetruth's own mirror sampler, which stratifies by outcome (1,498 and 1,499 of the 3,000 fill in
the oracle, against 233 and 195 of the prototype's mirror shadows), so they test far more fills than the CSV rows and
are not comparable one for one. Every disagreement is in a fill time or the queue-ahead trajectory; the filled
quantities agree in total. 5 of the 6 day-one oracle divergences are within 40 s of the open. The older raw-bytes rows
predate the liquidity rulings of the current build and have not been rerun with the prototype's shadows. An earlier
CSV run on the 19:53 build (before the last engine rule changes) agreed on 2,992 and 2,996.

The oracle's own limits (static counterfactual, invisible taker volume, hidden liquidity, auctions) are listed in
[How queuetruth is tested](testing.md).

## 4. How far are L2 queue models from exact truth? (RD-8 extended, the headline experiment)

Population: per day, 3,000 shadows sampled by `qt.sample_shadows` (seed 0; 750 per instrument; touch-relative prices with offsets 0 / -1
/ -2 / +1 ticks weighted 0.6 / 0.2 / 0.1 / 0.1; size 1 lot or the median order size, half each; submit times uniform
between 300 s after the open and 600 s before the close; planned life 300 s). Every shadow is a never-cancel order for the 60 s
horizon (audit A2). Truth is the zoo's exact arm. Bias = mean model fill probability minus the exact fill rate at the
horizon, with the zoo's day-block bootstrap 95% CI, in percentage points.

Exact fill rates: 2019-12-30 331 / 1,453 / 2,250 of 3,000 at 1 s / 10 s / 60 s (11.0%, 48.4%, 75.0%); 2020-01-30 555
/ 1,746 / 2,422 (18.5%, 58.2%, 80.7%). Zoo wall time for 17 models x 3,000 shadows at 4 threads: 11.6-12.8 s (day one),
15.7-17.3 s (day two). The tables below are the raw-bytes run (`.realdata/repro/phase1/headline/`, section 2), which
equals the CSV run on the same build cell for cell.

What it shows on these orders:

- NautilusTrader's touch fill model with the default `prob_fill_on_limit = 1` (`nt_prob_touch[p=1]`) is the most
  optimistic real model: +3.13 pp at 1 s on day one (14.17% vs 11.03% exact) and +2.80 pp on day two. This is
  verified against the real engine: nautilus_trader 1.231.0's `BacktestEngine`, fed the same L2 view as
  `OrderBookDelta`s with `FillModel(prob_fill_on_limit=1)` and the same orders, fills exactly the same orders as the
  faithful zoo model at 1 s, 10 s and 60 s on both days and on the synthetic fixture (100% agreement; Nautilus's own
  bias +3.13 pp [+2.53, +3.77] and +2.80 pp [+2.23, +3.37] at 1 s, order-level bootstrap). See the NautilusTrader
  cross-check below.
- The hftbacktest power and log models stay within about 1 pp of exact at every horizon on both days, mostly slightly
  pessimistic; `hbt_risk_adverse` is pessimistic by up to 1.3 pp.
- `nt_queue_l2` is within 0.8 pp on both days in faithful mode, and agrees with Nautilus's `queue_position=True` on
  100% of orders on a delta feed.
- The bounds span `front_of_queue` +0.9 .. +3.3 pp and `trade_through_only` -6.0 .. -15.8 pp. Faithful
  `nt_prob_touch[p=1]` reaches `front_of_queue` in all six day and horizon cells. The bounds bracket the exact arm order by order by construction (O-ZOO-6, tested on
  scenario and synthetic tapes); see the RD-8 note below for the real-data record.
- `hbt_l3_fifo` (MBO input) faithful, which reproduces hftbacktest's L3 queue model, is pessimistic (-1.3 .. -3.4
  pp); in corrected mode it equals the exact arm on every shadow (bias 0.00, as O-ZOO-4 requires).

These shadows are small orders at or near the touch of four of the most liquid US stocks, where most of them fill
within a minute; on 36 more names (section 4b) the NautilusTrader default overfills most on deep-queue names, while
order size barely moves first-fill bias. Liquidity buckets are terciles of the
traded volume in the 60 s before the shadow went live; queue deciles are deciles of exact queue ahead at entry, both
pooled over the four instruments of the day. Per-decile cells hold 92 to 520 shadows.

**faithful mode** (fill-rate bias in pp, 95% CI)

| model | 2019-12-30 1 s | 2019-12-30 10 s | 2019-12-30 60 s | 2020-01-30 1 s | 2020-01-30 10 s | 2020-01-30 60 s |
|---|---|---|---|---|---|---|
| `databento_mc[k=-0.8;particles=100]` | -0.06 [-0.33, +0.18] | -0.35 [-0.65, -0.06] | -0.61 [-0.94, -0.25] | -0.32 [-0.77, +0.09] | -0.60 [-0.96, -0.30] | -0.26 [-0.62, +0.04] |
| `databento_mc[k=-0.95;particles=100]` | -0.07 [-0.32, +0.17] | -0.55 [-0.88, -0.22] | -0.74 [-1.09, -0.36] | -0.35 [-0.78, +0.05] | -0.64 [-1.00, -0.34] | -0.30 [-0.64, +0.00] |
| `front_of_queue` | +3.13 [+2.63, +3.64] | +3.30 [+2.47, +4.14] | +1.43 [+1.01, +1.92] | +2.80 [+2.15, +3.55] | +2.03 [+1.51, +2.54] | +0.93 [+0.50, +1.33] |
| `hbt_l3_fifo` | -1.30 [-1.71, -0.84] | -1.93 [-2.49, -1.42] | -1.67 [-2.26, -1.10] | -2.17 [-2.85, -1.55] | -3.43 [-3.78, -3.11] | -1.83 [-2.41, -1.29] |
| `hbt_log` | -0.23 [-0.52, +0.06] | -0.62 [-0.97, -0.28] | -0.66 [-0.97, -0.38] | -0.50 [-0.88, -0.14] | -0.97 [-1.41, -0.56] | -0.28 [-0.64, +0.03] |
| `hbt_log2` | -0.27 [-0.51, -0.03] | -0.48 [-0.89, -0.10] | -0.70 [-1.11, -0.24] | -0.54 [-0.86, -0.21] | -0.83 [-1.19, -0.45] | -0.32 [-0.67, -0.03] |
| `hbt_power2[n=2]` | +0.07 [-0.26, +0.38] | +0.74 [+0.22, +1.28] | +0.04 [-0.30, +0.38] | -0.37 [-0.78, +0.07] | -0.53 [-0.90, -0.21] | -0.15 [-0.58, +0.24] |
| `hbt_power3[n=1]` | +0.07 [-0.26, +0.37] | +0.31 [-0.15, +0.87] | -0.11 [-0.43, +0.25] | -0.47 [-0.84, -0.10] | -0.49 [-0.86, -0.14] | -0.18 [-0.61, +0.18] |
| `hbt_power3[n=2]` | -0.13 [-0.37, +0.10] | -0.21 [-0.63, +0.20] | -0.53 [-0.92, -0.14] | -0.50 [-0.86, -0.14] | -0.65 [-1.04, -0.30] | -0.25 [-0.62, +0.07] |
| `hbt_power3[n=3]` | -0.27 [-0.51, -0.03] | -0.55 [-0.97, -0.13] | -0.84 [-1.29, -0.34] | -0.57 [-0.88, -0.28] | -0.83 [-1.20, -0.47] | -0.35 [-0.68, -0.07] |
| `hbt_power[n=1]` | +0.07 [-0.26, +0.37] | +0.31 [-0.15, +0.87] | -0.11 [-0.43, +0.25] | -0.47 [-0.84, -0.10] | -0.49 [-0.86, -0.14] | -0.18 [-0.61, +0.18] |
| `hbt_power[n=2]` | -0.07 [-0.34, +0.20] | +0.00 [-0.35, +0.31] | -0.39 [-0.71, -0.07] | -0.44 [-0.82, -0.03] | -0.65 [-1.04, -0.30] | -0.25 [-0.62, +0.07] |
| `hbt_power[n=3]` | -0.03 [-0.33, +0.24] | -0.03 [-0.43, +0.30] | -0.46 [-0.83, -0.10] | -0.47 [-0.83, -0.11] | -0.66 [-1.03, -0.33] | -0.25 [-0.63, +0.07] |
| `hbt_risk_adverse` | -0.37 [-0.58, -0.19] | -1.04 [-1.34, -0.72] | -1.18 [-1.51, -0.84] | -0.77 [-1.09, -0.49] | -1.27 [-1.63, -0.87] | -0.51 [-0.83, -0.25] |
| `nt_prob_touch[p=1]` | +3.13 [+2.63, +3.64] | +3.30 [+2.47, +4.14] | +1.43 [+1.01, +1.92] | +2.80 [+2.15, +3.55] | +2.03 [+1.51, +2.54] | +0.93 [+0.50, +1.33] |
| `nt_queue_l2` | -0.10 [-0.25, +0.00] | -0.77 [-0.98, -0.54] | -0.70 [-1.03, -0.38] | -0.10 [-0.25, +0.00] | -0.17 [-0.38, +0.04] | -0.23 [-0.42, -0.07] |
| `trade_through_only` | -5.97 [-7.71, -4.48] | -15.77 [-16.86, -14.63] | -10.13 [-12.00, -8.37] | -9.90 [-11.81, -8.21] | -12.93 [-13.80, -11.98] | -6.57 [-7.49, -5.60] |

**corrected mode** (fill-rate bias in pp, 95% CI)

| model | 2019-12-30 1 s | 2019-12-30 10 s | 2019-12-30 60 s | 2020-01-30 1 s | 2020-01-30 10 s | 2020-01-30 60 s |
|---|---|---|---|---|---|---|
| `databento_mc[k=-0.8;particles=100]` | -0.34 [-0.56, -0.15] | -0.81 [-1.10, -0.47] | -0.93 [-1.27, -0.62] | -0.71 [-1.02, -0.42] | -1.06 [-1.38, -0.71] | -0.44 [-0.73, -0.21] |
| `databento_mc[k=-0.95;particles=100]` | -0.37 [-0.58, -0.19] | -0.97 [-1.27, -0.65] | -1.06 [-1.38, -0.73] | -0.77 [-1.08, -0.48] | -1.18 [-1.52, -0.80] | -0.47 [-0.75, -0.23] |
| `front_of_queue` | +3.13 [+2.63, +3.64] | +3.30 [+2.47, +4.14] | +1.43 [+1.01, +1.92] | +2.80 [+2.15, +3.55] | +2.03 [+1.51, +2.54] | +0.93 [+0.50, +1.33] |
| `hbt_l3_fifo` | +0.00 [+0.00, +0.00] | +0.00 [+0.00, +0.00] | +0.00 [+0.00, +0.00] | +0.00 [+0.00, +0.00] | +0.00 [+0.00, +0.00] | +0.00 [+0.00, +0.00] |
| `hbt_log` | +0.03 [-0.32, +0.40] | +0.47 [-0.03, +1.01] | +0.00 [-0.33, +0.37] | -0.43 [-0.82, -0.06] | -0.47 [-0.86, -0.10] | -0.10 [-0.50, +0.26] |
| `hbt_log2` | -0.27 [-0.51, -0.03] | -0.53 [-0.93, -0.13] | -0.73 [-1.16, -0.29] | -0.53 [-0.86, -0.21] | -0.80 [-1.15, -0.44] | -0.30 [-0.62, -0.03] |
| `hbt_power2[n=2]` | +0.10 [-0.23, +0.42] | +0.60 [+0.10, +1.13] | +0.00 [-0.31, +0.37] | -0.37 [-0.77, +0.07] | -0.43 [-0.79, -0.10] | -0.13 [-0.53, +0.22] |
| `hbt_power3[n=1]` | +0.07 [-0.26, +0.37] | +0.27 [-0.20, +0.80] | -0.17 [-0.46, +0.17] | -0.47 [-0.83, -0.10] | -0.53 [-0.90, -0.19] | -0.17 [-0.56, +0.17] |
| `hbt_power3[n=2]` | -0.13 [-0.37, +0.10] | -0.23 [-0.66, +0.17] | -0.53 [-0.90, -0.16] | -0.53 [-0.86, -0.21] | -0.67 [-1.02, -0.33] | -0.23 [-0.59, +0.07] |
| `hbt_power3[n=3]` | -0.27 [-0.51, -0.03] | -0.53 [-0.94, -0.13] | -0.80 [-1.22, -0.32] | -0.57 [-0.87, -0.28] | -0.80 [-1.16, -0.45] | -0.33 [-0.64, -0.07] |
| `hbt_power[n=1]` | +0.07 [-0.26, +0.37] | +0.27 [-0.20, +0.80] | -0.17 [-0.46, +0.17] | -0.47 [-0.83, -0.10] | -0.53 [-0.90, -0.19] | -0.17 [-0.56, +0.17] |
| `hbt_power[n=2]` | -0.03 [-0.33, +0.24] | -0.03 [-0.37, +0.29] | -0.40 [-0.69, -0.10] | -0.47 [-0.82, -0.10] | -0.67 [-1.02, -0.33] | -0.23 [-0.59, +0.07] |
| `hbt_power[n=3]` | -0.03 [-0.33, +0.24] | -0.07 [-0.44, +0.27] | -0.47 [-0.83, -0.13] | -0.47 [-0.82, -0.10] | -0.63 [-1.00, -0.32] | -0.23 [-0.59, +0.07] |
| `hbt_risk_adverse` | -0.37 [-0.58, -0.19] | -1.00 [-1.29, -0.70] | -1.10 [-1.41, -0.78] | -0.77 [-1.08, -0.48] | -1.20 [-1.54, -0.83] | -0.47 [-0.75, -0.23] |
| `nt_prob_touch[p=1]` | +3.00 [+2.44, +3.56] | +2.87 [+1.89, +3.89] | +0.77 [+0.36, +1.24] | +2.23 [+1.42, +3.11] | +1.10 [+0.60, +1.56] | +0.60 [+0.03, +1.13] |
| `nt_queue_l2` | -0.37 [-0.58, -0.19] | -1.00 [-1.29, -0.70] | -1.13 [-1.44, -0.82] | -0.77 [-1.08, -0.48] | -1.27 [-1.59, -0.91] | -0.53 [-0.83, -0.26] |
| `trade_through_only` | -5.97 [-7.71, -4.48] | -15.77 [-16.86, -14.63] | -10.13 [-12.00, -8.37] | -9.90 [-11.81, -8.21] | -12.93 [-13.80, -11.98] | -6.57 [-7.49, -5.60] |

**By queue-ahead decile and liquidity bucket** (faithful mode, 60 s, bias in pp; decile 0 = least queue ahead)

| model | day | queue decile 0 .. 9 | liquidity bucket 0 / 1 / 2 |
|---|---|---|---|
| `databento_mc[k=-0.8;particles=100]` | 2019-12-30 | +0.0 +0.2 -2.0 -0.5 +0.3 -1.7 -0.5 -2.0 -0.3 +0.1 | -0.5 / -0.8 / -0.5 |
| `databento_mc[k=-0.8;particles=100]` | 2020-01-30 | +0.0 -0.4 +0.4 -1.6 +0.0 +0.1 +0.4 -0.9 +0.0 -0.6 | -0.1 / -0.5 / -0.2 |
| `databento_mc[k=-0.95;particles=100]` | 2019-12-30 | +0.0 +0.0 -2.0 -0.5 +0.2 -1.7 -0.8 -2.2 -0.6 -0.1 | -0.6 / -1.0 / -0.6 |
| `databento_mc[k=-0.95;particles=100]` | 2020-01-30 | +0.0 -0.4 +0.4 -1.6 +0.0 +0.0 +0.4 -1.0 +0.0 -0.6 | -0.1 / -0.5 / -0.3 |
| `front_of_queue` | 2019-12-30 | +0.0 +1.9 +0.3 +2.1 +0.7 +2.1 +1.2 +2.3 +2.0 +2.4 | +1.7 / +1.1 / +1.5 |
| `front_of_queue` | 2020-01-30 | +0.8 +0.4 +0.4 +0.3 +1.1 +1.7 +0.6 +1.4 +1.5 +1.8 | +1.5 / +0.5 / +0.8 |
| `hbt_l3_fifo` | 2019-12-30 | +0.9 +0.6 -4.5 -3.1 -0.9 -4.7 +0.0 -2.9 -1.2 -1.0 | -2.1 / -1.6 / -1.3 |
| `hbt_l3_fifo` | 2020-01-30 | +0.0 -1.3 -0.8 -5.9 -1.1 -2.9 -0.6 -1.4 -1.2 -2.4 | -2.3 / -1.5 / -1.7 |
| `hbt_log` | 2019-12-30 | +0.0 -0.3 -2.3 -0.5 -0.2 -1.3 -0.4 -2.0 +0.3 +0.4 | -0.7 / -0.7 / -0.5 |
| `hbt_log` | 2020-01-30 | +0.0 -0.5 +0.0 -1.6 +0.0 +0.0 +0.6 -1.1 +0.3 -0.6 | -0.2 / -0.5 / -0.1 |
| `hbt_log2` | 2019-12-30 | +0.0 +0.3 -2.1 -1.1 +0.5 -1.8 -0.4 -2.4 -0.6 +0.0 | -0.6 / -1.1 / -0.4 |
| `hbt_log2` | 2020-01-30 | +0.0 -0.5 +0.4 -1.6 +0.0 +0.6 +0.4 -1.5 +0.0 -0.6 | -0.2 / -0.4 / -0.3 |
| `hbt_power2[n=2]` | 2019-12-30 | +0.0 +0.7 -2.1 +1.1 +0.5 -0.5 +0.4 -1.0 +0.9 +0.7 | +0.0 / -0.1 / +0.2 |
| `hbt_power2[n=2]` | 2020-01-30 | +0.0 -0.5 +0.4 -1.6 +0.0 +0.6 +0.7 -0.8 +0.3 -0.3 | +0.0 / -0.4 / +0.0 |
| `hbt_power3[n=1]` | 2019-12-30 | +0.0 +0.3 -2.1 +0.0 +0.5 -0.9 +0.4 -1.0 +0.9 +0.7 | -0.1 / -0.2 / +0.0 |
| `hbt_power3[n=1]` | 2020-01-30 | +0.0 -0.5 +0.4 -1.6 +0.0 +0.6 +0.6 -0.7 +0.3 -0.6 | +0.0 / -0.4 / -0.1 |
| `hbt_power3[n=2]` | 2019-12-30 | +0.0 +0.0 -2.0 -0.5 +0.5 -1.3 -0.4 -2.4 +0.3 +0.4 | -0.5 / -0.8 / -0.2 |
| `hbt_power3[n=2]` | 2020-01-30 | +0.0 -0.5 +0.4 -1.6 +0.0 +0.6 +0.6 -1.4 +0.3 -0.6 | -0.1 / -0.4 / -0.2 |
| `hbt_power3[n=3]` | 2019-12-30 | +0.0 +0.0 -2.3 -1.1 +0.0 -1.8 -0.4 -2.7 -0.3 +0.0 | -0.8 / -1.2 / -0.5 |
| `hbt_power3[n=3]` | 2020-01-30 | +0.0 -0.5 +0.0 -1.6 +0.0 +0.6 +0.4 -1.5 +0.0 -0.6 | -0.3 / -0.4 / -0.3 |
| `hbt_power[n=1]` | 2019-12-30 | +0.0 +0.3 -2.1 +0.0 +0.5 -0.9 +0.4 -1.0 +0.9 +0.7 | -0.1 / -0.2 / +0.0 |
| `hbt_power[n=1]` | 2020-01-30 | +0.0 -0.5 +0.4 -1.6 +0.0 +0.6 +0.6 -0.7 +0.3 -0.6 | +0.0 / -0.4 / -0.1 |
| `hbt_power[n=2]` | 2019-12-30 | +0.0 +0.0 -2.0 -0.5 +0.5 -1.3 +0.0 -2.0 +0.6 +0.7 | -0.4 / -0.6 / -0.1 |
| `hbt_power[n=2]` | 2020-01-30 | +0.0 -0.5 +0.4 -1.6 +0.0 +0.6 +0.6 -1.4 +0.3 -0.6 | -0.1 / -0.4 / -0.2 |
| `hbt_power[n=3]` | 2019-12-30 | +0.0 +0.0 -2.0 -0.5 +0.2 -1.3 +0.0 -2.0 +0.3 +0.7 | -0.5 / -0.6 / -0.2 |
| `hbt_power[n=3]` | 2020-01-30 | +0.0 -0.5 +0.4 -1.6 +0.0 +0.6 +0.6 -1.4 +0.3 -0.6 | -0.1 / -0.4 / -0.2 |
| `hbt_risk_adverse` | 2019-12-30 | +0.0 -0.3 -2.4 -1.6 -0.2 -1.8 -0.4 -3.4 -0.6 -1.1 | -1.2 / -1.3 / -1.1 |
| `hbt_risk_adverse` | 2020-01-30 | +0.0 -0.5 +0.0 -1.6 -0.4 +0.0 +0.0 -1.5 +0.0 -0.7 | -0.5 / -0.6 / -0.4 |
| `nt_prob_touch[p=1]` | 2019-12-30 | +0.0 +1.9 +0.3 +2.1 +0.7 +2.1 +1.2 +2.3 +2.0 +2.4 | +1.7 / +1.1 / +1.5 |
| `nt_prob_touch[p=1]` | 2020-01-30 | +0.8 +0.4 +0.4 +0.3 +1.1 +1.7 +0.6 +1.4 +1.5 +1.8 | +1.5 / +0.5 / +0.8 |
| `nt_queue_l2` | 2019-12-30 | +0.0 -0.3 -0.3 -0.5 -0.2 -0.9 -1.2 -2.3 -0.3 -1.4 | -0.6 / -0.7 / -0.8 |
| `nt_queue_l2` | 2020-01-30 | +0.0 +0.0 +0.0 +0.0 -0.7 +0.0 +0.0 -0.7 +0.0 -0.9 | -0.5 / +0.0 / -0.2 |
| `trade_through_only` | 2019-12-30 | -16.8 -11.3 -9.0 -7.2 -9.5 -11.5 -11.2 -14.8 -7.8 -10.6 | -11.6 / -10.4 / -8.4 |
| `trade_through_only` | 2020-01-30 | -12.4 -7.5 -8.4 -3.4 -7.2 -5.2 -7.1 -8.7 -5.8 -6.0 | -8.0 / -6.0 / -5.7 |

**hftbacktest #312 aborts** (faithful mode, shadows of 3,000 left out of the model's metrics)

| model | 2019-12-30 | 2020-01-30 |
|---|---|---|
| `hbt_log` | 179 | 232 |
| `hbt_log2` | 195 | 245 |
| `hbt_power2[n=2]` | 323 | 342 |
| `hbt_power3[n=1]` | 261 | 294 |
| `hbt_power3[n=2]` | 191 | 225 |
| `hbt_power3[n=3]` | 197 | 236 |
| `hbt_power[n=1]` | 261 | 294 |
| `hbt_power[n=2]` | 186 | 217 |
| `hbt_power[n=3]` | 181 | 229 |
| `hbt_risk_adverse` | 268 | 313 |

Faithful hftbacktest models reproduce hftbacktest's partial-fill exchange, which aborts a backtest with
`InvalidOrderStatus` on these orders (issue #312). The zoo now stops only the affected shadow at that event and
leaves it out of that model's metrics (the corrected models are not affected); the counts above are how many of the
3,000 orders hftbacktest itself could not finish, 6.0% to 11.4% per model.

RD-8 checks on day one (`.realdata/results.json`): 17 models in both modes on the 3,000 sampled shadows and on
the prototype's 3,000 real-order mirror shadows (the cancel-bearing population, reported separately per A2); the
count bracket holds everywhere (shadows filled by the horizon, sampled: `front_of_queue` 2,614, exact 2,600,
`trade_through_only` 2,561; mirror: 277, 229, 228); `hbt_l3_fifo` corrected equals exact on all rows of both. That
run predates the bounds fix: shadow by shadow, 5 of the sampled shadows filled in the exact arm but not under
`front_of_queue` (0 on the mirror shadows). The bounds were then fixed so that the exact arm's fills are a subset of
`front_of_queue`'s by construction (O-ZOO-6); an order-by-order bracket property test passes on the scenario and
synthetic tapes, and on real day one the per-order violations went from 5 to 0 (3,000 sampled shadows, both modes).
In the decile and bucket table above no `front_of_queue` cell is negative and no `trade_through_only` cell is
positive.

### NautilusTrader cross-check

`scripts/nt_crosscheck/run.py` (its own venv with nautilus_trader 1.231.0; it calls only Nautilus's public Python API)
feeds the zoo's L2 view of each day to Nautilus's `BacktestEngine` as `OrderBookDelta`s, submits the same 3,000
never-cancel orders per day (same instrument, side, price, size and live time; one order per isolated window, since
Nautilus shares a print's volume between a strategy's resting orders), and compares each order's first fill with the
faithful zoo model. 121 (day one) and 84 (day two) of the orders are rejected or would cross at their live time and
stay unfilled in both. Bias in pp against the exact arm, with an order-level bootstrap 95% CI.

| Nautilus config | zoo model | day | NT fill rate at 1 s / 10 s / 60 s | NT bias at 1 s | orders that agree at 0.1 / 1 / 10 / 60 s |
|---|---|---|---|---|---|
| `FillModel(prob_fill_on_limit=1)` | `nt_prob_touch[p=1]` | 2019-12-30 | 14.17% / 51.73% / 76.43% | +3.13 [+2.53, +3.77] | 100% at every horizon |
| `FillModel(prob_fill_on_limit=1)` | `nt_prob_touch[p=1]` | 2020-01-30 | 21.30% / 60.23% / 81.67% | +2.80 [+2.23, +3.37] | 100% at every horizon |
| `queue_position=True`, `p = 1` | `nt_queue_l2` | 2019-12-30 | 10.93% / 47.67% / 74.30% | -0.10 [-0.23, +0.00] | 100% at every horizon |
| `queue_position=True`, `p = 1` | `nt_queue_l2` | 2020-01-30 | 18.40% / 58.03% / 80.50% | -0.10 [-0.23, +0.00] | 100% at every horizon |

On the synthetic fixture (400 orders) both configurations also agree on 100% of orders at every horizon. With
`prob_fill_on_limit = 0.29` Nautilus draws each touch fill at random, so orders are not compared one by one; its
realised fill rates are within 0.5 pp of the zoo's expected rates on both days.

Feed type matters. Nautilus resets `queue_position` whenever an `OrderBookDepth10` arrives, so fed the same days as
depth-10 snapshots its queue model fills like the touch model: 13.90% / 51.70% / 76.43% on day one (+2.87, +3.27,
+1.43 pp against exact) and +2.67, +2.03, +0.93 pp on day two, while the touch model is unchanged. The faithful NT
models describe a delta feed only.

## 4b. Breadth: 36 more Nasdaq symbols by liquidity tier

`scripts/repro/breadth.py` (`.realdata/repro/breadth/breadth.json`) streams each public day once more and keeps 36
symbols in five tiers fixed in advance (none of the four names above), then per tier ingests the raw bytes, runs the
replay checks and the 17-model zoo on three never-cancel shadow populations (seed 0, equal allocation over the tier's
names, same submit window and 300 s planned life as section 4): `standard` (the section 4 defaults), `large` (1,000
or 2,000 shares, about 10x and 20x the 100-share median order, same touch offsets) and `deep` (the standard sizes 3
or 5 ticks behind the touch). 3,000 standard and 2,000 large and deep shadows per tier and day. Day one of the run
loaded the 03:10 build, day two the 03:20 build (section Provenance). Subsets: 11,775,708 messages (sha256 of the ITCH
frames `bae3ee2c91889254...`) and 21,130,692 messages (`bfedb869ac7e5fd7...`); all 36 names were in both directories.

| tier | names | full-day ITCH messages per name, 2019-12-30 (rank among 8,906 symbols) |
|---|---|---|
| mega | AMZN, GOOGL, FB, NVDA, INTC | 536k (46), 781k (17), 545k (42), 456k (58), 753k (21) |
| deep_queue | BAC, F, GE, SIRI, T, PFE | 451k (59), 164k (347), 222k (220), 72k (838), 302k (129), 335k (111) |
| etf | QQQ, IWM, XLF, EEM, TLT | 2,377k (1), 1,941k (3), 249k (177), 273k (158), 530k (48) |
| mid | ROKU, ETSY, DOCU, TTD, ULTA, FFIV, JBHT, CHRW, ZBRA, LULU | 447k (62), 103k (599), 105k (590), 130k (480), 134k (454), 52k (1150), 54k (1110), 74k (818), 52k (1160), 117k (538) |
| small | PLUG, GPRO, SPWR, OSTK, FCEL, MARA, RIOT, HIMX, KTOS, AMRS | 80k (764), 60k (997), 119k (532), 40k (1496), 132k (468), 2k (5954), 7k (4324), 14k (3170), 46k (1306), 18k (2744) |

Exactness on the new names (raw ITCH bytes, rank oracle and trade parity as in section 2):

| tier | day | names | records | events | executions checked | price-priority violations | rank-0 share (raw / sweep-adj.) | invariants | trade parity mismatches |
|---|---|---|---|---|---|---|---|---|---|
| mega | 2019-12-30 | 5 | 3,528,601 | 3,069,183 | 86,620 | 0 | 0.9810 / 0.9924 | all 0 | 0 of 115,518 |
| mega | 2020-01-30 | 5 | 6,734,834 | 5,772,640 | 190,410 | 0 | 0.9847 / 0.9932 | all 0 | 0 of 267,238 |
| deep_queue | 2019-12-30 | 6 | 1,654,087 | 1,542,933 | 27,540 | 0 | 0.9640 / 0.9648 | all 0 | 0 of 38,306 |
| deep_queue | 2020-01-30 | 6 | 3,357,713 | 3,075,274 | 71,157 | 0 | 0.9659 / 0.9661 | all 0 | 0 of 95,280 |
| etf | 2019-12-30 | 5 | 5,716,346 | 5,368,202 | 60,043 | 0 | 0.9594 / 0.9616 | all 0 | 0 of 66,881 |
| etf | 2020-01-30 | 5 | 10,950,923 | 10,223,968 | 113,615 | 0 | 0.9758 / 0.9773 | all 0 | 0 of 125,643 |
| mid | 2019-12-30 | 10 | 1,524,086 | 1,264,935 | 56,101 | 0 | 0.9897 / 0.9991 | all 0 | 0 of 71,495 |
| mid | 2020-01-30 | 10 | 1,765,010 | 1,438,400 | 54,672 | 0 | 0.9970 / 0.9988 | all 0 | 0 of 69,285 |
| small | 2019-12-30 | 10 | 572,568 | 512,614 | 19,335 | 0 | 0.9537 / 0.9683 | all 0 | 0 of 24,302 |
| small | 2020-01-30 | 10 | 661,600 | 602,557 | 16,029 | 0 | 0.9714 / 0.9739 | all 0 | 0 of 20,324 |

`front_of_queue` and faithful `nt_prob_touch[p=1]` are identical in every tier, population, day and horizon, so the
tables show one column for both. All 17 models produced rows everywhere; each faithful hftbacktest model left out
0.05% to 8.2% of a population's shadows at the #312 event (`hbt_power3[n=2]`: 12 to 195 of 3,000 standard shadows).


**standard shadows** (faithful mode, 60 s; bias in pp, 95% CI)

| tier | day | exact fill rate | `nt_prob_touch[p=1]` | `nt_queue_l2` | `hbt_power3[n=2]` | `hbt_risk_adverse` | `databento_mc[k=-0.8;particles=100]` | `trade_through_only` |
|---|---|---|---|---|---|---|---|---|
| mega | 2019-12-30 | 58.5% | +1.53 [+1.12, +1.97] | -0.70 [-0.98, -0.45] | -1.61 [-1.94, -1.28] | -2.09 [-2.33, -1.84] | -1.56 [-1.86, -1.25] | -10.03 [-11.02, -8.95] |
| mega | 2020-01-30 | 68.6% | +0.97 [+0.56, +1.35] | -0.27 [-0.42, -0.13] | -1.17 [-1.53, -0.85] | -1.58 [-2.03, -1.17] | -1.23 [-1.68, -0.81] | -7.57 [-8.95, -6.20] |
| deep_queue | 2019-12-30 | 17.0% | +8.60 [+6.49, +10.59] | -0.60 [-0.89, -0.31] | +0.67 [+0.24, +1.13] | -0.27 [-0.47, -0.10] | +0.29 [+0.10, +0.46] | -11.03 [-13.10, -8.97] |
| deep_queue | 2020-01-30 | 28.1% | +5.80 [+4.40, +7.24] | -1.27 [-1.82, -0.75] | +0.94 [+0.39, +1.48] | -0.48 [-0.81, -0.19] | +0.46 [-0.07, +1.01] | -13.83 [-15.50, -12.36] |
| etf | 2019-12-30 | 43.0% | +3.17 [+2.32, +4.12] | -0.60 [-0.95, -0.29] | +0.03 [-0.16, +0.26] | -0.41 [-0.73, -0.17] | +0.25 [+0.09, +0.40] | -15.90 [-17.95, -14.08] |
| etf | 2020-01-30 | 60.1% | +0.80 [+0.39, +1.20] | -0.20 [-0.37, -0.06] | +0.00 [-0.13, +0.13] | -0.10 [-0.21, +0.00] | +0.06 [-0.07, +0.20] | -17.83 [-19.10, -16.60] |
| mid | 2019-12-30 | 38.0% | +3.33 [+2.73, +4.03] | -0.30 [-0.51, -0.13] | -1.16 [-1.58, -0.76] | -1.67 [-2.01, -1.42] | -0.86 [-1.29, -0.45] | -7.87 [-8.84, -6.82] |
| mid | 2020-01-30 | 41.0% | +3.90 [+3.11, +4.80] | -0.87 [-1.17, -0.56] | -1.07 [-1.72, -0.39] | -2.10 [-2.64, -1.51] | -0.60 [-1.17, -0.02] | -9.47 [-10.97, -7.98] |
| small | 2019-12-30 | 11.2% | +3.63 [+2.77, +4.48] | -0.27 [-0.41, -0.13] | +0.17 [-0.26, +0.57] | -0.20 [-0.51, +0.06] | -0.03 [-0.36, +0.24] | -7.67 [-9.32, -6.36] |
| small | 2020-01-30 | 13.5% | +3.67 [+3.02, +4.46] | -0.37 [-0.58, -0.19] | +0.10 [-0.03, +0.27] | -0.30 [-0.51, -0.11] | -0.00 [-0.13, +0.11] | -8.80 [-10.18, -7.53] |

**large shadows** (faithful mode, 60 s; bias in pp)

| tier | day | exact fill rate | `nt_prob_touch[p=1]` | `nt_queue_l2` | `hbt_power3[n=2]` | `hbt_risk_adverse` | `databento_mc[k=-0.8;particles=100]` | `trade_through_only` |
|---|---|---|---|---|---|---|---|---|
| mega | 2019-12-30 | 59.0% | +1.55 | -0.70 | -1.65 | -2.00 | -1.50 | -9.80 |
| mega | 2020-01-30 | 67.7% | +1.15 | -0.35 | -1.10 | -1.50 | -1.26 | -7.55 |
| deep_queue | 2019-12-30 | 17.2% | +8.00 | -0.70 | +0.60 | -0.25 | +0.27 | -10.95 |
| deep_queue | 2020-01-30 | 28.1% | +6.00 | -1.50 | +0.90 | -0.60 | +0.36 | -13.60 |
| etf | 2019-12-30 | 42.4% | +3.00 | -0.55 | +0.20 | -0.35 | +0.19 | -15.85 |
| etf | 2020-01-30 | 60.3% | +0.70 | -0.15 | -0.05 | -0.10 | -0.00 | -18.30 |
| mid | 2019-12-30 | 37.4% | +3.70 | -0.25 | -1.15 | -1.60 | -0.89 | -7.25 |
| mid | 2020-01-30 | 40.8% | +4.00 | -0.75 | -1.00 | -2.00 | -0.69 | -9.20 |
| small | 2019-12-30 | 11.7% | +3.65 | -0.30 | +0.00 | -0.25 | -0.18 | -7.90 |
| small | 2020-01-30 | 14.1% | +3.50 | -0.25 | +0.10 | -0.10 | -0.05 | -8.95 |

**deep shadows** (faithful mode, 60 s; bias in pp)

| tier | day | exact fill rate | `nt_prob_touch[p=1]` | `nt_queue_l2` | `hbt_power3[n=2]` | `hbt_risk_adverse` | `databento_mc[k=-0.8;particles=100]` | `trade_through_only` |
|---|---|---|---|---|---|---|---|---|
| mega | 2019-12-30 | 38.9% | +0.55 | -0.10 | -1.48 | -1.73 | -1.34 | -4.80 |
| mega | 2020-01-30 | 50.9% | +0.10 | -0.25 | -1.75 | -1.91 | -1.67 | -5.20 |
| deep_queue | 2019-12-30 | 0.2% | +0.00 | +0.00 | +0.00 | +0.00 | +0.00 | -0.10 |
| deep_queue | 2020-01-30 | 2.8% | +0.20 | -0.05 | -0.05 | -0.05 | -0.04 | -1.10 |
| etf | 2019-12-30 | 12.5% | +0.10 | -0.25 | -0.10 | -0.20 | +0.03 | -4.30 |
| etf | 2020-01-30 | 26.4% | +0.40 | -0.10 | +0.05 | -0.05 | +0.12 | -6.55 |
| mid | 2019-12-30 | 20.5% | +0.60 | +0.00 | -1.01 | -1.11 | -1.05 | -3.25 |
| mid | 2020-01-30 | 21.1% | +1.05 | +0.10 | -0.76 | -0.96 | -0.65 | -3.80 |
| small | 2019-12-30 | 0.7% | +0.10 | +0.00 | +0.00 | +0.00 | +0.00 | -0.30 |
| small | 2020-01-30 | 0.7% | +0.05 | +0.00 | +0.05 | +0.00 | +0.05 | -0.25 |

What the breadth run shows:

- The NautilusTrader default (`prob_fill_on_limit = 1`) overfills everywhere, and most on the low-priced large caps
  with deep queues (BAC, F, GE, SIRI, T, PFE): +8.60 pp at 60 s on day one (17.0% exact, so about half again as many
  fills) and +5.80 pp on day two; +9.60 and +4.90 pp at 300 s. On the small caps it is +3.6 / +3.7 pp at 60 s on
  11-14% exact fill rates and +5.5 / +4.3 pp at 300 s. On mega caps and ETFs it is +0.8 .. +3.2 pp at 60 s, peaking
  earlier (+2.9 .. +3.5 pp at 10 s on mega caps).
- The hftbacktest `power3[n=2]` model stays within 1.7 pp at 60 s on standard shadows in every tier: pessimistic on
  mega and mid caps (-1.1 .. -1.6 pp), slightly optimistic on the deep-queue names (+0.67 / +0.94 pp).
  `hbt_risk_adverse` is pessimistic by 1.6 to 2.1 pp on mega and mid caps.
- `nt_queue_l2` stays within 1.5 pp everywhere (largest on the deep-queue names on day two: -1.27 pp standard, -1.50
  pp large).
- Order size barely moves first-fill bias: the `large` rows are within 0.62 pp of the `standard` rows for every model
  shown, because the zoo scores the first fill, which depends on queue position, not on the order's own size; this
  run says nothing about fill quantity. Deeper placements cut the exact fill rate (to 0.2-2.8% on the deep-queue names
  and 0.7% on the small caps) and every bias with it; the hftbacktest and Databento models stay 0.7 to 1.8 pp
  pessimistic on mega and mid caps.
- `trade_through_only` is 7 to 18 pp below exact on standard and large shadows, most on ETFs (-15.9 / -17.8 pp
  standard).

## 5. Calibration, learned model, scorer, crypto (RD-9 .. RD-12)

**RD-9 calibration** (raw ITCH bytes, `.realdata/repro/phase1/results.json`; identical on the CSV dataset with the
same build), fitted on 2019-12-30 blocks 0-3 of 5 and evaluated on 2020-01-30 (6,000 sampled shadows on the two-day
dataset; bootstrap CIs over 30-minute blocks):

| Family | Estimate | 95% bootstrap CI | Events | Held-out day two, fitted vs default: Brier at 1 s / 10 s / 60 s; bias at 1 s (pp) |
|---|---|---|---|---|
| hftbacktest `power3` `n` | 3.098 | [2.922, 3.281] | 45,902 | 0.00940 / 0.0145 / 0.00743 vs (`n = 2`) 0.00973 / 0.0155 / 0.00741; -0.63 vs -0.53 |
| Databento `k` | -0.677 | [-0.695, -0.658] | 45,902 | 0.01021 / 0.01616 / 0.00606 vs (`k = -0.8`) 0.01015 / 0.01616 / 0.00608; -0.30 vs -0.32 |
| NautilusTrader `prob_touch` `p` | 0.287 | [0.269, 0.303] | 3,604 | 0.0115 / 0.0139 / 0.0072 vs (`p = 1`) 0.0280 / 0.0203 / 0.0093; +0.41 vs +2.80 |
| NautilusTrader `queue_l2_prob_touch` `p` | 0.871 | [0.816, 0.919] | 613 | 0.00118 / 0.00384 / 0.00245 vs (`p = 1`) 0.00100 / 0.00367 / 0.00233; -0.24 vs -0.10 |

On the earlier build the fit gave the same `power3`, Databento and `prob_touch` estimates; the held-out scores of the
two NautilusTrader rows and of `power3` moved with the verified faithful NautilusTrader models and the per-shadow #312
rule (earlier: `prob_touch` +0.22 vs +2.33 pp at 1 s; `queue_l2_prob_touch` 0.895 [0.849, 0.935] on 754 events).

The NautilusTrader touch probability is the parameter that moves the held-out day: fitted `p = 0.29` cuts the 1 s
overfill from +2.8 pp to +0.4 pp and more than halves the 1 s Brier score. For the power and Databento families the
fitted and default values score about the same on day two, and `queuetruth export` refuses the fitted power3 `n`
(`QT-CALIB-001`: worse than the default `n = 2` at the 60 s and 5 min horizons, better at 1 s and 10 s); the driver records the
refusal and writes the snippet with `--force` under its NOT VALIDATED header.

**RD-10 learned model** (raw ITCH bytes, `.realdata/repro/phase1/results.json`, identical to the earlier CSV run to
every printed digit; 40,000 sampled shadows over both days, 38,712 with features; train day one blocks 0-3, calib
block 4, test day two with 19,464 orders): Brier skill score vs the stratified Aalen-Johansen climatology on day two,
L2-only features: 0.095 [0.072, 0.122] at 1 s, 0.115 [0.102, 0.128] at 10 s, 0.166 [0.152, 0.177] at 60 s (gate:
lower bound > 0 at 1 s and 10 s, met). With privileged exact-queue features: 0.091 [0.069, 0.116], 0.114 [0.100,
0.126], 0.160 [0.145, 0.172] (latest full run; the earlier run differed only in the third decimal of some bounds); on these orders the exact queue adds no skill over the L2 features.

**RD-11 scorer** (`.realdata/results.json`), live log = the prototype's 3,000 sampled real orders of each day
(`scripts/realdata/real_orders_to_live.py`: day one 3,000 orders, 14,861 events, 261 orders with fills, 331 fills;
day two 3,000 orders, 14,881 events, 211 orders with fills, 271 fills), `sim = exact`, evaluation window 60 s:

| Day | Arm | both | neither | live only | sim only |
|---|---|---|---|---|---|
| 2019-12-30 | queuetruth, self-exclusion on (the default; 3,000 / 3,000 twins matched by order id) | 250 | 2,750 | 0 | 0 |
| 2019-12-30 | queuetruth, self-exclusion off (the twin stays in the tape, ahead of the sim leg) | 90 | 2,750 | 160 | 0 |
| 2019-12-30 | prototype fidelity run (et fill rule, no window), for reference | 235 | 2,739 | 26 | 0 |
| 2020-01-30 | queuetruth, self-exclusion on | 195 | 2,805 | 0 | 0 |
| 2020-01-30 | queuetruth, self-exclusion off | 87 | 2,805 | 108 | 0 |
| 2020-01-30 | prototype fidelity run, for reference | 195 | 2,789 | 16 | 0 |

RD-11 passes on both days: with self-exclusion the exact arm reproduces every one of the 3,000 real orders per day,
with the live filled quantity and the live first-fill time on all of them, and no sim first fill before the live
one. Without self-exclusion the sim leg queues behind its own still-resting twin, the bias audit A1 describes
(-5.3 pp on day one). The prototype rows are not a target: they use et's fill rule with the twin left in the
tape and no 60 s window. An earlier run on the 2026-10-19 01:35 UTC build gave 242 / 2,748 / 8 / 2 on day one,
before the root-cause fix of the self-exclusion path.

Zoo models as the simulator on day one, with self-exclusion (fill-rate bias in pp, 95% CI; recomputed by the latest
full run, which agrees with the earlier run): `front_of_queue` and `nt_prob_touch[p=1]` +0.53 [+0.30, +0.85];
`hbt_power[n=2]` -0.13 [-0.30, +0.10]; `hbt_power3[n=2]` -0.23 [-0.44, 0.00]; `nt_queue_l2` -0.30 [-0.56, 0.00];
`databento_mc[k=-0.8]` -0.33 [-0.79, +0.15]; `hbt_log2` -0.37 [-0.62, -0.06]; `hbt_risk_adverse` -0.47 [-0.75,
-0.14]; `trade_through_only` -7.83 [-9.39, -6.39]. These are real orders with real cancel times, where most orders
cancel before they could fill, so the biases are smaller than on the never-cancel shadows of section 4.

**RD-12 crypto** (venue `generic`, `size_exp -8`, taker window 100 us): Bitstamp 77,272 records (equal to
the prototype's converted count), 76,907 events, 135 trades, 8 invariants 0, 0 crossed books after the last record
with taker handling (12,447 taker orders suppressed); Bitfinex R0 51,701 records (equal), 51,201 events, 243 trades,
invariants 0, 51 out-of-order timestamps. Both verdicts are `warnings` (feed anomalies listed in the run). This shows
replay and counters on the captures, not fill fidelity: neither feed carries the execution records exact queue truth
needs.

## 5b. Real DBN bytes: Databento's test files

`scripts/realdata/dbn_samples.py` (`make dbn-samples`, opt-in, downloads) fetches the MBO test files of Databento's
`dbn` repository at commit `d368005a` (the DBN 0.71.0 release; Apache License 2.0, Copyright Databento, Inc.; not
committed here) into `.realdata/dbn-samples/` and checks their sha256 against the values recorded in the script. Result:
the five encodings (`test_data.mbo.dbn`, DBN v1 and v2 zstd, v3 plain and zstd) all ingest with `--format dbn` to the
same 2 records (ES futures, instrument 5482, 2020-12-28, verdict `clean`) and replay with every invariant 0 except
`unknown_order_refs = 2`, which is expected: both records cancel orders the excerpt never added. This checks DBN
decoding of Databento-written bytes across format versions; 2 records say nothing about book or fill fidelity on CME.

## 6. Defects found and fixed by these runs

- **Sequence-gap censoring in the zoo, calibration and feature engines.** They detected sequence
  gaps per partition; the prototype's CSV numbers sequences across the whole feed, so every partition jumps and
  every shadow was censored as `censored_gap` (2,874 of 3,000 in the first zoo run, which made RD-8 .. RD-10
  meaningless). They now leave gap detection to ingest, as `qt_replay::run_dataset` does.
- **`hbt_l3_fifo` corrected vs the exact engine (O-ZOO-4).** On the 19:53 build it differed on 154 prediction rows of
  the sampled shadows and 41 of the mirror shadows; on the current build it equals the exact arm on every row.
- **RD-11 helper.** `real_orders_to_live.py` failed on the hive partition types of current datasets (`instrument_id`
  uint32 in the files, int32 inferred from the path); fixed.

## 7. Performance

Measured on a loaded machine: other builds were compiling throughout (load1 43 to 101 on 8 cores), Low Power
Mode on, AC power (`env_class` `Apple M1 Pro|8c|16G|ac|lpm1`). Treat these as lower bounds; a quiet-machine baseline
is still to be recorded (`make bench` then `make bench-baseline`).

qtbench quick suite (2M generated records, 8 samples per case over 2 processes, medians with p10 / p90):

| Case | Median | p10 / p90 | SPEC 7.1 target | Met |
|---|---|---|---|---|
| ITCH parse + map | 9.60M msg/s | 2.91M / 16.38M | >= 5.0M | yes |
| replay, book + invariants (from Parquet) | 4.15M rec/s | 2.94M / 4.46M | >= 8M | no |
| replay with BBO change + trades sinks | 2.04M rec/s | 1.29M / 2.73M | >= 4M | no |
| replay with rank oracle + rank trace | 2.71M rec/s | 2.50M / 2.96M | >= 3M | no |
| replay with 3,000 shadows | 4.25M rec/s | 3.89M / 4.92M | | |
| replay with 3,000 shadows + trajectory `ahead` | 3.94M rec/s | 3.11M / 5.02M | | |
| replay with 100,000 shadows | 2.34M rec/s | 1.32M / 3.44M | | |
| L2Builder depth 10, cadence 0 | 2.27M rec/s | 1.12M / 3.84M | >= 3M | no |
| shadow overhead, 3,000 (throughput loss, paired) | -0.17 | | <= 0.15 | yes |
| shadow overhead, 100,000 (cost ratio, paired) | 2.08x | | <= 2.5x | yes |
| trajectory `ahead` extra (paired) | 0.099 | | <= 0.10 | yes |
| `qt.replay` vs `queuetruth replay` on the fixture (cost ratio) | 1.18x | | <= 1.05x | no |

Real day one (RD-13, same loaded machine). **RD-13 fails** under this load: the book-only replay from Parquet
measured 4.30M rec/s in the latest full run (`.realdata/results.json`) and 4.26M rec/s in the RD-13 rerun
(`.realdata/rd13/results.json`), against the 8M rec/s target. The other day-one targets checked there are met: etcsv
ingest 1.69M rec/s (rerun 1.94M; target 1.5M) and the zoo for 17 models x 3,000 shadows in 9.0 s faithful and 8.2 s
corrected at 4 threads (target <= 90 s). Also measured in the latest full run: the RD-2 replay (rank oracle, full BBO,
trade and BBO parity) 0.94M rec/s, 3,000 shadows with trajectory 8.4M rec/s, features for 40,000 shadows over both
days 9.6 s, `fit-hazard` 47 s (the L2-only fit; the <= 120 s target for 60,000 order-landmarks is not isolated here);
peak RSS of the driver 1.78 GB. Whether the replay target is met on a quiet machine is open until it is re-measured
(`make bench`, `make bench-baseline`).
The day-one end-to-end pipeline budget (<= 15 min, <= 2 GB RSS) and the zoo single-thread target were not measured
separately. The misses are recorded with their numbers; the replay kernel targets need a quiet-machine run before
any bottleneck analysis.

## 8. Reproduce

From the public Nasdaq files alone (no prototype artifact; streams about 3.5 and 5.6 GB, stores only the subsets):

```sh
make reproduce              # scripts/repro/reproduce.py: sections 2, 3 (raw rows), 4, RD-9, RD-10
make reproduce-breadth      # scripts/repro/breadth.py: section 4b
uv run --frozen --no-sync python scripts/repro/render_tables.py --repro .realdata/repro \
    --breadth .realdata/repro/breadth/breadth.json                # the tables of sections 4 and 4b
```

Against the prototype's artifacts, which are read in place (`<et-root>/out`) and are not public:

```sh
make realdata                                                     # RD-1 .. RD-13 -> .realdata/results.json
uv run --frozen --no-sync python scripts/realdata/headline.py --ds-root .realdata/ds --out .realdata/headline
uv run --frozen --no-sync python scripts/oracle/run_oracle.py --dataset .realdata/ds/nasdaq-2019-12-30 \
    --shadows <et-root>/out/fidelity-itch/shadows.csv --run-qt --out .realdata/oracle/2019-12-30
make bench BENCH_SUITE=quick                                      # -> .realdata/bench.json
```
