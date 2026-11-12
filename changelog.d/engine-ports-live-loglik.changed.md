- **`calibrate-live` computes its likelihood in Rust.** The live-fill likelihood and its order-by-grid matrix run in
  `crates/calib` (`qt_calib::live`); `queuetruth.score.calibrate_live.interval_loglik` keeps its signature. Fitted
  parameters, intervals and `trust:` lines are unchanged; single likelihood values agree with the earlier NumPy
  code to within 2e-16 relative.
