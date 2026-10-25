# Extension: per-symbol exactness in the W1 atlas, and flag tracing

Proposed text for the W1 scale-pipeline contract (`scripts/scale/README.md`, "Manifest contract", and the atlas
section of the design notes). Implemented in `scripts/scale/symbol_exactness.py`, `unit.py`, `atlas.py`,
`itch_split.py`, `plan.py`, `itch_ref.py`, `trace_flags.py`, `local_day.py` and `modal_app.py`. Tests:
`scripts/scale/tests/test_trace_flags.py`, which the pytest `testpaths` do not collect: they run only with `pytest
scripts/scale/tests` until the integrator moves `test_trace_flags.py` and `flag_itch.py` to `python/tests/scale/`
(and drops `scripts/scale/tests/conftest.py`) or adds the path to `testpaths`. That move is a required integration
step: it is what makes the strict xfail on finding F2 report the engine fix. Evidence:
`scripts/scale/findings/README.md`.

## Flags and exact symbol-days

**Flag fields** (`symbol_exactness.FLAG_FIELDS`): the eight book invariants of `qt.replay` (`cancels_exceeding_resting`,
`fills_exceeding_resting`, `fills_unknown_order`, `unknown_order_refs`, `duplicate_adds`, `unexpected_modifies`,
`crossed_books_after_last`, `negative_spreads`), the rank oracle's `executions_unknown_order` and
`price_priority_violations`, and trade parity's `trades_mismatches`, `trades_missing_derived` and
`trades_extra_derived`. A symbol-day is **exact** when every flag field of its own replay (rank oracle on, sweep gap
2 ms, trade parity on) is zero: its **replay checks are clean** (`replay_ok`). That says the book and the replay
checks are right, not that the shadows' fill truth is: the exact fill rule does not model halts (F2 below). A
symbol-day **trades normally** (`trading_ok`) when no span in which its Stock Trading Action (`H`) state was other
than T overlaps the regular session (`S` Q to `S` M; 09:30-16:00 unless the source's `session_hours` say
otherwise). It is **exact** (`exact_ok`) when both hold.

**Per-symbol attribution.** A unit whose summed replay checks have a non-zero flag replays each partition of its
dataset alone. A one-partition view is a directory with a copy of `manifest.json` holding only that partition and
symbolic links to the partition directory, `instruments.parquet`, `data_quality.json` and `trades_source/`. Every
summable counter of the per-partition replays (stats, invariants, parity counts, executions checked, unknown-order
executions, price-priority violations, sweeps, both FIFO rank histograms) must add up to the unit's; `complete` says
whether it did, and `sum_mismatches` lists the counters that did not. A one-partition dataset is not replayed again
(its symbol's counts are the unit's). An attribution that raises is recorded as `{"method", "complete": false,
"sum_mismatches": [], "symbols": {}, "error": "<exception type>"}` and the unit completes; `reduce-day` then labels
every symbol of the unit.

## Shard leaf (`qt.scale.shard_manifest`, additive)

A flagged unit's leaf gains

```json
"exactness_by_symbol": {
  "method": "per_partition_replay",
  "complete": true,
  "sum_mismatches": [],
  "symbols": {"<symbol>": {"<flag field>": <count>, ...}, ...}
}
```

listing the flagged symbols only. An exact unit's leaf has no such key, so its leaf hash, and a day root over exact
units only, are unchanged. A flagged unit's leaf hash changes, and so does any day root that contains one;
`verify_unit.py` also checks a re-run flagged unit in the pre-attribution form (the leaf without the key).

## Split and plan

`split_manifest.json` `routing.test_issues` and `<date>.directory.json` `test_issues`: every Stock Directory symbol
whose Authenticity (ITCH 5.0 `R` offset 29) is `T`, whether routed or not. `plan.py` excludes them with the reason
`test issue (Stock Directory authenticity T)`, after the `ZxZZT` prefix rule; a split manifest without the key
excludes by prefix only.

`split_manifest.json` `trading_state`: `{rule, h_messages, symbols_with_h, non_t_spans: {symbol: [[start_ns,
end_ns | null, state, reason], ...]}}` from every `H` of the day, routed or not: each `H` whose state is not T opens
a span that ends at the symbol's next `H` (null: the end of the day); times in ns since midnight; symbols whose
every `H` says T are not listed. `plan.py` copies a universe symbol's spans into its universe entry
(`non_t_spans`, only when it has any) and records `trading_state_recorded` in the source.

Each plan source also records `exclusions_sha256` (sha256 of the canonical JSON of that day's exclusions sorted by
symbol). Sources enter the `run_id`, so the same day planned under other exclusion rules (test issues, `--exclude`)
gets another run id.

## Day reduction (`atlas.py reduce-day`)

- `cells_all.parquet` gains the key columns `replay_ok`, `trading_ok` and `exact_ok` (bool);
  `strata_by_tier.parquet` gains `exact_ok`, and strata are summed apart for exact and other symbol-days.
- `day_manifest.json` gains `flagged_symbols` (every symbol-day that is not `exact_ok`: `{symbol: {unit_id,
  replay_ok, trading_ok, flags, attribution, primary_cause, causes, non_t_spans, planned_shadows}}`, where `flags`
  are the replay-check flags and `non_t_spans` the spans that overlap the session), `trading_state` (`{known,
  source: "plan" | "split_manifest" | null, session_ns, rule, symbols_not_trading,
  symbols_not_trading_with_shadows, symbols_not_trading_replay_ok}`, plus `problem` when a supplied split manifest
  was refused), `symbol_exactness_rule` and `symbol_flags_problems`; each `flagged_units` entry gains `flags` and
  `flagged_symbols` (its replay-flagged symbols).
- Trading state: from the plan (`trading_state_recorded`), or from `--trading-state`, the day's split manifest for
  a plan made before plans carried it, accepted only when its date and compressed sha256 equal the plan source's.
  Otherwise unknown: every symbol-day counts as `trading_ok` and `trading_state.known` is false.
- Attribution sources of replay flags, in order: the leaf's `exactness_by_symbol` (`leaf`); a `qt.scale.symbol_flags`
  sidecar (`--symbol-flags`) entry for the same `unit_id` whose `shard_leaf_hash` equals the shard's `leaf_hash` and
  whose trace `reproduced` the shard (`symbol_flags`); otherwise every partition of the unit is flagged with the
  unit's flags as `unit:<field>` (`unit`). An incomplete attribution counts as none. A sidecar whose `schema`,
  `run_id` or `date` differ from the plan's is not used at all. A sidecar entry for another leaf hash, one that did
  not reproduce the shard, or one whose symbols differ from the leaf's is reported in `symbol_flags_problems`.
  Causes come from the sidecar only, and only for a symbol whose sidecar entry names the same unit as a matching
  unit entry.

## Combination (`atlas.py combine --flag-policy label|exclude [--allow-legacy-days]`)

- `label` (the function's and the CLI's default): every symbol-day stays. `per_symbol.parquet` gains
  `n_flagged_days` (int) and `flags` (string, `"; "`-joined per day: `"<MMDDYYYY>: <field>=<count>, ... (<primary
  cause>), not trading <hh:mm:ss>-<hh:mm:ss | end> (<state> <reason>)"`, null when exact).
  `leaderboard_by_tier.parquet` gains `n_flagged_symbol_days`, `flagged_n` (shadows of those symbol-days) and
  `flagged_share` (`flagged_n / n`); `maps_by_tier.parquet` gains `flagged_n` and `flagged_share` (its strata are
  summed per tier, so they cannot count symbol-days).
- `exclude`: rows with `exact_ok = false` are dropped from `leaderboard_by_tier`, `per_symbol` and `maps_by_tier`.
  It refuses (`ValueError`) a day reduced before per-symbol exactness or with an unknown trading state, unless
  `allow_legacy_days`; then a day without `exact_ok` drops every symbol of its `flagged_units` (from
  `cells_all`; its per-tier strata cannot be split and stay whole) and its halts stay unknown.
- The runners that publish atlases default to `exclude`: `local_day.py --flag-policy`, `modal_app.py` `combine` and
  `combine_days` (`flag_policy`), which record the policy in their summaries.
- `atlas_manifest.json` gains `flag_policy`, `allow_legacy_days`, `flagged_symbol_days` (every flagged symbol-day
  with flags, spans, attribution and causes), `flagged_cell_rows` (cell rows with `exact_ok = false` before the
  policy), `days_without_symbol_exactness` and `days_without_trading_state`.

## Flag tracing (`trace_flags.py`, `qt.scale.symbol_flags`)

Per flagged unit: the bucket's sha256 must equal the unit spec's; the whole-unit replay must reproduce every summable
counter of the shard's `exactness`; the per-partition attribution must be complete; and an independent reference
(`itch_ref.scan`) must give, per symbol, the engine's `stats.events`, `executions_checked`,
`crossed_books_after_last`, `negative_spreads` and `price_priority_violations`; the engine's
`invariants.unknown_order_refs`, `invariants.fills_unknown_order` and `executions_unknown_order` must be zero (the
reference, like the mapper, makes no record for an unknown order); and the ingest's `itch_unknown_order_ref` must
equal the reference's count of unknown-order messages over the unit (reported as `(unit)`). A symbol with a
directory entry and no records (no partition) agrees when the reference has no events for it. Exit code 1 when any
check fails. `--restream-command SPLIT_MANIFEST` prints the `itch_split.py` command that re-creates the traced
units' buckets (every symbol of those buckets, the same `--buckets`) and each bucket's expected sha256.

**Causes** (`itch_ref.CAUSES`, most specific first): `test_symbol` (Authenticity T); `ipo_quoting_period` (state
H/P/Q and an IPO reason, or an IPO flag Y or `K` message before the first state T); `halt_or_pause_quoting` (state
H/P/Q); `cross_execution` (a violation by a non-printable `C`, with the nearest `Q` print); `pre_open_continuous`,
`post_close_continuous` (state T before the `S` Q time or from the `S` M time); `regular_session` (state T in
between, no cross: unexplained). A crossed episode (a maximal run of crossed events of one symbol) takes the cause of
its first event for all its events, because Nasdaq disseminates a halt or IPO cross as executions that continue after
the `H` T resumption message; `events_in_state_T` counts them per episode.

**Sidecar** `symbol_flags.json`: `{schema: "qt.scale.symbol_flags", schema_version: 1, run_id, date, units:
{unit_id: {shard_leaf_hash, reproduced, exactness_by_symbol}}, symbols: {symbol: {unit_id, flags, causes,
primary_cause, engine_matches_reference}}, causes}`. It holds counts and cause names only; the per-unit trace files
hold message positions and prices from the source file and stay out of the repository.

## Open: the engine breaks the halt contract (finding F2)

`docs/design/spec.md` 1.2 already sets the behaviour: "No auction modelling: shadows resting through an auction or
halt are censored (`end_state = censored_gap`)"; `docs/concepts/exact-truth.md` ("Where exactness stops") and
`docs/venue-semantics.md` ("Out of scope") repeat it to users. The engine does not do it. The ITCH mapper drops `H`
(Stock Trading Action) messages, so the replay has no trading state, and the exact fill rule fills shadows while a
symbol is halted, paused or before its IPO cross (`crossed` and `crossed_at_live` fills against a displayed book the
venue does not match); `sample_shadows` also submits shadows in those spans. The failing reproduction is
`scripts/scale/findings/repro_halt_shadow_fill.py`: its shadow resting into the halt should end `censored_gap` at
the halt with no fill, and its shadow submitted during the halt should never be live and end `censored_gap`.

The fix the contract asks for: carry the trading state per instrument (a status record or table from `H`, with
state and reason); end live shadows `censored_gap` when the state leaves T; never make a shadow live while the state
is not T; have the sampler skip non-T spans or tag those shadows; and have the invariant counters and the rank
oracle report crossed events and violations in non-T spans and crosses separately (F1). Taking part in the halt or
IPO cross at the cross price is not part of the v0.1 contract; it is a possible v0.2 extension.

Until the fix lands, `trading_ok` keeps those symbol-days out of atlases built with `--flag-policy exclude`, and the
user-facing pages that state the censoring (`docs/concepts/exact-truth.md`, `docs/venue-semantics.md`) need a
caveat; the integrator owns those edits.
