"""`atlas/v1/symbols/<letter>.json` (`qt.atlas_symbols`): per symbol, its tier, days and the bias of every library
setting at 1 s, 10 s and 1 min, from the atlas's `per_symbol.parquet` (one shard per first letter).

A shard is a small table: `settings` lists the library settings once, `columns` names the numbers of a cell, and each
symbol's `cells[i][j]` holds setting `i` at horizon `j` (null when the atlas has no number there), so a 20-day shard
stays near a megabyte."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from queuetruth.atlas._parts import Library
from queuetruth.schemas.models import QtDoc, QtModel, _tag

#: The numbers of a cell, in order: the bias in percentage points with its 95% day-bootstrap interval (null bounds on
#: a symbol seen on one day), the estimate shrunk toward the symbol's tier (empirical Bayes), the orders and the days.
COLUMNS = ("bias_pp", "lo_pp", "hi_pp", "shrunk_pp", "n", "n_days")


class SymbolSetting(QtModel):
    library: Library
    label: str
    model_id: str


class SymbolEntry(QtModel):
    symbol: str
    tier: str
    #: trading days the symbol was seen on
    days: int
    #: `cells[i][j]`: setting `i` at horizon `j`, the numbers of `columns` (`cells[i]` null: no number at all)
    cells: list[list[list[float | None] | None] | None]


class AtlasSymbols(QtDoc):
    """One shard: the symbols whose first letter is `letter` (`_` for any other first character)."""

    SCHEMA: ClassVar[str] = "qt.atlas_symbols"
    schema_: Literal["qt.atlas_symbols"] = _tag("qt.atlas_symbols")
    schema_version: Literal[1] = 1
    venue: str
    atlas_root: str
    letter: str
    mode: str
    shadow_population: str
    horizons: list[str]
    columns: list[str] = Field(default_factory=lambda: list(COLUMNS))
    settings: list[SymbolSetting] = Field(default_factory=list)
    symbols: list[SymbolEntry] = Field(default_factory=list)


DOC_MODELS = {AtlasSymbols.SCHEMA: AtlasSymbols}

__all__ = ["COLUMNS", "DOC_MODELS", "AtlasSymbols", "SymbolEntry", "SymbolSetting"]
