- **`queuetruth advanced run OP --spec FILE` and `queuetruth-core run OP --spec FILE`.** Run any operation of the
  table from a JSON spec (a file, or `-` for standard input), with `--out DIR` for its outputs. The result document
  is printed as JSON; errors are `qt.error` documents with the usual exit codes. `run --list` prints the operations
  with their spec schemas. `queuetruth-core` runs ingest, replay, shadows, shadows_mirror and features without
  Python and checks every spec against its published JSON Schema; the other operations need the Python package.
