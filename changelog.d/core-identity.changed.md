- **Run ids, seeds and the trust rule have one implementation, in Rust.** `queuetruth.schemas.runid.run_id`,
  `identity_document` and `drop_v1_1_defaults`, `queuetruth.stats.stable_seed` and `bootstrap_seed`, and
  `queuetruth.export.trust.annotate` call `queuetruth._core` instead of a Python copy. Names, signatures and bytes are
  unchanged; a spec holding an integer outside 64 bits now raises `ValueError` instead of hashing.
