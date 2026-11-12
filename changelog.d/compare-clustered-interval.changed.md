- **`compare`'s share of the backtest's fills that happened live has a clustered interval.** Orders on the same
  instrument and day are correlated, so the 95% interval now resamples instrument-by-day clusters. With fewer than
  5 clusters it keeps the Wilson interval and the answer says why (`orders counted as independent: 2
  instrument-day clusters, fewer than 5`).
