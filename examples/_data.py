"""Shared helpers for the examples: where the synthetic dataset is, where outputs go, and small printing helpers.

Dataset lookup order (`dataset_path`):

1. env `QT_EXAMPLE_DATA`, if set (for example a directory written by `queuetruth fixtures synth --out data`);
2. `<repo>/tests/fixtures/synth_small`, resolved relative to this file (a source checkout);
3. `./qt-example-data`, written once with `queuetruth.api.fixtures_synth` if it is not there yet.

Outputs (`out_dir`) go to `./qt-examples-out/<script name>` or, with env `QT_EXAMPLE_OUT`, to
`$QT_EXAMPLE_OUT/<script name>`. They never go inside the dataset or `tests/fixtures`.

Every example needs the compiled extension `queuetruth._core` (`make develop` in a checkout, or the wheel).
All data is synthetic: no market data, and no number printed here says anything about a real venue.
"""

from __future__ import annotations

import datetime
import os
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

import queuetruth as qt

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "synth_small"
GENERATED = Path("qt-example-data")
OUT_ROOT = Path("qt-examples-out")
MARKER = ".qt-example"

#: Shadow sample used by every example. The default lifetime (300 s) equals the zoo's default `h_max_ns`, so the
#: shadows count as never-cancel and the zoo leaderboard is defined (shorter lives make them cancel-bearing,
#: whose metrics go to `cancel_bearing/...` rows of `qt.zoo_errors` and leave the leaderboard null).
SHADOW_N = 400
SHADOW_SEED = 1

NS = 1_000_000_000


def dataset_path() -> Path:
    """The synthetic dataset directory (see the module docstring for the lookup order)."""
    env = os.environ.get("QT_EXAMPLE_DATA")
    if env:
        p = Path(env)
        if not (p / "manifest.json").exists():
            raise SystemExit(f"QT_EXAMPLE_DATA={env}: no manifest.json (write one with `queuetruth fixtures synth`)")
        return p.resolve()
    if (FIXTURE / "manifest.json").exists():
        return FIXTURE
    if not (GENERATED / "manifest.json").exists():
        print(f"writing the synthetic dataset to {GENERATED} (queuetruth.api.fixtures_synth)")
        qt.api.fixtures_synth(str(GENERATED))
    return GENERATED.resolve()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def out_root() -> Path:
    """Root of all example outputs: env `QT_EXAMPLE_OUT` or `./qt-examples-out`."""
    return Path(os.environ.get("QT_EXAMPLE_OUT") or OUT_ROOT).resolve()


def out_dir(script: str, *, fresh: bool = True) -> Path:
    """`<out_root>/<script stem>`, emptied first when `fresh` (only if it is an example output directory)."""
    p = out_root() / Path(script).stem
    for guarded in (REPO / "tests" / "fixtures", dataset_path()):
        if _inside(p, guarded.resolve()):
            raise SystemExit(f"{p}: refusing to write example outputs inside {guarded}")
    if fresh and p.exists():
        if not (p / MARKER).exists() and any(p.iterdir()):
            raise SystemExit(f"{p} exists and was not written by these examples; set QT_EXAMPLE_OUT elsewhere")
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)
    (p / MARKER).write_text("written by queuetruth examples\n", encoding="utf-8")
    return p


def sample_example_shadows(dataset: Path, out: Path) -> Path:
    """Samples the shared shadow schedule (`qt.shadow_spec`) to `out/shadows.parquet` and returns its path."""
    path = out / "shadows.parquet"
    summ = qt.sample_shadows({"dataset": {"path": str(dataset)}, "out": str(path), "n": SHADOW_N, "seed": SHADOW_SEED})
    print(f"sampled {summ.n} shadows: by instrument {summ.by_instrument}, by placement (ticks) {summ.by_placement}")
    return path


def table_by_schema(out: Path, files: Iterable[Any], schema: str) -> Path:
    """The output file whose `qt.schema` metadata is `schema`, taken from a summary's `files` list."""
    for f in files:
        p = out / f.name
        if p.suffix == ".parquet" and p.exists():
            meta = pq.read_schema(p).metadata or {}
            if meta.get(b"qt.schema", b"").decode() == schema:
                return p
    raise SystemExit(f"no {schema} table among the files of {out}")


def horizon_label(ns: int | str) -> str:
    """`100000000` -> `100ms`, `60000000000` -> `60s`."""
    v = int(ns)
    if v >= NS and v % NS == 0:
        return f"{v // NS}s"
    return f"{v // 1_000_000}ms"


def fmt(x: float | None, digits: int = 4) -> str:
    """A float rounded for display, or `n/a`."""
    return "n/a" if x is None else f"{x:.{digits}f}"


def fmt_p(p: float | None) -> str:
    """A p-value with 3 significant digits, or `n/a`."""
    return "n/a" if p is None else f"{p:.3g}"


def fmt_ci(value: float | None, lo: float | None, hi: float | None, digits: int = 4) -> str:
    """`value [lo, hi]`."""
    return f"{fmt(value, digits)} [{fmt(lo, digits)}, {fmt(hi, digits)}]"


def _cell(v: Any) -> str:
    if isinstance(v, datetime.datetime):
        return v.strftime("%H:%M:%S.%f")
    if isinstance(v, float):
        return f"{v:.4g}"
    return "" if v is None else str(v)


def print_rows(table: pa.Table, columns: Sequence[str], n: int = 5) -> None:
    """Prints the first `n` rows of `columns` as an aligned text table (timestamps as UTC time of day)."""
    t = table.select(list(columns)).slice(0, n)
    for i, f in enumerate(t.schema):
        # Nanosecond timestamps need pandas in to_pylist(); microseconds are enough for a printed time of day.
        if pa.types.is_timestamp(f.type) and f.type.unit == "ns":
            t = t.set_column(i, f.name, t.column(i).cast(pa.timestamp("us", tz=f.type.tz), safe=False))
    rows = t.to_pylist()
    cells = [[_cell(r[c]) for c in columns] for r in rows]
    widths = [max([len(c), *(len(row[i]) for row in cells)]) for i, c in enumerate(columns)]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    for row in cells:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
