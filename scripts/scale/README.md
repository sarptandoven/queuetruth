# W1 scale pipeline: every symbol of every public Nasdaq day

Scripts for the Fill-Model Bias Atlas (ROADMAP W1): stream a public Nasdaq TotalView-ITCH day once, split it into
symbol buckets, run small independent work units, and aggregate their summable statistics into leaderboards with
cross-day and cross-symbol intervals under a Merkle-rooted manifest. The scripts stay out of the wheel; the library
part is `queuetruth.eval.blocks` and `allocation: per_partition` in `sample_shadows`.

```
itch_split.py ─▶ split/<date>/ (K buckets) ─▶ plan.py ─▶ units ─▶ unit.py ×N ─▶ shards/<unit>/ ─▶ aggregate.py ─▶ atlas/
 one stream of     crc32(symbol) mod K        quotas,     bin-packed   ingest, replay checks,     block_stats   Merkle root,
 the raw day,      locate 0 to every bucket   exclusions  by cost      per-partition sample,      + manifest    leaderboards,
 never stored                                                          zoo x pops x modes                       per-symbol, FDR
```

| script | does |
|---|---|
| `itch_split.py` | streams a day (`--date` for a public EMI day with pinned hashes, or `--url`), routes each symbol's messages from its `R` on to bucket `crc32(symbol) mod K` and locate 0 to all buckets; checks the stream against the pinned values; per bucket the file and content sha256 and per symbol `[locate, messages, executions]` |
| `plan.py` | universe, exclusions with reasons (Nasdaq test symbols), per-symbol shadow quotas (`scale`, `fixed`, `breadth`), cost model, first-fit-decreasing units within a bucket, `run_id` |
| `unit.py` | one unit: bucket sha256 check, ingest of its symbols, replay exactness checks, `per_partition` shadow sample, native zoo per population and mode, `block_stats`, 1% audit sample of predictions, `shard_manifest.json` with telemetry |
| `aggregate.py` | verifies every shard (leaf hash, unit spec hash, output content digests), Merkle root, `zoo_errors_by_group`, `leaderboard` (two-way dates x symbols cluster bootstrap, BH), `per_symbol` (day-cluster CI, empirical-Bayes shrinkage, BH), summed exactness |
| `atlas.py` | multi-day atlas: `reduce-day` streams a day's verified shards into per-symbol-day cells and per-tier strata with the day's Merkle root; `combine` builds tier leaderboards (two-way dates x symbols bootstrap, BH), per-symbol cross-day estimates (day bootstrap, shrinkage toward the tier, BH), tier x stratum maps, and the root over the day roots |
| `atlas_site.py` | the public site from a `combine` directory: per tier, every fill model's bias against the exact replay at every horizon with its interval, the plain-words headline, the atlas and day Merkle roots with the verify command, `data/leaderboard.json`; static, no script, byte-identical for the same inputs; `--source iex` adds IEX's attribution to every page |
| `verify_unit.py` | re-runs one unit and checks its leaf against the published root (a run's or a day's manifest) |
| `symbol_exactness.py` | per-symbol exactness: when a unit's replay checks flag anything, replays each symbol alone (one-partition dataset views) and checks that the per-symbol counters add up to the unit's |
| `itch_ref.py` | an independent Python reading of ITCH 5.0: rebuilds the displayed book of chosen symbols, counts crossed events and price-priority violations as the engine does, and keeps the trading-state timeline (`H`, `K`, `J`, `h`), `Q` crosses and crossed episodes that explain them |
| `trace_flags.py` | traces a day's flagged units to symbols, ITCH message positions and causes; checks engine = reference per symbol; counts the flagged symbols' shadows submitted or filled while the symbol was not trading; writes the `symbol_flags.json` sidecar |
| `findings/` | what the 2020-01-30 pilot's flags were (`README.md`) and a failing reproduction of the engine defect they exposed |
| `prove_breadth.py` | the P0 proof below; `--compare-only --plan P --shards S` compares shards made elsewhere (the cloud gate) |
| `modal_app.py` | Modal entry points: `check_image`, `days` (split, plan, units, reduce per day, spend guard; dry run unless `--confirm-spend`), `combine_days` |

Liquidity tiers (`plan.py`) rank a day's symbols by executions (E, C and P messages): `T1_top100`, `T2_101_500`,
`T3_501_1500`, `T4_1501_4000`, `T5_rest`. A day whose system events put the close away from 16:00 (2025-11-28 closes
at 13:00) gets that session in its ingest spec.

## Why splitting does not change the numbers

- **Records.** A bucket holds exactly the messages a symbol-subset stream of its symbols keeps, in file order, so an
  ITCH ingest of the bucket writes byte-identical partitions to an ingest of the same symbols from the full day.
  With other symbols selected alongside, the rows are identical except `sequence`, which the ITCH mapper numbers
  across the selected locates; the numbering keeps the same order (tested in
  `python/tests/scale/test_scale_pipeline.py`).
- **Shadows.** `allocation: per_partition` gives each instrument-day `n`, or its `partition_quotas` entry
  (`"<locate>"` or `"<locate>_<YYYYMMDD>"`), and the draws are seeded per instrument-day, so a symbol-day gets the
  same shadows in whichever unit runs it (Rust and Python tests). A non-empty `partition_quotas` enters the
  `run_id`; the empty default does not, so existing run ids are unchanged.
- **Models.** Particles and Bernoulli touches are seeded per (model, shadow); the zoo is per partition.
- **Statistics.** `block_stats` sums the rows `evaluate_zoo` scores per model, population, stratum, horizon,
  symbol-day and 30-minute block. `evaluate_blocks` rebuilds the zoo's ratio metrics and their shared-draw block
  bootstrap CIs from those sums. On the 4-symbol day-one run it matches all 6,120 cells of `zoo_errors.parquet`
  within 6e-15, whole or split into four shards. The AJ leaderboard figure, time-to-fill and queue-tracking metrics
  and the cluster-robust discordance p-values need row-level data and are not rebuilt.

## Manifest contract

- `shard_manifest.json` has a `leaf` (unit spec sha256, bucket sha256, partition file sha256s, shadow file sha256s,
  zoo run ids, content digests of the four outputs, exactness counts, #312 abort counts) and its `leaf_hash` =
  `sha256(0x00 || canonical_json(leaf))`. Build info, telemetry and timings sit next to the leaf and are not hashed,
  so re-running the same unit on another machine gives the same leaf when the results agree.
- Content digests hash table contents, not Parquet bytes: rows sorted, values in a fixed binary form (`common.py`).
- A flagged unit's leaf also has `exactness_by_symbol` (`{"method": "per_partition_replay", "complete",
  "sum_mismatches", "symbols": {symbol: {flag: count}}}`, flagged symbols only). An exact unit's leaf has no such
  key, so its leaf hash is the same as before per-symbol attribution existed; `verify_unit.py` checks a re-run
  flagged unit against a root published before it in the leaf's pre-attribution form.
- `atlas_manifest.json` holds the Merkle root over the leaves in unit-id order (inner node `sha256(0x01 || l || r)`,
  odd node carried up) and every leaf's proof. `verify_unit.py` (or `modal run modal_app.py::verify`) re-runs a unit
  and checks it against the root.
- Floats may differ by up to 4 ulp off macOS arm64 (SPEC 7.2), which changes a digest. `verify_unit.py
  --published-shard` names the differing leaf fields. Gate any cloud image on `scripts/repro/reproduce.py` first.

## P0: local proof against the published breadth run

```
uv run --frozen --no-sync python scripts/scale/prove_breadth.py --breadth <path to breadth.json>
```

It streams both public days once (36 symbols kept, 8 buckets), plans 72 single-symbol units with the breadth run's
per-tier equal allocation as per-symbol quotas, runs them, aggregates with the tiers as groups and compares every
published cell with the result. Result of the run on 2026-10-22: see `Run on 2026-10-22 (M1 Pro, 3 workers x 2 threads, both days re-streamed from emi.nasdaq.com; every pinned stream
check passed; buckets deleted afterwards):

| | |
|---|---|
| units | 72 (36 symbols x 2 days), 0 errors; 72/72 shards valid; Merkle root `f8651471d86ac3e1...` |
| compared with `breadth.json` | 342,648 values: every `fill_rate_bias` cell (17 models x strata all / queue decile / liquidity bucket / instrument x 4 horizons x 3 populations x 2 modes x 5 tiers x 2 days: value, CI bounds, n), exact fill-rate counts, shadows per symbol, #312 aborts per model, replay checks per tier |
| mismatches | **0**; max abs difference 2.8e-17 (float summation order) |
| example | deep_queue, standard, faithful, 60 s, `nt_prob_touch[p=1]`: +8.60 [+6.49, +10.59] and +5.80 [+4.40, +7.24] pp, as RESULTS 4b |
| unit CPU | 527 s for 36.5M records and 70,000 shadows (zoo 404 s, ingest 44 s, block statistics 44 s, replay 15 s); peak RSS 842 MB |
| `verify_unit.py` | BAC 2020-01-30 re-run with another thread count: same leaf, proof reaches the root |` below.

Run on 2026-10-22 (M1 Pro, 3 workers x 2 threads, both days re-streamed from emi.nasdaq.com; every pinned stream
check passed; buckets deleted afterwards):

| | |
|---|---|
| units | 72 (36 symbols x 2 days), 0 errors; 72/72 shards valid; Merkle root `f8651471d86ac3e1...` |
| compared with `breadth.json` | 342,648 values: every `fill_rate_bias` cell (17 models x strata all / queue decile / liquidity bucket / instrument x 4 horizons x 3 populations x 2 modes x 5 tiers x 2 days: value, CI bounds, n), exact fill-rate counts, shadows per symbol, #312 aborts per model, replay checks per tier |
| mismatches | **0**; max abs difference 2.8e-17 (float summation order) |
| example | deep_queue, standard, faithful, 60 s, `nt_prob_touch[p=1]`: +8.60 [+6.49, +10.59] and +5.80 [+4.40, +7.24] pp, as RESULTS 4b |
| unit CPU | 527 s for 36.5M records and 70,000 shadows (zoo 404 s, ingest 44 s, block statistics 44 s, replay 15 s); peak RSS 842 MB |
| `verify_unit.py` | BAC 2020-01-30 re-run with another thread count: same leaf, proof reaches the root |

## Pilot cost (one full-market day on Modal)

Not run; needs Sarp's approval. The planner's cost model is fitted to the proof's 72 units (R^2 0.994, `plan.py`):
per symbol-day `1.6e-6 x records` for ingest and replay checks, plus per population and mode `1.45e-6 x records +
15.3e-9 x shadow-events + 0.8e-3 x shadows`. The per-shadow term dominates a full-market day: the `scale` quotas give
about 14.9M shadows (1,000 / 500 / 500 for every name with at least 50 executions, fewer for thin names).

| item (2020-01-30: 423M messages, ~478M records, 8,909 symbols less 7 test symbols) | estimate |
|---|---|
| work units: about 100 units of up to 300 core-s; the largest symbol is about 70 core-s | 8.2 core-h at laptop speed; 8 to 12 core-h on cloud cores |
| units on Modal, 2 cores and 4 GiB per container, preemptible (~$0.063 per core-h incl. 2 GiB), x1.3 overhead | $0.70 to $1.00 |
| split: one stream of the 5.6 GB file at EMI's 4 to 16 MB/s (6 to 25 min), 2 cores, 4 GiB | about $0.05 |
| aggregate: about 51M stats rows at day granularity, 8 cores and 32 GiB, 10 to 30 min (untested at this size) | $0.10 to $0.35 |
| image build (Rust release build of the wheel) and the 4-symbol reproduction gate in the image | $0.20 to $0.40 |
| retries and preemption (+20%) | $0.20 to $0.40 |
| **total for the one-day pilot** | **about $1.50 to $2.50; ask for a $5 cap** |

Wall time is about 45 to 60 minutes, mostly the EMI download. The 2019-12-30 day is similar (7.6 core-h at laptop
speed, because shadows rather than records dominate). Volume storage is a few GB (buckets ~6 to 7 GB at gzip level
1 plus ~1 to 2 GB of shards), deleted after the run unless kept. Risks the pilot measures: Python split throughput on
a full day (0.6 to 1.1M msgs/s here; it routes every message), the aggregator's memory at 51M rows, x86 versus arm64
float drift (gate on `reproduce.py` first), and Modal's concurrency limit on the plan.

## Flagged symbol-days: attribution, trading state, labels, tracing

The replay checks of a unit (`unit.py` step 2) sum over its symbols. A flag is any non-zero book invariant,
unknown-order execution, price-priority violation or trade-parity failure (`symbol_exactness.FLAG_FIELDS`). Clean
replay checks do not make a symbol-day's fill truth exact: the exact fill rule does not model halts, so shadows of a
halted or paused symbol are filled against a book the venue does not match (finding F2 in `findings/README.md`; the
v0.1 contract, `docs/design/spec.md` 1.2, censors them instead). A symbol-day is therefore labelled on both.

- **Attribution.** A flagged unit replays each of its symbols alone and records which symbols raised which flags in
  its leaf. The per-symbol counters must add up to the unit's exactly (`complete`); instruments replay
  independently, so they do. A one-symbol unit reuses its own counts; an attribution that raises is recorded as
  incomplete and the unit still completes.
- **Trading state.** `itch_split.py` records, for every symbol of the day, the spans in which its `H` (Stock Trading
  Action) state was not T (`split_manifest.json` `trading_state`); `plan.py` copies them into the universe
  (`non_t_spans`) and records `trading_state_recorded` in the source.
- **Labels.** `atlas.py reduce-day` marks every symbol-day `replay_ok` (no replay-check flag), `trading_ok` (no
  non-T span overlaps the regular session) and `exact_ok` (both) in `cells_all.parquet` (`strata_by_tier.parquet`:
  `exact_ok`) and lists the others in `day_manifest.json` `flagged_symbols`, with the attribution source of their
  replay flags: `leaf`, `symbol_flags` (a `trace_flags.py` sidecar for shards written before attribution, accepted
  only for the same run, date and leaf hash, from a trace that reproduced the shard) or `unit` (no complete
  attribution: every symbol of the unit is labelled, so nothing unverified passes as exact). For a plan made before
  plans carried the trading state, pass the day's split manifest from a re-stream with `--trading-state`.
- **Policy.** `atlas.py combine --flag-policy label` (the function's default) keeps the symbol-days that are not
  `exact_ok` and marks them: `per_symbol.parquet` `n_flagged_days` and `flags` (date, counts, cause, non-T spans),
  `leaderboard_by_tier` `n_flagged_symbol_days`, `flagged_n`, `flagged_share`, `maps_by_tier` `flagged_n`,
  `flagged_share`. `--flag-policy exclude` drops them from every table; it refuses a day reduced without per-symbol
  exactness or without a known trading state unless `--allow-legacy-days`. `local_day.py` and `modal_app.py`
  (`combine`, `combine_days`) default to `exclude`. `atlas_manifest.json` lists every flagged symbol-day either way.
  Since the halt fix (after 0.2.0) the engine censors shadows of a halted symbol from units ingested with it (F2),
  so a `trading_ok` label no longer means wrong fills there; `exclude` still keeps tables to the symbol-days whose
  replay checks are clean (F1).
- **Tracing.** Re-stream the flagged units' buckets (`trace_flags.py --restream-command
  SPLIT/<date>/split_manifest.json --out NEW_SPLIT` prints the `itch_split.py` command and each bucket's expected
  sha256) and run

```
uv run --frozen --no-sync python scripts/scale/trace_flags.py --plan plan.json --shards SHARDS --split SPLIT/<date> \
    --out TRACE_DIR
uv run --frozen --no-sync python scripts/scale/atlas.py reduce-day --plan plan.json --shards SHARDS --out DAY \
    --symbol-flags TRACE_DIR/symbol_flags.json --trading-state SPLIT/<date>/split_manifest.json
```

  The per-unit trace files hold message-level evidence (positions, timestamps, prices) taken from the Nasdaq file:
  keep them under `.scratch` or `.realdata`, never in the repository (`docs/data-and-licensing.md`).
- **Test issues.** `itch_split.py` records every directory symbol whose Stock Directory Authenticity is `T`
  (`routing.test_issues`) and `plan.py` excludes them (`test issue (Stock Directory authenticity T)`), next to the
  `ZxZZT` prefixes. A source's `exclusions_sha256` enters the run id, so a different universe of the same day gets a
  different run id.
- **Tests.** `scripts/scale/tests/` (`pytest scripts/scale/tests`): outside the pytest `testpaths` until they move to
  `python/tests/scale/`.
