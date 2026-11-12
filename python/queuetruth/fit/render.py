"""Terminal view of a `qt.fit` document: the answer first (parameter, interval, gain over the default on held-out
data, verdict), then the held-out table, why a refused fit is refused and what would make it pass, and the
snippet or the one line to paste."""

from __future__ import annotations

import math
from typing import Any

from queuetruth import units

#: Short reason of each refusal code, for the answer line.
SHORT = {
    "no_estimate": "the data gave no estimate",
    "few_events": "it was fitted on too few events",
    "at_grid_edge": "it sits at the end of the range the fit searches",
    "flat_likelihood": "the data do not determine it",
    "not_identified": "the data carry no information about it",
    "degenerate_ci": "there is too little data for an interval",
    "own_orders_in_depth": "your own orders were still in the depth it was fitted on",
    "no_held_out": "no held-out order was scored",
}
#: The horizon the answer prefers (the atlas headline horizon).
HEADLINE_NS = 60_000_000_000


def num(x: Any) -> str:
    """Four significant digits, `n/a` for a missing value."""
    if x is None or not isinstance(x, int | float) or not math.isfinite(x):
        return "n/a"
    return f"{x:.4g}"


def pp(x: Any) -> str:
    return "n/a" if x is None else f"{x:+.1f} pp"


def _ci(doc: dict[str, Any]) -> str:
    ci = doc["param"]["ci"]
    if not ci:
        return "no interval"
    lo, hi = ci
    word = "95% CI" if doc["param"]["ci_kind"] == "bootstrap" else "95% profile CI"
    return f"{word} {num(lo) if lo is not None else 'open'} to {num(hi) if hi is not None else 'open'}"


def _h(r: dict[str, Any]) -> str:
    """A held-out row's horizon for people (`1min`; `queuetruth.units`), from its nanoseconds."""
    return units.format_duration(int(r["horizon_ns"])) if r.get("horizon_ns") is not None else str(r["horizon"])


def headline(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The held-out row the answer quotes: among the horizons where the fit wins (all scored horizons when it wins
    nowhere), 1min when there, else the one where the default is most off."""
    scored = [r for r in rows if r["fitted_bias_pp"] is not None and r["default_bias_pp"] is not None]
    pool = [r for r in scored if r["result"] == "better"] or scored
    if not pool:
        return None
    at = [r for r in pool if r["horizon_ns"] == HEADLINE_NS]
    return at[0] if at else max(pool, key=lambda r: abs(r["default_bias_pp"]))


def gain(r: dict[str, Any]) -> str:
    """`at 1min its fill-rate error is +2.8 pp against +9.2 pp for the default, log loss 89% lower`."""
    text = (
        f"at {_h(r)} the fill rate is off by {pp(r['fitted_bias_pp'])} with the fit against "
        f"{pp(r['default_bias_pp'])} with the default"
    )
    f, d = r["fitted_logloss"], r["default_logloss"]
    if f is not None and d is not None and d > 0:
        rel = (d - f) / d * 100
        text += f", log loss {abs(rel):.0f}% {'lower' if rel >= 0 else 'higher'}"
    return text


def answer(doc: dict[str, Any]) -> str:
    """The answer in plain words (the first line of the report)."""
    p = doc["param"]
    sp = doc["split"]
    held = f"held-out data ({sp['test']}, {sp['n_test']} {sp['orders']})"
    default = f"the default {p['name']} = {num(p['default'])}"
    value = f"{p['name']} = {num(p['value'])} ({_ci(doc)})"
    r = headline(doc["held_out"])
    if doc["verdict"]["passed"]:
        text = f"Use {value} for {p['setting']}: on {held} it beats {default}"
        text += f"; {gain(r)}." if r is not None else "."
        return text + f" Snippet: {doc['files']['snippet']}"
    codes = [x["code"] for x in doc["verdict"]["reasons"]]
    if "no_estimate" in codes or p["value"] is None:
        return f"No fit for {p['setting']}: {SHORT['no_estimate']}. Keep {default}; no snippet written."
    first = next((c for c in codes if c in SHORT), None)
    if first is not None:
        why = SHORT[first]
    else:
        why = f"on {held} it does not beat {default}" + _losses(doc["held_out"])
        default = "the default"
    return f"Do not use {value} for {p['setting']} yet: {why}. Keep {default}; no snippet written."


def _losses(rows: list[dict[str, Any]]) -> str:
    """` (worse at 1min; mixed at 10s: better on one score, worse on the other)`, or empty."""
    parts = []
    for label, text in (("worse", "worse at {}"), ("mixed", "mixed at {}: better on one score, worse on the other")):
        hs = [_h(r) for r in rows if r["result"] == label]
        if hs:
            parts.append(text.format(", ".join(hs)))
    return f" ({'; '.join(parts)})" if parts else ""


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths, strict=True)))
        for r in [header, *rows]
    ]


def text(doc: dict[str, Any]) -> str:
    """The terminal report."""
    p, sp, v = doc["param"], doc["split"], doc["verdict"]
    lines = [doc["answer"], ""]
    lines.append(
        f"fit: {p['name']} = {num(p['value'])} ({_ci(doc)}) for {p['setting']}, from {p['n_events']} events; "
        f"family {doc['family']}, {doc['command']}"
    )
    lines.append(
        f"split: {sp['how']}: train {sp['train']} ({sp['n_train']} {sp['orders']}), held out {sp['test']} "
        f"({sp['n_test']} {sp['orders']})"
    )
    rows = doc["held_out"]
    if rows:
        lines.append(f"held out: the fit against the default {p['name']} = {num(p['default'])}")
        names = {"better": "yes", "worse": "NO", "mixed": "mixed", "tie": "tie"}
        lines += _table(
            ["horizon", "fill-rate error fit", "default", "log loss fit", "default", "better"],
            [
                [
                    _h(r),
                    pp(r["fitted_bias_pp"]),
                    pp(r["default_bias_pp"]),
                    num(r["fitted_logloss"]),
                    num(r["default_logloss"]),
                    names.get(r["result"], r["result"]),
                ]
                for r in rows
            ],
        )
        lines.append("(fill-rate error: the model's fill rate minus the exact or live one; lower log loss is better)")
    else:
        lines.append("held out: no comparison with the default")
    if v["passed"]:
        lines.append("verdict: PASS: the fit beats the library default on held-out data")
        sn = doc["snippet"]
        lines.append(f"paste: {sn['path']}")
        if sn.get("one_liner"):
            lines.append(f"  {sn['one_liner']}")
        for n in sn.get("notes") or []:
            lines.append(f"note: {n}")
    else:
        lines.append("verdict: DO NOT USE: " + ", ".join(r["code"] for r in v["reasons"]))
        lines.append("why:")
        lines += [f"  - {r['text']}" for r in v["reasons"]]
        lines.append("what would make it pass:")
        lines += [f"  - {m}" for m in v["remedies"]]
        lines.append("no snippet written")
    lines.append(f"files: {doc['files']['fit']}, {doc['files']['calibration']}")
    return "\n".join(lines)


__all__ = ["answer", "headline", "text"]
