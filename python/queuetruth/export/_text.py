"""Shared rendering helpers of the export templates (SPEC 5.10): `{x:.6g}` numbers, window and data-source text,
and the mapping from a calibration family to the hftbacktest builder call."""

from __future__ import annotations

import math

from queuetruth._version import __version__
from queuetruth.schemas.models import CalibrationResult, WindowEcho

VERSION = __version__
#: `param_name` values meaning the estimate is on the ln n scale (SPEC 5.4 C1 optimizes theta = ln n).
LOG_N_NAMES = ("ln_n", "log_n", "theta")


def g6(x: float) -> str:
    """`format(x, ".6g")` (the SPEC 5.10 substitution format)."""
    return format(float(x), ".6g")


def _ranges(idx: list[int]) -> str:
    out: list[str] = []
    xs = sorted(set(idx))
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[j] + 1:
            j += 1
        out.append(str(xs[i]) if i == j else f"{xs[i]}-{xs[j]}")
        i = j + 1
    return ",".join(out)


def window_text(w: WindowEcho) -> str:
    """`2019-12-30,2019-12-31` | `blocks 0-3/5` | `all data`."""
    if w.dates:
        return ",".join(w.dates)
    if w.blocks is not None:
        return f"blocks {_ranges(w.blocks.index)}/{w.blocks.n_blocks}"
    return "all data"


def data_sources_text(cal: CalibrationResult) -> str:
    """One `source_venue (validation_status, dataset <id12>)` item per data source, `; `-joined."""
    if not cal.data_sources:
        return "none recorded"
    return "; ".join(f"{d.source_venue} ({d.validation_status}, dataset {d.dataset_id[:12]})" for d in cal.data_sources)


def family_key(family: str) -> str:
    """Normalized family: `hbt_power3` -> `power3`, `databento_mc` -> `databento`, `hbt_risk_adverse` ->
    `risk_adverse`."""
    f = family.strip().lower()
    if f.startswith("hbt_"):
        f = f[4:]
    if f == "databento_mc":
        f = "databento"
    return f


def power_n(cal: CalibrationResult) -> float | None:
    """`n` of a power-family fit: `exp(estimate)` when `param_name` names the ln n scale, else `estimate`."""
    if cal.estimate is None or not math.isfinite(cal.estimate):
        return None
    if (cal.param_name or "").lower() in LOG_N_NAMES:
        return math.exp(cal.estimate)
    return float(cal.estimate)


def spec_value(cal: CalibrationResult, key: str, default: str) -> str:
    """A string field of the echoed spec (e.g. `exchange`), or `default`."""
    v = cal.spec.get(key) if isinstance(cal.spec, dict) else None
    return v if isinstance(v, str) else default
