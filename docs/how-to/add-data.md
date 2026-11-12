# Add order-level data

**Goal:** turn the order-level files you have, or a free IEX day, into one dataset that every other command reads
(`check --data`, `zoo`, `replay`, `fit`), with its data quality checked, in one command.

**You need:** queuetruth 0.4.0 or later. For `data fetch iex`, a network connection and a few GB of download
bandwidth (nothing large is stored).

## Add files you have

```sh
queuetruth data add 12302019.NASDAQ_ITCH50.gz 01302020.NASDAQ_ITCH50.gz --out nasdaq
```

`data add` reads what each file is, so you do not pass the format, the venue or the date:

- **Format**, from the file's first bytes (inflated when it is gzip or zstd): a pcap or pcapng capture is IEX DEEP+,
  `DBN` and a version byte is Databento DBN, ITCH 5.0 framing is Nasdaq TotalView-ITCH. Text files are told apart by
  their first lines: a Tardis `incremental_book_L2` CSV header, the JSON lines of the crypto captures (Bitstamp,
  Bitfinex, Coinbase `full` and `level3`, Kraken `level3`, Hyperliquid node), and NYSE TAQ Integrated Feed files by
  their product name (`EQY_US_<market>_IBF_<channel>_<YYYYMMDD>`). A capture directory is read by its first data
  file.
- **Venue**, from the format (ITCH: `nasdaq`, IEX: `iex`, NYSE: `nyse`, the crypto L3 captures: `generic`, which the
  engine reads as `crypto_l3`, Coinbase `full`: `coinbase`, Tardis: `generic` with the source venue from its
  `exchange` column), and for DBN from the dataset code in the file's header (`XNAS.ITCH`: `nasdaq`, `GLBX.MDP3`:
  `cme`).
- **Session date**, only for ITCH, whose timestamps count from midnight: from the file name (`MMDDYYYY.` or
  `YYYYMMDD_`), or from a `manifest.json` next to the file that names it. That covers the subset `queuetruth demo`
  keeps in its cache: `data add ~/.cache/queuetruth/demo/<entry>/subset.itch50.gz --out demo-day` needs no flags.
  Every other format dates each record itself.

Only when a file does not say, `data add` stops with `QT-USAGE-001`, names what it could not tell and why, and asks
for one of `--input-format`, `--venue` or `--date`. `--symbols AAPL,MSFT` keeps only those symbols.

Several files of one format go into one dataset: ITCH files are ingested one per day, the other formats in one
ingest, and the days are merged. Give an existing dataset as `--out` to add days to it; a day it already holds is
refused and the dataset stays as it was. Everything is built next to `--out` and swapped in only when it worked, so
a failed run changes nothing.

It prints the answer first, then one line per day with the session's first and last record in the venue's local
time. On the synthetic NYSE days in the repository's test fixtures (every symbol, order and price invented):

<!-- check-docs: run -->
```sh
queuetruth data add "$QT_FIXTURES"/nyse/EQY_US_NYSE_IBF_3_20260105.csv \
    "$QT_FIXTURES"/nyse/EQY_US_NYSE_IBF_4_20260105.csv \
    "$QT_FIXTURES"/nyse/EQY_US_NYSE_IBF_7_20260106.csv --out nyse-ds --quiet
test -f nyse-ds/dq_report.html
```

```text
nyse-ds: QTF, QTG, QTN, QTN B and 2 more on 2026-01-05 and 2026-01-06, 16,189 records; data quality warnings, checks warn.

date        session (America/New_York)  symbols  records
2026-01-05  07:15:10 to 16:00:30              4       77
2026-01-06  09:30:00 to 09:31:24              2   16,112

format nyse_ibf (NYSE TAQ Integrated Feed), venue nyse, dataset id dd2243b6e016
data quality warnings: sequence_gaps = 1; feed_gaps = 3; nyse_parity_ranks_approximate = 6
checks warn: 9 passed, 3 warned, 0 failed, 2 skipped (report nyse-ds/dq_report.html)
```

followed by the checks that did not pass, how each input was identified and the next command. The fixtures contain a
sequence gap and a publisher failover on purpose, hence the warnings. "Data quality" is the ingest verdict (`clean`,
`warnings` or `unusable`, from `data_quality.json`); "checks" is the [dq battery](check-a-feed.md) run on the
result, whose report is written into the dataset (`dq_report.json` and `dq_report.html`).

From Python, the same with a result whose `summary` is the answer and `table()` one row per day and symbol:

<!-- check-docs: skip (needs ITCH files) -->
```python
import queuetruth as qt

res = qt.data.add(["12302019.NASDAQ_ITCH50.gz", "01302020.NASDAQ_ITCH50.gz"], out="nasdaq")
print(res.summary)
res.table()
```

`--json` (or `--format json` before the command) prints the whole `qt.data_add` document. `queuetruth ingest`
stays for the options `data add` does not take (`--tick-size`, `--option`, an etcsv `--instruments` file).

## Fetch a free IEX day

IEX publishes every trading day of its DEEP+ feed, order by order, free of charge
([A real day from free IEX data](iex-public-demo.md) explains the feed and how queuetruth reads it):

<!-- check-docs: skip (streams a whole IEX day, several GB) -->
```sh
queuetruth data fetch iex --date 2026-10-09 --symbols AAPL,MSFT --out iex-aapl
```

It streams the day's file (`<YYYYMMDD>_IEXTP1_DPLS1.0.pcap.gz` in IEX's public bucket) once, keeps only the symbols
you ask for with the Rust engine that splits the atlas days, ingests them as `data add` does, and never stores the
day's file. It shows progress (MB read, the market time reached, messages kept) on stderr.

Nothing is downloaded until you agree: on a terminal it shows the plan and IEX's terms and asks; elsewhere it stops
with `QT-USAGE-001` unless you pass `--yes`. `--dry-run` shows the plan and downloads nothing:

<!-- check-docs: run -->
```sh
queuetruth data fetch iex --date 2026-10-09 --symbols AAPL,MSFT --dry-run
```

A day IEX has no file for (a weekend, a holiday, today before IEX publishes it) stops with `QT-INPUT-001`. The
capture must start on the date you asked for and hold every symbol you asked for (`QT-INPUT-002` otherwise, and
nothing is written). An existing dataset as `--out` gains the day.

IEX's terms require a citation on everything built from its data. `data fetch iex` records it in the dataset's
`manifest.json` (`attribution`: the source, the text, the terms link and the URL read), prints it under the
summary, and keeps it when you add more days:

> Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical Data
> Terms of Use.

([IEX Historical Data Terms of Use](https://iextrading.com/iex-historical-data-terms/); see
[Data and licensing](../data-and-licensing.md) for the other sources.)

## Next

- [Check my backtest](check-my-backtest.md): `queuetruth check BACKTEST --data nasdaq`.
- [Check a feed before you trust it](check-a-feed.md): the dq battery that `data add` runs, check by check.
