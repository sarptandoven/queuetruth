# Changelog

All notable changes to queuetruth are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).
The Python package, the Rust crates and `queuetruth._core` always share one version.

## [Unreleased]

### Added

- `ROADMAP.md`: what is being worked on and in what order (the Fill-Realism Atlas is `W1`).
- **`referee` GitHub Action** (`actions/referee`, `uses: sarptandoven/queuetruth/actions/referee@<ref>`): runs a
  simulator's command on every referee bug-class fixture (`$QT_DATASET`, `$QT_SCHEDULE` in, `$QT_FILLS` out),
  referees each class against the exact FIFO engine with the usual tolerances, writes a table to the job summary,
  `summary.json` and a shields.io endpoint `badge.json`, and fails the step when a class fails. The runner,
  `actions/referee/run.py`, also works outside GitHub. Guide: [Referee my simulator](docs/how-to/referee-your-simulator.md)
  section 4.
- **Fill-Realism Atlas site** (`scripts/scale/atlas_site.py`, ROADMAP W1): a static site from an `atlas.py combine`
  directory. For each liquidity tier it shows every fill model's fill-rate bias against the exact replay at every
  horizon with its 95% interval, sorted by size, and leads with one plain-words sentence. It also shows the atlas and
  day Merkle roots with the verify command, and writes `data/leaderboard.json` and the manifest. Pages run no
  script and load nothing from the network, and the output is byte-identical for the same inputs. `--source iex`
  puts IEX's required attribution on every page; `--per-symbol` adds a per-symbol page.
- `.github/workflows/fuzz.yml`: the fuzz crate's seed, regression and known-crash tests on every change to a parser,
  and a weekly libFuzzer campaign over every target (`fuzz/run.sh`, crashes uploaded as artifacts).
- `.github/workflows/release-nautilus.yml`: releases the `queuetruth-nautilus` plugin to PyPI on a
  `queuetruth-nautilus-vX.Y.Z` release (version checks, sdist and wheel, the plugin's tests on the built wheel with
  nautilus_trader 1.231.0 and the newest 2.x, Trusted Publishing with attestations; a dry run on manual dispatch).
  `release.yml` now runs only for `vX.Y.Z` tags.

### Changed

- README rewritten to fit one screen: the one-command demo first, the findings, a "pick your path" table and the
  evidence in short; the full tables stay in `docs/RESULTS.md` and `docs/testing.md`. The docs entry page lists the
  0.2 workflows (certificates, agent sandbox, prediction markets, forecasts, Freqtrade and Hummingbot).
- Docs: the service reference names the `zoo_` id prefix and says rate limits are per tenant; the self-hosting guide
  lists `zoo` among the `jobs submit` kinds and names the commands that stay local.

### Fixed

- **DBN files that crashed or exhausted the pinned decoder are input errors.** A DBN prelude announcing 100 to 103
  metadata bytes made `dbn` 0.71.0 panic, and one announcing up to 256 MiB made it allocate that much before reading
  any of it (a 24-byte file was enough). `qt_ingest::dbn::FrameCheck` now rejects a metadata block shorter than its
  fixed fields (`MIN_METADATA_BYTES`, 104) or longer than the file can hold (its size for an uncompressed file, the
  decompression guard's limit for a compressed one) as `QT-INPUT-002` before the decoder runs. The fuzz
  reproducers moved from `fuzz/known-crashes/` to `fuzz/regressions/dbn/`, and the `dbn` and `ingest` fuzz targets
  no longer skip them. The Parquet panics in `fuzz/known-crashes/parquet/` are still open.
- **Trading halts censor shadows end to end (finding F2).** Ingest records the spans in which each instrument is
  not in continuous trading in `data_quality.json` (`trading_halts`: `instrument_id`, `ts_start`, `ts_end`,
  `state`, `reason`; schema 1.2, additive, omitted when empty, never part of the verdict): Nasdaq ITCH Stock
  Trading Action `H` states `H`, `P` and `Q` and Operational Halt `h` of market `Q`, and IEX DEEP+ Trading Status
  `H`, `O` and `P` and Operational Halt `O`. `replay`, `why`, the zoo, calibration, features and the agent sandbox
  apply them: a shadow live when a halt starts ends `censored_gap` there, one whose `ts_live` falls in a halt never
  goes live. In the zoo both model arms follow (a halt start ends every live model pair, and no model activates a
  shadow inside a halt), so no model books a fill while an instrument is halted. `replay` adds one warning line
  when a run with shadows applies halts. `scripts/scale/findings/repro_halt_shadow_fill.py` now exits 0. Nothing
  changes for datasets without halts (the published Nasdaq tables included) or for datasets ingested before this
  release, which carry no spans: re-ingest those. DBN, NYSE and crypto sources record no spans yet. Rust API:
  `qt_ingest::halts`, `qt_replay::{halts_of, partition_halts}`, `qt_zoo::runner::StreamOptions::trading_halts`,
  `qt_calib::collect::collect_events_with_spans`, `qt_features::extract::extract_events_with_spans`. Design note:
  [halt censoring](docs/design/extensions/halt-censoring.md) H.6.

## [0.2.0] - 2026-10-25

Data schema 1.2: every new document and table is additive and registered with the others (`queuetruth schema list`,
`schemas/`); the changes older code or clients notice are the Migration and Upgrade notes under Changed. Design notes
for each change are listed in [spec section 10](docs/design/spec.md#10-design-extensions).

### Real-data results

- **Leaderboard rerun under the 0.1.1 common-population rule** (`docs/RESULTS.md` section 4; both public Nasdaq
  ITCH days, 3,000 never-cancel shadows per day, 16 models). Faithful mode now scores every model on the same 2,529
  and 2,518 orders (471 and 482 left out because a faithful hftbacktest model hit #312 on them); corrected mode, the
  exact arm and the abort counts are identical to 0.1.0, and every faithful cell moved by at most 1.02 pp. The raw
  ITCH and CSV ingests give the same 3,456 cells. NautilusTrader's default touch model (`nt_prob_touch[p=1]`) reads
  +3.08 / +2.50 pp at 1 s on the scored orders; run alone on all 3,000 orders it gives the published +3.13 / +2.80 pp.
  Section 4b (breadth) and the RD-9 held-out scores were not rerun and still use the 0.1.0 rule.
- **NautilusTrader 2.0.0rc7 cross-check rerun after the harness fix** (see Fixed): 0 orders fill after their cancel
  in every configuration, and every published 2.0 queue number is unchanged (-0.03 / -0.37 / -0.37 pp and
  -0.03 / -0.03 / -0.07 pp at 1 s / 10 s / 60 s, 18 and 10 differing orders, and the L3 result). At `p = 0.29`
  Nautilus's realised rates are within 0.5 pp of the expectation (largest gap 0.43 pp), not 0.2 pp.
- **Oracle agreement on the public ITCH days is now 3,000 of 3,000** mirror shadows on both days from the raw bytes
  (before: 2,992 and 2,985), and 2,997 and 3,000 of 3,000 on the prototype's CSV conversion; every former
  disagreement is classified in `docs/design/extensions/oracle-disagreements.md` (see Fixed).
- **RD-12 Bitstamp rerun with the fixed converter** (see Fixed): the book is seeded from the 12th snapshot (11
  skipped, 1.19 s later), giving 77,157 records, 76,792 events and 12,373 suppressed takers (before: 77,272, 76,907
  and 12,447); the 8 replay invariants and crossed books stay 0, and 0 of 5,407 later venue snapshots disagree with
  the book's queue order (before: 133 of 5,418). Bitfinex is unchanged.
- Conformal fill probabilities on the two Nasdaq days at `alpha = 0.1`: the L2-only hazard model calibrated on
  2019-12-30 covers 0.770 at 0.1 s and 0.795 at 1 s on 2020-01-30 (fills within 1 s were 2.2 times as frequent on
  day 2) and 0.903 to 0.906 from 10 s on; the martingale alarms in 6 of 27 horizon x landmark cells, all at 1 s or
  shorter, 3 of them past the family-wise threshold. Restricting day 2 to the calibration block's clock window lowers
  coverage further (0.667 at 0.1 s), so the shortfall is a day effect. Calibrated and tested on random halves of one
  day, coverage is 0.894 to 0.902 at every horizon with no alarm. Zoo models with smoothed p-values cover 0.893 to
  0.914 across the days at 1 s and 10 s, with no alarm.
- Fill toxicity, day one (2019-12-30, 2,879 live orders of 3,000 shadows, 16 models, 60 s fill window, corrected
  mode): from 100 ms to 10 s a sweep is a worse fill than a queue fill (paired difference -2.30 [-2.95, -1.67] ticks
  at 1 s); every model's wrongly granted fills are profitable (+1.1 to +2.5 ticks at 1 s); most fills the hbt and
  particle models miss are hidden-print fills, which the L2 feed carries but those models ignore. At the default
  60 s markout `trade_through_only` wrongly denies the most, -0.975 [-1.499, -0.645] ticks per order (rank 1 to 1),
  the 13 hbt, particle and `nt_queue_l2` models are a tie at -0.18 to -0.20, and corrected `hbt_l3_fifo` is exact
  (0).
- Leaderboard model confidence set on the 2019-12-30 Nasdaq day (3,000 sampled shadows, corrected): 4 of 16 models
  are tied for best at 10 s, 8 at 60 s and 300 s, and 14 at 0.1 s.
- NautilusTrader calibration inside NautilusTrader (`integrations/nautilus/repro/heldout.py`): the RD-9 fit from day
  one (`p = 0.2866`) cuts the 1 s overfill on the held-out day 2020-01-30 from +2.80 pp [+2.27, +3.37] to +0.40 pp
  [+0.00, +0.79] in nautilus_trader 2.0.0rc7 and to +0.33 pp [-0.07, +0.73] in 1.231.0 (mean of 8 seeds). The paired
  order-level test of the |bias| reduction excludes zero at 0.1, 1 and 10 s on both lines (1 s: +2.40 and +2.47 pp)
  but not at 60 s on 2.0.0rc7 (+0.51 [-0.01, +1.08] pp), where the fit underfills (-0.43 pp). One calibrated run has
  a worse order-level Brier score than `p = 1` at 10 s and 60 s (significantly at 60 s on 2.0.0rc7): `p` fixes the
  fill rate, not which orders fill. `queue_position=True` at `p = 1` needs no fit and is closer to exact (1.231.0:
  -0.10 pp, Brier 0.0010 against 0.0202 for one calibrated touch run at 1 s). Order by order NautilusTrader matches
  queuetruth's prediction for the fit: 0 mismatches on the orders it predicts with certainty, and the seed-mean fill
  rate within 0.08 pp elsewhere. The refused queue-gated fit (`p = 0.871`), forced through the same check on
  1.231.0, is worse than the default queue model from 1 s on, as its trust verdict said.
- hftbacktest itself on the same orders (Nasdaq ITCH, both days, AAPL / MSFT / SPY / TSLA, 5,795 orders, 6 hbt
  models): with this order flow hftbacktest 2.4.4 with `partial_fill_exchange` stops with error 14 (#312) on every
  one of the 8 instrument-days for every model, 7.3 to 26.6 minutes after the open; its fills on the 127 to 185
  orders per model live before the stop equal queuetruth's faithful models. With the proposed fix it runs to the
  end and its fills equal queuetruth's faithful models on all 5,795 orders of every model; first-fill rates are
  within 1 pp of exact truth for every model, horizon and day (`integrations/hftbacktest/README.md`).
- Freqtrade's candle fill rule on Nasdaq ITCH (AAPL, MSFT, SPY, TSLA; 3,000 sampled orders per day, 10-minute life,
  5m candles): with the order placed at the candle open the rule is 0.48% and 0.36% overfilled overall and 10.6%
  (95% 6.3% to 17.3%) and 12.0% (6.7% to 20.8%) on touch-only candles; placed 1 to 5 seconds into the candle, as a
  Freqtrade bot places, 1.9% to 3.3% of its fills are false. Orders landing half a candle late on average (clock
  grid) give 5.07% and 5.73%.
- Shortfall decomposition on the RD-11 real orders: the identity holds on every order row of each run, and the exact
  arm with self-exclusion gives exactly zero in every bucket.
- Impact warning on the 2019-12-30 Nasdaq day: no one-lot sampled shadow is flagged, while 1,125 of 1,400
  hundred-lot shadows are flagged by queue share.
- Agent sandbox episode on TSLA (Nasdaq 2019-12-30, 10:00 to 10:30 ET, 360 steps): 537 fills and 511 orders, all
  equal to the single full replay (asserted by `python/tests/sandbox/test_realdata_demo.py`).
- Certificates: a replay of the 2019-12-30 Nasdaq dataset (6,103,262 records, 3,000 shadows) re-executed bit for
  bit by `verify-cert --rerun`.
- W1 full-market pilot (2020-01-30): its flags (4,845 crossed or locked events, 397 price-priority violations, in 7
  of 130 units) come from 8 symbols, all halted, paused or before their IPO cross: ANPC and BDTX (IPOs that day),
  RKDA and DTSS (LULD pauses), PNRL, DNJR, BRQS and IPDN (halted all day, no shadows). Every violation is a
  non-printable cross execution. The engine and an independent rebuild count the same events, executions, crossed
  and negative-spread events and violations for all 645 symbols of those units (8.62M events); no order-book rebuild
  or replay-check defect (`scripts/scale/findings/README.md`).
- Own-account live-fill kit: a real 2-minute public Bitstamp capture (2026-10-24, `btcusd` and `usdtusd`: 14,604
  frames, no gap) replays consistently with all 1,223 of the venue's later snapshots (834 exact, 389 explained by
  channel skew of at most 38 messages, 0 unexplained); the RD-12 10-minute BTC/USD capture with all 5,407 from its
  first unambiguous seed. Measured feed facts: `live_orders` and trade stamps are whole milliseconds, the snapshot
  channel runs up to 165 messages ahead of `live_orders`, and same-millisecond orders at one price queue by order
  id. On 200 stand-in orders per market of the 2-minute capture, the exact arm reproduces every outcome. Removing
  one `order_created` of a short-lived order from the real `btcusd` stream is caught 30 times in 30 with the REST
  seed.

### Added

- **`queuetruth demo`**: a real-data answer in under a minute. It streams the first minutes of the public Nasdaq
  TotalView-ITCH 5.0 sample day 2019-12-30 (stopping at 09:45:10 ET: 312 to 317 MB of the 3.52 GB file), keeps AAPL,
  MSFT, SPY and TSLA, caches a 7.0 MB subset in the user cache directory, runs ingest, 3,000 never-cancel shadows
  and seven zoo models, and prints NautilusTrader's default fill model (queuetruth's faithful replica, which fills
  exactly the same orders as nautilus_trader 1.231.0 and 2.0.0rc7 on the published days) against the exact queue by
  horizon with the base rates: +3.6 pp at 1 s (29.4% exact vs 33.1%, 95% CI +2.6 to +4.8 pp), next to the published
  full-session +3.13 pp and the exact fill rates of both. Writes `qt-demo/demo.html` (two charts, no script, no
  external request) and `qt-demo/demo.json` (`qt.demo_summary`). Measured on an M1 Pro laptop under heavy load: 22
  to 53 s for a first run, 13 to 17 s from the cache. `--source iex` uses IEX DEEP+ 2026-10-09 (906 MB, carries
  IEX's attribution line); `--from-file` and `--url` take your own day file, prefix or subset, including the demo's
  own cached subset (same numbers and `run_id`). Downloads need consent (a prompt on a terminal, otherwise `--yes`;
  `DemoOptions.yes` in Python); `--dry-run` shows the plan. The default subsets are pinned by content sha256.
  Python API `queuetruth.demo` (`run_demo`, `plan`, `DemoOptions`): the ITCH and IEX subset logic of
  `scripts/realdata/itch_subset.py` and `scripts/iex/stream_subset.py` moved into the package as streaming filters
  with a market-time cut and exact vectorized fast paths (1.8x faster than the scalar loop on a 420 MB ITCH prefix
  and 3.3x on a 300 MB IEX prefix, measured under load; the scalar loops remain as the fallback and give identical
  output).
- **Certificates** (`queuetruth.cert`): `queuetruth certify RUN_DIR... --claim TEXT --out cert.json` writes a
  `qt.certificate` for the `--out` directories of `replay`, `zoo`, `calibrate`, `calibrate-live`, `fit-hazard`,
  `score`, `referee run` and `dq`: the spec echo, run id, engine version and per-file sha256 (raw and normalized per
  SPEC 7.2) of every run, the sha256 of every input file (datasets checked against their manifests, live-log
  directories, shadow specs, fills), claims whose `{RUN#/json/pointer}` references are stored as exact values, the
  environment and reproduction commands, sealed with the sha256 of the RFC 8785 canonical body and optionally an
  Ed25519 signature (`--key`). `certify` refuses runs whose recorded run id does not recompute from the inputs on
  disk. `--html` renders the certificate; `--attach` hashes extra files.
- `queuetruth verify-cert cert.json [--rerun]`: integrity, signature, key pin (`--trust FINGERPRINT`, `--pubkey`),
  input and output re-hashing (raw and normalized digests, sizes, dataset manifests and data spans), run-id
  binding, claim evidence and values, and linked commitments; with `--rerun` every run is executed again in a child
  process that never imports from the data directory, into a directory `verify-cert` creates, and every output must
  come back bit-identical (JSON without `timings`), or `equivalent` within the SPEC 7.2 cross-platform tolerance of
  4 ulp (a warning). Exit 1 when a check fails (`QT-GATE-001`); `--base` and `--map OLD=NEW` verify and re-execute
  a moved tree; `--work`, `--threads`, `--timeout` and `--force-rerun` control re-execution. Every field of a
  certificate used as a path is validated, so verifying a stranger's certificate never deletes or reads outside
  the certified directories.
- `queuetruth commit FILE... --out c.json` / `queuetruth reveal c.json FILE... --opening o.json`: salted, optionally
  signed hash commitments (commit now, reveal later, per-file selective opening with `cert partial-opening`) for
  forecasts and order schedules; `certify --commitment --opening` links one and records whether it was committed
  before the data. `queuetruth cert keygen|pubkey|render`: Ed25519 keys (PKCS#8 PEM, mode 0600, encrypted with
  `QT_CERT_KEY_PASSPHRASE`), fingerprints, HTML rendering. Signing needs the `cryptography` package. New documents
  `qt.certificate`, `qt.cert_verification`, `qt.commitment`, `qt.commitment_opening`, `qt.commitment_verification`
  and `qt.signing_key`. How-to: [Certify a result](docs/how-to/certify-a-result.md), with the threat model (what a
  certificate proves and what it does not); all 38 outputs of one synthetic run of each of the 8 run kinds are
  reproduced.
- **Trial ledger** (`queuetruth.ledger`, `queuetruth ledger list | stats | export | verify | add`): an append-only,
  hash-chained local record of every `zoo`, `calibrate`, `calibrate-live`, `fit-hazard`, `score` and
  `shadows sample` run (Python API and CLI) and of the strategy variants you register
  (`queuetruth.ledger.register_variant`, `register_variants` for a batch, `queuetruth ledger add`), with `run_id`, a
  configuration hash (`spec_hash`), the identity of every data input as that run's `run_id` resolves it, the code
  version (package, engine, core, source commit, your project's commit), wall-clock times, headline metrics (the
  zoo's per-model Brier and winner, a calibration's held-out score) and an optional per-trial returns or scores
  series with period ends, stored out of line and content-addressed. `n_trials(filter)` counts distinct trials: a
  rerun of the same `run_id` is one trial, a variant re-evaluated with a different result is a new one, a run linked
  by a variant counts with it, a failed or refused run is none. `selection_inputs(filter)` returns the trial count
  of one search (one study, variants by default) and the per-trial series as plain data for the Deflated Sharpe
  Ratio, and reports whether the series are aligned on the same periods (PBO, SPA, model confidence sets). Every
  read hash- and link-checks each record (`QT-LEDGER-001` on any edit, deletion, insertion or reordering);
  `ledger verify --anchor SEQ:HASH` also detects a rewritten history. Appends are serialized by a file lock and
  fsynced, so parallel runs share one chain; an append cut off by a crash is recovered or set aside. Stored in
  `$QT_LEDGER_DIR`, `<project root>/.queuetruth/ledger/` (git-ignored by default) or the per-user data directory,
  never inside a run's own input; `QT_LEDGER=off` disables it; the HTTP service never records, and test suites
  record only when asked. The count is a lower bound of what was tried. The ledger is a side output: a test runs
  the pipeline with it on and off and finds every output, summary and `run_id` byte-identical. New error codes
  `QT-LEDGER-001` (integrity) and `QT-LEDGER-002` (cannot write or lock). Concept page:
  [Trial ledger](docs/concepts/trial-ledger.md).
- **Selection-aware statistics** (`queuetruth.stats.selection`, re-exported from `queuetruth.stats` with the result
  classes `McsResult`, `PboResult`, `SpaResult` and `StepMResult`): probabilistic and deflated Sharpe ratios with the
  minimum track record length (`dsr_from_returns(..., n_trials=N)` takes a trial ledger's count), the probability
  of backtest overfitting by combinatorially symmetric cross-validation, Hansen's SPA test, Romano-Wolf StepM and
  the Hansen-Lunde-Nason model confidence set (`T_R` and `T_max`), with stationary, circular, moving-block and
  day/30-minute cluster bootstraps seeded like every other `queuetruth.stats` bootstrap. On clusters the MCS
  defaults to a wild cluster bootstrap-t, because the textbook fixed bootstrap variance keeps the whole set of 16
  equal models only 42% of the time on 13 clusters (wild: 88%). DSR reproduces the published worked example
  (0.9004); MCS (fixed), SPA and StepM equal `arch` 8.0.0 on the same bootstrap draws. Concept page:
  [Selection bias](docs/concepts/selection-bias.md).
- Zoo leaderboard model confidence set: each entry gains `first_fill_error`, `mcs_member` / `mcs_p` (per-shadow
  first-fill error, 90% MCS) and `mcs_brier_member` / `mcs_brier_p` by horizon (additive, default {}), computed on
  the leaderboard rows over the leaderboard's blocks for the L2-input models (`hbt_l3_fifo` left out). A horizon
  with fewer than 5 blocks gets null fields and an `mcs:` warning. The CLI adds a first-fill-error table with `#`
  for members and one plain-words line. The spec and run_id are unchanged.
- **`queuetruth zoo-toxicity --zoo DIR --data DATASET --out DIR`** (`queuetruth.eval.toxicity`): turns the zoo
  leaderboard into a PnL answer. It replays the zoo's shadows exactly and requires the replay to reproduce the zoo's
  exact arm shadow by shadow (`QT-INPUT-002` otherwise, unless `--allow-truth-mismatch`), tags every exact and model
  fill with its mechanism (`queue`, `swept`, `cross_on_arrival`, `crossed_resting`, `hidden_print`, `touch`,
  `particle`), every (model, order) pair with a status (`confirmed`, `phantom`, `missed`, `neither`; a model's fill
  is the one it books), measures signed mid markouts (`mo`/`mm`, ticks and bps) at 100 ms, 1 s, 10 s and 60 s, and
  splits each model's PnL error into the fills it wrongly grants, wrongly denies and gets at the wrong time
  (`bias = grant + timing + deny`), with shared block-bootstrap CIs. Models are ranked by the size of each term, with
  rank intervals and paired gaps that say which places are separated. Prints an answer-first terminal view; writes
  `summary.json` (`qt.zoo_toxicity`) and the `qt.zoo_toxicity_fills`, `qt.zoo_toxicity_markouts` and
  `qt.zoo_toxicity_shadows` Parquet tables. Concept page: [Fill toxicity](docs/concepts/fill-toxicity.md).
- **Conformal fill probabilities** (`queuetruth.model.conformal`): split and Mondrian (per landmark x stratum,
  optionally per label) conformal prediction sets and p-values for "filled within `tau`" around the learned hazard
  model's CIF or any zoo model's `p_fill`, Venn-Abers probability intervals, conformal lower and upper bounds on the
  time to the first fill, and an exchangeability martingale (Simple Jumper or power mixture, alarm by Ville's
  inequality, per cell and Bonferroni family-wise) over the calibration rows, then the test rows, that tests whether
  they are exchangeable, also when they share a session date. Refuses calibration or test rows the probabilities were
  fitted on (`train`, and `calib` for the isotonic `cif_fill`). Zoo labels follow the planned life of
  `qt.zoo_shadows` with never-cancel and cancel-bearing shadows as separate populations. `ConformalCalibrator` saves
  the calibration (`conformal_calibrator.parquet`, `qt.conformal_calibrator`) and applies it to orders without
  labels. `fit-hazard --conformal-alpha A` writes `conformal.json` (`qt.conformal_report`), `conformal_sets.parquet`
  (`qt.conformal_sets`) and the calibrator next to the model. Concept page:
  [Conformal fill probabilities](docs/concepts/conformal-fill-probability.md).
- **Implementation-shortfall decomposition**: `queuetruth score` writes `score_shortfall.parquet` (new table
  `qt.score_shortfall`): the sim-versus-live gap of every scored order, at every markout horizon, split into delay,
  spread paid, missed fills (opportunity cost at the fills' markouts), adverse selection, partial fills and fees or
  rebates, which sum exactly to the gap (exact in integer price-quantity units; to float rounding in currency). On
  units both arms filled the split follows implementation shortfall with the simulated fill as the benchmark: delay
  is the move of the mid between the two fills, spread the difference in effective half-spread, adverse selection
  the difference of the marks. Total rows per horizon and currency carry the sum, the mean per order and its 95% CI
  from the scorecard's clustered block bootstrap (one set of draws for all buckets), and count unvalued orders, fills
  without a fee and live P&L that is not decomposed. Library: `queuetruth.score.shortfall` (`decompose_order`,
  `shortfall_table`). How-to: [Decompose the sim-live gap](docs/how-to/decompose-the-sim-live-gap.md).
- **Independent-shadow impact warning** (`queuetruth.eval.impact`,
  [The impact warning](docs/concepts/impact-warning.md)). For each shadow that rested in the regular session it
  computes three figures: the square-root-law impact estimate in bps and ticks (flagged only when a `daily` table
  supplies consolidated volume; with venue volume alone it is reported as an upper bound), the shadow's share of the
  displayed queue at its price when it goes live (flagged for round lots), and its share of the volume traded over its life. Results are written as the registered table
  `qt.shadow_impact` with a `qt.shadow_impact_summary` document (counts at documented thresholds, sensitivity grids,
  distributions, input and output sha256). `zoo` and `score` add one `impact: ...` warning line when at least 1% of
  assessed shadows (in `score`, orders) are flagged, and never fail because of it. Entry point:
  `python -m queuetruth.eval.impact`.
- **Forecast referee**: `queuetruth forecast commit | resolve | verify | score | power | simulate` scores forecasts
  of any outcome. Forecasts and resolutions go into an append-only, sha256 hash-chained ledger (`qt.forecast_entry`,
  RFC 8785 canonical JSON, exclusive file lock, system clock that never goes backwards, `clock: declared` for
  supplied times); `qt.forecast_commitment` is a content-free head commitment to publish or have certified before
  the questions resolve, and `score --commitment --anchored-at --require-anchor` counts only forecasts covered by an
  anchored commitment attested before their cutoff. `score` refuses a ledger that does not verify and rejects every
  forecast recorded after its question's resolution entry or information cutoff (plus declared-clock, short-lead,
  kind and outcome mismatches), counts per forecaster what the resolutions did (unresolved, void, voided after an
  outcome, corrected), then reports Brier, log, spherical, RPS and CRPS with question-clustered bootstrap CIs;
  reliability, ECE with a CI and a cluster-aware consistency test; skill against the market mid at commit time (with
  a quote-age limit), a leak-free climatology and always-50%; Diebold-Mariano (Newey-West HAC,
  Harvey-Leybourne-Newbold, on cluster sums when questions are grouped) and cluster-bootstrap paired comparisons
  with Holm adjustment; the resolved questions needed to detect an edge; and executable fractional-Kelly PnL
  against a quote file with spread, displayed depth, latency (orders priced at the decision, filled only if the
  price has not moved against them) and a per-order rounded fee schedule (isolated per-question growth with a CI,
  the frictionless growth that equals the log-score difference against the mid, and a bankroll path flagged when
  it is cash-limited). Outputs: `qt.forecast_scorecard`, `forecast_eligibility.parquet`, `forecast_scores.parquet`,
  `forecast_bets.parquet`. Library: `queuetruth.forecast`. How-to:
  [Score forecasts before you trust them](docs/how-to/score-forecasts.md). Validated on simulated worlds with a
  known edge: the Brier edge over the market and the quarter-Kelly growth after a 2-cent spread are recovered inside
  their CIs, the paired test reaches the promised power at the computed sample size, a no-information forecaster
  loses after costs, and the scorecard's tests keep their level under a null with correlated question groups.
- **Prediction-market kit: `queuetruth pm fixtures | ingest | calibrate | score`** (`queuetruth.pm`). Reads your own
  Kalshi captures (WebSocket orderbook, trades, fills and order updates; REST queue positions, fills, orders,
  markets, series and incentive programs) or Polymarket captures (CLOB market and user channels, Gamma market
  metadata, and an optional Polygon log export of the CTF Exchange) into an L2 dataset per market on the YES price
  ladder, the same book without your own orders, and your canonical live log. `queuetruth zoo` runs on these
  datasets, and the kit's queue replay gives the same fills as its corrected mode. `pm fixtures` writes the invented
  example captures the how-to walks through (no venue data).
  - Kalshi sessions and sequence numbers: the orderbook frames of every input are merged by receive time, a
    reconnect (a new `subscribed`, a restarted `seq`, or a market moving to a new `sid`) gaps the book until its next
    snapshot and censors orders resting through it, duplicated frames are dropped, prints are deduplicated by trade
    id across the WebSocket and REST exports, and a REST orderbook of a streamed market is checked against the stream
    instead of rewriting the book.
  - On-chain constrained replay for Polymarket: `OrderFilled` / `OrdersMatched` of CTF Exchange v1 and v2 (and the
    Protocol V2 Exchange) decoded from `eth_getLogs` JSON or CSV (Keccak-256 implemented in Python), every resting
    order that filled in operator order, the queue ahead of each of your orders at its first fill (under price-time
    priority, which Polymarket's docs do not state; the kit assumes it), and a check of every print's side against
    the taker orders of its transaction.
  - Queue calibration against Kalshi's official queue positions (or the queue truth at your fills), with the
    `cancels_ahead` / `risk_adverse` queue bounds, bounds coverage, a held-out test with a bootstrap interval, a
    check against the bounds midpoint, a check on the orders that never filled, and a trust verdict in the form of
    `calibrate-live`'s (`usable` or `refused`, with reasons; `pm score` warns when it scores a refused fit). Each
    hftbacktest family also comes in an `atomic` variant that reads a decrease as one order leaving (Kalshi publishes
    each exchange action on its own), and the fit ranks models by an order-weighted RMSE (`--loss mae` available).
    On a generated 130-minute capture the atomic variants cut the test RMSE from 8.25 to 7.67 contracts and the MAE
    from 3.39 to 2.76; the generator cancels one order per action, which is what the atomic reading assumes, so the
    gain is partly by construction.
  - Reward-aware market-making PnL: spread capture minus adverse selection minus fees plus rewards (Kalshi Liquidity
    Incentive Program, Polymarket liquidity rewards and maker rebates), under the calibrated model and the
    `trade_through_only` / `front_of_queue` fill bounds, a per-order trading-PnL bound, markouts, and your live fills
    against the replay (fill-rate bias, McNemar, bracket violations; for a model fitted on the same capture, also on
    its held-out orders). Fees follow each venue's published formulas, including Kalshi's fee rounding and
    accumulator (reproducing the filed fee tables to the cent); each formula names its source and the date it was
    read.
  - How-to: [Score market making on Kalshi or Polymarket from my own captures](docs/how-to/prediction-market-making.md).
    JSON Schemas of the kit's documents are in `python/queuetruth/pm/schemas/`. The kit is for self-hosted use on
    your own data only: it makes no network calls and ships no venue data. A generated 130-minute Kalshi capture
    (50,000 exchange events) takes a few seconds of CPU per command.
- **Agent sandbox** (`queuetruth.sandbox`): a simulated exchange over one replayed instrument-day of an MBO dataset
  in which an agent's orders fill exactly as the recorded FIFO queue would have filled them. The agent's clock moves
  only forward; it sees the book, prints and its own orders up to `clock - feed_latency_ns`, and acts only at the
  times it advances to; limit (day or IOC), pegged (priced at arrival), market (IOC at a collar) orders, cancels and
  priority-keeping partial cancels take effect after a deterministic per-order latency. Every step re-runs the exact
  replay of the episode's prefix with the open orders on a scratch copy that physically ends at the visible cut (no
  look-ahead by construction; feed gaps enter it at their start, as the engine applies them); `finish` runs one full
  replay of every committed order on the original files and proves, fill by fill and order by order, that it equals
  what the agent was told (`consistency`). The scorecard (`qt.sandbox_scorecard`) gives PnL after maker/taker fees,
  fill rates by order class, markouts and adverse selection, execution cost against the decision-time mid, overlap
  diagnostics and a certificate; the committed order log (`qt.sandbox_order_log`) hash-chains every instruction with
  the information it was made on, its `open` entry commits to the engine version, dataset and full spec, and
  `queuetruth.sandbox.verify` checks the header and the partition files' sha256, then re-executes the log entry by
  entry (rejected instructions included, in the form they were validated in).
- **Sandbox MCP tools** (`queuetruth.mcp.sandbox_tools`): `qt_sandbox_start`, `qt_sandbox_observe`,
  `qt_sandbox_submit`, `qt_sandbox_cancel`, `qt_sandbox_advance`, `qt_sandbox_finish` (a compact
  `qt.sandbox_finish` summary by default; `detail`, `include_log`), `qt_sandbox_list` and `qt_sandbox_close`, added
  to a server with `register_sandbox_tools(app)` and served with `python -m queuetruth.mcp.sandbox_tools [--out DIR]`;
  datasets resolved and contained under `QT_MCP_ROOT` before any file is read, nothing written under it, idle
  sessions evicted when a new one needs the slot. How-to:
  [Give an agent a realistic exchange](docs/how-to/agent-sandbox.md).
- **Zoo runs over the HTTP API.** `POST /v1/zoos/estimate`, `POST /v1/zoos` (Idempotency-Key required),
  `GET /v1/zoos` and `GET /v1/zoos/{zoo_id}` take a `ZooSpec` whose dataset and shadows are tenant ids and run
  `queuetruth.run_zoo` as a new `zoo` job kind (ids `zoo_<ULID>`; `qt.job.kind` gains `zoo`; `GET /v1/jobs?kind=zoo`).
  The summary (`qt.zoo_summary`) is stored with the run, every output file (`summary.json`, `zoo_predictions`,
  `zoo_errors`, ...) downloads through `/v1/jobs/{job_id}/files`, usage records `shadow_orders` and `zoo_pairs`
  (shadows x models), retention clears expired zoo summaries, and `QT_JOB_TIMEOUTS_S` accepts `zoo`. Migration
  `0008_zoo_jobs.sql` adds the `zoos` table (forced row-level security, as every tenant table) and the job kind.
  `queuetruth.client` (`Client.estimate/submit/get_run("zoo", ...)`, `queuetruth jobs submit zoo`) and the remote MCP
  server (`qt_get_zoo`, `qt_submit_zoo` with `--allow-submit`, `qt_estimate` kind `zoo`) know the kind. The zoo
  estimate scales with `particles` and `bootstrap_b`.
- **List bounds on zoo and calibration specs.** At most 64 zoo model ids (no duplicates; each shaped
  `family[key=value;...]` with a family the engine knows) and 32 horizons per zoo or calibration spec; beyond them a
  submit or estimate answers 422 `validation-error` (`QT-CONFIG-001`), listing these and the existing
  `QT_COMPUTE_LIMITS` violations together. Bad parameter values are checked by the engine in the job, which fails
  with its `QT-CONFIG-001` message.
- **MCP run mode** (`queuetruth mcp serve --local --allow-run`, or `QT_MCP_ALLOW_RUN=1`, or
  `python -m queuetruth.mcp.server --allow-run`): `qt_local_sample_shadows`, `qt_local_run_replay` and
  `qt_local_run_zoo` run on local datasets without the service or Postgres, and `qt_local_why` runs under the same
  limits. Inputs must lie in `QT_MCP_ROOT`, `QT_MCP_DATA_ROOTS` or the run root (realpath, no symlink out), and none
  of these may be `/`, the home directory or a parent of it; outputs go to `QT_MCP_RUN_ROOT/<kind>/<run_id>` (named
  in the result's `_meta`) through a staging directory, and the same spec returns the finished run instead of
  running again. Each run is a fresh `python -I` child with a minimal environment, killed with its whole process
  group on timeout or cancel, under `QT_MCP_RUN_*` limits: wall clock, CPU seconds, output file size and total and
  free disk (watched while it runs), memory (a watchdog on the physical footprint on macOS, which counts compressed
  pages that RSS misses, and on `VmRSS` on Linux), threads, particles, bootstrap draws, models, horizons and shadow
  counts. POSIX only. Without `--allow-run`, `queuetruth mcp serve` still reads `QT_MCP_ALLOW_RUN`.
- **Optional MCP tool modules.** The stdio server imports `queuetruth.mcp.sandbox_tools` when installed and calls its
  `register_sandbox_tools(server)` (tools added with `QtMcpServer.add_tool` or written into the registry); a missing
  module is skipped, a broken one is logged and rolled back, a registered one is not registered twice, and
  `QT_MCP_OPTIONAL_TOOLS=0` turns the hook off. `build_server` registers optional modules only when asked.
- **NYSE order-by-order reader** (`ingest --format nyse-ibf`, `IngestSpec.format = "nyse_ibf"`, `qt_ingest::nyse`):
  the TAQ NYSE Integrated Feed CSV files of NYSE, NYSE American, NYSE Arca, NYSE National and NYSE Texas
  (`EQY_US_<market>_IBF_<channel>_<date>`, gzip or raw), read from the TAQ Products Client Specification v2.4b. Add,
  Modify (a same-price size decrease keeps priority; a new price or a larger size goes to the back), Delete, Replace
  (new Order ID, back of the queue), Order Execution (printable, and auction allocations flagged
  `PUBLISHER_SPECIFIC`), Non-Displayed Trade (hidden prints), Cross Trade (cross prints) and Add Order Refresh (the
  book is cleared and rebuilt after a publisher failover, inside a feed gap). A failover is treated as one of the
  whole channel, as the specification describes it: a symbol that had resting orders but got no refresh has its
  book cleared at its next add (or at the end of the file), inside a feed gap, unless it first references one of its
  resting orders. Dates, channels and the market come from the file names; inputs are read in canonical order, so
  instrument ids do not depend on the order given or on the symbol filter. Symbol Sequence Numbers are checked per
  symbol (a jump is a feed gap that censors shadows); channel sequence holes, which the TAQ product leaves for
  omitted control messages, are not. A clock check warns when SourceTime is not in the configured time zone, and a
  file without one order message is counted and named. Guide:
  [Read NYSE order-by-order data](docs/how-to/nyse-order-level.md).
- **`nyse` rule set, the first whose allocation is not FIFO.** NYSE allocates by Setter Priority and then round lots
  per Participant (Floor brokers, the DMM, the Book Participant; NYSE Rule 7.37(b)); its sister markets allocate by
  price-time (NYSE American since its move to Pillar on 2017-07-24; its NYSE MKT days before that, Floor-based
  parity, are labelled like NYSE). The feed does not say which Participant entered an order, so on NYSE
  queuetruth's FIFO queue ranks are approximations, and every NYSE dataset says so: the non-INFO counter
  `nyse_parity_ranks_approximate` (the number of its instrument-days on a parity market, traded or not;
  `nyse_parity_executions` counts their executions) makes its verdict `warnings`, replay repeats it in its
  `data_quality.reasons`, `queuetruth dq` reports it, and `--strict` fails (the guide gives a gate that ignores only
  the label). Price-time datasets carry no label. Validation status `synthetic_only` (see Known limitations).
- `qt_ingest::nyse::synth`: synthetic Integrated Feed days filled by a model of NYSE parity (or by time on the
  price-time markets) that records each execution's FIFO rank. `replay --rank-oracle` reproduces that ground truth
  exactly on the committed synthetic days (NYSE: 1,013 of 3,678 executions behind the head of their FIFO queue, a
  share set by the generator's parameters, not an estimate for NYSE; NYSE Arca: 0 of 1,982) and on a 2,349,888-line
  synthetic NYSE day (1,070,418 executions). On real data the rank oracle mixes the parity effect with the price-time
  baseline (0.3% to 4.6% of Nasdaq executions are behind the head), so the guide compares NYSE with a same-day
  price-time control. Property tests: for any generated day of any of the five markets, the FIFO rank of every
  execution recomputed from the reader's records equals the generator's truth; any mix of valid, truncated and
  garbage lines, failovers included, maps to a self-consistent record stream. The HTML report labels a dataset read
  from NYSE files "Vendor data; venue rules validated on synthetic data only".
- NYSE INFO counters `nyse_parity_executions`, `nyse_modify_keep_priority`, `nyse_modify_lose_priority`,
  `nyse_modify_noop`, `nyse_replace_orders`, `nyse_replace_reduce_same_price`, `nyse_nonprintable_executions`,
  `nyse_nonprintable_hidden_trades`, `nyse_zero_volume_crosses`, `nyse_attributed_adds`, `nyse_trade_cancels`,
  `nyse_cross_corrections`, `nyse_failover_books_kept`; schema 1.2 enum values `nyse_ibf` (`qt.ingest_spec`) and
  `nyse` (manifest `rule_set`, and the `rule_set` enum of `service/openapi.json`).
- **Databento DBN**: definition records inside mixed-schema files (saved live sessions) now supply the raw symbol,
  the tick and the matching algorithm, as `definition` files of DBN v1, v2 and v3 already did; live symbol-mapping
  records name instruments too. `qt_ingest::dbn::DbnDefinition`, `DbnReader::next_definition_record` and
  `DbnReader::scan_symbology` expose every field the ingest uses. Instrument rows of a DBN ingest take the contract
  multiplier (`min_price_increment_amount / min_price_increment`: ES 50, ZN 1000) and the currency from the
  definitions, so currency markouts on CME data are in money per contract; without a definition they stay 1 and
  USD.
- Vintage checks for Databento's 2026-10-31 XNAS.ITCH re-issue: inputs that disagree on one UTC date (a symbol with
  two instrument ids, an id with two symbols) and XNAS.ITCH definitions before 2023-11-28 of both vintages (on any
  dates) are `QT-INPUT-002`, naming the files, the symbol, the date and the `raw_instrument_id`. When the MBO
  metadata maps no raw symbol (`ALL_SYMBOLS`, parent or `instrument_id` requests), the pairing cannot be checked:
  the ingest warns and counts `dbn_xnas_vintage_unverified`. Inputs from two Databento datasets are `QT-INPUT-002`.
  A definition received before 01:00 UTC may carry the previous date's id (the last after-hours hour). Both vintages
  of a day ingest to the same symbols and ticks. Guide:
  [Ingest Databento MBO for CME and Nasdaq](docs/how-to/databento-mbo.md) (free cost estimate with the `databento`
  client, the Nasdaq request window that keeps one Eastern session whole, every DBN warning, error and counter, CME
  notes).
- DBN counters: `dbn_definition_repeats`, `dbn_definition_deletes`, `dbn_definitions_without_tick`,
  `dbn_symbol_mapping_records`, `dbn_alias_mapping_intervals` and `dbn_mixed_schema_other_records` (INFO, never part
  of the verdict); `dbn_definitions_undated`, `dbn_xnas_vintage_unverified` and `dbn_gateway_error_records`
  (`ErrorMsg` and slow-reader `SystemMsg` records; verdict `warnings`).
- **Freqtrade and Hummingbot reconciliation** (`queuetruth.live.importers.freqtrade`,
  `queuetruth.live.importers.hummingbot`; Python API, no CLI command yet): importers for Freqtrade backtest results
  (zip, JSON, `.last_result.json`), trades databases (dry-run and live) and bot logs (restoring the entry orders of
  trades Freqtrade deletes from its database), and for Hummingbot bot databases (`Order`, `OrderStatus`,
  `TradeFill`; `SqliteDecimal(6)` read exactly) with their trades CSVs. `live.import_sim("freqtrade" |
  "hummingbot", ..., pair_with=[live inputs])` writes the simulator log with the paired live `order_key`s
  (Freqtrade: by trade, candle and role; Hummingbot: by symbol, side, price and creation time; both O(n log n)).
  `reconcile_freqtrade(sim, live, out)` (a backtest or a dry-run bot against a live or dry-run bot) and
  `reconcile_hummingbot(sim, live, out)` write both logs, run `score` (external arm) and explain every order of
  either run (`reconcile.json` as the registered document `qt.bot_reconcile`, `reconcile_orders.csv`: missed fill,
  extra live fill, partial fill, price, timing, both unfilled, stop-loss not triggered, no twin on either side),
  with a SPEC 7.3 `run_id` over the inputs' content and every option. How-to:
  [Reconcile Freqtrade and Hummingbot runs](docs/how-to/reconcile-freqtrade-hummingbot.md).
- `candle_fill_audit`: Freqtrade's backtest fill rule (placement clamp, then `low <= price <= high` per candle, full
  fill) replayed on the shadow orders of an exact replay, with a placement lag, queue overfill, fills credited from
  prints outside the order's life, FIFO-only fills by reason, and Wilson and cluster-bootstrap intervals.
- **Own-account live-fill kit** (`tools/livefill/`, `python -m livefill`, in the repository, not the wheel; guide
  [Score my own live fills](docs/how-to/own-account-live-fills.md)). For Bitstamp: a capture daemon that records the
  public `live_orders`, `live_trades` and `detail_order_book` channels plus full REST books to Parquet, with gap
  detection per market and connection (`event_id` / `pre_event_id` chain breaks, chain restarts, reconnects,
  stalls), stale REST books kept out of later segments, and per-segment export to
  `queuetruth ingest --format bitstamp`; `check`, which replays each segment one stream at a time, explains every
  later venue snapshot and flags deletes of orders whose create was lost; an order harness for randomized
  maker-or-cancel orders that is a dry run by default, refuses live orders without `--live`, the confirmation string,
  API keys in the environment and explicit notional caps, and fails closed (every unsettled answer is an unknown
  outcome, and on exit it cancels every open order carrying the run's client order id prefix); a matcher that finds
  each own order in the public feed by venue order id (or by signature) with its exact queue position, fills, fees
  and consistency checks; `stand-in`, which rehearses everything on other traders' orders; and `pipeline`, which
  writes the canonical live logs, runs `queuetruth score` with the exact arm and zoo models over one or more
  captures, and pools every segment into one 2x2 table, exact McNemar test and segment-clustered fill-rate bias per
  arm. The guide also describes the IBKR route (limit orders on IEX matched against DEEP+), without trading code.
- **hftbacktest integration** (`integrations/hftbacktest/`, in the repository, not the wheel; hftbacktest runs in
  its own venv). `export_config.py` turns power-family calibrations into a `qtint.hbt_queue_config` file: fitted
  `n` at full precision with its CIs, per-instrument estimates, held-out scores, trust verdict and trust flags
  (`heterogeneous_strata` and the other non-refusing flags as notes), optional `n = 2` defaults and the
  parameter-free `log` / `log2` models with their train quasi log likelihood; calibrations on different bases are
  refused, refused fits need `--force`. `hbt_config.apply` applies an entry to a `BacktestAsset`. `reconcile.py`
  runs hftbacktest itself (`hbt_driver.py`, a numba loop identical to the O-ZOO-3 per-row driver on 2,988 of 2,988
  synthetic runs with the released and the patched build) on the same orders and L2 view as the zoo, and compares
  fills order by order with queuetruth's faithful models and with the exact replay (first fill, full fill, per day,
  first-fill time differences, `per_order.parquet`). Its cache is keyed by a manifest of the inputs and of each
  hftbacktest build's version and native-module hash. For hftbacktest #312: a minimal reproduction, a proposed
  upstream patch with 7 Rust and 5 Python tests, its relation to the open pull request #323 (whose `>=` change misses
  the report's own lot 0.01 / qty 0.1 residual), and `upstream/verify.sh` to rebuild the evidence
  (`integrations/hftbacktest/UPSTREAM.md`). Not submitted.
- **`queuetruth-nautilus`** (`integrations/nautilus`, a separate package with its own `pyproject.toml`, Apache-2.0,
  depending on `nautilus_trader>=1.231.0,<3`; not on PyPI): turns a `qt.calibration_result` or the
  `export nautilus` text into NautilusTrader objects for the installed release line: `fill_model` (`FillModel` in
  1.x, `DefaultFillModel` in 2.x) with the fitted `prob_fill_on_limit`, `latency_model` (`LatencyModel` /
  `StaticLatencyModel`), and `venue_options` / `venue_config` with `book_type=L2_MBP`, `trade_execution=True` and
  `queue_position` from the fitted family. Refused calibrations raise `QT-CALIB-001` unless forced; caveats
  (corrected or unrecorded fit mode, a queue-gated fit on 2.x, `p < 1` without a seed, unfitted latency components,
  an unverified version) are `CalibrationWarning`s, once per call. CLI `queuetruth-nautilus inspect|snippet|env`
  with `--format json` (one `qt.nautilus_inspect` or `qt.error` document on stdout, schema in the package). Tested
  on nautilus_trader 1.231.0, 2.0.0rc4, rc5 and rc7, including the README example, the rendered snippet and a
  `BacktestNode` run of `venue_config` in real backtests, and a synthetic `BacktestEngine` check of the per-touch
  contract. `integrations/nautilus/repro/heldout.py` is the held-out check inside NautilusTrader (results above);
  `integrations/nautilus/UPSTREAM.md` is a draft PR description for contributing the calibration guidance and a
  per-touch regression test to NautilusTrader (not opened).
- **`queuetruth.frames`**: run outputs as pandas or polars DataFrames, whichever is installed (pandas first;
  `backend=` or env `QT_FRAMES_BACKEND` to choose). `load_run(dir, verify=True)` opens any run directory as a mapping
  of lazily read, cached tables plus its summary and run id, and checks every file the summary (or a nested run
  summary) lists against the recorded sha256, size and row count (`QT-INPUT-002` on a mismatch; names outside the
  run directory are refused). `leaderboard` (with the run's scoring population, `n_leaderboard` and
  `n_excluded_hbt_312`), `held_out` and `scorecards` flatten the zoo, calibration and scorecard summaries into tidy
  tables with fixed columns; `records` turns any list field of a summary into a frame; `read` and `to_frame` convert
  single tables, with column and row filters pushed down to the Parquet reader. Integers are never rounded: nullable
  integer and boolean dtypes and `ArrowDtype` nested columns on pandas, every Arrow type on polars. No new
  dependency: neither library installed is `QT-USAGE-001`.
- Four executed example notebooks in `examples/notebooks/`, each with an "Open in Colab" badge and a setup cell that
  installs queuetruth from PyPI: a quickstart on synthetic data; "Is my NautilusTrader backtest too optimistic?"
  (stored outputs on the two public Nasdaq ITCH sample days, aggregates only: the default `FillModel` overfills by
  +3.13 pp at 1 s on 2019-12-30 and +2.80 pp on 2020-01-30, as in `docs/RESULTS.md`, and a fitted
  `prob_fill_on_limit = 0.287` beats the default on the held-out day); calibrate and export; and scoring three
  simulators against a synthetic live log. They are built from percent-format sources by
  `examples/notebooks/build.py`, which executes them with nbclient and fails on a cell error, a local path in any
  output or a notebook above 200 KB. How-to: [Use queuetruth from a notebook](docs/how-to/notebooks.md). The stored
  outputs were made with 0.1.1 and are re-executed at the next release.
- `python -m queuetruth.model.migrate DIR --allow-pickle --sha256 <digest>` (or `--trusted`, `--out OUT`,
  `--format json`) converts a v0.1 model directory to `model.npz`; nothing is unpickled without `--allow-pickle`,
  and with `--out` every copied file is checked before anything is written.
- `queuetruth.model`: `model_sha256(card)` (the digest of the listed model file; `pickle_sha256` stays as its v0.1
  name), `TreeEnsemble` (prediction form of the GBDT, equal to scikit-learn's `predict_proba`),
  `LegacyPickleWarning`; `HazardModel.fit(..., threads=)`, `fit_threads_used`, `fitted_with`, `state`,
  `sklearn_estimator`, and the in-process helpers `split_arrays` and `predict_split`.
- **Halt censoring in the engine** (`qt_replay::TradingHalt`, `Engine::set_trading_halts`,
  `ReplayOptions::trading_halts`; Rust API only, see Fixed and Known limitations).
- `queuetruth --help` lists the Python-implemented commands (`demo`, `forecast`, `ledger`, `pm`, `certify`,
  `verify-cert`, `commit`, `reveal`, `cert`) with every flag, generated from their argparse parsers
  (`crates/cli/gen_python_commands.py`, checked by a test), so shell completion and the CLI reference include them;
  `queuetruth help G [SUB]` prints the group's own help. `queuetruth mcp serve --allow-run` is the flag form of
  `QT_MCP_ALLOW_RUN=1`.
- New registered documents: `qt.demo_summary`, `qt.demo_plan`, `qt.demo_sources`, `qt.conformal_report`,
  `qt.bot_reconcile`, the six `qt.ledger_*` documents, `qt.selection_inputs`, the eight `qt.sandbox_*` documents and
  the `qt.sandbox_spec` spec, with the certificate, forecast, toxicity, impact and shortfall documents above. New
  registered tables: `qt.zoo_toxicity_fills`, `qt.zoo_toxicity_markouts`, `qt.zoo_toxicity_shadows`,
  `qt.conformal_sets`, `qt.conformal_calibrator`, `qt.ledger`, `qt.score_shortfall` and `qt.shadow_impact`. The
  conformal and ledger tables carry `qt.schema_version = "1"`, which the registry records per table. The `qt.pm_*`
  documents (in `queuetruth.pm.schemas`) and the `qt.forecast_*` tables stay outside the registry.
- **Fuzzing.** `fuzz/`: cargo-fuzz targets for the parsers of untrusted input: ITCH 5.0 (framing, parsing and
  mapping, plus a parser-only target), IEX DEEP+ pcap / pcapng, raw and gzip, through IEX-TP sequencing and the
  mapper (plus a parser-only target), DBN plain and zstd through `DbnReader` and the dated symbology,
  `qt_ingest::ingest` end to end for ITCH, IEX and DBN, the etcsv and Parquet ingest formats with the dataset reader,
  the seven crypto converters, the exact-decimal JSON reader, and `qt_crypto::prepare` with `qt_crypto::write`.
  Oracles beyond "no panic": input bytes only cause input, config or data class errors (never `QT-ENGINE-001`), ITCH
  and DEEP+ messages round-trip through `encode`, mapped records carry `LAST` and the message sequence, DEEP+
  delivers each `(channel, session)` in strictly increasing sequence order, written datasets read back, and a
  timestamp's session day brackets it. `fuzz/run.sh` runs them at low priority; `cargo test` in `fuzz/` (`make
  fuzz-rust`, part of the local `make gate-3`) replays every seed, regression and known crash on stable.
- Property-based fuzz suite for the Python parsers that untrusted uploads reach (`python/tests/fuzz/`, Hypothesis):
  the FIX wire framing and field parser, FIX UTCTimestamps, QuickFIX data dictionaries, FileLog / FileStore /
  sequence-number / event-log readers, the `fix44` importer, the Binance, Bybit, OKX, Coinbase, Kraken, Deribit and
  Hyperliquid importers, the JSON-lines reader, IBKR Flex (XML and CSV), the instrument-map CSV, the hftbacktest and
  NautilusTrader importers (CSV, JSON lines, Parquet) and the `dq` dataset loader and checks (including
  cross-source parity). Contract per example: return, or raise `QtError` with a `QT-*` code; never another
  exception, never more than 20 s, never more than 256 MiB of traced allocations (a 1.5 GiB resident-memory backstop
  catches C-level allocations). `QT_FUZZ_EXAMPLES` sets the budget (default 40 per property, derandomized),
  `QT_FUZZ_PROFILE=deep` draws fresh random examples, `QT_FUZZ_STATS=<path>` writes per-target counts, outcomes,
  times and memory peaks; `make fuzz-py` runs a deep campaign. Every test carries the `fuzz` marker, and every bug
  below has a regression test (`python/tests/fuzz/test_regressions.py`).
- **W1 scale pipeline, per-symbol exactness.** A unit whose replay checks flag anything replays each symbol alone and
  records the flagged symbols in its leaf (`exactness_by_symbol`; the per-symbol counters must add up to the unit's;
  a one-symbol unit reuses its own counts) (`scripts/scale/symbol_exactness.py`). `itch_split.py` records every
  symbol's spans out of state T from the day's `H` messages (`trading_state`) and Stock Directory test issues
  (Authenticity `T`, which `plan.py` excludes); `plan.py` carries the spans in the universe, and
  `atlas.py reduce-day` labels every symbol-day `replay_ok` (no replay-check flag), `trading_ok` (no halt, pause or
  quotation-only span in the session) and `exact_ok` (both); `--trading-state` supplies a split manifest for older
  plans. `atlas.py combine --flag-policy label|exclude`: `label` marks the symbol-days that are not `exact_ok` in
  `per_symbol.parquet` and gives every `leaderboard_by_tier` and `maps_by_tier` row its flagged share of shadows;
  `exclude` drops them, and refuses days that cannot say which symbol-days are flagged unless
  `--allow-legacy-days`. `local_day.py` and `modal_app.py` combine with `exclude` by default.
- `scripts/scale/trace_flags.py` and `itch_ref.py`: trace a day's flagged units to symbols, ITCH message positions
  and causes (test issue, IPO before its first trade, halt or LULD pause, cross execution, or unexplained), check
  the engine against an independent Python book rebuild per symbol, count the flagged symbols' shadows submitted or
  filled while not trading, print the re-stream command for the flagged buckets (`--restream-command`), and write a
  `symbol_flags.json` sidecar that attributes shards written earlier.
- **Local nightly cross-checks** (`scripts/nightly/nightly.py`, `make nightly` / `make nightly-reduced`; never GitHub
  Actions or a cloud runner): cross-checks the faithful zoo models against the pinned NautilusTrader, the newest
  NautilusTrader 2.x release candidate with a wheel for the host (advisory) and hftbacktest, reruns a public Nasdaq
  ITCH reproduction subset and the quick benchmark, each in its own uv venv (the queuetruth side from a release wheel
  of the checkout), then writes a static HTML results page and a JSON history. Each check is diffed against its own
  last result in the same mode; timings, build identity and paths are reported but never counted. Exit codes 0 ok /
  1 fail / 2 error / 3 changed / 4 advisory only, plus a `flags` list. The benchmark baseline is the last passing
  night (or `--bench-baseline`), and a benchmark on a saturated machine errors instead of being judged. Stopping the
  runner stops the running check's process group and records a partial run. `schedule launchd|cron` prints a plist
  or crontab line and installs nothing. How-to: [Run the nightly cross-checks](docs/how-to/nightly-checks.md).
  `scripts/nightly/hbt_xcheck.py` builds hftbacktest inputs (event file, orders sidecar, the zoo's first fills) from
  any dataset, counting each reason a shadow is dropped; the directory is also a valid `QT_ZOO3_DIR` for
  `python/tests/zoo/test_hbt_crosscheck.py`.
- Bitstamp converter counters: `seed_ambiguous` (a data-quality warning when no snapshot is an unambiguous seed),
  and the INFO counters `id_ties_reranked`, `maker_in_taker_window`, `trades_after_maker_create` and
  `seed_snapshots_skipped`.
- Docs: a [Glossary](docs/concepts/glossary.md) of every project term (shadow, zoo, exact arm, faithful and
  corrected, bounds, hftbacktest #312 and the common population, `run_id`, calibration export, scorer arms,
  self-exclusion and about 70 more); every design note published under
  [Design extensions](docs/design/spec.md#10-design-extensions); a Read the Docs build (`.readthedocs.yaml` installs
  mkdocs-material pinned to `uv.lock`, runs `docs/check_docs.py --no-run` and the chart check, then builds
  `mkdocs.yml` with warnings as errors; no GitHub Actions involved); `docs/assets/readme_chart.py` renders the
  README's 1 s leaderboard chart (`docs/assets/fill-bias-1s.svg`) from the faithful-mode table in `docs/RESULTS.md`
  (stdlib only, deterministic, light and dark themes), and `--check` fails when the table and the chart disagree
  (`make check-docs` runs it).
- Make targets: `test-extra` (the livefill, nightly, scale and hftbacktest test directories, also in pytest's
  `testpaths`), `test-nautilus` (the plugin's own session), `fuzz-py`, `fuzz-rust`, `nightly`, `nightly-reduced`
  and `lint-py`; `make test` runs `test-extra` and `test-nautilus`, and `make fmt` / `make lint` cover `tools/` and
  `integrations/`.

### Changed

- **`queuetruth report` is answer-first.** A new answer block follows the header with one plain sentence per
  question the inputs answer (which L2 model is closest to the exact replay and which models tie with it, whether
  any model overstates passive fills against the exact replay, whether the simulator fills like live, whether a
  calibration can be exported, whether the learned model beats the base rate, the referee result per simulator, the
  data-quality verdict and the inputs it does not cover), each with its base rate and 95% interval where the inputs
  carry them, a status badge and a link to its drill-down. A missing interval is shown as NO CI, never as a pass.
  The zoo leaderboard is its own section right after it (compact table with the exact fill rate row; the full
  table, scores and per-model sentences are collapsed under it). Inputs with sha256, scope and limits, spec echoes,
  file hashes, why traces, per-horizon charts, reliability per model, calibration strata and export code, and extra
  position instruments are collapsed into `<details>` blocks; every section stays reachable from the table of
  contents. `qt.report_summary.sections` gains `answer` and `leaderboard`. The report has a light/dark theme switch
  that needs no JavaScript, prints in the light theme with print-friendly breaks and always prints the inputs with
  sha256 and the scope and limits, keeps the table of contents to one row on narrow screens, and has a skip link,
  labelled regions, row headers and table captions. The quickstart report drops from 52.5 to 6.3 screens of 900 px
  (58 to 6 printed pages); the real 2019-12-30 Nasdaq zoo report from 28.9 to 6.5 screens (37 to 8 pages). Output
  stays byte-identical for identical inputs.
- **`fit-hazard` writes `model.npz` instead of `model.pkl`**: a deterministic ZIP of a JSON header and `.npy` arrays
  (trees and isotonic layers) that loading parses without executing code and checks structurally before use
  (crafted or damaged files are `QT-INPUT-002`). `model.json` lists `model.npz`; predictions and every card value
  are unchanged (byte-identical `predictions.parquet` on the RD-10 real-data fit).
  `HazardModel.load(path, *, sha256=None, trusted=False, allow_pickle=False)`: `model.npz` needs no trust flag (its
  sha256 is still checked against the card, and against `sha256` when given; without `sha256` the model's
  `warnings` say it is not authenticated). A v0.1 `model.pkl` is refused with `QT-CONFIG-001` unless
  `allow_pickle=True`, and then still needs its trusted `sha256` or `trusted=True`; loading it emits
  `LegacyPickleWarning`. A model fitted under another scikit-learn version than the installed one says so in its
  `warnings`.
- `fit-hazard` CPU time on the RD-10 workload (66,460 real order-landmarks, zoo comparison on 2,000,000 rows), one
  thread: 61.8 s to 68.6 s against 77.2 s for v0.1 at `OMP_NUM_THREADS=1` (11% to 20% less), so the SPEC 7.1 target
  of 120 s holds whenever the fit has one core to itself. The fit predicts each row at most once (train rows only
  when `predictions.parquet` is written) and the zoo comparison is vectorised. Results are bit-for-bit unchanged.
  Wall time on a shared, loaded laptop was 53 s to 152 s (median 84 s over 8 runs, 3 over 120 s).
- The GBDT fit no longer starts one OpenMP thread per core whatever the load: it uses `HazardModel.fit(threads=)`,
  else `OMP_NUM_THREADS`, else the allotted CPUs when the process is confined to fewer than the host has, else the
  CPUs not already busy (`os.cpu_count()` minus the 1-minute load average). On a machine at load 180 to 460, v0.1's
  8 threads took 441 s to 1,877 s wall and twice the CPU of its own one-thread run.
- **Migration (learned model).** `HazardModel.estimator` is now a `TreeEnsemble`, not the
  `HistGradientBoostingClassifier`; code that used scikit-learn attributes on it (`n_iter_`, `get_params`,
  `staged_predict_proba`) should use `HazardModel.sklearn_estimator`, which exists only in the process that fitted
  the model (None after `load`). Code that picked the digest by name
  (`next(f.sha256 for f in card.files if f.name == "model.pkl")`) should call `queuetruth.model.model_sha256(card)`,
  as `examples/06_learned_model.py` now does. Convert v0.1 model directories once with
  `python -m queuetruth.model.migrate`.
- **Upgrade note (service).** `qt.job.kind` gains `zoo` and is a closed enum: a `queuetruth` client older than this
  release fails to parse a `GET /v1/jobs` page (`QT-REMOTE-001`) once the tenant has a zoo job. Upgrade clients
  before submitting zoo jobs.
- DBN: pro-rata and allocation books are rejected at ingest as the spec's non-goals require: a DBN partition whose
  definition names a `match_algorithm` other than FIFO is `QT-CONFIG-001` at its first selected record, naming it and
  the other selected non-FIFO instruments; list the FIFO contracts with `--symbols`. A refused DBN ingest removes
  what it wrote to `--out`, so the same `--out` can be used again. `dbn_other_records_skipped` now counts only
  records of another schema inside an `mbo` or `trades` file; such records in a mixed-schema file are counted in the
  INFO counter `dbn_mixed_schema_other_records`.
- `prepare_zoo` takes `planned_life` as its required third positional argument, before `features`.
- `reconcile.json` of the Freqtrade and Hummingbot reconciliation now carries `"schema": "qt.bot_reconcile"` and
  `"schema_version": 1`; `ReconcileOrder`, `ReconcileScore` and `ReconcileReport` live in `queuetruth.schemas` and
  are still importable from their old paths.
- `queuetruth export nautilus` writes `cancel_latency_nanos` and imports the 2.x names
  (`nautilus_trader.execution.DefaultFillModel` / `StaticLatencyModel`) first, falling back to the 1.x
  `nautilus_trader.backtest.models` names; the header says which versions the names were checked on (1.231.0 and
  2.0.0rc7), and the notes say 2.0.0rc7 needs an explicit `fee_model` on every backtest venue. The snippet is now run
  against stand-in 1.x and 2.x modules in the default tests, and against real nautilus_trader interpreters named in
  `QT_NT_PYTHON`.
- The instrument map (`live/imap.py`) reads `price_exp`, `size_exp` and `instrument_id` as plain ASCII integers only
  (other Unicode digits and `1_0` are `QT-INPUT-002`), and `session_date` must be an ASCII `YYYY-MM-DD`; a compact
  date such as `20261015` was accepted before and is now refused. Errors name the file and line.
- Bitstamp ingest results change (see Fixed): datasets ingested from the same capture get a later seed and different
  record counts.
- W1 scale pipeline: a flagged unit's shard leaf gains `exactness_by_symbol`, so its leaf hash, and any day root that
  contains it, differ from roots published before; exact units' leaves are unchanged. `verify_unit.py` also accepts
  a re-run flagged unit in the pre-attribution form. A plan source records `exclusions_sha256` and
  `trading_state_recorded`, and sources enter the run id: re-planning a day gives a new run id (and new unit spec
  hashes), and the same day planned under other exclusions gets another. `cells_all.parquet` and
  `strata_by_tier.parquet` gain the label columns above; `atlas.py combine` treats a day directory written before
  them as legacy (its flagged units' symbols are not exact).
- `scripts/realdata/headline.py` records the scored population and `summarize.py` prints it.
- README front page rewritten for PyPI: tagline, the 1 s leaderboard chart (both Nasdaq days, day-block bootstrap 95%
  CIs), a four-line quickstart and a "bring your data" table of every input format and how far each is checked. The
  home page counts 14 queue-model configurations plus 2 bounds (it said 15; `hbt_power[n=1]` left the default set in
  0.1.1). The root `mkdocs.yml` takes `site_url` from Read the Docs' `READTHEDOCS_CANONICAL_URL` (unset locally),
  links the repository and sets `validation.anchors: warn`, so a link to a missing heading fails
  `mkdocs build --strict`; `docs/assets/*.py` is no longer copied into the site.

### Fixed

- Integration fixes found by the final verification of the merged tree:
  - The zoo's impact warning no longer embeds the run's output path, so `summary.json` is identical wherever a run
    is written (SPEC 7.2; certificate re-runs compare it). The local MCP run mode and the service gave different
    documents for the same run before.
  - The live-fill kit's `ingest_suppressed` flag follows the Bitstamp converter's new rule: an order that a trade
    names as the maker is kept by the ingest (`maker_in_taker_window`) and is no longer flagged, so the example
    pipeline matches all 24 own orders.
  - The trial ledger's parallel-append test runs each writer as its own interpreter, so it passes however pytest is
    invoked (it failed under `make test-py`).
  - The committed OpenAPI document is regenerated for the new leaderboard fields; the no-market-data guard skips the
    ledger's git-ignored runtime directory (`.queuetruth/`).
  - `queuetruth mcp serve` lists the agent-sandbox tools only with `--allow-run`, like the other run tools: they run
    replays, and the default server stays read-only (the release smoke test caught the change of default).

- **Receipt-order priority of released orders on Nasdaq ITCH data** (`exact` rule). Orders Nasdaq holds until the
  open are released 0.27 s to 40 s after it with their original, smaller ITCH references and keep their receipt
  priority. An `A` at a shadow's price whose reference is below the largest reference added on the instrument before
  the shadow went live now joins the shadow's queue ahead instead of queueing behind it; a twin-linked shadow
  (self-exclusion) takes its twin's own reference; `queuetruth why` labels such an add as ahead. Applies when
  `Instrument.venue` is Nasdaq and `Instrument.source` is `itch`, `etcsv` or `dbn` (`dbn` with the `nasdaq` rule set
  is taken to be XNAS.ITCH); other venues, sources and the `et` rule keep arrival order.
- **Hidden prints after a sweep, on the same Nasdaq ITCH data.** A side-`N` print at the timestamp of an execution
  sweep of one resting side (no other change on that side since) is the sweeping aggressor reaching hidden interest
  on that side: it no longer fills shadows on the other side, and when it is off the tick grid (a midpoint
  execution) it still trades through shadows on the swept side. IEX DEEP+ and other sources keep the previous
  inference of the print's side. Results on Nasdaq ITCH data change for shadows live in the first 40 s after the
  open, twin-linked shadows there, and shadows reached by such prints; run ids change with the next version bump.
- The independent oracle (`qt_oracle` 0.1.3) applies the same receipt-order priority. With both fixes the oracle and
  queuetruth agree on 3,000 of 3,000 mirror shadows on both public ITCH days, and on the prototype's CSV conversion
  on 2,997 and 3,000 of 3,000 (the 3: the oracle's 1 s opening-cross window on a CSV without cross flags declines a
  continuous hidden print 29 ms after MSFT's cross; a 0.85 s window agrees on all 3,000); the oracle's fork never
  diverges from the tape. Every former disagreement is classified with evidence in
  `docs/design/extensions/oracle-disagreements.md`; `scripts/oracle/disagreements.py` reproduces it (with input
  hashes and versions in every output).
- **Trading halts censor shadows in the engine** (finding F2 of the atlas trace; SPEC 1.2). Given the spans in which
  an instrument is not in continuous trading, a shadow live when a halt starts ends `censored_gap` at the halt, and a
  shadow whose `ts_live` falls in a halt never goes live and ends `censored_gap` at its `ts_live`; both fill rules,
  twin-linked shadows (A1) included. Without spans every result is unchanged. Rust port of
  `scripts/scale/findings/repro_halt_shadow_fill.py` and a property test against the unhalted replay:
  `crates/replay/tests/halts.rs`. Only Rust callers that supply the spans get the fix yet (see Known limitations).
- **Bitstamp converter** (`qt_crypto::bitstamp`), the three findings of the live-fill kit: within one timestamp,
  new orders at the same side and price are ranked by order id, not by arrival (each order's own messages keep their
  order, and a trade is placed after its maker's `order_created`); an order created and deleted inside the taker
  window is no longer dropped as a taker when a trade names it as the maker; and the book is seeded from the first
  snapshot that no later-arriving message is stamped before (else the first, with `seed_ambiguous` set). Every
  usable snapshot's raw line is kept until the seed is chosen (about 49 MB more on the RD-12 capture).
- `scripts/nt_crosscheck/run.py` and `l3_mbo.py` never cancelled an order at the end of its window on
  nautilus_trader 2.x (the order object stayed `INITIALIZED`, and 2.x `cancel_order` takes a `ClientOrderId`); the
  cancel now reads the order from the cache, and both reports count orders that fill after their cancel, which must
  be 0.
- `queuetruth export nautilus` wrote `delete_latency_nanos`, which neither nautilus_trader 1.231.0 nor 2.0.0rc7
  accepts, and imported `nautilus_trader.backtest.models`, which 2.x does not have (see Changed).
- `shadows sample --allocation per_partition` was accepted by the CLI and then rejected; the CLI value is now
  `per-partition`, like the other hyphenated values (the spec value stays `per_partition`).
- `queuetruth help forecast` and `queuetruth help pm` did not work.
- DBN: parent (`ES.FUT`) and continuous (`ES.c.0`) requests: their metadata is keyed by the requested symbol, which
  queuetruth used as every contract's raw symbol (all children of `ES.FUT` were named `ES.FUT`, no definition
  matched, so the tick defaulted to 1 unit, and `--symbols ESZ4` selected nothing). Such keys are now aliases: they
  select instruments in `--symbols`, and a continuous key names one only when no definition does and it maps to that
  one id on the date. A definition with an undefined `ts_recv` is dated by its `ts_event` instead of never being
  valid on any date. A `raw_instrument_id` of `i32::MAX` (the null in Databento's old-vintage definitions) reads as
  undefined.
- The testkit's reference engine classified INFO source counters with its own three-name list; it now uses
  `qt_ingest::dq::is_info_source_counter`, so an IEX dataset with maintained-priority modifies is no longer a
  `warnings` verdict there.
- IEX DEEP+: three IEX-TP sessions each jumping to a sequence number near `i64::MAX` overflowed the
  `sequence_gap_messages` counter (panic in debug builds, wrap in release); it now saturates.
- Crypto ingest: a timestamp in the last day `i64` nanoseconds can start (2262-04-11) overflowed the session-date and
  midnight arithmetic; it is now `QT-INPUT-002`. A `session_date` / `midnight_utc_ns` whose session day does not fit
  in `i64` nanoseconds is `QT-CONFIG-001` naming the field that was set, instead of an overflow.
- `dmul` rounded products of long decimals at 80 digits; it is now exact.
- RFC 3339 times named impossible dates silently (2026-02-30 became 2026-03-02, offset `+99:99` was accepted,
  non-ASCII digits were read) or raised `ValueError` (month 13, year 0). They are now `QT-INPUT-002`; a leap second
  (second 60) is still accepted. The same holds for IBKR Flex wall times (an offset such as `+99:99` and non-ASCII
  digits are refused), `YYYY-MM-DD` dates, FIX UTCTimestamps (February 30 is no longer a timestamp) and
  `local_ts_regex` times.
- The JSON-lines reader let `ValueError` (an integer past Python's 4300-digit limit) and `RecursionError` (deep
  nesting) escape, also inside a wrapped `msg` string; `recv_ns` / `local_ns` past int64 were accepted.
- A Binance executionReport with neither `T` nor `E` raised `KeyError`.
- Nested fields of the wrong JSON type raised `AttributeError` in the Binance (`params`, `error`), Coinbase
  (`events`, `orders`, `order_configuration`, list items), Deribit (`params`), Hyperliquid (`order`, list items) and
  Kraken (`fees`, `data` items) importers. Non-object list items are skipped and counted; a wrongly typed object
  field is `QT-INPUT-002` naming it.
- FIX integers longer than 4300 digits (BodyLength, data-field lengths, MsgSeqNum, sequence-number files, FileStore
  indexes) raised `ValueError`; FIX integers are now at most 20 ASCII digits. A FileStore index record with a
  negative sequence number, offset or size was taken at face value; it is now an index mismatch. With a data
  dictionary, a 5000-digit unknown tag raised `ValueError` in the unknown-tag report.
- A gzip input with a corrupt deflate stream raised `zlib.error`. An IBKR Flex `<Trade>` nested 100 000 elements deep
  raised `RecursionError`.
- Simulator importers: `size_exp` went through `int()` without a range check (`ValueError`, `TypeError`, `True`
  read as 1), `order_key_map` that is not an object raised `AttributeError`, and invalid UTF-8 in a Parquet column
  name raised `UnicodeDecodeError`. They are now `QT-CONFIG-001` / `QT-INPUT-001`. An hftbacktest `status`,
  `time_in_force` or `order_type` of `--1` or of more than 4300 digits raised `ValueError`; such a value is now read
  as an enum name. A Parquet date or timestamp column outside Python's date range raised `OverflowError`; it is now
  `QT-INPUT-002`.
- CSV inputs (IBKR Flex, simulators, the instrument map) let `csv.Error` escape on a bare CR inside an unquoted
  field or a field over 131 072 characters; it is now `QT-INPUT-002` naming the file and line.
- `queuetruth dq`: manifest fields of the wrong JSON type raised `TypeError`; partition and instrument columns of
  the wrong Arrow type reached numpy (`TypeError`); invalid UTF-8 in a Parquet column name raised
  `UnicodeDecodeError`; `data_quality.json` fields of the wrong type raised `AttributeError`;
  `instruments.parquet` with a session date at the int32 limits raised `OverflowError`. The manifest,
  `instruments.parquet`, `data_quality.json` and partition columns are now validated on load (`QT-INPUT-002` naming
  the field), no looser than the engine: `session_date` must be an integer day number and `price_exp` (manifest and
  instruments) within -12..0. The stale-book check raised `IndexError` on a partition with trade prints and no book
  update; the sub-penny rule raised `OverflowError` at `price_exp` -128 (now refused on load); cross-source parity
  raised `OverflowError` when the price rescale factor left int64 and wrapped around silently when a rescaled price
  did (prices compared wrongly). The rescale is now checked (`QT-INPUT-004`).

### Security

- The learned model no longer uses pickle: `model.npz` is parsed without executing code, and a v0.1 `model.pkl`
  loads only with `allow_pickle=True` plus a trusted sha256 or `trusted=True` (see Changed).
- `queuetruth dq` joined manifest partition paths to the dataset root unchecked, so a crafted manifest could make it
  read any file the process can read (absolute paths, `..`); paths are now checked lexically like the engine's, so
  symlinked partition directories still work. The vintage check still probes the manifest's `inputs[].path` (it
  reveals whether that path exists and its size); run `dq` on untrusted datasets without source probing.
- Resource exhaustion from crafted inputs (found by the Python fuzz suite): decimals with extreme exponents
  (`1e999999999`) made the importers build billion-digit integers and strings, so input decimals are now limited to
  |x| < 1e31 with at most 40 fractional digits (`QT-INPUT-004` otherwise; zero with any exponent is zero); a gzip
  input was expanded whole, and may now expand at most 200x its size plus 64 MiB (`QT-INPUT-002` past it); Parquet
  inputs had no expansion limit (a 4.3 KB file of 300 000 dictionary-encoded rows grew an import by 832 MiB) and now
  decode to at most 200x the file size plus 16 MiB (simulators) or 64 MiB (`dq`), checked from the footer and batch
  by batch, with nested columns refused; the FileStore index and `.seqnums` readers took quadratic time on a long
  digit run (a 40 000-digit `.header` took 13 s); a MsgSeqNum jump (for example to 10**18) stored every missing
  number in a set, and gaps are now kept as ranges.
- The QuickFIX data dictionary's DTD refusal searched the raw bytes, so a UTF-16 document carried entity
  declarations through, and non-ASCII digits were accepted as field numbers. Document type declarations are now
  refused on the parser's own view of the document, in any encoding (IBKR Flex XML too).
- IEX DEEP+: a pcap / pcapng record header announcing a 16 MiB record on a short stream no longer allocates and
  zeroes 16 MiB before reporting the truncation; the read buffer grows with the bytes that arrive.
- `verify-cert` validates every certificate field it uses as a path, so verifying a stranger's certificate never
  deletes or reads outside the certified directories, and `--rerun` runs each certified run in a child process that
  never imports from the data directory.
- MCP run mode confines inputs and outputs to the configured roots (realpath, no symlink out, never `/` or the home
  directory) and runs each job in a fresh `python -I` child with a minimal environment and resource limits.

### Known limitations

- Halts: ingest does not record the trading state yet (the ITCH mapper still drops `H` messages), so `replay`
  through the Python API, the CLI and the service never passes halt spans and shadows can still fill while a
  symbol is halted (finding F2). On 2020-01-30, 1,125 of the 8,000 shadows of ANPC, BDTX, RKDA and DTSS were first
  filled while the symbol was not trading; `repro_halt_shadow_fill.py` still exits 1. Build published atlas tables
  with `atlas.py combine --flag-policy exclude`. Shadows sampled inside a halt, halt-cross executions sent after
  resumption, and crossed-book counts during halts are also not handled yet.
- DBN: the pinned `dbn` 0.71.0 decoder panics on a prelude announcing 100 to 103 metadata bytes (plain or zstd):
  `queuetruth ingest --format dbn` exits 1 with a Python `PanicException` traceback and a service job fails with
  `QT-ENGINE-001` instead of `QT-INPUT-002`. A 24-byte file announcing 256 MiB of metadata makes it allocate 256 MiB
  before reporting the truncation. Reproducers are in `fuzz/known-crashes/dbn/` and `fuzz/known-crashes/dbn-alloc/`.
- Parquet: corrupt metadata in a `parquet` ingest input or a dataset file panics inside the `arrow-ipc` and
  `parquet` crates, with the same CLI and service effect. Reproducers are in `fuzz/known-crashes/parquet/`. The fixes
  belong in `crates/ingest/src/dbn.rs` and `crates/schema/src/io.rs` (see `docs/design/extensions/rust-fuzz.md`).
- NYSE: validation status `synthetic_only`. NYSE's sample files come only under website terms that rule out this use
  without written permission, so no NYSE data was downloaded or replayed; the reader is tested on hand-written and
  synthetic files (`tests/fixtures/nyse/`). The parity label is not yet carried by the per-shadow `dq_flags` of a
  replay or by the zoo, calibration and features outputs.
- Prediction markets: no real Kalshi or Polymarket capture has been run through the kit. Polymarket's trade side,
  its `price_change` size meaning and its matching priority are undocumented and assumed; Polymarket reconnects are
  not detected; `pm score` replays each of your orders as an independent shadow; pm calibrations cannot yet feed
  `queuetruth export`.
- The own-account live-fill kit has no real own-account fills yet, and its Bitstamp v2 order API use (signing,
  `client_order_id` lookups) has not been checked against the real server.
- The agent sandbox has no blind mode (absolute prices and dates identify the day), and each step re-replays the
  episode's prefix, so a full liquid day in 1 s steps takes at least 26 minutes.
- Certificates: cross-platform re-execution was not measured (the 4-ulp path is covered by unit tests and a
  simulated re-execution only); there is no HTTP or MCP certify endpoint, no RFC 3161 client and no key revocation
  or rotation.
- `zoo-toxicity` and conformal sets are not exposed through the HTTP service or the MCP server; zoo conformal has
  no CLI command.
- The nightly runner's full mode has not been run end to end (it exceeds 20 minutes at 2 threads); its diff is
  covered by unit tests. The Linux memory watchdog of MCP run mode and the service has only run its kill path on
  macOS.
- The multi-thread `fit-hazard` on a quiet machine and the SPEC 7.1 bench case `ana.real_day1.fit_hazard_60k` are not
  measured yet; `model.npz` relies on scikit-learn's private tree layout and is tested on 1.9.1 only.

## [0.1.1] - 2026-10-23

A release for first-time users: a stranger should get a readable answer in five minutes. Data schema unchanged
(1.2); the new summary fields are additive with defaults.

### Real-data results

- NautilusTrader cross-check re-run on nautilus_trader 2.0.0rc7 (2026-10-22, same 3,000 orders per day; `run.py`
  now runs on 1.x and 2.x). The default touch model (`DefaultFillModel(prob_fill_on_limit=1)`) fills exactly the same
  orders as 1.231.0 on both days and the synthetic fixture, so the overfill stands: +3.13 pp [+2.53, +3.77] at 1 s on
  2019-12-30 and +2.80 pp [+2.23, +3.37] on 2020-01-30, and faithful `nt_prob_touch` still agrees on 100% of orders.
- 2.0 changes Nautilus's queue model. On a delta feed it also caps queue ahead at a level's new size when the level
  shrinks (present from 2.0.0rc4), so faithful `nt_queue_l2` (1.231.0's rules) agrees with 2.0.0rc7's
  `queue_position=True` on 99.60% to 100% of orders per day and horizon (100% at 0.1 s on day one); every difference
  (18 orders on day one, 10 on day two) is the cap, and Nautilus fills each one earlier. Nautilus's own bias against exact at 1 s / 10 s / 60 s goes
  from -0.10 / -0.77 / -0.70 pp to -0.03 / -0.37 / -0.37 pp on day one and from -0.10 / -0.17 / -0.23 pp to
  -0.03 / -0.03 / -0.07 pp on day two.
- The `OrderBookDepth10` reset is gone from 2.0.0rc5 (#4942): fed depth snapshots, 2.0's queue model fills exactly
  the orders it fills on the delta feed (1.231.0: like the touch model, +2.87 and +2.67 pp at 1 s).
- Fed the MBO book as L3 deltas (`book_type=L3_MBO`), 2.0.0rc7's queue model fills the same orders as the exact
  replay at every horizon on day one; on day two it differs on 1 order at 0.1 s and 2 at 10 s (hidden prints, which
  the harness passes as `NO_AGGRESSOR` trades and the exact replay does not count; bias at most +0.07 pp). 1.231.0's
  L3 queue model overfills: +1.73 pp at 1 s on day one, +1.67 pp on day two.

### Changed

- **One common population for every zoo model.** A shadow on which any faithful hftbacktest model hit its bug #312
  is now left out of every model's metrics (`queuetruth.eval.common_population`), not just the aborted model's.
  The aborts hit the orders that reach a partial fill, so in 0.1.0 the hbt rows were scored on fewer, less
  fill-prone shadows than the others (n = 945 against 994 in the quickstart), against a truth computed on all of
  them. On the synthetic quickstart the hbt models move from about -2.9 pp to -0.8 pp at 60 s; expect the hbt rows of
  a real-data leaderboard to move too, and every row to be computed on fewer shadows. New summary fields
  `n_excluded_hbt_312` and `n_leaderboard`. The calibrate held-out comparison (fitted vs default) uses the same
  rule.
- **`--models all` runs 16 models, not 17.** `hbt_power[n=1]` (`b / (b + f)`) equals `hbt_power3[n=1]`
  (`1 - f / (f + b)`) in every cell and was dropped from the default set; it still runs when named.
- **Unit-less durations are rejected** on every flag whose name does not end in `-ns`: `--lifetime 5` was silently
  5 ns and is now `QT-USAGE-001` ("write 5s, 5ms, 5us or 5ns"). `0` and the `*-ns` flags keep accepting bare
  integers.
- **Quiet terminal output.** The per-model `hbt_312_abort:` warnings collapse into one line with a link to the
  hftbacktest issue; informational lines (`info: `, `ci_block=`, `self_exclusion: matched`, all-zero shadow
  diagnostics) print only with `--verbose` or `--log-level info`. The answer is flushed to stdout before stderr.
  `summary.json` keeps every warning.
- **Real Databento CME data is no longer labelled synthetic.** The report used the manifest's `validation_status`
  (the venue rule set's validation level) as the data's provenance, so a DBN file ingested with `--venue cme` got a
  red "Synthetic or fixture data" banner. Vendor-format data (`dbn`, `itch`, `iex`, `etcsv`) whose rules are
  validated on synthetic data only now gets "Vendor data; venue rules validated on synthetic data only".
- README: the PyPI page no longer says the package is not on PyPI, every link is absolute (relative links 404 on
  PyPI), and PyPI, Python and licence badges were added.
- The NautilusTrader cross-check tests pin each version's rules: on 2.x the depth feed must fill like the delta
  feed, and the `p = 0.29` expectation is checked under each side's own rules.

### Added

- **Base rates next to every bias.** Leaderboard entries carry `exact_fill_rate` and `model_fill_rate` per horizon
  (`fill_rate_bias = model_fill_rate - exact_fill_rate`). The CLI table and the report table start with an "exact
  fill rate" row, and each plain-words sentence reads "fills 56.2% of orders vs 57.0% in the exact replay, 0.8 pp
  less often".
- `queuetruth zoo --list-models` (also `--format json`): the `all` set with one line per model, then the
  parameterised and opt-in families.
- "Did you mean": an unknown model family names the closest one (`hbt_powr3` -> `hbt_power3`), and model ids are
  checked before any data is read.
- `--help` shows each flag's default (from the spec's JSON Schema, e.g. `--lifetime [default: 5m]`,
  `--horizons [default: 100ms,1s,10s,60s ...]`).
- `sample_shadows`: `allocation: per_partition` with `partition_quotas` (`"<instrument_id>"` or
  `"<instrument_id>_<YYYYMMDD>"`): each instrument-day draws `n` (or its quota) shadows whatever else the dataset
  holds, so a dataset split into shards draws exactly the shadows of the whole. An empty `partition_quotas` stays out
  of `run_id`; existing run ids are unchanged.
- `queuetruth.eval.blocks`: summable zoo statistics per model, population, stratum, horizon, instrument-day and
  30-minute block (`block_stats`); `evaluate_blocks` rebuilds `evaluate_zoo`'s ratio metrics and block-bootstrap
  CIs from them across shards; `cluster_bootstrap` (two-way dates x symbols), `shrink` (empirical Bayes) and `bh`.
- `scripts/scale/`: the W1 full-market pipeline (ITCH day split into symbol buckets, planner, work units with shard
  manifests, aggregator with a Merkle-rooted run manifest, single-unit verifier, Modal entry points, and the local
  proof that 72 single-symbol units reproduce the 36-symbol breadth run of docs/RESULTS.md 4b).
- `scripts/nt_crosscheck/l3_mbo.py` and `l3_score.py`: feed the shadows' MBO book to NautilusTrader as L3 deltas and
  score each order's first fill against the exact replay.

### Fixed

- The local store's signed-URL check and the live-log, FIX and CLI parsers guarded `int()` with `str.isdigit()`,
  which accepts characters such as `²` that `int()` rejects; a tampered `exp` on `PUT /v1/_store/{key}` returned
  500 instead of 403 (found by the API fuzz test). Every guard now uses `isdecimal()`, and signed-URL expiries must
  be ASCII digits.

## [0.1.0] - 2026-10-19

First public version. Validated on real data for Nasdaq TotalView-ITCH (two public days, four symbols) and IEX
DEEP+ (one public day, four symbols); everything
else is tested on synthetic data and fixtures, as listed under "Known limitations". Evidence:
[docs/RESULTS.md](docs/RESULTS.md) and [How queuetruth is tested](docs/testing.md). Data schema 1.2
(design: [spec 3.12](docs/design/spec.md)).

### Real-data results

- Exact replay of 16,754,562 records of two public Nasdaq ITCH days (2019-12-30, 2020-01-30; AAPL, MSFT, SPY, TSLA)
  from the raw bytes with queuetruth's own ITCH reader: 0 invariant violations, 0 trade-parity mismatches over 633,287
  prints, 0 price-priority violations; BBO and rank traces, and 3,000 of 3,000 mirror shadows per day (under its
  fill rule), identical to the author's earlier private prototype. An independent matching-engine oracle agrees with
  the default shadow fill rule on 2,992 and 2,985 of 3,000 shadows from the raw ITCH bytes (queuetruth's own mirror
  sampler) and on 2,997 of 3,000 per day of the prototype's mirror shadows on its CSV conversion.
- Public reproduction from the raw Nasdaq files alone (`make reproduce`, `scripts/repro/reproduce.py`): the
  leaderboard and RD-9 from the raw bytes are identical to the same runs on the CSV conversion with the same build,
  and RD-10 to the earlier CSV run. Rerun on commit `4be5240`, `make reproduce` reproduced the leaderboard on both
  days, all four calibrations, the learned model and the oracle identically (1,162 s; `.realdata/repro/results.json`
  sha256 `446c6b09ba71dbd8...`). Against the earlier published leaderboard, every change comes from engine changes: the bounds
  (`trade_through_only` now -6.0 .. -15.8 pp, `front_of_queue` +0.9 .. +3.3 pp), the verified faithful NautilusTrader
  models, and faithful hftbacktest shadows that reach the #312 event now left out per shadow (179 to 342 of 3,000
  per model, 6.0% to 11.4%); the hftbacktest rows moved by at most 0.1 pp.
- NautilusTrader result verified against the real engine: nautilus_trader 1.231.0, `FillModel(prob_fill_on_limit=1)`,
  its `BacktestEngine` fed the L2 view as `OrderBookDelta`s with the same 3,000 orders per day, overfills by
  +3.13 pp [+2.53, +3.77] at 1 s on 2019-12-30 (14.17% against 11.03% exact) and +2.80 pp on 2020-01-30.
  queuetruth's faithful NautilusTrader models (`nt_prob_touch`, `nt_queue_l2`, `nt_queue_l2_prob_touch` at `p = 1`)
  agree with it on 100% of orders at 0.1 s, 1 s, 10 s and 60 s on both days and on the synthetic fixture
  (`scripts/nt_crosscheck`, separate venv, public API only). On an `OrderBookDepth10` feed Nautilus resets
  `queue_position` at every snapshot, so its queue model then fills like the touch model (+0.9 to +3.3 pp above exact
  at 1 s to 60 s); the faithful models describe the delta feed.
- L2 model leaderboard on 3,000 never-cancel shadows per day: the hftbacktest power and log models stay within
  about 1 pp of exact; `hbt_risk_adverse` is pessimistic by up to 1.3 pp; faithful `nt_prob_touch[p=1]` reaches the
  `front_of_queue` bound. The bounds were redefined (`front_of_queue` also fills on hidden prints and on a cross at
  arrival; `trade_through_only` fills only on a displayed trade-through) so that they bracket the exact arm order
  by order, and O-ZOO-6 now tests that shadow by shadow on the scenario corpus, generated CME, Nasdaq and generic
  tapes and `synth_small`. On real day one the per-order violations went from 5 (the earlier run, before the
  redefinition) to 0 (3,000 sampled shadows, both modes).
- Breadth (`make reproduce-breadth`): 36 more Nasdaq symbols in five liquidity tiers on the same two days, from the
  raw bytes, with 0 invariant violations, 0 price-priority violations and 0 trade-parity mismatches in every tier.
  The NautilusTrader default overfills in every tier, most on the deep-queue names (+8.60 pp at 60 s on day one);
  hftbacktest `power3[n=2]` stays within 1.7 pp and `nt_queue_l2` within 1.5 pp at 60 s; order size barely moves
  first-fill bias.
- Calibration fitted on day one and scored on day two (Nasdaq; `p` is venue-specific): NautilusTrader touch
  `p = 0.287` cuts the 1 s overfill from +2.80 pp to +0.41 pp; the queue-gated `p` (0.871 [0.816, 0.919], 613
  events) does not beat `p = 1`; power3 `n` and Databento `k` score about like their defaults, and the export refuses
  the fitted `n`.
- Scorer (RD-11): 3,000 real orders per day used as a live log are reproduced exactly by the exact arm with
  self-exclusion on both days (250 / 2,750 / 0 / 0 and 195 / 2,805 / 0 / 0; identical filled quantity and
  first-fill time on every order). This rests on per-shadow twin linking: an excluded live order is replaced, for
  its own shadow only, by that shadow, which takes the order's exact queue place and is filled by the order's
  executions, while every other shadow still sees the order on the tape. An earlier build, before the root-cause fix
  of the self-exclusion path, disagreed with live on 10 orders on day one (242 / 2,748 / 8 / 2).
- Performance (RD-13 passes): the book-only replay of real day one from Parquet runs at 8.56M to 8.60M records/s
  against its 8M target, with Low Power Mode on. Every SPEC 7.1 throughput target is met with byte-identical outputs
  (synthetic replay 14.5M records/s, with Parquet sinks 6.25M, ITCH ingest 4.56M messages/s, 100,000 shadows 5.28M
  records/s); the zoo runs 17 models x 3,000 shadows in 9.0 s.
- Learned fill model: Brier skill over climatology on the held-out day 0.095 [0.072, 0.122] at 1 s and 0.115
  [0.102, 0.128] at 10 s.

### Added

- **Ingest** into one partitioned Parquet dataset format: Nasdaq TotalView-ITCH 5.0, Databento DBN MBO (versions 1
  to 3), the earlier prototype's CSV layout (`etcsv`), our own normalized Parquet, crypto L3 captures (Bitstamp, Bitfinex, Coinbase)
  and Tardis L2 (L2 only: simulation, markouts and live calibration, never exact truth). Every dataset carries a
  validation label.
- **Exact replay**: per-instrument FIFO book with invariant counters, a queue-rank oracle with sweep grouping,
  derived BBO, depth and trades, a data-quality report and parity checks against the source's own prints.
- **Shadow orders** with the fill rules of the independent specification audit (queue ahead from the records of the
  orders ahead, hidden prints behind displayed interest, taker fills on crossed books, CME same-size modifies losing
  priority); per-fill timeline, queue trajectory, latency, cancels and amends; `--fill-rule et` for parity with the
  earlier prototype; `--exclude-order-ids` to remove real orders from the tape.
- **`why`**: one order event by event with a one-sentence conclusion; `--model` / `--zoo` add a queue model's
  estimate and the first event where it diverges from the exact queue.
- **Queue-model zoo**: 17 rows by default, 15 model configurations plus 2 bounds (hftbacktest RiskAdverse,
  Power/Power2/Power3/Log/Log2 and L3FIFO, ported from its source; NautilusTrader queue and touch models and
  Databento's Monte Carlo estimator, re-implemented from their documentation; front-of-queue and
  trade-through-only bounds) in `faithful` and `corrected` mode, scored with outcome-independent censoring and
  cluster-robust discordance tests by horizon, queue decile and liquidity; `--amends`, `--exclude-order-ids`;
  answer-first leaderboard in the terminal.
- **Calibration** of `n`, `k` and `p` against exact truth, and `calibrate-live` against your own fills on L2-only or
  MBO tapes, each with a trust verdict (grid edge, identification, beats the default out of sample).
- **Fail-safe export** of hftbacktest and NautilusTrader snippets: a refused calibration exits 1 with
  `QT-CALIB-001`; `--force` writes it under a NOT VALIDATED header. `export replay-schedule` writes a program that
  replays a live order schedule in hftbacktest or NautilusTrader; `export latency-npz` and `export l2-npz`.
- **Learned fill model**: a discrete-time competing-risk hazard model (`features`, `fit-hazard`);
  `HazardModel.load` unpickles only with a trusted sha256 or an explicit `trusted=True`.
- **Sim-vs-live scorer**: canonical live order log; importers for Binance, Bybit, OKX, Coinbase, Kraken, Deribit,
  Hyperliquid, IBKR Flex, FIX drop copies and QuickFIX engine logs (C++, J, n, Go; FIX 4.2, 4.4, 5.0SP2), and for
  hftbacktest and NautilusTrader output (`live import --venue sim-hftbacktest|sim-nautilus`); own-order exclusion on
  MBO and L2 tapes; instruction-sized legs; one evaluation window for both arms; cluster-robust primary test; time to
  fill; markouts; `--fail-*` CI gates; answer-first terminal output.
- **Referee**: exact-FIFO regression oracle for other simulators with 16 synthetic bug-class fixtures.
- **Interfaces**: Python API, CLI, self-contained HTML reports (`--sort-horizon`), JSON summaries validated by
  published JSON Schemas (schema 1.1), an MCP stdio server (extra `[mcp]`) and an HTTP client (extra `[client]`).
- **Quickstart data**: `queuetruth fixtures synth --out DIR` writes the synthetic dataset used by the docs, so a pip
  install needs no repository checkout.
- **Self-hostable service** (`queuetruth-service`, FastAPI + PostgreSQL + S3-compatible store) with separate
  unprivileged database roles for the api and the worker, tenant binding by API key or held job, per-tenant job and
  compute limits, zero-egress mode and a Docker Compose stack. It is not published to PyPI in 0.1.0; run it from the
  repository or the compose stack in `deploy/`. Per-kind job timeouts (`QT_JOB_TIMEOUTS_S`, falling back to
  `QT_JOB_TIMEOUT_S`); tombstone ids use the `del_` prefix (migration 0006 renames older `tomb_` ids); errors a
  handler raises (any 4xx problem, and 5xx) are not stored under an idempotency key, so a retry with the same key
  works once the cause is fixed.

- `queuetruth dq` (and `queuetruth.api.dq`, `queuetruth.dq.run`): a data-quality battery over any ingested MBO
  dataset, with an optional second dataset of the same sessions for cross-source parity. Fourteen checks (sequence,
  crossed and stale books, orphan references, sizes and prices, trade parity, timestamps, session coverage,
  symbology, snapshots, vintage, cross-source parity, rate drift, plus the ingest and replay counters), each `pass`,
  `warn`, `fail` or `skip` with its numbers and the first offending rows. Writes `dq_report.json` (`qt.dq_report`)
  and a self-contained `dq_report.html`; exits 1 when a check reaches `--fail-on` (default `warn`). Guide:
  [Check a feed before you trust it](docs/how-to/check-a-feed.md).
- Order-level crypto sources for `queuetruth ingest`: `--format coinbase-level3` (Coinbase Exchange WebSocket
  `level3` plus REST level-3 snapshots), `kraken-l3` (Kraken WebSocket v2 `level3`; exact inside the subscribed depth
  window only, CRC32-checked after every message) and `hyperliquid-node` (a non-validator node's raw book diffs,
  fills and order statuses plus an L4 snapshot). New `--option` keys: `depth`, `checksum_precision` (Kraken),
  `snapshot_block`, `max_block_gap_ms` (Hyperliquid). Each source's exactness caveats are written as ingest
  warnings. Guide: [Ingest order-level crypto feeds](docs/how-to/crypto-l3-sources.md).
- IEX DEEP+: `--format iex-deep-plus` reads IEX HIST DEEP+ pcap / pcapng files (gzip or raw) order by order under
  the new `iex` rule set: IEX-TP sequencing and A/B de-duplication per channel, and the DEEP+ Order Modify priority
  bit mapped exactly (a maintained-priority size decrease keeps the queue position, a reset goes to the back). IEX
  HIST data may be redistributed with IEX's attribution line, so this is the public demo source
  ([Replay a public IEX DEEP+ day](docs/how-to/iex-public-demo.md), `scripts/iex/stream_subset.py`). Real-data run
  on 2026-10-09 (SPY, AAPL, NVDA, TSLA; 4,435,281 records): verdict `clean`, every replay invariant 0, and all
  21,301 displayed executions at FIFO rank 0 of their price level; the `iex` rule set is labelled `real_data`.
- Schema 1.2 documents `qt.dq_report` and `qt.dq_spec` are registered with the other JSON Schemas
  (`queuetruth schema list`); the HTTP API's ingest format lists (`/v1/meta`, dataset creation) include the four new
  formats.
- Crypto L3 datasets record their resolved rule set in the manifest (`rule_set: crypto_l3` for Bitstamp, Bitfinex,
  Coinbase and the order-level sources when ingested with `--venue generic`), so side-`N` prints consume displayed
  queue; a manifest without `rule_set` replays as `generic`.

### Security

- Dropped the `zip` crate (RUSTSEC-2026-0367, -0368, -0369): hftbacktest `.npz` files are written by a small
  in-crate zip writer (DEFLATE, matching np.savez_compressed; CRC-32 known-answer and numpy round-trip tested).
  queuetruth never extracted archives, so it was not exposed; `cargo audit` is clean.
- The service was hardened after a security review; every finding was reproduced, fixed and kept as a regression test
  (`service/tests/security/`): tenant isolation that never fails open (the api and the worker refuse to start
  unless connected as their own unprivileged role, `FORCE ROW LEVEL SECURITY` plus explicit tenant filters, signed
  tenant bindings that need an API key's HMAC or a held job attempt and cannot be replayed); job children with a
  minimal environment and no service secrets; upload sizes enforced by the store and archive extraction bounded
  inside the job child; per-tenant rate limits, key caps, bounded compute knobs and a cap on each tenant's running
  jobs; `QT_ENV=prod` refusing placeholder, short or low-variety secrets and `QT_FORWARDED_ALLOW_IPS=*`; the compose
  stack publishing the api and the store on 127.0.0.1 by default; hash-pinned image dependencies, digest-pinned base
  images, and `cargo-audit` and `pip-audit` in CI.

### Packaging

- One abi3 wheel per platform (`cp311-abi3`, CPython 3.11 and later) built with maturin, plus an sdist. Wheels carry
  `LICENSE`, `NOTICE`, the hftbacktest MIT licence and the licences of every bundled Rust crate.
- Published platforms: macOS arm64 and x86_64, Linux x86_64 and aarch64 (manylinux). Windows and musllinux wheels
  are built by the release workflow as experiments only and are not published. Elsewhere, pip builds from the sdist
  (Rust toolchain required).
- Standalone `queuetruth-core` binaries (native commands only, no Python) are attached to the GitHub release, not
  shipped in the wheel.
- PyPI uploads use Trusted Publishing with PEP 740 attestations; every artifact also gets a GitHub build-provenance
  attestation.

### Project

- Contributor guide (`CONTRIBUTING.md`, DCO sign-off), security policy (`SECURITY.md`), code of conduct, issue and
  pull request templates. The design specification and a summary of how the project was built and verified are in
  `docs/design/`.

### Known limitations

- The Databento model is re-implemented from its public tutorial and has not been checked against Databento's own
  code. The NautilusTrader models are checked against nautilus_trader 1.231.0 on a delta-fed L2 book only; on an
  `OrderBookDepth10` feed Nautilus's queue model behaves like its touch model. The `export nautilus` snippet and the
  `export replay-schedule --target nautilus` strategy are not run against NautilusTrader in the tests.
- Real-data validation covers two Nasdaq days: four very liquid symbols in full, plus 36 more symbols in five
  liquidity tiers (first-fill bias only, no fill quantity). Other venues and other regimes are not shown.
- DBN/CME semantics are synthetic only (Databento's DBN test files decode, but no real CME session was replayed).
- The Bitstamp and Bitfinex readers are checked on one 10-minute capture per venue (counters, not fill fidelity);
  the Coinbase `level3`, Kraken L3 and Hyperliquid node readers on synthetic fixtures only.
- Live-log and QuickFIX importers are tested on fixtures written from public format documentation and the engines'
  sources, not on real account exports.
- Performance is measured on one Apple silicon laptop with Low Power Mode on (every SPEC 7.1 throughput target met;
  real day one replays at 8.56M records/s). Other hardware is unmeasured, and `fit-hazard` (172 s against 120 s) and
  the Python binding's overhead over the CLI (18% against 5%) still miss their targets.
- Exact truth covers displayed liquidity on FIFO venues only; hidden liquidity, auctions, pro-rata books and
  own-order queue interaction are out of scope.
