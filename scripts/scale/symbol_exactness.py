"""Per-symbol exactness for the W1 scale pipeline: which symbol-days of a unit raised the replay checks' flags.

A unit's replay checks (`unit.py`) sum over its symbols, so one symbol with a crossed book flags the whole unit.
When a unit is flagged, `attribute()` replays each partition of the unit's dataset on its own and returns the same
summable counts per symbol. Instruments replay independently (the book, the rank oracle and trade parity are per
instrument), so the per-symbol counts add up exactly to the unit's; `attribute()` checks that and reports it.

A one-partition view of a dataset is a directory holding a copy of `manifest.json` with only that partition and
symbolic links to the partition directory, `instruments.parquet`, `data_quality.json` and `trades_source/`
(`partition_view`); `replay` reads only the partitions its manifest lists, so trade parity compares that
instrument's source prints only.

Flag fields (`FLAG_FIELDS`): the eight book invariants, `executions_unknown_order`, `price_priority_violations` and
the trade-parity failures (`trades_mismatches`, `trades_missing_derived`, `trades_extra_derived`). A symbol-day is
exact when all of them are zero.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

INV8 = (
    "cancels_exceeding_resting",
    "fills_exceeding_resting",
    "fills_unknown_order",
    "unknown_order_refs",
    "duplicate_adds",
    "unexpected_modifies",
    "crossed_books_after_last",
    "negative_spreads",
)
PARITY_FLAGS = ("trades_mismatches", "trades_missing_derived", "trades_extra_derived")
FLAG_FIELDS = (*INV8, "executions_unknown_order", "price_priority_violations", *PARITY_FLAGS)
#: The attribution method recorded next to the per-symbol counts.
METHOD = "per_partition_replay"


def flags_of(ex: dict[str, Any] | None) -> dict[str, int]:
    """The non-zero flag counters of one `unit.exactness_counts` document (`{}` when exact or absent)."""
    if not ex:
        return {}
    out: dict[str, int] = {}
    for k in INV8:
        if (ex.get("invariants") or {}).get(k):
            out[k] = int(ex["invariants"][k])
    for k in ("executions_unknown_order", "price_priority_violations"):
        if ex.get(k):
            out[k] = int(ex[k])
    for k in PARITY_FLAGS:
        if (ex.get("parity") or {}).get(k):
            out[k] = int(ex["parity"][k])
    return out


def is_flagged(ex: dict[str, Any] | None) -> bool:
    return bool(flags_of(ex))


def _summable(ex: dict[str, Any]) -> dict[str, int]:
    """Every integer counter of an exactness document, flattened (`stats.records`, `invariants.x`, ...)."""
    out: dict[str, int] = {}
    for k in ("stats", "invariants", "parity"):
        for f, v in (ex.get(k) or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                out[f"{k}.{f}"] = v
    for k in ("executions_checked", "executions_unknown_order", "price_priority_violations", "sweeps"):
        if isinstance(ex.get(k), int):
            out[k] = ex[k]
    for k in ("fifo_rank_histogram", "fifo_rank_histogram_sweep_adjusted"):
        for i, v in enumerate(ex.get(k) or []):
            out[f"{k}[{i}]"] = v
    return out


def sum_check(unit_ex: dict[str, Any], per_symbol: dict[str, dict[str, Any]]) -> list[str]:
    """Counters whose per-symbol sum differs from the unit's (`[]` when the attribution is complete)."""
    want = _summable(unit_ex)
    got: dict[str, int] = dict.fromkeys(want, 0)
    for ex in per_symbol.values():
        for k, v in _summable(ex).items():
            got[k] = got.get(k, 0) + v
    return [
        f"{k}: unit {want.get(k)} != sum {got.get(k)}"
        for k in sorted(set(want) | set(got))
        if want.get(k) != got.get(k)
    ]


def partition_view(ds: Path, partition: dict[str, Any], dest: Path) -> Path:
    """A dataset directory at `dest` that holds only `partition` (one entry of `manifest.json` `partitions`)."""
    man = json.loads((ds / "manifest.json").read_text())
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(json.dumps({**man, "partitions": [partition]}))
    for name in ("instruments.parquet", "data_quality.json", "trades_source"):
        if (ds / name).exists():
            os.symlink((ds / name).resolve(), dest / name)
    for f in partition["files"]:
        part_dir = Path(f["path"]).parent
        (dest / part_dir).parent.mkdir(parents=True, exist_ok=True)
        if not (dest / part_dir).exists():
            os.symlink((ds / part_dir).resolve(), dest / part_dir)
    return dest


def replay_counts(ds: Path, out: Path, threads: int = 1) -> dict[str, Any]:
    """The replay checks of `unit.py` on one dataset (rank oracle, 2 ms sweeps, trade parity) as summable counts."""
    from unit import exactness_counts

    import queuetruth as qt

    rs = qt.replay(
        {
            "dataset": {"path": str(ds)},
            "out": str(out),
            "threads": threads,
            "rank_oracle": True,
            "sweep_gap_ns": 2_000_000,
            "parity_trades": True,
        }
    )
    return exactness_counts(rs.model_dump(by_alias=True))


def attribute(
    ds: Path,
    unit_ex: dict[str, Any],
    loc2sym: dict[int, str],
    threads: int = 1,
    work: Path | None = None,
    replay: Callable[[Path, Path, int], dict[str, Any]] = replay_counts,
    full: bool = False,
) -> dict[str, Any]:
    """Replays every partition of `ds` alone; returns the per-symbol attribution recorded in a shard leaf:

    `{"method", "complete", "sum_mismatches", "symbols": {symbol: {flag: count}}}` with only the flagged symbols
    listed (an exact symbol has no entry; a unit is one session date). `complete` is true when every summable
    counter of the per-partition replays adds up to the unit's (`sum_check`). A one-partition dataset is not
    replayed again: its symbol's counts are the unit's. `full` adds `"counts"`: every symbol's whole exactness
    document (for tracing; never part of a leaf)."""
    man = json.loads((ds / "manifest.json").read_text())
    per: dict[str, dict[str, Any]] = {}
    if len(man["partitions"]) == 1:  # one symbol: its counts are the unit's, no second replay
        p = man["partitions"][0]
        per[loc2sym.get(p["instrument_id"], str(p["instrument_id"]))] = unit_ex
        return _attribution(unit_ex, per, full)
    tmp = Path(tempfile.mkdtemp(prefix="qt-attr-", dir=work))
    try:
        for i, p in enumerate(man["partitions"]):
            view = partition_view(ds, p, tmp / f"v{i}")
            sym = loc2sym.get(p["instrument_id"], str(p["instrument_id"]))
            if sym in per:  # a unit is one bucket file, i.e. one session date
                raise ValueError(f"{ds}: {sym} has more than one partition")
            per[sym] = replay(view, tmp / f"r{i}", threads)
            shutil.rmtree(tmp / f"r{i}", ignore_errors=True)
            shutil.rmtree(view, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _attribution(unit_ex, per, full)


def failed_attribution(error: BaseException) -> dict[str, Any]:
    """The leaf record of an attribution that raised: incomplete, so `atlas.py reduce-day` labels every symbol of
    the unit (only the exception type is kept, so the leaf does not depend on paths or machine)."""
    return {"method": METHOD, "complete": False, "sum_mismatches": [], "symbols": {}, "error": type(error).__name__}


def _attribution(unit_ex: dict[str, Any], per: dict[str, dict[str, Any]], full: bool) -> dict[str, Any]:
    bad = sum_check(unit_ex, per)
    out = {
        "method": METHOD,
        "complete": not bad,
        "sum_mismatches": bad,
        "symbols": {k: f for k, ex in sorted(per.items()) if (f := flags_of(ex))},
    }
    if full:
        out["counts"] = dict(sorted(per.items()))
    return out
