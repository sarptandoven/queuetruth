"""Read-only view of a dataset directory (SPEC 3.3) for the scorer: kind, instruments, instrument-day of a
timestamp, partition end times (`ts_max`, the "end of data" of an instrument-day) and the data-quality document."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa

from queuetruth.dataset import Dataset, open_dataset

DAY_NS = 86_400_000_000_000


class DatasetView:
    """Dataset metadata needed for scoring (a view of `queuetruth.dataset.Dataset`; a directory without a manifest
    reads as an MBO dataset without partitions)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.dataset: Dataset = open_dataset(self.root, missing_ok=True)
        self.manifest: dict[str, Any] = self.dataset.manifest.to_dict()
        self.kind: str = self.dataset.manifest.kind
        self.instruments: pa.Table | None = (
            self.dataset.table("instruments") if (self.root / "instruments.parquet").exists() else None
        )
        self.ts_max: dict[tuple[int, int], int] = {
            p.key: p.ts_max for p in self.dataset.partitions if p.ts_max is not None
        }

    def day(self, instrument_id: int | None, ts: int) -> tuple[int, int]:
        """(session_date days, local midnight ns) of `ts` for the instrument (UTC day without instrument rows)."""
        s = self.dataset.session_at(int(instrument_id), ts) if instrument_id is not None else None
        if s is not None:
            return s.session_date, int(s.midnight_utc_ns or 0)
        d = ts // DAY_NS
        return int(d), int(d * DAY_NS)

    def block_of(self, instrument_id: int | None, ts: int, n_blocks: int) -> int:
        """Index of the equal-duration block (of `n_blocks`) of the instrument-day's continuous session holding
        `ts` (clamped to the first/last block; 0 without instrument session rows), as `api._window_mask`/`qt_calib`."""
        s = self.dataset.session_at(int(instrument_id), ts) if instrument_id is not None else None
        if s is None or s.open_s is None or s.close_s is None:
            return 0
        open_ns = int(s.midnight_utc_ns or 0) + s.open_s * 1_000_000_000
        length = max(s.close_s - s.open_s, 1) * 1_000_000_000
        return min(max(int(float(ts - open_ns) / float(length) * n_blocks // 1), 0), n_blocks - 1)

    def end_of_data(self, instrument_id: int | None, session_date: int) -> int | None:
        """Last event time of the instrument-day partition, if known."""
        if instrument_id is None:
            return None
        return self.ts_max.get((int(instrument_id), int(session_date)))

    def data_quality(self) -> dict[str, Any] | None:
        """`data_quality.json` as a dict, if present."""
        return self.dataset.data_quality()

    def data_source(self) -> dict[str, Any]:
        """`{dataset_id, venue, source_venue, validation_status}` for calibration provenance."""
        return self.dataset.manifest.data_source()
