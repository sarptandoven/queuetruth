- **`queuetruth compare --watch` and `--once`**: the comparison runs again when new live records arrive (a live
  directory or a growing file) and prints one line per check with the drift since the baseline window: the
  fill-share gap, the P&L gap per order and the 1 s markout gap, each with its 95% interval, and an `ALERT:` line
  when an interval excludes zero and the drift is at least `--alert-pp` (default 2), `--alert-ticks` (default
  0.25) or `--alert-pnl` (off by default). Exit codes as `fit`: 0 no alert, 1 alert, 2 error. The state is
  `<out>/watch.json`. No network and no notifications; the how-to shows cron and a CI schedule.
- **`compare` says what the gap is made of.** A new answer line runs McNemar's test on the discordant fills (the
  orders only one run filled), clustered by instrument and day (Obuchowski) when there are at least 5 such
  clusters, and the same line for the exact queue against live with `--data`. With `--data` and a measured
  latency, the latency is fitted before the queue (as `calibrate-live --latency-from-log`) and the answer says
  which share of the exact queue's gap to live the latency explains. `compare.json` gains `discordance`,
  `exact_discordance`, `latency_first` and `fills.interval`.
