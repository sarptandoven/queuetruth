# Extension: `queuetruth demo`

Proposed spec text for the integrator (a new subsection of SPEC 4.4, the command line, plus a new document type).

## Command

`queuetruth demo [--source ID] [--symbols A,B] [--minutes N] [--n N] [--models M,...|all] [--url URL | --from-file
PATH] [--input-format itch|iex] [--date YYYY-MM-DD] [--out DIR] [--cache-dir DIR] [--refresh] [--yes] [--dry-run]
[--max-download-mb MB] [--list-sources] [--format human|json] [--threads N] [--seed N] [--quiet] [--verbose]`

Python only (`queuetruth-core` does not know it). It is dispatched by `queuetruth.cli.main` before the clap grammar
(`queuetruth.demo._dispatch.maybe_run`, stdlib only: the first token that is not a global option is `demo`, or the
command is `help demo`; the demo's modules are imported only then). `queuetruth --help` does not list it until the
grammar gains a `Cmd { path: &["demo"], imp: Implementation::Python, .. }` entry (integrator; then regenerate
docs/reference/cli.md).

## Sources and window

- Sources (`queuetruth.demo.sources.SOURCES`): `nasdaq-2019-12-30` (default; alias `nasdaq`), the public Nasdaq
  TotalView-ITCH 5.0 sample file `12302019.NASDAQ_ITCH50.gz`, default symbols AAPL MSFT SPY TSLA; `iex-2026-10-09`
  (alias `iex`), IEX HIST DEEP+ `20261009_IEXTP1_DPLS1.0.pcap.gz`, default symbols SPY AAPL NVDA TSLA. `--url` and
  `--from-file` replace the byte source. Format: `--input-format`, else the file name (last path segment,
  percent-decoded), else the `queuetruth.demo_subset` manifest next to a `--from-file` subset, else the first
  inflated bytes of a file; date: `--date`, else the file name, else that manifest; neither is ever taken from a
  built-in source (`QT-USAGE-001` when unknown). The first inflated bytes must match the format and an IEX capture's
  first Send Time must fall on the session date (US/Eastern), else `QT-INPUT-002`.
- Window (`DemoWindow`, US/Eastern): placement `[09:35:00, 09:35:00 + minutes)` (`open + 300 s`); data cut
  `end + h_max (300 s) + 10 s`. The first message stamped at or after the cut (ITCH timestamp; IEX-TP Send Time)
  ends the subset; nothing after it is read. An input that ends before the cut is complete when its last message is
  at or after `end + h_max` (`filter.complete = "end_of_input"`; the demo's own subsets end just before the cut),
  otherwise `QT-INPUT-002`. Both venues start the day with an empty book, so the prefix is an exact
  order-level record of the day up to the cut. `minutes` in 1..360.

## Subset and cache

- One pass: byte stream (HTTP(S) with `Range: bytes=<offset>-` resumption requiring 206 from that byte, or a file)
  -> sha256 and count of the raw bytes -> gzip inflate (multi-member) or pass-through -> symbol filter with the cut
  -> gzip subset (level 1, mtime 0). ITCH selection: locate 0 and the locates of the requested symbols from their
  `R` on (the `scripts/realdata/itch_subset.py` rules). IEX selection, renumbering and gap carry-over: the
  `scripts/iex/stream_subset.py` rules. Vectorized fast paths walk the framing by pointer doubling over candidate
  frame starts and fall back to the scalar loop for any chunk they cannot prove; both give identical output.
- Cache: `<cache root>/demo/<key>/{subset.itch50.gz|subset.pcap.gz, manifest.json}`, root = `--cache-dir`,
  `QT_CACHE_DIR`, or the platform user cache directory (macOS `~/Library/Caches/queuetruth`, Windows
  `%LOCALAPPDATA%\queuetruth\Cache`, else `$XDG_CACHE_HOME/queuetruth` or `~/.cache/queuetruth`). Key =
  `<source id>_<HHMM>-<minutes>m_<SYM-SYM...>`; custom inputs use `<format>-<date>-<12 hex of sha256(location,
  size:mtime for files)>`. A cache entry is used when the manifest's schema, `schema_version` and `filter_version`
  are current, the subset size and sha256 match the file and, for a pinned configuration, the content matches the
  pin. A fetch builds into a fresh sibling directory and is renamed into place only on success (a failed refresh
  keeps the old entry; concurrent runs never share files). Manifest `queuetruth.demo_subset` v1: source (location, HTTP status and headers,
  total bytes, bytes read, sha256 of the bytes read, resumptions, decompressed bytes, attribution), symbols, window,
  filter statistics (messages read, kept by type and symbol, locates or sessions, gaps, duplicates, content sha256,
  first and last timestamps), subset file sha256, timing.
- Pins (`queuetruth.demo.subset.PINS`): for the default configuration of each source, the subset content sha256 and
  kept-message count; a mismatch is `QT-INPUT-002` and nothing is cached. Any input (also `--url` / `--from-file`)
  whose content, symbols, window, format and date match a pin is recorded as `pinned_source` and reported as that
  source (its note and published reference).
- HTTP: resumption sends `Range` and `If-Range` (the first response's strong ETag, else Last-Modified) and requires
  206 from the offset with the same ETag (`QT-INPUT-001` otherwise); a TLS certificate failure is not retried.
  Inflation yields at most 8 MiB per piece.
- Guards: an input that ends before the cut, a requested symbol with no message, more than `--max-download-mb`
  (default 4000) read without reaching the cut: `QT-INPUT-002`; free disk below 2 GiB: `QT-INPUT-001`; network or
  file errors: `QT-INPUT-001`.
- Consent: a remote cache miss downloads only with `--yes` (`DemoOptions.yes`) or a confirmation callback that
  returns True; the Python API without either raises `QT-USAGE-001`. On a terminal the CLI prompts on stderr. An
  estimate above `--max-download-mb` (past the last checkpoint: the last segment's rate, capped at the file size) is
  refused before the download. `--dry-run` prints `qt.demo_plan` (or its text) and touches nothing.

## Run

`ingest` (format `itch` venue `nasdaq`, or `iex_deep_plus` venue `iex`; `session_date` of the source, `price_exp`
-9, tick 0.01, lot 1) into `<out>/dataset`; `shadows sample` with `n` (default 3,000), `seed`, `window_open_s =
start - open_s`, `window_close_s = close_s - end` (session hours from `instruments.parquet`), `lifetime_ns = h_max`;
`zoo` faithful at the default horizons with `--models` (default `nt_prob_touch[p=1]`, `nt_queue_l2`,
`hbt_power3[n=2]`, `hbt_risk_adverse`, `databento_mc[k=-0.8;particles=100]`, `front_of_queue`,
`trade_through_only`; it must include `nt_prob_touch[p=1]`). `<out>` (default `qt-demo`) must be absent, empty or a
previous demo output (holds the marker `.qt-demo`, written first, or a `qt.demo_summary` `demo.json`, or only the
entries below); only `dataset`, `shadows.parquet`, `zoo`, `demo.html`, `demo.json` are replaced, and the last two
are written through a temporary file and a rename.

## Answer and interval

Per model and horizon: the leaderboard's `exact_fill_rate`, `model_fill_rate` and `fill_rate_bias.value` (audit
ruling A2). Interval: percentile 95% of a cluster bootstrap, clusters = (instrument, `floor((ts_live - min ts_live)
/ 60 s)`) over the leaderboard population (never-cancel shadows, label eligible, `common_population`), replicate =
`sum_k c_k num_k / sum_k c_k den_k` with `c = block_counts(K, 2000, bootstrap_seed(seed, "demo_cluster"))`,
`num_k = sum (p - y)`, `den_k = rows`. It is computed only when no never-cancel shadow has a truncated label
(administrative end before `ts_live + h_max`), every row's `p` is finite, and `mean(p - y)` equals the leaderboard
value within 1e-9;
otherwise the cell has `ci_method = "none"` and a warning names the reason. Headline: `nt_prob_touch[p=1]` at 1 s.

## Document `qt.demo_summary` v1

`run_id` (SPEC 7.3 over the demo spec: format, date, symbols, window, n, seed, canonical model ids sorted, mode,
interval settings, `subset` ref resolved to the subset's content sha256; `out` and `threads` excluded; source id,
location and gzip bytes are provenance, not identity),
`queuetruth_version`, `created`, `spec`, `source` (id, title, format, session date, location, attribution, terms
URL, note), `window`, `data` (symbols, subset path, bytes, sha256, content sha256, kept messages, cache hit, pinned,
remote, bytes read, file bytes, resumptions, records, data-quality verdict and reasons, dataset id), `n_shadows`,
`n_leaderboard`, `n_excluded_hbt_312`, `zoo_run_id`, `headline` (model id, label, `verified_against`, sentence,
cells by horizon),
`models` (short ids without `mode=faithful` / `exch=partial`, cells), `interval`, `reference` (published
full-session bias, interval and exact fill rate for the source, or null), `outputs`, `timings`, `warnings`. A cell: `exact_fill_rate`,
`model_fill_rate`, `bias`, `ci_lo`, `ci_hi`, `n`, `ci_method`. `qt.demo_plan` v1: source, window, symbols, cache
directory, cache hit, download needed, estimated bytes, whether the estimate is extrapolated, the download cap,
file bytes, out. `qt.demo_sources` v1 (`--list-sources --format json`): default, aliases, sources (id, title,
format, date, URL, default symbols, attribution, terms URL). All three are Pydantic models in
`queuetruth.schemas.demo` (re-exported by `queuetruth.demo.summary`), registered in
`queuetruth.schemas.models.DOC_MODELS` and exported to `schemas/json` and the Rust core's embedded copies.

## Outputs and licensing

`demo.html` is self-contained (no script, no external request), holds aggregate statistics only, names a local
input by its file name only, lists the run's warnings, and for IEX
sources carries IEX's attribution line with a link to the IEX Historical Data Terms of Use; the terminal output and
`qt.demo_summary.source.attribution` carry it too. The subset stays in the user's cache.
