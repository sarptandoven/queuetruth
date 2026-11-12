# See it in a minute

`queuetruth demo` answers one question on a public trading day before you bring any data: how often does a popular
backtester's default fill model fill small resting orders, compared with the exact replay of the real queue? It
streams the first minutes of a public order-level day, places simulated resting orders in the real queue and prints
the answer first. It needs queuetruth 0.2.0 or later; `pipx run queuetruth demo --yes` runs it without installing
anything.

On our heavily loaded laptop a first run took 22 to 53 s and downloaded about 315 MB; a rerun from the cache took 13
to 17 s.


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
([Results](../RESULTS.md), section 4).

Compared with the full-session numbers published in [Results](../RESULTS.md) (orders placed from 09:35 to 15:50): at
1 s this window's +3.6 pp [+2.6, +4.8] agrees with the session's +3.13 pp (block-bootstrap interval +2.63 to
+3.64; see [the headline number](../RESULTS.md#the-headline-number)). At 10 s and 60 s it is
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
the same (placements from 300 s after the open, planned life 300 s, [Results](../RESULTS.md) section 4): the opening
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
  marker). `queuetruth demo --help` or `queuetruth help demo` lists every flag.

The Nasdaq sample files carry no terms of use of their own and Nasdaq claims copyright in them (see
[Data and licensing](../data-and-licensing.md)): the demo keeps the filtered subset in your cache, prints aggregate
statistics only, and `demo.html` holds no order-level rows. The full report of the zoo run
(`queuetruth report --in qt-demo/zoo`) does hold event-level traces; keep it to yourself for Nasdaq data.

## Next

- [Your first leaderboard](../getting-started.md): the same steps by hand, on synthetic data.
- [A real day from free IEX data](../how-to/iex-public-demo.md): a whole day you download yourself.
- [The Fill-Realism Atlas](../concepts/fill-realism-atlas.md): the same comparison over every trading day.
