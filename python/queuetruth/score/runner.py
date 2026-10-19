"""How the scorer, the referee and live calibration reach the engines and the live-log package.

`ScoreRunner` is the seam: the default implementation calls `queuetruth.api.replay` / `queuetruth.api.run_zoo`,
`queuetruth._core.bbo_from_l2` and `queuetruth.live.{build_schedule, to_schedule, validate}` (all imported lazily);
tests inject fakes. Output tables of engine runs are located by their `qt.schema` metadata, not by file name.

Optional `build_schedule(live_dir, *, entry, latency_ns, keep_priority_venues)`: a runner that has it returns the
`queuetruth.live.Schedule` (tables plus the live package's exclusion and entry-fallback accounting,
`queuetruth.live.schedule_report`); the scorer then reports those counts and passes its keep-priority venues. A
runner without it (test fakes) is asked for `to_schedule` and the scorer infers the exclusion reasons.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.schemas.specs import ReplaySpec, ZooSpec


class ScoreRunner(Protocol):
    """Engine and live-log entry points used by `queuetruth.score`.

    A runner whose `native_replay_options` attribute is true accepts the `queuetruth.api.replay` keyword options
    (`exclude_order_ids`, `fill_rule`, `hidden_print_policy`) on `replay`; without it the scorer falls back to a
    filtered copy of the dataset for self-exclusion (audit ruling A1) and passes no options."""

    def replay(self, spec: ReplaySpec) -> Any:
        """Exact replay writing SPEC 5.1.6 outputs to `spec.out`."""

    def run_zoo(self, spec: ZooSpec) -> Any:
        """Zoo run writing `qt.zoo_predictions` / `qt.zoo_fills` to `spec.out`."""

    def bbo_from_l2(self, dataset: str, out: str) -> Any:
        """`qt.bbo` and `qt.trades` derived from an L2-only dataset, written under `out`."""

    def to_schedule(self, live_dir: str, *, entry: str, latency_ns: int | None) -> tuple[pa.Table, pa.Table]:
        """`qt.shadow_spec`, `qt.shadow_amend` of a live log (SPEC 5.7.2)."""

    def validate(self, live_dir: str) -> list[str]:
        """Live-log validator warnings (raises `QtError` on errors)."""


class DefaultRunner:
    """Real implementations (`queuetruth.api`, `queuetruth._core`, `queuetruth.live`)."""

    native_replay_options = True

    def replay(self, spec: ReplaySpec, **options: Any) -> Any:
        from queuetruth import api

        return api.replay(spec, **options) if options else api.replay(spec)

    def run_zoo(self, spec: ZooSpec) -> Any:
        from queuetruth import api

        return api.run_zoo(spec)

    def bbo_from_l2(self, dataset: str, out: str) -> Any:
        from queuetruth import _core  # type: ignore[attr-defined]

        return _core.bbo_from_l2(json.dumps({"dataset": {"path": dataset}, "out": out, "threads": 1}))

    def to_schedule(self, live_dir: str, *, entry: str, latency_ns: int | None) -> tuple[pa.Table, pa.Table]:
        from queuetruth import live

        return live.to_schedule(live_dir, entry=entry, latency_ns=latency_ns)

    def build_schedule(
        self,
        live_dir: str,
        *,
        entry: str,
        latency_ns: int | None,
        keep_priority_venues: frozenset[str] | None = None,
    ) -> Any:
        """`queuetruth.live.build_schedule` (tables plus counts; optional runner method, see the module doc)."""
        from queuetruth import live

        return live.build_schedule(
            live_dir, entry=entry, latency_ns=latency_ns, keep_priority_venues=keep_priority_venues
        )

    def validate(self, live_dir: str) -> list[str]:
        from queuetruth import live

        return list(live.validate(live_dir))


def find_table(root: str | Path, name: str) -> pa.Table | None:
    """The table with `qt.schema = name` under `root` (single file, or the concatenation of all matching files in
    sorted path order), or None."""
    r = Path(root)
    if not r.exists():
        return None
    found = []
    for p in sorted(r.rglob("*.parquet")):
        try:
            meta = pq.read_schema(p).metadata or {}
        except (OSError, pa.ArrowInvalid):
            continue
        if meta.get(b"qt.schema", b"").decode() == name:
            found.append(pq.read_table(p))
    if not found:
        return None
    return found[0] if len(found) == 1 else pa.concat_tables(found, promote_options="permissive")
