"""Time one `qt.replay()` call (SPEC 4.3) for the binding-overhead comparison with `queuetruth-core replay`.

Prints one JSON line: {"records", "call_s", "records_per_s", "import_s"}. The import is timed separately so the
comparison is call vs CLI process (the CLI process includes its own startup, which is the fair user-facing view;
SPEC 7.1 asks for "within 5%").
"""

from __future__ import annotations

import argparse
import json
import time

t0 = time.perf_counter()
import queuetruth as qt  # noqa: E402  API[py]

IMPORT_S = time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rank-oracle", action="store_true")
    a = ap.parse_args()
    # API[py] qt.replay(spec: ReplaySpec | dict) -> ReplaySummary (GIL released inside _core)
    spec = {"dataset": {"path": a.data}, "out": a.out, "rank_oracle": a.rank_oracle, "bbo": "change"}
    t = time.perf_counter()
    summ = qt.replay(spec)
    call_s = time.perf_counter() - t
    n = summ.stats.records
    print(json.dumps({"records": n, "call_s": call_s, "records_per_s": n / call_s, "import_s": IMPORT_S}))


if __name__ == "__main__":
    main()
