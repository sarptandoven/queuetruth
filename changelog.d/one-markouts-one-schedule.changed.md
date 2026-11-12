- **One markouts function and one schedule builder.** Every markout (the scorer, the toxicity evaluation, the
  prediction-market score, the sandbox and the shortfall decomposition) now comes from
  `queuetruth.eval.markouts.markouts`, with the price path, censoring at the end of data, the sign convention and
  the units as explicit settings. Every order schedule is built by `queuetruth.schemas.schedule` from one validated
  row type (`ScheduleLeg`, `ScheduleAmend`). Every published number is unchanged, bit for bit.
- **`read_schedule` checks its rows.** A schedule leg or amend with a missing column, a wrong type or a value out
  of range is now `QT-INPUT-002` naming the leg, before the replay program is written.
