"""Documents of the forecast referee (`queuetruth forecast`): ledger entries, commitments, ledger summaries, the
score spec and the scorecard.

The models follow `queuetruth.schemas.models`: `schema` / `schema_version` first, unknown fields rejected, fields
named `schema` stored as `schema_`. Timestamps are integer ns since the Unix epoch, UTC. Probabilities lie in
[0, 1]. Every loss is negatively oriented (lower is better); skill and `mean_diff` are positive when the forecaster
beats the baseline.

The four documents are added to `queuetruth.schemas.models.DOC_MODELS` when this module is imported (the import
in `queuetruth.schemas`), so `parse_document`, `schemas/json` and the samples include them. They are Python-only
(no `qt_schema` mirror).
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from queuetruth.schemas.models import DOC_MODELS, CiValue, FileEntry, QtDoc, QtModel, RunId, Sha256, _tag

Prob = Annotated[float, Field(ge=0.0, le=1.0)]
#: A finite float (NaN and infinities are refused: a ledger is untrusted input and its hashes need finite numbers).
Finite = Annotated[float, Field(allow_inf_nan=False)]
Kind = Literal["binary", "categorical", "continuous"]
Baseline = Literal["market", "climatology", "uniform"]
Metric = Literal["brier", "log", "spherical", "rps", "crps"]
#: Eligibility of one forecast entry (`forecast_eligibility.parquet` column `status`, scorecard `rejections`).
Status = Literal[
    "scored",
    "superseded",
    "unresolved",
    "void",
    "after_resolution_entry",
    "after_cutoff",
    "short_lead",
    "declared_clock",
    "as_of_after_commit",
    "kind_mismatch",
    "outcome_mismatch",
    "unanchored",
    "anchored_after_cutoff",
]
#: Statuses of the leakage and validity guard (`unresolved` and `void` are not scored either, but they are about the
#: question, not the forecast, and are counted in `ForecasterScore.selection`).
REJECT_STATUSES: tuple[str, ...] = (
    "after_resolution_entry",
    "after_cutoff",
    "short_lead",
    "declared_clock",
    "as_of_after_commit",
    "kind_mismatch",
    "outcome_mismatch",
    "unanchored",
    "anchored_after_cutoff",
)
SUM_TOL = 1e-6


# ---- forecasts and resolutions ----------------------------------------------------------------------------------


class BinaryForecast(QtModel):
    """P(outcome = 1)."""

    kind: Literal["binary"] = "binary"
    p: Prob


class CategoricalForecast(QtModel):
    """Probability per outcome label. `forecast commit` normalises them to sum to 1 (refusing a sum more than 1e-3
    away) and `score` re-checks the sum against `SUM_TOL` and the question's outcome set."""

    kind: Literal["categorical"] = "categorical"
    probs: dict[str, Prob]


class NormalDist(QtModel):
    """Gaussian predictive distribution."""

    family: Literal["normal"] = "normal"
    mean: Finite
    sd: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]


class QuantileDist(QtModel):
    """Predictive quantiles: `values[i]` at probability level `levels[i]` (strictly increasing, in (0, 1));
    `values` non-decreasing."""

    family: Literal["quantiles"] = "quantiles"
    levels: list[Finite]
    values: list[Finite]

    @model_validator(mode="after")
    def _check(self) -> QuantileDist:
        if not self.levels or len(self.levels) != len(self.values):
            raise ValueError("levels and values must be non-empty and of equal length")
        if any(not 0.0 < x < 1.0 for x in self.levels):
            raise ValueError("levels must lie in (0, 1)")
        if any(b <= a for a, b in zip(self.levels, self.levels[1:], strict=False)):
            raise ValueError("levels must be strictly increasing")
        if any(b < a for a, b in zip(self.values, self.values[1:], strict=False)):
            raise ValueError("values must be non-decreasing (quantiles cross)")
        return self


class SampleDist(QtModel):
    """Ensemble or Monte Carlo samples of the predictive distribution (equal weights)."""

    family: Literal["samples"] = "samples"
    samples: Annotated[list[Finite], Field(min_length=1)]


class ContinuousForecast(QtModel):
    """A predictive distribution of a real-valued outcome."""

    kind: Literal["continuous"] = "continuous"
    dist: Annotated[NormalDist | QuantileDist | SampleDist, Field(discriminator="family")]


ForecastBody = Annotated[BinaryForecast | CategoricalForecast | ContinuousForecast, Field(discriminator="kind")]


class ForecastPayload(QtModel):
    """One forecast: who, which question, what. `as_of_ns` is the forecaster's own information timestamp (optional;
    it may not be later than the commit). `nonce` (64 hex, 256 random bits) makes the payload digest unguessable, so
    a published commitment does not reveal the forecast before it is opened."""

    type: Literal["forecast"] = "forecast"
    question_id: Annotated[str, Field(min_length=1)]
    forecaster: Annotated[str, Field(min_length=1)]
    forecast: ForecastBody
    as_of_ns: int | None = None
    nonce: Sha256


class ResolutionPayload(QtModel):
    """How a question resolved. `outcome`: 0 or 1 (binary), a label of `outcomes` (categorical), a number
    (continuous); null only when `void`. `info_cutoff_ns`: when the outcome became knowable, if earlier than
    `resolved_at_ns`. `ordered`: categorical outcomes are ordinal in the listed order (enables RPS). `group`:
    related questions (shared climatology and, with `cluster: group`, one bootstrap cluster)."""

    type: Literal["resolution"] = "resolution"
    question_id: Annotated[str, Field(min_length=1)]
    kind: Kind
    outcome: Finite | str | None
    outcomes: list[str] | None = None
    ordered: bool = False
    resolved_at_ns: int
    info_cutoff_ns: int | None = None
    void: bool = False
    group: str | None = None
    source: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ResolutionPayload:
        """The rules of `ledger.normalize_resolution`, enforced on every entry read back (a ledger is untrusted)."""
        if self.info_cutoff_ns is not None and self.info_cutoff_ns > self.resolved_at_ns:
            raise ValueError("info_cutoff_ns is after resolved_at_ns")
        if self.outcomes is not None and (len(self.outcomes) < 2 or len(set(self.outcomes)) != len(self.outcomes)):
            raise ValueError("outcomes must list two or more distinct labels")
        if self.outcome is None:
            if not self.void:
                raise ValueError("outcome is required unless the question is void")
            return self
        if self.kind == "binary":
            if isinstance(self.outcome, str) or self.outcome not in (0.0, 1.0):
                raise ValueError("a binary outcome must be 0 or 1")
        elif self.kind == "categorical":
            if self.outcomes is None or not isinstance(self.outcome, str) or self.outcome not in self.outcomes:
                raise ValueError("a categorical outcome must be one of outcomes")
        elif isinstance(self.outcome, str):
            raise ValueError("a continuous outcome must be a number")
        return self


Payload = Annotated[ForecastPayload | ResolutionPayload, Field(discriminator="type")]


class ForecastEntry(QtDoc):
    """`qt.forecast_entry`: one line of a forecast ledger (`ledger.jsonl`). `entry_sha256` = sha256 of the RFC 8785
    canonical JSON of the entry without `entry_sha256`; `prev_sha256` is the previous entry's hash (64 zeros for
    seq 0); `payload_sha256` = sha256 of the canonical JSON of `payload`. `clock`: `system` (the writer's UTC clock,
    never earlier than the previous entry) or `declared` (a time the writer supplied; not trusted by `score`)."""

    SCHEMA: ClassVar[str] = "qt.forecast_entry"
    schema_: Literal["qt.forecast_entry"] = _tag("qt.forecast_entry")
    schema_version: Literal[1] = 1
    seq: Annotated[int, Field(ge=0)]
    prev_sha256: Sha256
    recorded_at_ns: int
    clock: Literal["system", "declared"]
    payload_sha256: Sha256
    payload: Payload
    entry_sha256: Sha256


class ForecastCommitment(QtDoc):
    """`qt.forecast_commitment`: a plain-JSON commitment to a ledger head. It names the head entry's hash, which
    commits to every earlier entry through the chain, and carries no forecast content, so it can be published,
    anchored or wrapped by a certificate (a timestamp authority signs `sha256(canonical_json(commitment))`) before
    the questions resolve. `ledger_id` is the hash of entry 0."""

    SCHEMA: ClassVar[str] = "qt.forecast_commitment"
    schema_: Literal["qt.forecast_commitment"] = _tag("qt.forecast_commitment")
    schema_version: Literal[1] = 1
    ledger_id: Sha256
    head_seq: Annotated[int, Field(ge=0)]
    head_sha256: Sha256
    head_recorded_at_ns: int
    n_entries: Annotated[int, Field(ge=1)]
    n_forecasts: Annotated[int, Field(ge=0)]
    n_resolutions: Annotated[int, Field(ge=0)]
    hash_alg: Literal["sha256"] = "sha256"
    chain_rule: str
    engine_version: str


class LedgerProblem(QtModel):
    """One integrity problem found by `verify_ledger`."""

    seq: int | None
    line: int
    problem: str


class LedgerEntryRef(QtModel):
    """One appended entry, as the commit and resolve summaries list it."""

    seq: int
    type: Literal["forecast", "resolution"]
    question_id: str
    forecaster: str | None
    entry_sha256: Sha256


class ForecastLedgerSummary(QtDoc):
    """`qt.forecast_ledger_summary` (`forecast commit`, `forecast resolve`, `forecast verify`): what was appended,
    the head commitment and the chain verification (`verified` false lists `problems`)."""

    SCHEMA: ClassVar[str] = "qt.forecast_ledger_summary"
    schema_: Literal["qt.forecast_ledger_summary"] = _tag("qt.forecast_ledger_summary")
    schema_version: Literal[1] = 1
    action: Literal["commit", "resolve", "verify"]
    ledger: str
    engine_version: str
    appended: int
    entries: list[LedgerEntryRef]
    entries_truncated: bool
    commitment: ForecastCommitment | None
    verified: bool
    problems: list[LedgerProblem]
    warnings: list[str]


# ---- score spec -------------------------------------------------------------------------------------------------


class FileRef(QtModel):
    """A local file (enters `run_id` by its sha256)."""

    path: str


class FeeSchedule(QtModel):
    """Taker fee per order of `C` contracts at price `P` (per-contract price in [0, 1], payout 1):
    `C * (per_contract + notional_rate * P + quadratic * P * (1 - P))`, rounded up to a multiple of `round_up_to`
    when it is positive. `quadratic = 0.07, round_up_to = 0.01` is the historical Kalshi taker formula."""

    per_contract: Annotated[float, Field(ge=0.0)] = 0.0
    notional_rate: Annotated[float, Field(ge=0.0)] = 0.0
    quadratic: Annotated[float, Field(ge=0.0)] = 0.0
    round_up_to: Annotated[float, Field(ge=0.0)] = 0.0


class PnlSpec(QtModel):
    """Executable PnL of binary forecasts against the market file's quotes. One taker order per scored
        (forecaster, question) at `recorded_at_ns + latency_ns`, sized by `kelly_fraction` times the Kelly stake after
        the spread and fees, capped by `max_participation` of the displayed depth at the touch and by cash; settled at
        resolution. `lot`: contract granularity (0 = continuous, for the frictionless identity). `price_at`: `decision`
    prices a marketable limit order from the quote at the decision and fills it at arrival only when the price is no
    worse (`price_moved` otherwise); `arrival` prices it from the arrival quote (no market-data delay; optimistic)."""

    kelly_fraction: Annotated[float, Field(gt=0.0, le=1.0)] = 0.25
    bankroll: Annotated[float, Field(gt=0.0)] = 1000.0
    latency_ns: Annotated[int, Field(ge=0)] = 0
    max_participation: Annotated[float, Field(gt=0.0, le=1.0)] = 1.0
    max_quote_age_ns: Annotated[int, Field(ge=0)] | None = None
    min_edge: Annotated[float, Field(ge=0.0)] = 0.0
    lot: Annotated[float, Field(ge=0.0)] = 1.0
    fees: FeeSchedule = Field(default_factory=FeeSchedule)
    price_at: Literal["decision", "arrival"] = "decision"


class Anchor(QtModel):
    """A `qt.forecast_commitment` and the time a third party attested it (a timestamp certificate, a publication):
    every entry up to its `head_seq` existed by `anchored_at_ns`."""

    commitment: FileRef
    anchored_at_ns: int


class ForecastScoreSpec(QtModel):
    """Spec of `forecast score` (echoed in the scorecard; every output-affecting option is a field).

    `anchors` / `require_anchor`: with `require_anchor`, a forecast counts only when an anchored commitment covers
    it (`unanchored` otherwise) and the earliest such anchor is before the question's cutoff
    (`anchored_after_cutoff` otherwise), so timing truth no longer rests on the ledger writer's clock. Every anchor
    must match the ledger (`QT-INPUT-002` otherwise). `market_max_age_ns`: the oldest quote the market baseline uses
    (default: `pnl.max_quote_age_ns`, else no limit). `pairwise`: forecaster-pair comparisons for `all` pairs,
    `none`, or the pairs among the `k` forecasters with the most scored questions. `unresolved: fail` refuses to
    score while any forecast's question is unresolved."""

    ledger: FileRef
    market: FileRef | None = None
    anchors: list[Anchor] = Field(default_factory=list)
    require_anchor: bool = False
    market_max_age_ns: Annotated[int, Field(ge=0)] | None = None
    pairwise: Literal["all", "none"] | Annotated[int, Field(ge=2)] = "all"
    unresolved: Literal["warn", "fail"] = "warn"
    out: str | None = None
    policy: Literal["last", "first", "mean"] = "last"
    baselines: list[Baseline] = Field(default_factory=lambda: ["market", "climatology", "uniform"])
    cluster: Literal["question", "group"] = "question"
    n_bins: Annotated[int, Field(ge=2, le=100)] = 10
    bootstrap_b: Annotated[int, Field(ge=0, le=100_000)] = 1000
    seed: Annotated[int, Field(ge=0)] = 0
    alpha: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.05
    power: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.8
    min_edge: Annotated[float, Field(gt=0.0)] | None = None
    dm_lag: Annotated[int, Field(ge=0)] | None = None
    min_lead_ns: Annotated[int, Field(ge=0)] = 0
    allow_declared_clock: bool = False
    log_eps: Annotated[float, Field(gt=0.0, lt=0.5)] = 1e-6
    climatology_min_prior: Annotated[int, Field(ge=1)] = 5
    pnl: PnlSpec | None = Field(default_factory=PnlSpec)

    @field_validator("baselines")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("baselines must not repeat")
        return v


# ---- scorecard ----------------------------------------------------------------------------------------------------


class MetricScore(QtModel):
    """Mean loss over the forecaster's scored questions, with a cluster-bootstrap percentile CI."""

    kind: Kind
    metric: Metric
    n: int
    mean: CiValue
    n_clipped: int = 0


class ReliabilityBin(QtModel):
    """One equal-width bin of the reliability diagram: Wilson 95% interval of the observed rate and the
    consistency band (2.5% and 97.5% quantiles of the rate when outcomes are drawn from the forecasts)."""

    lo: float
    hi: float
    count: int
    mean_p: float | None
    observed_rate: float | None
    rate_lo: float | None
    rate_hi: float | None
    consistency_lo: float | None
    consistency_hi: float | None


class Calibration(QtModel):
    """Calibration of binary forecasts (`target: binary`) or the top-label confidence of categorical ones
    (`target: top_label`): ECE (equal-width bins) with a cluster-bootstrap CI, its expectation and p-value under
    perfect calibration (outcomes resampled from the forecasts), and the Murphy decomposition (equal-count bins)."""

    target: Literal["binary", "top_label"]
    n: int
    n_bins: int
    ece: CiValue
    ece_if_calibrated: float | None
    ece_p_value: float | None
    murphy: dict[str, float | None]
    bins: list[ReliabilityBin]


class DmTest(QtModel):
    """Diebold-Mariano test of equal expected loss on per-question loss differentials ordered by resolution time:
    Newey-West (Bartlett) long-run variance with `lag` lags, Harvey-Leybourne-Newbold correction, Student t with
    `units - 1` df; two-sided. `units` is the number of questions, or of clusters when a cluster holds more than one
    row (then the test runs on cluster sums: CR1 at lag 0). `statistic` > 0 when the subject has the lower loss;
    both are null when the differences are constant but not zero, or fewer than 2 units."""

    statistic: float | None
    p_value: float | None
    lag: int
    units: int


class Comparison(QtModel):
    """Paired comparison on common questions: `mean_diff` = mean loss of `reference` minus mean loss of `subject`
    (positive = subject better) with a cluster-bootstrap CI and two-sided p-value, `skill` = 1 - loss(subject) /
    loss(reference) with its CI, the DM test, and `p_holm`: the DM p-value Holm-adjusted across every comparison
    of the scorecard with the same kind and metric (baselines and forecaster pairs). A reference is a baseline
    name (`market`, `climatology`, `uniform`) or another forecaster; for forecaster pairs `lead_diff_ns` is the
    median over common questions of the subject's lead minus the reference's (positive = the subject committed
    earlier, with less information)."""

    subject: str
    reference: str
    kind: Kind
    metric: Metric
    n: int
    n_clusters: int
    mean_diff: CiValue
    skill: CiValue
    p_bootstrap: float | None
    dm: DmTest
    p_holm: float | None = None
    lead_diff_ns: int | None = None


class PowerAnalysis(QtModel):
    """Resolved questions needed to detect an edge in `metric` against `reference` with a two-sided paired test at
    `alpha` and `power` (noncentral t, design effect from clustering): for the spec's `min_edge` (the planning
    number) and for the observed edge; `achieved_power` = power of the current sample against the observed edge.
    The observed-edge numbers are noisy estimates (post-hoc power is a function of the p-value)."""

    reference: str
    kind: Kind
    metric: Metric
    n: int
    observed_edge: float | None
    sd_diff: float | None
    design_effect: float
    achieved_power: float | None
    n_required_observed: int | None
    min_edge: float | None
    n_required_min_edge: int | None


class PnlResult(QtModel):
    """Executable PnL (binary forecasts with quotes). `isolated_growth` = mean log return of each bet sized on a
    fresh bankroll (CI by cluster bootstrap): the per-question edge after costs. `frictionless_growth` = a full-Kelly
    bettor at the decision-time mid with no spread, fees, depth limit or latency, which equals the mean log-score
    difference against that mid. The bankroll path: `log_growth` = log(final / bankroll) (null when the bankroll is
    lost), `growth_per_bet` = log_growth / n_bets, `total_return`, `max_drawdown`; `path_cash_limited` is true when
    cash skipped or trimmed more than 10% of the candidates, and the path then describes an arbitrary subset of
    bets (read `isolated_growth`)."""

    n_candidates: int
    n_bets: int
    n_skipped: dict[str, int]
    bankroll: float
    final_wealth: float
    total_return: float
    log_growth: float | None
    growth_per_bet: float | None
    isolated_growth: CiValue
    frictionless_growth: float | None
    max_drawdown: float
    fees_paid: float
    spread_paid: float
    contracts: float
    depth_limited: int
    cash_limited: int
    path_cash_limited: bool
    hit_rate: float | None


class Selection(QtModel):
    """What the resolutions did to one forecaster's record. Resolutions, cutoffs and voids are trusted input: whoever
    writes them can shape the record, so these counts make that visible. `unresolved` / `void`: forecast entries
    on questions with no resolution, or a void effective resolution. `voided_after_outcome`: questions of this
    forecaster's entries first resolved with an outcome and later corrected to void. `outcome_corrected`:
    questions whose scored outcome differs from the first resolution's. `cutoff_tightened`: questions whose
    correction moved the cutoff earlier."""

    unresolved: int = 0
    void: int = 0
    voided_after_outcome: int = 0
    outcome_corrected: int = 0
    cutoff_tightened: int = 0


class ForecasterScore(QtModel):
    """Every number for one forecaster (`n_rejected`: the leakage and validity guard; `n_anchored`: scored forecasts
    covered by an anchored commitment)."""

    forecaster: str
    n_forecasts: int
    n_scored: int
    n_rejected: int
    selection: Selection
    n_anchored: int
    kinds: dict[str, int]
    scores: list[MetricScore]
    calibration: list[Calibration]
    skill: list[Comparison]
    power: list[PowerAnalysis]
    pnl: PnlResult | None
    median_lead_ns: int | None


class AnchorEcho(QtModel):
    """One anchored commitment that was checked against the ledger."""

    path: str
    commitment_sha256: Sha256
    head_seq: int
    head_sha256: Sha256
    anchored_at_ns: int


class LedgerEcho(QtModel):
    """The ledger that was scored, and the anchored commitments it was checked against."""

    path: str
    ledger_id: Sha256
    head_seq: int
    head_sha256: Sha256
    n_entries: int
    verified: bool
    anchors: list[AnchorEcho] = Field(default_factory=list)


class ScoreCounts(QtModel):
    """Forecast entries by eligibility status, and resolved questions."""

    forecasts: int
    by_status: dict[str, int]
    questions: int
    questions_resolved: int
    questions_void: int
    forecasters: int


class Rejection(QtModel):
    """One rejected forecast entry (the first 1,000; all of them are in `forecast_eligibility.parquet`)."""

    seq: int
    question_id: str
    forecaster: str
    status: Status
    detail: str


class ForecastScorecard(QtDoc):
    """`qt.forecast_scorecard` (`forecast score`, `forecast_scorecard.json`)."""

    SCHEMA: ClassVar[str] = "qt.forecast_scorecard"
    schema_: Literal["qt.forecast_scorecard"] = _tag("qt.forecast_scorecard")
    schema_version: Literal[1] = 1
    run_id: RunId
    engine_version: str
    spec: dict[str, Any]
    ledger: LedgerEcho
    counts: ScoreCounts
    forecasters: list[ForecasterScore]
    comparisons: list[Comparison]
    rejections: list[Rejection]
    rejections_truncated: bool
    files: list[FileEntry]
    warnings: list[str]


FORECAST_DOC_MODELS: dict[str, type[QtDoc]] = {
    m.SCHEMA: m for m in (ForecastEntry, ForecastCommitment, ForecastLedgerSummary, ForecastScorecard)
}
DOC_MODELS.update(FORECAST_DOC_MODELS)

__all__ = [
    "FORECAST_DOC_MODELS",
    "REJECT_STATUSES",
    "Anchor",
    "AnchorEcho",
    "BinaryForecast",
    "Calibration",
    "CategoricalForecast",
    "Comparison",
    "ContinuousForecast",
    "DmTest",
    "FeeSchedule",
    "FileRef",
    "ForecastCommitment",
    "ForecastEntry",
    "ForecastLedgerSummary",
    "ForecastPayload",
    "ForecastScoreSpec",
    "ForecastScorecard",
    "ForecasterScore",
    "LedgerEcho",
    "LedgerEntryRef",
    "LedgerProblem",
    "MetricScore",
    "NormalDist",
    "PnlResult",
    "PnlSpec",
    "PowerAnalysis",
    "QuantileDist",
    "Rejection",
    "ReliabilityBin",
    "ResolutionPayload",
    "SampleDist",
    "ScoreCounts",
    "Selection",
]
