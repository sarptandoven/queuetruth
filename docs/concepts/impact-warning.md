# The impact warning

Shadows are independent: the replay places each one in the real queue, but the shadow never consumes liquidity,
never changes the book and never sees the other shadows ([Shadow orders](shadow-orders.md)). That is exact for an
order too small to matter and doubtful for one that is not. An order that is a large part of its price level, or of
the volume traded while it rests, changes what other traders see and do, and a simulator that keeps the tape fixed
cannot show that.

`queuetruth.eval.impact` measures, for every shadow, how doubtful the assumption is. It writes one
`qt.shadow_impact` row per shadow and a `qt.shadow_impact_summary` document. `zoo` and `score` add one warning line
when the flagged share of a run is material. On the day-one Nasdaq zoo run it reads:

```text
impact: 1125 of 2879 shadows (39.1%) may not be independent of the book (>= 10% of the displayed queue at live,
round lot or more: 1125; >= 5% of traded volume over life: 0; impact >= 1 tick on venue volume only (upper bound,
not flagged): 750); a fill rate over them could move by up to 39.1 pts; per shadow: python -m
queuetruth.eval.impact zoo --zoo DIR
```

(one line in the output; wrapped here). The warning changes no number in the run. It says which results lean on an
assumption the data call weak, and how much is at stake: if every flagged shadow's outcome flipped, a fill rate over
the assessed shadows would move by at most the flagged share.

## Which shadows are assessed

Only shadows that rested in the regular session are assessed and flagged (`status = live`):

- `rejected`: shadows that never rested (`rejected_would_cross`, `rejected_no_touch`). They carry no figures.
- `out_of_session`: shadows that went live before the open or after the close. They carry queue and volume shares
  for information, but no impact estimate, because the daily figures describe the regular session.

The summary counts both groups (`n_rejected`, `n_out_of_session`), and `material_share` divides by the assessed
shadows (`n_assessed`).

## Three measures

Each measure looks at one way an order affects other traders.

**Price impact (square-root law).** Across many markets, the average price move caused by executing a quantity `Q`
is close to

```text
I = Y * sigma_daily * sqrt(Q / V)
```

where `sigma_daily` is the daily volatility, `V` the daily volume and `Y` a coefficient of order one (Toth et al.
2011; Bouchaud et al. 2018, chapter 12; Almgren et al. 2005 fit an exponent of 3/5 to US equity orders). The default
is `Y = 1`. The law was measured on metaorders, and for small volumes impact becomes linear in the volume (Donier et
al. 2015 predict it; Bucci et al. 2019 measure the crossover from linear to square root). Below a participation
`Q / V = linear_below` (default `1e-5`) the estimate is continued linearly,
`Y * sigma_daily * sqrt(linear_below) * (Q / V) / linear_below`. Where the crossover lies depends on how the order is
executed, so the default is a convention; set `linear_below = 0` for the pure square root. The estimate is reported
as a fraction, in basis points and in ticks (`impact_frac * ref_price / tick_size`).

**Queue share.** `Q / (level_qty_at_live + Q)`, where `level_qty_at_live` is the displayed quantity at the shadow's
price when it goes live (the shadow is never in the book). Order flow at a level depends on the level's size (Huang
et al. 2015), and the sizes at the best prices predict the next price move (Gould and Bonart 2016). A shadow that is
a large part of its level therefore changes a quantity other traders act on. A share of 1 means the shadow would be
alone at its price: it would create the level, and inside the spread it would set the quote. Zoo runs on L2 data use
the visible level. The replay also reports `queue_share_max`, the largest share of the remaining quantity over the
shadow's life from its queue trajectory. That figure looks ahead (a level tends toward the shadow as it empties into
it), so it is reported for information and never flagged.

**Volume share.** `filled_qty / (filled_qty + life_volume)`, where `life_volume` is the volume the venue printed in
the instrument during the shadow's life `(ts_live, ts_end]` (auction crosses excluded when the dataset marks them).
An independent shadow's fills are added on top of the real prints. Had the order been real, the aggressors that
filled it would have taken that much from the queue instead, so the tape the replay keeps fixed is off by this share.
When a shadow replaced a self-excluded twin (`score` with self-exclusion, or a replay with `exclude_order_ids`), the
twin's executions are the shadow's fills and are already in the trades, so the share is `filled_qty / life_volume`
(`fills_in_tape`). Zoo runs on L2 data have no exact filled quantity and no volume share.

## Daily volume and volatility

By default both come from the dataset, per instrument and session date:

- `daily_volume`: the venue's traded volume in the regular session (`open_s` to `close_s` of `qt.instruments`).
- `sigma_daily`: the square root of the realized variance of 5-minute log returns of the last trade price over the
  session (previous-tick sampling; Liu, Patton and Sheppard 2015 find 5-minute realized variance hard to beat). The
  price at the open is the opening cross print when one prints before the first trade, else the first trade, so the
  opening interval counts. Fewer than 12 returns gives no estimate. A non-positive price in the session (a calendar
  spread, a negative-price future) leaves it null: log returns are undefined there. The session figure leaves out
  the overnight return, so it is lower than a close-to-close volatility.

One venue sees only part of the market's volume, and a smaller `V` gives a larger impact. Venue figures overstate the
impact by `sqrt(consolidated volume / venue volume)`. The impact flag therefore applies only to shadows whose `V`
came from a `daily` table (`volume_source = user`). With venue volume only, the shadows at or above the threshold are
counted as `n_impact_venue_bound` and shown in the warning as an upper bound. `flag_venue_volume_impact = true` flags
them anyway. The `daily` table has the columns `instrument_id`, `session_date` (date32), `daily_volume` and
`sigma_daily`, each figure nullable.

## Thresholds

| Flag | Default | What the default means |
|---|---|---|
| `flag_impact` | `impact_ticks >= 1`, consolidated `V` only | The estimate reaches the price grid. Below a tick it is smaller than the smallest price change, so it says nothing about the queue the replay follows. This is a convention. |
| `flag_queue` | at-live share `>= 0.10` and `qty >= queue_min_qty` | The shadow is at least a tenth of its displayed level. `queue_min_qty` defaults to the instrument's round lot: 100 shares on US equity venues (`nasdaq`, `iex`), else `lot_size`. In 2019 an odd lot was not part of the protected quote (Regulation NMS), so an odd lot alone at a price did not set the quote others saw. The 10% level is a convention. |
| `flag_volume` | volume share `>= 0.05` | One print in twenty of the shadow's life would have been the shadow's own. This is a convention. |
| run warning | flagged share of assessed shadows `>= 0.01` | Below 1% of shadows, even if every flagged outcome flipped, a fill rate over the assessed shadows would move by less than one percentage point. |

`material` is true when any flag is set. Every threshold is a field of `ImpactConfig`. The summary's `sensitivity`
block counts the assessed shadows at a grid of other thresholds (the impact grid counts every evaluable estimate,
whatever its volume source), and `n_queue_below_min_qty` counts the shadows at or above the queue-share threshold that
are smaller than `queue_min_qty`.

## Run it

`zoo` and `score` add the warning line by themselves. `score` counts live orders rather than legs (an order is
flagged when any of its legs is) and names the first flagged order keys, because its replay runs in a temporary
directory. For the per-shadow table, run the module on a replay or a zoo output directory. The dataset and, for a zoo
run, `h_max_ns` default to the run's `summary.json` spec. On the synthetic fixture:

<!-- check-docs: run -->
```bash
queuetruth zoo --data "$QT_FIXTURES/synth_small" --shadows "$QT_FIXTURES/synth_small/truth/shadows.parquet" \
  --out zoo --models front_of_queue --bootstrap-b 20 > /dev/null
python -m queuetruth.eval.impact zoo --zoo zoo --out impact > summary.json
python -c "import json; d = json.load(open('summary.json')); print(d['n_assessed'], d['n_material'], d['material'])"
```

`replay --replay DIR` reads `shadow_results.parquet`, `trades.parquet` (else the dataset's trades) and, when present,
`queue_trajectory.parquet`. `--twins` marks every shadow as having replaced a self-excluded twin; it is the default
when the replay summary reports every shadow twin-linked. Both commands take `--dataset DIR`, `--daily FILE` and
`--config JSON` (any `ImpactConfig` field), write `shadow_impact.parquet` and `shadow_impact_summary.json` (with the
sha256 of every input and of the table) and print the summary. From Python:

<!-- check-docs: skip (needs replay outputs) -->
```python
from queuetruth.eval import impact

table = impact.shadow_impact(results, trades, instruments, trajectory=trajectory)
summary = impact.summarize(table)
print(summary.warning)
```

A replay shadow's life ends at its full fill, else at the end row of its queue trajectory, else at the earlier of its
cancel and its planned end, else at its session's close. A zoo shadow's life is the largest prediction horizon up to
`h_max_ns`, cut by the planned cancel and the administrative end, and its `filled_qty` is the exact filled quantity at
that horizon. The session of a shadow is the instrument row whose day contains `ts_live`; without one it is the UTC
day, with no tick size.

The hooks that `zoo` and `score` call never fail the run. An input they cannot assess gives one line,
`impact: not assessed (<code>: <message>)`, and the run finishes.

## Output columns

| Column | Meaning |
|---|---|
| `shadow_id`, `instrument_id`, `session_date`, `status` | The shadow, its session and whether it was assessed (`live`, `rejected`, `out_of_session`). |
| `qty`, `filled_qty`, `fills_in_tape` | Posted and filled quantity (null without an exact arm), and whether the fills are already in the trades. |
| `ts_live`, `ts_end`, `end_source` | Life of the shadow and what ended it (`full_fill`, `trajectory_end`, `cancel`, `planned_end`, `admin_end`, `horizon`, `session_close`, `rejected`). |
| `ref_price`, `ref_price_source`, `tick_size` | Price used for ticks: the shadow's own (`order`), else the last trade at or before `ts_live` (`last_trade`), else none. |
| `daily_volume`, `volume_source`, `sigma_daily`, `daily_participation` | `V` and where it came from, `sigma_daily`, `Q / V`. |
| `impact_frac`, `impact_bps`, `impact_ticks` | The impact estimate (assessed shadows only). |
| `level_qty_at_live`, `queue_share_at_live`, `queue_share_max`, `queue_min_qty` | Queue share at live, its maximum over the life, and the size the queue flag needs. |
| `life_volume`, `volume_share` | Volume printed during the life and the shadow's share of it. |
| `flag_impact`, `flag_queue`, `flag_volume`, `material` | The flags. |

Rows are sorted by `shadow_id`, and the same inputs give byte-identical files.

## On real data

Nasdaq TotalView-ITCH, 2019-12-30, AAPL, MSFT, SPY and TSLA, venue daily figures:

| Instrument | Venue session volume | `sigma_daily` |
|---|---|---|
| AAPL | 5,947,543 | 1.35% |
| MSFT | 3,845,915 | 0.84% |
| SPY | 6,936,342 | 0.48% |
| TSLA | 2,956,454 | 3.14% |

**Sampled shadows.** The 3,000 shadows of the day-one zoo run `d1-sampled-corrected` (sizes 1 and 100; at the touch,
one or two ticks behind it or one tick inside; life = the 300 s evaluation window). The 121 one-tick-inside shadows
rejected as would-cross are not assessed, which leaves 2,879:

| Figure | p50 | p90 | p99 | max |
|---|---|---|---|---|
| `impact_bps` | 0.034 | 1.83 | 1.83 | 1.83 |
| `impact_ticks` | 0.14 | 7.56 | 7.67 | 7.68 |
| `queue_share_at_live` | 0.083 | 1.0 | 1.0 | 1.0 |
| `volume_share` | 0.00005 | 0.0040 | 0.0094 | 0.014 |

1,125 of 2,879 (39.1%) are flagged, all by queue share. No shadow reaches 5% of the volume, and no impact flag is set
because `V` is the venue's. Size decides it:

- One-lot shadows: 0 of 1,479 flagged. 232 are at least 10% of their level and 172 are alone at their price, but
  they are odd lots. Their largest impact estimate is 0.14 tick.
- Hundred-lot shadows: 1,125 of 1,400 flagged. By instrument: AAPL 368 of 373 (median share at live 0.43), TSLA 372
  of 377 (0.74), MSFT 221 of 310 (0.14), SPY 164 of 340 (0.091).
- 347 shadows are alone at their price: the 183 one-tick-inside shadows that went live (each would set a new best
  price) and 164 behind the touch at an empty price.

With venue volume only, 750 shadows reach a tick of impact (every hundred-lot AAPL and TSLA shadow). Treat that figure
as an upper bound, because it uses one venue's volume and `Y = 1` for a single resting order.

**Real orders.** The 3,000 real Nasdaq orders of the day-one replay, mirrored as shadows and replayed with their own
orders self-excluded (`exclude_order_ids`, exact fill rule: each shadow takes its twin's place in the queue and is
filled by the twin's executions). The orders are 1 to 8,070 shares, median 100. 99 went live outside the regular
session, which leaves 2,901 assessed:

| Figure | p50 | p90 | p99 | max |
|---|---|---|---|---|
| `impact_ticks` (venue volume) | 0.83 | 2.24 | 8.11 | 38.0 |
| `queue_share_at_live` | 0.33 | 1.0 | 1.0 | 1.0 |
| `volume_share` (1,411 with prints in their life) | 0.0 | 0.28 | 1.0 | 1.0 |

2,082 of 2,901 (71.8%) are flagged: 2,049 by queue share and 194 by volume share. 903 orders were alone at their
price when they arrived. Real orders on this venue are a large part of the level they join, so a strategy that posts
round lots in these names should expect the warning. Without self-exclusion, each mirror's twin is still in the book
and in the tape. The twin then doubles the level (no share at live above 0.5, none alone) and prints the mirror's
fills, which is why the earlier mirror figures were inflated.

**The volume caveat in numbers.** A `daily` table with each venue volume multiplied by a factor (hypothetical
consolidated volumes; no consolidated volume was available offline) sets `volume_source = user`, so the impact flag
applies. At factors 1, 3 and 5, the impact-flagged real orders are 1,326, 310 and 224, and the impact-flagged sampled
shadows are 750, 377 and 377 (at 3 and 5 all are TSLA hundred-lots). Nasdaq was one of many lit and off-exchange
venues in these names in 2019, so a factor of 3 is probably on the low side. The queue and volume flags do not
depend on `V`.

These figures are what `python -m queuetruth.eval.impact zoo` and `replay` print with default settings, run on the
day-one zoo outputs and on a self-excluded replay of the mirrors (`queuetruth replay ... --fill-rule exact
--exclude-order-ids`). The splits by size, instrument and offset filter the same `qt.shadow_impact` table.

## Limits

- The square-root law describes metaorders: a parent order executed in many pieces over minutes to days. A single
  resting order mostly affects the market by being seen, which the queue share covers better. Read the impact
  estimate as the order of magnitude if all of the order traded, not as a forecast.
- Hidden liquidity and other venues are not in the queue share. It is the share of the displayed queue on this
  venue.
- The flags say an assumption is doubtful. They do not re-run the comparison without the flagged shadows; the
  warning bounds how far a fill rate could move.
- Shadows of one run do not see each other, and the measures do not add them up. Two shadows at the same price each
  get their own share.
- Without `trades_source/` the dataset's trades are its MBO `T` records, which do not mark auction crosses, so crosses
  then count in the life volume and in the realized variance.

## References

- Almgren, R., Thum, C., Hauptmann, E. and Li, H. (2005). Direct estimation of equity market impact. *Risk* 18(7).
- Bouchaud, J.-P., Bonart, J., Donier, J. and Gould, M. (2018). *Trades, Quotes and Prices: Financial Markets Under
  the Microscope.* Cambridge University Press, chapter 12.
- Bucci, F., Benzaquen, M., Lillo, F. and Bouchaud, J.-P. (2019). Crossover from linear to square-root market impact.
  *Physical Review Letters* 122, 108302.
- Donier, J., Bonart, J., Mastromatteo, I. and Bouchaud, J.-P. (2015). A fully consistent, minimal model for
  non-linear market impact. *Quantitative Finance* 15(7).
- Gould, M. D. and Bonart, J. (2016). Queue imbalance as a one-tick-ahead price predictor in a limit order book.
  *Market Microstructure and Liquidity* 2(2).
- Huang, W., Lehalle, C.-A. and Rosenbaum, M. (2015). Simulating and analyzing order book data: the queue-reactive
  model. *Journal of the American Statistical Association* 110(509).
- Liu, L. Y., Patton, A. J. and Sheppard, K. (2015). Does anything beat 5-minute RV? A comparison of realized
  measures across multiple asset classes. *Journal of Econometrics* 187(1).
- Toth, B., Lemperiere, Y., Deremble, C., de Lataillade, J., Kockelkoren, J. and Bouchaud, J.-P. (2011). Anomalous
  price impact and the critical nature of liquidity in financial markets. *Physical Review X* 1, 021006.
- U.S. Securities and Exchange Commission (2005). Regulation NMS, Release No. 34-51808.
