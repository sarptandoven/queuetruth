"""Fail-safe assessment of a `qt.calibration_result` before its parameter is exported (trader review D1, D2, D4, E1).

The rule has one implementation, `qt_calib::trust` (`assess`, `annotate`), which this module calls through
`queuetruth._core`; what stays here reads the structured field, formats for display and raises the refusal.

A calibration is exportable only when all of these hold:

- it has a finite estimate fitted on at least `MIN_EXPORT_EVENTS` events (`no_estimate`, `few_events`);
- no refusing `trust:` flag is present: `at_grid_edge`, `flat_likelihood`, `not_identified`, `degenerate_ci`
  (written by `qt_calib` for `calibrate` and by `calibrate-live`; see `crates/calib/src/trust.rs` for the exact
  definitions, mirrored by `grid_lines` below), `own_orders_in_depth` (`calibrate-live` on an L2-only tape whose own
  resting orders could not be removed from the depth, trader review E2);
- it beats the family's DEFAULT parameters out of sample (`not_beating_default`, `no_held_out`): at every held-out
  horizon where both models are scored, the fitted model is not worse than the default on either proper score (log
  loss and Brier, tolerance `1e-9` relative), and it is strictly better on at least one score at one horizon or
  more. The defaults are the libraries' own: hftbacktest power families `n = 2`, Databento `k = -0.8`, NautilusTrader
  `prob_fill_on_limit = 1` (`held_out` holds the fitted model, `baseline_held_out` the default).

Latency calibrations (`kind = latency`) carry no queue parameter: they need entry-latency samples only.

The result is recorded in the structured `qt.calibration_result.trust` field (schema 1.1: `verdict`, `reasons` in
`REFUSING` order, `flags` = every diagnostic token, `beats_default` per horizon), which the export reads first, and,
for back-compatibility, as `warnings` lines: `trust: beats_default: <horizon_ns>=<better|tie|worse|mixed|n/a>,...
(...)` and `trust: verdict: exportable` or `trust: verdict: refused (<flag>, ...)`. `queuetruth export
hftbacktest|nautilus` raises `ExportRefused` (`QT-CALIB-001`, exit code 1) for a refused calibration unless forced
(`--force`, `ExportSpec.force`); a forced snippet carries a loud header.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from queuetruth.errors import QtError
from queuetruth.schemas.models import CalibrationResult, CalibrationTrust

#: Fewest events (allocation events, touches, filled train orders) of an exportable fit.
MIN_EXPORT_EVENTS = 30
#: Relative tolerance of a tie between two scores.
TIE_REL_TOL = 1e-9
#: chi-square(1) 95% quantile (profile region).
CHI2_95 = 3.841459
#: Tie tolerance of two log likelihoods, relative to the maximum (as `qt_calib::trust::FLAT_REL_TOL`).
FLAT_REL_TOL = 1e-9
#: Half-width share of the grid width that counts as a flat likelihood (as `qt_calib::trust::FLAT_FRACTION`).
FLAT_FRACTION = 0.25
PREFIX = "trust: "
#: Flags that make a calibration non-exportable.
REFUSING = (
    "no_estimate",
    "few_events",
    "at_grid_edge",
    "flat_likelihood",
    "not_identified",
    "degenerate_ci",
    "own_orders_in_depth",
    "no_held_out",
    "not_beating_default",
)
_LINE = re.compile(r"^trust: ([a-z_]+): (.*)$", re.S)
#: Lines this module writes (replaced, not duplicated, by `annotate`).
OWN_TOKENS = ("beats_default", "verdict")


class ExportRefused(QtError):
    """`QT-CALIB-001`: the calibration is degenerate, at a grid edge or does not beat the default out of sample.
    Exit code 1 (the export completed its check and the check failed), like a failed gate."""

    def __init__(self, message: str, hint: str | None = None, details: dict | None = None) -> None:
        super().__init__("QT-CALIB-001", message, hint, details)

    @property
    def exit_code(self) -> int:
        return 1


@dataclass(frozen=True)
class HorizonVerdict:
    """Held-out comparison of the fitted and the default model at one horizon."""

    horizon_ns: int
    fitted_logloss: float | None
    default_logloss: float | None
    fitted_brier: float | None
    default_brier: float | None
    #: `better` | `worse` | `tie` | `mixed` (better on one score, worse on the other) | `n/a` (not scored).
    label: str

    @property
    def beats(self) -> bool | None:
        """True when better, None when not scored, else False."""
        return None if self.label == "n/a" else self.label == "better"


@dataclass(frozen=True)
class Trust:
    """Assessment of one calibration."""

    #: `trust:` flags found or computed: token -> text.
    flags: dict[str, str]
    #: Held-out comparison per horizon (ascending).
    horizons: list[HorizonVerdict]
    fitted_id: str | None
    default_id: str | None
    #: Refusal reasons as (token, text), in `REFUSING` order.
    reasons: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the calibration may be exported."""
        return not self.reasons

    @property
    def beats_default(self) -> dict[str, bool | None]:
        """`{horizon_ns: beats}` (None = not scored)."""
        return {str(h.horizon_ns): h.beats for h in self.horizons}


# ---- formatting -------------------------------------------------------------------------------------------------


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


# ---- held-out comparison ----------------------------------------------------------------------------------------


def horizon_label(ll: int | None, br: int | None) -> str:
    """Label from the log-loss and Brier comparisons (-1 fitted better, 0 tie, 1 worse, None not comparable), as
    `qt_calib::trust::horizon_label`."""
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


def _rust_assess(cal: CalibrationResult) -> dict:
    """`qt_calib::trust::assess`: the export rule, implemented once in Rust."""
    from queuetruth import _core

    return json.loads(_core.trust_assess(cal.model_dump_json()))


def _horizons(d: dict) -> list[HorizonVerdict]:
    return [
        HorizonVerdict(
            h["horizon_ns"],
            h["fitted_logloss"],
            h["default_logloss"],
            h["fitted_brier"],
            h["default_brier"],
            h["label"],
        )
        for h in d["horizons"]
    ]


def held_out_verdicts(cal: CalibrationResult) -> tuple[str | None, str | None, list[HorizonVerdict]]:
    """(fitted model id, default model id, per-horizon verdicts) from `held_out` / `baseline_held_out`."""
    d = _rust_assess(cal)
    return d["fitted_id"], d["default_id"], _horizons(d)


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


# ---- assessment ---------------------------------------------------------------------------------------------------


def parse_flags(warnings: Sequence[str]) -> dict[str, str]:
    """`trust:` lines of a warnings list: token -> text (the last line of a token wins)."""
    out: dict[str, str] = {}
    for w in warnings:
        m = _LINE.match(w)
        if m:
            out[m.group(1)] = m.group(2)
    return out


#: Generic texts of tokens known only from the structured field (no `trust:` line to quote).
_TOKEN_TEXT = {
    "no_estimate": "the fit has no finite estimate",
    "few_events": f"fitted on fewer than {MIN_EXPORT_EVENTS} events",
    "at_grid_edge": "the maximum is at an end of the fitting grid",
    "flat_likelihood": "the likelihood is flat: the parameter is not determined by the data",
    "not_identified": "the likelihood is the same at every grid value",
    "degenerate_ci": "the bootstrap interval has zero width",
    "own_orders_in_depth": "the user's own resting orders are still in the L2 depth the fit ran on",
    "no_held_out": "no held-out comparison with the default",
    "not_beating_default": "the fit does not beat the default out of sample",
}


def _compute(cal: CalibrationResult, fmt: Callable[[int], str] | None = None) -> Trust:
    """Assessment from the `trust:` lines and the numbers (ignores the structured `trust` field), by
    `qt_calib::trust::assess`. `fmt` rewrites the horizons of the `not_beating_default` text for display."""
    d = _rust_assess(cal)
    hs = _horizons(d)
    flags = {k: v for k, v in d["flags"]}
    reasons = [(k, v) for k, v in d["reasons"]]
    if fmt is not None:
        shown = Trust(flags, hs, d["fitted_id"], d["default_id"])
        reasons = [(k, held_out_summary(shown, fmt) if k == "not_beating_default" else v) for k, v in reasons]
    return Trust(flags, hs, d["fitted_id"], d["default_id"], reasons)


def assess(cal: CalibrationResult, fmt: Callable[[int], str] | None = None) -> Trust:
    """Exportability of a calibration (see the module doc). The structured `trust` field (schema 1.1) decides when
    present: its `verdict` / `reasons` and `beats_default` labels win, with texts from the `trust:` lines or the
    recomputed reasons; without it the assessment is computed from the lines and the numbers. `fmt` writes the
    horizons in the reason texts (default `dur`, the form stored in the calibration's warnings; displays pass
    `queuetruth.units.format_duration`)."""
    t = _compute(cal, fmt)
    st = cal.trust
    if st is None:
        return t
    computed = dict(t.reasons)
    flags = dict(t.flags)
    for tok in st.flags:
        flags.setdefault(tok, _TOKEN_TEXT.get(tok, "see the calibration warnings"))
    reasons = [(k, computed.get(k) or flags.get(k) or _TOKEN_TEXT.get(k, k)) for k in st.reasons]
    if st.verdict == "refused" and not reasons:
        reasons = [("not_beating_default", "the calibration's trust verdict is refused")]
    if st.verdict == "exportable":
        reasons = []
    hs = t.horizons
    if st.beats_default:
        by = {h.horizon_ns: h for h in hs}
        hs = [
            HorizonVerdict(int(k), *(_metrics(by.get(int(k)))), label)
            for k, label in sorted(st.beats_default.items(), key=lambda kv: int(kv[0]))
        ]
    return Trust(flags, hs, t.fitted_id, t.default_id, reasons)


def _metrics(h: HorizonVerdict | None) -> tuple[float | None, float | None, float | None, float | None]:
    if h is None:
        return None, None, None, None
    return h.fitted_logloss, h.default_logloss, h.fitted_brier, h.default_brier


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
    `beats_default` and `verdict` lines (replacing older ones), by `qt_calib::trust::annotate`. The assessment is
    recomputed from the lines and the numbers: an earlier structured field (e.g. the Rust part's, which has no
    held-out comparison) is replaced."""
    from queuetruth import _core

    d = json.loads(_core.trust_annotate(cal.model_dump_json()))
    return cal.model_copy(update={"warnings": d["warnings"], "trust": CalibrationTrust.model_validate(d["trust"])})


def refusal(cal: CalibrationResult, target: str) -> ExportRefused | None:
    """The `ExportRefused` error of a non-exportable calibration (None when exportable)."""
    t = assess(cal)
    if t.ok:
        return None
    reasons = "; ".join(f"{k}: {v}" for k, v in t.reasons)
    return ExportRefused(
        f"refusing to export calibration {cal.run_id} ({cal.family}) to {target}: {reasons}",
        "the parameter is not safe to paste; fit on more data or a wider grid, or pass --force to write the snippet "
        "with a warning header",
        {"run_id": cal.run_id, "reasons": [k for k, _ in t.reasons], "beats_default": t.beats_default},
    )


def header_lines(cal: CalibrationResult, param_text: str) -> list[str]:
    """Comment lines stating the fit, its events, the held-out result and (when refused) the reasons."""
    t = assess(cal)
    lines = []
    if cal.kind != "latency":
        ci_b = cal.ci_bootstrap or (None, None)
        ci_p = cal.ci_profile or (None, None)
        lines.append(
            f"# fit: {param_text} ({cal.kind}); {cal.n_events} events from {cal.n_shadows} shadows; "
            f"bootstrap CI [{g6(ci_b[0])}, {g6(ci_b[1])}]; profile CI [{g6(ci_p[0])}, {g6(ci_p[1])}]"
        )
        lines.append(f"# held-out: {held_out_summary(t)}")
        for tok in ("plateau", "profile_ci_open", "heterogeneous_strata"):
            if tok in t.flags:
                lines.append(f"# note ({tok}): {t.flags[tok]}")
    if not t.ok:
        lines.append(
            "# !! NOT VALIDATED: `queuetruth export` refuses this calibration (`--force` writes it anyway). Reasons:"
        )
        lines += [f"# !!   - {k}: {v}" for k, v in t.reasons]
        lines.append("# !! Do not paste these values into a production config without your own out-of-sample check.")
    return lines


# ---- grid diagnostics (Python port of qt_calib::trust, used by calibrate-live) -------------------------------------


def grid_lines(thetas: Sequence[float], ll: Sequence[float], param: str, to_nat: Callable[[float], float]) -> list[str]:
    """`trust:` lines of a grid likelihood (same definitions and texts as `qt_calib::trust::grid_lines`)."""
    n = len(thetas)
    vals = [x if math.isfinite(x) else -math.inf for x in ll]
    if n == 0 or all(v == -math.inf for v in vals):
        return []
    i = max(range(n), key=lambda j: (vals[j], -j))
    g = vals[i]
    tol = FLAT_REL_TOL * max(abs(g), 1.0)
    a = b = i
    while a > 0 and g - vals[a - 1] <= tol:
        a -= 1
    while b + 1 < n and g - vals[b + 1] <= tol:
        b += 1
    width = thetas[-1] - thetas[0] if n > 1 else 0.0
    finite = [v for v in vals if v > -math.inf]
    constant = n > 1 and g - min(finite) <= tol
    edge = None if n < 2 else ("lower" if a == 0 else "upper" if b == n - 1 else None)
    centre = 0.5 * (thetas[a] + thetas[b])
    parts = []
    for s in (a - 1, b + 1):
        if 0 <= s < n:
            d = abs(thetas[s] - centre)
            if d > 0:
                parts.append(2.0 * (g - vals[s]) / (d * d) if vals[s] > -math.inf else math.inf)
    curv = sum(parts) / len(parts) if parts else 0.0
    half = math.sqrt(CHI2_95 / curv) if curv > 0 else math.inf
    pa = pb = i
    while pa > 0 and 2.0 * (g - vals[pa - 1]) <= CHI2_95:
        pa -= 1
    while pb + 1 < n and 2.0 * (g - vals[pb + 1]) <= CHI2_95:
        pb += 1
    spans = n > 1 and pa == 0 and pb == n - 1
    flat = spans or (half >= FLAT_FRACTION * width and width > 0)
    lo, hi = to_nat(thetas[0]), to_nat(thetas[-1])
    out = []
    if constant:
        out.append(
            f"{PREFIX}not_identified: the likelihood is the same at every grid value ({param} in [{g6(lo)}, "
            f"{g6(hi)}]); the data carry no information about {param}"
        )
    if edge is not None:
        at = lo if edge == "lower" else hi
        out.append(
            f"{PREFIX}at_grid_edge: the maximum is at the {edge} end of the grid ({param} = {g6(at)}; grid {param} "
            f"in [{g6(lo)}, {g6(hi)}]); the likelihood may keep rising outside it"
        )
    if b > a and not constant:
        out.append(
            f"{PREFIX}plateau: the likelihood ties over {b - a + 1} grid points, {param} in "
            f"[{g6(to_nat(thetas[a]))}, {g6(to_nat(thetas[b]))}]; any value there fits equally well"
        )
    if flat and not constant:
        why = (
            "the 95% profile region holds every grid value"
            if spans
            else f"the curvature at the maximum implies a 95% half-width of {g6(half)} grid units, at least "
            f"{FLAT_FRACTION} of the grid width {g6(width)}"
        )
        out.append(f"{PREFIX}flat_likelihood: {why}; {param} is not determined by the data")
    elif (pa == 0) != (pb == n - 1):
        side = "lower" if pa == 0 else "upper"
        out.append(
            f"{PREFIX}profile_ci_open: the 95% profile region reaches the {side} grid end; that side of the interval "
            "is unbounded on this grid"
        )
    return out


__all__ = [
    "MIN_EXPORT_EVENTS",
    "REFUSING",
    "ExportRefused",
    "HorizonVerdict",
    "Trust",
    "annotate",
    "assess",
    "dur",
    "grid_lines",
    "header_lines",
    "held_out_summary",
    "held_out_verdicts",
    "parse_flags",
    "refusal",
    "structured",
    "trust_lines",
]
