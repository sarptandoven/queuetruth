- **Every verb's result reads the same way** (plan section 4.2). `qt.check`, `qt.fit`, `qt.compare`, `qt.atlas.lookup`,
  `qt.atlas.calibration`, `qt.data.add`, `qt.data.fetch_iex` and the new `qt.demo(backtester=..., minutes="5min")`
  return results with `.summary` (the line the CLI prints first), `.table()` (a DataFrame in human units through
  `queuetruth.frames`; nested intervals become `x`, `x_ci_lo`, `x_ci_hi` columns), `.interval` (the interval behind
  the answer, in words), `.to_html()` (the CLI's page where it writes one), `.doc`, `.save(path)` (`.html`, `.csv`
  or the JSON document) and a compact notebook view (the answer, the interval and the head of the table). The shared
  part is `queuetruth.results.Result`; every existing attribute stays. `qt.check`, `qt.fit`, `qt.compare`, `qt.demo`,
  `qt.atlas.lookup` and `qt.atlas.calibration` show typed signatures (`inspect.signature`, the Python API
  reference), `qt.check(..., horizon="1s")` takes the P&L horizon and `qt.compare(..., window=...)` its window in any
  `queuetruth.units` form.
- **A notebook per framework** in `examples/notebooks/`: NautilusTrader, hftbacktest, Backtrader, LEAN, Freqtrade,
  VectorBT and backtesting.py, each showing its one line on synthetic data and the synthetic atlas, stored with
  outputs and an "Open in Colab" badge. A framework that is not installed shows its cell skipped with a message.
