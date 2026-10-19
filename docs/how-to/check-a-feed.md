# Check a feed before you trust it

**Goal:** run a data-quality battery over an order-level (MBO) dataset and get a pass, warn or fail per check with
the offending rows, as a JSON document for your pipeline and an HTML page for people.

**You need:** a dataset written by `queuetruth ingest` (ITCH, DBN, etcsv, crypto L3, IEX DEEP+) or
`queuetruth fixtures synth`. Nothing else: `queuetruth dq` reads the dataset's Parquet files and runs the exact
replay itself.

## Run it

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --venue nasdaq --out synth
queuetruth dq --data synth --out dq
```

The terminal leads with the verdict, then one line per check:

```text
data quality: PASS (12 pass, 0 warn, 0 fail, 2 skipped) over synth (198,456 records, 2 date(s), 2 symbol(s))

  ok    dq_counters       replay verdict clean
  ok    sequence          0 gap(s) (0 missing message(s)); 0 regression(s); 0 cross-instrument collision(s); ...
  ok    crossed_book      0 crossed and 0 locked event end(s) of 120000 event(s)
  ...
  skip  cross_source      not run: no second dataset (--compare DIR)
  skip  rate_drift        not run: needs three or more session dates of one symbol
```

`dq/dq_report.html` is a self-contained page (no network requests, light and dark themes) with every check, its
numbers and its evidence rows. `dq/dq_report.json` is the same content as a `qt.dq_report` document;
`--format json` prints it to stdout as well.

Exit codes: `0` when no check reached the `--fail-on` status, `1` when one did (`QT-GATE-001` on stderr), `2` for
an error (missing dataset, unknown check id). `--fail-on warn` is the default; `--fail-on fail` exits 1 only on a
failed check; `--fail-on never` always exits 0.

## The checks

| Id | What it looks at | Warn | Fail |
|---|---|---|---|
| `dq_counters` | The `qt.data_quality` counters and verdict (from the replay when it ran, else from ingest). | verdict `warnings` | verdict `unusable` |
| `sequence` | Per `(session_date, channel_id)`: gaps, regressions, the same number on two instruments; exact duplicate records. | any | missing messages or duplicates above 1e-3 of records |
| `crossed_book` | Best bid at or above the best ask at the end of an event (the record carrying `F_LAST`). | any | above 1e-3 of events |
| `stale_book` | Regular-hours stretches with no book update longer than `--stale-ns` while trades print; prints through the standing book. | any | prints through the book above 1e-3 of prints |
| `orphan_refs` | Cancels, fills and modifies of orders never added (since the last clear), and the replay's unknown references. | any | above 1e-3 of records |
| `size_price` | Zero sizes on adds, fills and prints; sizes above 2^63; undefined or non-positive prices; resting prices off the tick grid. | any | above 1e-3 of records, or any size overflow |
| `trade_parity` | Replay-derived trades against the dataset's source prints (`trades_source`). | any mismatch | above 1e-3 of trades |
| `timestamps` | `ts_event` going backwards; the `ts_recv - ts_event` distribution, negative latency, outliers. | any | regressions or negative latency above 1e-3 |
| `session_coverage` | Data before the open and after the close (counted), late starts, early ends, no regular-hours data, a tail cut at 00:00 UTC. | any | never |
| `symbology` | One instrument id per symbol across days and one symbol per id; units (`price_exp`, tick, lot) stable. | a change | duplicate rows, a partition without an instrument row, `price_exp` differing for one id |
| `snapshots` | Snapshot runs, clears, snapshots without the clear the venue sends, records before the first snapshot, `MAYBE_BAD_BOOK`. | any | `MAYBE_BAD_BOOK` |
| `vintage` | Partition files re-hashed against the manifest, `dataset_id` recomputed, input files still on disk, DBN versions and vendor datasets. | missing provenance, mixed vintages, inputs that changed | a partition file that changed or is missing |
| `cross_source` | Record-level parity with a second dataset of the same sessions (`--compare`). | any difference | above 1e-3 of records |
| `rate_drift` | Each day's record count against the symbol's median over its other days (three or more days). | below 0.25x or above 4x | never |

The 1e-3 failure threshold is the same as the `unusable` threshold of the ingest verdict. Every check explains
itself in its `notes`: which rule set it used, which records it could not judge and why.

A few rules that keep the checks honest on real feeds:

- Sequence numbers are only checked where they mean something: not for ITCH (the reader numbers the messages), not
  for rule sets that mark them as not meaningful. A partition that holds a sparse share of its channel's numbering
  (a symbol-filtered download, where the holes are other instruments' messages) is reported in the notes, not as
  gaps; the ingest counters, computed on the whole feed before the symbol filter, stay authoritative. A snapshot
  resets the channel, so the jump across it is no gap.
- A print counts as through the book only if the opposite best is better than the print price both at the end of
  the print's own event and after every event of its timestamp. Sweeps, same-timestamp refills and feeds that send
  the removal of a filled order as a later message do not count. Each through print carries `catch_up_ns`, how long
  the book took to agree with it: a few hundred milliseconds means the book channel lags the trade channel; no
  catch-up at all means the book is wrong.
- When most records carry one clock only (`ts_recv` equal to `ts_event`), the latency percentiles describe the
  records that carry both.
- US equity quotes below $1.00 may use $0.0001 increments (Reg NMS Rule 612); the tick-grid check allows them.

## Read the report from code

<!-- check-docs: run -->
```python
import json

rep = json.load(open("dq/dq_report.json"))
print(rep["status"], rep["counts"])
for c in rep["checks"]:
    if c["status"] in ("warn", "fail"):
        print(c["id"], c["summary"])
        for ev in c["evidence"]:
            print("  ", ev["name"], f"{len(ev['rows'])} of {ev['total']} rows")
            for row in ev["rows"][:3]:
                print("    ", dict(zip(ev["columns"], row)))
```

The same run from Python, without writing files:

<!-- check-docs: run -->
```python
from queuetruth import api

rep = api.dq({"dataset": {"path": "synth"}, "checks": ["sequence", "timestamps", "vintage"], "replay": False})
for c in rep.checks:
    print(c.id, c.status, c.metrics.get("records"))
```

| Field | Meaning |
|---|---|
| `status` | The worst check status (`pass` when every check passed or was skipped). |
| `gate_failed` | The status reached `spec.fail_on` (the CLI exits 1). |
| `counts` | Checks per status. |
| `dataset`, `compare` | Path, `dataset_id`, format, venue, partitions, records, dates and symbols of each dataset. |
| `checks[].metrics` | The numbers behind the status (counts, shares, percentiles; durations in ns). |
| `checks[].evidence[]` | Tables `{name, columns, rows, total, truncated}`: the first `--max-evidence` rows (default 20) of `total`. Timestamps are integer ns UTC, prices integers in the instrument's `price_exp`. |
| `checks[].notes` | Caveats: the rules applied and what could not be judged. |
| `spec` | The spec echo. |

The JSON Schemas are `qt.dq_report` and `qt.dq_spec` (`queuetruth schema show qt.dq_report`, or
`schemas/json/qt.dq_report.schema.json` and `qt.dq_spec.schema.json` in the repository).

## Compare two sources of the same session

Ingest the same day from two sources and compare them record by record. Partitions are matched by
`(session_date, symbol)`, so instrument ids may differ (ITCH locate codes against Databento ids); prices are rescaled
to the finer `price_exp`. Records are compared on `(ts_event, action, side, price, size, order_id)`; the
feed-specific fields (`ts_recv`, `sequence`, `flags`, channel and instrument ids) are not. Differences that vanish
when `ts_event` is ignored are reported as timestamp-only.

<!-- check-docs: skip (needs two real ingests of the same day) -->
```sh
queuetruth ingest --format itch --in 12302019.NASDAQ_ITCH50.gz --venue nasdaq --symbols AAPL,MSFT --out itch
queuetruth ingest --format dbn --in xnas-itch-20191230.mbo.dbn.zst --venue nasdaq --out dbn
queuetruth dq --data itch --compare dbn --out dq-itch-vs-dbn
```

On the 2019-12-30 Nasdaq ITCH day (AAPL, MSFT, SPY, TSLA), the qt ITCH reader against the independent et parser's
output (`etcsv`) matched all 6,103,262 records, with no timestamp-only differences.

## Faster runs and thresholds

- `--no-replay` skips the exact replay: `crossed_book` and `trade_parity` are skipped, `stale_book` keeps its
  quiet-interval part and `orphan_refs` its scan. Use it for a quick pass over a large dataset.
- `--checks sequence,timestamps,vintage` runs only those checks.
- `--stale-ns 30s`, `--latency-outlier-ns 5ms` (default: 10 x p99, at least 1 ms), `--session-tolerance-ns 10m`
  (default 5 min) and `--max-evidence 100` tune the thresholds and the evidence size.
- `--verify-sources` re-hashes the raw input files recorded in the manifest (by default only their size is
  compared).

In CI, a nightly job that ingests the day's files can gate on the battery:

<!-- check-docs: skip (CI sketch) -->
```sh
queuetruth dq --data "ds/$DAY" --out "dq/$DAY" --fail-on fail --format json > "dq/$DAY/summary.json"
```

## What it does not check

- Exchange checksums (Kraken L3 CRC32 and similar) and vendor `.md5sum` / `.done` markers.
- How often a modify kept or lost priority, and statistical drift beyond per-day record counts.
- Gaps on ITCH files (the file format carries no MoldUDP sequence numbers), and gaps hidden by a symbol filter
  applied before the data reached you.
