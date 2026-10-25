"""`qt.demo_summary`: the document `queuetruth demo` prints with `--format json` and writes to `<out>/demo.json`, and
`qt.demo_plan`, what `--dry-run` prints, and `qt.demo_sources`, what `--list-sources --format json` prints.

Fractions are fractions (0.0313 = 3.13 pp); horizon keys are nanoseconds as decimal strings, as in
`qt.zoo_summary`. `run_id` follows SPEC 7.3: the first 16 hex of sha256 over the canonical demo spec (format,
session date, symbols, window, shadows, seed, canonical model ids, interval settings) with the subset resolved to
the sha256 of its content (the framed ITCH messages or the pcap bytes, before gzip), and `out` and `threads`
excluded. Where the subset came from (source, URL, path) and how it was compressed are provenance, not identity:
the same content gives the same id on any machine. `timings`, `created` and the paths are the only fields that vary
between identical runs.

The models live here so that `queuetruth.schemas.models` can register the three documents in `DOC_MODELS` without
importing the `queuetruth.demo` package (which `queuetruth.cli` keeps unloaded for other commands);
`queuetruth.demo.summary` re-exports every name.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from queuetruth.schemas.models import QtDoc, QtModel


class Cell(QtModel):
    """One model at one horizon: fill rates, bias (model minus exact) and its 95% interval."""

    exact_fill_rate: float | None
    model_fill_rate: float | None
    bias: float | None
    ci_lo: float | None
    ci_hi: float | None
    n: int
    ci_method: Literal["cluster_bootstrap", "none"]


class ModelRow(QtModel):
    """A zoo model's cells by horizon (`horizon_ns` as a decimal string)."""

    model_id: str
    cells: dict[str, Cell]


class Headline(QtModel):
    """The answer: NautilusTrader's default fill model against the exact queue. `verified_against` says which
    NautilusTrader releases the zoo's faithful replica was checked against, order for order (docs/RESULTS.md)."""

    model_id: str
    label: str
    verified_against: str
    sentence: str
    cells: dict[str, Cell]


class Reference(QtModel):
    """A published number on the same source, for comparison (full day, more shadows)."""

    label: str
    bias: dict[str, float]
    ci_lo: dict[str, float]
    ci_hi: dict[str, float]
    #: the exact fill rate of the published population at the same horizons
    exact_fill_rate: dict[str, float]


class SourceInfo(QtModel):
    id: str
    title: str
    format: Literal["itch", "iex"]
    session_date: str
    location: str
    attribution: str | None
    terms_url: str | None
    note: str | None


class WindowInfo(QtModel):
    timezone: Literal["America/New_York"] = "America/New_York"
    placement_start: str
    placement_end: str
    cut: str
    minutes: int
    h_max_s: int


class DataInfo(QtModel):
    symbols: list[str]
    subset_path: str
    subset_bytes: int
    subset_sha256: str
    content_sha256: str
    kept_messages: int
    cache_hit: bool
    pinned: bool
    #: the input is an http(s) URL (else a local file); `bytes_downloaded` counts what was read either way
    remote: bool
    bytes_downloaded: int
    file_bytes: int | None
    resumptions: int
    records: int
    data_quality: Literal["clean", "warnings", "unusable"]
    data_quality_reasons: list[str]
    dataset_id: str | None


class Interval(QtModel):
    method: str
    cluster: str
    clusters: int
    bootstrap_b: int
    note: str


class Outputs(QtModel):
    dir: str
    html: str
    json_path: str = Field(alias="json")
    dataset: str
    shadows: str
    zoo: str


class Timings(QtModel):
    fetch_s: float
    ingest_s: float
    shadows_s: float
    zoo_s: float
    answer_s: float
    total_s: float


class DemoSummary(QtDoc):
    """Summary of `queuetruth demo` (module docstring)."""

    SCHEMA: ClassVar[str] = "qt.demo_summary"
    schema_: Literal["qt.demo_summary"] = Field(default="qt.demo_summary", alias="schema")
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    queuetruth_version: str
    created: str
    spec: dict[str, Any]
    source: SourceInfo
    window: WindowInfo
    data: DataInfo
    n_shadows: int
    n_leaderboard: int | None
    #: shadows left out of every model because a faithful hftbacktest model stops on them (its issue #312)
    n_excluded_hbt_312: int
    zoo_run_id: str
    headline: Headline
    models: list[ModelRow]
    interval: Interval
    reference: Reference | None
    outputs: Outputs
    timings: Timings
    warnings: list[str]


class DemoPlan(QtDoc):
    """What `queuetruth demo --dry-run` would do."""

    SCHEMA: ClassVar[str] = "qt.demo_plan"
    schema_: Literal["qt.demo_plan"] = Field(default="qt.demo_plan", alias="schema")
    schema_version: Literal[1] = 1
    source: SourceInfo
    window: WindowInfo
    symbols: list[str]
    cache_dir: str
    cache_hit: bool
    download_needed: bool
    estimated_download_bytes: int | None
    #: the estimate lies past the last measured checkpoint (extrapolated, capped at the file's size)
    estimate_extrapolated: bool
    max_download_bytes: int | None
    file_bytes: int | None
    out: str


class DemoSourceRow(QtModel):
    id: str
    title: str
    format: Literal["itch", "iex"]
    session_date: str
    url: str
    default_symbols: list[str]
    attribution: str | None
    terms_url: str | None


class DemoSources(QtDoc):
    """The public days `queuetruth demo --list-sources` knows."""

    SCHEMA: ClassVar[str] = "qt.demo_sources"
    schema_: Literal["qt.demo_sources"] = Field(default="qt.demo_sources", alias="schema")
    schema_version: Literal[1] = 1
    default: str
    aliases: dict[str, str]
    sources: list[DemoSourceRow]


#: The demo documents by schema name (merged into `queuetruth.schemas.models.DOC_MODELS`).
DEMO_DOC_MODELS: dict[str, type[QtDoc]] = {m.SCHEMA: m for m in (DemoSummary, DemoPlan, DemoSources)}

__all__ = [
    "DEMO_DOC_MODELS",
    "Cell",
    "DataInfo",
    "DemoPlan",
    "DemoSourceRow",
    "DemoSources",
    "DemoSummary",
    "Headline",
    "Interval",
    "ModelRow",
    "Outputs",
    "Reference",
    "SourceInfo",
    "Timings",
    "WindowInfo",
]
