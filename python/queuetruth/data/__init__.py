"""`queuetruth data`: bring order-level data in, in one command.

- `qt.data.add(files, out=...)` (`queuetruth data add FILES --out DATASET`): ingest one or more files, possibly of
  several days, into one dataset; the format, venue and session date are inferred (`queuetruth.data.infer`), the
  days are merged (`queuetruth.data.merge`) and the data-quality checks run (`queuetruth.data.add`).
- `qt.data.fetch_iex(date, symbols, out=...)` (`queuetruth data fetch iex --date D --symbols S`): a free IEX DEEP+
  day, streamed and filtered to a few symbols, with IEX's attribution in the manifest (`queuetruth.data.iex`).
"""

from __future__ import annotations

from queuetruth.data.add import DataResult, add
from queuetruth.data.iex import FetchPlan, fetch_iex, plan_fetch
from queuetruth.data.infer import Detected, detect

__all__ = ["DataResult", "Detected", "FetchPlan", "add", "detect", "fetch_iex", "plan_fetch"]
