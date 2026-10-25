"""qt_oracle: an independent ground-truth oracle for queuetruth shadow orders (audit finding 11, ruling A11).

The oracle is a deliberately small price/display/time-priority matching engine written from the venue facts in
SPEC 3.2 and the audit rulings A1-A6, without reading `crates/book` or `crates/replay`. It reconstructs the taker
flow of an MBO tape from its executions, inserts each shadow as a real resting order into a private copy of the book
and re-matches that flow, so the shadow's fills come from matching, not from queue-accounting rules.

Modules:
- `venue`: the venue facts the engine needs (modify priority, hidden-order priority, fill/removal pairing).
- `tape`: records, event grouping, the tape book (what the feed says) and readers (qt datasets, et CSV, in-memory).
- `engine`: the counterfactual fork book with the shadow inserted, shadow lifecycle and the partition runner.
- `runner`: shadow / amend / own-order readers, partition routing, dataset and streaming et CSV runs.
- `compare`: normalisation of queuetruth / et / JSON results, comparison and the categorised discrepancy report.
- `scenarios`: a tiny DSL for hand-built tapes (SPEC 6.2 notation) and qt dataset / shadow writers.

The limits below are part of the oracle's contract; every discrepancy report repeats them.
"""

ORACLE_VERSION = "0.1.3"
# 0.1.0 was frozen before any comparison with queuetruth. 0.1.1 (after the first real-day comparison) treats a
# printed execution at a price better for the taker than the order's display price as trading at that price.
# 0.1.2 models ruling A1 per shadow: an excluded order stays on the tape for every shadow except its own (twin).
# 0.1.3 ranks a level by order reference on Nasdaq TotalView-ITCH data (receipt order: orders held until the open
# are released with their original references and execute first; docs/design/extensions/oracle-disagreements.md);
# a twin-linked shadow entering at its twin's A ranks at the twin's own reference.

LIMITS = (
    "Static counterfactual: the taker flow is fixed (each execution's size and time; the limit is the worst "
    "executed price of its sweep). Takers or makers that would have acted differently because the shadow was "
    "there are not modelled; resting-order adds, cancels and modifies are replayed as the feed shows them.",
    "Invisible taker volume: IOC remainders and marketable orders that executed nothing on the tape leave no "
    "trace, so a shadow that adds liquidity (improves the touch) may be under-filled; a remainder that rests on "
    "the book after its executions is visible (an add that is marketable only against the shadow) and is matched.",
    "Hidden liquidity is known only when it prints: a shadow that arrives marketable against unseen hidden "
    "orders, hidden residuals after a print and midpoint pegs that would re-peg around the shadow are not "
    "modelled. A side-N print strictly inside the tape spread on the tick grid has no observable resting side; "
    "by default its volume is offered to a shadow resting at or through its price (policy 'strict' declines).",
    "Prints that the tape itself shows could not have traded under display/price priority (displayed interest "
    "at or better than the print price still resting, or a locked opposite side) are not used, and neither are "
    "cross prints (Nasdaq: side-N prints within cross_window_s of the open/close, or trades_source kind=cross).",
    "Non-continuous states: an add or modify that locks or crosses the tape book rests unmatched, as on the "
    "tape; auctions/uncrossing are not modelled and executions during them are re-matched at resting prices.",
    "Venue facts are restated from SPEC 3.2 and rulings A3/A6, not re-derived from venue documentation. On CME "
    "an M is compared with the order's remaining size after same-event F records, so an M that raises the size "
    "above that remaining loses priority even when it is below the last displayed size (A6 as worded keeps it).",
    "Time priority is arrival order, or order-reference (receipt) order on Nasdaq TotalView-ITCH data (a dbn "
    "dataset with venue nasdaq is taken to be XNAS.ITCH). Tapes that execute out of that order make the fork "
    "diverge from the tape (reported as oracle_divergence); match_mode 'directed' keeps the tape's choice of maker "
    "and only adds the shadow's own priority. A shadow's receipt point is the largest reference added on its own "
    "instrument before it went live (a twin-linked shadow entering at its twin's A: the twin's reference), a lower "
    "bound on the reference counter the instrument shares with other symbols, so an order received after that "
    "reference but before the shadow and released after it from the pre-open hold ranks behind the shadow "
    "(optimistic); the tape check covers the ranking of real orders only.",
    "Lifecycle conventions are shared with SPEC 5.2 by design (activation at the first event with ts_event > "
    "ts_live, cancels/amends at the first event after their effective time, crossed-at-live fills stamped "
    "ts_live). Only latency_ns is applied (no LatencyModel components or jitter); there is no sequence-gap "
    "censoring (only R, MAYBE_BAD_BOOK and session end); race windows and dq_flags are not computed.",
    "Each shadow is evaluated alone (shadows never interact); the oracle is pure Python (minutes per ITCH day).",
    "Report attribution is heuristic: a disagreement takes the category of the oracle's notes at the first "
    "divergence (or the latest earlier note, flagged); et references carry only first fill and total quantity.",
)
