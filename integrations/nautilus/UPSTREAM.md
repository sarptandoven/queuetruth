# Draft: a validated `prob_fill_on_limit` for NautilusTrader

Status: draft for discussion. Not opened upstream. Every number below comes from a run in this repository
(`repro/results/`, `docs/RESULTS.md` of queuetruth) and is reproducible from public Nasdaq data.

## Draft PR description

**Title:** docs + tests: what `prob_fill_on_limit` means, how to calibrate it, and a regression test for its
per-touch contract

**Summary**

NautilusTrader's probabilistic fill model (`FillModel` in 1.x, `DefaultFillModel` in 2.x) fills a passive limit
order touched by a trade print at its price with probability `prob_fill_on_limit`, default `1.0`. We measured what
that default does against an exact FIFO replay of the same Nasdaq order-level data, inside `BacktestEngine` itself,
and what a fitted value does on a held-out day. This PR proposes:

1. A documentation section ("Calibrating `prob_fill_on_limit`") under the backtesting concepts page: the per-touch
   meaning of the parameter, the bias of the default on real data, a closed-form estimator, and the caveats (the
   value is per print, so it does not transfer between feeds with different print rates).
2. A regression test pinning the per-touch contract that any calibrated value relies on: on an L2 book, with
   `queue_position=False` and `trade_execution=True`, (a) a print at the order's price fills it with probability
   `p`, independently per print; (b) a print through the order's price fills it with certainty; (c) `p = 0`
   never fills on an at-price print. The test is a seeded, synthetic, sub-second `BacktestEngine` run (no data
   files).
3. No new runtime code and no new dependency. Users who want a fitted value from exact data keep using the
   existing constructor; the external `queuetruth-nautilus` package builds it from a calibration file with its
   provenance and refuses fits that do not beat the default out of sample.

**Evidence**

Setup: Nasdaq TotalView-ITCH, AAPL, MSFT, SPY and TSLA on 2019-12-30 and 2020-01-30 (public sample files), 3,000
passive limit orders per day (1 or 100 shares, from 2 ticks behind the touch to 1 tick inside it), sampled over the
session and not cancelled within the 60 s scored. Each
order runs in its own window of `BacktestEngine`, fed the L2 (top-10, every event) view of the day as
`OrderBookDelta`s and `TradeTick`s; exact truth is a FIFO replay of the full order book with the order inserted at
the back of its level.

| config, held-out day 2020-01-30 (3,000 orders) | fill rate at 1 s | bias vs exact at 1 s, pp [95% CI] | bias at 10 s / 60 s, pp | Brier at 1 s, one run |
|---|---|---|---|---|
| exact FIFO replay | 18.50% | 0 | 0 / 0 | 0 |
| `prob_fill_on_limit=1.0` (default), 2.0.0rc7 | 21.30% | +2.80 [+2.27, +3.37] | +2.03 / +0.93 | 0.0280 |
| `prob_fill_on_limit=0.2866` fitted on 2019-12-30, 2.0.0rc7, mean of 8 seeds | 18.90% | +0.40 [+0.00, +0.79] | -0.37 / -0.43 | 0.0202 |
| default, 1.231.0 | 21.30% | +2.80 [+2.27, +3.37] | +2.03 / +0.93 | 0.0280 |
| fitted, 1.231.0, mean of 8 seeds | 18.83% | +0.33 [-0.07, +0.73] | -0.39 / -0.31 | 0.0214 |
| `queue_position=True`, `p = 1`, 1.231.0 | 18.40% | -0.10 [-0.23, +0.00] | -0.17 / -0.23 | 0.0010 |

Paired over orders (resampled jointly), the fitted value reduces the absolute bias of the default by +2.40
[+1.97, +2.85] pp at 1 s on 2.0.0rc7 and +2.47 [+2.02, +2.93] pp on 1.231.0, with the CI excluding zero at 0.1, 1
and 10 s on both lines. At 60 s it does not on 2.0.0rc7 (+0.51 [-0.01, +1.08] pp; 1.231.0: +0.62 [+0.10, +1.17]),
and the fitted model underfills there (-0.43 and -0.31 pp). Order by order, a single seeded run beats the default's
Brier score at 0.1 and 1 s only; at 10 and 60 s it is worse (significantly at 60 s on 2.0.0rc7, -0.0033
[-0.0068, -0.0002]), because `p` thins fills at random and so fixes the fill rate, not which orders fill. On the
fitting day the default overfills by +3.13 pp at 1 s (14.17% against an exact 11.03%), the same in 1.231.0 and
2.0.0rc7.

`queue_position=True` at `p = 1` is within 0.1 pp at 1 s on 1.231.0 (the row above, delta feed) with a Brier score
about 20 times lower than one fitted touch run's. queuetruth's 2.0.0rc7 figure (-0.03 pp at 1 s) is pending a
re-run, because the harness that produced it did not cancel orders at the end of their windows on 2.x (porting note
below), and with queue tracking resting orders share print volume. The touch model is the venue default
(`queue_position=False`), and it is the one whose parameter needs a number; the docs should still say that queue
tracking, where the data allows it, is the better fix.

**Estimator** (closed form, no simulation). For each training order record the touches (prints at its price)
before its exact first fill or before censoring. If the order's exact first fill happened on its `m_i`-th touch,
`d_i = 1`; otherwise `d_i = 0` with `M_i` touches observed. A first fill caused by a trade-through or a cross is
censored at that fill (those orders contribute only the touches before it); dropping them instead biases `p`
upward (0.300 for a true 0.200 in simulation).

```text
p_hat = sum d_i / sum [ d_i m_i + (1 - d_i) M_i ]
```

On 2019-12-30 (blocks 0-3 of 5, 1,536 orders, 3,604 touch events) this gives `p = 0.287`, bootstrap 95% CI
[0.269, 0.303] over 30-minute blocks.

**Caveats to document**

- `p` is per print. A feed that prints more often (or reports odd lots, or aggregates prints differently) needs
  its own fit. A value fitted on Nasdaq equities is not a crypto default.
- The touch model has no queue. `p` absorbs queue position on average; it cannot express that a deep queue fills
  later than a short one. `queue_position=True` does, and on these days it is closer to exact at every horizon.
- `p` is a random thinning: it corrects the aggregate fill rate, not which orders fill, so one run's per-order
  accuracy (and anything conditioned on fills, such as markouts) can be worse than `p = 1`. Seed every run, and
  average seeds for per-order analysis.
- Feed type: in 1.231.0 an `OrderBookDepth10` snapshot resets the queue position (from 2.0.0rc5, #4942, it no
  longer does), so the queue model on a depth-10 feed behaved like the touch model in 1.x.

## Design note

**Why docs and a test, not a new fill model.** The fitted model is the existing constructor with a different
number. Adding a `CalibratedFillModel` class upstream would duplicate `DefaultFillModel` and tie NautilusTrader to
one calibration format. What NautilusTrader can own cheaply is the contract that makes any calibration meaningful
(the per-touch semantics), and guidance that the default is an upper bound.

**The contract the test pins.** queuetruth verified, on real data, that NautilusTrader's touch model fills exactly
the orders a rule-set replica predicts: 100% order-level agreement at 0.1 / 1 / 10 / 60 s on both days in 1.231.0
and 2.0.0rc7 at `p = 1`, and realised fill rates within 0.5 pp (1.231.0) and 0.2 pp (2.0.0rc7) of the per-touch
expectation `1 - (1 - p)^touches` at `p = 0.29`. In the held-out run with the fitted `p = 0.2866` (8 seeds), every
order whose expectation is 0 or 1 (2,842 to 2,970 of 3,000, by horizon) filled exactly as expected in every seed on
both lines, and on the rest the seed-mean fill rate is within 0.08 pp of the expectation with a binomial spread. A change to how often the model draws (for example one draw per
book update instead of per print) would silently change what every calibrated `p` means; the proposed test catches
that. Its assertions are statistical with a fixed seed: with `N = 400` orders and one print each, the fill count
lies within 4 standard deviations of `N p`; with two prints, within 4 standard deviations of `N (1 - (1 - p)^2)`.
The same assertions run in `integrations/nautilus/tests/test_engine.py` of queuetruth against 1.231.0 and
2.0.0rc7 today.

**What the PR would add, concretely.**

- `docs/concepts/backtesting.md`: a "Calibrating `prob_fill_on_limit`" subsection (about 40 lines, the table and
  estimator above, the caveats).
- The three contract checks above in the fill-model unit tests (Rust or Python, wherever the maintainers prefer
  for 2.x), built on the existing test kit's instrument and book helpers.
- No change to defaults. Changing `prob_fill_on_limit`'s default is a behaviour change for every user and should
  be a separate discussion; the evidence here is for two days of four large US names.

**What stays outside NautilusTrader.** The exact replay, the fitting and the held-out scoring stay in queuetruth
(Apache-2.0); `queuetruth-nautilus` is a separate package that imports NautilusTrader's public API only (no
NautilusTrader source is copied, LGPL-3.0 respected). It maps a `qt.calibration_result` to the release line's names
(`FillModel` / `LatencyModel` in 1.x, `DefaultFillModel` / `StaticLatencyModel` in 2.x, `cancel_latency_nanos` in
both) and refuses a fit whose recorded verdict says it does not beat `p = 1` out of sample.

**Porting observations from 2.0.0rc7 (separate from this PR, worth an issue or a docs note).** While moving the
cross-check harness to 2.x we hit two silent behaviours: the order object returned by `order_factory.limit(...)`
stays `INITIALIZED` after `submit_order` (the cache holds the live order), and `Strategy.cancel_order` takes a
`ClientOrderId` where 1.x took the order; passing the order raises `TypeError`, which inside a
`clock.set_time_alert_ns` callback does not stop the run (with logging bypassed nothing surfaces). Code ported
from 1.x that cancels via the factory's object therefore never cancels. A line in the 2.0 migration notes would
save users the same hunt.

## Before opening

- Re-run `integrations/nautilus/repro/heldout.py` on the latest release candidate (or 2.0.0 final) and replace the
  table.
- Ask on the NautilusTrader Discussions or Discord whether the maintainers want the test in Rust or Python, and
  whether the docs section belongs in concepts or in a how-to.
- Keep the claims to what the two public days show.
