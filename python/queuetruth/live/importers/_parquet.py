"""Bounded reading of compressed inputs; the implementation lives in `queuetruth.dataset` (`read_parquet_bounded`)."""

from __future__ import annotations

from queuetruth.dataset import MAX_RATIO, SLACK, expansion_limit
from queuetruth.dataset import read_parquet_bounded as read_parquet

__all__ = ["MAX_RATIO", "SLACK", "expansion_limit", "read_parquet"]
