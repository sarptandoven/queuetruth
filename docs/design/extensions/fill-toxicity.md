# Extension: fill-type tags and the adverse-selection ranking of zoo models (`zoo-toxicity`)

Status: built (Python; one CLI grammar row). Proposed for SPEC 4.4 (command table), 3.10 (documents) and 5.6
(evaluation). User page: docs/concepts/fill-toxicity.md. Implementation: `python/queuetruth/eval/toxicity.py`;
tests: `python/tests/eval/test_toxicity.py`.

## Command

| Command | Runs in | Required | Optional |
|---|---|---|---|
| `zoo-toxicity` | python | `--zoo DIR`, `--data DATASET`, `--out DIR` | `--shadows`, `--amends`, `--horizons` (100ms,1s,10s,60s), `--window` (the zoo's `h_max_ns`), `--rank-horizon` (60s), `--bootstrap-b` (1000), `--allow-truth-mismatch`, `--seed` (0), `--threads` (1) |

Arguments (`queuetruth.eval.toxicity.ToxicitySpec`, unknown fields rejected, `QT-CONFIG-001` on invalid values):
`zoo` (PathRef: a zoo output directory), `dataset` (PathRef: the MBO dataset the zoo ran on), `shadows`
(PathRef|null: the zoo run's shadow spec; null = the zoo summary's `spec.shadows`), `amends` (PathRef|null; null =
the zoo summary's `spec.amends`), `out` (str|null), `horizons_ns` ([int], at least one, every value > 0; stored
sorted and without duplicates), `window_ns` (int > 0 | null; null = the zoo's `h_max_ns`, resolved before the
run_id), `rank_horizon_ns` (int > 0, one of `horizons_ns`), `bootstrap_b` (int >= 1), `allow_truth_mismatch` (bool,
false), `threads` (int >= 1, execution-only: the replay's threads), `seed` (int >= 0). The spec is Python-only (no
`qt.*_spec` schema yet).

## Inputs and the exact replay

From `--zoo`: `summary.json` (`qt.zoo_summary`; `QT-INPUT-002` when it is not one), `zoo_predictions.parquet`,
`zoo_fills.parquet` (`QT-INPUT-001` when missing, hint: rerun without `--no-fills`) and `zoo_shadows.parquet` (the
planned life, clamped to `ts_live` as `api._planned_life`). `--data` must hold a `manifest.json` (`QT-INPUT-001`)
of an MBO dataset (`QT-INPUT-002`) whose `dataset_id` equals the zoo's (the zoo summary's `spec.dataset.dataset_id`,
or the manifest at its recorded path when that resolves; `QT-INPUT-002` on a difference). The horizons, the rank
horizon and the window (a zoo prediction horizon: `spec.horizons_ns` up to `h_max_ns`, plus `h_max_ns`) are checked
against the zoo summary before the replay (`QT-CONFIG-001`). The exact arm's fill rows come from `qt.replay` with the
zoo summary's `shadows` (or `--shadows`) and `amends` (or `--amends`; `QT-INPUT-001`, hint naming the flag, when the
recorded path does not resolve), `latency`, `exclude_order_ids` and `seed`, `threads` = `--threads`, `bbo = change`,
`trajectory = none`, fill rule `exact` and the venue's default hidden-print policy (the zoo's exact engine), into a
temporary directory. Its `shadow_results`, `shadow_fills`, `trades` and `bbo` are used; the instrument-days come
from the dataset's `instruments.parquet` and the instrument-day's last event from the manifest's partition `ts_max`.

## Population

The leaderboard's rule (5.6) applied at `W`: `common_population` (no faithful hbt #312 abort on the shadow), an exact arm
(`truth_filled` not null), no planned cancel before `ts_live + W` and an administrative end at or after `ts_live + W`
(`W` = `window_ns`, default the zoo's `h_max_ns`, which must be a prediction horizon: `QT-CONFIG-001`). The exact
truth is read from the `horizon_ns = W` prediction rows. When the replay's fill rows with `ts_event <= ts_live + W`
disagree with that truth on some population shadow (filled vs not filled, or a different first-fill time), the replay
is not the zoo's exact arm: `QT-INPUT-002` (`truth_mismatch: <n> of <m> shadow(s) ...`, hint naming `--data`,
`--shadows`, `--amends`). With `allow_truth_mismatch` those shadows are left out instead (`n_excluded.truth_mismatch`,
a warning line, `truth_mismatch_rate` = mismatches / checked population shadows). A shadow whose replay `end_state`
starts with `rejected_` (it never went live) and that no arm fills is left out (`n_excluded.never_live`); a model
fill on one keeps it, as a phantom. Every population shadow must have a replay `shadow_results` row (`QT-INPUT-002`)
and a planned-life row (`QT-INPUT-002`, A2 / OC-7). `n_excluded` = {`hbt_312`, `no_exact_arm`, `cancel_bearing`,
`no_label`, `truth_mismatch`, `never_live`}.

## Mechanisms and statuses

Exact rows (`ts_event <= ts_live + W`): `queue_depleted` -> `queue`, `trade_through` -> `swept`, `crossed` ->
`crossed_resting`, `crossed_at_live` -> `cross_on_arrival`; a `queue_depleted` or `trade_through` row whose
(instrument_id, ts_event, sequence) is a `qt.trades` row of kind `hidden` and of no kind `displayed` is
`hidden_print` (`ts_event` keeps the sequences of different days apart; the
replay writes a print record's own sequence; an execution's T, F and removal records share one; ITCH cross prints
are kind `cross` and never tag). Model rows (`qt.zoo_fills`, same window): `queue` -> `queue`, `trade_through` ->
`swept`, `crossed` -> `crossed_resting`, `crossed_at_live` -> `cross_on_arrival`, `touch_draw` -> `touch`. Particle
families (`PARTICLE_FAMILIES`, `databento_mc`): one row per shadow with `p_fill(W) >= 0.5` and a non-null
`model_ts_first_fill <= ts_live + W`, at that time (the median particle first fill), `fill_seq` 0, the shadow's
whole `qty`, reason and mechanism `particle`, liquidity `maker`. Unknown reasons are `QT-INPUT-002`.

Status of (model, shadow): model filled = the model has a `qt.zoo_fills` row with `ts_event <= ts_live + W` (the
realized simulated fill; this includes the random-draw families `nt_prob_touch[p<1]` and `nt_queue_l2_prob_touch`,
whose analytic `p_fill` would otherwise disagree with their drawn rows), and for `PARTICLE_FAMILIES` `p_fill >= 0.5`
at `horizon_ns = W` (NaN = not filled); exact filled = `truth_filled` there; `confirmed`, `phantom` (model only),
`missed` (exact only), `neither`. `n_inconsistent` is a cross-check, warning `inconsistent: <model>: <n> shadow(s)
...`: a non-particle pair whose `p_fill` at `W` is exactly 0 or 1 and disagrees with the presence of a fill row (the
rows decide; the pair keeps its markouts), or a filled particle pair without a `model_ts_first_fill` in the window
(left out of every markout statistic).

Fill prices: exact rows `fill_px`; model maker rows the shadow's `qt.shadow_results.price`; model taker rows
(`liquidity = taker`) `min(ask, limit)` for a buy and `max(bid, limit)` for a sell at the prevailing quote at
`ts_event` (`limit` itself when there is none, with a `taker_price:` warning).

## Markouts

Per fill row and horizon `h` (SPEC 5.7.5 conventions, oracle O-SCORE-3): instrument-day of the fill = the
`qt.instruments` row with the latest `midnight_utc_ns <= ts_event` (the first row when none); `M(t)` = mid of the
last `qt.bbo` row of that instrument-day with `ts_event <= t` (equal timestamps: the last in file order), undefined
when there is none, a side is null, or `t` is after the instrument-day's last event; `M(t-)` = the last row strictly
before `t` (no end check). `mo_ticks = d (M(t+h) - P) / tick`, `mo_bps = d (M(t+h) - P) / |P| 1e4` (P != 0),
`mm_ticks = d (M(t+h) - M(t-)) / tick`, `mm_bps = d (M(t+h) - M(t-)) / M(t-) 1e4` (M(t-) > 0), `d` = +1 for side
`B`, -1 otherwise; integer price differences are formed before any float operation. `mo_*` needs a fill price.

## Statistics

First fill of (arm, shadow) = its row with the smallest (`ts_event`, `fill_seq`). Per model `m` and horizon `h`,
with `mo^m_s`, `mo^e_s` the first-fill `mo_ticks` of shadow `s` in the model and the exact arm, the shadows `ok` =
population, not a particle pair without a median first fill, `mo^m_s` defined if the model filled `s`, `mo^e_s`
defined if the exact arm filled it (`n = |ok|`). Each statistic is `sum(num) / sum(den)` over shadows:

| Statistic | num | den |
|---|---|---|
| `model_pnl_ticks` | `mo^m` on model-filled | 1 on `ok` |
| `exact_pnl_ticks` | `mo^e` on exact-filled | 1 on `ok` |
| `grant_ticks` | `mo^m` on phantom | 1 on `ok` |
| `deny_ticks` | `-mo^e` on missed | 1 on `ok` |
| `timing_ticks` | `mo^m - mo^e` on confirmed | 1 on `ok` |
| `bias_ticks` | grant + timing + deny | 1 on `ok` |
| `phantom_mo_ticks`, `missed_mo_ticks` (`mo^e`), `confirmed_mo_ticks`, `confirmed_exact_mo_ticks` (`mo^e`) | the markout | 1 on `ok` and the status |
| `*_mm_ticks` (same four) | first-fill `mm_ticks` | 1 on population (less the particle pairs above), the status and `mm` defined |
| `*_bps` (grant, deny, timing, bias) | as the ticks rows with `mo_bps` | 1 on the shadows whose `mo_bps` are defined likewise |

`bias = grant + timing + deny = model_pnl - exact_pnl` holds exactly (tested). Fill-type table `by_type`: arms in
order `exact`, then models in first-appearance order of the predictions; per arm the status groups (exact: null;
model: `confirmed`, `phantom`, then `missed` = the exact rows of the model's missed shadows), per group each present
mechanism in `MECHANISMS` order then `all`; per horizon `mo_ticks`, `mo_bps`, `mm_ticks`, `mm_bps` with num =
`fill_qty x value`, den = `fill_qty`, over the group's rows where the value is defined.

CIs: 95% percentile. Blocks: `block_labels(session_date, ts_live)` over the population's shadows (dates when >= 5,
else UTC half-hours); draws `block_counts(n_blocks, bootstrap_b, stable_seed(seed, [b"bootstrap",
"zoo_toxicity"]))`, one matrix for every statistic; replicate `b` = `sum_k count_bk num_k / sum_k count_bk den_k`
with `num_k`, `den_k` the block sums (a fill row belongs to its shadow's block); replicates with a zero denominator
are dropped. A statistic with no row in its denominator has `value`, `ci_lo`, `ci_hi` null and `n` 0. Rankings at
`rank_horizon_ns`: `grant`, `deny` and `bias` each by `|grant_ticks|`, `|deny_ticks|`, `|bias_ticks|` descending
(rank 1 = the largest distortion of that kind; `value` keeps its sign); null values last; ties in model order;
`rank` from 1. From the shared replicates of the absolute values (`(B, models)`; NaN = minus infinity): `rank_lo` =
2.5% quantile (method `lower`) of the replicate's competition rank `1 + #{others > it}`, `rank_hi` = 97.5% quantile
(method `higher`) of `#{models >= it}` (null for a null value); `gap_to_next` = {`value` = `|v_i| - |v_next|`,
percentile CI of the replicate differences} and `separated_from_next` = the CI's lower end > 0 (both null on the last
place or next to a null value).

## Outputs

`<out>/summary.json`: `qt.zoo_toxicity` (Pydantic `queuetruth.schemas.models.ZooToxicity`, generated JSON Schema
`schemas/json/qt.zoo_toxicity.schema.json`, Python-only like the service documents): `run_id`, `engine_version`,
`spec` (echo), `zoo_run_id`, `zoo_mode`, `n_shadows`, `n_excluded`, `truth_mismatch_rate` (null when no shadow was
checked), `window_ns`, `horizons_ns` (sorted, unique),
`ci_block` (`day`/`30min`), `n_blocks`, `bootstrap_b`, `exact` {`n_filled`, `n_fills`, `fills_by_mechanism`},
`models` [{`model_id`, `family`, `n_shadows`, `n_model_filled`, `n_exact_filled`, `n_confirmed`, `n_phantom`,
`n_missed`, `n_inconsistent`, `phantom_rate`, `missed_rate`, `horizons` {h: {`n`, the statistics above as
{`value`, `ci_lo`, `ci_hi`, `n`}}}}], `by_type` [{`arm`, `status`, `mechanism`, `n_fills`, `n_shadows`, `qty`,
`markouts` {h: {`mo_ticks`, `mo_bps`, `mm_ticks`, `mm_bps`}}}], `rankings` {`horizon_ns`, `grant`, `deny`, `bias`:
[{`rank`, `model_id`, `value`, `ci_lo`, `ci_hi`, `rank_lo`, `rank_hi`, `gap_to_next`, `separated_from_next`}]},
`files`, `warnings` (the planned-life clamp notes first).

`run_id` (7.3): `run_id(spec, engine_version, resolve)` of the spec dump, where the `zoo` reference resolves to
`sha256` over the lines `"<name>\n<sha256>\n"` of `summary.json`, `zoo_predictions.parquet`, `zoo_fills.parquet`,
`zoo_shadows.parquet` in that order and every other reference as `queuetruth.score._io.resolve_ref` (dataset ->
`dataset_id`, file -> its sha256). `out` and `threads` are execution-only; `window_ns` is hashed resolved, so an
explicit window equal to `h_max_ns` is the same run.

Arrow tables (ZSTD Parquet through `schemas.io.write_table`; metadata `qt.schema`, `qt.schema_version` = "1.1",
`qt.engine_version`; dictionaries `dictionary<int8, utf8>` with the listed values; defined in
`queuetruth.eval.toxicity.TABLES`, and registered in `qt_schema::tables`, `queuetruth.schemas.arrow.TABLES` and
`schemas/arrow`):

```text
qt.zoo_toxicity_fills      one row per fill row (exact arm first, then models; by shadow, then fill_seq)
  arm: utf8 | shadow_id: utf8 | fill_seq: uint32 | instrument_id: uint32 | side: uint8 | ts_event: ts
  px: int64 [null] | fill_qty: float64 | liquidity: dict(maker,taker)
  mechanism: dict(queue,swept,cross_on_arrival,crossed_resting,hidden_print,touch,particle)
  status: dict(confirmed,phantom) [null] (null on exact rows) | reason: utf8
qt.zoo_toxicity_markouts   one row per fill row x horizon (fill order, then horizons ascending)
  arm: utf8 | shadow_id: utf8 | fill_seq: uint32 | horizon_ns: int64
  mo_ticks: float64 [null] | mo_bps: float64 [null] | mm_ticks: float64 [null] | mm_bps: float64 [null]
qt.zoo_toxicity_shadows    one row per (model, population shadow): models in order, shadows in first-appearance order
  model_id: utf8 | shadow_id: utf8 | instrument_id: uint32 | session_date: date32 | ts_live: ts
  status: dict(confirmed,phantom,missed,neither) | model_ts_first_fill: ts [null] | exact_ts_first_fill: ts [null]
  model_mechanism: dict(<mechanisms>) [null] | exact_mechanism: dict(<mechanisms>) [null]
```

## Integration notes

- The three tables were registered at integration in `qt_schema::tables` and `queuetruth.schemas.arrow.TABLES`
  (with `schemas/arrow` dumps), so `doctor` and `schema show` cover them; a test checks that the writer produces
  exactly the registered layouts.
- `docs/reference/cli.md` and the other generated references need `docs/gen_reference.py`; `docs/mkdocs.yml` needs
  the new concept page in its nav.
- Not exposed through the HTTP service or the MCP server yet.
- Human view: `queuetruth.eval.toxicity.render_toxicity`, registered in `cli.render` like the zoo, score and dq
  views (it falls back to the plain rendering on any error).
