# Extension: hftbacktest integration (`integrations/hftbacktest/`)

Proposed spec text for the integrator. It extends SPEC 5.10 (export templates) and 5.3.5 (hftbacktest npz export)
with a machine-readable queue-model config, an order-by-order reconciliation against hftbacktest, and the
handling of hftbacktest #312. Nothing here changes the `queuetruth` package, its schemas or its CLI; the code lives
under `integrations/hftbacktest/` and runs the hftbacktest side in a separate venv.

## E-HBT-1 Queue-model config (`qtint.hbt_queue_config` v1)

`export_config.py` turns `qt.calibration_result` files of the power families (`power`, `power2`, `power3`; C1,
5.4) into one JSON document, canonical (sorted keys, two-space indent, trailing newline, no NaN), so equal inputs
give equal bytes:

```
{schema: "qtint.hbt_queue_config", schema_version: 1, queuetruth_version,
 models: [{model_id, family, builder, args, exchange, exchange_builder, role, train_quasi_loglik, train_events,
           source, notes}]}
```

- `family` in `power|power2|power3|log|log2|risk_adverse`; `builder` is the hftbacktest `BacktestAsset` method
  (`power_prob_queue_model{,2,3}`, `log_prob_queue_model{,2}`, `risk_adverse_queue_model`) and `args` its float
  arguments (one positive finite `n` for the power families, none otherwise). `exchange` in `partial|no_partial`
  with `exchange_builder` `partial_fill_exchange|no_partial_fill_exchange`.
- `model_id` follows the ModelId grammar of 5.3.2 with `n` printed at full precision (shortest round-trip
  positional, as Rust `{}`), so the same string selects the identical model in `queuetruth zoo --models`. The
  snippet of 5.10 keeps `g6` rounding for display; the config never rounds.
- `role`: `fitted` (the calibration's estimate), `default` (`--defaults`: the family at `n = 2`, the held-out
  baseline), `fixed` (`--log`: the parameter-free log families).
- `source` of a `fitted` entry: run id, family, mode, `param_name` and `estimate` as the calibration states them,
  then on the `n` scale (`exp` applied when `param_name` is `ln_n`, `log_n` or `theta`): `n`, `ci_bootstrap_n`,
  `ci_profile_n` and `stratified` (per stratum `n`, `ci_n`, `events`); events, shadows, train and test windows, L2
  view, `held_out` (`{model_id, test, brier, logloss, fill_rate_bias}` per horizon), `held_out_summary`,
  `trust_verdict`, `trust_reasons` (refusing tokens) and `trust_flags` (every trust token with its text), data
  sources. A `default` entry's `source.held_out` holds the baseline's held-out metrics.
- `notes` of a `fitted` entry, in order: `NOT VALIDATED (exported with --force): ...` when forced; `held-out:
  <held_out_summary>`; `note (<token>): <text>` for each of `plateau`, `profile_ci_open`, `heterogeneous_strata`
  present (the same lines as the header of `queuetruth export hftbacktest`, `trust.header_lines`); the #312, #316
  and L2-view notes.
- `train_quasi_loglik`: the C1 quasi log likelihood on the calibration's train events. For `fixed` log entries it is
  parsed from the calibration's `info: comparison quasi log likelihood hbt_log{,2} = <l> (<n> events)` lines
  (5.4); null when absent or not finite. It ranks entries by how well they explain where cancellations come from,
  which is not the fill-rate target: the held-out metrics and the reconciliation's realism tables are the guide to
  fill realism.
- Comparison basis: several `--calibration`s must agree on dataset ids, shadows, train window, L2 view, exchange,
  mode and train events; otherwise `QT-CONFIG-001` ("export them to separate configs").
- Trust gate: a calibration that `queuetruth.export.trust.assess` refuses, or that has no finite positive `n`, is
  not exported (`ExportRefused`, `QT-CALIB-001`, exit 1, as `queuetruth export`) unless `--force`.
- Errors (exit 2): a non-power family `QT-CONFIG-001`; a missing file `QT-INPUT-001`; a malformed calibration
  `QT-INPUT-002`; a malformed config (unknown family, wrong arity, non-finite or non-positive `n`, unknown exchange
  or role, `model_id` not matching its fields, duplicate ids) `QT-CONFIG-001`.
- `hbt_config.apply(asset, entry)` applies the entry to a `BacktestAsset` (stdlib only, usable in the hftbacktest
  venv).

If the integrator promotes the format to a registered `qt.*` schema, a pydantic model and a generated JSON Schema
should follow (scripts/gen_schemas.py); until then the validation in `hbt_config.validate_entry` is the contract.

## E-HBT-2 hftbacktest driver (`qtint.hbt_driver_run` v1)

`hbt_driver.py` runs hftbacktest (any build) on a 5.3.5 npz and orders sidecar with zero latency and fees on
`HashMapMarketDepthBacktest`, as a numba `@njit` loop:

- An action at `ts` is issued after every feed row with `exch_ts <= ts` and before any later row (the O-ZOO-3 and
  5.3.2 activation rule). While an own order is live the loop steps to every distinct `exch_ts`; otherwise it jumps
  to the next action and, after the last one, replays the rest of the feed.
- A fill is recorded when an order's local `leaves_qty` decreases, at the order's `exch_timestamp`; two fills of one
  order at one timestamp are one record with their summed quantity.
- A cancel is sent only while the order is locally `NEW` or `PARTIALLY_FILLED` with no pending request.
- The first non-zero code of `elapse`, `submit_*` or `cancel` stops the run (`error: {code, ts, live_order_ids}`).
  If the failing `elapse` spanned more than one feed timestamp, the run is repeated stepping every timestamp from
  that elapse's start, so `error.ts` is the timestamp of the failing event.
- Provenance: `engine` (label), `engine_identity` (hftbacktest version, sha256 of its native module, numba,
  llvmlite, numpy and Python versions; `--identify` prints it alone), `npz_load_s` and `compile_s` (numba
  compilation on a two-row dummy feed before any timed run); per run `wall_s` is loop time only.
- Errors exit 2: missing arguments `QT-USAGE-001`, a bad config `QT-CONFIG-001`, a malformed sidecar
  `QT-INPUT-002`, an order id submitted twice `QT-INPUT-003`.
- Validated against the per-row O-ZOO-3 driver (`evidence/synthetic_regression.py`): identical fills (per order and
  timestamp) and stop codes on the 2,988 runs of the O-ZOO-3 scenarios with the released and the patched build,
  and on random feeds in the tests.

## E-HBT-3 Reconciliation (`qtint.hbt_reconciliation` v1)

`reconcile.py` (queuetruth venv) runs `replay`, `zoo --mode faithful` on the config's models, `export l2-npz` per
instrument-day with the same L2 view, writes one sidecar per instrument-day (every zoo shadow with a resolved price
that the exact engine did not reject and whose planned end is after `ts_live`, submitted GTX at `ts_live`,
cancelled at the planned end `min(ts_cancel_planned, session_close)`; skipped shadows are counted by reason,
`zero_life` included), runs `hbt_driver.py` in each given venv and writes `reconciliation.json` (deterministic: no
wall times; those go to `timings.json`), `reconciliation.md` and `per_order.parquet`:

- Cache: `manifest.json` (`qtint.hbt_reconcile_manifest` v1) records the dataset id (`manifest.json` `dataset_id`,
  else the instruments' sha256), the shadows' sha256, the L2 view, the exchange, the sorted model ids and, per
  `--hbt` label, its engine identity. Other inputs on an existing `--out`, a changed build under an existing label,
  or cached stages without a manifest are refused with `QT-USAGE-001` unless `--fresh`, which deletes the cached
  stages first. A cached driver result for other models or another build is refused the same way.
- Quantities are compared in real units: `qt.zoo_fills.fill_qty` (raw size units) is scaled by the
  instrument-day's `10^size_exp`.
- Engine parity per model and build: `orders`; `orders_compared` (all orders for a build that ran to the end; for a
  stopped build only the orders with `ts_live` before its stop on that instrument-day); `orders_with_fills`
  (compared orders with a fill on either side); `identical_fills` (compared orders whose fill sequence, aggregated
  per timestamp and cut at the stop, equals the faithful zoo's); `same_first_fill` (among `orders_with_fills`);
  up to 20 `differ` examples.
- Per instrument-day: the build's stop (`error`), `orders_live_before_stop`, `session_open_ns`, the first
  `hbt_312` zombie fill in `qt.zoo_fills`, the number of zombies, and `fills_before_stop_equal_reference` against
  the `--reference` build.
- `qt_first_abort` per model and build: the zoo's first aborted pair (from the stable `hbt_312_abort:` warning) and
  whether the build stops on that pair's instrument-day at that pair's abort time.
- For builds that ran to the end: `realism` (per horizon, over shadows whose label is defined (5.3.2), the 2x2 of
  exact first fill within `h` against the build's, both fill rates and the paired difference with the
  cluster-robust bootstrap CI and exact McNemar p of `queuetruth.stats.clustered_mcnemar`, clusters instrument x
  date x 30 min, `B = 1000`, seed from `--seed`), `realism_full_fill` (the same for the full fill; the build's full
  fill is the fill after which its leaves round to zero lots), `realism_by_date` (first fill, per session date) and
  `first_fill_delta` (over the whole order life: orders filled by both, by one, by neither; among those filled by
  both, how many at the same timestamp, earlier, later, and the 5/25/50/75/95% quantiles and mean absolute value of
  build minus exact, ns).
- `per_order.parquet`: one row per model, build and order: ids, side, price, quantity, `ts_live`, `ts_end`,
  `engine_stop_ts`, `compared`, exact `ts_first_fill` / `ts_full_fill` / `filled_qty`, the build's and the faithful
  zoo's first fill and filled quantity (the build's full fill too), `hbt_equals_qt`, and the differences
  `d_first_fill_ns` and `d_filled_qty` (build minus exact).
- Exit codes: 0 done, 2 any `QT-*` error.

## E-HBT-4 hftbacktest #312 (proposed upstream fix, not submitted)

`integrations/hftbacktest/upstream/` holds the patch against hftbacktest 5f3ec40 (applies to py-v2.4.4), its
Rust and Python tests, a minimal reproduction and `verify.sh`, which rebuilds the Rust and Python test results, the
reproduction's exit codes, the synthetic regression, the driver parity and the #316 measurement from public
sources and the repository's O-ZOO-3 scenarios. The real-data numbers need the Nasdaq ITCH datasets
(`reconcile.py`, then `evidence/readme_tables.py`). An open upstream pull request, nkaz001/hftbacktest#323, already
claims #312 with the `>=` change this patch shows to be incomplete; UPSTREAM.md recommends a review comment on it
rather than a competing pull request. The faithful zoo models keep reproducing the released behaviour (5.3.3 a,
per-pair abort); the patched build's fills equal the faithful zoo's on every order, because the fix only removes an
order that is already complete. If hftbacktest ships a fix, the faithful mode can gain an `hbt_version` switch that
disables the abort instead of excluding the shadows from the common population (5.6).

## E-HBT-5 Relation to the package's hftbacktest paths, and proposed wiring

The package already has two hftbacktest paths; this integration adds a third for a different question.

| path | input | output | question it answers |
|---|---|---|---|
| `queuetruth export hftbacktest` (5.10) | one calibration | a Python snippet (`g6` parameter, trust header) | what do I paste into my backtest? |
| `queuetruth export replay-schedule --target hftbacktest` | the user's live order schedule | a replay script (10 ms steps) whose output `import_sim("hftbacktest")` and `score --sim-log` read | does my backtest reproduce my live fills? |
| `integrations/hftbacktest` (this extension) | calibrations, shadows, a dataset | `qtint.hbt_queue_config`, `reconciliation.json`, `per_order.parquet` | which hftbacktest model is closest to exact truth on this data, and does hftbacktest itself agree with queuetruth's re-implementation? |

Proposed integrator wiring (not done here): `queuetruth export hftbacktest --format config` writing E-HBT-1 from
the package; `export replay-schedule --config FILE --model-id ID` so the replay script's `configure_asset` calls
`hbt_config.apply` (the README shows the manual edit today); optionally, `hbt_driver.py` writing its fills in the
`import_sim("hftbacktest")` JSON-lines format so its output can go to `score --sim-log`.

## Integrator hub edits (made at integration)

- `integrations/hftbacktest/tests` is in the pytest `testpaths` and in `make test-extra`; the tests that need
  hftbacktest use `.scratch/hbt-venv` and `.scratch/hbt-venv-312`, or `QT_HBT_PY` / `QT_HBT_PY_312`, and skip with a
  reason when they are missing.
- `integrations` is in the ruff paths of `make fmt`, `make lint` and CI.
- The docs nav lists this page under Design extensions; `integrations/hftbacktest/README.md` stays in the repository
  (it is outside `docs/`).
- The changelog fragment is folded into the CHANGELOG's Unreleased section.
