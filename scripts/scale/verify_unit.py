#!/usr/bin/env python3
"""Re-run one unit of a published W1 run and check it against the run's Merkle root.

    uv run --frozen --no-sync python scripts/scale/verify_unit.py --day DAY_DIR \\
        --atlas-manifest atlas_manifest.json --unit-id 10092026-b000-u0001 --split SPLIT_DIR

`--day` is a reduced day as `atlas.py reduce-day` writes it and the `atlas-days` release keeps it (`day_manifest.json`
and `plan.json`); `--plan PLAN --atlas DAY_MANIFEST` name the two files instead. `--atlas-manifest` also checks that the
day's root is the one the atlas lists for that date and that the atlas root recomputes from the listed day roots.
Needs the unit's bucket file (re-create it with `iex_split.py` or `itch_split.py` from the public day; its sha256 is in
the unit spec). Checks, in order: the unit spec in the plan hashes to the leaf's `unit_spec_sha256`; the re-run's leaf
hash equals the published leaf; the published leaf's Merkle proof reaches the published root. On a leaf mismatch it
prints which leaf fields differ (e.g. a float content digest on another platform: SPEC 7.2 allows up to 4 ulp off
macOS arm64, integers must match exactly). A leaf holds sha256 digests of the Parquet files the build wrote, so
re-run at the `git_head` the day manifest's `builds` names: the script says when this checkout is another commit.
A flagged unit's leaf now carries `exactness_by_symbol` (`symbol_exactness.py`); a root published before that holds
the leaf without it, so the re-run leaf is also compared in that form and reported as `pre-attribution`. Exit 0 when
everything matches, 1 otherwise.

`queuetruth verify atlas --day D --unit U --rerun` runs the same checks from the published atlas without the day's
files at hand (`queuetruth.atlas.verify`).
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

# the checks live in the package (`queuetruth verify atlas` runs them without a checkout); kept here by name
from queuetruth.atlas.verify import check_atlas, check_build  # noqa: E402
from queuetruth.atlas.verify import diff_fields as _diff  # noqa: E402

__all__ = ["check_atlas", "check_build", "main"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", type=Path, help="a reduced day directory (day_manifest.json and plan.json)")
    ap.add_argument("--plan", type=Path, help="the plan, when --day is not given")
    ap.add_argument("--atlas", type=Path, help="the day manifest (day_manifest.json), when --day is not given")
    ap.add_argument("--atlas-manifest", type=Path, help="atlas_manifest.json: also check the day root and atlas root")
    ap.add_argument("--unit-id", required=True)
    ap.add_argument("--split", type=Path, required=True)
    ap.add_argument("--published-shard", type=Path, help="the published shard dir, to name differing leaf fields")
    ap.add_argument("--threads", type=int, default=1)
    a = ap.parse_args(argv)
    if a.day is not None:
        if a.plan is not None or a.atlas is not None:
            ap.error("give --day, or --plan and --atlas, not both")
        a.plan, a.atlas = a.day / "plan.json", a.day / "day_manifest.json"
    elif a.plan is None or a.atlas is None:
        ap.error("give --day, or both --plan and --atlas")
    plan = json.loads(a.plan.read_text())
    day = json.loads(a.atlas.read_text())
    unit = next((u for u in plan["units"] if u["unit_id"] == a.unit_id), None)
    entry = next((x for x in day["leaves"] if x["unit_id"] == a.unit_id), None)
    if unit is None or entry is None:
        print(f"{a.unit_id}: not in the plan / day manifest", file=sys.stderr)
        return 1
    if day.get("plan_sha256") and K.sha256_hex(a.plan.read_bytes()) != day["plan_sha256"]:
        print("note: the plan file's sha256 is not the day manifest's plan_sha256 (the unit spec is still checked)")
    for line in check_build(day, K.build_info()):
        print(line)
    atlas_ok = True
    if a.atlas_manifest is not None:
        atlas_ok, msg = check_atlas(day, json.loads(a.atlas_manifest.read_text()))
        print(msg)
    ok = K.verify_proof(entry["leaf_hash"], entry["proof"], day["merkle_root"])
    print(f"published leaf reaches the root: {ok}")
    with tempfile.TemporaryDirectory(prefix="qt-verify-") as tmp:
        m = run_unit(unit, a.split, Path(tmp) / a.unit_id, a.threads)
    same = m["leaf_hash"] == entry["leaf_hash"]
    print(f"re-run leaf {m['leaf_hash']} {'==' if same else '!='} published {entry['leaf_hash']}")
    if not same and "exactness_by_symbol" in m["leaf"]:
        old = K.leaf_hash({k: v for k, v in m["leaf"].items() if k != "exactness_by_symbol"})
        same = old == entry["leaf_hash"]
        print(f"pre-attribution leaf {old} {'==' if same else '!='} published {entry['leaf_hash']}")
    if not same and a.published_shard:
        pub = json.loads((a.published_shard / "shard_manifest.json").read_text())["leaf"]
        print("differing leaf fields:", ", ".join(_diff(pub, m["leaf"])) or "(none)")
    return 0 if ok and same and atlas_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
