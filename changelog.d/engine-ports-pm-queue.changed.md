- **The prediction-market queue models run in Rust.** `queuetruth.pm.queue.Engine` calls `qt_zoo::pm::run` through
  `queuetruth._core`, and the family lists come from one registry, `qt_zoo::pm::PM_FAMILIES`
  (`queuetruth._core.pm_families()`). `Model`, `SimOrder`, `OrderResult` and `Engine` keep their names and
  signatures. Fills, end states and the `pm calibrate` and `pm score` documents are unchanged; queue estimates of the
  `power` and `log` families can differ in the last bit where NumPy used its AVX-512 kernels.
