# Oracle disagreements on real data (extension of SPEC 5.2 and the oracle contract)

Status: built and tested (`crates/replay/src/shadow/`, `crates/replay/tests/oracle_disagreements.rs`,
`python/tests/oracle/qt_oracle/`, `python/tests/oracle/test_oracle_receipt_priority.py`,
`scripts/oracle/disagreements.py`). This note is the contract the fixes were built against and the evidence for
every classification, written to be merged into [the design specification](../spec.md) (section 5.2, the `exact`
rule, and the oracle section) and into section 3 of [the results](../../RESULTS.md).

## O.1 Summary

The independent matching-engine oracle was compared with queuetruth's default `exact` rule on four real runs, all
on the public Nasdaq TotalView-ITCH sample days (AAPL, MSFT, SPY, TSLA):

- raw ITCH bytes (`scripts/repro/reproduce.py`), 3,000 `qt.mirror_real_orders` shadows per day (seed 0), real cross
  prints (`--cross-window-s 0`): 8 disagreements on 2019-12-30 and 15 on 2020-01-30;
- the prototype's CSV conversion (`.realdata/ds/nasdaq-<date>`, source `etcsv`), the prototype's 3,000 mirror
  shadows per day, cross prints by a 1 s window (`--cross-window-s 1.0`, the CSV marks no crosses): 3 and 3 in the
  stored report (`.realdata/oracle/<date>`, build of 2026-10-18 21:30, oracle 0.1.1), 3 and 2 when rerun with
  queuetruth 0.1.1 and oracle 0.1.2.

All 29 shadows (23 + 6) are explained, order by order, in O.6. There were three root causes on the engines' side and
one convention of the comparison:

| Root cause | Who was wrong | Shadows | Fix |
|---|---|---|---|
| Receipt-order priority (D1, O1): orders Nasdaq holds until the open are released 0.27 s to 40 s after it with their original, smaller references, and keep their receipt priority | both engines (14), the oracle only (8) | 22 | queuetruth rule D1, oracle rule O1 |
| Hidden-print resting side (D2): a side-`N` print that continues a sweep of one side was offered to shadows on the other side | queuetruth | 3 | queuetruth rule D2 |
| Swept midpoint print (D3): a side-`N` print off the tick grid that continues a sweep was never used | queuetruth | 1 | queuetruth rule D3 |
| Opening-cross window: on the CSV, which marks no crosses, the oracle declines every side-`N` print in the first second; one 29 ms after MSFT's opening cross is a continuous hidden execution | neither engine (the comparison's setting) | 3 | none; a 0.85 s window agrees |

After the fixes the engines agree on 3,000 of 3,000 shadows on both raw-ITCH days (filled quantity 169,123 and
171,093, unchanged in total), on 3,000 of 3,000 on the CSV of 2020-01-30 and on 2,997 of 3,000 on the CSV of
2019-12-30, the 3 being the cross-window convention (3,000 of 3,000 with a 0.85 s window). The oracle's fork never
diverges from the tape any more on any of the four runs (O.5).

Scope of the changes. All three queuetruth rules apply only under the `exact` rule to instruments whose order ids
are Nasdaq TotalView-ITCH references: `Instrument.venue` Nasdaq and `Instrument.source` `itch`, `etcsv` or `dbn`
(`order_ref_priority`; a `dbn` dataset with the `nasdaq` rule set is taken to be XNAS.ITCH). D2 and D3 additionally
need hidden prints to be used (`rules.supports_hidden_orders` and a hidden-print policy other than `ignore`, the
Nasdaq default). Other venues, IEX DEEP+ data (rule set `iex`, source `iex`), Nasdaq-rules datasets from `parquet`
or synthetic sources, and the `et` rule are unchanged, including the shadow-side inference of hidden prints. The
rank oracle, trade parity, BBO parity, the prototype mirror-shadow checks and the headline experiment are unchanged
(O.7).

## O.2 Method

1. Reproduce. `reproduce.py` ingests the two public ITCH subsets, samples the shadows with `qt.mirror_real_orders`
   and runs `scripts/oracle/run_oracle.py --run-qt --cross-window-s 0`. The baseline (oracle 0.1.2, queuetruth
   0.1.1) gives 2,992 / 3,000 and 2,985 / 3,000, the numbers in section 3 of the results. The CSV runs repeat the
   stored configuration (`oracle_summary.json`: the dataset, the prototype's `shadows.csv`, `--cross-window-s 1.0`).
2. Evidence per shadow. `scripts/oracle/disagreements.py classify` walks the tape of each disagreeing shadow from
   activation to one second after its divergence and records four kinds of evidence (O.6). `priority` measures the
   venue fact behind D1 on every instrument (O.3); `bound` measures the receipt bound of D1 (O.4).
3. Attribution. Each engine was fixed separately, and `disagreements.py matrix` compares {old, fixed} oracle
   results with {0.1.1, fixed} queuetruth outputs. A shadow that agrees under (fixed oracle, 0.1.1 queuetruth) was an
   oracle bug; under (old oracle, fixed queuetruth) a queuetruth bug; only under (fixed, fixed) a bug both had.
4. Independent check. Fixing both engines with the same rule could make them agree by construction, so the ranking
   of real orders is checked against the tape itself: the oracle notes `orders_ahead_differ_from_tape` and
   `taker_volume_unmatched_in_fork` whenever the venue executes an order that is not the front of the fork's queue.
   Under the fixed rule that never happens on any of the four runs. This checks where released orders rank among
   real orders; where the shadow ranks (its receipt bound, O.4) is a shared convention that the tape cannot check,
   and is measured separately.

| Run | oracle old + queuetruth 0.1.1 | oracle old + queuetruth fixed | oracle fixed + queuetruth 0.1.1 | both fixed |
|---|---|---|---|---|
| raw ITCH, 2019-12-30 (oracle old: 0.1.2) | 8 | 32 | 32 | 0 |
| raw ITCH, 2020-01-30 | 15 | 41 | 39 | 0 |
| CSV, 2019-12-30 | 3 | 12 | 12 | 3 |
| CSV, 2020-01-30 | 2 | 13 | 11 | 0 |

The numbers are disagreements out of 3,000. The CSV "0.1.1" arm is the main checkout's extension; its baseline
differs from the stored report on 2020-01-30 (2 against 3) because the stored report was made with the build of
2026-10-18 21:30 and oracle 0.1.1 (O.6).

## O.3 The venue fact: receipt-order priority of released orders

ITCH order reference numbers increase in receipt order within an instrument, but they are not one feed-wide counter.
On both days the four instruments draw from three counters: SPY and TSLA share one, AAPL and MSFT each have their
own. After the first minute, every AAPL and MSFT `A` (767,508 and 607,327 on 2019-12-30; 1,020,484 and 922,736 on
2020-01-30) carries a reference below the largest reference already added on the four instruments together, by a
median of 19 million and 44 million references, while SPY and TSLA merged in time never go below their running
maximum (`disagreements.py bound`, `counter_groups`). So references are compared only within an instrument.

Within an instrument, every `A` record whose reference is below the largest reference already added is in the first
40.02 s after the open (none before it, none after), and on AAPL, MSFT and TSLA 55 to 74 percent of them land at a
level already holding a larger reference, where appending them at the tail puts them behind orders Nasdaq ranks
after them (`disagreements.py priority`):

| Day | Instrument | `A` out of reference order | at a level with a larger reference | seconds after the open |
|---|---|---|---|---|
| 2019-12-30 | AAPL | 21,094 | 11,628 | 0.78 to 39.74 |
| 2019-12-30 | MSFT | 18,712 | 10,667 | 0.83 to 39.60 |
| 2019-12-30 | SPY | 40 | 6 | 0.27 to 0.79 |
| 2019-12-30 | TSLA | 12,527 | 8,834 | 0.48 to 39.02 |
| 2020-01-30 | AAPL | 24,126 | 13,409 | 0.49 to 40.02 |
| 2020-01-30 | MSFT | 20,805 | 12,583 | 0.28 to 39.90 |
| 2020-01-30 | SPY | 40 | 3 | 1.17 to 3.96 |
| 2020-01-30 | TSLA | 20,168 | 14,931 | 0.97 to 39.78 |

The matching engine executes those released orders by reference. Of the executions of a displayed order that is not
the front of its level in arrival order during the first 60 s, the executed order is the front in reference order in
608 of 617 on 2019-12-30 (AAPL 170 / 170, MSFT 345 / 351, SPY 0 / 3, TSLA 93 / 93) and 500 of 552 on 2020-01-30
(AAPL 83 / 83, MSFT 137 / 178, SPY 0 / 10, TSLA 280 / 281). Before the open no execution hits a non-front order in
arrival order on 2019-12-30, and one (SPY) does on 2020-01-30. After the first minute most out-of-order executions
on AAPL and TSLA are still in reference order (released orders resting longer), while SPY's (1,343 and 1,220 a day,
none in reference order) and many of MSFT's (730 of 1,238 and 72 of 555 in reference order) are not: a different
phenomenon, which none of the disagreements depends on.

So the rule is not that Nasdaq ranks a level by reference; it is that orders held until the open keep their receipt
(reference) priority when released. This is consistent with Nasdaq accepting orders before the open that are
eligible only for the regular session and entering them on the book after the opening cross; whatever the reason,
the feed itself shows the priority. The behaviour is specific to Nasdaq's own reference numbering, so the rules below
are scoped to it.

## O.4 Rules

**D1 (queuetruth, `exact` rule, Nasdaq ITCH data).** `order_ref_priority(inst)` is true when
`inst.venue == Nasdaq` and `inst.source` is `itch`, `etcsv` or `dbn` (sources whose order ids are ITCH order
references; for `dbn` the dataset is taken to be XNAS.ITCH, so other Databento MBO feeds must not be ingested with
the `nasdaq` rule set). At activation a shadow records `prio_ref`, the largest order reference added on its
instrument (in its partition) before it went live. A later `A` (not a duplicate add, not a priority-losing modify)
at the shadow's price with a reference below `prio_ref` was received before the shadow: it joins the shadow's exact
queue ahead (`exact_qa`, `ahead_set`) and its cancels and executions are queue-ahead changes, not volume behind. A
reference above `prio_ref` stays behind however late it arrives. The `why` tracer labels such an `A` as ahead. Other
venues, other sources, synthetic data and the `et` rule keep arrival order.

*Twin-linked shadows (ruling A1).* A shadow that enters at its twin's `A` (the user's own order, excluded from the
tape) was received at the twin's reference: `prio_ref` is the twin's reference, and resting orders with a larger
reference (received after the twin, displayed before it was released from the hold) leave its queue ahead. This is
exact on ITCH data, where a priority-losing replace takes a new reference. A shadow entering at a priority-keeping
`M` keeps the instrument bound.

*The receipt bound and its bias.* `prio_ref` is the largest reference the instrument has added, a lower bound on the
counter it shares with other symbols (O.3). An order received after the instrument's last `A` but before the shadow,
held, and released later, has a reference above `prio_ref` and ranks behind the shadow when Nasdaq ranks it ahead:
an optimistic bias (more fills, earlier). It is largest for early pre-market shadows on instruments with sparse
pre-market adds. A tighter bound needs the counter's other symbols, which a per-instrument partition replay does not
see and a subset dataset may not hold. Measured on the two raw-ITCH days (`disagreements.py bound`, against the
fixed-oracle runs): of the `A` records at a live shadow's level while it was live, 2,298 and 5,907 are below the
per-instrument bound (ranked ahead) and 0 on either day fall between it and the bound of the counter group within
the dataset (SPY with TSLA). The groups of AAPL and MSFT have no other member in the dataset, so the measurement
says nothing about the symbols outside it.

**D2 (queuetruth, hidden prints, Nasdaq ITCH data).** On `order_ref_priority` instruments, per resting side, an
execution sweep is open from an `F` on that side until another record changes that side (an `A` or `M` there, a `C`
there that is not the removal of an execution of the sweep, a clear) or the `ts_event` changes. A side-`N` print at
the `ts_event` of exactly one open sweep is the sweeping aggressor continuing into hidden interest on that side, so
it reaches only shadows on that side. With no open sweep, sweeps of both sides, or another instrument, the side is
inferred from the shadow as before (ruling A3d).

**D3 (queuetruth, hidden prints, Nasdaq ITCH data).** A side-`N` print that continues a sweep may be off the tick
grid (a midpoint execution the aggressor reached; its limit is at or beyond the print), so under `BehindAndThrough` it
trades through a shadow it passes. Without a sweep an off-grid print may be two hidden orders meeting at the midpoint
and is still never used.

D2 and D3 are scoped to ITCH data because that is where they were validated; on IEX DEEP+ (whose `T` messages become
side-`N` prints) and other sources they are not, so queuetruth keeps the 0.1.1 behaviour there and the oracle, which
takes the resting side from the open sweep everywhere, may differ from it on such prints.

**O1 (oracle 0.1.3).** `InstrumentMeta.order_ref_priority` (same condition as D1). The tape book and every fork key a
displayed level by `2 * reference` for an order entering the book and `2 * max_ref + 1` for an order re-entering
it (a duplicate add, a modify that loses priority); the shadow keys at `2 * max_ref + 1` with `max_ref` taken at
activation, or at `2 * twin` when it enters at its twin's `A`. So a released order with an older reference ranks
ahead of orders already resting, and anything re-entering after the shadow ranks behind it. The oracle already took
the resting side of a side-`N` print from the open sweep and needed no change for D2 and D3. `qt_oracle.LIMITS` states
the receipt bound and its bias.

## O.5 Independent check against the tape

| Run | Oracle | Shadows with an `oracle_divergence` note | `orders_ahead_differ_from_tape` notes | `taker_volume_unmatched_in_fork` notes |
|---|---|---|---|---|
| raw ITCH, 2019-12-30 | 0.1.2 | 63 | 594 | 43 |
| raw ITCH, 2019-12-30 | fixed | 0 | 0 | 0 |
| raw ITCH, 2020-01-30 | 0.1.2 | 70 | 2,113 | 128 |
| raw ITCH, 2020-01-30 | fixed | 0 | 0 | 0 |
| CSV, 2019-12-30 | 0.1.2 | 13 | 67 | 20 |
| CSV, 2019-12-30 | 0.1.3 | 0 | 0 | 0 |
| CSV, 2020-01-30 | 0.1.2 | 11 | 84 | 18 |
| CSV, 2020-01-30 | 0.1.3 | 0 | 0 | 0 |

The other notes (crosses, hidden prints, off-price executions) are the oracle's documented limits and stay.

## O.6 Classification, order by order

Times are seconds from the 09:30 open (negative: pre-market). "Report category" is the heuristic label of the
baseline discrepancy report; "Evidence" counts, between activation and divergence + 1 s, D1 releases at the
shadow's level with a reference below its bound, executions that hit the reference-order front of a level at or
better than its price while the arrival-order front was another order (O1, at / better), and side-`N` prints of a
sweep of the opposite side (D2) or off the grid on its own side (D3). "Fixed by" is the attribution of O.2.

Raw ITCH bytes, `qt.mirror_real_orders`:

| Day | Shadow | Order | Live | Divergence | Report category | Evidence | Root cause | Category | Fixed by |
|---|---|---|---|---|---|---|---|---|---|
| 12-30 | `r2806627` | MSFT ask 159.00 x 50 | -5362.1 | 1.50 | oracle divergence | D1 12, O1 16 / 0 | receipt-order priority | queuetruth bug and oracle bug | both |
| 12-30 | `r4768961` | AAPL bid 289.21 x 100 | -3448.2 | 13.86 | oracle divergence | D1 1, O1 1 / 8 | receipt-order priority | queuetruth bug and oracle bug | both |
| 12-30 | `r5164507` | MSFT bid 158.90 x 1 | 3.1 | 14.94 | oracle divergence | O1 27 / 0 | receipt-order priority (released before the shadow) | oracle bug | oracle |
| 12-30 | `r6107781` | AAPL bid 289.00 x 60 | -1080.8 | 38.43 | oracle divergence | D1 124, O1 135 / 16 | receipt-order priority | queuetruth bug and oracle bug | both |
| 12-30 | `r8403413` | AAPL bid 289.00 x 1 | 1.5 | 38.43 | oracle divergence | D1 134, O1 135 / 16 | receipt-order priority | queuetruth bug and oracle bug | both |
| 12-30 | `r110073796` | TSLA bid 413.00 x 500 | 5207.8 | 5225.22 | crosses | D2 1 | hidden-print resting side | queuetruth bug (hidden prints) | queuetruth |
| 12-30 | `r143222381` | AAPL ask 292.15 x 500 | 10825.4 | 14891.46 | oracle divergence | O1 2 / 38 | receipt-order priority (released before the shadow) | oracle bug | oracle |
| 12-30 | `r247161108` | TSLA ask 415.22 x 100 | 18447.3 | 18448.15 | crosses | D2 1 | hidden-print resting side | queuetruth bug (hidden prints) | queuetruth |
| 01-30 | `r8636124` | SPY bid 324.05 x 377 | -5496.9 | -5496.82 | crosses | D2 1 | hidden-print resting side | queuetruth bug (hidden prints) | queuetruth |
| 01-30 | `r9593112` | TSLA bid 625.00 x 2 | 19.0 | 32.26 | oracle divergence | D1 38, O1 140 / 51 | receipt-order priority | queuetruth bug and oracle bug | both |
| 01-30 | `r15271952` | TSLA bid 624.67 x 25 | -323.3 | 32.40 | crosses | O1 0 / 253 | receipt-order priority at a better level (phantom liquidity in the fork) | oracle bug | oracle |
| 01-30 | `r17257932` | TSLA bid 620.00 x 1 | 4.8 | 125.91 | oracle divergence | D1 192, O1 160 / 295 | receipt-order priority | queuetruth bug and oracle bug | both |
| 01-30 | `r8206704` | TSLA bid 620.00 x 16 | 26.2 | 125.92 | oracle divergence | D1 135, O1 160 / 209 | receipt-order priority | queuetruth bug and oracle bug | both |
| 01-30 | `r10212512` | TSLA ask 640.00 x 18 | 23.7 | 435.62 | oracle divergence | D1 97, O1 100 / 109 | receipt-order priority | queuetruth bug and oracle bug | both |
| 01-30 | `r11735044` | TSLA ask 640.00 x 100 | 24.3 | 435.62 | oracle divergence | D1 27, O1 100 / 109 | receipt-order priority | queuetruth bug and oracle bug | both |
| 01-30 | `r14756800` | TSLA ask 640.00 x 1 | 24.8 | 435.62 | oracle divergence | O1 100 / 109 | receipt-order priority (released before the shadow) | oracle bug | oracle |
| 01-30 | `r31324540` | TSLA ask 640.00 x 60 | 382.7 | 435.62 | oracle divergence | O1 100 / 64 | receipt-order priority (released before the shadow) | oracle bug | oracle |
| 01-30 | `r16840624` | TSLA ask 642.00 x 99 | 1.0 | 467.59 | oracle divergence | D1 24, O1 19 / 318 | receipt-order priority | queuetruth bug and oracle bug | both |
| 01-30 | `r45800228` | TSLA ask 648.00 x 1 | 885.7 | 2078.92 | oracle divergence | O1 14 / 115 | receipt-order priority (released before the shadow) | oracle bug | oracle |
| 01-30 | `r17243428` | TSLA ask 650.00 x 29 | 4.5 | 2489.11 | oracle divergence | D1 718, O1 374 / 580 | receipt-order priority | queuetruth bug and oracle bug | both |
| 01-30 | `r105372220` | TSLA ask 644.10 x 400 | 3308.4 | 3311.38 | hidden prints | D3 1 | swept midpoint print | queuetruth bug (hidden prints) | queuetruth |
| 01-30 | `r3176141` | AAPL bid 320.00 x 750 | 7.3 | 5131.85 | oracle divergence | D1 333, O1 269 / 2, D2 2 | receipt-order priority (and the D2 prints after it) | queuetruth bug and oracle bug | both |
| 01-30 | `r3540997` | AAPL bid 320.00 x 29 | 9.5 | 5131.85 | oracle divergence | D1 255, O1 269 / 2, D2 2 | receipt-order priority (and the D2 prints after it) | queuetruth bug and oracle bug | both |

Prototype CSV, the prototype's mirror shadows (the 6 of the stored report):

| Day | Shadow | Order | Live | Divergence | Report category | Evidence | Root cause | Category | Fixed by |
|---|---|---|---|---|---|---|---|---|---|
| 12-30 | `r4009027` | MSFT bid 158.97 x 1 | 0.8 | 0.86 | hidden prints | none | opening-cross window: a continuous hidden print 29 ms after the cross, declined by the 1 s window | comparison setting (queuetruth right) | none (0.85 s window agrees) |
| 12-30 | `r4009511` | MSFT bid 158.97 x 3 | 0.8 | 0.86 | hidden prints | none | as `r4009027` | comparison setting (queuetruth right) | none (0.85 s window agrees) |
| 12-30 | `r4009523` | MSFT bid 158.97 x 1 | 0.8 | 0.86 | hidden prints | none | as `r4009027` | comparison setting (queuetruth right) | none (0.85 s window agrees) |
| 01-30 | `r14052964` | TSLA bid 625.00 x 800 | 20.0 | 32.27 | oracle divergence | O1 140 / 20 | receipt-order priority (the instant of `r9593112`) | oracle bug | oracle |
| 01-30 | `r15450144` | TSLA bid 624.82 x 10 | -270.9 | 32.40 | oracle divergence | O1 0 / 253 | receipt-order priority at a better level (the instant of `r15271952`) | oracle bug | oracle |
| 01-30 | `r6691948` | TSLA ask 650.00 x 2 | -9267.4 | 2489.11 | oracle divergence | D1 19, O1 374 / 580 | receipt-order priority (the instant of `r17243428`) | queuetruth bug and oracle bug | both |

Totals: 14 both engines, 8 oracle only, 4 queuetruth only, 3 comparison setting. `r6691948` agrees under (oracle
0.1.2, queuetruth 0.1.1), where both engines used arrival order, and disagrees under both mixed arms, so both
engines changed it together; the stored report's disagreement came from the older build. Timestamp ties: 0 (both
engines apply the records of one `ts_event` in feed order; the same-timestamp executions of `r143222381` at
13:38:11.513 follow reference order, which O1 covers). Auction or opening-cross handling by either engine: 0; the
raw-ITCH divergences near the open are the release of held orders between 0.27 s and 40.02 s after it (O.3), and the
three CSV ones are the oracle's cross window, below.

What each kind looked like, with the real records (the regression tests replay them):

- **D1, `r4768961`.** The shadow joins AAPL's 289.21 bid behind order 4768961 at 08:32:31. Order 3957993
  (received before it) is released at 09:30:02.846 and executed first at 09:30:13.864334161. queuetruth 0.1.1 put it
  behind the shadow and filled the shadow 2 with 100 displayed shares still ahead; the oracle 0.1.2 matched the 2
  shares to 4768961 instead and diverged from the tape. Both now fill the shadow first at 09:30:13.868560963, by
  trade-through.
- **O1, `r143222381`.** An AAPL ask at 292.15 behind 5610853 (displayed before the open) and 4162377, 4667329 (older
  references, released at the open). Nasdaq executes 4162377 at 13:38:11.394, then 4667329 and 5610853 at
  13:38:11.513. queuetruth counted all three as ahead and was right; the 0.1.2 fork matched 5610853 first.
- **O1 at a better level, `r15271952`.** TSLA bid 624.67. A better bid level held an order displayed first and an
  older reference released later; the tape executes the older one, then the other is cancelled. In arrival order the
  fork had matched the first, so the released order stayed as phantom liquidity, and an ask that the tape shows
  resting at 624.67 traded with it instead of the shadow.
- **D2, `r247161108`.** TSLA ask 415.22. At 14:37:28.153975743 a seller hits the 1-share bid at 415.22 and, in the
  next message of the same timestamp, 99 hidden shares there (`T N 415.22 99`). queuetruth 0.1.1 inferred the hidden
  order's side from the shadow and filled the ask 86 with that seller's volume; the shadow fills its last 86 at
  14:37:35.460539122, when a buyer executes the order behind it. `r110073796` (a buyer's sweep reaching a bid) and
  `r8636124` (pre-market, 07:58) are the same.
- **D3, `r105372220`.** TSLA ask 644.10 with nothing ahead. At 15:25:11.383254342 a buyer lifts the displayed ask at
  644.27 (2) and, in the next message, 4 shares of a hidden ask at the midpoint 644.315. The buyer's limit was at
  least 644.315, through the shadow, so the shadow trades those 4 too; 0.1.1 skipped every print off the grid.
- **Cross window, `r4009027`, `r4009511`, `r4009523`.** MSFT bids at 158.97, live 0.83 s after the open. MSFT's
  opening cross prints at 09:30:00.829393767 (220,504 shares at 158.99; trade kind `cross` in the raw-ITCH ingest,
  `hidden` in the CSV, which carries no cross flag). The 946 displayed shares at 158.97 execute at 0.830413921, and
  at 09:30:00.858300935 a buyer takes 500 hidden shares at 158.97 in 32 prints, kind `hidden` in the raw-ITCH
  ingest too: a continuous execution. queuetruth uses it (`queue_depleted`); the oracle, which on the CSV can only
  recognise crosses by the 1 s window, declines it and fills the shadows 153 ms later, at 09:30:01.011678762. Rerun with
  `--cross-window-s 0.85` (after every opening and closing cross of that day, the last at 0.829 s), the engines agree
  on 3,000 of 3,000. The raw-ITCH runs use the real cross flags (`--cross-window-s 0`) and have no such case.

## O.7 Verification

- Rust: `crates/replay/tests/oracle_disagreements.rs`, 16 tests. The 4 `real_*` tests (`r4768961`, `r247161108`,
  `r110073796`, `r105372220`) and 5 rule-edge tests fail on the 0.1.1 sources and pass with the fix; 4 more (the
  `why` label, the two twin bounds, D2 and D3 off ITCH sources) fail with those parts of the fix disabled; 3 pin
  behaviour that must not change (other venues, the `et` rule, a sweep of the shadow's own side). `cargo test -p
  queuetruth-replay -p queuetruth-book` (including `twins.rs`), `cargo clippy --workspace --all-targets -- -D
  warnings` and `cargo fmt --check` pass.
- Python: `python/tests/oracle/test_oracle_receipt_priority.py` (O1, with the real sequences of `r4768961`,
  `r143222381` and the `r15271952` mechanism, and the twin bound), `test_disagreements_script.py`, and the whole
  oracle suite (173 unit and 40 integration tests).
- Oracle reruns with the final oracle (0.1.3) and the final queuetruth extension, same shadows: raw ITCH 3,000 /
  3,000 on both days (filled quantity 169,123 vs 169,123 and 171,093 vs 171,093; oracle end states 1,498 and 1,499
  filled, as before); CSV 2,997 / 3,000 and 3,000 / 3,000 (37,994 and 22,237 filled on both sides), and 3,000 /
  3,000 on the CSV of 2019-12-30 with a 0.85 s cross window.
- Exactness, with the final extension: `reproduce.py --steps subset,ingest,replay` (invariants, rank oracle, trade
  parity on both raw-bytes days) passes; `validate_et.py` RD-1, RD-1b, RD-2, RD-5, RD-6, RD-6s (rank oracle, BBO,
  trade parity, the prototype's mirror shadows on the CSV ingests) and RD-11 (the scorer with twin-linked shadows
  under the `exact` arm: 3,000 of 3,000 orders matched on both days, 250 / 2,750 / 0 / 0 on 2019-12-30 with
  self-exclusion) pass. The rank oracle checks the book's arrival-order FIFO bookkeeping, which is unchanged:
  D1 changes only the shadow's queue accounting. RD-9 and RD-10 (`reproduce.py --steps calib,hazard`) were not
  rerun.
- Headline experiment (`scripts/realdata/headline.py`, both days): every number identical before and after (its
  shadows go live 300 s after the open, after every release).
- Speed: a replay of the 3,000 mirror shadows on 2020-01-30 (10.65 million records) took 3.08 to 4.76 s (median
  4.10) with 0.1.1 and 3.32 to 4.92 s (median 3.76) with the fix, 12 runs each in alternating batches of 3, in Low
  Power Mode: no difference above the run-to-run noise. Off ITCH data the per-record tracking is skipped entirely.

## O.8 Consequences and follow-ups

- Results change for shadows on Nasdaq ITCH data that are live while held orders are released (the first 40 s after
  the open), for twin-linked shadows there, and for shadows reached by a side-`N` print continuing a sweep. Outputs
  carry `ENGINE_VERSION` (the workspace version), so the next release must bump it: the same `run_id` (SPEC 7.3)
  would otherwise name different outputs.
- Text to update at integration (the numbers come from O.1 and O.7):
  - `docs/RESULTS.md` section 3: the two `reproduce.py` rows become 3,000 / 3,000 with 0 disagreements; the two CSV
    rows become 2,997 / 3,000 (cross window 3; 3,000 with a 0.85 s window) and 3,000 / 3,000 on the final build;
    drop "5 of the 6 day-one oracle divergences are within 40 s of the open" and point to this page.
  - `README.md` line 69 and line 169, `docs/index.md` line 74, `docs/testing.md` lines 152 to 155: "2,992 and 2,985
    of 3,000" becomes "3,000 of 3,000 on both days"; "2,997 of 3,000 per day" (CSV) becomes "2,997 and 3,000 of
    3,000 (the 3: the oracle's 1 s cross window on a CSV without cross flags)".
  - `docs/RESULTS.md` line 10, "No copy of either is in this repository": the regression tests transcribe about 40
    real records (references, timestamps, prices, sizes) of the public ITCH days; the owner should confirm this
    under the data-licensing guardrails and amend the sentence ("apart from a few records transcribed in regression
    tests"), or the tests can be relabelled with values that keep the same ordering.
  - SPEC 5.2 (`exact` rule) and `docs/venue-semantics.md`: state D1 to D3 as written in O.4, with their scope.
  - `docs/mkdocs.yml`: this is the first page under `docs/design/extensions/`; add it to the nav or exclude the
    directory from the nav check.
- `crates/replay/src/real_orders.rs` (the port of the prototype's real-order lives) still records a released held
  order as joining the tail of its level, while the shadow engine ranks it by reference. Kept for parity with the
  prototype (RD-6); a follow-up can apply D1 to its queue-at-add when `order_ref_priority` holds.
- `dbn` with the `nasdaq` rule set is taken to be XNAS.ITCH. A follow-up should record the Databento dataset code at
  ingest and gate D1 on it, or emit a replay diagnostic with the count and time range of adds below the running
  maximum reference, so that misuse is visible.
- The receipt bound (O.4) is a per-instrument lower bound with an optimistic bias that these days do not exercise;
  tightening it needs the venue's counter groups.
- Not explained here, because no disagreement depends on them: the out-of-order executions after the first minute
  that reference order does not explain (O.3), mostly SPY and MSFT.

## O.9 Reproduce

With the public ITCH days under `.realdata/itch` and a replay output from `reproduce.py`. `disagreements.py` writes
one JSON file per call (`--out` is a file, unlike `run_oracle.py`, whose `--out` is a directory), with a provenance
block: each input with its sha256, the oracle version, the queuetruth version and the sha256 of its extension.

<!-- check-docs: skip (needs the public ITCH days, about 25 minutes per day of oracle) -->
```sh
# DS: the ingested dataset, SHADOWS: its shadow file, OUT: a new output directory,
# BASELINE: the earlier oracle run's output directory
DS=...; SHADOWS=...; OUT=...; BASELINE=...
uv run --frozen --no-sync python scripts/oracle/run_oracle.py --dataset "$DS" --shadows "$SHADOWS" \
    --run-qt --cross-window-s 0 --out "$OUT"
uv run --frozen --no-sync python scripts/oracle/disagreements.py priority --dataset "$DS" --out "$OUT"/priority.json
uv run --frozen --no-sync python scripts/oracle/disagreements.py bound --dataset "$DS" --results "$OUT" \
    --out "$OUT"/bound.json
uv run --frozen --no-sync python scripts/oracle/disagreements.py classify --dataset "$DS" --shadows "$SHADOWS" \
    --report "$BASELINE"/discrepancy_report.json --out "$OUT"/classify.json
uv run --frozen --no-sync python scripts/oracle/disagreements.py matrix --old-oracle "$BASELINE" \
    --new-oracle "$OUT" --old-qt "$BASELINE"/qt_replay --new-qt "$OUT"/qt_replay --out "$OUT"/matrix.json
```

The CSV runs use the stored configuration instead: `--dataset .realdata/ds/nasdaq-<date>`, the prototype's
`shadows.csv` named in `.realdata/oracle/<date>/oracle_summary.json`, and `--cross-window-s 1.0`.
