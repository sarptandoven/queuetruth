"""JSON document and settings of the conformal fill probabilities (extension `conformal-fill`,
docs/design/extensions/conformal-fill.md): `qt.conformal_report` (`conformal.json`, registered in `DOC_MODELS`) and
its parts, and `ConformalSpec`, the settings it echoes. The computations are in `queuetruth.model.conformal`, which
re-exports every name here.

This module imports only Pydantic and `queuetruth.schemas.models` base classes, so `queuetruth.schemas.models` can
register the report in `DOC_MODELS` at import time.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from queuetruth.schemas.models import HEX16, QtDoc, QtModel, _tag

REPORT_SCHEMA = "qt.conformal_report"

StratumName = Literal["dist_bucket", "side", "instrument", "predicted"]


class ConformalSpec(QtModel):
    """Settings of a conformal evaluation (`evaluate`) and of a `ConformalCalibrator`."""

    alpha: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.1
    strata: list[StratumName] = Field(default_factory=lambda: ["dist_bucket"])
    label_conditional: bool = False
    smoothed: bool = False
    seed: Annotated[int, Field(ge=0, lt=2**64)] = 0
    martingale: Literal["jumper", "mixture"] = "jumper"
    jumper_rate: Annotated[float, Field(gt=0.0, le=1.0)] = 0.01
    alarm_threshold: Annotated[float, Field(gt=1.0)] = 100.0
    shuffle_within_day: bool = True


class CoverageCell(QtModel):
    """Prediction-set coverage of one horizon x landmark x stratum (`all` = pooled) on the test rows.

    `n_calib` is the calibration size of the category (with `label_conditional`, the smaller of the two per-label
    sizes); `informative` = it reaches `queuetruth.model.conformal.min_informative_n(alpha)`. Below that size
    conservative sets always hold both labels, and smoothed sets are randomized (a label is dropped at random, with
    probability up to `alpha`) and carry no information about the row. `coverage_fill` / `coverage_nofill` are the
    coverage among the `n_test_fill` test rows that filled / the others (guaranteed only with `label_conditional`).
    `va_p0_mean`, `va_p1_mean` average the Venn-Abers interval; `va_true_label_mean` averages the probability
    computed with the observed label, which equals the fill rate in expectation under exchangeability."""

    horizon_ns: int
    landmark_ns: int | None
    stratum: str
    n_calib: int
    n_test: int
    n_test_fill: int
    coverage: float | None
    coverage_ci_lo: float | None
    coverage_ci_hi: float | None
    frac_empty: float | None
    frac_singleton: float | None
    frac_both: float | None
    fill_rate_calib: float | None
    fill_rate_test: float | None
    mean_p_test: float | None
    va_p0_mean: float | None
    va_p1_mean: float | None
    va_true_label_mean: float | None
    coverage_fill: float | None
    coverage_nofill: float | None
    informative: bool


class FillTimeCell(QtModel):
    """Coverage of the conformal fill-time bounds of one landmark x stratum (`all` = pooled) on the test rows."""

    landmark_ns: int | None
    stratum: str
    n_calib: int
    n_test: int
    lower_coverage: float | None
    upper_coverage: float | None
    interval_coverage: float | None
    frac_lower_claim: float | None
    frac_upper_claim: float | None
    median_lower_ns: int | None
    median_upper_ns: int | None


class DriftGroup(QtModel):
    """Martingale statistics of one (role, session date) group, in processing order (`group` = `<role>:<date>`, or
    `<role>` when the dates are unknown)."""

    group: str
    role: Literal["calib", "test"]
    date: str | None
    n: int
    log10_end: float
    log10_max: float
    mean_pvalue: float
    ks_uniform: float


class DriftCell(QtModel):
    """Exchangeability martingale of the calibration rows, then the test rows, of one horizon x landmark. `alarm` is
    at the per-cell level `1 / alarm_threshold`."""

    horizon_ns: int
    landmark_ns: int
    method: str
    n: int
    log10_final: float
    log10_max: float
    alarm: bool
    alarm_group: str | None
    groups: list[DriftGroup]


class ConformalReport(QtDoc):
    """`conformal.json`: coverage of conformal fill sets, Venn-Abers intervals and fill-time bounds on the test rows
    and the exchangeability martingale (extension `conformal-fill`). `drift_familywise_alarm` = some drift cell
    reached `drift_familywise_threshold` = `alarm_threshold x len(drift)` (Bonferroni: family-wise false-alarm
    probability at most `1 / alarm_threshold` under exchangeability)."""

    SCHEMA: ClassVar[str] = REPORT_SCHEMA
    schema_: Literal["qt.conformal_report"] = _tag(REPORT_SCHEMA)
    schema_version: Literal[1] = 1
    run_id: Annotated[str, Field(pattern=HEX16)]
    spec: dict[str, Any]
    source: Literal["hazard", "zoo"]
    model_id: str
    score: str
    alpha: float
    grid_ns: list[int]
    n_calib_rows: int
    n_test_rows: int
    calib_dates: list[str]
    test_dates: list[str]
    guarantee: str
    cells: list[CoverageCell]
    fill_time: list[FillTimeCell]
    drift: list[DriftCell]
    drift_familywise_threshold: float | None
    drift_familywise_alarm: bool
    warnings: list[str]


#: The conformal documents by schema name (merged into `queuetruth.schemas.models.DOC_MODELS`).
CONFORMAL_DOC_MODELS: dict[str, type[QtDoc]] = {REPORT_SCHEMA: ConformalReport}

__all__ = [
    "CONFORMAL_DOC_MODELS",
    "REPORT_SCHEMA",
    "ConformalReport",
    "ConformalSpec",
    "CoverageCell",
    "DriftCell",
    "DriftGroup",
    "FillTimeCell",
    "StratumName",
]
