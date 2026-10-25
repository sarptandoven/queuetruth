"""Re-executes one certified run in a child process: `python -m queuetruth.cert.rerun` reads
`{"kind": ..., "spec": {...}, "out": "..."}` on stdin, runs the `queuetruth.api` function of that kind with the spec
and `out` (relative paths resolve against the process's working directory, which `verify-cert` sets to the base
directory), and prints the summary document (or a `qt.error` document) on stdout. Exit codes: 0 ran, 2 error.

A child process keeps the verifier's own state (working directory, imported modules, thread pools) out of the run.
`verify-cert` starts it as `python -P -m queuetruth.cert.rerun` with `PYTHONSAFEPATH=1`: the working directory (the
base, which for a stranger's certificate is their data bundle) is never put on `sys.path`, so a `json.py` or a
`queuetruth/` package placed there is not imported. The child reads the data files only through queuetruth's own
readers.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from queuetruth.cert.runs import API_FUNCTIONS
from queuetruth.errors import EXIT_ERROR, EXIT_OK, QtError


def run(kind: str, spec: dict[str, Any], out: str) -> dict[str, Any]:
    """Runs `kind` with `spec` writing to `out`; returns the summary document."""
    from queuetruth import api

    if kind not in API_FUNCTIONS:
        raise QtError("QT-INPUT-002", f"unknown run kind {kind!r}")
    fn = getattr(api, API_FUNCTIONS[kind])
    doc = fn({**spec, "out": out})
    return doc.model_dump(mode="json") if hasattr(doc, "model_dump") else dict(doc)


def main() -> int:
    """Entry point of the child process."""
    try:
        req = json.loads(sys.stdin.read())
        doc = run(str(req["kind"]), dict(req["spec"]), str(req["out"]))
    except QtError as e:
        print(json.dumps(e.to_document()))
        return EXIT_ERROR
    except Exception as e:  # any failure of the re-executed command is reported, never raised
        print(json.dumps(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}").to_document()))
        return EXIT_ERROR
    print(json.dumps(doc, allow_nan=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
