# Score forecasts before you trust them

**Goal:** turn a forecaster's claim ("my probabilities beat the market") into three checked numbers: whether each
forecast was on record before its question could be known (timing truth), how good the probabilities are by proper
scores, calibration and paired tests against honest baselines, and whether the edge survives the spread, the fees
and the displayed depth when it is bet with fractional Kelly (edge truth).

**You need:** `pip install queuetruth`. A forecast is any probability for a binary or categorical question, or a
predictive distribution for a number. Quotes are optional; without them you get every number except the market
baseline and the PnL.

The commands are `queuetruth forecast commit | resolve | verify | score`, plus `power` and `simulate`. They run in
Python only (no Rust engine, no market data download).

## 1. A world with a known edge

`forecast simulate` writes the inputs of this page from a synthetic world in which the right answer is known: the
market prices question i at `q_i`, the true probability is `p_i = expit(logit(q_i) + 0.6 Z_i)`, and the outcome is
drawn from `p_i`. Forecaster `sharp` reports `p_i`, so its edge over the market is real; forecaster `crowd` reports
the market price plus noise, so it has none.

<!-- check-docs: run -->
```sh
queuetruth forecast simulate --out ex
head -3 ex/forecasts.csv ex/resolutions.csv ex/quotes.csv
```

```text
==> ex/forecasts.csv <==
question_id,forecaster,p,nonce
q0000,sharp,0.7117,8d0acefdfc3fb005a573a4d9563d16ce2303bb2065eaa704a1f779cc308858dc
q0001,sharp,0.6831,160c635bdaab4b1d0958f6fae567f85295fcfe8228fa5be5814faa8643d7b84f

==> ex/resolutions.csv <==
question_id,outcome,resolved_at,info_cutoff
q0000,1,2026-10-25T19:15:29.000000000Z,2026-10-25T18:15:29.000000000Z
q0001,1,2026-10-25T19:15:29.000000000Z,2026-10-25T18:15:29.000000000Z

==> ex/quotes.csv <==
question_id,ts,best_bid,best_ask,bid_depth,ask_depth
q0000,2026-10-24T18:15:29.000000000Z,0.6217,0.6417,500,500
q0001,2026-10-24T18:15:29.000000000Z,0.8270,0.8470,500,500
```

The 400 questions open now (`--start` changes that) and resolve a day later, with the information cutoff an hour
before. Each quote has a 2-cent spread and 500 contracts a side. Every value, nonces included, depends only on
`--seed` and `--start`.

## 2. Commit forecasts before their questions resolve

<!-- check-docs: run -->
```sh
queuetruth forecast commit --forecasts ex/forecasts.csv --ledger ledger.jsonl --commitment-out commitment.json
```

```text
forecast commit: verified, appended 800, head seq 799 34bdbdbb47495cec25c525d4d6dd3a62f2c6302f2c349bd09cda761b4d604389
```

`ledger.jsonl` is an append-only file with one `qt.forecast_entry` per line, in canonical JSON:

```json
{"clock":"system","entry_sha256":"93a2cf16...","payload":{"as_of_ns":null,"forecast":{"kind":"binary","p":0.7117},"forecaster":"sharp","nonce":"8d0acefd...","question_id":"q0000","type":"forecast"},"payload_sha256":"14219234...","prev_sha256":"0000000000000000000000000000000000000000000000000000000000000000","recorded_at_ns":1792869331189283000,"schema":"qt.forecast_entry","schema_version":1,"seq":0}
```

Canonical JSON here is RFC 8785 (keys sorted by UTF-16 code units, no whitespace, floats in ECMAScript form) with
one exception: integers are written exactly. RFC 8785 reads every number as an IEEE double, and `recorded_at_ns`
(about 1.8e18) is above 2^53, so a stock JCS library rounds it and computes different hashes. To verify a ledger
yourself, parse integers exactly (Python's `json` does; in JavaScript use a BigInt-aware parser), or run
`queuetruth forecast verify`, which is the reference verifier.

- `payload_sha256` is the sha256 of the payload's canonical JSON, `entry_sha256` the sha256 of the entry without
  itself, and `prev_sha256` the previous entry's hash. Changing, dropping, reordering or inserting any entry breaks
  every later hash.
- `recorded_at_ns` is the writer's UTC clock and never goes backwards along the chain (`clock: system`). `--at`
  records a time you supply instead, marked `clock: declared`; `score` ignores such entries unless you pass
  `--allow-declared-clock`.
- The `nonce` (256 random bits; generated when your file has none) makes the payload hash unguessable, so a
  published hash does not reveal the forecast.
- Appends hold an exclusive file lock, verify the whole chain first and refuse a ledger that does not verify
  (`QT-INPUT-002`); `verify` and `score` read under a shared lock, so they never see half of an append. Committing
  the same payload twice is refused (`QT-INPUT-003`). A row without a nonce gets a fresh one on every run, so
  re-running the same commit would add the same forecasts again: a row without a nonce whose content (forecaster,
  question, forecast, `as_of`) is already in the ledger is refused too (`QT-INPUT-003`) unless you pass
  `--allow-repeat`. A changed forecast for the same question is a new entry.

Forecast files may be `.csv`, `.tsv`, `.jsonl`, `.json` or `.parquet`, one row per forecast:

| Column | Meaning |
|---|---|
| `question_id` | required |
| `forecaster` | required, or `--forecaster NAME` for the whole file |
| `p` | binary: P(outcome = 1) |
| `probs` | categorical: a JSON object `{"label": probability}`; a sum within 1e-3 of 1 is renormalised, anything else is refused |
| `mean`, `sd` | continuous, Gaussian |
| `quantiles` | continuous: `{"0.1": 3.2, "0.5": 4.0, "0.9": 5.1}`, or the columns `levels` and `values` |
| `samples` | continuous: a JSON list of ensemble members |
| `as_of` | optional: your own information timestamp (refused if it is later than the commit) |
| `nonce` | optional: 64 lowercase hex characters |
| `declared_at` | optional, for importing a timestamped track record: the time to record for each row (`clock: declared`, scored only with `--allow-declared-clock`); every row needs one and they must not decrease, because ledger order is time order |

Timestamps everywhere are integer ns since the epoch or ISO 8601 with an explicit offset
(`2026-11-03T23:59:00Z`); a date-time without an offset is refused because its instant is ambiguous. An integer
before 1990 or after 2262 as ns is refused as well, with the unit it probably is: epoch seconds, ms or us read as ns
would put every quote in January 1970 and turn a post-resolution quote into the "market at commit".

### What the chain proves, and what it does not

The chain proves order and integrity: every forecast entry sits before or after every resolution entry, and nothing
was changed afterwards. It does not prove absolute time on its own, because whoever holds the file can rewrite it
from the first changed entry onward and re-chain the rest. `commitment.json` closes that gap:

```json
{
  "schema": "qt.forecast_commitment",
  "schema_version": 1,
  "ledger_id": "93a2cf169104d94b3381f118c1a5de7ca4fbdab4af60a4a30f8114cd60f9adb6",
  "head_seq": 799,
  "head_sha256": "34bdbdbb47495cec25c525d4d6dd3a62f2c6302f2c349bd09cda761b4d604389",
  "head_recorded_at_ns": 1792869331262097000,
  "n_entries": 800,
  "n_forecasts": 800,
  "n_resolutions": 0,
  "hash_alg": "sha256",
  "chain_rule": "entry_sha256 = sha256(canonical_json(entry - entry_sha256)); ...",
  "engine_version": "0.1.1"
}
```

It names the head hash, which commits to every earlier entry, and carries no forecast content. Publish it, send it
to a counterparty or have a timestamp authority sign `sha256(canonical_json(commitment))` before the questions
resolve; later, `forecast verify --commitment` shows that the ledger still contains those entries unchanged. A
rewritten ledger that re-chains cleanly still fails against the earlier commitment (tested in
`python/tests/forecast/test_ledger.py::test_full_rewrite_is_caught_by_a_published_commitment`).

`score` can make the anchor part of the verdict, so timing truth no longer rests on the writer's clock:

```sh
queuetruth forecast score --ledger ledger.jsonl --out score \
  --commitment commitment.json --anchored-at 2026-10-24T18:20:00Z --require-anchor
```

Each `--commitment` (repeatable) is paired with the `--anchored-at` time a third party attested it (the timestamp
on the certificate or the publication). `score` refuses to run when an anchored commitment no longer matches the
ledger (`QT-INPUT-002`), echoes the anchors in the scorecard's `ledger.anchors`, and with `--require-anchor` scores
a forecast only when a commitment covers it (`unanchored` otherwise) and the earliest such anchor is before the
question's cutoff (`anchored_after_cutoff` otherwise). The writer's `recorded_at_ns` still sets the lead time and
the market baseline. Without `--require-anchor` the anchors are checked and counted (`n_anchored`), nothing more.

## 3. Record the resolutions

<!-- check-docs: run -->
```sh
queuetruth forecast resolve --resolutions ex/resolutions.csv --ledger ledger.jsonl
queuetruth forecast commit --forecasts ex/late.csv --ledger ledger.jsonl
queuetruth forecast verify --ledger ledger.jsonl --commitment commitment.json
```

| Column | Meaning |
|---|---|
| `question_id`, `outcome`, `resolved_at` | required (`outcome`: 0/1, yes/no or true/false; a label; a number) |
| `kind` | `binary`, `categorical` or `continuous`; inferred when absent (categorical when `outcomes` is given, binary for 0/1, else continuous) |
| `outcomes` | categorical labels, a JSON list or `a\|b\|c`; `ordered: true` makes them ordinal (enables RPS) |
| `info_cutoff` | when the outcome became knowable, if earlier than `resolved_at` (a poll closing, a data release) |
| `void` | the question was annulled; its forecasts are not scored |
| `group` | related questions: shared climatology, and one bootstrap cluster with `--cluster group` |
| `source` | free text: where the resolution came from |

A second resolution of a question is refused unless you pass `--correct`. A correction changes the outcome that is
scored (the last resolution entry wins). The cutoff is the earliest one over every resolution entry, so a
correction can move it earlier (when it says the outcome was knowable sooner) and never later: a forecast that was
late stays late, and a forecast in the window the correction closes becomes `after_cutoff`.

Resolutions, cutoffs and voids are trusted input. Whoever writes them shapes the record: resolving only the
questions you got right, voiding a loss, or leaving bad calls unresolved all improve a scorecard without a single
late forecast. They should come from the question's source or from the referee, never from the forecaster. The
scorecard makes the effect visible: every forecaster has a `selection` block (forecasts on unresolved and on voided
questions, questions voided by a correction after an outcome, questions whose outcome or cutoff a correction
changed), each non-zero count is a warning, and `--unresolved fail` refuses to score while any forecast's question
is still open.

The third command plays a forecaster called `late` who commits 0.99 or 0.01 for every question after the
resolutions are on the ledger. `score` must not count any of it.

## 4. Score

<!-- check-docs: run -->
```sh
queuetruth forecast score --ledger ledger.jsonl --market ex/quotes.csv \
  --fee-quadratic 0.07 --fee-round-up-to 0.01 --out score
```

```text
forecast scorecard 6c387eb83cd867a3: 800 of 1200 forecasts scored, 400 rejected by the leakage and validity guard, 400 of 400 questions resolved

crowd: 400 scored, 0 rejected
  binary brier: 0.2003 [0.1814, 0.2168] (n 400)
  vs baseline:market: skill -0.028 [-0.056, 0.002], DM p 0.0978, Holm p 0.126 (n 400)
  vs baseline:uniform: skill 0.199 [0.129, 0.266], DM p 2.38e-07, Holm p 9.5e-07 (n 400)
  calibration (binary): ECE 0.0769 [0.0285, 0.1005], expected if calibrated 0.0524, p 0.042
  power vs baseline:market: at the observed edge -0.00543 (noisy): n/a questions
  PnL per question (isolated): -0.00182 [-0.00398, 0.00044], frictionless -0.01479; 39 bets of 400, fees 29.42, spread 20.12
  bankroll path (CASH-LIMITED: an arbitrary subset of bets): return +22.01%, growth/bet 0.00510, max drawdown 0.00%
  PnL no bet: insufficient_cash 233, no_edge 121, size_zero 6, fees_exceed_edge 1

sharp: 400 scored, 0 rejected
  binary brier: 0.1849 [0.1695, 0.2035] (n 400)
  vs baseline:market: skill 0.051 [-0.004, 0.100], DM p 0.0631, Holm p 0.126 (n 400)
  vs baseline:uniform: skill 0.260 [0.191, 0.331], DM p 1.65e-11, Holm p 8.27e-11 (n 400)
  calibration (binary): ECE 0.0459 [0.0000, 0.0520], expected if calibrated 0.0525, p 0.663
  power vs baseline:market: at the observed edge 0.00991 (noisy): 937 questions
  PnL per question (isolated): 0.00707 [0.00198, 0.01260], frictionless 0.02120; 20 bets of 400, fees 27.68, spread 19.23
  bankroll path (CASH-LIMITED: an arbitrary subset of bets): return +22.80%, growth/bet 0.01027, max drawdown 0.00%
  PnL no bet: insufficient_cash 307, no_edge 70, size_zero 2, fees_exceed_edge 1

crowd vs sharp (binary brier): mean diff -0.0153 [-0.0279, -0.0027], DM p 0.023, Holm p 0.0689 (n 400)
file: forecast_eligibility.parquet (1200 rows)
file: forecast_scores.parquet (800 rows)
file: forecast_bets.parquet (800 rows)
warning: crowd: the PnL bankroll path is cash-limited (cash skipped or trimmed 235 of 400 candidates), so its return and drawdown describe an arbitrary subset of bets; read isolated_growth
warning: sharp: the PnL bankroll path is cash-limited (cash skipped or trimmed 308 of 400 candidates), so its return and drawdown describe an arbitrary subset of bets; read isolated_growth
warning: 400 forecast(s) rejected by the leakage and validity guard (see rejections and forecast_eligibility.parquet)
```

Every number above is deterministic for a given ledger content and `--seed`; the run id and the hashes change
because the ledger records the wall clock. `python/tests/forecast/test_engine.py::test_worked_example_numbers`
rebuilds this example at a fixed start and pins every number quoted here. How to read it:

- **The guard worked.** All 400 `late` forecasts were rejected (`after_resolution_entry`), so the perfect-hindsight
  forecaster does not appear at all.
- **Sharp beats the market, but 400 questions cannot show it yet.** Its Brier skill over the market is 0.051 with a
  95% CI of [-0.004, 0.100] and a Diebold-Mariano p of 0.063. If the observed edge (0.0099 Brier points per
  question) were the true one, 937 resolved questions would be needed for 80% power. That number is itself a noisy
  estimate; for planning, pass `--min-edge` with the smallest edge worth detecting and read `n_required_min_edge`.
  Against always-50% the skill is overwhelming, which is why that baseline flatters everyone.
- **Crowd is worse than the market it copies** (skill -0.028) and its ECE p-value of 0.042 says its noise shows up
  as miscalibration. Head to head, crowd loses to sharp by 0.0153 Brier points (DM p 0.023; 0.069 after Holm over
  the scorecard's family of tests).
- **The money answer is the per-question line.** `isolated` growth (each bet sized on a fresh bankroll, 0 when there
  is no bet) separates them: sharp 0.00707 [0.00198, 0.01260] per question, crowd -0.00182 [-0.00398, 0.00044].
  Sharp's frictionless growth of 0.0212 is what a full-Kelly bettor would earn at the mid with no fees; quarter
  Kelly, the spread and the quadratic fee keep about a third of it.
- **The bankroll path is flagged, not trusted.** Both paths end up (+22.0% and +22.8%) because all 400 questions
  are open at once: quarter-Kelly stakes use up the cash after 20 to 40 bets (`insufficient_cash`) and those few
  bets, in ledger order, decide the path. The scorecard marks such a path `CASH-LIMITED` (`path_cash_limited` in the
  JSON) and warns. Max drawdown is 0% because every question settles at one instant and equity goes straight from
  the bankroll to the final value.

`--format json` prints the same content as a `qt.forecast_scorecard` document, also written to
`score/forecast_scorecard.json`.

### The leakage guard

Every forecast entry gets one status, in this order (the first rule that fails names it):

| Status | Rule |
|---|---|
| `unresolved` | no resolution entry yet (not scored; counted in `selection`, not as a rejection) |
| `void` | the question was annulled (not scored; counted in `selection`, not as a rejection) |
| `after_resolution_entry` | the forecast's ledger position is after the question's first resolution entry |
| `after_cutoff` | `recorded_at_ns >= min(info_cutoff, resolved_at)` over every resolution entry of the question |
| `short_lead` | less than `--min-lead` between the commit and the cutoff |
| `declared_clock` | the commit time was supplied with `--at` or `declared_at` (unless `--allow-declared-clock`) |
| `unanchored` | with `--require-anchor`: no anchored commitment covers the entry |
| `anchored_after_cutoff` | with `--require-anchor`: the earliest anchor covering the entry was attested at or after the cutoff |
| `as_of_after_commit` | the forecaster's own `as_of` is later than the commit |
| `kind_mismatch` | a binary forecast of a categorical question, and so on |
| `outcome_mismatch` | categorical labels differ from the question's outcomes, or the probabilities do not sum to 1 |
| `scored`, `superseded` | eligible; `--policy last` (default) scores the last eligible forecast per forecaster and question, `first` the first, `mean` averages all of them |

`forecast_eligibility.parquet` lists every forecast entry with its status, cutoff and lead time. A ledger that does
not verify is never scored, and a run where nothing is eligible fails with `NOT SCORED` and the count per status
(`QT-INPUT-002`, exit 2).

### Proper scores

All scores are losses (lower is better), averaged per question with a question-clustered bootstrap CI:

| Kind | Scores |
|---|---|
| binary | Brier `(p - y)^2`; log `-ln p_y` (floored at `--log-eps`, 1e-6; the clipped count is reported); spherical `1 - p_y / sqrt(p^2 + (1 - p)^2)` |
| categorical | Brier `sum_k (P_k - e_k)^2` (range 0 to 2); log; spherical; ranked probability score for ordered outcomes, which equals the binary Brier for two outcomes |
| continuous | CRPS: closed form for `mean`/`sd` (Gneiting and Raftery 2007), the empirical-distribution form for `samples`, the quantile-score sum for `quantiles` (exact as the levels get dense; `|q - y|` for a lone median) |

Known values are tested: for example CRPS(N(0, 1), 0) = 0.2336950, and the normal CRPS agrees with numerical
integration to 1e-7 relative.

### Calibration

For binary forecasts (and the top-label confidence of categorical ones) the scorecard has a 10-bin reliability
diagram with Wilson intervals, the expected calibration error with a cluster-bootstrap CI, and the Murphy
decomposition. ECE is biased upward in small samples: a perfectly calibrated forecaster still shows about 0.05 on
400 questions. So the CI is the basic bootstrap interval `[2 ECE - q_hi, 2 ECE - q_lo]` (clipped at 0), which
reflects that bias instead of sitting above the estimate, and the scorecard draws outcomes from the forecasts
themselves (`--bootstrap-b` times) to give the ECE a calibrated forecaster would show with the same forecasts
(`expected if calibrated`), a p-value for "calibrated", and a consistency band per bin. Those draws use one uniform
per cluster, so forecasts of one question (`--policy mean`) or of one group (`--cluster group`) move together
instead of counting as independent evidence. In the tests, 3,000 calibrated forecasts give a p-value above 0.05 and
the same events reported overconfidently give one below 0.01; for a calibrated forecaster on 12 groups of 8 copies
of one event the test rejects at most 10% of the time at the 5% level, where independent draws rejected over 80%.

### Skill and paired tests

Each forecaster is compared with three baselines, all computed from what was known when each forecast was recorded:

| Reference | Forecast |
|---|---|
| `baseline:market` | the mid of the last quote at or before the commit, no older than `--market-max-age` (default `--max-quote-age`, else no limit), for binary questions with quotes; a quote one ns later is never read |
| `baseline:climatology` | the Laplace-smoothed frequency of outcomes of questions in the same group (or of the same kind and outcome set) that resolved before the commit, once `--climatology-min-prior` (5) exist; for grouped continuous questions, the empirical distribution of those outcomes |
| `baseline:uniform` | 0.5, or 1/K |

Climatology computed from all resolved questions would leak the future into the baseline; here it never does
(tested: questions that resolve after a commit do not enter that forecast's baseline).

`forecast_scores.parquet` records the quote each baseline used (`market_quote_ts`, `market_quote_age_ns`), and the
scorecard warns when their median age at the commit is above 6 hours: a sparse quote file turns "market at
commit" into an old snapshot, which flatters forecasters who commit after news.

Every comparison, against a baseline or between two forecasters on their common questions, reports the mean loss
difference and the skill `1 - loss / loss_reference` with question-clustered bootstrap CIs (`--cluster group`
resamples groups of related questions), and a Diebold-Mariano test on the per-question differences ordered by
resolution time: Newey-West long-run variance (lag `floor(4 (n/100)^(2/9))` unless `--dm-lag`), the
Harvey-Leybourne-Newbold correction and Student t with n - 1 df. With lag 0 it is exactly the paired t test, and
its variance matches statsmodels' HAC estimator to 1e-10 relative. Measured over 4,000 simulations at the 5% level,
it rejects 5.5%, 4.7% and 5.1% of independent null samples of 40, 120 and 400 questions, and 6.9%, 6.4% and 6.4%
of samples whose differences are MA(1) with coefficient 0.5: the HAC correction leaves the test about 1.5 points
too liberal under strong serial correlation.

When a cluster holds more than one question (`--cluster group`, or several forecasts of one question with
`--policy mean`), the DM test runs on the cluster sums of the differences, ordered by each cluster's last
resolution, with `G - 1` df for `G` clusters (at lag 0 this is the CR1 cluster-robust t test). The scorecard prints
`n 96 in 12 clusters`. Correlated questions (one election, markets on one event) are therefore never counted as
independent evidence: in 200 null worlds of two equally informed forecasters on 12 groups of 8 questions that share
one outcome, the scorecard's DM test rejected 2.5%, Holm 0.5%, the cluster bootstrap 9% and the calibration test of
a calibrated forecaster 5.5% at the 5% level. Treating the 96 questions as independent, the DM test rejected 23% of
2,000 such samples and independent calibration draws rejected over 80% (both are tests in
`python/tests/forecast/`). The bootstrap is somewhat
liberal with that few clusters, so every forecaster with fewer than 30 independent questions or clusters gets a
warning. `p_holm` adjusts the DM p-values over every comparison of the scorecard with the same kind and metric;
differences that are constant but not zero have no DM p-value (and a warning) rather than p = 0.

Forecaster pairs are compared on their common questions. `lead_diff_ns` is the median of the subject's lead minus
the reference's: a forecaster who commits close to the cutoff has more information than one who committed early,
and the comparison does not adjust for that. `--pairwise none` turns pairs off, and `--pairwise K` keeps the pairs
among the K forecasters with the most scored questions (the full set grows with the square of the forecasters).

### How many questions you need

`power` in the scorecard answers "is this sample big enough": the questions needed for 80% power at `--min-edge`
(the planning number), and, as noisy estimates, the observed edge (mean loss difference against the reference), its
standard deviation, the design effect from clustering, the power at the observed edge and the questions needed for
it. Power computed at the observed edge is a function of the p-value, so it explains a result rather than predicting
the next one. The same calculation stands alone:

<!-- check-docs: run -->
```sh
queuetruth forecast power --edge 0.01 --sd 0.1 --n 400
```

```text
edge: 0.01
sd: 0.1
alpha: 0.05
power: 0.8
design_effect: 1.0
n_required: 787
n: 400
power_at_n: 0.5140864602248497
```

It uses the noncentral t distribution of the paired test. Two checks back it: in 1,500 simulations at the required
n the paired test rejected 80% of the time (within 3.5 points), and in a known-edge world the questions it asked for
gave 80% power within 7 points over 300 simulated samples. `power` and `simulate` print small schema-less objects
with `--format json` (the fields shown above; `simulate` gives `out`, `seed`, `questions` and `files`), since they
describe a calculation or the files written rather than a result to keep.

### Executable PnL

With `--market`, each (forecaster, binary question) becomes at most one taker order, from the last scored forecast;
later updates are not traded. The market file has one row per quote update of the question's YES contract:

| Column | Meaning |
|---|---|
| `question_id`, `ts` | the question and the quote time |
| `best_bid`, `best_ask` | prices in [0, 1] (payout 1); either may be empty for a one-sided book |
| `bid_depth`, `ask_depth` | contracts displayed at the touch |

The order is priced from the last quote at or before the commit (none, or older than `--max-quote-age`: no bet)
and arrives at the commit time plus `--latency` (at or after the cutoff: no bet). It is a marketable limit order: it
fills at the arrival quote's price when that is no worse than the price it was decided at, at most
`--max-participation` of the arrival depth, and otherwise does not fill (`price_moved`). `--price-at arrival`
instead prices it from the arrival quote, which models a bettor at the venue with no market-data delay and is more
optimistic. With no latency the two agree. The order buys YES at the ask or NO at one minus the bid, whichever has
an edge after the fee, `C (fee_per_contract + fee_notional_rate P + fee_quadratic P (1 - P))` rounded
up to `--fee-round-up-to` per order (0.07 and 0.01 is the historical Kalshi taker formula). The stake is
`--kelly-fraction` (0.25) of the Kelly fraction `(p - c) / (1 - c)` for a contract costing `c`, in whole `--lot`s,
at most `--max-participation` of the displayed depth and the cash on hand; a bet whose expected value after the
rounded fee is not positive is dropped. Positions settle at `resolved_at`.

Three growth numbers come back:

| Field | Meaning |
|---|---|
| `isolated_growth` | mean log return per question with a quote, each bet sized on a fresh bankroll; CI by cluster bootstrap. The per-question edge after costs, whatever the overlap of questions |
| `frictionless_growth` | full Kelly at the mid in force at the commit, with no spread, fees, depth limit or latency |
| `growth_per_bet`, `total_return`, `max_drawdown`, `path_cash_limited` | the bankroll path: arrivals and settlements in time order, equity at cost with one point per settlement instant (positions that settle together settle together); `path_cash_limited` is set when cash skipped or trimmed more than 10% of the candidates |

`frictionless_growth` equals the log-score `mean_diff` against `baseline:market` on the same questions (when the
baseline has no tighter quote-age limit), and this identity is what makes the PnL a referee rather than a
backtest: a full-Kelly bettor at price `m` multiplies wealth by `p/m` or `(1 - p)/(1 - m)`, so its log growth is
exactly the log-score difference. The tests check it question by question and on the worked example, check that a
forecaster who knows the truth recovers the analytic expected
quarter-Kelly growth after a 2-cent spread inside the bootstrap CI over 6,000 questions, and check that a
forecaster with noise but no information loses after the spread and the quadratic fee.
`forecast_bets.parquet` holds every candidate with the quote it was priced from (`decision_quote_ts`), the quote it
met (`quote_ts`), the side, price, size, fee, cost, payout or the reason for no bet.

Quotes you feed in carry their source's terms. Prediction-market data you do not own may not be redistributed, and
the scorecard and `forecast_bets.parquet` contain quote-derived values (prices, mids, the market baseline); check
[Data and licensing](../data-and-licensing.md) before you share them with a third party.

## Outputs

| File | Content |
|---|---|
| `forecast_scorecard.json` | `qt.forecast_scorecard`: run id, spec, ledger head and anchors, counts, per forecaster selection counts, scores, calibration, skill, power and PnL, pairwise comparisons, the first 1,000 rejections |
| `forecast_eligibility.parquet` | `qt.forecast_eligibility`: every forecast entry, its status and the time an anchor first covered it |
| `forecast_scores.parquet` | `qt.forecast_scores`: every scored forecast, its losses, each baseline's losses on the same question, and the market quote the baseline used |
| `forecast_bets.parquet` | `qt.forecast_bets`: every PnL candidate (with `--market`) |

The run id follows the usual rule: the spec without `out`, with the ledger and market files replaced by their
sha256. The JSON Schemas are under `schemas/json/qt.forecast_*.schema.json`.

## Limits

- The local ledger orders and seals entries; absolute time needs the commitment published or certified by someone
  else before resolution, and `--require-anchor` to make `score` depend on it.
- Resolutions are trusted input (see section 3); the scorecard counts what they did but cannot tell a right void
  from a convenient one.
- The market baseline and the PnL cover binary questions (YES-contract quotes). Categorical questions get the
  climatology and uniform baselines; continuous questions get climatology when they share a `group`.
- The PnL takes liquidity at the touch only: it does not walk the book beyond `bid_depth`/`ask_depth`, rest limit
  orders or model queue position. Many questions open at once make the bankroll path cash-limited; read
  `isolated_growth` for the per-question edge, or lower `--kelly-fraction`.
- Pairwise comparisons grow with the square of the forecasters: 40 forecasters on 200 shared questions make 2,340
  comparisons. Use `--pairwise K` or `none` for large fields.
- Speed, measured on an 8-core Apple M1 Pro laptop (16 GB) shared with other work (load average 60 to 80 at the time),
  verifying a 30,000-entry ledger took 1.7 to 2.3 s, and scoring its 20,000 forecasts (10,000 questions, two
  forecasters) with 1,000 bootstrap resamples, a market file and PnL took 13 s.
