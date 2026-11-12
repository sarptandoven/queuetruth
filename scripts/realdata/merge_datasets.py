"""Merge single-day queuetruth dataset directories into one multi-date dataset (SPEC 3.3 layout).

The merge itself lives in the package (`queuetruth.data.merge`, also used by `queuetruth data add`, which ingests
several days into one dataset in one command). This command line is kept for the scripts that call it
(`scripts/repro/reproduce.py`, `validate_et.py`); errors still end the process with their message.

    python merge_datasets.py --out .realdata/ds/nasdaq-2d .realdata/ds/nasdaq-2019-12-30 .realdata/ds/nasdaq-2020-01-30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from queuetruth.data import merge as _m
from queuetruth.errors import QtError

DQ_SUM = _m.DQ_SUM
STATUS_RANK = _m.STATUS_RANK
VERDICT_RANK = _m.VERDICT_RANK
dataset_id = _m.dataset_id


def merge(inputs: list[Path], out: Path) -> dict:
    """`queuetruth.data.merge.merge`; a refusal ends the process with its message, as before."""
    try:
        return _m.merge(inputs, out, created_by="merge_datasets.py")
    except QtError as e:
        raise SystemExit(e.message) from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("inputs", type=Path, nargs="+")
    a = ap.parse_args(argv)
    man = merge(a.inputs, a.out)
    print(json.dumps({"out": str(a.out), "dataset_id": man["dataset_id"], "partitions": len(man["partitions"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
