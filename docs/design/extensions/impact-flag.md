# Extension: impact and queue-share warnings (`impact-flag`)

Proposed spec text for the integrator. Concept page: `docs/concepts/impact-warning.md`. Code:
`python/queuetruth/eval/impact.py`; tests: `python/tests/eval/test_impact.py`.

## 1.2 Out of scope (addition)

Shadows stay independent (no market impact). `queuetruth.eval.impact` quantifies when that assumption is doubtful
and warns; it never changes a fill.

## 3.4 New table `qt.shadow_impact` (Python writer)

Registered in `queuetruth.schemas.arrow.TABLES` and mirrored in `qt_schema` (`crates/schema/src/tables.rs`,
`SHADOW_IMPACT`, after `qt.score_tests`); `schemas/arrow/qt.shadow_impact.json` is generated. One row per shadow,
sorted by `shadow_id`; standard table metadata plus `qt.shadow_impact.source` (`replay`, `zoo`),
`qt.shadow_impact.volume_source` (`venue_session`, `user`, `mixed`) and, for zoo inputs,
`qt.shadow_impact.horizon_ns` and (when given) `qt.shadow_impact.h_max_ns`. Written with `schemas.io.write_table`.

```text
qt.shadow_impact          output: one row per shadow (Python eval, independent-shadow diagnostics)
  shadow_id: utf8 | instrument_id: uint32 | session_date: date32 | status: dict(live,rejected,out_of_session)
  qty: uint64 | filled_qty: uint64 [null] | fills_in_tape: bool | ts_live: ts | ts_end: ts
  end_source: dict(full_fill,trajectory_end,cancel,planned_end,admin_end,horizon,session_close,rejected)
  ref_price: int64 [null] | ref_price_source: dict(order,last_trade,none) | tick_size: int64 [null]
  daily_volume: uint64 [null] | volume_source: dict(venue_session,user) | sigma_daily: float64 [null]
  daily_participation: float64 [null] | impact_frac: float64 [null] | impact_bps: float64 [null]
  impact_ticks: float64 [null] | level_qty_at_live: uint64 [null] | queue_share_at_live: float64 [null]
  queue_share_max: float64 [null] | queue_min_qty: uint64 | life_volume: uint64 [null] | volume_share: float64 [null]
  flag_impact: bool | flag_queue: bool | flag_volume: bool | material: bool
```

## 3.10 New document `qt.shadow_impact_summary`

`queuetruth.schemas.models.ShadowImpactSummary` (in `DOC_MODELS`; Python-only, listed in `qt_schema`'s
`PYTHON_ONLY`): `source`, `config` (`ImpactConfig`), `volume_source`, `h_max_ns`, `horizon_ns`, `fills_in_tape`,
`dataset`, `inputs` and `files` (`FileEntry` lists: every input read and the written table, with sha256),
`n_shadows`, `n_rejected`, `n_out_of_session`, `n_assessed`, `n_evaluable_impact`, `n_evaluable_queue`,
`n_evaluable_volume`, `n_flag_impact`, `n_impact_venue_bound`, `n_flag_queue`, `n_queue_below_min_qty`,
`n_flag_volume`, `n_alone_at_level`, `n_material`, `material_share` (over assessed shadows; null when none),
`material`, `distributions` (`impact_bps`, `impact_ticks`, `daily_participation`, `queue_share_at_live`,
`queue_share_max`, `volume_share` over assessed shadows: `n`, `p50`, `p90`, `p99`, `max`, numpy linear quantiles),
`sensitivity` (assessed shadows at fixed threshold grids), `by_instrument` (material counts), `days` (per
instrument-day volume, sigma, volume source and counts), `warning` (string or null).

## 5.6a Semantics (normative)

- Session: the `qt.instruments` row with `midnight_utc_ns <= ts_live < midnight_utc_ns + 1 day` (latest), regular
  session `[midnight + open_s, midnight + close_s]`; without one the UTC day and no tick size.
- Status: `rejected` (replay `end_state` `rejected_*`; zoo `exact_end_state` `rejected*`): no figures;
  `out_of_session` (`ts_live` outside `[open, close)` of a known session, or the replay's `out_of_session`): queue and
  volume shares only; else `live`. Only `live` rows are flagged and counted in `n_assessed`.
- `daily_volume` = venue trade volume (every kind) in the session. `sigma_daily` = sqrt of the realized variance of
  previous-tick log trade prices (non-cross) on the grid `open, open + rv_interval_ns, ..., close`; grid points before
  the first trade take the opening cross print in `[open, first trade]` or else the first trade's price; returns
  ending before the first trade are not counted; null with fewer than `min_returns` returns or with a non-positive
  price in the session. A `daily` table (`instrument_id` int, `session_date` date32 or int days, `daily_volume` int,
  `sigma_daily` float; non-null keys) overrides either figure per instrument-day; `volume_source = user` when it
  gave `V`.
- `impact_frac = Y * sigma * sqrt(Q / V)` for `Q / V >= linear_below`, else `Y * sigma * sqrt(lb) * (Q / V) / lb`;
  `impact_ticks = impact_frac * ref_price / tick_size`.
- `queue_share_at_live = Q / (level_qty_at_live + Q)` (zoo: `visible_level_qty_at_live` when the exact level is
  null). `queue_share_max` = max of that and `leaves / (level_qty_after + leaves)` over `qt.queue_trajectory` rows
  (`leaves = Q - filled_qty > 0`); information only.
- Life `(ts_live, ts_end]`: replay = full fill, else trajectory `end` row, else min(cancel effective, planned end),
  else the session close; zoo = min(planned cancel, admin end, `ts_live + horizon`), `horizon` = the largest
  prediction horizon `<= h_max_ns` (all horizons when null; `QT-CONFIG-001` when a shadow has none), `filled_qty` =
  `truth_filled_qty` at that horizon (null on L2 datasets). `life_volume` = non-cross volume in the life.
  `volume_share = filled / (filled + life_volume)`, or `min(filled / life_volume, 1)` with `fills_in_tape`.
- Flags (inclusive): `flag_impact` = `impact_ticks >= impact_ticks` and (`volume_source = user` or
  `flag_venue_volume_impact`); `flag_queue` = `queue_share_at_live >= queue_share` and `qty >= queue_min_qty`
  (default: 100 shares in size units when `venue` or `source_venue` is `nasdaq` or `iex`, else `lot_size`, else 1);
  `flag_volume` = `volume_share >= volume_share`.
- Defaults (`ImpactConfig`): `coefficient = 1`, `linear_below = 1e-5`, `rv_interval_ns = 300 s`, `min_returns = 12`,
  `impact_ticks = 1`, `flag_venue_volume_impact = false`, `queue_share = 0.10`, `queue_min_qty = null`,
  `volume_share = 0.05`, `warn_fraction = 0.01`.
- Warning: when `n_material > 0` and `n_material / n_assessed >= warn_fraction`, `zoo` (`api.run_zoo`) and `score`
  (exact arm; on L2 datasets the zoo arm, impact and queue share only) append one `warnings` line starting
  `impact: `. `score` counts orders (an order is flagged when any leg is), marks legs of self-excluded twins
  (native exclusion) as `fills_in_tape` and names up to five flagged order keys; its internal zoo run is
  `suppressed()`. The hooks are fail-soft: any `QtError`, I/O, Arrow, value, type, key or memory error gives the
  single line `impact: not assessed (<code>: <message>)`.
- Errors (direct API and the module CLI): missing columns or nulls in required columns `QT-INPUT-002`; duplicate
  `shadow_id` or `daily` rows `QT-INPUT-003`; no trades in a dataset `QT-INPUT-001`; invalid config or an `h_max_ns`
  below every horizon `QT-CONFIG-001`.

## 4.x Entry point

`python -m queuetruth.eval.impact replay --replay DIR [--dataset DIR] [--twins] --out DIR` and
`python -m queuetruth.eval.impact zoo --zoo DIR [--dataset DIR] [--h-max-ns N] --out DIR` (both `--daily FILE`,
`--config JSON`). The dataset and `h_max_ns` default to the run's `summary.json` spec; `--twins` defaults to on when
the replay summary reports every shadow twin-linked. They write `shadow_impact.parquet` and
`shadow_impact_summary.json` and print the summary (exit 0, or 2 with a `qt.error` document). A `queuetruth impact`
subcommand (crates/cli grammar + `cli.py`) is not added here: it needs the CLI grammar, its golden files and a Rust
rebuild, owned elsewhere.
