"""Regenerates `schemas/json`, `schemas/arrow` and `schemas/samples` from `queuetruth.schemas` (`make schemas`).

With `--check`, writes nothing and exits 1 if any committed file differs (O-SCHEMA-2).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from queuetruth.schemas import export  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Writes (or checks) every generated schema artifact."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if committed files differ; write nothing")
    args = ap.parse_args(argv)
    if args.check:
        stale = []
        files = export.artifacts()
        for rel, text in sorted(files.items()):
            p = ROOT / rel
            if not p.exists() or p.read_text(encoding="utf-8") != text:
                stale.append(rel)
        for sub in ("schemas/json", "schemas/arrow", "schemas/samples"):
            for p in sorted((ROOT / sub).glob("*.json")):
                if f"{sub}/{p.name}" not in files:
                    stale.append(f"{sub}/{p.name} (stale)")
        for rel in stale:
            print(f"differs: {rel}", file=sys.stderr)
        return 1 if stale else 0
    written = export.write_all(ROOT)
    print(f"{len(written)} schema files up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
