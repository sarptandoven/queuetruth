"""Leaderboard presentation (trader review C1, top-10 item 4): rank the zoo models by the absolute fill-rate bias at a
horizon that matters and state each model's bias in one plain-English sentence.

- `sort_horizon(horizons, target)`: `target` (default 60 s) when it is one of the horizons, else the largest horizon
  below it, else the smallest horizon (None without horizons).
- `rank_leaderboard(entries, horizon_ns)`: entries ordered by `|fill_rate_bias(h)|` at the sort horizon (models
  without a value last), then by Brier at that horizon, then by model id. Brier alone ties when most models never
  fill at short horizons (Brier 0.0000 for 15 of 17 models at 1 s in the review), so it is only a tie-break.
- `bias_sentence(entry, horizon_ns, aborts=None)`: "At 60 s, hbt_power3[n=2] fills 10.3% of orders vs 11.0% in the
  exact replay, 0.7 pp less often (95% CI -1.3 to -0.3 pp)."; a CI containing zero reads "..., within noise (95% CI
  ...)". The two rates are the entry's `model_fill_rate` and `exact_fill_rate` (0.1.1); a summary without them reads
  "fills 0.7 pp less often than the exact replay (...)". With `aborts` (`abort_shares` of the zoo summary's warnings)
  a model with faithful hbt #312 aborts adds "; 12 of 3,000 shadow pairs (0.4%) aborted at the hbt #312 event
  (excluded)".
- `rates_at(entry, horizon_ns)`: (model fill rate, exact fill rate) at the horizon.
- `abort_shares(warnings)`: model id -> (aborted pairs, shadows) from the `hbt_312_abort: ` warning lines.

Entries are `qt.zoo_summary.leaderboard` items, as dicts (`evaluate_zoo`) or `LeaderboardEntry` models; the bias is
model minus exact truth on never-cancel shadows whose label exists at the horizon (SPEC 5.6, audit ruling A2).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

DEFAULT_SORT_HORIZON_NS = 60_000_000_000
#: Biases smaller than this (in absolute value) read "as often as".
NEGLIGIBLE_BIAS = 0.0005


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _finite(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def bias_at(entry: Any, horizon_ns: int | None) -> tuple[float | None, float | None, float | None]:
    """(value, ci_lo, ci_hi) of the entry's `fill_rate_bias` at the horizon (Nones when absent)."""
    if horizon_ns is None:
        return None, None, None
    fb = _field(entry, "fill_rate_bias") or {}
    cv = fb.get(str(int(horizon_ns))) if isinstance(fb, dict) else None
    if cv is None:
        return None, None, None
    return _finite(_field(cv, "value")), _finite(_field(cv, "ci_lo")), _finite(_field(cv, "ci_hi"))


def rates_at(entry: Any, horizon_ns: int | None) -> tuple[float | None, float | None]:
    """(model fill rate, exact fill rate) of the entry at the horizon (Nones when absent, e.g. before 0.1.1)."""
    if horizon_ns is None:
        return None, None
    key = str(int(horizon_ns))
    out = []
    for name in ("model_fill_rate", "exact_fill_rate"):
        m = _field(entry, name) or {}
        out.append(_finite(m.get(key)) if isinstance(m, dict) else None)
    return out[0], out[1]


def brier_at(entry: Any, horizon_ns: int | None) -> float | None:
    """The entry's Brier at the horizon (None when absent)."""
    if horizon_ns is None:
        return None
    b = _field(entry, "brier") or {}
    return _finite(b.get(str(int(horizon_ns)))) if isinstance(b, dict) else None


def horizons_of(entries: Iterable[Any]) -> list[int]:
    """Horizons present in the entries' `fill_rate_bias` or `brier` maps, ascending."""
    out: set[int] = set()
    for e in entries:
        for name in ("fill_rate_bias", "brier"):
            m = _field(e, name) or {}
            if isinstance(m, dict):
                out.update(int(k) for k in m)
    return sorted(out)


def sort_horizon(horizons: Sequence[int], target_ns: int = DEFAULT_SORT_HORIZON_NS) -> int | None:
    """The horizon the leaderboard is sorted at (module docstring)."""
    hs = sorted({int(h) for h in horizons})
    if not hs:
        return None
    if int(target_ns) in hs:
        return int(target_ns)
    below = [h for h in hs if h < int(target_ns)]
    return below[-1] if below else hs[0]


def rank_leaderboard(entries: Sequence[Any], horizon_ns: int | None) -> list[Any]:
    """Entries sorted by absolute fill-rate bias at `horizon_ns`, then Brier, then model id."""

    def key(e: Any) -> tuple[bool, float, float, str]:
        v = bias_at(e, horizon_ns)[0]
        b = brier_at(e, horizon_ns)
        bias = abs(v) if v is not None else math.inf
        return (v is None, bias, b if b is not None else math.inf, str(_field(e, "model_id")))

    return sorted(entries, key=key)


def duration_text(ns: int) -> str:
    """`60 s`, `500 ms`, `1.5 s`, `5 min` (plain ASCII; minutes from 5 min on)."""
    v = float(ns)
    for unit, scale in (("min", 6e10), ("s", 1e9), ("ms", 1e6), ("us", 1e3)):
        if v >= scale and (unit != "min" or (v % scale == 0 and v >= 3e11)):
            return f"{v / scale:g} {unit}"
    return f"{int(ns)} ns"


def pct(x: float) -> str:
    """A fraction as a percentage: one decimal (two below 0.1%), no sign (`11.0`, `0.04`)."""
    v = 100.0 * x
    return f"{v:.{1 if abs(v) >= 0.1 or v == 0 else 2}f}"


def _pp(x: float, signed: bool = True) -> str:
    """Percentage points of a fraction: one decimal (two below 0.1 pp), explicit sign unless zero or unsigned."""
    v = 100.0 * x
    digits = 1 if abs(v) >= 0.1 or v == 0 else 2
    if not signed:
        return f"{abs(v):.{digits}f}"
    s = f"{v:+.{digits}f}"
    return s[1:] if float(s) == 0 else s


ABORT_PREFIX = "hbt_312_abort: "


def abort_shares(warnings: Iterable[str] | None) -> dict[str, tuple[int, int]]:
    """Model id -> (aborted pairs, shadows) from a zoo summary's `hbt_312_abort: <model>: <n> of <N> shadow
    pair(s) ...` warning lines (faithful hbt #312 aborts, per (model, shadow) pair)."""
    out: dict[str, tuple[int, int]] = {}
    for w in warnings or ():
        if not isinstance(w, str) or not w.startswith(ABORT_PREFIX):
            continue
        rest = w[len(ABORT_PREFIX) :]
        model, _, tail = rest.partition(": ")
        parts = tail.split(" ", 3)
        if len(parts) >= 3 and parts[1] == "of" and parts[0].isdigit() and parts[2].isdigit():
            out[model] = (int(parts[0]), int(parts[2]))
    return out


def _abort_text(n: int, total: int) -> str:
    share = f" ({100.0 * n / total:.1f}%)" if total else ""
    return f"{n:,} of {total:,} shadow pairs{share} aborted at the hbt #312 event (excluded)"


def bias_sentence(entry: Any, horizon_ns: int | None, aborts: dict[str, tuple[int, int]] | None = None) -> str:
    """One plain-English sentence on the model's fill-rate bias at the horizon (module docstring)."""
    m = str(_field(entry, "model_id"))
    ab = (aborts or {}).get(m)
    if ab is not None and ab[0] > 0:
        return _bias_sentence(entry, horizon_ns, m)[:-1] + "; " + _abort_text(*ab) + "."
    return _bias_sentence(entry, horizon_ns, m)


def _bias_sentence(entry: Any, horizon_ns: int | None, m: str) -> str:
    if horizon_ns is None:
        return f"{m}: no horizon with an exact label."
    when = duration_text(int(horizon_ns))
    v, lo, hi = bias_at(entry, horizon_ns)
    if v is None:
        return f"At {when}, {m} has no fill-rate bias: no shadow has an exact label at that horizon."
    ci = f"95% CI {_pp(lo)} to {_pp(hi)} pp" if lo is not None and hi is not None else "no CI"
    noise = lo is not None and hi is not None and lo <= 0.0 <= hi
    rate, exact = rates_at(entry, horizon_ns)
    more = "more" if v > 0 else "less"
    if abs(v) < NEGLIGIBLE_BIAS:
        if rate is not None and exact is not None:
            what = f"fills {pct(rate)}% of orders, as often as the exact replay ({pct(exact)}%, bias {_pp(v)} pp)"
        else:
            what = f"fills as often as the exact replay (bias {_pp(v)} pp)"
    elif rate is not None and exact is not None:
        what = (
            f"fills {pct(rate)}% of orders vs {pct(exact)}% in the exact replay, {_pp(v, signed=False)} pp {more} often"
        )
    else:
        what = f"fills {_pp(v, signed=False)} pp {more} often than the exact replay"
    return f"At {when}, {m} {what}{', within noise' if noise and abs(v) >= NEGLIGIBLE_BIAS else ''} ({ci})."


__all__ = [
    "ABORT_PREFIX",
    "DEFAULT_SORT_HORIZON_NS",
    "abort_shares",
    "bias_at",
    "bias_sentence",
    "brier_at",
    "duration_text",
    "horizons_of",
    "pct",
    "rank_leaderboard",
    "rates_at",
    "sort_horizon",
]
