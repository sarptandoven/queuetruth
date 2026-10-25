# Extension: implementation-shortfall decomposition of the sim-live gap (`qt.score_shortfall`)

Proposed SPEC text (new 5.7.6, after the scorer's statistics) and the table schema. Implemented in
`python/queuetruth/score/shortfall.py`; the scorer hook is one call in `build_scorecard`
(`python/queuetruth/score/scorecard.py`).

## 5.7.6 Shortfall decomposition

For every scored order and every markout horizon `h` (`markout_horizons_ns`), each arm's result is the markout P&L
of its fills net of fees, `R_a = sum_f [ d (M(t_f + h) - P_f) q_f k - fee_f ]`, over the arm's fills of
`qt.score_fills` (maker and taker, inside the evaluation window W). `d = +1` buy / `-1` sell, `M` the venue mid of
the prevailing quote with the markout end-of-data rule and `markout_past_end`, `k = 10^price_exp 10^size_exp
multiplier` of the instrument-day of `ts_entry`. The gap is `G = R_sim - R_live` (positive: the simulator overstates
live).

Units are matched first in, first out: the k-th unit filled by live (fills in time order, ties in `fill_seq` order)
is paired with the k-th unit filled by the simulator, `k <= m = min(q_live, q_sim)`. `M-` is the mid of the last
quote strictly before the fill (as `mm_ticks`). The matched-unit terms follow implementation shortfall (Perold) with
the simulated fill as the benchmark: mid at the fill, price against that mid, mark. Buckets:

- `delay`: over matched units, `d (M-_live - M-_sim) k`.
- `spread`: over matched units, `d ((P_live - M-_live) - (P_sim - M-_sim)) k`.
- `adverse_selection`: over matched units, `d (M_sim(t+h) - M_live(t+h)) k`.
- `missed_fill`: an order filled (priced quantity > 0) in one arm only: `+R_sim` or `-R_live`, gross of fees.
- `partial_fill`: both arms filled, `q_live != q_sim`: the gross markout value of the units beyond `m` of the arm with
  more (`+` sim, `-` live).
- `fees`: `sum fee_live - sum fee_sim` over every fill. Fees in a currency other than the instrument's are left out;
  null fees count as 0. Both are counted in `note` (null fees only on an order with a known fee on some fill). The
  scorer's sim arm carries the live fee rate per liquidity (`apply_fee_rates`), so an external simulator's own fees
  are not used.

Properties: a late taker (same effective half-spread and mark in both arms) is all `delay`; two passive fills at the
same price have `delay = -spread` and the whole difference in `adverse_selection`; `delay` and `spread` do not depend
on `h`.

Identity: `delay + spread + adverse_selection + missed_fill + partial_fill + fees = G`, exact in integers of half
price units times size units (`2M = bid + ask`); each currency column is its integer times `k / 2`, correctly
rounded once, and `total_ccy` is computed from each arm's fills independently (it equals `sum(mo_ccy) - fees` of
`qt.score_markouts`, ref `mid`, per arm).

An order is unvalued at a horizon (`valued = false`, null buckets, reason in `note`) when a fill has no price (the
scorer passes the quantity of the maker and taker fills it dropped for having no price; a library caller without
those counts gets unpriced maker quantity inferred from `qt.score_orders.{live,sim}_filled_qty`), when `M(t+h)` is
null for a fill, or when `M-` is null for a fill with a matched unit; without a dataset every order is unvalued.

Totals per (horizon, currency) over valued orders: `sum`, `mean` and the 95% percentile CI of the per-order mean by
the scorer's block bootstrap. The block kind is chosen once over all scored orders, as in `statistics.assemble`
(`statistics.block_labels(orders, bootstrap.block)`), and reused for every (horizon, currency) subset; one stream
per (currency, horizon), test id `score_shortfall:<currency or ->:<h>`, shared by every bucket
(`stats.block_counts`, `bootstrap.b`). Replicates equal `stats.block_replicates` of the mean with that seed. The
total rows' `note` also counts what is not decomposed: live fills of orders outside the scored orders (fills-only
logs) and live orders without a sim outcome (`n_unmatched_live`).

Rows: total rows first, ordered by (horizon, currency, statistic `sum`, `mean`, `ci_lo`, `ci_hi`), then order rows by
(horizon, `order_key`). Written as `score_shortfall.parquet` whenever `score --out` is set, and listed in the summary's
`files`.

## `qt.score_shortfall` (Arrow)

| # | column | type | nullable | values / meaning |
|---|---|---|---|---|
| 1 | `scope` | dictionary<int8, utf8> | no | `order`, `total` |
| 2 | `statistic` | dictionary<int8, utf8> | no | `value` (order rows), `sum`, `mean`, `ci_lo`, `ci_hi` |
| 3 | `horizon_ns` | int64 | no | markout horizon |
| 4 | `order_key` | utf8 | yes | null on total rows |
| 5 | `instrument_id` | uint32 | yes | |
| 6 | `session_date` | date32 | yes | instrument-day of `ts_entry` |
| 7 | `side` | dictionary<int8, utf8> | yes | `buy`, `sell` |
| 8 | `class` | dictionary<int8, utf8> | yes | the scorecard's maker class: `both`, `neither`, `live_only`, `sim_only` |
| 9 | `currency` | utf8 | yes | instrument currency |
| 10 | `qty` | uint64 | yes | order quantity (total rows: sum over valued orders) |
| 11 | `live_qty` | uint64 | yes | priced fill quantity in W, maker and taker |
| 12 | `sim_qty` | uint64 | yes | same, sim arm |
| 13 | `matched_qty` | uint64 | yes | `min(live_qty, sim_qty)` |
| 14 | `valued` | bool | no | total rows: at least one valued order |
| 15-20 | `delay_ccy`, `spread_ccy`, `missed_fill_ccy`, `adverse_selection_ccy`, `partial_fill_ccy`, `fees_ccy` | float64 | yes | buckets in currency |
| 21 | `total_ccy` | float64 | yes | the gap `R_sim - R_live` |
| 22 | `notional_ccy` | float64 | yes | `qty * abs(limit_px) * k`, limit price of the first leg; null when it is 0 or null |
| 23 | `live_notional_ccy` | float64 | yes | live filled notional `sum abs(P) q k` (bps denominator) |
| 24 | `sim_notional_ccy` | float64 | yes | same, sim arm |
| 25 | `n` | uint64 | no | 1 / 0 on order rows; valued orders on total rows |
| 26 | `note` | utf8 | yes | unvalued reason and fee counts; on total rows the unvalued, fee and not-decomposed counts and the bootstrap description |

Schema metadata: `qt.schema = qt.score_shortfall`, `qt.schema_version = 1.1`, `qt.engine_version`. All columns are
v0.1 columns (no `since`).

## Integration notes (for the integrator)

- Registration (done here): `qt.score_shortfall` is in `queuetruth.schemas.arrow.TABLES` after `qt.score_tests`,
  mirrored in `crates/schema/src/tables.rs` (`SCORE_SHORTFALL`), with `schemas/arrow/qt.score_shortfall.json` from
  `scripts/gen_schemas.py` and the table counts in `crates/schema/src/tests.rs` and `python/tests/test_schemas.py`
  raised from 29 to 30 (another workstream that adds a table raises the same counts; the merged value is the sum).
  Left for the integrator, because this workstream may not edit `docs/reference/*`: run
  `python docs/gen_reference.py` (the table appears in `docs/reference/schemas.md`); `docs/check_docs.py` reports the
  reference as stale until then.
- Hub edits made: `python/queuetruth/score/scorecard.py` (one import; a dict `unpriced` that collects the quantity of
  the fills skipped for having no price, at the existing `if f.px is None` skip; one `score_shortfall(...)` call with
  `unpriced_qty` and `n_unmatched_live`, written after `score_tests.parquet`; it reads `fills_ext` after
  `apply_fee_rates`), and the expected file list of
  `python/tests/score/test_score_scorecard.py::test_exact_arm_outputs_and_schema` (one line). The task named
  `score/runner.py` for the hook, but the scorer writes its outputs in `build_scorecard`, where the BBO of the run is
  in memory; `runner.py` is unchanged.
- Docs: add `how-to/decompose-the-sim-live-gap.md` to `docs/mkdocs.yml` nav. Stale lists of the scorer's outputs:
  `docs/concepts/scorecard.md` "Output files" (add `score_shortfall.parquet`, `qt.score_shortfall`: the gap per order
  and horizon split into delay, spread, missed fills, adverse selection, partial fills and fees, with totals and CIs),
  `docs/how-to/score-nautilus-vs-binance.md` "Reading the result" ("five tables" becomes six), and the
  "Output directory" bullet of SPEC 5.7 (`docs/design/spec.md`) together with the new 5.7.6 above.
- Follow-ups (not done here): surface the decomposition outside the parquet file (a one-line shortfall headline in
  the CLI RESULT text and `summary.json`, a section in the HTML report, the MCP and HTTP score results); move
  `shortfall.quote_groups` into `markouts.py` so `compute_markouts` indexes the BBO once (it still builds its quote
  groups with a per-row loop, and the two are kept equal by `test_quote_groups_equal_the_markout_quotes`).
- Performance: the step always runs. It plans each order once (FIFO segments, fees), finds every fill's `M-` once
  and its marks once per horizon with vectorized `searchsorted`, does only small integer sums per order and horizon,
  and accumulates the output column by column. Synthetic benchmark (`shortfall_table`, 20,000 orders with 4 fills per
  arm, 7 horizons, 140,028 rows) on the shared laptop under load (load average 150 to 230), 2026-10-24: 20.2, 27.8
  and 43.7 s before this change, 9.5, 10.7 and 13.3 s after; traced peak memory for 5,000 orders 80 MB before, 66 MB
  after. Cost is linear in orders times horizons. Further steps: vectorize the per-segment sums, and offer a spec
  switch to skip the table (the spec model is not owned here).
