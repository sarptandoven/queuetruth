"""Adopt the real-hftbacktest golden vectors of an hbt cross-check run into `tests/fixtures/zoo/hbt_real/`.

Source: a directory produced by the hbt cross-check harness (hftbacktest 2.4.4 wheel and a Rust build of the crate at
commit 5f3ec40b2afb764e0fea112f941ed85523ef4e88, MIT; 83 scenarios x 18 models x 2 exchanges). Written here:
- `scenarios.json`: copied (feed rows and own-order actions per scenario; replay contract).
- `golden_ozoo1_rust.json`: copied (O-ZOO-1 rows computed by hbt's own `ProbQueueModel` code).
- `runs.json`: compact per-run expectations for the Rust test: `error` `[step, ts]` or null; `fills`
  `[step, order_id, ts, fill_qty_bits, leaves_bits, status]`; `events` `[step, order_id, status]`; `states`
  `[step, order_id, f_bits, t_bits | null]` = the queue state after the last hook of that (step, order).
- `hooks.json.gz`: every recorded hook `[run, step, order_id, hook, a_bits, b_bits, ret_bits, f_bits, t_bits]`
  (hook 0 new_order, 1 trade, 2 depth, 3 is_filled; null = not applicable) for the Python transliteration test.
Usage: `python python/tests/zoo/adopt_hbt_vectors.py <prep hbt dir>`; then `write_manifest`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "tests" / "fixtures" / "zoo" / "hbt_real"
HOOK = {"new_order": 0, "trade": 1, "depth": 2, "is_filled": 3}


def _bits(v: str | None) -> int | None:
    return None if v is None else int(v)


def adopt(src: Path) -> None:
    """Copies and compacts the vectors."""
    OUT.mkdir(parents=True, exist_ok=True)
    fx = src / "fixtures"
    shutil.copyfile(fx / "scenarios.json", OUT / "scenarios.json")
    shutil.copyfile(fx / "golden_ozoo1_rust.json", OUT / "golden_ozoo1_rust.json")
    runs = json.loads((fx / "expected" / "hbt_rust_runs.json").read_text())
    compact = []
    hooks = []
    for k, r in enumerate(runs["runs"]):
        last: dict[tuple[int, int], list] = {}
        for h in r["hooks"]:
            last[(h["step"], h["order_id"])] = [h["step"], h["order_id"], _bits(h["f_bits"]), _bits(h["t_bits"])]
            hooks.append(
                [
                    k,
                    h["step"],
                    h["order_id"],
                    HOOK[h["hook"]],
                    _bits(h["a_bits"]),
                    _bits(h["b_bits"]),
                    _bits(h["ret_bits"]),
                    _bits(h["f_bits"]),
                    _bits(h["t_bits"]),
                ]
            )
        err = r["error"]
        compact.append(
            {
                "scenario": r["scenario"],
                "model_id": r["model_id"],
                "error": None if err is None else [err["step"], err["ts"]],
                "fills": [
                    [
                        f["step"],
                        f["order_id"],
                        f["ts"],
                        int(f["fill_qty_bits"]),
                        _float_bits(f["leaves_qty"]),
                        f["status"],
                    ]
                    for f in r["fills"]
                ],
                "events": [[e["step"], e["order_id"], e["status"]] for e in r["order_events"]],
                "states": [last[key] for key in sorted(last)],
            }
        )
    doc = {
        "schema": "qt.zoo_hbt_runs",
        "schema_version": 1,
        "hbt_commit": runs["hbt_commit"],
        "engine": runs["engine"],
        "runs": compact,
    }
    (OUT / "runs.json").write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n")
    with gzip.GzipFile(OUT / "hooks.json.gz", "wb", mtime=0) as fh:
        fh.write((json.dumps(hooks, separators=(",", ":")) + "\n").encode())
    write_manifest(OUT.parent)


def _float_bits(x: float) -> int:
    import struct

    return struct.unpack("<Q", struct.pack("<d", float(x)))[0]


def write_manifest(root: Path) -> None:
    """Rewrites `MANIFEST.sha256` (sha256sum format, sorted paths) for every file under `root`."""
    lines = []
    for p in sorted(x for x in root.rglob("*") if x.is_file() and x.name != "MANIFEST.sha256"):
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{h}  {p.relative_to(root).as_posix()}")
    (root / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    adopt(Path(sys.argv[1]))
