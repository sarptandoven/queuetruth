"""JSON documents of SPEC 3.10 and 4.5 as Pydantic v2 models (the source of truth for `schemas/json`).

Every document carries `"schema": "qt.<name>"` and `"schema_version": 1` first; fields named `schema`, `pass` and
`from` are stored under the Python attribute names `schema_`, `pass_` and `from_` and always serialized by alias.
Fields marked `?` in SPEC 3.10 are optional and nullable; every other field is required (statistics that can be
undefined are required but nullable). `{h: v}` maps are keyed by the horizon in ns as a decimal string.
`qt_schema::json` mirrors every analysis document with the same field names and order.

Schema 1.1 (SPEC 3.11): fields marked "1.1" are additive, optional and default to null (= not reported, the v0.1
behaviour); `schema_version` stays the integer major 1 and documents without them stay valid.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

HEX16 = r"^[0-9a-f]{16}$"
HEX64 = r"^[0-9a-f]{64}$"

RunId = Annotated[str, Field(pattern=HEX16)]
Sha256 = Annotated[str, Field(pattern=HEX64)]
HorizonKey = Annotated[str, Field(pattern=r"^[0-9]+$")]
IdKey = Annotated[str, Field(pattern=r"^-?[0-9]+$")]
#: `{h: v}` map: horizon in ns (decimal string) -> value (null when undefined).
HMap = dict[HorizonKey, float | None]
Fraction = float | None


def _require_tag(schema: dict[str, Any]) -> None:
    req = schema.setdefault("required", [])
    for key in ("schema_version", "schema"):
        if key not in req:
            req.insert(0, key)


class QtModel(BaseModel):
    """Base of every document part: unknown fields rejected, aliases used for serialization."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        """`BaseModel.model_dump` with `by_alias=True` by default."""
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:  # type: ignore[override]
        """`BaseModel.model_dump_json` with `by_alias=True` by default."""
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(**kwargs)


class QtDoc(QtModel):
    """A top-level document: `schema` and `schema_version` are required in the JSON Schema."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, json_schema_extra=_require_tag)
    SCHEMA: ClassVar[str] = ""


def _tag(name: str) -> Any:
    return Field(default=name, alias="schema")


# ---- shared parts -----------------------------------------------------------------------------------------------


class FileEntry(QtModel):
    """One output file: name relative to the output directory, row count for tables, size and sha256."""

    name: str
    rows: int | None = None
    bytes: int
    sha256: Sha256


class CiValue(QtModel):
    """Point estimate with a confidence interval."""

    value: float | None
    ci_lo: float | None
    ci_hi: float | None


Files = list[FileEntry]
Timings = dict[str, float]

# ---- qt.error ---------------------------------------------------------------------------------------------------


class Error(QtDoc):
    """CLI / API error object (exit code 2)."""

    SCHEMA: ClassVar[str] = "qt.error"
    schema_: Literal["qt.error"] = _tag("qt.error")
    schema_version: Literal[1] = 1
    code: Annotated[str, Field(pattern=r"^QT-[A-Z]+-[0-9]{3}$")]
    message: str
    hint: str | None = None
    details: dict[str, Any]


# ---- datasets ---------------------------------------------------------------------------------------------------


class InputFile(QtModel):
    """Provenance of one ingest input."""

    path: str
    bytes: int
    sha256: Sha256


class PartitionFile(QtModel):
    """One Parquet file of a partition, path relative to the dataset root."""

    path: str
    rows: int
    sha256: Sha256


class PartitionEntry(QtModel):
    """One (instrument, session date) partition; `session_date` in days since 1970-01-01."""

    instrument_id: int
    session_date: int
    files: list[PartitionFile]
    rows: int
    ts_min: int
    ts_max: int


VenueName = Literal["generic", "nasdaq", "cme", "coinbase"]
#: 1.1: rule-set names (`qt_types::RuleSet`): every venue plus `crypto_l3`; 1.2 adds `iex` (IEX DEEP+) and `nyse`
#: (NYSE Group TAQ Integrated Feed).
RuleSetName = Literal["generic", "nasdaq", "cme", "coinbase", "crypto_l3", "iex", "nyse"]
ValidationStatusName = Literal["real_data", "synthetic_only", "assumed", "fixture_only"]


class SourceVintage(QtModel):
    """1.1: data vintage of one source file (DBN metadata; null fields when the format has none)."""

    #: sha256 of the input file (as in `inputs`).
    sha256: Sha256
    #: DBN version of the file.
    dbn_version: int | None = None
    #: Databento dataset code (`XNAS.ITCH`, `GLBX.MDP3`, ...).
    dataset: str | None = None
    #: DBN schema name (`mbo`, ...).
    dbn_schema: str | None = None
    #: Metadata query start, UTC ns.
    start_ns: int | None = None
    #: Metadata query end, UTC ns (exclusive).
    end_ns: int | None = None
    #: Publisher id of the records, when the file holds exactly one.
    publisher_id: int | None = None


class DatasetManifest(QtDoc):
    """`manifest.json` of a dataset directory (SPEC 3.3)."""

    SCHEMA: ClassVar[str] = "qt.dataset_manifest"
    schema_: Literal["qt.dataset_manifest"] = _tag("qt.dataset_manifest")
    schema_version: Literal[1] = 1
    dataset_id: Sha256
    kind: Literal["mbo", "l2"]
    source_format: str
    venue: VenueName
    source_venue: str
    inputs: list[InputFile]
    partitions: list[PartitionEntry]
    price_exp_by_instrument: dict[IdKey, int]
    created_by: str
    validation_status: ValidationStatusName
    #: 1.1: rule set the engines apply (e.g. `crypto_l3`); null = derived from `venue` (v0.1 behaviour).
    rule_set: RuleSetName | None = None
    #: 1.1: data vintage per source file (one entry per input; a list because datasets may have several inputs);
    #: null = not recorded.
    source_vintage: list[SourceVintage] | None = None


class FeedGap(QtModel):
    """A gap in the feed of one instrument."""

    instrument_id: int
    ts_start: int
    ts_end: int
    reason: str


class TradingHaltSpan(QtModel):
    """A span `[ts_start, ts_end)` in which one instrument was not in continuous trading as its feed reported it
    (SPEC 1.2, halt censoring): replay, `why`, the zoo, calibration and features censor shadows there."""

    instrument_id: int
    ts_start: int
    #: The message that resumed trading; the end of the input's session day when trading did not resume.
    ts_end: int
    #: Feed state that opened the span, `<source>_<message type>:<state>` (e.g. `itch_H:P`, `iex_O:O`).
    state: str
    #: Feed reason code (ITCH `Reason`, e.g. `LUDP`); empty when the feed gives none.
    reason: str


class DataQuality(QtDoc):
    """Data-quality counters and verdict (SPEC 5.1.5)."""

    SCHEMA: ClassVar[str] = "qt.data_quality"
    schema_: Literal["qt.data_quality"] = _tag("qt.data_quality")
    schema_version: Literal[1] = 1
    records: int
    maybe_bad_book_records: int
    bad_ts_recv_records: int
    snapshot_records: int
    pre_snapshot_rows_skipped: int
    unknown_order_refs: int
    duplicate_adds: int
    unexpected_modifies: int
    sequence_gaps: int
    sequence_regressions: int
    out_of_order_ts_event: int
    crossed_books_after_last: int
    hidden_prints: int
    off_price_executions: int
    feed_gaps: list[FeedGap]
    #: 1.2: spans in which an instrument was not in continuous trading (venue state, never part of the verdict);
    #: omitted when empty.
    trading_halts: list[TradingHaltSpan] = Field(default_factory=list)
    source_specific: dict[str, int]
    verdict: Literal["clean", "warnings", "unusable"]
    reasons: list[str]

    @model_serializer(mode="wrap")
    def _omit_empty_halts(self, handler: SerializerFunctionWrapHandler):
        """As the engine writes it: `trading_halts` is left out when empty, so a document without halts dumps the
        same keys as before schema 1.2 and as `data_quality.json` on disk. No return annotation: pydantic then keeps
        the model's own schema in serialization mode (the HTTP API's OpenAPI document), not a generic object."""
        out = handler(self)
        if not out.get("trading_halts"):
            out.pop("trading_halts", None)
        return out


class IngestSummary(QtDoc):
    """Summary of `ingest`."""

    SCHEMA: ClassVar[str] = "qt.ingest_summary"
    schema_: Literal["qt.ingest_summary"] = _tag("qt.ingest_summary")
    schema_version: Literal[1] = 1
    spec: dict[str, Any]
    manifest: DatasetManifest
    data_quality: DataQuality
    files: Files
    timings: Timings | None = None
    #: 1.1: ingest warnings (v0.1 producers: empty).
    warnings: list[str] = Field(default_factory=list)


# ---- replay -----------------------------------------------------------------------------------------------------


class ReplayStats(QtModel):
    """Replay record / event counters."""

    records: int
    events: int
    trades: int
    instruments: int
    partitions: int
    snapshot_records: int


class Invariants(QtModel):
    """The 8 book invariant counters (SPEC 3.5)."""

    cancels_exceeding_resting: int
    fills_exceeding_resting: int
    fills_unknown_order: int
    unknown_order_refs: int
    duplicate_adds: int
    unexpected_modifies: int
    crossed_books_after_last: int
    negative_spreads: int


class RankWindow(QtModel):
    """Rank-oracle statistics over the first 60 s after the open."""

    checked: int
    rank_violations: int
    rank_violations_sweep_adjusted: int
    rank0_share: Fraction
    rank0_share_sweep_adjusted: Fraction


class RankBucket(QtModel):
    """Rank-oracle statistics of one 30-minute local bucket."""

    checked: int
    rank0: int
    rank0_sweep_adjusted: int
    rank0_share: Fraction
    rank0_share_sweep_adjusted: Fraction


class RankOracle(QtModel):
    """Rank-oracle summary (SPEC 5.1.4, et semantics)."""

    executions_checked: int
    executions_unknown_order: int
    price_priority_violations: int
    fifo_rank_histogram: Annotated[list[int], Field(min_length=6, max_length=6)]
    fifo_rank_histogram_sweep_adjusted: Annotated[list[int], Field(min_length=6, max_length=6)]
    rank0_share: Fraction
    rank0_share_sweep_adjusted: Fraction
    sweeps: int
    sweeps_multi_fill: int
    sweep_gap_ns: int
    first_60s_after_open: RankWindow
    rank0_share_by_bucket: dict[Annotated[str, Field(pattern=r"^[0-9]{2}:[0-9]{2}$")], RankBucket]


class Parity(QtModel):
    """Trade and BBO parity against source data (SPEC 5.1.7)."""

    trades_compared: int
    trades_mismatches: int
    trades_missing_derived: int
    trades_extra_derived: int
    bbo_compared: int
    bbo_mismatches: int
    first_mismatches: list[dict[str, Any]]
    pass_: bool = Field(alias="pass")


class ShadowCounts(QtModel):
    """Shadow outcome counters."""

    n: int
    n_live: int
    n_filled: int
    n_partial_only: int
    n_cancelled: int
    n_censored: int
    n_rejected: int
    fills_rows: int
    trajectory_rows: int


class ShadowDiagnostics(QtModel):
    """1.1 (3.11 #20): shadow fill-rule diagnostics of a replay (audit rulings A1, A3, A5)."""

    hidden_print_fills: int
    hidden_at_px_with_display_ahead: int
    taker_fills: int
    crossed_unfilled: int
    behind_exec_with_ahead: int
    excluded_requested: int
    excluded_matched: int
    excluded_records: int
    #: Excluded orders replaced by their own shadow and kept on the tape (A1 twin linking; additive, default 0).
    twins_linked: int = 0
    #: Twin executions credited while the shadow's displayed queue ahead was still positive (additive, default 0).
    twin_fills_with_queue_ahead: int = 0
    #: Twin executions at a price other than the twin's displayed price (additive, default 0).
    twin_fills_off_display: int = 0


class ReplayTimings(QtModel):
    """Wall-clock timings (excluded from determinism hashes)."""

    wall_s: float
    records_per_s: float | None


class ReplaySummary(QtDoc):
    """Summary of `replay` (SPEC 5.1.6)."""

    SCHEMA: ClassVar[str] = "qt.replay_summary"
    schema_: Literal["qt.replay_summary"] = _tag("qt.replay_summary")
    schema_version: Literal[1] = 1
    run_id: RunId
    engine_version: str
    spec: dict[str, Any]
    stats: ReplayStats
    invariants: Invariants
    rank_oracle: RankOracle | None = None
    parity: Parity | None = None
    shadows: ShadowCounts | None = None
    data_quality: DataQuality
    files: Files
    warnings: list[str]
    timings: ReplayTimings | None = None
    #: 1.1 (3.11 #20); null = not reported (v0.1: one `warnings` line).
    shadow_diagnostics: ShadowDiagnostics | None = None


class ShadowSampleSummary(QtDoc):
    """Summary of `shadows sample`, `shadows mirror-real` and `shadows from-live`."""

    SCHEMA: ClassVar[str] = "qt.shadow_sample_summary"
    schema_: Literal["qt.shadow_sample_summary"] = _tag("qt.shadow_sample_summary")
    schema_version: Literal[1] = 1
    spec: dict[str, Any]
    n: int
    by_instrument: dict[IdKey, int]
    by_placement: dict[IdKey, int]
    files: Files


# ---- zoo --------------------------------------------------------------------------------------------------------


class McNemarSmall(QtModel):
    """Discordant counts and exact McNemar p-value."""

    b: int
    c: int
    p_exact: float | None


class LeaderboardEntry(QtModel):
    """One model's metrics vs exact truth (SPEC 5.6)."""

    model_id: str
    brier: HMap
    logloss: HMap
    fill_rate_bias: dict[HorizonKey, CiValue]
    overfill_bias: CiValue
    qty_bias: float | None
    ttf_log_ratio_median: float | None = None
    ttf_iad: float | None
    qtrack_mae: float | None = None
    mcnemar: McNemarSmall
    #: 0.1.1 (additive): exact fill rate by horizon, the truth Aalen-Johansen CIF of the leaderboard population (the
    #: same for every model); `fill_rate_bias = model_fill_rate - exact_fill_rate`.
    exact_fill_rate: HMap = Field(default_factory=dict)
    #: 0.1.1 (additive): the model's fill rate by horizon, its mean forecast over the label-eligible rows.
    model_fill_rate: HMap = Field(default_factory=dict)
    #: additive (selection-stats): mean per-shadow first-fill error by horizon,
    #: `|min(T_model, h) - min(T_exact, h)| / h` with `T = ts_first_fill - ts_live` (null = never), a share of the
    #: horizon, on the leaderboard rows.
    first_fill_error: HMap = Field(default_factory=dict)
    #: additive (selection-stats): in the 90% model confidence set for first-fill error at the horizon (Hansen, Lunde,
    #: Nason 2011, `T_R`, wild cluster bootstrap-t over the leaderboard blocks): statistically tied with the best
    #: model; null where undefined (order-level reference, no rows, or fewer than 5 bootstrap blocks).
    mcs_member: dict[HorizonKey, bool | None] = Field(default_factory=dict)
    #: additive (selection-stats): the MCS p-value for first-fill error (member iff `mcs_p > 0.10`); resolution
    #: `1 / bootstrap_b`, so 0 means below `1 / bootstrap_b`, not certainty.
    mcs_p: HMap = Field(default_factory=dict)
    #: additive (selection-stats): in the 90% model confidence set for the Brier loss at the horizon.
    mcs_brier_member: dict[HorizonKey, bool | None] = Field(default_factory=dict)
    #: additive (selection-stats): the MCS p-value for the Brier loss (resolution `1 / bootstrap_b`).
    mcs_brier_p: HMap = Field(default_factory=dict)


class ZooSummary(QtDoc):
    """Summary of `zoo`; the Rust runner leaves `leaderboard` empty (filled by `queuetruth.eval`)."""

    SCHEMA: ClassVar[str] = "qt.zoo_summary"
    schema_: Literal["qt.zoo_summary"] = _tag("qt.zoo_summary")
    schema_version: Literal[1] = 1
    run_id: RunId
    spec: dict[str, Any]
    n_shadows: int
    models: list[str]
    leaderboard: list[LeaderboardEntry]
    files: Files
    warnings: list[str]
    #: 1.1 (additive): faithful hbt #312 aborts per model id, the number of (model, shadow) pairs stopped by the
    #: `InvalidOrderStatus` event (their prediction rows from the abort on carry status `hbt_312_abort`); models
    #: without an abort are absent. The `hbt_312_abort: ` warning lines stay.
    hbt_312_aborts: dict[str, int] = Field(default_factory=dict)
    #: 0.1.1 (additive): shadows left out of every model's metrics because at least one faithful hbt pair aborted on
    #: them at #312, so that every model is scored on one common population.
    n_excluded_hbt_312: int = 0
    #: 0.1.1 (additive): never-cancel shadows with an exact label in the leaderboard population (the same for every
    #: model); null from the Rust runner, which does not evaluate.
    n_leaderboard: int | None = None


# ---- shadow impact (docs/concepts/impact-warning.md; queuetruth.eval.impact) -------------------------------------


class ImpactConfig(QtModel):
    """Coefficients and thresholds of `queuetruth.eval.impact` (defaults and references:
    docs/concepts/impact-warning.md). Flags are inclusive (`>=`)."""

    #: `Y` of the square-root law (order one in Toth et al. 2011 and Bouchaud et al. 2018, ch. 12).
    coefficient: float = Field(default=1.0, ge=0.0)
    #: Below this daily participation `Q / V` the impact is continued linearly (0 = pure square root).
    linear_below: float = Field(default=1e-5, ge=0.0, lt=1.0)
    #: Realized-variance sampling interval (5 minutes: Liu, Patton and Sheppard 2015).
    rv_interval_ns: int = Field(default=300_000_000_000, gt=0)
    #: Fewest grid returns for a `sigma_daily` (12 = one hour of 5-minute returns).
    min_returns: int = Field(default=12, ge=1)
    impact_ticks: float = Field(default=1.0, gt=0.0)
    #: Also flag impact computed from the venue's own session volume (an upper bound; off by default).
    flag_venue_volume_impact: bool = False
    queue_share: float = Field(default=0.10, gt=0.0, le=1.0)
    #: Smallest posted quantity the queue-share flag considers (dataset size units); null = the instrument's round
    #: lot (100 shares on US equity venues, else `lot_size`).
    queue_min_qty: int | None = Field(default=None, ge=0)
    volume_share: float = Field(default=0.05, gt=0.0, le=1.0)
    warn_fraction: float = Field(default=0.01, ge=0.0, le=1.0)


class ImpactDistribution(QtModel):
    """Quantiles (numpy linear) of one per-shadow figure over the assessed shadows where it is defined."""

    n: int
    p50: float | None
    p90: float | None
    p99: float | None
    max: float | None


class ImpactSensitivity(QtModel):
    """Assessed shadows at or above one threshold of a figure."""

    threshold: float
    n_flagged: int


class ImpactDay(QtModel):
    """Daily figures of one instrument-day and its shadows."""

    instrument_id: int
    session_date: str
    daily_volume: int | None
    sigma_daily: float | None
    volume_source: Literal["venue_session", "user"]
    n_shadows: int
    n_material: int


class ShadowImpactSummary(QtDoc):
    """`qt.shadow_impact_summary`: counts, distributions and the warning line of one `qt.shadow_impact` table."""

    SCHEMA: ClassVar[str] = "qt.shadow_impact_summary"
    schema_: Literal["qt.shadow_impact_summary"] = _tag("qt.shadow_impact_summary")
    schema_version: Literal[1] = 1
    #: What the shadows came from: a replay (`qt.shadow_results`) or a zoo run (`qt.zoo_predictions`).
    source: Literal["replay", "zoo"]
    config: ImpactConfig
    volume_source: Literal["venue_session", "user", "mixed"]
    #: Zoo: the evaluation-window cap asked for and the largest prediction horizon used as the life (null: replay).
    h_max_ns: int | None
    horizon_ns: int | None
    #: The shadows' own fills are already in the trades (self-excluded twins), so `volume_share` = filled / life.
    fills_in_tape: bool
    dataset: str | None
    inputs: Files
    files: Files
    n_shadows: int
    #: Shadows that never rested (`rejected_*`) and shadows live outside the regular session: never flagged.
    n_rejected: int
    n_out_of_session: int
    n_assessed: int
    n_evaluable_impact: int
    n_evaluable_queue: int
    n_evaluable_volume: int
    n_flag_impact: int
    #: Assessed shadows at or above the impact threshold on venue volume only (an upper bound; not flagged unless
    #: `flag_venue_volume_impact`).
    n_impact_venue_bound: int
    n_flag_queue: int
    #: Assessed shadows at or above the queue-share threshold whose size is below `queue_min_qty` (not flagged).
    n_queue_below_min_qty: int
    n_flag_volume: int
    #: Assessed shadows alone at their price at live (`level_qty_at_live = 0`).
    n_alone_at_level: int
    n_material: int
    material_share: float | None
    material: bool
    distributions: dict[str, ImpactDistribution]
    sensitivity: dict[str, list[ImpactSensitivity]]
    by_instrument: dict[IdKey, int]
    days: list[ImpactDay]
    warning: str | None


# ---- zoo toxicity (docs/design/extensions/fill-toxicity.md) -------------------------------------------------------

#: Fill mechanisms of `qt.zoo_toxicity` (`queuetruth.eval.toxicity.MECHANISMS`) plus the roll-up `all`.
ToxMechanism = Literal[
    "queue", "swept", "cross_on_arrival", "crossed_resting", "hidden_print", "touch", "particle", "all"
]


class ToxStat(QtModel):
    """Ratio-of-sums estimate with its 95% block-bootstrap percentile CI; `n` = rows (shadows or fills) with weight
    in the denominator. `value` is null when the denominator is 0."""

    value: float | None
    ci_lo: float | None
    ci_hi: float | None
    n: int


class ToxicityHorizon(QtModel):
    """One model at one markout horizon: per-shadow first-fill markouts (unit quantity) over the `n` shadows whose
    first-fill markouts are defined in every arm that filled them. `*_ticks` / `*_bps` sums are per shadow of the
    population (`bias = grant + timing + deny = model_pnl - exact_pnl`, positive = the model overstates PnL); the
    `*_mo_*` / `*_mm_*` entries are means over the shadows of one status (fill-price and mid-to-mid markouts)."""

    n: int
    model_pnl_ticks: ToxStat
    exact_pnl_ticks: ToxStat
    grant_ticks: ToxStat
    deny_ticks: ToxStat
    timing_ticks: ToxStat
    bias_ticks: ToxStat
    grant_bps: ToxStat
    deny_bps: ToxStat
    timing_bps: ToxStat
    bias_bps: ToxStat
    phantom_mo_ticks: ToxStat
    missed_mo_ticks: ToxStat
    confirmed_mo_ticks: ToxStat
    confirmed_exact_mo_ticks: ToxStat
    phantom_mm_ticks: ToxStat
    missed_mm_ticks: ToxStat
    confirmed_mm_ticks: ToxStat
    confirmed_exact_mm_ticks: ToxStat


class ToxicityModel(QtModel):
    """Shadow-level discordance of one model against the exact arm within the fill window, and its markouts."""

    model_id: str
    family: str
    n_shadows: int
    n_model_filled: int
    n_exact_filled: int
    n_confirmed: int
    n_phantom: int
    n_missed: int
    #: cross-check of the forecast against the fill rows: non-particle pairs whose p_fill of 0 or 1 at the window
    #: disagrees with the presence of a fill row (the rows decide the status), and filled particle pairs without a
    #: median first fill in the window (no markout)
    n_inconsistent: int
    phantom_rate: float | None
    missed_rate: float | None
    horizons: dict[HorizonKey, ToxicityHorizon]


class TypeMarkouts(QtModel):
    """Quantity-weighted mean markouts of the fill rows of one type at one horizon."""

    mo_ticks: ToxStat
    mo_bps: ToxStat
    mm_ticks: ToxStat
    mm_bps: ToxStat


class FillTypeStats(QtModel):
    """Fill rows of one arm (`exact` or a model id), status and mechanism; `missed` rows are the exact arm's fills of
    the shadows the model did not fill (their mechanism is the exact one)."""

    arm: str
    status: Literal["confirmed", "phantom", "missed"] | None
    mechanism: ToxMechanism
    n_fills: int
    n_shadows: int
    qty: float
    markouts: dict[HorizonKey, TypeMarkouts]


class ToxGap(QtModel):
    """Paired difference `|value| - |next value|` of two neighbouring places of a ranking, with its 95% percentile CI
    from the shared bootstrap replicates."""

    value: float
    ci_lo: float | None
    ci_hi: float | None


class ToxicityRank(QtModel):
    """One place of a ranking: rank 1 = the largest |value| (the largest PnL distortion of that kind); `value` keeps
    its sign. `rank_lo`..`rank_hi` is the 95% rank interval over the bootstrap replicates (null when the value is
    undefined); `gap_to_next` is the paired gap to the next place and `separated_from_next` whether its CI lies above
    0 (null on the last place or next to an undefined value)."""

    rank: int
    model_id: str
    value: float | None
    ci_lo: float | None
    ci_hi: float | None
    rank_lo: int | None
    rank_hi: int | None
    gap_to_next: ToxGap | None
    separated_from_next: bool | None


class ToxicityRankings(QtModel):
    """Models ranked at `horizon_ns` by `|grant_ticks|` (fills wrongly granted), `|deny_ticks|` (fills wrongly denied)
    and `|bias_ticks|`, largest first; undefined values last, ties in model order."""

    horizon_ns: int
    grant: list[ToxicityRank]
    deny: list[ToxicityRank]
    bias: list[ToxicityRank]


class ToxicityExact(QtModel):
    """The exact arm's fills in the window over the population."""

    n_filled: int
    n_fills: int
    fills_by_mechanism: dict[str, int]


class ZooToxicity(QtDoc):
    """Summary of `zoo-toxicity`: fill-type tags, markouts and the adverse-selection ranking of the zoo's models
    (extension fill-toxicity; Python only)."""

    SCHEMA: ClassVar[str] = "qt.zoo_toxicity"
    schema_: Literal["qt.zoo_toxicity"] = _tag("qt.zoo_toxicity")
    schema_version: Literal[1] = 1
    run_id: RunId
    engine_version: str
    spec: dict[str, Any]
    zoo_run_id: str | None
    zoo_mode: str | None
    n_shadows: int
    n_excluded: dict[str, int]
    #: share of the checked shadows whose replay did not reproduce the zoo's exact arm (non-zero only with
    #: `allow_truth_mismatch`); null when no shadow was checked
    truth_mismatch_rate: float | None
    window_ns: int
    horizons_ns: list[int]
    ci_block: Literal["day", "30min"]
    n_blocks: int
    bootstrap_b: int
    exact: ToxicityExact
    models: list[ToxicityModel]
    by_type: list[FillTypeStats]
    rankings: ToxicityRankings
    files: Files
    warnings: list[str]


# ---- calibration ------------------------------------------------------------------------------------------------


class HeldOut(QtModel):
    """Held-out metrics of one model."""

    brier: HMap
    logloss: HMap
    fill_rate_bias: HMap


class StratumEstimate(QtModel):
    """Parameter estimate in one stratum."""

    stratum_kind: str
    stratum: str
    estimate: float | None
    ci_lo: float | None = None
    ci_hi: float | None = None
    n: int


class Quantiles(QtModel):
    """Latency quantiles in ns."""

    p10: float | None
    p50: float | None
    p90: float | None
    p99: float | None
    n: int


class LatencyFit(QtModel):
    """Latency distributions from a live log."""

    entry_ns: Quantiles
    response_ns: Quantiles
    cancel_ns: Quantiles
    amend_ns: Quantiles
    clock_sync_assumed: bool


class ExportSnippet(QtModel):
    """Paste-ready config text with notes."""

    python: str
    notes: list[str]


class Exports(QtModel):
    """Config exports; all members are null in the Rust-only `fit.json`."""

    hftbacktest: ExportSnippet | None = None
    nautilus: ExportSnippet | None = None
    latency_npz: str | None = None


class DataSource(QtModel):
    """Provenance of the data a calibration used."""

    dataset_id: str
    venue: str
    source_venue: str
    validation_status: ValidationStatusName


class BlockWindowEcho(QtModel):
    """Block selection echo."""

    index: list[int]
    n_blocks: int


class WindowEcho(QtModel):
    """Train/test window echo (dates or blocks)."""

    dates: list[str] | None = None
    blocks: BlockWindowEcho | None = None


class HeldOutDiscordance(QtModel):
    """1.1 (3.11 #10): order-level held-out discordance of `calibrate-live` at the window H (model filled =
    `p_fill(H) >= 0.5`): exact and cluster-robust McNemar and the fill-rate bias with its block-bootstrap CI."""

    n: int
    b: int
    c: int
    fill_rate_bias: float | None
    ci_lo: float | None
    ci_hi: float | None
    p_exact: float | None
    p_clustered: float | None
    clustered_method: str | None
    n_clusters: int


class CalibrationTrust(QtModel):
    """1.1: structured form of the `trust:` warning lines (`queuetruth.export.trust`, `qt_calib::trust`)."""

    #: `exportable`, or `refused` (`queuetruth export` raises `QT-CALIB-001` unless forced).
    verdict: Literal["exportable", "refused"]
    #: Refusing tokens in `queuetruth.export.trust.REFUSING` order (empty when exportable).
    reasons: list[str]
    #: Every `trust:` diagnostic token present (`at_grid_edge`, `plateau`, `profile_ci_open`, ...), sorted.
    flags: list[str]
    #: Held-out comparison with the default model per horizon (ns, decimal string).
    beats_default: dict[HorizonKey, Literal["better", "tie", "worse", "mixed", "n/a"]]


class CalibrationResult(QtDoc):
    """Result of `calibrate` (SPEC 5.4)."""

    SCHEMA: ClassVar[str] = "qt.calibration_result"
    schema_: Literal["qt.calibration_result"] = _tag("qt.calibration_result")
    schema_version: Literal[1] = 1
    run_id: RunId
    spec: dict[str, Any]
    #: 1.1 (3.11 #10) adds `live` (`calibrate-live`; as built it reports the family's kind).
    kind: Literal["alloc", "survival", "nt_touch", "latency", "live"]
    family: str
    mode: str
    param_name: str | None = None
    estimate: float | None = None
    ci_profile: tuple[float | None, float | None] | None = None
    ci_bootstrap: tuple[float | None, float | None] | None = None
    loglik: float | None = None
    n_events: int
    n_shadows: int
    train: WindowEcho
    test: WindowEcho
    held_out: dict[str, HeldOut]
    baseline_held_out: dict[str, HeldOut]
    stratified: list[StratumEstimate]
    latency: LatencyFit | None = None
    exports: Exports
    data_sources: list[DataSource]
    warnings: list[str]
    #: 1.1 (3.11 #10); null = not a live calibration (v0.1: `stratified` rows with `stratum_kind = held_out_test`).
    held_out_discordance: HeldOutDiscordance | None = None
    #: 1.1: export trust assessment; null = not assessed (v0.1: `trust:` lines in `warnings`, which stay).
    trust: CalibrationTrust | None = None


class LiveCalibrationGridPoint(QtModel):
    """Train log-likelihood at one grid value."""

    theta: float
    value: float
    loglik: float


class LiveCalibrationTest(QtDoc):
    """1.1 (3.11 #11): `live_calibration_test.json` of `calibrate-live` (fitted model, grid profile, held-out
    discordance; same fields as `HeldOutDiscordance`)."""

    SCHEMA: ClassVar[str] = "qt.live_calibration_test"
    schema_: Literal["qt.live_calibration_test"] = _tag("qt.live_calibration_test")
    schema_version: Literal[1] = 1
    model_id: str
    grid: list[LiveCalibrationGridPoint]
    n: int
    b: int
    c: int
    fill_rate_bias: float | None
    ci_lo: float | None
    ci_hi: float | None
    p_exact: float | None
    p_clustered: float | None
    clustered_method: str | None
    n_clusters: int


# ---- features and learned model ---------------------------------------------------------------------------------


class CauseCounts(QtModel):
    """Rows by outcome cause."""

    fill: int
    cancel: int
    censor: int


class FeatureSummary(QtDoc):
    """Summary of `features`."""

    SCHEMA: ClassVar[str] = "qt.feature_summary"
    schema_: Literal["qt.feature_summary"] = _tag("qt.feature_summary")
    schema_version: Literal[1] = 1
    spec: dict[str, Any]
    n_orders: int
    n_rows: int
    by_cause: CauseCounts
    feature_names: list[str]
    privileged_names: list[str]
    files: Files


class SplitEcho(QtModel):
    """Split scheme and the dates/blocks of each part."""

    scheme: str
    train: Any
    calib: Any
    test: Any


class SplitCounts(QtModel):
    """Orders per split."""

    train: int
    calib: int
    test: int


class HazardScores(QtModel):
    """Test-split scores (SPEC 5.5.7); `c_index_uno` and `auc` are discrimination only (improper)."""

    rcll: float | None
    ipcw_logloss: HMap
    brier: HMap
    bss_vs_climatology: dict[HorizonKey, CiValue]
    ibs: float | None
    calib_slope: HMap
    calib_intercept: HMap
    spiegelhalter_z: HMap
    hl_p: HMap
    c_index_uno: float | None
    auc: HMap
    #: 1.1 (3.11 #22): Hosmer-Lemeshow p with 8 df (et parity) next to `hl_p`; null = not reported.
    hl_p_et: HMap | None = None


class ZooBrier(QtModel):
    """Brier of a zoo model on the same test rows."""

    brier: HMap


class HazardModelCard(QtDoc):
    """`model.json` of `fit-hazard` (SPEC 5.5.8)."""

    SCHEMA: ClassVar[str] = "qt.hazard_model_card"
    schema_: Literal["qt.hazard_model_card"] = _tag("qt.hazard_model_card")
    schema_version: Literal[1] = 1
    model_id: str
    format_version: Literal[1]
    spec: dict[str, Any]
    grid_ns: list[int]
    causes: list[Literal["fill", "cancel"]]
    feature_names: list[str]
    privileged: bool
    split: SplitEcho
    n_orders: SplitCounts
    n_rows_train: int
    scores_test: HazardScores
    zoo_comparison: dict[str, ZooBrier] | None = None
    recalibration: Literal["isotonic", "none"]
    files: Files
    warnings: list[str]


# ---- live logs --------------------------------------------------------------------------------------------------


class LiveInput(QtModel):
    """Provenance of one importer input."""

    path: str
    sha256: Sha256


class LiveCoverage(QtModel):
    """1.1 (3.11 #14): order-source coverage of a live log: orders known only from fills."""

    fills_only: bool
    n_fills_only_orders: int


class UnmappedInstrument(QtModel):
    """1.1: an instrument without an instrument-map entry (its orders carry `instrument_id = null`)."""

    venue: str
    instrument: str
    n_orders: int


class InstrumentScale(QtModel):
    """1.1: price / size exponents of one instrument of a live log (`queuetruth.live.instrument_scales`)."""

    venue: str
    instrument: str
    instrument_id: int | None
    price_exp: int
    size_exp: int
    scale_source: Literal["instrument_map", "default"]
    n_orders: int


class LiveImportSummary(QtDoc):
    """Summary of `live import`."""

    SCHEMA: ClassVar[str] = "qt.live_import_summary"
    schema_: Literal["qt.live_import_summary"] = _tag("qt.live_import_summary")
    schema_version: Literal[1] = 1
    venue: str
    inputs: list[LiveInput]
    n_orders: int
    n_events: int
    n_fills: int
    dedupe_dropped: int
    unmatched_fills: int
    warnings: list[str]
    #: 1.1 (3.11 #15) adds `simulated` (simulator outputs).
    validation_status: Literal["real", "fixture_only", "simulated"]
    files: Files
    #: 1.1 (3.11 #14); null = not reported (v0.1: a `coverage=fills_only` warning).
    coverage: LiveCoverage | None = None
    #: 1.1 (3.11 #14): share of resting limit orders whose entry time fell back; null = not reported / no orders.
    entry_fallback_share: float | None = None
    #: 1.1: instruments without an instrument-map entry (v0.1: the `unmapped_instruments:` warning, which stays).
    unmapped_instruments: list[UnmappedInstrument] = Field(default_factory=list)
    #: 1.1: per-instrument scales of the written log; null = not reported.
    instrument_scales: list[InstrumentScale] | None = None


class LiveManifest(QtDoc):
    """`manifest.json` of a canonical live-log directory (SPEC 3.8)."""

    SCHEMA: ClassVar[str] = "qt.live_manifest"
    schema_: Literal["qt.live_manifest"] = _tag("qt.live_manifest")
    schema_version: Literal[1] = 1
    venues: list[str]
    accounts: list[str]
    n_orders: int
    n_events: int
    instrument_map_sha256: Sha256 | None = None
    created_by: str
    #: 1.1 (3.11 #14); null = not reported.
    coverage: LiveCoverage | None = None
    #: 1.1 (3.11 #14); null = not reported.
    entry_fallback_share: float | None = None


class LiveValidation(QtDoc):
    """Result of `live validate`."""

    SCHEMA: ClassVar[str] = "qt.live_validation"
    schema_: Literal["qt.live_validation"] = _tag("qt.live_validation")
    schema_version: Literal[1] = 1
    path: str
    n_orders: int
    n_events: int
    errors: list[str]
    warnings: list[str]


# ---- scorecard --------------------------------------------------------------------------------------------------


class SimSource(QtModel):
    """Which simulator arm was scored."""

    kind: Literal["external", "exact", "zoo"]
    model_id: str | None = None
    path: str | None = None


class TwoByTwo(QtModel):
    """Per-order fill outcome table."""

    both: int
    neither: int
    live_only: int
    sim_only: int


class Bias(QtModel):
    """Signed fill-rate and quantity bias (sim minus live)."""

    fill_rate_bias: float | None
    ci_lo: float | None
    ci_hi: float | None
    relative_overfill: float | None
    qty_bias: float | None
    qty_bias_ci_lo: float | None
    qty_bias_ci_hi: float | None
    direction: Literal["sim_overfills", "sim_underfills", "no_significant_bias"]


class McNemar(QtModel):
    """McNemar test on the discordant pairs (SPEC 5.7.5)."""

    b: int
    c: int
    chi2_cc: float
    p_chi2: float | None
    p_exact: float | None
    odds_ratio: float | None = None
    or_ci_lo: float | None = None
    or_ci_hi: float | None = None


class DecileRow(QtModel):
    """2x2 table and test within one queue decile."""

    decile: int
    both: int
    neither: int
    live_only: int
    sim_only: int
    p_exact: float | None
    p_adj: float | None


class LiquidityRow(QtModel):
    """2x2 table and test within one liquidity bucket."""

    bucket: int
    both: int
    neither: int
    live_only: int
    sim_only: int
    p_exact: float | None
    p_adj: float | None


class Ttf(QtModel):
    """Time-to-fill comparison."""

    n_joint: int
    median_log_ratio: float | None = None
    ci_lo: float | None = None
    ci_hi: float | None = None
    wilcoxon_p: float | None = None
    paired_perm_p: float | None = None
    logrank_p: float | None = None
    gray_p: float | None = None
    cif_fill: dict[Literal["live", "sim"], HMap]


class MarkoutRow(QtModel):
    """Mean markout of one arm at one horizon."""

    horizon_ns: int
    ref: Literal["mid", "microprice"]
    arm: Literal["live", "sim"]
    n: int
    mean_ticks: float | None
    ci_lo: float | None
    ci_hi: float | None


class MarkoutDiffRow(QtModel):
    """Paired first-fill markout difference (sim minus live)."""

    horizon_ns: int
    ref: Literal["mid", "microprice"]
    n: int
    mean_diff_ticks: float | None
    ci_lo: float | None
    ci_hi: float | None
    p_adj: float | None


class DiscordantMarkoutRow(QtModel):
    """Markouts of sim-only vs live-only fills."""

    horizon_ns: int
    sim_only_mean: float | None
    live_only_mean: float | None
    diff: float | None
    ci_lo: float | None
    ci_hi: float | None


class Equivalence(QtModel):
    """TOST equivalence results."""

    fill_rate_margin: float
    tost_p: float | None
    fill_rate_equivalent: bool
    markout_margin_ticks: float
    markout_horizon_ns: int
    markout_tost_p: float | None
    markout_equivalent: bool


class Fdr(QtModel):
    """Multiple-testing summary."""

    method: Literal["bh", "by"]
    q: float
    n_tests: int
    n_rejected: int


class Verdict(QtModel):
    """Headline and gates."""

    headline: str
    signed_bias: float | None
    gates_passed: bool
    failed_gates: list[str]


class DiscordanceClustered(QtModel):
    """1.1 (3.11 #9): the cluster-robust primary discordance test (audit ruling A10)."""

    method: str
    estimate: float | None
    ci_lo: float | None
    ci_hi: float | None
    p_value: float | None
    n_clusters: int
    n_clusters_discordant: int
    design_effect: float | None
    n_effective: float | None


class ArmCounts(QtModel):
    """A count per scorecard arm."""

    live: int
    sim: int


class ScorecardSummary(QtDoc):
    """Summary of `score` (SPEC 5.7)."""

    SCHEMA: ClassVar[str] = "qt.scorecard_summary"
    schema_: Literal["qt.scorecard_summary"] = _tag("qt.scorecard_summary")
    schema_version: Literal[1] = 1
    run_id: RunId
    spec: dict[str, Any]
    live_source: str
    sim_source: SimSource
    n_orders: int
    n_matched: int
    n_unmatched_live: int
    n_unmatched_sim: int
    n_overlap_same_px: int
    two_by_two: TwoByTwo
    bias: Bias
    mcnemar: McNemar
    by_queue_decile: list[DecileRow]
    by_liquidity: list[LiquidityRow]
    ttf: Ttf
    markouts: list[MarkoutRow]
    markout_diff: list[MarkoutDiffRow]
    sim_only_vs_live_only_markout: list[DiscordantMarkoutRow]
    equivalence: Equivalence
    fdr: Fdr
    verdict: Verdict
    data_quality: DataQuality | None = None
    files: Files
    warnings: list[str]
    #: 1.1 (3.11 #9): live orders without an MBO twin (self-exclusion); null = not reported.
    n_twin_unmatched: int | None = None
    #: 1.1 (3.11 #9): share of live orders matched to an MBO twin; null = not reported / not applicable.
    twin_match_rate: float | None = None
    #: 1.1 (3.11 #9); null = not reported (v0.1: `qt.score_tests` row `discordance_clustered` + `clusters:` warning).
    discordance_clustered: DiscordanceClustered | None = None
    #: 1.1 (3.11 #9): taker fills per arm, excluded from maker statistics (A5); null = not reported.
    n_taker_fills: ArmCounts | None = None
    #: 1.1 (3.11 #9): the live log is fills-only; null = not reported (v0.1: `fills_only:` warning).
    fills_only: bool | None = None


# ---- referee, why, report, export, misc -------------------------------------------------------------------------


class SimulatorEcho(QtModel):
    """Simulator under test."""

    name: str
    version: str | None = None


class Discordant(QtModel):
    """Discordant fill outcomes."""

    sim_only: int
    exact_only: int


class ToleranceEcho(QtModel):
    """Referee pass thresholds (echo)."""

    max_discordant: int
    max_qty_mismatch: int
    max_abs_ttf_err_ns: int


class RefereeExample(QtModel):
    """One disagreeing order."""

    order_key: str
    exact: dict[str, Any]
    sim: dict[str, Any]


class RefereeReport(QtDoc):
    """Result of `referee run` (SPEC 5.8)."""

    SCHEMA: ClassVar[str] = "qt.referee_report"
    schema_: Literal["qt.referee_report"] = _tag("qt.referee_report")
    schema_version: Literal[1] = 1
    spec: dict[str, Any]
    simulator: SimulatorEcho
    n_orders: int
    n_both_filled: int
    discordant: Discordant
    qty_mismatch: int
    max_abs_ttf_err_ns: int | None = None
    ttf_err_p50_ns: int | None = None
    by_reason: dict[str, int]
    tolerance: ToleranceEcho
    pass_: bool = Field(alias="pass")
    examples: Annotated[list[RefereeExample], Field(max_length=20)]


class WhySubject(QtModel):
    """The traced order."""

    shadow_id: str | None = None
    order_key: str | None = None


class WhyOutcome(QtModel):
    """Outcome of the traced order."""

    filled_qty: int
    ts_first_fill: int | None = None
    end_state: str


SideStr = Literal["B", "A", "N"]


class WhyEvent(QtModel):
    """One record at the order's level, with queue state after it."""

    ts_event: int
    sequence: int
    action: Literal["A", "C", "M", "R", "T", "F", "N"]
    order_id: int
    side: SideStr
    price: int | None
    size: int
    effect: str
    is_ahead: bool
    queue_ahead_after: int
    rank_after: int
    level_qty_after: int
    fill_qty: int
    note: str | None = None


class WhyDivergence(QtModel):
    """1.1: first event after which the model's and the exact queue ahead differ."""

    index: Annotated[int, Field(ge=0)]
    ts_event: int


class WhyTrace(QtDoc):
    """Event-by-event trace of one shadow or live order."""

    SCHEMA: ClassVar[str] = "qt.why_trace"
    schema_: Literal["qt.why_trace"] = _tag("qt.why_trace")
    schema_version: Literal[1] = 1
    subject: WhySubject
    instrument_id: int
    side: SideStr
    price: int | None
    qty: int
    ts_live: int | None = None
    outcome: WhyOutcome
    events: list[WhyEvent]
    truncated: bool
    #: 1.1: zoo model compared with the exact trace; null = no model (v0.1).
    model_id: str | None = None
    #: 1.1: the model's queue-ahead estimate as of each event, aligned with `events` (null entries before the model's
    #: first estimate); null = no model. Kept at trace level (not on `WhyEvent`) so the Rust `WhyEvent` literal in
    #: qt_replay keeps compiling.
    model_queue_ahead: list[float | None] | None = None
    #: 1.1: first divergence of model and exact queue ahead; null = none or no model.
    first_divergence: WhyDivergence | None = None
    #: 1.1: one plain-language sentence; null = not produced (v0.1).
    conclusion: str | None = None
    #: 1.1: caveats (v0.1 producers: empty).
    warnings: list[str] = Field(default_factory=list)


class ReportSummary(QtDoc):
    """Summary of `report`."""

    SCHEMA: ClassVar[str] = "qt.report_summary"
    schema_: Literal["qt.report_summary"] = _tag("qt.report_summary")
    schema_version: Literal[1] = 1
    inputs: list[str]
    out: str
    sections: list[str]
    bytes: int
    sha256: Sha256


class ExportSummary(QtDoc):
    """Summary of `export`."""

    SCHEMA: ClassVar[str] = "qt.export_summary"
    schema_: Literal["qt.export_summary"] = _tag("qt.export_summary")
    schema_version: Literal[1] = 1
    target: Literal["hftbacktest", "nautilus", "l2_npz", "latency_npz"]
    files: Files
    text: str | None = None


class FixtureEntry(QtModel):
    """One written fixture directory."""

    name: str
    path: str
    files: Files


class FixturesSummary(QtDoc):
    """Summary of `referee fixtures`."""

    SCHEMA: ClassVar[str] = "qt.fixtures_summary"
    schema_: Literal["qt.fixtures_summary"] = _tag("qt.fixtures_summary")
    schema_version: Literal[1] = 1
    out: str
    fixtures: list[FixtureEntry]


class Doctor(QtDoc):
    """Result of `doctor`."""

    SCHEMA: ClassVar[str] = "qt.doctor"
    schema_: Literal["qt.doctor"] = _tag("qt.doctor")
    schema_version: Literal[1] = 1
    python: str
    platform: str
    machine: str
    core_version: str | None
    schemas_ok: bool
    postgres: str | None = None
    docker: str | None = None
    problems: list[str]


class Bench(QtDoc):
    """Result of `bench replay`."""

    SCHEMA: ClassVar[str] = "qt.bench"
    schema_: Literal["qt.bench"] = _tag("qt.bench")
    schema_version: Literal[1] = 1
    data: str
    records: int
    wall_s: list[float]
    records_per_s_median: float | None


# ---- service documents (SPEC 3.10 last rows and 4.5) ------------------------------------------------------------


class Progress(QtModel):
    """Job progress."""

    fraction: Annotated[float, Field(ge=0, le=1)]
    stage: str


class ResourceRef(QtModel):
    """The resource a job produces or acts on."""

    type: str
    id: str


class ProblemError(QtModel):
    """One validation error location."""

    loc: list[str | int]
    msg: str


class Problem(QtDoc):
    """RFC 9457 problem details."""

    SCHEMA: ClassVar[str] = "qt.problem"
    schema_: Literal["qt.problem"] = _tag("qt.problem")
    schema_version: Literal[1] = 1
    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str
    request_id: str
    errors: list[ProblemError] | None = None


class Job(QtDoc):
    """A service job."""

    SCHEMA: ClassVar[str] = "qt.job"
    schema_: Literal["qt.job"] = _tag("qt.job")
    schema_version: Literal[1] = 1
    id: str
    kind: Literal["ingest", "replay", "calibration", "scorecard", "report", "dataset_validate", "deletion", "zoo"]
    state: Literal["queued", "running", "succeeded", "failed", "canceled", "expired"]
    progress: Progress
    resource: ResourceRef
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: Problem | None = None
    usage: dict[str, float]
    files_expire_at: str | None = None


class ManifestFile(QtModel):
    """One downloadable job file."""

    name: str
    bytes: int
    sha256: Sha256
    content_type: str
    url: str
    expires_at: str


class FileManifest(QtDoc):
    """Downloadable files of a job."""

    SCHEMA: ClassVar[str] = "qt.file_manifest"
    schema_: Literal["qt.file_manifest"] = _tag("qt.file_manifest")
    schema_version: Literal[1] = 1
    job_id: str
    files: list[ManifestFile]


class Estimate(QtDoc):
    """Cost and time estimate of a job."""

    SCHEMA: ClassVar[str] = "qt.estimate"
    schema_: Literal["qt.estimate"] = _tag("qt.estimate")
    schema_version: Literal[1] = 1
    kind: str
    records_est: int
    shadows_est: int
    cpu_seconds_est: float
    bytes_out_est: int
    cost_units: float
    eta_seconds: float


class Retention(QtModel):
    """Retention in days."""

    uploads_days: Annotated[int, Field(ge=1, le=365)]
    outputs_days: Annotated[int, Field(ge=1, le=365)]


class Tenant(QtDoc):
    """A tenant."""

    SCHEMA: ClassVar[str] = "qt.tenant"
    schema_: Literal["qt.tenant"] = _tag("qt.tenant")
    schema_version: Literal[1] = 1
    id: str
    name: str
    plan: str
    zero_egress: bool
    retention: Retention


Scope = Literal["read", "write", "admin"]


class ApiKey(QtDoc):
    """An API key (secret never included)."""

    SCHEMA: ClassVar[str] = "qt.api_key"
    schema_: Literal["qt.api_key"] = _tag("qt.api_key")
    schema_version: Literal[1] = 1
    id: str
    prefix: str
    scopes: list[Scope]
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


class ApiKeyCreated(QtDoc):
    """A newly created API key with its secret (shown once)."""

    SCHEMA: ClassVar[str] = "qt.api_key_created"
    schema_: Literal["qt.api_key_created"] = _tag("qt.api_key_created")
    schema_version: Literal[1] = 1
    id: str
    prefix: str
    scopes: list[Scope]
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None
    #: Nullable (1.1, 3.11 #19): null in idempotent replays of the create request (E16, E34).
    secret: str | None


class UploadPart(QtModel):
    """One presigned upload part."""

    part_number: int
    url: str
    expires_at: str


class Upload(QtDoc):
    """A multipart upload."""

    SCHEMA: ClassVar[str] = "qt.upload"
    schema_: Literal["qt.upload"] = _tag("qt.upload")
    schema_version: Literal[1] = 1
    id: str
    purpose: Literal["market_data", "live_log", "sim_log", "shadow_spec", "model", "other"]
    filename: str
    size_bytes: int
    sha256: Sha256
    status: Literal["pending", "complete", "verified", "failed"]
    parts: list[UploadPart]
    created_at: str


#: 1.2 `qt.dq_report` check statuses, best to worst (`skip` = not applicable or not run; never worse than `pass`).
DqStatus = Literal["pass", "warn", "fail", "skip"]


class DqEvidence(QtModel):
    """One evidence table of a dq check: the first `max_evidence` rows of `total` (`truncated` when rows were
    dropped). Timestamps are integer ns, prices integer fixed point in the instrument's `price_exp`."""

    name: str
    columns: list[str]
    rows: list[list[Any]]
    total: int
    truncated: bool


class DqCheck(QtModel):
    """One dq check: `status` (`pass`/`warn`/`fail`/`skip`), a one-line `summary`, the numbers behind it
    (`metrics`), evidence tables and caveats (`notes`)."""

    id: str
    title: str
    status: DqStatus
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence: list[DqEvidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DqDataset(QtModel):
    """The dataset checked by `dq`, as its manifest describes it."""

    path: str
    dataset_id: str | None
    kind: str | None
    source_format: str | None
    venue: str | None
    source_venue: str | None
    rule_set: str | None = None
    validation_status: str | None
    partitions: int
    records: int
    session_dates: list[str]
    symbols: list[str]


class DqCounts(QtModel):
    """dq checks per status."""

    pass_: int = Field(alias="pass")
    warn: int
    fail: int
    skip: int


class DqReport(QtDoc):
    """1.2: `qt.dq_report` (`queuetruth dq`, `dq_report.json`): `status` = the worst check status (`pass` when every
    check passed or was skipped); `gate_failed` = the status reached `spec.fail_on`."""

    SCHEMA: ClassVar[str] = "qt.dq_report"
    schema_: Literal["qt.dq_report"] = _tag("qt.dq_report")
    schema_version: Literal[1] = 1
    engine_version: str
    spec: dict[str, Any]
    dataset: DqDataset
    compare: DqDataset | None = None
    status: Literal["pass", "warn", "fail"]
    gate_failed: bool
    counts: DqCounts
    checks: list[DqCheck]
    files: list[FileEntry]
    warnings: list[str] = Field(default_factory=list)


class Dataset(QtDoc):
    """A service dataset."""

    SCHEMA: ClassVar[str] = "qt.dataset"
    schema_: Literal["qt.dataset"] = _tag("qt.dataset")
    schema_version: Literal[1] = 1
    id: str
    name: str
    kind: Literal["mbo", "l2"]
    format: str
    venue: str
    status: Literal["pending", "validating", "ready", "failed", "deleted"]
    upload_ids: list[str]
    manifest: DatasetManifest | None = None
    data_quality: DataQuality | None = None
    created_at: str
    job_id: str | None = None


class UsageRow(QtModel):
    """One metered quantity."""

    key: str
    meter: str
    quantity: float
    unit: str


class UsageReport(QtDoc):
    """Usage over a period."""

    SCHEMA: ClassVar[str] = "qt.usage_report"
    schema_: Literal["qt.usage_report"] = _tag("qt.usage_report")
    schema_version: Literal[1] = 1
    from_: str = Field(alias="from")
    to: str
    group_by: Literal["meter", "day", "job"]
    rows: list[UsageRow]


class Limits(QtModel):
    """Service limits."""

    max_upload_bytes: int
    part_size_bytes: int
    max_jobs_running: int
    rate_limits: dict[str, Any]


class Meta(QtDoc):
    """Service metadata."""

    SCHEMA: ClassVar[str] = "qt.meta"
    schema_: Literal["qt.meta"] = _tag("qt.meta")
    schema_version: Literal[1] = 1
    api_version: Literal["v1"]
    engine_version: str
    schema_versions: dict[str, int]
    limits: Limits
    formats: list[str]
    zero_egress: bool


class Health(QtDoc):
    """Service health."""

    SCHEMA: ClassVar[str] = "qt.health"
    schema_: Literal["qt.health"] = _tag("qt.health")
    schema_version: Literal[1] = 1
    status: Literal["ok", "degraded"]
    db: str
    store: str
    queue: str
    version: str


class Page(QtDoc):
    """One page of a list endpoint."""

    SCHEMA: ClassVar[str] = "qt.page"
    schema_: Literal["qt.page"] = _tag("qt.page")
    schema_version: Literal[1] = 1
    data: list[Any]
    has_more: bool
    next_cursor: str | None


#: Every document model by schema name, in a stable order (`schemas/json/<name>.schema.json`).
DOC_MODELS: dict[str, type[QtDoc]] = {
    m.SCHEMA: m
    for m in (
        Error,
        DatasetManifest,
        DataQuality,
        IngestSummary,
        ReplaySummary,
        ShadowSampleSummary,
        ZooSummary,
        ShadowImpactSummary,
        ZooToxicity,
        CalibrationResult,
        LiveCalibrationTest,
        FeatureSummary,
        HazardModelCard,
        LiveImportSummary,
        LiveManifest,
        ScorecardSummary,
        RefereeReport,
        WhyTrace,
        ReportSummary,
        Job,
        FileManifest,
        Estimate,
        Problem,
        ExportSummary,
        FixturesSummary,
        LiveValidation,
        Doctor,
        Bench,
        DqReport,
        Dataset,
        Upload,
        ApiKey,
        ApiKeyCreated,
        UsageReport,
        Tenant,
        Meta,
        Health,
        Page,
    )
}


# `queuetruth certify` / `verify-cert` / `commit` / `reveal` / `cert keygen` documents (queuetruth.cert.models), the
# conformal report (queuetruth.schemas.conformal), the `queuetruth demo` documents (queuetruth.schemas.demo) and the
# trading-bot reconciliation (queuetruth.schemas.reconcile). The documents of the ledger and sandbox packages are added
# by `queuetruth.schemas` (its `__init__`).
from queuetruth.cert.models import CERT_DOC_MODELS  # noqa: E402
from queuetruth.schemas.conformal import CONFORMAL_DOC_MODELS  # noqa: E402
from queuetruth.schemas.demo import DEMO_DOC_MODELS  # noqa: E402
from queuetruth.schemas.reconcile import RECONCILE_DOC_MODELS  # noqa: E402

DOC_MODELS.update(CERT_DOC_MODELS)
DOC_MODELS.update(CONFORMAL_DOC_MODELS)
DOC_MODELS.update(DEMO_DOC_MODELS)
DOC_MODELS.update(RECONCILE_DOC_MODELS)


def parse_document(data: dict[str, Any]) -> QtDoc:
    """Validates any `qt.*` document by its `schema` field."""
    name = data.get("schema")
    model = DOC_MODELS.get(name) if isinstance(name, str) else None
    if model is None:
        raise ValueError(f"unknown document schema {name!r}")
    return model.model_validate(data)


__all__ = sorted(
    [n for n, v in list(globals().items()) if isinstance(v, type) and issubclass(v, QtModel)]
    + ["DOC_MODELS", "DqStatus", "Files", "HMap", "Timings", "parse_document"]
)
