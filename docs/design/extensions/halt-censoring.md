# Trading halts censor shadows (fix of finding F2; SPEC 1.2)

Status: built end to end for Nasdaq ITCH and IEX DEEP+ (H.6). The engine side came first (`crates/replay/src/engine.rs`,
`crates/replay/src/shadow/mod.rs`, `crates/replay/tests/halts.rs`); ingest now records the trading state and every
engine entry point applies it. DBN, NYSE and the crypto sources record no spans yet (H.5). This note is the contract
the fix was built against, written to be merged into [the design specification](../spec.md) (sections 1.2 and
5.2.7).

## H.1 The defect

SPEC 1.2 (non-goals for v0.1): "No auction modelling: shadows resting through an auction or halt are censored
(`end_state = censored_gap`)". The engine had no trading state, so the exact fill rule matched shadows through
halts, LULD pauses and IPO quotation periods as if the venue were trading continuously. On the 2020-01-30
full-market pilot, 1,125 of the 8,000 shadows of ANPC, BDTX, RKDA and DTSS were first filled while the symbol was not
trading, all by `crossed` or `crossed_at_live` fills against a displayed book the venue did not match (finding F2,
[`scripts/scale/findings/README.md`](https://github.com/sarptandoven/queuetruth/blob/main/scripts/scale/findings/README.md); [the atlas trace
note](atlas-trace.md)). The book replay was right; only the shadows' fill truth was wrong, so no replay-check flag
catches it.

## H.2 The contract as built

Input: `qt_replay::TradingHalt {instrument_id, ts_start, ts_end}`, a span `[ts_start, ts_end)` in UTC ns in which the
instrument is not in continuous trading (Nasdaq ITCH: from an `H` message with state `H`, `P` or `Q` to the next
`H` with state `T`; `ts_end = i64::MAX` when trading does not resume). `Engine::set_trading_halts(&[TradingHalt])`
takes them (call before the first record; calls add up; overlapping spans merge; `ts_end <= ts_start` is
`QT-CONFIG-001`); `ReplayOptions::trading_halts` gives each partition engine the spans of its instrument
(`run_dataset_with`). Spans of instruments without shadows are ignored. Both fill rules, `exact` and `et`, follow:

1. **A shadow live when a halt starts** ends `censored_gap` at `ts_start`. As for feed gaps, the censoring happens
   at the start of the instrument's first event with `ts_event > ts_start`, so an event at `ts_start` itself still
   trades (on ITCH, messages with the halt message's timestamp may precede it in the feed), and its fills stand.
   Before the halt ends the shadows, every shadow's own work effective before `ts_start` is done in input order:
   a cancel or amend effective before `ts_start` still applies (`cancelled`, `amended_to_filled`), and one
   effective at `ts_start` loses to the halt, as it loses to a feed gap (agenda order: time, then gap, cancel).
2. **A shadow whose `ts_live` falls in `[ts_start, ts_end)`** never goes live and ends `censored_gap` at its
   `ts_live`, with no queue state (`ts_activated_event` and the `*_at_live` columns null). A cancel, or an amend to
   zero, effective at or before `ts_live` still ends it `cancelled` (5.2.1). The halt instant is halted and the
   resumption instant trades: `ts_live = ts_end` goes live normally.
3. **A shadow that went live before the halt but whose instrument had no event between its `ts_live` and the halt**
   is activated on the first event after the halt as before, on the book it went live on (no event changed it), so
   a `crossed_at_live` fill at its `ts_live` is a fill before the halt and stands; then it ends `censored_gap` at
   `ts_start`. Its `ts_activated_event` is later than its end time, as already happens for a shadow whose cancel
   comes before its activation event.
4. **Twins (A1).** A shadow that would be linked to an excluded order whose `A` / `M` record falls in a halt is
   linked and ends `censored_gap` at its `ts_live` without going live; the twin stays on the tape for everyone
   else, as it does when its shadow is cancelled.
5. **End of input.** A pending shadow whose `ts_live` falls in a halt ends `censored_gap` at its `ts_live`, not
   `open_at_end` or `censored_session_end`. A live shadow whose halt starts at or after the instrument's last event
   keeps the existing end (`censored_session_end` or `open_at_end` at that last event, which comes first).
6. **Everything else is unchanged.** Without spans no code path differs; with spans, a shadow live only before the
   halt or only from the resumption on has the same result, fills and trajectory as without them (H.3 property
   test), and feed gaps, clears and bad-book records censor as before.

Taking part in the halt or IPO cross at the cross price is not part of the v0.1 contract; it is a possible v0.2
extension.

## H.3 Evidence

`crates/replay/tests/halts.rs`, 9 tests, all failing before the fix (with `set_trading_halts` accepting and ignoring
the spans) and passing after it:

- `f2_reproduction_itch_day_halt_censors_shadows`: the Rust port of
  `scripts/scale/findings/repro_halt_shadow_fill.py`, the same 15-message ITCH day encoded by `qt_ingest::itch`,
  ingested by `qt_ingest::ingest` and replayed by `run_dataset_with`, with the spans derived from its `H` messages.
  Without the spans it reproduces the finding exactly (`S1` 100 @ 10.00 `crossed` at 10:02:00, `S2` 100 @ 9.99
  `crossed_at_live` at 10:02:30); with them there is no fill, `S1` (live, queue ahead 0) ends `censored_gap` at
  10:01:00 and `S2` ends `censored_gap` at its `ts_live` 10:02:30 without going live.
- One test per rule of H.2: the cancel before the halt and the tie at it (rule 1), `ts_live` at the halt instant,
  inside the halt and at the resumption instant (2), an event at the halt instant that still trades and a partial
  fill kept (1), activation after a halt without events (3), a twin added during a halt (4), a halt to the end of
  the input (5), and the empty-span configuration error.
- `only_the_halt_changes_anything_on_random_streams`: 72 random venue-consistent streams (CME, Nasdaq, generic;
  `exact` and `et`; 12 seeds each; 1,200 events, 60 abs and touch shadows with latency jitter, cancels and amends;
  a halt in the middle third) compared shadow by shadow with the replay without the halt: a shadow live from the
  resumption on or ended before the halt has the identical result and fills; a shadow with `ts_live` in the halt
  never goes live, has no fill, and ends `censored_gap` at its `ts_live` (or `cancelled`); a shadow live at the halt
  keeps exactly its fills up to the halt instant and ends `censored_gap` there (or as before when it ended at that
  very instant); no fill falls inside the halt; and spans of another instrument change no row. Every case occurs
  for every venue and rule.
- The existing suites pass unchanged: `cargo test --locked -p queuetruth-replay -p queuetruth-book` (including the
  receipt-priority tests `oracle_disagreements.rs`, `twins.rs`, O-SH-2 against the independent reference and O-SH-3),
  the workspace tests with `--features gate`, `cargo clippy -D warnings` and `cargo fmt --check` on the changed files.

## H.4 What changes in results

Nothing for any run that passes no spans: every public entry point today (Python `qt.replay`, the CLI, the service,
`why`, zoo, calibration, features) passes none (H.5). On the two public Nasdaq days the four symbols (AAPL, MSFT,
SPY, TSLA) have one `H` message each, state `T` before the open, so their spans are empty and their results cannot
change either. The real-data checks were rerun with the rebuilt extension; see H.4.1.

### H.4.1 Real-data rerun

`scripts/realdata/validate_et.py --only RD-1,RD-2,RD-5,RD-6,RD-6s,RD-11` (the prototype's CSV ingests of 2019-12-30
and 2020-01-30 under `.realdata`, as in the oracle-disagreements report), run once with the extension built before
the fix and once with the extension rebuilt from the fixed sources (`ws ext`), same machine, same day: every check
passes both times (RD-1, RD-1b, RD-2, RD-5, RD-6, RD-6s, RD-11). Of the 2,739 values in the two `results.json`
files, the 25 that differ are wall times, throughputs and the run's own output paths; every count (invariants,
rank oracle, BBO and trade parity, the 3,000 mirror shadows per day, RD-11's 3,000 of 3,000 matched orders per day)
is identical, and all 50 Parquet outputs the two runs wrote are byte-identical. No exactness result changes.
`repro_halt_shadow_fill.py` with the rebuilt extension still exits 1 (H.5).

## H.5 Open

- **Other sources.** The DBN, NYSE and crypto readers record no spans: DBN skips `status` records (and refuses
  `status` files), NYSE uses its security status only for the open time, and the crypto venues publish no
  per-instrument halt state in the captures read. Datasets from them replay as before. Feed positions of the state
  messages are not recorded either; the engine's tie rule (H.2 rule 1) decides the halt instant.
- **Halt crosses disseminated after the resumption.** Nasdaq sends the halt or IPO cross's executions after the
  `H` T message (54 to 148 crossed events per cross on 2020-01-30). A shadow that goes live in that window sees the
  still-crossed displayed book and can be filled `crossed_at_live`. The span ends at the resumption message, so this
  window is treated as trading.
- **Sampling.** `sample_shadows` still draws shadows inside halts; they now end `censored_gap` instead of being
  skipped or tagged.
- **Reference.** The independent reference of O-SH-2 (`qt_testkit::try_reference_shadows`) has no halts; the halt
  rules are checked by the tests above and the property against the engine's own unhalted replay.
- **F1.** The replay checks still count crossed events and price-priority violations inside halts and crosses
  together with continuous trading.

## H.6 Built: the trading state from ingest to every engine

- **Ingest** (`crates/ingest/src/halts.rs`, `HaltTracker`): an instrument is halted while its trading status or an
  operational halt says so; a span opens when it leaves continuous trading and closes at the message that returns
  it (a change between halted states, such as ITCH `H` to `Q` before an IPO cross, keeps the span and its first
  state). Nasdaq ITCH: `H` states `H`, `P`, `Q` and `h` of market `Q`; `T` closes (`K` and `W` only announce times
  and levels). IEX DEEP+: Trading Status `H`, `O`, `P` and Operational Halt `O`; `T` and `N` close. Only selected
  instruments are tracked, and a span always names an instrument of `instruments.parquet` (IEX writes rows for
  instruments with records only, so the spans of a symbol without records, halted all session, are dropped). A span
  still open at the end of an ITCH file ends at the next midnight of its day (ITCH
  locates are per day); IEX ids are shared across inputs, so an IEX span may run from one day file into the next and
  one still open ends at the end of the last message's session day.
- **Contract** (schema 1.2, additive): `qt.data_quality.trading_halts`, a list of
  `{instrument_id, ts_start, ts_end, state, reason}` (`state` e.g. `itch_H:P`, `iex_O:O`; `reason` the feed's code,
  e.g. `LUDP`, `IPO1`). Halts are venue state, not a data defect: they never change the verdict. The key is omitted
  when the list is empty, so a dataset without halts writes the same bytes as before.
- **Engines.** `qt_replay::halts_of` reads the spans and `qt_replay::partition_halts` gives each partition those of
  its instrument that overlap its records or its session day (so a span of another day never reaches a partition
  of a source with per-day ids). `run_dataset_with` applies the dataset's spans unless the caller passes its own in
  `ReplayOptions::trading_halts`, and adds one warning (`trading halts: N spans on M instruments applied ...`) when
  a run with shadows applies any; `why` and `run_partition` do the same, and the zoo
  (`StreamOptions::trading_halts`), calibration (`collect_events_with_spans`) and features
  (`extract_events_with_spans`) set them on their exact engines. The agent sandbox cuts them like feed gaps: a
  halt announced before the visible cut is in the scratch dataset with its end at most at the cut, a later one is
  not.
- **Zoo model arms.** The engine reports an end to its observers only for shadows that were live, so a shadow it
  censors at its `ts_live` inside a halt is invisible to the L2 and `hbt_l3_fifo` arms, which go live on their own
  clock. `PartitionSim::set_trading_halts` gives the arms the spans: each halt start is an administrative end
  (audit A2) that ends every live pair `censored_gap`, and a shadow whose `ts_live` falls in a halt never goes live
  in either arm (after the cancel and amend-to-zero checks, as in the engine).
- **Evidence.** `crates/replay/tests/halts.rs` `f2_reproduction_itch_day_halt_censors_shadows` now ingests the
  F2 day and checks that the recorded span equals the test's own reading of the `H` messages, that the default
  replay censors `S1` and `S2`, that a dataset without spans (as written by 0.2.0) still reproduces the defect, and
  that spans passed by the caller give the same outputs; `crates/ingest/src/itch/mapper.rs` and
  `crates/ingest/tests/iex_messages.rs` test the span rules of each source; `crates/zoo/tests/trading_halts.rs`
  checks that the zoo's exact arm equals replay, that no model books a fill during a halt or for a shadow submitted
  in one, and that without the span the upper bound does fill there. `repro_halt_shadow_fill.py` exits 0 and
  `scripts/scale/tests/test_trace_flags.py` asserts it (the strict xfail is gone).
- **What changes in results.** Nothing for a dataset without halts, and nothing for datasets ingested before the
  fix (their `data_quality.json` has no spans: re-ingest to apply them). On the two public Nasdaq days the four
  published symbols have no non-`T` span in the session (H.4), so the published tables are unchanged.
