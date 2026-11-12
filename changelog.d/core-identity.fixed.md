- **Numbers in Rust-written `trust:` lines use Python's `.6g` form exactly.** `calibrate` wrote `0.00001` for
  `1e-05`, `1000000` where rounding reaches `1e+06`, `0` for `-0` and `NaN` for `nan`; it now writes what Python
  writes.
