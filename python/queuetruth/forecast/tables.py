"""Arrow schemas of the forecast referee's Parquet outputs (Python-only tables; same metadata keys as
`queuetruth.schemas.arrow`: `qt.schema`, `qt.schema_version`, `qt.engine_version`).

- `qt.forecast_eligibility`: every forecast entry of the ledger and why it was or was not scored (`anchored_at`: the
  earliest attested time of an anchored commitment covering it).
- `qt.forecast_scores`: every scored forecast, its losses and the baselines' losses on the same question
  (`<baseline>_<metric>`; null when the baseline or the metric does not apply), and the market quote the baseline
  used (`market_quote_ts`, `market_quote_age_ns` at the commit).
- `qt.forecast_bets`: every PnL candidate (scored binary forecast) and the order it became, or why not
  (`decision_quote_ts`: the quote the order was priced from; `quote_ts`: the quote it met on arrival).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth._version import __version__
from queuetruth.schemas.arrow import ARROW_SCHEMA_VERSION

TS = pa.timestamp("ns", tz="UTC")
TABLE_VERSION = ARROW_SCHEMA_VERSION
METRICS = ("brier", "log", "spherical", "rps", "crps")
BASELINES = ("market", "climatology", "uniform")


def _meta(name: str) -> dict[bytes, bytes]:
    return {
        b"qt.schema": name.encode(),
        b"qt.schema_version": TABLE_VERSION.encode(),
        b"qt.engine_version": __version__.encode(),
    }


ELIGIBILITY = pa.schema(
    [
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("recorded_at", TS, nullable=False),
        pa.field("clock", pa.utf8(), nullable=False),
        pa.field("anchored_at", TS, nullable=True),
        pa.field("forecaster", pa.utf8(), nullable=False),
        pa.field("question_id", pa.utf8(), nullable=False),
        pa.field("kind", pa.utf8(), nullable=False),
        pa.field("status", pa.utf8(), nullable=False),
        pa.field("detail", pa.utf8(), nullable=True),
        pa.field("cutoff", TS, nullable=True),
        pa.field("lead_ns", pa.int64(), nullable=True),
    ],
    metadata=_meta("qt.forecast_eligibility"),
)

SCORES = pa.schema(
    [
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("forecaster", pa.utf8(), nullable=False),
        pa.field("question_id", pa.utf8(), nullable=False),
        pa.field("kind", pa.utf8(), nullable=False),
        pa.field("cluster", pa.utf8(), nullable=False),
        pa.field("recorded_at", TS, nullable=False),
        pa.field("resolved_at", TS, nullable=False),
        pa.field("lead_ns", pa.int64(), nullable=False),
        pa.field("p", pa.float64(), nullable=True),
        pa.field("outcome", pa.utf8(), nullable=False),
        *[pa.field(m, pa.float64(), nullable=True) for m in METRICS],
        pa.field("log_clipped", pa.bool_(), nullable=False),
        pa.field("market_p", pa.float64(), nullable=True),
        pa.field("market_quote_ts", TS, nullable=True),
        pa.field("market_quote_age_ns", pa.int64(), nullable=True),
        pa.field("climatology_p", pa.float64(), nullable=True),
        *[pa.field(f"{b}_{m}", pa.float64(), nullable=True) for b in BASELINES for m in METRICS],
    ],
    metadata=_meta("qt.forecast_scores"),
)

BETS = pa.schema(
    [
        pa.field("forecaster", pa.utf8(), nullable=False),
        pa.field("question_id", pa.utf8(), nullable=False),
        pa.field("p", pa.float64(), nullable=False),
        pa.field("decided_at", TS, nullable=False),
        pa.field("exec_at", TS, nullable=True),
        pa.field("quote_ts", TS, nullable=True),
        pa.field("decision_quote_ts", TS, nullable=True),
        pa.field("status", pa.utf8(), nullable=False),
        pa.field("side", pa.utf8(), nullable=True),
        pa.field("price", pa.float64(), nullable=True),
        pa.field("mid", pa.float64(), nullable=True),
        pa.field("edge", pa.float64(), nullable=True),
        pa.field("kelly_f", pa.float64(), nullable=True),
        pa.field("contracts", pa.float64(), nullable=False),
        pa.field("fee", pa.float64(), nullable=False),
        pa.field("cost", pa.float64(), nullable=True),
        pa.field("payout", pa.float64(), nullable=True),
        pa.field("outcome", pa.float64(), nullable=False),
    ],
    metadata=_meta("qt.forecast_bets"),
)

SCHEMAS: dict[str, pa.Schema] = {
    "qt.forecast_eligibility": ELIGIBILITY,
    "qt.forecast_scores": SCORES,
    "qt.forecast_bets": BETS,
}


def write_table(rows: list[dict[str, Any]], schema: pa.Schema, path: Any) -> int:
    """Writes rows (dicts keyed by column; timestamps as int ns) as Parquet with the schema; returns the row count.

    Columns are written in schema order; the Parquet file carries no creation timestamp, so equal rows give
    byte-identical files."""
    cols = {}
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if f.type == TS:
            cols[f.name] = pa.array(vals, type=pa.int64()).cast(TS)
        else:
            cols[f.name] = pa.array(vals, type=f.type)
    table = pa.Table.from_pydict(cols, schema=schema)
    pq.write_table(table, path, compression="zstd")
    return table.num_rows


__all__ = ["BASELINES", "BETS", "ELIGIBILITY", "METRICS", "SCHEMAS", "SCORES", "write_table"]
