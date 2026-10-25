# Extension: forecast referee (`queuetruth forecast`)

Spec text for the forecast referee, to merge into `docs/design/spec.md` (new section after the scorecard, and rows
in 3.10, 4.3 and 4.4). Python only; no Rust engine change.

## Purpose

Timing truth and edge truth for forecasts of any outcome: a forecast counts only if it was on a verifiable record
before its question could be known; it is scored by proper rules against baselines that use no future information;
and its edge is converted into fractional-Kelly taker bets against quotes with spread, depth and fees.

## Ledger (`queuetruth.forecast.ledger`)

- File: JSON Lines, one `qt.forecast_entry` per line, canonical JSON (`ledger.canonical_json`, byte-identical to
  `queuetruth.schemas.runid.canonical_json`; strings escaped by the C JSON encoder for speed): RFC 8785 except that
  integers are written exactly (RFC 8785 numbers are IEEE doubles, and `recorded_at_ns` is above 2^53). A
  third-party verifier must parse integers exactly; `forecast verify` is the reference verifier. Lines are parsed
  with NaN and Infinity refused; a non-finite number, an overflowing literal or a payload that fails its model is a
  ledger problem (exit 1 from `verify`, `QT-INPUT-002` from `score`), never a crash.
- Entry: `seq` (0-based, contiguous), `prev_sha256` (64 zeros for seq 0), `recorded_at_ns`, `clock`
  (`system` | `declared`), `payload_sha256 = sha256(canonical_json(payload))`, `payload` (`forecast` |
  `resolution`), `entry_sha256 = sha256(canonical_json(entry - entry_sha256))`.
- Append: exclusive `flock`, full verification first (a broken chain is `QT-INPUT-002` and is never extended), a
  payload already present is `QT-INPUT-003`, a second resolution of a question is `QT-INPUT-003` unless
  `allow_correction`, and a forecast whose nonce was generated (`generated_nonce[i]`; the CLI sets it for rows
  without a nonce unless `--allow-repeat`) is `QT-INPUT-003` when the same content (payload without the nonce) is
  already in the ledger or the batch. System time is `max(now, last system time)`; `declared_at_ns` (one time, or
  one per payload; the CLI's `declared_at` column, non-decreasing) writes `clock: declared`. Readers
  (`verify_ledger`, `score`) take a shared `flock`.
- Verify: per line valid JSON and model, seq, link, both hashes, system clock non-decreasing, no duplicate payload,
  final newline.
- Commitment `qt.forecast_commitment`: `ledger_id` (hash of entry 0), head seq / hash / time, counts, `chain_rule`,
  `engine_version`; no forecast content. `check_commitment(entries, c)` is empty when entry `c.head_seq` still has
  hash `c.head_sha256` and entry 0 hash `ledger_id`. The value a timestamp certificate signs is
  `commitment_digest(c) = sha256(canonical_json(c))`.
- Forecast payload: `question_id`, `forecaster`, `forecast` (`binary {p}` | `categorical {probs}` |
  `continuous {dist: normal {mean, sd} | quantiles {levels, values} | samples {samples}}`), `as_of_ns?`, `nonce`
  (64 hex). Resolution payload: `question_id`, `kind`, `outcome`, `outcomes?`, `ordered`, `resolved_at_ns`,
  `info_cutoff_ns?` (not after `resolved_at_ns`), `void`, `group?`, `source?`; validated on every read (binary outcome
  in {0, 1}, categorical outcome in `outcomes`, distinct `outcomes`, outcome required unless void, finite numbers).

## Eligibility (the leakage guard)

Per forecast entry, first failing rule: `unresolved`, `void`, `after_resolution_entry` (seq after the question's
first resolution entry), `after_cutoff` (`recorded_at_ns >= min over resolution entries of min(info_cutoff_ns,
resolved_at_ns)`; so a correction can move the cutoff earlier, never later), `short_lead` (`< min_lead_ns`),
`declared_clock` (unless `allow_declared_clock`), `unanchored` / `anchored_after_cutoff` (with `require_anchor`: no
anchor covers the entry, or the minimum `anchored_at_ns` over anchors with `head_seq >= seq` is at or after the
cutoff), `as_of_after_commit`, `kind_mismatch`, `outcome_mismatch` (labels differ from `outcomes`, or the sum
differs from 1 by more than 1e-6). Then `policy`: `last` | `first` (the others `superseded`) | `mean`. Nothing
scored: `QT-INPUT-002` "NOT SCORED: 0 of N forecasts scored (status counts)". `unresolved: fail` makes any
`unresolved` entry `QT-INPUT-002`. `REJECT_STATUSES` (the leakage and validity guard, `rejections`, `n_rejected`)
excludes `unresolved` and `void`, which are counted per forecaster in `selection` instead.

Anchors (`anchors: [{commitment: FileRef, anchored_at_ns}]`, `require_anchor`): every commitment must satisfy
`check_commitment` (else `QT-INPUT-002`); echoed in `ledger.anchors` with `commitment_digest`. `recorded_at_ns` still
sets lead time and the market baseline (an anchor is an upper bound on when the entry existed, so using it for the
baseline would penalise honest forecasters who anchor periodically; using `min(recorded_at, anchored_at)` would
differ from `recorded_at` only for a writer whose clock runs fast).

Selection (`ForecasterScore.selection`, per forecaster, over the questions of its forecast entries):
`unresolved`, `void` (entries), `voided_after_outcome` (first resolution not void, effective void),
`outcome_corrected` (both not void, outcomes differ), `cutoff_tightened` (effective cutoff before the first
resolution's). Each non-zero count is a warning.

## Scores and statistics

- Losses per forecast: binary Brier, log (`-ln max(p_y, log_eps)`), spherical (`1 - p_y / ||p||`); categorical
  multi-class Brier, log, spherical, RPS (ordered); continuous CRPS (normal closed form; empirical for samples;
  `2 sum_k w_k QS_tau_k` for quantiles with cell weights split halfway between levels). Per (forecaster, question)
  the mean over the policy's forecasts.
- Baselines (reference `baseline:<name>`), from information at `recorded_at_ns`: `market` = mid of the last quote
  with `ts <= recorded_at_ns` and `recorded_at_ns - ts <= market_max_age_ns` (default `pnl.max_quote_age_ns`, else
  no limit; binary; the quote's `ts` and age go to `forecast_scores.parquet`, and a median age above 6 h warns); `climatology` = Laplace frequency over questions of the same pool (group;
  else kind and outcome set) with `resolved_at_ns < recorded_at_ns`, at least `climatology_min_prior`; continuous
  only with a group (empirical CRPS); `uniform` = 0.5 or 1/K.
- Clusters: question (`cluster: question`) or group. Bootstrap: `stable_seed(seed, [b"bootstrap", test_id])`,
  multiplicities identical to `queuetruth.stats.block_counts`, drawn in chunks of at most 4,000,000 cells; test ids
  `forecast/<kind>/<metric>/<subject>/<reference>`, `.../mean`, `.../calibration`, `forecast/pnl/<who>`.
- Comparison: `mean_diff = mean(loss_ref - loss_subject)`, `skill = 1 - sum loss_subject / sum loss_ref`, percentile
  CIs, `p_bootstrap` (two-sided, `queuetruth.stats.bootstrap_p_two_sided`), DM on differences ordered by
  `(resolved_at_ns, question_id)`: Bartlett LRV with `dm_lag` or `floor(4 (n/100)^(2/9))`, HLN factor with
  `h = lag + 1`, t(n - 1). When some cluster holds more than one row, DM runs on the `G` cluster sums
  `u_g = sum_{i in g}(d_i - dbar)` ordered by each cluster's last row: `Var(dbar) = G LRV_u / n^2`, lag rule, HLN
  factor and df use `G` (`units` in `DmTest`); at lag 0 this is CR1. Constant non-zero differences give a null
  statistic and p-value. `p_holm`: Holm over every comparison with the same (kind, metric), on DM p-values.
  Pairwise comparisons: `pairwise` = `all` | `none` | `k` (the k forecasters with the most scored questions, ties by
  name); `lead_diff_ns` = median over common questions of the subject's lead minus the reference's (per question,
  the median lead of its scored forecasts). A forecaster with fewer than 30 distinct clusters scored warns.
- Calibration: equal-width bins `min(floor(p K), K - 1)`; ECE `sum_k |sum_{i in k}(y_i - p_i)| / N` with the basic
  cluster-bootstrap interval `[max(0, 2 ECE - q_hi), max(0, 2 ECE - q_lo)]`; Wilson 95% per bin; consistency
  resampling with one uniform per cluster, `y*_i = 1[u_{g(i)} < p_i]` (`stable_seed(seed, [b"consistency",
  test_id])`, `G` uniforms per draw) for
  `ece_if_calibrated`, `ece_p_value = (1 + #{ECE* >= ECE}) / (1 + B)` and per-bin 2.5% / 97.5% bands; Murphy from
  `queuetruth.stats.murphy`. Categorical: top-label confidence.
- Power (primary metric per kind against each baseline): `delta = mean d`, `sd = sd(d)`, design effect = cluster-robust
  over i.i.d. variance of the mean (at least 1); power of the two-sided paired t test with `n / deff` effective
  questions via the noncentral t; `required_n` = smallest n reaching `power` (normal formula above 1e6, None above
  1e9).

## Executable PnL (`queuetruth.forecast.pnl`)

One candidate per (forecaster, binary question): the last scored forecast. Decision `t0 = recorded_at_ns`, arrival
`t = t0 + latency_ns`; no bet when `t >= cutoff`. `price_at: decision` (default): the decision quote is the last
with `ts <= t0`; side, price and size come from it; at arrival the order fills at the last quote with `ts <= t` when
that side's price is no worse (else `price_moved`), at that price, size capped by `max_participation` of the arrival
depth. `price_at: arrival`: the decision quote is the last with `ts <= t`. No bet when there is no decision quote or
it is older than `max_quote_age_ns` at the decision. Sides: YES at the ask (ask depth), NO at `1 - bid` (bid depth). `unit_fee(P) = per_contract +
notional_rate P + quadratic P (1 - P)`; `c = P + unit_fee(P)`; the side with `p_side - c > min_edge`; Kelly
`f* = (p_side - c) / (1 - c)`; contracts `floor_lot(kelly_fraction f* equity / c)`, capped at
`floor_lot(max_participation depth)` and by cash; the order fee is `C unit_fee(P)` rounded up to `round_up_to`;
dropped when `C (p_side - P) - fee <= 0`. Bankroll path: events by time, settlements first on ties, every position
with the same `resolved_at_ns` settled together, one equity point (cash + open cost) per settlement instant.
Reported: counts and skip reasons (`no_quotes`, `no_quote_yet`, `stale_quote`, `after_cutoff`, `no_edge`,
`no_depth`, `size_zero`, `fees_exceed_edge`, `insufficient_cash`, `price_moved`), final wealth, total return, log
growth, growth per bet, max drawdown, fees, spread paid against the mid, contracts, depth- and cash-limited counts,
`path_cash_limited` (`insufficient_cash + cash_limited > 0.10 n_candidates`; warns), hit rate, `isolated_growth`
(fresh bankroll per decision, 0 when no bet, cluster-bootstrap CI) and `frictionless_growth` (full Kelly at the
decision-time mid, no latency: equals the log `mean_diff` against `baseline:market` on the same questions when the
baseline has no tighter age limit).

## Documents and tables

JSON (Python-only, added to `DOC_MODELS` by `queuetruth.forecast.schemas`, listed in
`crates/schema/src/tests.rs` `PYTHON_ONLY`): `qt.forecast_entry`, `qt.forecast_commitment`,
`qt.forecast_ledger_summary`, `qt.forecast_scorecard`. The score spec (`ForecastScoreSpec`) is echoed in the
scorecard; `run_id` per 7.3 with `ledger` and `market` as file refs.

Arrow (Python-only, `queuetruth.forecast.tables`, metadata `qt.schema`, `qt.schema_version` =
`queuetruth.schemas.arrow.ARROW_SCHEMA_VERSION`, `qt.engine_version`): `qt.forecast_eligibility` (with
`anchored_at`), `qt.forecast_scores` (with `market_quote_ts`, `market_quote_age_ns`), `qt.forecast_bets` (with
`decision_quote_ts`). `forecast power` and `forecast simulate` print schema-less JSON objects. They are not in
`queuetruth.schemas.arrow.SCHEMAS` because that registry must equal `qt_schema::schema` exactly.

## CLI (4.4)

`queuetruth.cli.main` hands command lines whose first command word is `forecast` to
`queuetruth.forecast.cli.forecast_main` (argparse; globals `--format`/`--output-format`, `--quiet`, `--verbose`,
`--log-level`, `--seed`, `--threads` accepted anywhere). Exit 1 only for `verify` with problems (`QT-GATE-001`).

Not yet in the clap grammar, so `queuetruth --help`, `docs/reference/cli.md` and the standalone `queuetruth-core`
binary do not list it. Proposed rows for `crates/cli/src/grammar.rs` (all `Implementation::Python`), plus golden
cases in `crates/cli/tests/golden/cli_argv.json` and a `_dispatch` branch calling `forecast.cli.run`:

| Path | Flags |
|---|---|
| `forecast commit` | `--forecasts` (req), `--ledger` (req), `--forecaster`, `--at`, `--commitment-out`, `--allow-repeat` |
| `forecast resolve` | `--resolutions` (req), `--ledger` (req), `--at`, `--correct`, `--commitment-out` |
| `forecast verify` | `--ledger` (req), `--commitment` |
| `forecast score` | `--ledger` (req), `--out` (req), `--market`, `--market-max-age` (Dur), `--commitment` (repeatable), `--anchored-at` (repeatable, Ts), `--require-anchor`, `--unresolved`, `--pairwise`, `--price-at`, `--policy`, `--baselines`, `--cluster`, `--bins`, `--bootstrap-b`, `--alpha`, `--power`, `--min-edge`, `--dm-lag`, `--min-lead` (Dur), `--allow-declared-clock`, `--log-eps`, `--climatology-min-prior`, `--no-pnl`, `--kelly-fraction`, `--bankroll`, `--latency` (Dur), `--max-participation`, `--max-quote-age` (Dur), `--pnl-min-edge`, `--lot`, `--fee-per-contract`, `--fee-notional-rate`, `--fee-quadratic`, `--fee-round-up-to` |
| `forecast power` | `--edge` (req), `--sd` (req), `--alpha`, `--power`, `--design-effect`, `--n` |
| `forecast simulate` | `--out` (req), `--questions`, `--edge-sd`, `--half-spread`, `--depth`, `--start` |

## Errors

No new codes: `QT-USAGE-001` (CLI, unit-less duration), `QT-CONFIG-001` (spec field), `QT-INPUT-001` (missing
file), `QT-INPUT-002` (row, timestamp, ledger integrity, failed anchor, unresolved with `unresolved: fail`, NOT
SCORED), `QT-INPUT-003` (duplicate payload, repeated forecast without nonce, or second resolution), `QT-GATE-001`
(verify). Integer timestamps outside 1990 to 2262 as ns are `QT-INPUT-002` naming the likely unit.
