"""Pydantic models of the trial ledger: the stored record (`qt.ledger_record`) and the documents of the `ledger`
commands (`qt.ledger_list`, `qt.ledger_stats`, `qt.ledger_verification`, `qt.ledger_export`,
`qt.selection_inputs`).

Each ledger line is the canonical JSON (RFC 8785, `queuetruth.schemas.runid.canonical_json`) of
`{"hash": H, "record": R}`, where `H` is the sha256 of the exact UTF-8 bytes of `R` in that line and `R.prev` is the
`H` of the line before (64 zeros for the genesis record). `schema_version` is 1; unknown fields are rejected.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from queuetruth.schemas.models import HEX16, HEX64, QtDoc, QtModel

HEX32 = r"^[0-9a-f]{32}$"
#: `study` and `name` labels: the id rule of SPEC 0.1 (`shadow_id`, `order_key`).
LABEL = r"^[A-Za-z0-9_.:\-]{1,128}$"
ERROR_CODE = r"^QT-[A-Z]+-[0-9]{3}$"
#: Longest stored performance series (values per record).
MAX_SERIES = 1_000_000
I64_MIN, I64_MAX = -(2**63), 2**63 - 1
#: Longest free-text note.
MAX_NOTE = 4096

Hex16 = Annotated[str, Field(pattern=HEX16)]
Hex32 = Annotated[str, Field(pattern=HEX32)]
Hex64 = Annotated[str, Field(pattern=HEX64)]
Label = Annotated[str, Field(pattern=LABEL)]

#: Record kinds: `genesis` (seq 0, one per ledger), one per recorded entry point, and user-registered `variant`s.
Kind = Literal["genesis", "zoo", "calibrate", "calibrate-live", "fit-hazard", "score", "shadows-sample", "variant"]
#: The kinds that are trials (everything but `genesis`), in display order.
TRIAL_KINDS: tuple[str, ...] = (
    "zoo",
    "calibrate",
    "calibrate-live",
    "fit-hazard",
    "score",
    "shadows-sample",
    "variant",
)
#: Kinds recorded automatically by `queuetruth.api` and the CLI.
RUN_KINDS: tuple[str, ...] = TRIAL_KINDS[:-1]
SeriesKind = Literal["returns", "scores"]
Scalar = float | int | str | bool | None


class Series(QtModel):
    """A per-trial performance series as data: per-period `returns` (fractions, not percent) or `scores` (any
    metric, one value per evaluation unit; `higher_is_better` says which way is good), optionally indexed by the
    period end of each value (UTC ns, strictly increasing), which is what lets several trials' series be aligned."""

    kind: SeriesKind
    values: list[float] = Field(min_length=1, max_length=MAX_SERIES)
    #: Period end of each value (UTC ns, strictly increasing, one per value); null = not stated.
    index: list[int] | None = None
    #: Periods per year of a returns series (252 for daily equity returns); null = not stated.
    periods_per_year: float | None = Field(default=None, gt=0)
    higher_is_better: bool = True

    @field_validator("values")
    @classmethod
    def _finite(cls, v: list[float]) -> list[float]:
        for i, x in enumerate(v):
            if not math.isfinite(x):
                raise ValueError(f"values[{i}] is not finite ({x!r})")
        return v

    @model_validator(mode="after")
    def _index(self) -> Series:
        ix = self.index
        if ix is not None:
            if len(ix) != len(self.values):
                raise ValueError(f"index has {len(ix)} entries for {len(self.values)} values")
            for i in range(1, len(ix)):
                if ix[i] <= ix[i - 1]:
                    raise ValueError(f"index[{i}] = {ix[i]} does not follow index[{i - 1}] = {ix[i - 1]}")
            if ix and not (I64_MIN <= ix[0] and ix[-1] <= I64_MAX):
                raise ValueError("index values must fit in a signed 64-bit integer (UTC ns)")
        return self


class SeriesRef(QtModel):
    """A record's series, stored out of line: the values (and index) live in `<ledger dir>/series/<sha256>.qts`,
    content-addressed, and the record keeps their sha256, so the hash chain covers them and counting, listing and
    statistics never read them."""

    kind: SeriesKind
    #: sha256 of the series file (`queuetruth.ledger._series` layout).
    sha256: Hex64
    n_obs: int = Field(ge=1, le=MAX_SERIES)
    #: The series carries a period-end index.
    indexed: bool
    periods_per_year: float | None = Field(default=None, gt=0)
    higher_is_better: bool = True


class CodeVersion(QtModel):
    """What code produced the trial."""

    queuetruth: str
    #: `qt.engine_version` of the tables (`queuetruth.schemas.arrow.ENGINE_VERSION`).
    engine: str
    #: `queuetruth._core.version()`; null without the compiled extension.
    core: str | None = None
    #: HEAD commit of the queuetruth source checkout the package was imported from (40 hex); null for an installed
    #: wheel. Uncommitted changes are not reflected.
    git: str | None = None
    #: HEAD commit of the git checkout holding the working directory (the caller's project), when there is one;
    #: uncommitted changes are not reflected.
    project_git: str | None = None
    #: The caller's own code version (for a variant: its strategy's commit or tag).
    user: str | None = Field(default=None, max_length=256)


class ErrorInfo(QtModel):
    """The `QT-*` error of a run that failed."""

    code: Annotated[str, Field(pattern=ERROR_CODE)]
    message: str = Field(max_length=MAX_NOTE)


class LedgerRecord(QtDoc):
    """One ledger record (the `record` of a ledger line)."""

    SCHEMA: ClassVar[str] = "qt.ledger_record"
    schema_: Literal["qt.ledger_record"] = Field(default="qt.ledger_record", alias="schema")
    schema_version: Literal[1] = 1
    #: Random id fixed by the genesis record; every record of the ledger repeats it.
    ledger_id: Hex32
    #: 0 for genesis, then +1 per record.
    seq: int = Field(ge=0)
    #: `hash` of the previous line (64 zeros for genesis).
    prev: Hex64
    kind: Kind
    status: Literal["ok", "error"]
    #: Group of trials searched for one claim (null for genesis).
    study: Label | None = None
    #: The trial's identity: the `run_id` of a run; for a variant a hash of (name, params, dataset, code version,
    #: outcome), the outcome being its series (or, without one, its metrics), so a re-evaluation that gives a
    #: different result is a new trial; null for genesis and failed runs (no outcome, not a trial).
    trial_id: Hex16 | None = None
    run_id: Hex16 | None = None
    #: First 16 hex of sha256 of the configuration (the spec without execution-only fields, defaulted 1.1 fields
    #: and data references; a variant: its name and params), independent of data and engine version.
    spec_hash: Hex16 | None = None
    #: sha256 of the canonical JSON of `inputs`; null without data inputs.
    dataset_hash: Hex64 | None = None
    #: Identity of every data reference of the spec by its field path (`dataset` -> `dataset_id`, a file -> its
    #: sha256, a live log -> its manifest sha256; SPEC 7.3) as the run's own `run_id` resolves it; for a variant,
    #: `dataset` (the content identity of a file or dataset directory it names) or `dataset_label` (a free label).
    inputs: dict[str, str] = Field(default_factory=dict)
    name: Label | None = None
    params: dict[str, Any] | None = None
    #: Headline scalars of the run's summary (or the caller's, for a variant).
    metrics: dict[str, Scalar] = Field(default_factory=dict)
    series: SeriesRef | None = None
    #: Runs a variant was evaluated with (their `run_id`s).
    run_ids: list[Hex16] = Field(default_factory=list)
    code: CodeVersion | None = None
    error: ErrorInfo | None = None
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    #: Wall clock (UTC ns) when the run started; null for genesis and variants without one.
    started_at_ns: int | None = None
    #: Wall clock (UTC ns) when the record was written.
    recorded_at_ns: int


class Head(QtModel):
    """The last record of a ledger: anchor it outside the ledger (`seq:hash`) to detect a rewritten history."""

    seq: int
    hash: Hex64


class LedgerFilter(QtModel):
    """Record selection. Every set field must match; prefixes and globs are case-sensitive."""

    #: Record kinds (default: every trial kind).
    kinds: list[Kind] | None = None
    #: Study name or `fnmatch` glob.
    study: str | None = None
    #: Variant or run name `fnmatch` glob.
    name: str | None = None
    #: `all` (default), `ok` or `error`. Failed runs are never trials, so this does not change trial counts.
    status: Literal["ok", "error", "all"] = "all"
    #: `recorded_at_ns >= since_ns`.
    since_ns: int | None = None
    #: `recorded_at_ns < until_ns`.
    until_ns: int | None = None
    #: Prefix of `dataset_hash` or of any `inputs` value.
    dataset: str | None = None
    #: Prefix of `run_id` (or of a variant's linked `run_ids`).
    run_id: str | None = None
    #: Prefix of `spec_hash`.
    spec_hash: str | None = None


class Problem(QtModel):
    """One integrity problem found by `verify`."""

    #: 1-based line of `ledger.jsonl`.
    line: int
    #: The record's `seq` when it could be read.
    seq: int | None = None
    reason: Literal[
        "malformed",
        "hash_mismatch",
        "not_canonical",
        "invalid_record",
        "seq_gap",
        "broken_link",
        "ledger_id_mismatch",
        "genesis",
        "anchor_mismatch",
        "series_missing",
        "series_mismatch",
    ]
    message: str


class LedgerVerification(QtDoc):
    """`ledger verify`: the full integrity check of a ledger file."""

    SCHEMA: ClassVar[str] = "qt.ledger_verification"
    schema_: Literal["qt.ledger_verification"] = Field(default="qt.ledger_verification", alias="schema")
    schema_version: Literal[1] = 1
    ledger: str
    ok: bool
    ledger_id: Hex32 | None
    n_records: int
    head: Head | None
    #: Bytes after the last newline: an append that was cut off (never committed); the next append moves them to
    #: `torn/` and restores the file. Not a failure.
    uncommitted_tail_bytes: int
    #: The `seq:hash` (or `hash`) the history was checked against; null = none given.
    anchor: str | None = None
    #: The anchored record is present unchanged; null = no anchor given.
    anchor_ok: bool | None = None
    problems: list[Problem]
    #: Problems beyond the first 100 are counted, not listed.
    n_problems: int


class KindStats(QtModel):
    """Counts of one kind or study."""

    records: int
    #: Trial count (see `LedgerStats.n_trials`).
    trials: int
    configs: int
    errors: int


class LedgerStats(QtDoc):
    """`ledger stats`: what the filter selects."""

    SCHEMA: ClassVar[str] = "qt.ledger_stats"
    schema_: Literal["qt.ledger_stats"] = Field(default="qt.ledger_stats", alias="schema")
    schema_version: Literal[1] = 1
    ledger: str
    ledger_id: Hex32
    head: Head
    filter: LedgerFilter
    #: Records the filter selects (failed runs included unless status is `ok`).
    n_records: int
    #: Trials among them: distinct `trial_id`s, a trial whose records carry k > 1 different series counting k
    #: times, and a run that a selected variant links (`run_ids`) counting with that variant, not again.
    n_trials: int
    #: Distinct `spec_hash`es (configurations, across datasets).
    n_configs: int
    #: Failed runs among the selected records.
    n_errors: int
    #: Trials with a series.
    n_with_series: int
    #: Trials whose records carry different series (each series counted as a trial).
    n_conflicts: int
    #: Runs not counted on their own because a selected variant links them.
    n_linked: int
    by_kind: dict[str, KindStats]
    by_study: dict[str, KindStats]
    first_recorded_at_ns: int | None
    last_recorded_at_ns: int | None


class LedgerList(QtDoc):
    """`ledger list`: the selected records, oldest first."""

    SCHEMA: ClassVar[str] = "qt.ledger_list"
    schema_: Literal["qt.ledger_list"] = Field(default="qt.ledger_list", alias="schema")
    schema_version: Literal[1] = 1
    ledger: str
    ledger_id: Hex32
    head: Head
    filter: LedgerFilter
    #: Records the filter selects.
    n_matching: int
    #: Trials among them whose records carry different series.
    n_conflicts: int
    #: The newest `limit` of them (all with limit 0), oldest first.
    records: list[LedgerRecord]


class LedgerExport(QtDoc):
    """`ledger export`: what was written."""

    SCHEMA: ClassVar[str] = "qt.ledger_export"
    schema_: Literal["qt.ledger_export"] = Field(default="qt.ledger_export", alias="schema")
    schema_version: Literal[1] = 1
    ledger: str
    ledger_id: Hex32
    head: Head
    out: str
    format: Literal["jsonl", "json", "parquet", "selection"]
    n_records: int
    bytes: int
    sha256: Hex64


class Envelope(QtModel):
    """A ledger line as data: the record and its stored hash."""

    hash: Hex64
    record: LedgerRecord


class SeriesData(QtModel):
    """The values (and index) of a stored series, checked against its sha256."""

    values: list[float]
    index: list[int] | None = None


class LedgerDump(QtDoc):
    """The `.json` export: the selected records with their hashes, the series they reference (by sha256, checked),
    and the head they were read at."""

    SCHEMA: ClassVar[str] = "qt.ledger_dump"
    schema_: Literal["qt.ledger_dump"] = Field(default="qt.ledger_dump", alias="schema")
    schema_version: Literal[1] = 1
    ledger_id: Hex32
    head: Head
    filter: LedgerFilter
    entries: list[Envelope]
    series: dict[str, SeriesData] = Field(default_factory=dict)


class SelectionTrial(QtModel):
    """One entry of `selection_inputs`: a trial, or one of the different series of a conflicted trial."""

    trial_id: Hex16
    kind: Kind
    study: str | None
    name: str | None
    run_id: str | None
    spec_hash: str | None
    #: The data the trial was evaluated on (`dataset_hash` of its latest record).
    dataset_hash: str | None
    #: Records of this trial (a rerun of the same spec on the same data is the same trial).
    n_records: int
    first_recorded_at_ns: int
    #: The trial's records carry different series; each is its own entry and counts as a trial.
    conflict: bool
    #: sha256 of the series (null without one).
    series_sha256: str | None
    #: The series of the requested kind; null when the trial has none.
    series: list[float] | None
    #: Period end of each value (UTC ns); null when the series was registered without one.
    index: list[int] | None
    n_obs: int
    periods_per_year: float | None
    higher_is_better: bool | None


class SelectionInputs(QtDoc):
    """`selection_inputs`: the trial count and per-trial series a selection statistic consumes. Plain data; nothing
    is computed from the series. `n_trials` with the variance of the trials' Sharpe ratios is what the Deflated
    Sharpe Ratio needs; PBO, SPA and model confidence sets also need every trial's series on the same periods,
    which `aligned` reports."""

    SCHEMA: ClassVar[str] = "qt.selection_inputs"
    schema_: Literal["qt.selection_inputs"] = Field(default="qt.selection_inputs", alias="schema")
    schema_version: Literal[1] = 1
    ledger_id: Hex32
    head: Head
    #: Every record was hash- and link-checked when read, and every returned series against its sha256.
    chain_verified: bool
    filter: LedgerFilter
    series_kind: SeriesKind
    #: Trials the filter selects, with or without a series: the count to deflate by. A trial whose records carry
    #: k > 1 different series counts k times; a run linked by a selected variant counts with it, not again.
    n_trials: int
    #: Distinct configurations (`spec_hash`) among them, across datasets.
    n_configs: int
    #: Trials by kind (one kind unless `allow_mixed`).
    by_kind: dict[str, int]
    #: Studies of the selected trials (one unless `allow_mixed`).
    studies: list[str]
    #: Entries that have a series of `series_kind`.
    n_with_series: int
    #: Trials whose records carry different series (reruns that disagree): every series is listed.
    conflicts: list[Hex16]
    #: Runs not counted on their own because a selected variant links them (`run_ids`).
    linked_run_ids: list[Hex16]
    #: Every trial has a series of `series_kind` with an index, and all indexes are equal.
    aligned: bool
    #: The common index when `aligned`, else null.
    index: list[int] | None
    #: In order of first record.
    trials: list[SelectionTrial]


#: Every document model of the ledger, by schema name.
DOCUMENTS: dict[str, type[QtDoc]] = {
    m.SCHEMA: m
    for m in (LedgerRecord, LedgerVerification, LedgerStats, LedgerList, LedgerExport, LedgerDump, SelectionInputs)
}


def json_schemas() -> dict[str, dict[str, Any]]:
    """JSON Schema (draft 2020-12) of every ledger document, by schema name."""
    out: dict[str, dict[str, Any]] = {}
    for name, model in sorted(DOCUMENTS.items()):
        doc = model.model_json_schema(by_alias=True, mode="serialization")
        doc.pop("title", None)
        out[name] = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": name, **doc}
    return out


__all__ = [
    "DOCUMENTS",
    "MAX_SERIES",
    "RUN_KINDS",
    "TRIAL_KINDS",
    "CodeVersion",
    "Envelope",
    "ErrorInfo",
    "Head",
    "KindStats",
    "LedgerDump",
    "LedgerExport",
    "LedgerFilter",
    "LedgerList",
    "LedgerRecord",
    "LedgerStats",
    "LedgerVerification",
    "Problem",
    "SelectionInputs",
    "SelectionTrial",
    "Series",
    "SeriesData",
    "SeriesRef",
    "json_schemas",
]
