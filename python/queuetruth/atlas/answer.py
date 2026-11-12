"""`qt.atlas_answer`: what `queuetruth atlas --format json` prints and `qt.atlas.lookup(...)` returns as `.doc`."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from queuetruth.atlas._parts import Interval, Library, Verdict
from queuetruth.schemas.models import QtDoc, QtModel, _tag


class AnswerRow(QtModel):
    """One setting in the answer's table."""

    library: Library
    label: str
    model_id: str
    horizon: str
    fill_rate_bias_pp: Interval
    verdict: Verdict
    n: int
    #: trading days behind the number (`n_dates` of the tier row, or the symbol's days)
    days: int | None


class AnswerConfig(QtModel):
    library: Library
    label: str
    model_id: str
    fill_rate_bias_pp: Interval
    verdict: Verdict
    verdict_text: str
    config: str | None
    config_kind: str | None
    note: str | None


class AtlasAnswer(QtDoc):
    SCHEMA: ClassVar[str] = "qt.atlas_answer"
    schema_: Literal["qt.atlas_answer"] = _tag("qt.atlas_answer")
    schema_version: Literal[1] = 1
    #: the answer in plain words, one line per library
    summary: str
    venue: str
    venue_name: str
    atlas_root: str
    built_at: str
    window: str
    library: str | None
    symbol: str | None
    tier: str
    tier_name: str
    horizon: str
    mode: str
    shadow_population: str
    #: where the data came from: a directory, or the URL and the cached copy it was checked into
    source: str
    rows: list[AnswerRow] = Field(default_factory=list)
    configs: list[AnswerConfig] = Field(default_factory=list)
    attribution: str | None
    warnings: list[str] = Field(default_factory=list)


DOC_MODELS = {AtlasAnswer.SCHEMA: AtlasAnswer}

__all__ = ["DOC_MODELS", "AnswerConfig", "AnswerRow", "AtlasAnswer"]
