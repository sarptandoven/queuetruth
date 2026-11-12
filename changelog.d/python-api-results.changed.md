- The results of `check`, `compare`, `fit` and `data add` print a one-line `repr`
  (`CheckResult('Your backtest filled ...')`) instead of the whole document, and `CheckResult` and `CompareResult`
  in a notebook show the compact view; `to_html()` is the full page as before. `CheckResult.table()`,
  `CompareResult.table()` and `DataResult.table()` now build the DataFrame through `queuetruth.frames` (nullable
  integer and boolean dtypes).
- `check` without data names the venue its touch tables come from as the atlas manifest names it (the published
  atlas still reads "IEX DEEP+"), instead of always saying IEX DEEP+; the `qt.check_estimate` document gains
  `atlas.venue_name`.
