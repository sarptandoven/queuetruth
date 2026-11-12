"""The trust rule as the Python code had it before it moved to `qt_calib::trust` (copied verbatim from
`queuetruth/export/trust.py` at the commit before the move). Reference only: `test_trust_core_parity.py` checks that
the Rust rule gives the same bytes on many synthetic calibrations. Do not change it."""

# ruff: noqa

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence

from queuetruth.export.trust import (
    MIN_EXPORT_EVENTS,
    OWN_TOKENS,
    PREFIX,
    REFUSING,
    TIE_REL_TOL,
    HorizonVerdict,
    Trust,
)
from queuetruth.schemas.models import CalibrationResult, CalibrationTrust, HeldOut

_LINE = re.compile(r"^trust: ([a-z_]+): (.*)$", re.S)


def g6(x: float | None) -> str:
    """`format(x, ".6g")`, `-` for None."""
    return "-" if x is None else format(float(x), ".6g")


def dur(ns: int) -> str:
    """`100ms`, `1s`, `90s`, `5m` (exact units only; else ns): the form of the `trust:` lines stored in a
    calibration's warnings, kept so those files keep their bytes. What people read uses
    `queuetruth.units.format_duration` (`1min`, `5min`), which parses every form this writes."""
    for unit, size in (("h", 3_600_000_000_000), ("m", 60_000_000_000), ("s", 1_000_000_000), ("ms", 1_000_000)):
        if ns >= size and ns % size == 0 and not (unit == "m" and ns < 300_000_000_000):
            return f"{ns // size}{unit}"
    for unit, size in (("us", 1_000),):
        if ns >= size and ns % size == 0:
            return f"{ns // size}{unit}"
    return f"{ns}ns"


def _tie(a: float, b: float) -> bool:
    return abs(a - b) <= TIE_REL_TOL * max(abs(a), abs(b), 1e-300)


def _cmp(f: float | None, d: float | None) -> int | None:
    """-1 fitted better (lower), 0 tie, 1 worse, None not comparable."""
    if f is None or d is None or not (math.isfinite(f) and math.isfinite(d)):
        return None
    if _tie(f, d):
        return 0
    return -1 if f < d else 1


def horizon_label(ll: int | None, br: int | None) -> str:
    """Label from the log-loss and Brier comparisons (`_cmp` codes)."""
    codes = [c for c in (ll, br) if c is not None]
    if not codes:
        return "n/a"
    if 1 in codes and -1 in codes:
        return "mixed"
    if 1 in codes:
        return "worse"
    if -1 in codes:
        return "better"
    return "tie"


def _first(d: dict[str, HeldOut]) -> tuple[str | None, HeldOut | None]:
    for k, v in d.items():
        return k, v
    return None, None


def held_out_verdicts(cal: CalibrationResult) -> tuple[str | None, str | None, list[HorizonVerdict]]:
    """(fitted model id, default model id, per-horizon verdicts) from `held_out` / `baseline_held_out`."""
    fid, fho = _first(cal.held_out)
    did, dho = _first(cal.baseline_held_out)
    if fho is None or dho is None:
        return fid, did, []
    keys = sorted({int(k) for k in (*fho.logloss, *fho.brier, *dho.logloss, *dho.brier)})
    out = []
    for h in keys:
        k = str(h)
        fl, dl = fho.logloss.get(k), dho.logloss.get(k)
        fb, db = fho.brier.get(k), dho.brier.get(k)
        out.append(HorizonVerdict(h, fl, dl, fb, db, horizon_label(_cmp(fl, dl), _cmp(fb, db))))
    return fid, did, out


def held_out_summary(t: Trust, fmt: Callable[[int], str] | None = None) -> str:
    """One line: `beats the default <id> at 1s, 10s; tie at 100ms` / `WORSE than ...` / `not evaluated ...`. `fmt`
    writes the horizons (default `dur`: the form stored in the calibration's warnings)."""
    fmt = fmt or dur
    if not t.horizons or all(h.label == "n/a" for h in t.horizons):
        return "not evaluated (no held-out metrics for the fitted and the default model)"
    groups: dict[str, list[str]] = {}
    for h in t.horizons:
        groups.setdefault(h.label, []).append(fmt(h.horizon_ns))
    parts = []
    for label, word in (
        ("worse", "WORSE at"),
        ("mixed", "MIXED (better on one score, worse on the other) at"),
        ("better", "better at"),
        ("tie", "tie at"),
        ("n/a", "not scored at"),
    ):
        if label in groups:
            parts.append(f"{word} {', '.join(groups[label])}")
    bad = [h for h in t.horizons if h.label in ("worse", "mixed")]
    detail = ""
    if bad:
        h = bad[0]
        detail = (
            f" (log loss at {fmt(h.horizon_ns)}: fitted {g6(h.fitted_logloss)} vs default {g6(h.default_logloss)}; "
            f"Brier {g6(h.fitted_brier)} vs {g6(h.default_brier)})"
        )
    verdict = "beats" if _beats(t.horizons) else "DOES NOT BEAT"
    return f"{verdict} the default {t.default_id or '?'} out of sample: {'; '.join(parts)}{detail}"


def _beats(hs: Sequence[HorizonVerdict]) -> bool:
    labels = [h.label for h in hs if h.label != "n/a"]
    return bool(labels) and "better" in labels and not any(x in ("worse", "mixed") for x in labels)


def parse_flags(warnings: Sequence[str]) -> dict[str, str]:
    """`trust:` lines of a warnings list: token -> text (the last line of a token wins)."""
    out: dict[str, str] = {}
    for w in warnings:
        m = _LINE.match(w)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _legacy_flags(cal: CalibrationResult) -> dict[str, str]:
    """Flags derivable from the numbers of a result written before `trust:` lines existed."""
    out: dict[str, str] = {}
    ci = cal.ci_bootstrap
    if ci is not None and ci[0] is not None and ci[0] == ci[1]:
        out["degenerate_ci"] = f"bootstrap CI [{g6(ci[0])}, {g6(ci[1])}] has zero width"
    grid = cal.spec.get("grid") if isinstance(cal.spec, dict) else None
    fam = (cal.family or "").lower().removeprefix("hbt_")
    if cal.estimate is not None and isinstance(grid, dict) and cal.kind == "alloc":
        g = grid.get("databento" if fam == "databento" else "alloc")
        if isinstance(g, dict) and {"lo", "hi"} <= set(g):
            lo, hi = float(g["lo"]), float(g["hi"])
            theta = cal.estimate if fam == "databento" else math.log(max(cal.estimate, 1e-300))
            if any(abs(theta - e) <= 1e-9 * max(1.0, abs(e)) for e in (lo, hi)):
                out["at_grid_edge"] = f"estimate {g6(cal.estimate)} is the end of the fitting grid"
    return out


def _compute(cal: CalibrationResult, fmt: Callable[[int], str] | None = None) -> Trust:
    """Assessment from the `trust:` lines and the numbers (ignores the structured `trust` field)."""
    flags = parse_flags(cal.warnings)
    if not any(k not in OWN_TOKENS for k in flags):
        flags.update({k: v for k, v in _legacy_flags(cal).items() if k not in flags})
    fid, did, hs = held_out_verdicts(cal)
    reasons: dict[str, str] = {}
    if cal.kind == "latency":
        lat = cal.latency
        if lat is None or not lat.entry_ns.n:
            reasons["no_estimate"] = "no entry-latency samples (no ack with an exchange match/gateway timestamp)"
        return Trust(flags, [], None, None, [(k, reasons[k]) for k in REFUSING if k in reasons])
    if cal.estimate is None or not math.isfinite(cal.estimate):
        reasons["no_estimate"] = "the fit has no finite estimate"
    if cal.n_events < MIN_EXPORT_EVENTS:
        reasons["few_events"] = f"fitted on {cal.n_events} event(s), fewer than {MIN_EXPORT_EVENTS}"
    for tok in ("at_grid_edge", "flat_likelihood", "not_identified", "degenerate_ci", "own_orders_in_depth"):
        if tok in flags:
            reasons[tok] = flags[tok]
    t = Trust(flags, hs, fid, did)
    if not hs or all(h.label == "n/a" for h in hs):
        reasons["no_held_out"] = (
            "no held-out comparison with the default (empty test window, or a Rust-only fit.json): "
            "the fit is not shown to beat the default out of sample"
        )
    elif not _beats(hs):
        reasons["not_beating_default"] = held_out_summary(t, fmt)
    return Trust(flags, hs, fid, did, [(k, reasons[k]) for k in REFUSING if k in reasons])


def structured(t: Trust) -> CalibrationTrust:
    """The schema 1.1 `trust` field of an assessment."""
    return CalibrationTrust(
        verdict="exportable" if t.ok else "refused",
        reasons=[k for k, _ in t.reasons],
        flags=sorted(k for k in t.flags if k not in OWN_TOKENS),
        beats_default={str(h.horizon_ns): h.label for h in t.horizons},
    )


def trust_lines(t: Trust) -> list[str]:
    """`trust: beats_default: ...` (when held-out metrics exist) and `trust: verdict: ...` lines."""
    out = []
    if t.horizons:
        codes = ",".join(f"{h.horizon_ns}={h.label}" for h in t.horizons)
        out.append(f"{PREFIX}beats_default: {codes} ({held_out_summary(t)})")
    if t.ok:
        out.append(f"{PREFIX}verdict: exportable")
    else:
        out.append(f"{PREFIX}verdict: refused ({', '.join(k for k, _ in t.reasons)})")
    return out


def annotate(cal: CalibrationResult) -> CalibrationResult:
    """A copy of `cal` with the structured `trust` field (schema 1.1) and its `warnings` ending with the
    `beats_default` and `verdict` lines (replacing older ones). The assessment is recomputed from the lines and the
    numbers: an earlier structured field (e.g. the Rust part's, which has no held-out comparison) is replaced."""
    kept = [w for w in cal.warnings if not any(w.startswith(f"{PREFIX}{t}:") for t in OWN_TOKENS)]
    base = cal.model_copy(update={"warnings": kept, "trust": None})
    t = _compute(base)
    return base.model_copy(update={"warnings": kept + trust_lines(t), "trust": structured(t)})
