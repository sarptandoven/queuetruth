"""Timings of the trial ledger behind the Cost table of docs/concepts/trial-ledger.md.

Not collected by pytest (the file name does not start with `test_`). Run it from the repository root:

    PYTHONPATH=python python python/tests/ledger/bench_ledger.py [--quick] [--json OUT]

Every row is the median (and the 90th percentile) of repeated runs in a fresh temporary directory; recording is
forced on and nothing is written outside the temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

os.environ["QT_LEDGER"] = "on"

from queuetruth import ledger as L
from queuetruth.ledger import _capture as cap
from queuetruth.schemas import models as m


def _times(fn: Callable[[int], Any], n: int) -> tuple[float, float]:
    """(median, p90) wall time of `fn(i)` over `n` calls, in ms."""
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        fn(i)
        out.append((time.perf_counter() - t0) * 1e3)
    out.sort()
    return statistics.median(out), out[min(len(out) - 1, int(0.9 * len(out)))]


def _row(rows: list[dict[str, Any]], what: str, n: int, med: float, p90: float, **extra: Any) -> None:
    rows.append({"what": what, "n": n, "median_ms": round(med, 3), "p90_ms": round(p90, 3), **extra})
    print(f"{what:<72} n={n:<5} median {med:9.3f} ms  p90 {p90:9.3f} ms  {extra or ''}", flush=True)


def _rets(rng: random.Random, k: int) -> list[float]:
    return [rng.gauss(0.0, 0.01) for _ in range(k)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="smaller ledgers and fewer repetitions")
    ap.add_argument("--json", metavar="OUT", help="also write the rows as JSON")
    a = ap.parse_args()
    reps = 100 if a.quick else 500
    big = 2_000 if a.quick else 10_000
    rng = random.Random(7)
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        led = L.Ledger(root / "a")
        med, p90 = _times(lambda i: L.register_variant("v", {"i": i}, ledger=led), reps)
        _row(rows, "append a variant without a series", reps, med, p90)
        med, p90 = _times(lambda i: L.register_variant("r", {"i": i}, returns=_rets(rng, 252), ledger=led), reps)
        _row(rows, "append a variant with 252 returns", reps, med, p90)
        long_n = 30
        med, p90 = _times(lambda i: L.register_variant("s", {"i": i}, scores=_rets(rng, 100_000), ledger=led), long_n)
        _row(rows, "append a variant with 100,000 scores", long_n, med, p90)
        batch = [{"name": "b", "params": {"i": i}, "returns": _rets(rng, 252)} for i in range(100)]
        med, p90 = _times(lambda i: L.register_variants(batch, ledger=L.Ledger(root / f"batch{i}")), 20)
        _row(rows, "register_variants: a batch of 100 variants with 252 returns", 20, med, p90)

        # a run's record from its summary, with the file hash cold (a fresh CLI process) and warm
        shadows = root / "shadows.parquet"
        size_mb = 64
        shadows.write_bytes(os.urandom(size_mb << 20))
        ds = root / "ds"
        ds.mkdir()
        (ds / "manifest.json").write_text('{"schema": "qt.dataset_manifest", "dataset_id": "' + "d" * 64 + '"}')
        echo = {"dataset": {"path": str(ds)}, "shadows": {"path": str(shadows)}, "seed": 1}
        summ = m.ZooSummary(
            run_id="0" * 16, spec=echo, n_shadows=1, models=["a"], leaderboard=[], files=[], warnings=[]
        )

        def cold(_: int) -> None:
            cap._FILE_HASHES.clear()
            cap.run_record("zoo", summary=summ)

        med, p90 = _times(cold, 10)
        _row(rows, f"derive a zoo record, file hash cold ({size_mb} MiB shadows file)", 10, med, p90)
        med, p90 = _times(lambda i: cap.run_record("zoo", summary=summ), reps)
        _row(rows, "derive a zoo record, file hash cached", reps, med, p90)

        # a ledger of `big` records, each with a 252-value returns series (out of line)
        led = L.Ledger(root / "big")
        for start in range(0, big, 500):
            L.register_variants(
                [{"name": "g", "params": {"i": i}, "returns": _rets(rng, 252)} for i in range(start, start + 500)],
                ledger=led,
            )
        mb = led.path.stat().st_size / 1e6
        series_mb = sum(p.stat().st_size for p in (led.directory / "series").iterdir()) / 1e6
        med, p90 = _times(lambda i: L.register_variant("tail", {"i": i}, ledger=led), 100)
        _row(rows, f"append to a ledger of {big:,} records", 100, med, p90, ledger_mb=round(mb, 1))
        k = 5
        med, p90 = _times(lambda i: L.n_trials(ledger=led), k)
        _row(rows, f"n_trials over {big:,} records", k, med, p90)
        med, p90 = _times(lambda i: L.list_records(limit=1, ledger=led), k)
        _row(rows, f"list_records(limit=1) over {big:,} records", k, med, p90)
        med, p90 = _times(lambda i: L.selection_inputs(ledger=led), k)
        _row(rows, f"selection_inputs over {big:,} records (loads {big:,} series)", k, med, p90)
        med, p90 = _times(lambda i: L.verify_chain(ledger=led), 3)
        _row(rows, f"verify of {big:,} records and their series files", 3, med, p90, series_mb=round(series_mb, 1))

        # long series: 30 variants with 100,000 scores each
        led = L.Ledger(root / "long")
        L.register_variants(
            [{"name": "l", "params": {"i": i}, "scores": _rets(rng, 100_000)} for i in range(30)], ledger=led
        )
        med, p90 = _times(lambda i: L.n_trials(ledger=led), k)
        _row(
            rows,
            "n_trials over 30 records with 100,000-value series",
            k,
            med,
            p90,
            ledger_kb=round(led.path.stat().st_size / 1e3, 1),
        )
        med, p90 = _times(lambda i: L.selection_inputs(series="scores", ledger=led), k)
        _row(rows, "selection_inputs over 30 records with 100,000-value series", k, med, p90)

    meta = {
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
    }
    print(json.dumps(meta))
    if a.json:
        Path(a.json).write_text(json.dumps({"meta": meta, "rows": rows}, indent=2) + "\n")


if __name__ == "__main__":
    main()
