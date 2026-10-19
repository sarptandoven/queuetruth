"""hftbacktest `BacktestAsset` builder chain (SPEC 5.10), checked against hftbacktest 2.4.4.

Builder per family: power -> `power_prob_queue_model(n)`, power2 -> `power_prob_queue_model2(n)`, power3 ->
`power_prob_queue_model3(n)`, databento with `-1 < k <= 0` -> `power_prob_queue_model3(1/(1+k))` (expected-value
approximation of the Monte Carlo estimator), log -> `log_prob_queue_model()`, log2 -> `log_prob_queue_model2()`,
risk_adverse -> `risk_adverse_queue_model()`. Families without an hbt equivalent (databento with `k > 0`, the NT
touch families, latency-only results) omit the builder line and say so in the notes. The exchange follows the fit's
`exchange` (`partial` default); `.intp_order_latency("<npz>")` is added when the result carries a latency npz.

Header (trader review D1, D2): after the SPEC 5.10 run and data lines, the fit (estimate, events, CIs) and the
held-out comparison with the default (`queuetruth.export.trust`) are stated; a calibration the export refuses also
carries a `# !! NOT VALIDATED` block with the reasons (the snippet in `calibration.json` and a `--force` export).
"""

from __future__ import annotations

import math

from queuetruth.export import trust
from queuetruth.export._text import (
    VERSION,
    data_sources_text,
    family_key,
    g6,
    power_n,
    spec_value,
    window_text,
)
from queuetruth.schemas.models import CalibrationResult

POWER_BUILDERS = {
    "power": "power_prob_queue_model",
    "power2": "power_prob_queue_model2",
    "power3": "power_prob_queue_model3",
}
FIXED_BUILDERS = {
    "log": "log_prob_queue_model",
    "log2": "log_prob_queue_model2",
    "risk_adverse": "risk_adverse_queue_model",
}
EXCHANGES = {"partial": "partial_fill_exchange", "no_partial": "no_partial_fill_exchange"}

NOTE_316 = (
    "hftbacktest #316/#312: released versions apply partial fills of resting orders to position only when the order "
    "completes, and a fill exactly equal to leaves can raise InvalidOrderStatus; check partial-fill accounting in "
    "your version before comparing fill quantities."
)
NOTE_L2 = (
    "Queue models run on conflated L2 depth: the fitted parameter is specific to the feed, depth and conflation it "
    "was fitted on; refit when the data source changes."
)


def l2_note(cal: CalibrationResult) -> str:
    """The conflated-L2 caveat, stating the L2 view the fit actually used (trader review D5)."""
    l2 = cal.spec.get("l2") if isinstance(cal.spec, dict) else None
    if not isinstance(l2, dict):
        return NOTE_L2
    cadence = int(l2.get("cadence_ns") or 0)
    depth = int(l2.get("depth_levels") or 0)
    view = f"{'all levels' if depth == 0 else f'top {depth} levels'}, " + (
        "unconflated (every book event)" if cadence == 0 else f"conflated every {cadence} ns"
    )
    return (
        f"The fit used this L2 view of its data source: {view}. The parameter is specific to that feed, depth and "
        "conflation: if your backtest runs on conflated L2 or another depth, refit on that view."
    )


def builder_call(cal: CalibrationResult) -> tuple[str | None, list[str]]:
    """(`name(args)` or None, notes) for the result's family."""
    fam = family_key(cal.family)
    notes: list[str] = []
    if fam in POWER_BUILDERS:
        n = power_n(cal)
        if n is None:
            return None, [f"no finite estimate for {cal.family}: queue-model line omitted"]
        return f"{POWER_BUILDERS[fam]}({g6(n)})", notes
    if fam == "databento":
        k = cal.estimate
        if k is None or not math.isfinite(k):
            return None, ["no finite estimate for databento k: queue-model line omitted"]
        if k <= -1.0:
            return None, [f"databento k = {g6(k)} <= -1 has no power3 equivalent: queue-model line omitted"]
        if k > 0.0:
            return None, [f"databento k = {g6(k)} > 0 has no hftbacktest equivalent: queue-model line omitted"]
        notes.append(
            f"power_prob_queue_model3(1/(1+k)) with k = {g6(k)} is the expected-value approximation of the "
            "Databento Monte Carlo estimator, not the estimator itself."
        )
        return f"power_prob_queue_model3({g6(1.0 / (1.0 + k))})", notes
    if fam in FIXED_BUILDERS:
        return f"{FIXED_BUILDERS[fam]}()", notes
    return None, [f"family {cal.family} has no hftbacktest queue model: queue-model line omitted"]


def hftbacktest_notes(cal: CalibrationResult) -> list[str]:
    """Notes accompanying `hftbacktest_snippet` (always includes #316/#312 and the conflated-L2 caveat)."""
    _, notes = builder_call(cal)
    if cal.exports.latency_npz is None:
        notes.append("no latency calibration: keep your existing latency model (intp_order_latency line omitted).")
    elif not cal.exports.latency_npz.startswith("/"):
        notes.append(
            f"the latency file path {cal.exports.latency_npz!r} is relative to the calibration's output directory: "
            "point intp_order_latency at it from where your backtest runs."
        )
    t = trust.assess(cal)
    if not t.ok:
        notes.append(
            "NOT VALIDATED: `queuetruth export` refuses this calibration ("
            + ", ".join(k for k, _ in t.reasons)
            + "); see the snippet header."
        )
    return [*notes, NOTE_316, l2_note(cal)]


def param_text(cal: CalibrationResult) -> str:
    """`n = 2.34568` (natural units) or `k = -0.8`, `p = 0.2`; `no estimate` when there is none."""
    fam = family_key(cal.family)
    if fam in POWER_BUILDERS:
        n = power_n(cal)
        return f"n = {g6(n)}" if n is not None else "n = no estimate"
    if cal.estimate is None or not math.isfinite(cal.estimate):
        return f"{cal.param_name or 'parameter'} = no estimate"
    return f"{cal.param_name or 'parameter'} = {g6(cal.estimate)}"


def hftbacktest_snippet(cal: CalibrationResult) -> str:
    """Paste-ready hftbacktest `BacktestAsset` chain (SPEC 5.10 layout plus the fit / held-out header lines)."""
    call, _ = builder_call(cal)
    exchange = EXCHANGES.get(spec_value(cal, "exchange", "partial"), "partial_fill_exchange")
    lines = [
        f"# queuetruth {VERSION} | calibration {cal.run_id} | {cal.family} fitted on {window_text(cal.train)}; "
        f"held-out {window_text(cal.test)}",
        f"# data: {data_sources_text(cal)}",
        *trust.header_lines(cal, param_text(cal)),
        "# Paste into your existing BacktestAsset chain (keep your data/tick/lot/fee settings).",
        "from hftbacktest import BacktestAsset",
        "asset = (",
        "    BacktestAsset()",
        "    # ... your existing .data(...), .tick_size(...), .lot_size(...), fee model ...",
    ]
    if call is not None:
        lines.append(f"    .{call}")
    lines.append(f"    .{exchange}()")
    if cal.exports.latency_npz is not None:
        lines.append(f"    .intp_order_latency({_py_str(cal.exports.latency_npz)})")
    lines.append(")")
    return "\n".join(lines) + "\n"


def _py_str(s: str) -> str:
    """A double-quoted Python string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
