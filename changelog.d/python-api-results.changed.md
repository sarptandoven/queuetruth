- The results of `check`, `fit` and `data add` print a one-line `repr` (`CheckResult('Your backtest filled ...')`)
  instead of the whole document, and `CheckResult` in a notebook shows the compact view; `to_html()` is the full
  page as before. `CheckResult.table()` and `DataResult.table()` now build the DataFrame through
  `queuetruth.frames` (nullable integer and boolean dtypes).
