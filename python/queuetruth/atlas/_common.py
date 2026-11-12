"""Labels, units and wording shared by the atlas site (`scripts/scale/atlas_site.py`) and `queuetruth atlas`
(stdlib only).

Human units at the boundary: horizons as `1s`, `10s`, `1min`; fill rates in percent and biases in percentage points
(pp); P&L in ticks. A model id such as `nt_prob_touch[mode=faithful;p=1]` gets a label a trader reads
("NautilusTrader default (touch fill)"); the id stays next to it for programs and for the details.

The wording rules are the site's: a difference is stated as a finding only when its 95% interval excludes 0 on the
side of its estimate (`side`).
"""

from __future__ import annotations

import math
from typing import Any

from queuetruth import units

#: Library keys of the data API and the CLI -> the library's own name.
LIBRARY_NAMES = {
    "hftbacktest": "hftbacktest",
    "nautilus": "NautilusTrader",
    "databento": "Databento",
    "bound": "Bounds",
    "other": "Other",
}
#: The libraries a reader can pick (`queuetruth atlas --library`), in display order.
LIBRARIES = ("hftbacktest", "nautilus", "databento")
#: Model family -> library key.
FAMILY_LIBRARY = {
    "nt_prob_touch": "nautilus",
    "nt_queue_l2_prob_touch": "nautilus",
    "nt_queue_l2": "nautilus",
    "hbt_l3_fifo": "hftbacktest",
    "hbt_risk_adverse": "hftbacktest",
    "hbt_power3": "hftbacktest",
    "hbt_power2": "hftbacktest",
    "hbt_power": "hftbacktest",
    "hbt_log2": "hftbacktest",
    "hbt_log": "hftbacktest",
    "databento_mc": "databento",
    "front_of_queue": "bound",
    "trade_through_only": "bound",
}
#: Families that are not a setting a reader of an L2 backtest can pick: hftbacktest's L3 FIFO model needs
#: order-level input, the very data the exact replay uses.
NOT_A_SETTING = ("hbt_l3_fifo",)

#: Liquidity tiers of the atlas in plain words.
TIER_WORDS = {
    "ALL": "all symbols",
    "T1_top100": "the 100 most traded symbols",
    "T2_101_500": "the 101st to 500th most traded symbols",
    "T3_501_1500": "the 501st to 1,500th most traded symbols",
    "T4_1501_4000": "the 1,501st to 4,000th most traded symbols",
    "T5_rest": "the less traded symbols",
}


def parse_id(model_id: str) -> tuple[str, dict[str, str]]:
    """`hbt_power3[exch=partial;mode=faithful;n=2]` -> (`hbt_power3`, {exch: partial, mode: faithful, n: 2})."""
    fam, _, rest = model_id.partition("[")
    params: dict[str, str] = {}
    for kv in rest.rstrip("]").split(";") if rest else ():
        k, _, v = kv.partition("=")
        if k:
            params[k] = v
    return fam, params


def library_of(model_id: str) -> str:
    """The library key of a zoo model id (`other` for an unknown family)."""
    return FAMILY_LIBRARY.get(parse_id(model_id)[0], "other")


def _num(text: str | None) -> str | None:
    """A parameter as the library would print it: `1`, `0.5`, `-0.8`."""
    if text is None:
        return None
    try:
        x = float(text)
    except ValueError:
        return text
    return f"{x:g}" if math.isfinite(x) else text


def label(model_id: str) -> str:
    """The human label of a zoo model id, e.g. `NautilusTrader default (touch fill)`, `hftbacktest Power3 queue
    model (n=2)`; an unknown family keeps its id."""
    fam, p = parse_id(model_id)
    prob = _num(p.get("p", "1"))
    if fam == "nt_prob_touch":
        text = (
            "NautilusTrader default (touch fill)"
            if prob == "1"
            else f"NautilusTrader touch fill, prob_fill_on_limit={prob}"
        )
    elif fam == "nt_queue_l2":
        text = "NautilusTrader queue position (queue_position=True)"
        if prob != "1":
            text += f", prob_fill_on_limit={prob}"
    elif fam == "nt_queue_l2_prob_touch":
        text = f"NautilusTrader queue position, touch probability {prob}"
    elif fam in ("hbt_power", "hbt_power2", "hbt_power3"):
        word = {"hbt_power": "Power", "hbt_power2": "Power2", "hbt_power3": "Power3"}[fam]
        n = _num(p.get("n"))
        text = f"hftbacktest {word} queue model" + (f" (n={n})" if n is not None else "")
    elif fam in ("hbt_log", "hbt_log2"):
        text = f"hftbacktest {'Log' if fam == 'hbt_log' else 'Log2'} queue model"
    elif fam == "hbt_risk_adverse":
        text = "hftbacktest RiskAdverse queue model"
    elif fam == "hbt_l3_fifo":
        text = "hftbacktest L3 FIFO queue model (needs order-level data)"
    elif fam == "databento_mc":
        k = _num(p.get("k"))
        text = "Databento queue position estimate" + (f" (k={k})" if k is not None else "")
    elif fam == "front_of_queue":
        text = "Upper bound: always at the front of the queue"
    elif fam == "trade_through_only":
        text = "Lower bound: fills only when the price trades through"
    else:
        return model_id
    if fam.startswith("hbt_") and p.get("exch", "partial").replace("-", "_") == "no_partial":
        text += ", no partial fills"
    if p.get("mode", "faithful") != "faithful":
        text += f" ({p['mode']} mode)"
    return text


def is_default(model_id: str) -> bool:
    """The library's own default fill rule: NautilusTrader's touch fill model with `prob_fill_on_limit=1`."""
    fam, p = parse_id(model_id)
    return fam == "nt_prob_touch" and _num(p.get("p", "1")) == "1" and p.get("mode", "faithful") == "faithful"


def horizon_label(ns: int) -> str:
    """`100ms`, `1s`, `1min`: the key of a horizon in the published data API (`atlas/v1`). This form is frozen so the
    published files keep their bytes: the largest of `min`, `s` and `ms` that divides it. For the atlas horizons it is
    what `queuetruth.units.format_duration` writes, and `queuetruth.units.parse_duration` reads every key back."""
    n = int(ns)
    for unit, scale in (("min", 60_000_000_000), ("s", 1_000_000_000), ("ms", 1_000_000)):
        if n >= scale and n % scale == 0:
            return f"{n // scale}{unit}"
    return f"{n / 1_000_000:g}ms"


def horizon_words(text: str) -> str:
    """`1min` -> `1 minute`, `10s` -> `10 seconds`, `100ms` -> `100 milliseconds`."""
    return units.horizon_words(parse_horizon(text))


def parse_horizon(text: str) -> int:
    """`1min`, `1m`, `60s`, `60 s`, `100ms` (or nanoseconds) -> nanoseconds; ValueError when it is not a positive
    duration (`queuetruth.units.parse_duration`)."""
    try:
        ns = units.parse_duration(text, what="horizon")
    except units.DurationError:
        raise ValueError(f"not a horizon: {text!r} (use e.g. 1s, 10s, 1min)") from None
    if ns <= 0:
        raise ValueError(f"not a horizon: {text!r} (use e.g. 1s, 10s, 1min)")
    return ns


def pp(x: float | None) -> str:
    """A bias in percentage points with its sign: `+1.50`; `n/a` when missing."""
    return "n/a" if x is None or not math.isfinite(x) else f"{x:+.2f}"


def pct(x: float | None) -> str:
    return "n/a" if x is None or not math.isfinite(x) else f"{x:.2f}"


def side(iv: dict[str, Any] | None) -> int:
    """+1 when an interval `{value, lo, hi}` lies clearly above 0, -1 clearly below, else 0 (also when missing). A
    skewed interval (estimate and interval on opposite sides of 0) is 0, never a finding."""
    if not iv:
        return 0
    v, lo, hi = iv.get("value"), iv.get("lo"), iv.get("hi")
    if v is None or lo is None or hi is None:
        return 0
    if lo > 0 and v > 0:
        return 1
    if hi < 0 and v < 0:
        return -1
    return 0


def verdict_key(iv: dict[str, Any] | None) -> str:
    """`too_optimistic`, `too_pessimistic` or `no_clear_difference` (`side`)."""
    return {1: "too_optimistic", -1: "too_pessimistic"}.get(side(iv), "no_clear_difference")


VERDICT_TEXT = {
    "too_optimistic": "still clearly off: fills too many orders",
    "too_pessimistic": "still clearly off: fills too few orders",
    "no_clear_difference": "no clear difference from the real queue (95% interval includes 0)",
}


def interval_text(iv: dict[str, Any] | None, unit: str = "pp") -> str:
    """`+1.50 pp (95% interval +1.20 to +1.80)`."""
    if not iv or iv.get("value") is None:
        return "n/a"
    fmt = pp if unit == "pp" else (lambda x: "n/a" if x is None else f"{x:+.3f}")
    s = f"{fmt(iv['value'])} {unit}"
    if iv.get("lo") is not None and iv.get("hi") is not None:
        s += f" (95% interval {fmt(iv['lo'])} to {fmt(iv['hi'])})"
    return s


def _how_much(row: dict[str, Any]) -> str:
    """`fills 1.50 pp more orders than the real queue (13% more fills; 95% interval +1.20 to +1.80 pp)`, or the
    no-clear-difference wording."""
    iv = row["fill_rate_bias_pp"]
    v = iv["value"]
    ci = ""
    if iv.get("lo") is not None and iv.get("hi") is not None:
        ci = f"95% interval {pp(iv['lo'])} to {pp(iv['hi'])} pp"
    s = side(iv)
    if s == 0:
        return f"is not clearly different from the real queue ({pp(v)} pp; {ci or 'no interval'})"
    exact = (row.get("exact_fill_rate_pct") or {}).get("value")
    rel = f"{abs(v) / exact * 100:.0f}% {'more' if s > 0 else 'fewer'} fills; " if exact else ""
    return f"fills {abs(v):.2f} pp {'more' if s > 0 else 'fewer'} orders than the real queue ({rel}{ci})"


def settings(rows: list[dict[str, Any]], library: str) -> list[dict[str, Any]]:
    """The rows of `library` a reader can pick (not its L3 model), closest point estimate first."""
    out = [
        r
        for r in rows
        if r["library"] == library
        and parse_id(r["model_id"])[0] not in NOT_A_SETTING
        and (r.get("fill_rate_bias_pp") or {}).get("value") is not None
    ]
    return sorted(out, key=lambda r: (abs(r["fill_rate_bias_pp"]["value"]), r["model_id"]))


def library_answer(rows: list[dict[str, Any]], library: str) -> str | None:
    """One plain sentence (two at most) for a library from data API rows of one tier and horizon: its default
    against the real queue when it has one (NautilusTrader), else the range of its settings; then its closest
    setting and whether it is still clearly off."""
    mine = settings(rows, library)
    if not mine:
        return None
    name = LIBRARY_NAMES[library]
    close = mine[0]
    default = next((r for r in mine if is_default(r["model_id"])), None)
    if default is not None:
        lead = f"{name}'s default (touch fill) {_how_much(default)}."
        if close is default:
            return lead + " It is also the closest of its settings in the atlas."
    elif len(mine) == 1:
        return f"{label(close['model_id'])} {_how_much(close)}."
    else:
        vals = sorted(r["fill_rate_bias_pp"]["value"] for r in mine)
        lead = (
            f"{name}'s {len(mine)} settings in the atlas range from {pp(vals[0])} to {pp(vals[-1])} pp against the "
            "real queue."
        )
    s = side(close["fill_rate_bias_pp"])
    tail = (
        "no clear difference from the real queue"
        if s == 0
        else f"still clearly {'too optimistic' if s > 0 else 'too pessimistic'}"
    )
    return f"{lead} Closest: {label(close['model_id'])}, {pp(close['fill_rate_bias_pp']['value'])} pp, {tail}."


def exact_answer(rows: list[dict[str, Any]], horizon: str, where: str, venue: str) -> str | None:
    """`Within 1 minute, the real queue on IEX DEEP+ fills 11.97% of small resting orders (all symbols).`"""
    vals = [(r.get("exact_fill_rate_pct") or {}).get("value") for r in rows]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return (
        f"Within {horizon_words(horizon)}, the real queue on {venue} fills {pct(vals[0])}% of small resting "
        f"orders ({where})."
    )


__all__ = [
    "FAMILY_LIBRARY",
    "LIBRARIES",
    "LIBRARY_NAMES",
    "NOT_A_SETTING",
    "TIER_WORDS",
    "VERDICT_TEXT",
    "exact_answer",
    "horizon_label",
    "horizon_words",
    "interval_text",
    "is_default",
    "label",
    "library_answer",
    "library_of",
    "parse_horizon",
    "parse_id",
    "pct",
    "pp",
    "settings",
    "side",
    "verdict_key",
]
