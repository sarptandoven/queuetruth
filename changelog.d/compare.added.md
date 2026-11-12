- **`queuetruth compare --backtest B --live L [--data D]` and `qt.compare(...)`**: your backtest against your live
  run, order by order, in one command (docs/how-to/compare-backtest-and-live.md). It reads the backtester from the
  file (through the Backtester registry) and the live run's kind (a canonical live log, a bot's own database and
  log, or a venue export) from its records, with `--backtester` and `--venue` to say them, then runs the existing
  comparison: the reconciler for Freqtrade, Hummingbot, Backtrader, VectorBT and LEAN, and `score` with the
  simulator arm for hftbacktest and NautilusTrader; with `--data` also the markouts, the P&L gap per order and the
  exact arm. A venue export is imported first. No instrument map is needed for two logs of the same venue; with
  `--data` the live instruments are matched to the dataset's symbols and only a name that matches none is asked
  for. The answer comes first: how many of the backtest's fills happened live (with a 95% interval), the fills
  only one side made and why, the P&L gap, time to fill (Kaplan-Meier, clustered log-rank), markouts at 100 ms,
  1 s and 10 s, and the latency measured on the live log; then the orders that differ. It writes `orders.csv`
  (symbols, decimal prices, venue-local times), `compare.json` and, with `--html`, `compare.html`. `reconcile` and
  `score` are unchanged.
