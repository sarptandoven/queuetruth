# Extension: `queuetruth-nautilus` (calibrated NautilusTrader fill model)

Proposed spec text for a new subsection after 5.10 ("Export templates"). It describes a separate package under
`integrations/nautilus/` with its own `pyproject.toml`, released independently of `queuetruth` (it does not import
`queuetruth` at run time, and the queuetruth package does not depend on it).

## 5.10.1 NautilusTrader plugin (`integrations/nautilus`, package `queuetruth-nautilus`)

**Purpose.** Turn a `qt.calibration_result` (or the text `export nautilus` writes) into the objects NautilusTrader's
`BacktestEngine.add_venue` and `BacktestVenueConfig` take, for the installed release line, with the trust rules of
5.10 enforced at construction time.

**Inputs.** `load_calibration(path)`: a `qt.calibration_result` JSON file (`schema_version` 1, schema 1.1 fields
optional), a directory holding `calibration.json` or `fit.json`, or an `export nautilus` text (first line
`# queuetruth <version> | calibration <run_id> | NautilusTrader ...`). Errors: `QT-INPUT-001` missing or unreadable,
`QT-INPUT-002` anything else, including an NT-family estimate outside [0, 1], a `ci_bootstrap` that is not null or
two numbers-or-nulls, an `n_events` or `n_shadows` that is not a non-negative integer or null, a malformed `train` or
`test` window, and an export text whose `base_latency_nanos` is not 0.

**Mapping (normative).**

| calibration | NautilusTrader |
|---|---|
| `family_key(family)` in (`nt_prob_touch`, `nt_queue_l2_prob_touch`), finite `estimate` | `prob_fill_on_limit = estimate` (full precision from JSON; 6 significant digits from export text) |
| family `nt_queue_l2_prob_touch` | `queue_position = True`; otherwise `False` |
| any NT-family fill probability | `book_type = L2_MBP`, `trade_execution = True` |
| `latency.entry_ns.p50`, `amend_ns.p50`, `cancel_ns.p50` with `n > 0` | `insert_latency_nanos`, `update_latency_nanos`, `cancel_latency_nanos` (rounded, clamped at 0); `base_latency_nanos = 0`; a component without samples is 0 and named in a `CalibrationWarning` |
| `prob_slippage`, `random_seed` | never fitted: caller arguments (`prob_slippage` in [0, 1], `random_seed` in [0, 2^64 - 1], else `QT-CONFIG-001`) |

Class names per release line: 1.x `nautilus_trader.backtest.models.FillModel` / `LatencyModel`, 2.x
`nautilus_trader.execution.DefaultFillModel` / `StaticLatencyModel`; both take `cancel_latency_nanos`. In 1.x
`BacktestVenueConfig` receives `ImportableFillModelConfig` / `ImportableLatencyModelConfig` (paths to the built-in
classes and their `FillModelConfig` / `LatencyModelConfig`); in 2.x it receives the objects.

**Trust (normative).** The verdict is read from the structured `trust` field, else from the last `trust: verdict:`
warning line, else from `queuetruth.export.trust.assess` when queuetruth is importable, else it is `refused` with
reason `no_trust_verdict`. A structured `trust` object whose `verdict` is not `exportable` or `refused`, or whose
`reasons`, `flags` or `beats_default` are malformed, is `QT-INPUT-002` (it never falls back to the warning line).
An export text is `refused` when it carries the `# !! NOT VALIDATED` block (reasons from its `# !!   - <token>:
<text>` lines) and `exportable` otherwise; its `mode` is read from the `[mode=...]` of the baseline it names, else
`unknown`. Every constructor (`fill_model`, `latency_model`, `venue_options`, `venue_config`, `render_snippet`) raises
`CalibrationRefused` (`QT-CALIB-001`, exit 1) for a refused verdict unless `force=True`, which emits a
`CalibrationWarning`. Further `CalibrationWarning`s: a `mode` other than `faithful` (or `unknown`); a queue-gated fit
used on 2.x (fitted under 1.231.0 queue rules, 5.3.3); `0 < p < 1` without a `random_seed`; a latency component
without samples; an installed version outside the cross-checked lines 1.231.x and 2.0.x (`render_snippet` checks the
requested `nt_major` instead of the installed version). Each public call checks once and emits each warning once,
attributed to the caller. A version older than 1.231.0 or a missing nautilus_trader is `QT-USAGE-001`. On 2.x,
`venue_config` requires a `fee_model` keyword (`QT-CONFIG-001` without it), because from 2.0.0rc7 a backtest venue
refuses to start without one (rc4 and rc5 accept it; requiring it on every 2.x release keeps code portable).

**CLI.** `queuetruth-nautilus inspect CAL [--format text|json] [--force]`, `snippet CAL [--nt-major 1|2] [--seed N]
[--force]`, `env [--format text|json]`; `--json` is an alias of `--format json`. Exit codes as `queuetruth`: 0, 1 for
`QT-CALIB-001`, 2 otherwise (an unexpected exception is `QT-ENGINE-001`). Errors print `error: <code>: <message>` and
`hint: ...` on stderr, or with `--format json` exactly one `qt.error` document on stdout; `inspect --format json`
prints one `qt.nautilus_inspect` document (JSON Schema `queuetruth_nautilus/schemas/qt.nautilus_inspect.schema.json`)
whose `refused` field is the `qt.error` of a refused calibration. Warnings go to stderr.

**Validation (acceptance).** `integrations/nautilus/repro/heldout.py` runs the 5.3.3 cross-check harness on the
calibration's test dates with the package-built fill model substituted for the harness's own (the factory asserts
the requested probability equals the calibrated one) and scores each order's first fill against the exact arm.
Fill times are compared as exact int64 nanoseconds. Statistics use an order-level bootstrap (2,000 replicates,
seed 0) in which every paired statistic resamples orders jointly. Acceptance for an exportable `nt_prob_touch` fit:
at the 1 s horizon the calibrated model's |bias| (mean over seeds) is lower than the default's, and the 95% CI of
the paired |bias| reduction (|default bias| minus |calibrated bias|) excludes zero. The report also gives, per
horizon, the same reduction for one run (|bias| averaged over seeds), the paired order-level Brier gain for the
seed mean and for one run, and the seed-mean fills against queuetruth's per-order prediction for the fit
(`held_out/zoo_predictions.parquet`): mismatches on orders predicted with certainty, the mean difference with its CI,
and the squared deviation over the binomial variance. The criterion can fail: a `p` far below the fit underfills
and is rejected (`test_repro.py::test_paired_test_does_not_certify_an_overcorrection`). The criterion is met on
2020-01-30 with the RD-9 fit (`p = 0.2866`) on nautilus_trader 2.0.0rc7 (+2.40 [+1.97, +2.85] pp) and 1.231.0
(+2.47 [+2.02, +2.93] pp); at 60 s the reduction is not significant on 2.0.0rc7 and one run's Brier score is worse
than the default's at 10 s and 60 s (`integrations/nautilus/repro/results/`). Engine-level tests (synthetic, seeded)
pin the per-touch contract on both release lines: `p = 1` fills every touched order, `p = 0` none while a
trade-through still fills, one touch fills within 4 standard deviations of `N p` and two touches of
`N (1 - (1 - p)^2)`, equal seeds give equal fills, and `queue_position = True` from a queue-gated fit waits for the
visible shares ahead.

**Gap in 5.10 found by this work, fixed at integration.** `export nautilus` in queuetruth 0.1.1 emitted
`LatencyModel(..., delete_latency_nanos=...)` and imported from `nautilus_trader.backtest.models`; neither 1.231.0
nor 2.0.0rc7 accepts the keyword, and 2.x has no such module. `queuetruth.export.nautilus` now writes
`cancel_latency_nanos` and imports the 2.x names first, falling back to the 1.x ones (SPEC 5.10). The plugin still
reads both keywords, so snippets written by 0.1.1 keep working.
