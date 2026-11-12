- **`queuetruth --help` shows the six verbs first.** It lists `demo`, `atlas`, `check`, `fit`, `compare` and
  `data`, then the helpers `why` (also spelled `explain`), `report` and `verify`, then `advanced`. Every other
  command runs as `queuetruth advanced CMD ...` with the same output, exit code and JSON; `queuetruth help advanced`
  lists them. `queuetruth verify cert` is `verify-cert` as a `verify` subcommand. The CLI reference and the guides
  use the new spellings, and `queuetruth data add` where it does the job of `ingest`.
- **Operations moved into `queuetruth.ops`, one module per operation.** `queuetruth.api` re-exports every name it
  had, so `qt.<name>` and `queuetruth.api.<name>` keep working.
