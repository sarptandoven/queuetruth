"""Write the files generated from the importer registry (`queuetruth.live.importers`).

    uv run --frozen --no-sync python scripts/gen_importers.py           # write them
    uv run --frozen --no-sync python scripts/gen_importers.py --check   # exit 1 if any would change

- python/queuetruth/schemas/_live_venues.py: the `LiveImportSpec.venue` values (the built-in importers), so the
  contracts layer never imports the importers. `make schemas` then writes the JSON Schema enum and the Rust
  `LiveVenue` from it.
- crates/cli/src/live_importers.rs: which venues take `--fix-*` flags and `--option KEY=VALUE` in the CLI grammar.

Run by `make generate` before the schemas (they read the first file). Docs: docs/reference/importers.md is written by
docs/gen_reference.py from the same registry.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when a file is stale; write nothing")
    a = ap.parse_args(argv)
    from queuetruth.live import importers

    stale = []
    for rel, text in importers.generated().items():
        p = ROOT / rel
        if p.exists() and p.read_text(encoding="utf-8") == text:
            continue
        stale.append(rel)
        if not a.check:
            p.write_text(text, encoding="utf-8")
    if a.check and stale:
        print(f"stale: {', '.join(stale)}; run `make generate`", file=sys.stderr)
        return 1
    for rel in stale:
        print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
