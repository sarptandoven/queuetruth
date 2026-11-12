- **`ShadowSampleSpec.partition_quotas` keeps its key order in Rust.** The Rust mirror sorted the keys when it wrote
  the spec back; it now keeps them in the order given, as the schema and the Python model do.
