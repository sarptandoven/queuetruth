"""Plain-word lines of a scorecard summary that more than one command prints: the time-to-fill line and the markout
line of hftbacktest discussion #332, and the small formatters they use.

`queuetruth score` (`queuetruth.cli`) and `queuetruth compare` (`queuetruth.compare`) both print these lines, so
they live here, below both.
"""

from __future__ import annotations

import math
from typing import Any

from queuetruth import units

#: Markout horizons of the terminal answer (hftbacktest discussion #332): 100 ms, 1 s, 10 s.
PRACTITIONER_HORIZONS_NS = (100_000_000, 1_000_000_000, 10_000_000_000)


def pct_points(x: float | None, signed: bool = True) -> str:
    """A fraction as percentage points (`+1.3`)."""
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{x * 100:+.1f}" if signed else f"{abs(x) * 100:.1f}"


def pvalue(x: float | None) -> str:
    """A p-value with three significant digits."""
    return "n/a" if x is None else f"{x:.3g}"


def horizon(ns: int | str | None) -> str:
    """Horizon label: `100ms`, `1s`, `1min` (`queuetruth.units.format_duration`)."""
    return "-" if ns is None else units.format_duration(int(ns))


def pct(x: float | None) -> str:
    """A fraction as a whole percentage (`42%`)."""
    return "n/a" if x is None or not math.isfinite(x) else f"{x * 100:.0f}%"


def practitioner_lines(doc: dict[str, Any]) -> list[str]:
    """The time-to-fill line (Kaplan-Meier share still unfilled at 1 s, else the first scored horizon, with the
    clustered log-rank) and the markout line (sim minus live first-fill markout at 100 ms, 1 s and 10 s)."""
    out: list[str] = []
    sv = doc.get("ttf_survival")
    if isinstance(sv, dict) and sv.get("arms"):
        hs = list(sv.get("horizons_ns") or [])
        if hs:
            j = hs.index(1_000_000_000) if 1_000_000_000 in hs else 0
            arms = sv["arms"]

            def at(arm: str) -> str:
                a = (arms.get(arm) or {}).get("at") or []
                x = a[j] if j < len(a) else {}
                if x.get("survival") is None:
                    return f"{arm} n/a"
                ci = (
                    f" [{pct(x['ci_lo'])}, {pct(x['ci_hi'])}]"
                    if x.get("ci_lo") is not None and x.get("ci_hi") is not None
                    else ""
                )
                return f"{arm} {pct(x['survival'])}{ci}"

            lr = sv.get("logrank") or {}
            d = (sv.get("diff") or [{}] * (j + 1))[j]
            diff = ""
            if d.get("diff") is not None:
                diff = f"; sim minus live {pct_points(d['diff'])} pp"
                if d.get("ci_lo") is not None and d.get("ci_hi") is not None:
                    diff += f" (95% CI {pct_points(d['ci_lo'])} to {pct_points(d['ci_hi'])})"
            out.append(
                f"time to fill: still unfilled after {horizon(hs[j])} (Kaplan-Meier, cancels censored): "
                f"{at('live')} vs {at('sim')}{diff}; log-rank p = {pvalue(lr.get('p_value'))} (by instrument, "
                f"{lr.get('n_clusters')} clusters)"
            )
    diffs = {
        int(r["horizon_ns"]): r
        for r in doc.get("markout_diff") or []
        if isinstance(r, dict) and r.get("ref") == "mid" and r.get("mean_diff_ticks") is not None
    }
    parts = []
    for h in PRACTITIONER_HORIZONS_NS:
        r = diffs.get(h)
        if r is None:
            continue
        has_ci = r.get("ci_lo") is not None and r.get("ci_hi") is not None
        ci = f" [{r['ci_lo']:+.2f}, {r['ci_hi']:+.2f}]" if has_ci else ""
        parts.append(f"{horizon(h)} {r['mean_diff_ticks']:+.2f}{ci}")
    if parts:
        out.append("markouts: sim minus live, ticks of mid, orders filled in both (95% CI): " + ", ".join(parts))
    return out


__all__ = ["PRACTITIONER_HORIZONS_NS", "horizon", "pct", "pct_points", "practitioner_lines", "pvalue"]
