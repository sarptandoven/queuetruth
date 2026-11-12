- **Advanced commands at the top level.** `queuetruth ingest`, `replay`, `shadows`, `zoo`, `calibrate`, `score`,
  `reconcile`, `verify-cert` and the other commands under `advanced` still work at the top level and print one line
  on stderr after the command, naming the new spelling and the verb that does the job where one does (for example
  `zoo` and `shadows` -> `check`, `calibrate` and `export` -> `fit`, `ingest` and `dq` -> `data add`). `--quiet` or
  `QT_NO_DEPRECATION=1` hides it; help and `doctor`, `mcp` and `bench` never print it.
