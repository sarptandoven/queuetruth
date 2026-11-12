- **Rust reads documents that leave out optional fields.** 55 optional fields of the JSON documents (for example
  `qt.error` `hint`, `qt.job` `started_at`) were required by the Rust mirrors although the schemas allow them to be
  absent; they now read as `null`. Rust still writes every field.
