"""Result types shared by the statistics functions (SPEC 4.3 `queuetruth.stats`)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class McNemarResult:
    """McNemar test on discordant counts (SPEC 5.7.5)."""

    b: int
    c: int
    chi2_cc: float
    p_chi2: float
    p_exact: float
    odds_ratio: float | None
    or_ci: tuple[float, float] | None


@dataclass(frozen=True)
class WilcoxonResult:
    """Wilcoxon signed-rank result (matches `scipy.stats.wilcoxon`)."""

    statistic: float
    pvalue: float


@dataclass(frozen=True)
class TestResult:
    """A test statistic and its p-value."""

    __test__ = False  # not a pytest class

    statistic: float
    p_value: float


@dataclass(frozen=True)
class StepFunction:
    """Right-continuous step function: `value[i]` holds on `[time[i], time[i + 1])`; `time[0] = 0` for estimators."""

    time: np.ndarray
    value: np.ndarray

    def at(self, t: float | np.ndarray, before: float = np.nan) -> float | np.ndarray:
        """Value at `t` (right-continuous); `before` for `t < time[0]`."""
        tt = np.asarray(t, dtype=float)
        i = np.searchsorted(self.time, tt, side="right") - 1
        out = np.where(i >= 0, self.value[np.clip(i, 0, None)], before)
        return float(out) if out.ndim == 0 else out

    def left(self, t: float | np.ndarray, before: float = np.nan) -> float | np.ndarray:
        """Left limit at `t` (value on the last step starting strictly before `t`); `before` if none."""
        tt = np.asarray(t, dtype=float)
        i = np.searchsorted(self.time, tt, side="left") - 1
        out = np.where(i >= 0, self.value[np.clip(i, 0, None)], before)
        return float(out) if out.ndim == 0 else out


@dataclass(frozen=True)
class CI:
    """Point estimate with a percentile confidence interval."""

    estimate: float
    lo: float
    hi: float
