# Findings: the replay-check flags of the 2020-01-30 full-market pilot

The local full-market pilot of 2020-01-30 (run `45c0dd05ddf70b67`, 130 units, 8,908 symbol-days, day root
`3b5e6590...`, `_core` sha256 `004ecd3b...`) flagged 4,845 crossed or locked events (4,826 with a negative spread)
and 397 price-priority violations in 7 of its 130 units, with 0 trade-parity mismatches. This page traces every flag
to its symbol, its ITCH messages and its cause. Short version:

- **8 symbols, all of them halted, paused or before their IPO cross.** ANPC and BDTX listed that day (IPO quotation
  period, then the IPO cross); RKDA and DTSS had LULD pauses with halt crosses; PNRL, DNJR, BRQS and IPDN were halted
  all day. Under ITCH semantics Nasdaq accepts and displays orders while a symbol is halted and matches nothing until
  the halt or IPO cross, so the displayed book legitimately locks and crosses. Every one of the 397 violations is a
  non-printable `C` execution inside a halt or IPO cross: the cross executes every order at one price at once and ITCH
  lists the executions one by one, in no price order.
- **The replay checks count correctly.** An independent Python rebuild of the book from the raw messages (`itch_ref.py`)
  and the engine agree on five per-symbol counters (events, executions checked, crossed, negative-spread and
  price-priority-violation events) for all 645 symbols of the 7 units, 8,619,686 events and 182,710 executions; a second
  trace with two more checks found no engine record that references an unknown order, and the ingest's count of
  unknown-order messages equal to the reference's, in all 7 units. The rebuild is independent code that follows the
  ingest mapper's semantics (`crates/ingest/src/itch/mapper.rs`); it compares counts, not book states. No test issue
  among the 8, no order-book rebuild problem, no replay-check defect.
- **The shadows' fill truth is not exact while a symbol is halted (F2, an engine defect).** The v0.1 contract censors
  shadows resting through a halt (`docs/design/spec.md` 1.2); the exact fill rule instead assumes continuous matching,
  so a shadow on a crossed halted book is filled at once. 1,125 of the 8,000 shadows of ANPC, BDTX, RKDA and DTSS
  (14.1%) were first filled while their symbol was not trading. A halt needs no crossed book to do harm, and the replay
  checks flag only halts whose display crossed: on 2020-01-30 every symbol of the universe that was out of state T in
  the session was also flagged or had no shadows, but nothing makes that true on other days. Reproduction:
  `repro_halt_shadow_fill.py`; the atlas labels these symbol-days by trading state (`trading_ok`). **Fixed after
  0.2.0:** ingest records each instrument's non-`T` spans (`qt.data_quality.trading_halts`) and replay, `why`, the
  zoo (both model arms), calibration and features censor shadows there, so the reproduction exits 0
  (`docs/design/extensions/halt-censoring.md` H.6). Datasets ingested before the fix carry no spans: re-ingest them.
- **Test issues were in the universe.** Five Stock Directory test issues (Authenticity `T`) passed the prefix rule in
  the 7 re-streamed buckets alone; they had no executions and no shadows. `itch_split.py` now records test issues and
  `plan.py` excludes them (F3).

## How it was traced

1. The 7 flagged units' buckets were re-streamed from emi.nasdaq.com on 2026-10-24 with the pipeline's own split
   (`itch_split.py --date 01302020 --buckets 32 --symbols <the 1,899 symbols of buckets 4, 6, 11, 12, 15, 26, 30>`,
   the command `trace_flags.py --restream-command` prints): 804 s at 7.0 MB/s, every pinned stream check passed, and
   all 7 bucket files are byte-identical to the pilot's (file sha256, framed-content sha256, message counts and
   per-symbol counts). 1.18 GB, deleted afterwards. A second re-stream the same day
   (624 s, every pinned check passed, the same 7 bucket hashes, deleted afterwards) also recorded the day-wide
   trading state (`split_manifest.json` `trading_state`: 8,921 `H` messages for 8,906 symbols) and fed a second
   trace with the unknown-order checks (209 s): the same results and the same sidecar.
2. `trace_flags.py --plan plan.json --shards shards --split <re-streamed> --out trace` (239 s wall, 136 s user CPU
   on the M1 Pro): per unit it re-ingested and replayed the unit and reproduced every summable counter
   of the shard (7 of 7), replayed each symbol alone and checked that the per-symbol counters add up to the unit's
   (7 of 7 complete), rebuilt the book independently and compared it per symbol (645 of 645 agree), classified each
   flagged event by the symbol's trading state, and drew each flagged symbol's shadows again to count those
   submitted or filled while the symbol was not trading.
3. `atlas.py reduce-day --symbol-flags trace/symbol_flags.json` (later with `--trading-state` too, see "Atlas
   outputs") on the unchanged pilot shards: the day root is
   unchanged, `cells_all.parquet` is identical apart from the new `exact_ok` column, and the 8 symbols are listed in
   `day_manifest.json` `flagged_symbols` with attribution `symbol_flags` (1,536 cell rows of 2,945,280: the 4
   symbols with shadows x 384 cells).

`01302020-symbol_flags.json` is the trace's sidecar (counts and cause names only): pass it to `atlas.py reduce-day
--symbol-flags` to label the pilot's shards, which predate per-symbol attribution. The per-unit trace files hold
message positions, timestamps and prices from the Nasdaq file and stay outside the repository
(`docs/data-and-licensing.md`); the numbers below are counts and times only. To check a row, re-run the two
commands above; `itch_ref.scan` reports each episode's first and last message by position in the bucket file
(`idx`) and by ingest `sequence`.

## Every flagged unit, per symbol

Times are New York, from the symbol's `H` (Stock Trading Action) messages. "Not trading" is any state other than T.
"Crossed" counts locked events too (best bid >= best ask at event end); the negative-spread count is in brackets
when it differs.

| unit | symbol | tier | crossed | violations | not trading (state, reason) | crossed episodes | cause |
|---|---|---|---|---|---|---|---|
| b004-u0002 | ANPC | T4 | 261 | 39 | 03:07-13:54:06 (H; Q IPOQ from 13:30) | 1: 12:35:01-13:54:06, 261 events, 54 after the resumption | IPO quotation period; violations in the IPO cross (`Q` type H, 13:54:06) |
| b006-u0001 | RKDA | T3 | 857 | 50 | 10:00:52-10:10:52 (P LUDP) | 2: 82 and 775 events, 54 after the resumption | LULD pause; violations in the halt cross (10:10:52) |
| b011-u0003 | PNRL | T5 | 130 | 0 | all day (H) | 1: 08:01:09 to the end of the day | halted all day |
| b012-u0001 | BDTX | T2 | 1,363 | 141 | 04:00-11:08:16 (H; Q IPOQ from 10:10) | 1: 09:19:53-11:08:16, 1,363 events, 148 after the resumption | IPO quotation period; violations in the IPO cross (11:08:16) |
| b012-u0001 | DTSS | T3 | 2,200 (2,181) | 167 | 09:42:29-09:52:29 and 09:52:53-09:57:53 (P LUDP twice) | 2: 1,297 and 903 events, 92 and 110 after the resumptions | two LULD pauses; violations in the two halt crosses |
| b015-u0003 | DNJR | T5 | 9 | 0 | all day (H) | 1: 09:30:00-16:00:00, 9 events | halted all day |
| b026-u0003 | BRQS | T5 | 1 | 0 | all day (H) | 1: 12:06:02-14:26:52, 1 event | halted all day |
| b030-u0003 | IPDN | T5 | 24 | 0 | all day (H) | 1: 09:30:01-16:00:00, 24 events | halted all day |
| | **total** | | **4,845 (4,826)** | **397** | | | |

Every other symbol of the 7 units has no flag (the attribution is complete: the per-symbol counters add up to the
unit's). The 19 locked events without a negative spread are DTSS's. ANPC and BDTX carry the IPO flag `Y` in the
Stock Directory; the four all-day halts have no executions, so the planner gave them no shadows and they have no atlas
cells.

**Why some crossed events fall after the resumption.** Nasdaq disseminates a halt or IPO cross as the `Q` print, the `H`
T resumption and the cross's non-printable `C` executions, which continue after the resumption (ANPC: the `Q` print and
the `H` T at 13:54:06, and the book uncrossed at a non-printable cross execution within the same second). Until the last
of them the displayed book is still crossed, so these events belong to the halt: a crossed episode keeps the cause of
the event that crossed the book (`itch_ref.py`), and `events_in_state_T` counts them (54, 54, 148, 92 and 110 above).

## Causes ruled out

- **Test symbols.** None of the 8 is a test issue (all have Authenticity `P`). See F3 for the test issues that are
  in the universe without flags.
- **Order-book rebuild.** The other six book invariants (unknown order references, fills of unknown orders,
  duplicate adds, unexpected modifies, over-executions, over-cancels) and unknown-order executions are zero in all 7
  units, trade parity has zero mismatches, missing or extra trades, and the independent rebuild agrees with the
  engine on every symbol's event and execution counts.
- **Engine bugs in the replay checks.** The counters are right as defined: the book at event end is crossed, and
  the cross executes an order behind a better-priced one on the same side. What the counters cannot see is the
  trading state (F1).

## F1 (by design, documented): the replay has no trading state

The ITCH mapper drops `H`, `K`, `J` and `h` messages (`crates/ingest/src/itch/mapper.rs`), so
`crossed_books_after_last`, `negative_spreads` and the rank oracle's `price_priority_violations` count halt periods and
crosses like continuous trading. On this day that is all of them. Proposal (`docs/design/extensions/atlas-trace.md`):
carry the trading state per instrument and report these counters separately for non-T spans and crosses.

## F2 (engine defect, failing reproduction): shadows are filled while a symbol is halted

**The contract.** `docs/design/spec.md` 1.2 (non-goals for v0.1): "No auction modelling: shadows resting through an
auction or halt are censored (`end_state = censored_gap`)". `docs/concepts/exact-truth.md` ("Where exactness
stops") and `docs/venue-semantics.md` ("Out of scope") tell users the same. The engine breaks it: the ITCH mapper
drops `H` messages, so the replay has no trading state and the exact fill rule matches shadows continuously through
halts, LULD pauses and IPO quotation periods. Until the fix lands, those two pages overstate what the replay does
and need a caveat (the integrator owns those edits).

**Reproduction.** `repro_halt_shadow_fill.py` builds a 15-message ITCH day: a symbol halts, a sell order arrives
below a resting shadow buy and is cancelled before the resumption, and a second shadow buy arrives during the halt
above the displayed asks. Expected under the contract: the first shadow is not filled and ends `censored_gap` at
the halt; the second is never live, is not filled and ends `censored_gap`. Actual: both are filled during the halt,
at 10:02:00 (`crossed`, maker, 100 @ 10.00) and 10:02:30 (`crossed_at_live`, taker, 100 @ 9.99), and end
`filled`. The book replay of that day has no flag at all: only the shadows' truth is wrong, so replay-check flags
cannot catch F2. The script exits 1 while the defect exists. The strict xfail
`test_trace_flags.py::test_halt_shadow_fill_reproduction` (in `scripts/scale/tests/`) runs it and fails outright on
a setup error, so the fix will show up as an unexpected pass once that test runs: `scripts/scale/tests` is outside
the pytest `testpaths` until it moves to `python/tests/scale/` (a required integration step; until then run `pytest
scripts/scale/tests`).
Taking part in the halt or IPO cross is not part of the v0.1 contract; it is a possible v0.2 extension.

**Status: fixed in the engine, not yet reachable from ingest.** Given the non-T spans (`qt_replay::TradingHalt`,
`Engine::set_trading_halts`, `ReplayOptions::trading_halts`), the engine ends `S1` `censored_gap` at the halt and
`S2` `censored_gap` at its `ts_live`, never live, with no fill: `crates/replay/tests/halts.rs`
(`f2_reproduction_itch_day_halt_censors_shadows`) runs this same ITCH day through `qt_ingest` and `run_dataset_with`
(`docs/design/extensions/halt-censoring.md`). Ingest still drops `H`, so no dataset carries the spans and the
Python API cannot pass them: this script still exits 1 and the strict xfail stays an expected failure until ingest
records the trading state and the Python layer passes it.

Effect on the pilot's replay-flagged symbols (shadows drawn again per symbol-day with the unit's seed and quotas,
replayed with the exact fill rule's defaults):

| symbol | shadows | submitted while not trading | submitted on a crossed book | filled | first filled while not trading |
|---|---|---|---|---|---|
| ANPC | 2,000 | 950 | 856 | 1,008 | 534 |
| BDTX | 2,000 | 518 | 518 | 1,518 | 465 |
| RKDA | 2,000 | 68 | 68 | 903 | 63 |
| DTSS | 2,000 | 68 | 68 | 1,025 | 63 |
| **total** | **8,000** | **1,604** | **1,510** | **4,454** | **1,125** |

All 1,125 are `crossed` or `crossed_at_live` fills.

**Halts the replay checks do not flag.** A halt flags the replay checks only when the displayed book crosses during it;
the reproduction above is a halt that does not. Measured on the pilot from the day-wide trading state: 10 symbols of the
universe were out of state T at some time in the regular session (9 spans in state H, 2 in Q with reason IPOQ, 3 in P
with reason LUDP). Eight are the replay-flagged symbols above; the other two, AHI and SVA, were halted all day, had no
executions and no shadows. So on 2020-01-30 there was no halted but unflagged symbol-day with shadows, and the exclusion
numbers below stand. That is a property of this day, not a rule: a LULD pause or a news halt whose display never crosses
leaves no replay flag, which is why the atlas now labels trading state directly.

**The label.** `itch_split.py` now records every symbol's non-T spans from the day's `H` messages, `plan.py`
carries them, and `atlas.py reduce-day` marks a symbol-day `trading_ok = false` when one overlaps the regular
session; `exact_ok` requires both `replay_ok` and `trading_ok`, so `atlas.py combine --flag-policy exclude` leaves
out the replay-flagged and the halted symbol-days alike. Publish atlas tables with `exclude` until the engine
carries the trading state; `local_day.py` and `modal_app.py` now default to it.

## F3 (fixed here): test issues passed the prefix rule

The Stock Directory marks test issues with Authenticity `T`. In the 7 re-streamed buckets (1,899 directory symbols)
six are marked: ZWZZT (already excluded by the `ZxZZT` prefixes) and ZEXIT, NTEST.B, ATEST.C, CTEST.O and CBO, which
were in the pilot's universe (tier T5, no executions, no shadows; CTEST.O had a directory entry only, the unit's one
`missing_symbols`). `itch_split.py` now lists every test issue of the day (`routing.test_issues`) and `plan.py`
excludes them with the reason `test issue (Stock Directory authenticity T)`. The other 25 buckets were not
re-streamed, so the day-wide count is not measured here.

## Atlas outputs

- New shards: a flagged unit's leaf has `exactness_by_symbol`. Re-running three pilot units with the new `unit.py`
  (b026-u0003 and b011-u0003, flagged; b004-u0003, exact): the flagged leaves equal the pilot's apart from the new key,
  which names BRQS and PNRL with the trace's counts (the attribution took 1.1 s and 2.3 s for 107 and 112 symbols),
  and the exact unit's leaf hash is unchanged (`4ac8db49...`). Flagged units' leaf hashes, and any day root holding
  one, change; `verify_unit.py` accepts the pre-attribution form.
- Labels: `atlas.py reduce-day --symbol-flags 01302020-symbol_flags.json --trading-state <split_manifest.json
  of the second re-stream>` on the unchanged pilot shards (86 s): the day root is unchanged (`3b5e6590...`), 10
  symbol-days are not `exact_ok` (the 8 replay-flagged, plus AHI and SVA with `trading_ok` false only), and 1,536
  of 2,945,280 cell rows are not `exact_ok` (the 4 symbols with shadows x 384 cells), the same rows as with replay
  flags alone. `atlas.py combine` (`label`) on the earlier reduction gave the pilot's tables plus `n_flagged_days`
  and `flags` in `per_symbol.parquet` (e.g. ANPC `01302020: crossed_books_after_last=261, negative_spreads=261,
  price_priority_violations=39 (ipo_quoting_period)`; the new label also lists the non-T spans); `per_symbol` was
  otherwise identical, and the leaderboard and the maps equal the pilot's within 3.5e-16 and 4.4e-14 relative
  (summation order). The leaderboard's `flagged_share` columns were added after that run; computed from the cells,
  the example cell below has `flagged_share` 0.074% (1,985 of 2,696,642 shadows, 4 symbol-days).
- Exclusion, replay flags only (the first version of this work, kept for comparison): `--flag-policy exclude` dropped
  ANPC (T4), BDTX (T2), RKDA and DTSS (T3), for example 1,985 of the 2,696,642 shadows of the ALL-tier
  deep-population cells (0.07%). Small in percentage points (at most 0.09 pp on the T2 fill-rate bias, 0.015 pp on
  ALL), but the leaderboard's intervals are tight: 237 of the 2,202 cells with an interval moved by more than half
  their 95% half-width, and 85 of 2,304 cells changed whether their interval excludes zero (76 became significant, 9
  stopped being so; 59 in T4, 21 in ALL, 5 in T2). Example: ALL tier, faithful, deep population, 1 s,
  `hbt_power3[exch=partial;mode=faithful;n=1]`: fill-rate bias -0.0132 pp [-0.0287, -0.0028] with the four
  symbol-days, -0.0024 pp [-0.0032, -0.0016] without them. Four halted symbol-days out of 8,908 carry 82% of that
  cell's bias and widen its interval sixteenfold.
- Exclusion with the trading state: the set of symbol-days with cells that are not `exact_ok` is the same
  as with replay flags alone (checked on the reduced cells), so `--flag-policy exclude` gives the tables measured
  above; it was not re-run.
