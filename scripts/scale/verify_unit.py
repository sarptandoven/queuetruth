#!/usr/bin/env python3
"""Re-run one unit of a published W1 run and check it against the run's Merkle root.

    uv run --frozen --no-sync python scripts/scale/verify_unit.py --plan plan.json \\
        --atlas atlas_manifest.json --unit-id 12302019-b007-u0003 --split DIR

Needs the unit's bucket file (re-create it with `itch_split.py` from the public day; its sha256 is in the unit spec).
Checks, in order: the unit spec in the plan hashes to the leaf's `unit_spec_sha256`; the re-run's leaf hash equals
the published leaf; the published leaf's Merkle proof reaches the published root. On a leaf mismatch it prints which
leaf fields differ (e.g. a float content digest on another platform: SPEC 7.2 allows up to 4 ulp off macOS arm64,
integers must match exactly). Exit 0 when everything matches, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402
from unit import run_unit  # noqa: E402


def _diff(a: object, b: object, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
        return out
    return [] if a == b else [path]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--atlas", type=Path, required=True)
    ap.add_argument("--unit-id", required=True)
    ap.add_argument("--split", type=Path, required=True)
    ap.add_argument("--published-shard", type=Path, help="the published shard dir, to name differing leaf fields")
    ap.add_argument("--threads", type=int, default=1)
    a = ap.parse_args(argv)
    plan = json.loads(a.plan.read_text())
    atlas = json.loads(a.atlas.read_text())
    unit = next((u for u in plan["units"] if u["unit_id"] == a.unit_id), None)
    entry = next((x for x in atlas["leaves"] if x["unit_id"] == a.unit_id), None)
    if unit is None or entry is None:
        print(f"{a.unit_id}: not in the plan / atlas manifest", file=sys.stderr)
        return 1
    ok = K.verify_proof(entry["leaf_hash"], entry["proof"], atlas["merkle_root"])
    print(f"published leaf reaches the root: {ok}")
    with tempfile.TemporaryDirectory(prefix="qt-verify-") as tmp:
        m = run_unit(unit, a.split, Path(tmp) / a.unit_id, a.threads)
    same = m["leaf_hash"] == entry["leaf_hash"]
    print(f"re-run leaf {m['leaf_hash']} {'==' if same else '!='} published {entry['leaf_hash']}")
    if not same and a.published_shard:
        pub = json.loads((a.published_shard / "shard_manifest.json").read_text())["leaf"]
        print("differing leaf fields:", ", ".join(_diff(pub, m["leaf"])) or "(none)")
    return 0 if ok and same else 1


if __name__ == "__main__":
    raise SystemExit(main())
