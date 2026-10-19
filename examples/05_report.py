"""One HTML report from the outputs of examples 01 to 04.

Shows: `qt.report` (`ReportSpec`: `inputs` = output directories of replay, zoo, calibrate and score runs, `out` =
the HTML path) and the returned `qt.schemas.ReportSummary` (path, sections, size, sha256). The report is one
self-contained HTML file.

The inputs are read from `<outputs root>/01_replay_shadows/replay`, `02_zoo/zoo`, `03_calibrate_export/cal` and
`04_score_own_orders/score_exact`, `score_zoo`. When one is missing, this script first runs the example that writes
it (its own output is not printed), so it also works on its own.

Needs the compiled extension (`make develop` or the wheel). Synthetic data only: the numbers mean nothing about
real venues.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import time
from pathlib import Path

from _data import out_dir, out_root

import queuetruth as qt

#: (example module, output subdirectory) of every report input.
INPUTS = [
    ("01_replay_shadows", "replay"),
    ("02_zoo", "zoo"),
    ("03_calibrate_export", "cal"),
    ("04_score_own_orders", "score_exact"),
    ("04_score_own_orders", "score_zoo"),
]


def ensure_inputs() -> list[Path]:
    """The input directories, running the examples that write missing ones."""
    dirs = []
    for module, sub in INPUTS:
        p = out_root() / module / sub
        if not p.exists():
            print(f"{p} missing: running {module}.py first")
            t0 = time.perf_counter()
            with contextlib.redirect_stdout(io.StringIO()):
                importlib.import_module(module).main()
            print(f"  done in {time.perf_counter() - t0:.1f} s")
        dirs.append(p)
    return dirs


def main() -> None:
    """Renders the report and prints its summary."""
    inputs = ensure_inputs()
    out = out_dir(__file__)
    # `qt.api.report` is the same function as `qt.report`. Once `queuetruth.report` (the subpackage) has been
    # imported, the package attribute `qt.report` refers to that subpackage instead.
    r = qt.api.report(
        {
            "inputs": [str(p) for p in inputs],
            "out": str(out / "report.html"),
            "title": "queuetruth examples (synthetic data)",
            "max_why": 5,
        }
    )
    print(f"\nreport {r.out}")
    print(f"  {r.bytes} bytes, sha256 {r.sha256}")
    print(f"  inputs: {', '.join(r.inputs)}")
    print(f"  sections: {', '.join(r.sections)}")


if __name__ == "__main__":
    main()
