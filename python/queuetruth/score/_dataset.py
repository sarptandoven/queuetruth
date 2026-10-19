"""Read-only view of a dataset directory (SPEC 3.3) for the scorer: kind, instruments, instrument-day of a
timestamp, partition end times (`ts_max`, the "end of data" of an instrument-day) and the data-quality document."""

from __future__ import annotations

import bisect
import json
from pathlib import Path
from typing import Any

import pyarrow as pa

from queuetruth.score._io import read_manifest, read_parquet, rows

DAY_NS = 86_400_000_000_000


class DatasetView:
    """Dataset metadata needed for scoring."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.manifest: dict[str, Any] = read_manifest(self.root)
        self.kind: str = str(self.manifest.get("kind", "mbo"))
        ip = self.root / "instruments.parquet"
        self.instruments: pa.Table | None = read_parquet(ip) if ip.exists() else None
        self._inst: dict[int, list[dict[str, Any]]] = {}
        for r in rows(self.instruments) if self.instruments is not None else []:
            self._inst.setdefault(int(r["instrument_id"]), []).append(r)
        for v in self._inst.values():
            v.sort(key=lambda r: int(r.get("midnight_utc_ns") or 0))
        self.ts_max: dict[tuple[int, int], int] = {}
        for p in self.manifest.get("partitions", []) or []:
            try:
                self.ts_max[(int(p["instrument_id"]), int(p["session_date"]))] = int(p["ts_max"])
            except (KeyError, TypeError, ValueError):
                continue

    def day(self, instrument_id: int | None, ts: int) -> tuple[int, int]:
        """(session_date days, local midnight ns) of `ts` for the instrument (UTC day without instrument rows)."""
        v = self._inst.get(int(instrument_id)) if instrument_id is not None else None
        if v:
            mids = [int(r.get("midnight_utc_ns") or 0) for r in v]
            i = max(bisect.bisect_right(mids, ts) - 1, 0)
            return int(v[i]["session_date"]), mids[i]
        d = ts // DAY_NS
        return int(d), int(d * DAY_NS)

    def block_of(self, instrument_id: int | None, ts: int, n_blocks: int) -> int:
        """Index of the equal-duration block (of `n_blocks`) of the instrument-day's continuous session holding
        `ts` (clamped to the first/last block; 0 without instrument session rows), as `api._window_mask`/`qt_calib`."""
        v = self._inst.get(int(instrument_id)) if instrument_id is not None else None
        if not v:
            return 0
        mids = [int(r.get("midnight_utc_ns") or 0) for r in v]
        r = v[max(bisect.bisect_right(mids, ts) - 1, 0)]
        if r.get("open_s") is None or r.get("close_s") is None:
            return 0
        open_ns = int(r.get("midnight_utc_ns") or 0) + int(r["open_s"]) * 1_000_000_000
        length = max(int(r["close_s"]) - int(r["open_s"]), 1) * 1_000_000_000
        return min(max(int(float(ts - open_ns) / float(length) * n_blocks // 1), 0), n_blocks - 1)

    def end_of_data(self, instrument_id: int | None, session_date: int) -> int | None:
        """Last event time of the instrument-day partition, if known."""
        if instrument_id is None:
            return None
        return self.ts_max.get((int(instrument_id), int(session_date)))

    def data_quality(self) -> dict[str, Any] | None:
        """`data_quality.json` as a dict, if present."""
        p = self.root / "data_quality.json"
        if not p.exists():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return doc if isinstance(doc, dict) else None

    def data_source(self) -> dict[str, Any]:
        """`{dataset_id, venue, source_venue, validation_status}` for calibration provenance."""
        m = self.manifest
        return {
            "dataset_id": str(m.get("dataset_id", "")),
            "venue": str(m.get("venue", "")),
            "source_venue": str(m.get("source_venue", m.get("venue", ""))),
            "validation_status": str(m.get("validation_status", "synthetic_only")),
        }
