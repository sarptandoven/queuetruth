- **One operation table and one runner.** Every operation (replay, zoo, calibrate, score and the others, and the
  verbs `check`, `fit`, `compare`, `data`, `atlas`, `demo`) is declared once as an `OperationDef` in
  `queuetruth.ops.table`, with its spec model, document model, what it touches and its cost.
  `queuetruth.runner.run(name, spec, out=..., context=...)` validates the spec, keeps paths inside the caller's
  roots, refuses the network unless allowed and returns the document, the answer line, the warnings and the files.
  The service's jobs and the MCP run tools call it, so each gives the same document as Python.
- **The verbs as MCP tools.** With `--allow-run`, `queuetruth mcp serve` also lists `qt_check`, `qt_fit`,
  `qt_compare`, `qt_data`, `qt_atlas` and `qt_demo`, generated from the table and run in the same sandbox as the
  other run tools; the first line of their text is the answer. A spec that needs the network is refused unless
  `QT_MCP_ALLOW_NETWORK=1`. Every existing tool keeps its name, arguments and schemas.
