"""Spec objects of SPEC 4.2 (shared by the Python API, CLI, HTTP API and MCP).

Unknown fields are rejected. Durations are integer ns. `out` and `threads` are execution-only: they are excluded
from `run_id` and from summary echoes (`queuetruth.schemas.spec_echo`). `qt_schema::specs` mirrors every model here
with the same field names, order and defaults.

Schema 1.1 (SPEC 3.11, E38): the fields marked "1.1" are additive. Each default equals the as-built v0.1
behaviour, and a 1.1 field that holds its default is left out of the `run_id` identity document
(`queuetruth.schemas.runid.V1_1_DEFAULTS`), so specs that do not set them keep their v0.1 `run_id`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
DateStr = Annotated[str, Field(pattern=DATE_PATTERN)]
NonNegInt = Annotated[int, Field(ge=0)]


class SpecModel(BaseModel):
    """Base of every spec: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


# ---- references -------------------------------------------------------------------------------------------------


class PathRef(SpecModel):
    """Local path reference (CLI, Python API, MCP local mode)."""

    path: str


class DatasetIdRef(SpecModel):
    """Service dataset id reference (HTTP API)."""

    dataset_id: str


class UploadRef(SpecModel):
    """Service upload id reference (HTTP API)."""

    upload_id: str


class JobFile(SpecModel):
    """One output file of a service job."""

    job_id: str
    name: str


class JobFileRef(SpecModel):
    """Reference to a service job output file."""

    job_file: JobFile


DatasetRef = PathRef | DatasetIdRef
FileRef = PathRef | UploadRef | JobFileRef


# ---- shared building blocks -------------------------------------------------------------------------------------


class LatencyModel(SpecModel):
    """Shadow latency components (SPEC 3.1 semantics)."""

    feed_ns: NonNegInt = 0
    decision_ns: NonNegInt = 0
    gateway_ns: NonNegInt = 0
    jitter_ns: NonNegInt = 0


class L2Config(SpecModel):
    """L2 view synthesis settings (SPEC 5.3.1)."""

    depth_levels: Annotated[int, Field(ge=0, le=65535)] = 10
    cadence_ns: NonNegInt = 0
    trade_position: Literal["before", "after"] = "before"
    include_counts: bool = True
    emit_hidden_trades: bool = True


class BlockWindow(SpecModel):
    """Block selection: equal-duration blocks of each date's continuous session."""

    index: list[NonNegInt]
    n_blocks: Annotated[int, Field(ge=1)] = 5


class WindowSpec(SpecModel):
    """Train/test window: dates or blocks; both null = all data."""

    dates: list[DateStr] | None = None
    blocks: BlockWindow | None = None


class GridRange(SpecModel):
    """Evenly spaced grid `lo..=hi` with `n` points."""

    lo: float
    hi: float
    n: Annotated[int, Field(ge=1)]


def _alloc_grid() -> GridRange:
    return GridRange(lo=-3.0, hi=4.0, n=141)


def _databento_grid() -> GridRange:
    return GridRange(lo=-0.99, hi=0.99, n=199)


def _survival_grid() -> GridRange:
    return GridRange(lo=-0.98, hi=0.9, n=25)


class CalibrationGrid(SpecModel):
    """Optimizer grids: alloc in ln n, databento and survival in k."""

    alloc: GridRange = Field(default_factory=_alloc_grid)
    databento: GridRange = Field(default_factory=_databento_grid)
    survival: GridRange = Field(default_factory=_survival_grid)


class BootstrapSpec(SpecModel):
    """Block bootstrap settings; `auto` = day blocks with >= 5 dates, else 30-minute blocks."""

    b: Annotated[int, Field(ge=1)]
    block: Literal["auto", "day", "30min"] = "auto"
    seed: NonNegInt = 0


def _calib_bootstrap() -> BootstrapSpec:
    return BootstrapSpec(b=200)


def _score_bootstrap() -> BootstrapSpec:
    return BootstrapSpec(b=1000)


class Placement(SpecModel):
    """Touch-relative placement with a sampling weight."""

    offset_ticks: int
    weight: Annotated[float, Field(ge=0)]


def _default_placements() -> list[Placement]:
    return [
        Placement(offset_ticks=0, weight=0.6),
        Placement(offset_ticks=-1, weight=0.2),
        Placement(offset_ticks=-2, weight=0.1),
        Placement(offset_ticks=1, weight=0.1),
    ]


class QtyRule(SpecModel):
    """Quantity rule: `lots` (with `lots`) or `median_order_size`, with a sampling weight."""

    kind: Literal["lots", "median_order_size"]
    lots: Annotated[int, Field(ge=1)] | None = None
    weight: Annotated[float, Field(ge=0)]


def _default_qty() -> list[QtyRule]:
    return [QtyRule(kind="lots", lots=1, weight=0.5), QtyRule(kind="median_order_size", weight=0.5)]


DEFAULT_HORIZONS_NS = [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000]


def _horizons() -> list[int]:
    return list(DEFAULT_HORIZONS_NS)


# ---- top-level specs --------------------------------------------------------------------------------------------


class IngestSpec(SpecModel):
    """`ingest`: source files -> normalized dataset (SPEC 3.3)."""

    #: 1.2 adds `iex_deep_plus` and `nyse_ibf` (qt_ingest) and `coinbase_level3`, `kraken_l3`, `hyperliquid_node`
    #: (qt_crypto).
    format: Literal[
        "itch",
        "dbn",
        "etcsv",
        "parquet",
        "bitstamp",
        "bitfinex",
        "coinbase",
        "tardis_l2",
        "iex_deep_plus",
        "coinbase_level3",
        "kraken_l3",
        "hyperliquid_node",
        "nyse_ibf",
    ]
    inputs: Annotated[list[FileRef], Field(min_length=1)]
    venue: str
    source_venue: str | None = None
    out: str | None = None
    symbols: list[str] | None = None
    session_date: DateStr | None = None
    midnight_utc_ns: int | None = None
    tz: Literal["America/New_York", "UTC"] | None = None
    instruments: FileRef | None = None
    price_exp: Annotated[int, Field(ge=-12, le=0)] = -9
    size_exp: int | None = None
    tick_size: str | None = None
    lot_size: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    strict: bool = False
    #: 1.1 (3.11 #18): continuous-session open / close in seconds after local midnight; null = venue default (3.3)
    #: or the v0.1 `options.open_s` / `options.close_s` keys.
    open_s: NonNegInt | None = None
    close_s: NonNegInt | None = None


class ExcludeOrderId(SpecModel):
    """One real order removed from the tape before shadows are injected (audit ruling A1, self-exclusion)."""

    instrument_id: NonNegInt
    order_id: NonNegInt
    #: 1.1: session date of the order (venue order ids can repeat across days, e.g. ITCH); null = every date.
    session_date: DateStr | None = None


class ReplaySpec(SpecModel):
    """`replay`: exact replay with optional shadows, rank oracle, parity and L2 output."""

    dataset: DatasetRef
    out: str | None = None
    venue: str | None = None
    shadows: FileRef | None = None
    amends: FileRef | None = None
    latency: LatencyModel = Field(default_factory=LatencyModel)
    rank_oracle: bool = False
    sweep_gap_ns: NonNegInt = 2_000_000
    bbo: Literal["none", "change", "full"] = "change"
    depth_levels: Annotated[int, Field(ge=0, le=65535)] = 0
    trajectory: Literal["none", "ahead", "full"] = "ahead"
    race_window_ns: NonNegInt = 0
    real_orders: bool = False
    l2: L2Config | None = None
    parity_trades: bool = False
    parity_bbo: FileRef | None = None
    strict: bool = False
    threads: Annotated[int, Field(ge=1)] = 1
    seed: NonNegInt = 0
    #: 1.1 (3.11 #1): shadow fill rule; `et` reproduces the author's earlier et prototype (RD-5).
    fill_rule: Literal["exact", "et"] = "exact"
    #: 1.1 (3.11 #1): side-N print policy at the shadow price; null = venue default (`behind_and_through` on rule sets
    #: with `hidden_lower_priority`, else `ignore`).
    hidden_print_policy: Literal["ignore", "behind", "behind_and_through"] | None = None
    #: 1.1 (3.11 #1): real orders removed from the tape (ruling A1).
    exclude_order_ids: list[ExcludeOrderId] = Field(default_factory=list)


class ShadowSampleSpec(SpecModel):
    """`shadows sample`: seeded random shadow orders."""

    dataset: DatasetRef
    out: str | None = None
    n: Annotated[int, Field(ge=0)]
    seed: NonNegInt = 0
    allocation: Literal["equal", "proportional", "per_partition"] = "equal"
    #: Additive (W1 scale runs), `per_partition` only: shadows per instrument-day by key `"<instrument_id>"` or
    #: `"<instrument_id>_<YYYYMMDD>"` (the dated key wins); other instrument-days get `n`.
    partition_quotas: dict[str, NonNegInt] = Field(default_factory=dict)
    sides: list[Literal["B", "A"]] = Field(default_factory=lambda: ["B", "A"])
    placements: list[Placement] = Field(default_factory=_default_placements)
    qty: list[QtyRule] = Field(default_factory=_default_qty)
    window_open_s: NonNegInt = 300
    window_close_s: NonNegInt = 600
    lifetime_ns: NonNegInt = 300_000_000_000
    latency_ns: NonNegInt = 0


class MirrorSpec(SpecModel):
    """`shadows mirror-real`: shadows joining directly behind sampled real orders."""

    dataset: DatasetRef
    out: str | None = None
    n: Annotated[int, Field(ge=0)] = 3000
    seed: NonNegInt = 0
    stratify: bool = True
    placement: Literal["behind_real"] = "behind_real"


class ZooSpec(SpecModel):
    """`zoo`: run queue models on the L2 view and compare with exact truth."""

    dataset: DatasetRef
    shadows: FileRef
    out: str | None = None
    models: list[str] = Field(default_factory=lambda: ["all"])
    mode: Literal["faithful", "corrected"] = "faithful"
    exchange: Literal["partial", "no_partial"] = "partial"
    horizons_ns: list[NonNegInt] = Field(default_factory=_horizons)
    h_max_ns: NonNegInt = 300_000_000_000
    l2: L2Config = Field(default_factory=L2Config)
    particles: Annotated[int, Field(ge=1)] = 100
    seed: NonNegInt = 0
    latency: LatencyModel = Field(default_factory=LatencyModel)
    write_fills: bool = True
    write_trajectory: bool = False
    threads: Annotated[int, Field(ge=1)] = 1
    bootstrap_b: Annotated[int, Field(ge=1)] = 1000
    #: 1.1 (3.11 #7): shadow amends (`qt.shadow_amend`); null = no amends (v0.1 behaviour).
    amends: FileRef | None = None
    #: 1.1 (extends 3.11 #1 to the zoo): real orders removed from the tape before the exact truth and the L2
    #: view are built (self-exclusion, ruling A1; same semantics as `ReplaySpec.exclude_order_ids`).
    exclude_order_ids: list[ExcludeOrderId] = Field(default_factory=list)


class CalibrationSpec(SpecModel):
    """`calibrate`: fit queue-model parameters against exact truth, or latency from live logs."""

    kind: Literal["alloc", "survival", "nt_touch", "latency"]
    family: Literal["power", "power2", "power3", "databento", "nt_prob_touch", "nt_queue_l2_prob_touch", "latency"]
    dataset: DatasetRef | None = None
    shadows: FileRef | None = None
    live: FileRef | None = None
    train: WindowSpec = Field(default_factory=WindowSpec)
    test: WindowSpec = Field(default_factory=WindowSpec)
    out: str | None = None
    mode: Literal["faithful", "corrected"] = "faithful"
    exchange: Literal["partial", "no_partial"] = "partial"
    l2: L2Config = Field(default_factory=L2Config)
    horizons_ns: list[NonNegInt] = Field(default_factory=_horizons)
    h_max_ns: NonNegInt = 300_000_000_000
    grid: CalibrationGrid = Field(default_factory=CalibrationGrid)
    bootstrap: BootstrapSpec = Field(default_factory=_calib_bootstrap)
    particles: Annotated[int, Field(ge=1)] = 100
    seed: NonNegInt = 0
    threads: Annotated[int, Field(ge=1)] = 1


class FeatureSpec(SpecModel):
    """`features`: landmark features and outcomes for shadows or real orders."""

    dataset: DatasetRef
    out: str | None = None
    population: Literal["shadows", "real_orders"] = "shadows"
    shadows: FileRef | None = None
    n_real_orders: NonNegInt | None = None
    landmarks_ns: list[NonNegInt] = Field(default_factory=lambda: [0, 1_000_000_000, 5_000_000_000])
    #: Policy-cancel distance in ticks (shadows); 1.1: null or 0 = never cancel (labels without the policy cancel, for
    #: the zoo-vs-hazard comparison of review F6).
    cancel_away_ticks: NonNegInt | None = 2
    h_max_ns: NonNegInt = 60_000_000_000
    l2: L2Config = Field(default_factory=L2Config)
    trackers: list[str] = Field(
        default_factory=lambda: ["hbt_risk_adverse", "hbt_power3[n=1]", "hbt_power3[n=3]", "hbt_log2"]
    )
    privileged: bool = True
    seed: NonNegInt = 0
    threads: Annotated[int, Field(ge=1)] = 1


class SplitSpec(SpecModel):
    """Train/calib/test split for the learned model (SPEC 5.5.6)."""

    scheme: Literal["auto", "days", "blocks"] = "auto"
    n_blocks: Annotated[int, Field(ge=3)] = 5
    embargo_ns: NonNegInt = 60_000_000_000


class GbdtSpec(SpecModel):
    """HistGradientBoostingClassifier settings."""

    max_iter: Annotated[int, Field(ge=1)] = 300
    learning_rate: Annotated[float, Field(gt=0)] = 0.05
    max_leaf_nodes: Annotated[int, Field(ge=2)] = 31
    min_samples_leaf: Annotated[int, Field(ge=1)] = 100
    l2_regularization: Annotated[float, Field(ge=0)] = 1.0


class HazardFitSpec(SpecModel):
    """`fit-hazard`: discrete-time competing-risk hazard model."""

    features: FileRef
    out: str | None = None
    grid_ns: list[NonNegInt] = Field(
        default_factory=lambda: [
            100_000_000,
            250_000_000,
            500_000_000,
            1_000_000_000,
            2_000_000_000,
            5_000_000_000,
            10_000_000_000,
            30_000_000_000,
            60_000_000_000,
        ]
    )
    split: SplitSpec = Field(default_factory=SplitSpec)
    gbdt: GbdtSpec = Field(default_factory=GbdtSpec)
    recalibration: Literal["isotonic", "none"] = "isotonic"
    privileged: bool = False
    seed: NonNegInt = 0
    bootstrap_b: Annotated[int, Field(ge=1)] = 1000
    zoo_predictions: FileRef | None = None
    #: 1.1: `qt.zoo_shadows` (planned life, never-cancel flag) of the zoo predictions; null = `zoo_shadows.parquet`
    #: next to `zoo_predictions` when present.
    zoo_shadows: FileRef | None = None


class FixOptions(SpecModel):
    """FIX 4.4 drop-copy parsing options."""

    delimiter: str = "auto"
    local_ts_regex: str | None = None
    #: 1.1 (3.11 #13): venue name written to the canonical log and used for instrument-map lookups (null = `fix44`).
    venue_name: str | None = None
    #: 1.1: messages failing CheckSum(10) / BodyLength(9): `exclude` (dropped with a warning) or `keep`.
    checksum: Literal["exclude", "keep"] = "exclude"
    #: 1.1: QuickFIX data dictionary XML path(s) (custom tags and enums); null = none.
    data_dictionary: str | list[str] | None = None
    #: 1.1: session (file stem such as `FIX.4.4-ME-CME`, or counterparty CompID) -> venue name.
    session_venues: dict[str, str] = Field(default_factory=dict)
    #: 1.1: tag holding the instrument instead of Symbol(55) (e.g. 107 SecurityDesc); null = 55.
    instrument_tag: Annotated[int, Field(ge=1)] | None = None


class LiveImportSpec(SpecModel):
    """`live import`: venue exports -> canonical live log (SPEC 3.8)."""

    #: 1.1 (3.11 #13) adds `sim_hftbacktest` and `sim_nautilus` (simulator outputs; log venue `sim:<name>`).
    venue: Literal[
        "binance",
        "bybit",
        "okx",
        "coinbase",
        "kraken",
        "deribit",
        "hyperliquid",
        "ibkr_flex",
        "fix44",
        "sim_hftbacktest",
        "sim_nautilus",
    ]
    inputs: Annotated[list[FileRef], Field(min_length=1)]
    out: str | None = None
    account: str | None = None
    instrument_map: FileRef | None = None
    append: bool = False
    fix: FixOptions = Field(default_factory=FixOptions)
    #: 1.1 (3.11 #13): account time zone (IANA name) of venue-local times (IBKR Flex); null = UTC.
    tz: str | None = None


class SimExternal(SpecModel):
    """Simulator arm from a canonical live-log directory written by the user's simulator."""

    kind: Literal["external"]
    log: FileRef


class SimExact(SpecModel):
    """Simulator arm = exact replay of the schedule."""

    kind: Literal["exact"]


class SimZoo(SpecModel):
    """Simulator arm = one zoo model on the L2 view."""

    kind: Literal["zoo"]
    model: str
    l2: L2Config = Field(default_factory=L2Config)


SimSpec = Annotated[SimExternal | SimExact | SimZoo, Field(discriminator="kind")]


class FdrSpec(SpecModel):
    """False-discovery-rate control for secondary tests."""

    method: Literal["bh", "by"] = "bh"
    q: Annotated[float, Field(gt=0, lt=1)] = 0.05


class EquivalenceSpec(SpecModel):
    """TOST equivalence margins."""

    fill_rate_margin: Annotated[float, Field(ge=0)] = 0.02
    markout_margin_ticks: Annotated[float, Field(ge=0)] = 0.25
    markout_horizon_ns: NonNegInt = 1_000_000_000


class GatesSpec(SpecModel):
    """Scorecard gates (exit code 1 when failed)."""

    max_abs_fill_rate_bias: Annotated[float, Field(ge=0)] | None = None
    require_equivalence: bool = False


class ScorecardSpec(SpecModel):
    """`score`: live log vs simulator arm (SPEC 5.7)."""

    live: FileRef
    sim: SimSpec
    dataset: DatasetRef | None = None
    out: str | None = None
    entry: Literal["exchange_ack", "send_plus_latency"] = "exchange_ack"
    latency: LatencyModel | None = None
    markout_horizons_ns: list[NonNegInt] = Field(
        default_factory=lambda: [
            50_000_000,
            100_000_000,
            1_000_000_000,
            10_000_000_000,
            15_000_000_000,
            60_000_000_000,
            300_000_000_000,
        ]
    )
    markout_refs: list[Literal["mid", "microprice"]] = Field(default_factory=lambda: ["mid"])
    ttf_horizons_ns: list[NonNegInt] = Field(default_factory=_horizons)
    bootstrap: BootstrapSpec = Field(default_factory=_score_bootstrap)
    fdr: FdrSpec = Field(default_factory=FdrSpec)
    equivalence: EquivalenceSpec = Field(default_factory=EquivalenceSpec)
    gates: GatesSpec = Field(default_factory=GatesSpec)
    perm_b: Annotated[int, Field(ge=1)] = 2000
    #: 1.1 (3.11 #8): evaluation horizon H of ruling A9 (60 s).
    window_ns: Annotated[int, Field(ge=1)] = 60_000_000_000
    #: 1.1 (3.11 #8): live-to-MBO twin matching window for self-exclusion (1 ms).
    match_window_ns: NonNegInt = 1_000_000
    #: 1.1 (3.11 #8): remove the live orders' own MBO records from the exact replay (ruling A1).
    self_exclude: bool = True
    #: 1.1 (3.11 #21): markouts whose horizon passes the end of data: null = undefined, `last_quote` = last quote.
    markout_past_end: Literal["last_quote"] | None = None


class ReportSpec(SpecModel):
    """`report`: static HTML report from output directories."""

    inputs: Annotated[list[str], Field(min_length=1)]
    out: str | None = None
    title: str = "queuetruth report"
    max_why: NonNegInt = 20
    #: 1.1: horizon the zoo leaderboard is sorted at (absolute fill-rate bias); null = 60 s.
    leaderboard_sort_horizon_ns: Annotated[int, Field(ge=1)] | None = None


class SimulatorInfo(SpecModel):
    """Name and version of the simulator under test."""

    name: str
    version: str | None = None


def _default_simulator() -> SimulatorInfo:
    return SimulatorInfo(name="unknown")


class RefereeTolerance(SpecModel):
    """Referee pass thresholds."""

    max_discordant: NonNegInt = 0
    max_qty_mismatch: NonNegInt = 0
    max_abs_ttf_err_ns: NonNegInt = 0


class RefereeSpec(SpecModel):
    """`referee run`: exact-FIFO regression oracle for another simulator."""

    dataset: DatasetRef
    schedule: FileRef
    sim_fills: FileRef
    simulator: SimulatorInfo = Field(default_factory=_default_simulator)
    tolerance: RefereeTolerance = Field(default_factory=RefereeTolerance)
    out: str | None = None


class WhySpec(SpecModel):
    """`why`: event-by-event trace of one shadow or live order."""

    dataset: DatasetRef
    shadows: FileRef | None = None
    shadow_id: str | None = None
    live: FileRef | None = None
    order_key: str | None = None
    max_events: Annotated[int, Field(ge=1)] = 5000
    out: str | None = None
    #: 1.1: real orders removed from the tape (the user's own orders, ruling A1; as `ReplaySpec.exclude_order_ids`).
    exclude_order_ids: list[ExcludeOrderId] = Field(default_factory=list)


class ExportSpec(SpecModel):
    """`export`: config snippets and npz files."""

    target: Literal["hftbacktest", "nautilus", "l2_npz", "latency_npz"]
    calibration: FileRef | None = None
    dataset: DatasetRef | None = None
    instrument_id: NonNegInt | None = None
    session_date: DateStr | None = None
    live: FileRef | None = None
    l2: L2Config = Field(default_factory=L2Config)
    feed_latency_ns: NonNegInt = 0
    out: str | None = None
    #: 1.1: write the snippet of a calibration the trust check refuses (with a `# !! NOT VALIDATED` header).
    force: bool = False


class LiveCalibrationSpec(SpecModel):
    """1.1 (3.11 #11): `calibrate-live`: fit a zoo parameter to the user's own live outcomes on an L2 (or MBO) tape
    (as built in `queuetruth.score.calibrate_live`)."""

    dataset: DatasetRef
    live: FileRef
    family: Literal["power", "power2", "power3", "databento", "nt_prob_touch", "nt_queue_l2_prob_touch"]
    out: str | None = None
    entry: Literal["exchange_ack", "send_plus_latency"] = "exchange_ack"
    latency: LatencyModel | None = None
    mode: Literal["faithful", "corrected"] = "faithful"
    exchange: Literal["partial", "no_partial"] = "partial"
    l2: L2Config = Field(default_factory=L2Config)
    horizons_ns: list[Annotated[int, Field(ge=1)]] = Field(
        default_factory=lambda: [100_000_000, 1_000_000_000, 10_000_000_000]
    )
    window_ns: Annotated[int, Field(ge=1)] = 60_000_000_000
    grid: GridRange | None = None
    train: WindowSpec = Field(default_factory=WindowSpec)
    test: WindowSpec = Field(default_factory=WindowSpec)
    particles: Annotated[int, Field(ge=1)] = 100
    bootstrap: BootstrapSpec = Field(default_factory=_calib_bootstrap)
    seed: NonNegInt = 0
    threads: Annotated[int, Field(ge=1)] = 1
    #: 1.1: remove the live orders' own records (MBO) or resting size (L2 depth) before the zoo runs (ruling A1).
    self_exclude: bool = True
    #: 1.1: live-to-MBO twin matching window for self-exclusion (1 ms).
    match_window_ns: NonNegInt = 1_000_000


#: Top-level spec models and their JSON Schema names (`schemas/json/<name>.schema.json`).
#: `DqSpec` defaults (schema 1.2).
DQ_DEFAULT_STALE_NS = 60_000_000_000
DQ_DEFAULT_SESSION_TOLERANCE_NS = 300_000_000_000
DQ_DEFAULT_MAX_EVIDENCE = 20


class DqSpec(SpecModel):
    """1.2: `queuetruth dq` spec (`qt.dq_spec`; checks in `queuetruth.dq`). `dataset`: the dataset to check; `out`:
    output directory for `dq_report.json` and `dq_report.html` (null = return the report, write nothing); `compare`:
    a second dataset of the same session(s) for the cross-source parity check; `checks`: run only these check ids
    (null = all); `fail_on`: the CLI exits 1 when any check reaches this status (`warn`: warn or fail; `fail`: fail
    only; `never`); `max_evidence`: evidence rows kept per table; `stale_ns`: a book with no update for longer than
    this while trades print is stale; `latency_outlier_ns`: `ts_recv - ts_event` above this is an outlier (null =
    10 x p99, at least 1 ms); `session_tolerance_ns`: slack at the session open and close before coverage warns;
    `replay`: run the exact replay for the book checks (crossed books, prints through the book, trade parity, replay
    counters); `verify_sources`: re-hash the raw input files recorded in the manifest when they still exist
    (otherwise only their size is compared); `title`: HTML title."""

    dataset: PathRef
    out: str | None = None
    compare: PathRef | None = None
    checks: list[str] | None = None
    fail_on: Literal["warn", "fail", "never"] = "warn"
    max_evidence: int = Field(default=DQ_DEFAULT_MAX_EVIDENCE, ge=0, le=10_000)
    stale_ns: int = Field(default=DQ_DEFAULT_STALE_NS, gt=0)
    latency_outlier_ns: int | None = Field(default=None, gt=0)
    session_tolerance_ns: int = Field(default=DQ_DEFAULT_SESSION_TOLERANCE_NS, ge=0)
    replay: bool = True
    verify_sources: bool = False
    title: str | None = None


SPEC_MODELS: dict[str, type[SpecModel]] = {
    "qt.ingest_spec": IngestSpec,
    "qt.replay_spec": ReplaySpec,
    "qt.shadow_sample_spec": ShadowSampleSpec,
    "qt.mirror_spec": MirrorSpec,
    "qt.zoo_spec": ZooSpec,
    "qt.calibration_spec": CalibrationSpec,
    "qt.feature_spec": FeatureSpec,
    "qt.hazard_fit_spec": HazardFitSpec,
    "qt.live_import_spec": LiveImportSpec,
    "qt.scorecard_spec": ScorecardSpec,
    "qt.report_spec": ReportSpec,
    "qt.referee_spec": RefereeSpec,
    "qt.why_spec": WhySpec,
    "qt.export_spec": ExportSpec,
    "qt.live_calibration_spec": LiveCalibrationSpec,
    "qt.dq_spec": DqSpec,
}

__all__ = [
    "DQ_DEFAULT_MAX_EVIDENCE",
    "DQ_DEFAULT_SESSION_TOLERANCE_NS",
    "DQ_DEFAULT_STALE_NS",
    "SPEC_MODELS",
    "BlockWindow",
    "BootstrapSpec",
    "CalibrationGrid",
    "CalibrationSpec",
    "DatasetIdRef",
    "DatasetRef",
    "DqSpec",
    "EquivalenceSpec",
    "ExcludeOrderId",
    "ExportSpec",
    "FdrSpec",
    "FeatureSpec",
    "FileRef",
    "FixOptions",
    "GatesSpec",
    "GbdtSpec",
    "GridRange",
    "HazardFitSpec",
    "IngestSpec",
    "JobFile",
    "JobFileRef",
    "L2Config",
    "LatencyModel",
    "LiveCalibrationSpec",
    "LiveImportSpec",
    "MirrorSpec",
    "PathRef",
    "Placement",
    "QtyRule",
    "RefereeSpec",
    "RefereeTolerance",
    "ReplaySpec",
    "ReportSpec",
    "ScorecardSpec",
    "ShadowSampleSpec",
    "SimExact",
    "SimExternal",
    "SimSpec",
    "SimZoo",
    "SimulatorInfo",
    "SpecModel",
    "SplitSpec",
    "UploadRef",
    "WhySpec",
    "WindowSpec",
    "ZooSpec",
]
