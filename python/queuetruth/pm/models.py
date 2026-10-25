"""Specs and summary documents of `queuetruth pm` (Pydantic v2; JSON Schemas in `queuetruth/pm/schemas/`).

The documents follow SPEC 3.10 conventions (`schema`, `schema_version: 1`, no NaN) and the run identity of SPEC
7.3 (`run_id` over the spec without `out`, input files by sha256). They are Python-only: they are not registered in
`queuetruth.schemas.DOC_MODELS`, whose JSON Schemas the Rust core embeds; `queuetruth.pm.schemas.export()` writes
theirs and a test keeps the committed files current.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar, Literal, TypeVar

from pydantic import Field, ValidationError

from queuetruth.errors import QtError
from queuetruth.schemas.models import FileEntry, QtDoc, QtModel, RunId, Sha256, _tag
from queuetruth.schemas.specs import PathRef, SpecModel

NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(ge=1)]
Venue = Literal["kalshi", "polymarket"]
#: A non-negative decimal amount written as text (`"0.0175"`), so money never passes through a binary float.
Dollars = Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]+)?$")]

S = TypeVar("S", bound=SpecModel)


def validate_spec(model: type[S], spec: S | dict[str, Any]) -> S:
    """`spec` as `model`; a dict that does not validate is `QT-CONFIG-001` naming the first bad field."""
    if isinstance(spec, model):
        return spec
    try:
        return model.model_validate(spec)
    except ValidationError as e:
        errors = json.loads(e.json(include_url=False))
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        msg = f"{model.__name__}: {loc + ': ' if loc else ''}{first.get('msg', 'invalid spec')}"
        if len(errors) > 1:
            msg += f" (+{len(errors) - 1} more)"
        raise QtError("QT-CONFIG-001", msg, details={"errors": errors}) from None


#: Queue-model families of `queuetruth.pm.queue` (`power3` etc. take a parameter `n`).
FAMILIES = ("power3", "power2", "power", "log", "log2", "risk_adverse", "cancels_ahead", "front_of_queue")


# ---- specs ------------------------------------------------------------------------------------------------------


class PmIngestSpec(SpecModel):
    """`pm ingest`: the user's own captures -> datasets, live log and tables."""

    venue: Venue
    inputs: Annotated[list[PathRef], Field(min_length=1)]
    #: Polymarket only: Polygon log exports (`eth_getLogs` JSON or CSV) for the on-chain constrained replay.
    chain: list[PathRef] = Field(default_factory=list)
    out: str | None = None
    account: str = "default"
    #: Kalshi only: NO-level pricing of the orderbook channel (auto = from the captured subscribe command).
    kalshi_yes_price: Literal["auto", "true", "false"] = "auto"
    #: Polymarket only: condition id -> [YES token, NO token] when the captures carry no market metadata.
    markets: dict[str, tuple[str, str]] = Field(default_factory=dict)
    #: Symbol -> instrument id (default 1.. in sorted symbol order).
    instrument_ids: dict[str, NonNegInt] = Field(default_factory=dict)
    #: Window for attaching prints to book changes (default 1 s Kalshi, 2 s Polymarket).
    match_window_ns: NonNegInt | None = None
    #: Window for placing own-order changes on book changes (default: `match_window_ns`).
    own_match_window_ns: NonNegInt | None = None
    #: Polymarket only: decode only these exchange contract addresses (default every exchange event).
    exchanges: list[str] | None = None


class PmCalibrationSpec(SpecModel):
    """`pm calibrate`: queue-position models against Kalshi's official queue positions, and the queue truth at
    each own maker fill (both venues)."""

    pm: PathRef
    out: str | None = None
    families: list[str] = Field(default_factory=lambda: ["power3", "power2", "power", "log", "log2"])
    #: Also fit each family's `atomic` variant (a decrease beyond the prints is one order leaving).
    atomic: bool = True
    #: The loss to fit and rank by: `rmse` scores the expected queue; `mae` scores the median, and since a cancel is
    #: either ahead or behind, it rewards models that put every ambiguous cancel on one side (a biased queue).
    loss: Literal["rmse", "mae"] = "rmse"
    #: Grid of ln n for the families with a parameter.
    grid_lo: float = -4.0
    grid_hi: float = 4.0
    grid_n: Annotated[int, Field(ge=2, le=401)] = 33
    #: Fraction of own orders (by entry time) that trains; the rest tests.
    train_fraction: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.5
    #: Where a queue sample sits on the book's clock: `recv` (local receive time) or `mid` (midpoint of the local
    #: send and receive times when the request was captured).
    sample_time: Literal["recv", "mid"] = "mid"
    bootstrap_b: Annotated[int, Field(ge=0)] = 500
    seed: NonNegInt = 0


class PmScoreSpec(SpecModel):
    """`pm score`: reward-aware market-making PnL band and the sim-versus-live comparison."""

    pm: PathRef
    out: str | None = None
    #: Hypothetical quotes (CSV / Parquet, see the how-to); without them the user's own live orders are scored.
    quotes: PathRef | None = None
    #: The fitted model: a `pm calibrate` result, or a model id (`power3[n=1.5]`, `risk_adverse`, ...).
    calibration: PathRef | None = None
    model: str | None = None
    #: Settlement value of YES per instrument symbol in dollars (overrides the captured result); unsettled markets
    #: are marked to the last mid.
    settle: dict[str, Dollars] = Field(default_factory=dict)
    member: Literal["direct", "non_direct"] = "direct"
    #: Own orders without a cancel instruction (filled live) rest at most this long in the simulation, and both arms
    #: of the sim-versus-live comparison look this far after entry (the scorer's window, SPEC 5.7).
    window_ns: PosInt = 60_000_000_000
    markout_horizons_ns: list[NonNegInt] = Field(
        default_factory=lambda: [1_000_000_000, 10_000_000_000, 60_000_000_000, 300_000_000_000]
    )
    #: Kalshi `flat` series: per-contract fees in dollars.
    flat_taker: Dollars | None = None
    flat_maker: Dollars | None = None
    #: Polymarket liquidity rewards: in-game multiplier and the daily reward when not in the captured metadata.
    poly_multiplier: float = 1.0
    poly_daily_reward: Dollars | None = None
    #: Polymarket fee category when the market metadata has no fee schedule (e.g. `sports`).
    poly_category: str | None = None
    #: Your actual payouts to compare with (dollars): Kalshi LIP and Polymarket rewards / rebates for the window.
    actual_rewards: Dollars | None = None
    bootstrap_b: Annotated[int, Field(ge=0)] = 500
    seed: NonNegInt = 0


# ---- documents --------------------------------------------------------------------------------------------------


class DatasetBrief(QtModel):
    """One written dataset."""

    dataset_id: Sha256
    records: NonNegInt
    verdict: Literal["clean", "warnings", "unusable"]
    reasons: list[str]


class InstrumentBrief(QtModel):
    """One instrument of an ingest."""

    instrument_id: NonNegInt
    symbol: str
    tick_size: PosInt
    size_exp: int
    batches: NonNegInt
    trades: NonNegInt
    trades_matched: NonNegInt
    trades_unmatched: NonNegInt
    block_trades: NonNegInt
    feed_gaps: NonNegInt


class PmIngestSummary(QtDoc):
    """Summary of `pm ingest` (`pm_ingest.json`)."""

    SCHEMA: ClassVar[str] = "qt.pm_ingest_summary"
    schema_: Literal["qt.pm_ingest_summary"] = _tag("qt.pm_ingest_summary")
    schema_version: Literal[1] = 1
    run_id: RunId
    spec: dict[str, Any]
    venue: Venue
    terms: str
    book: DatasetBrief
    book_own_free: DatasetBrief | None
    live: dict[str, int] | None
    own_free: dict[str, int] | None
    queue_samples: NonNegInt
    chain: dict[str, int] | None
    instruments: list[InstrumentBrief]
    counters: dict[str, int]
    warnings: list[str]
    files: list[FileEntry]


class ModelFit(QtModel):
    """One queue model's errors (estimate minus truth) against the calibration target, in contracts (Kalshi) or
    shares (Polymarket). Losses are order-weighted: each order's errors are reduced first, then averaged over
    orders."""

    model_id: str
    family: str
    n: float | None
    #: The one-order-per-change variant (`queuetruth.pm.queue`).
    atomic: bool = False
    #: The same model in the zoo's spelling (`queuetruth zoo --models`, `calibrate-live`), None when the zoo has no
    #: such model (`cancels_ahead`, the atomic variants, the bounds midpoint).
    zoo_id: str | None = None
    #: The document's `loss` on the train and test orders, and a percentile bootstrap interval over test orders.
    train_loss: float | None
    test_loss: float | None
    test_loss_ci: tuple[float | None, float | None]
    test_mae: float | None
    test_rmse: float | None
    #: Mean signed error and median absolute error of the test samples (negative bias: the model's queue is too
    #: short, so its fills come too early).
    test_bias: float | None
    test_median_abs: float | None
    n_train_samples: NonNegInt
    n_test_samples: NonNegInt
    #: Order-weighted mean absolute error and mean signed error of the queue ahead just before each own order's
    #: first filling batch.
    fill_truth_mae: float | None
    fill_truth_bias: float | None
    n_fill_truth: NonNegInt
    #: Of the test legs (an order at one price) that never filled live, how many the model filled while they rested
    #: (a queue that is too short fills orders that did not fill); None for the bounds midpoint, which is not replayed.
    unfilled_filled: NonNegInt | None = None


class PmCalibrationTrust(QtModel):
    """Whether `pm score --calibration` should use the fitted model (`queuetruth.pm.calibrate.PM_REFUSING`), in the
    form of `calibrate-live`'s `trust`."""

    #: `usable`, or `refused` (`pm score --calibration` warns and repeats the verdict in its headline).
    verdict: Literal["usable", "refused"]
    #: Refusing tokens in `PM_REFUSING` order (empty when usable).
    reasons: list[str]
    #: Every diagnostic token present, refusing or not (`fill_truth_selection`), sorted.
    flags: list[str]


class FillTruth(QtModel):
    """Queue truth at the first maker fill of own orders (contracts / shares): the size that left the queue ahead by
    cancels between entry and that fill (mean per order), what each model implies, the share of all cancels at the
    level in that time that were ahead of the order, and orders whose true queue ahead fell outside the bounds.
    `n_chain` of the `n_orders` orders take their truth from the on-chain fill order (Polymarket)."""

    n_orders: NonNegInt
    n_chain: NonNegInt
    cancel_ahead_true: float | None
    by_model: dict[str, float | None]
    fraction_ahead_true: float | None
    bound_violations: NonNegInt


class PmCalibration(QtDoc):
    """Result of `pm calibrate` (`pm_calibration.json`)."""

    SCHEMA: ClassVar[str] = "qt.pm_calibration"
    schema_: Literal["qt.pm_calibration"] = _tag("qt.pm_calibration")
    schema_version: Literal[1] = 1
    run_id: RunId
    spec: dict[str, Any]
    venue: Venue
    terms: str
    #: `run_id` of the `pm ingest` the fit ran on (its content identity).
    pm_run_id: RunId
    #: What the fit minimised: official queue positions (Kalshi) or the queue ahead at own fills.
    fit_on: Literal["queue_samples", "fill_truth"]
    #: The loss the fit minimised and the models are ranked by (`PmCalibrationSpec.loss`).
    loss: Literal["rmse", "mae"]
    #: Unit of every error and size in the document.
    units: Literal["contracts", "shares"]
    n_orders: NonNegInt
    n_samples: NonNegInt
    n_train_orders: NonNegInt
    n_test_orders: NonNegInt
    #: Orders of each half with queue truth (official positions, or fill truth when the fit is on it).
    n_train_orders_with_truth: NonNegInt
    n_test_orders_with_truth: NonNegInt
    #: The split: orders are sorted by (first entry time ns, order key) and test from this pair on (None: no test
    #: orders); `pm score` uses it to report the sim-versus-live comparison of the test orders on the same capture.
    test_from: tuple[int, str] | None
    #: Test legs that never filled live (the denominator of `ModelFit.unfilled_filled`).
    n_unfilled_test_orders: NonNegInt
    bounds_coverage: float | None
    best: str | None
    best_beats_bounds_midpoint: bool | None
    trust: PmCalibrationTrust
    models: list[ModelFit]
    fill_truth: FillTruth
    warnings: list[str]
    files: list[FileEntry]


class Band(QtModel):
    """A quantity under the pessimistic and optimistic fill bounds and the calibrated model (dollars)."""

    lower: float | None
    model: float | None
    upper: float | None


class PnlParts(QtModel):
    """PnL decomposition of one fill scenario (dollars): `trading = spread_capture - adverse_selection` (fills
    marked to the settlement or the last mid), `total = trading - fees + rewards + rebates`; `position` is the
    ending YES position in contracts / shares."""

    fills: NonNegInt
    filled_qty: float
    spread_capture: float | None
    adverse_selection: float | None
    trading: float | None
    fees: float
    rewards: float
    rewards_lower: float
    rewards_upper: float
    rebates: float
    total: float | None
    position: float


class SimVsLive(QtModel):
    """The user's own orders replayed under a fill model versus what happened live."""

    model_id: str
    #: Which orders: `all`, or the calibration's `train` / `test` orders when the model was fitted on this capture
    #: (the train rows are in-sample).
    orders: Literal["all", "train", "test"] = "all"
    n_orders: NonNegInt
    both: NonNegInt
    neither: NonNegInt
    live_only: NonNegInt
    sim_only: NonNegInt
    fill_rate_live: float | None
    fill_rate_sim: float | None
    fill_rate_bias: float | None
    fill_rate_bias_ci: tuple[float | None, float | None]
    mcnemar_p: float | None
    qty_live: float
    qty_sim: float
    ttf_log_ratio_median: float | None
    bracket_violations: NonNegInt


class PmScore(QtDoc):
    """Result of `pm score` (`pm_score.json`)."""

    SCHEMA: ClassVar[str] = "qt.pm_score"
    schema_: Literal["qt.pm_score"] = _tag("qt.pm_score")
    schema_version: Literal[1] = 1
    run_id: RunId
    spec: dict[str, Any]
    venue: Venue
    terms: str
    source: Literal["quotes", "live"]
    model_id: str
    #: Where the model came from: `calibration` (`pm calibrate`), `given` (`model`) or `default` (uncalibrated).
    model_source: Literal["calibration", "given", "default"]
    #: The calibration's trust verdict when the model came from one.
    calibration_trust: PmCalibrationTrust | None
    n_orders: NonNegInt
    mark: dict[str, Any]
    scenarios: dict[str, PnlParts]
    band: dict[str, Band]
    markouts: list[dict[str, Any]]
    rewards: dict[str, Any]
    live: PnlParts | None
    sim_vs_live: list[SimVsLive]
    #: Percentile bootstrap interval over orders of the fitted model's trading PnL net of fees (not rewards).
    trading_pnl_ci: tuple[float | None, float | None]
    warnings: list[str]
    files: list[FileEntry]


class PmFixtures(QtDoc):
    """What `pm fixtures` wrote (printed; the files are the output)."""

    SCHEMA: ClassVar[str] = "qt.pm_fixtures"
    schema_: Literal["qt.pm_fixtures"] = _tag("qt.pm_fixtures")
    schema_version: Literal[1] = 1
    out: str
    #: What the files are: invented captures in the venues' documented formats, no venue data.
    note: str
    files: list[FileEntry]


DOC_MODELS: dict[str, type[QtDoc]] = {
    "qt.pm_ingest_summary": PmIngestSummary,
    "qt.pm_calibration": PmCalibration,
    "qt.pm_score": PmScore,
    "qt.pm_fixtures": PmFixtures,
}
SPEC_MODELS: dict[str, type[SpecModel]] = {
    "qt.pm_ingest_spec": PmIngestSpec,
    "qt.pm_calibration_spec": PmCalibrationSpec,
    "qt.pm_score_spec": PmScoreSpec,
}

__all__ = [
    "DOC_MODELS",
    "FAMILIES",
    "SPEC_MODELS",
    "Band",
    "DatasetBrief",
    "Dollars",
    "FillTruth",
    "InstrumentBrief",
    "ModelFit",
    "PmCalibration",
    "PmCalibrationSpec",
    "PmCalibrationTrust",
    "PmFixtures",
    "PmIngestSpec",
    "PmIngestSummary",
    "PmScore",
    "PmScoreSpec",
    "PnlParts",
    "SimVsLive",
    "validate_spec",
]
