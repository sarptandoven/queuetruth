"""Venue facts used by the oracle's matching engine (restated from SPEC 3.2 and audit rulings A3/A6).

Only what a price/time matching engine needs is kept:
- `modify_present`: whether `M` records exist on the feed (Nasdaq ITCH replaces are `C` + `A`; an `M` there is ignored).
- `modify_rule`: when an `M` keeps time priority.
  - `cme` (ruling A6): only a strict size decrease at the same price keeps priority. An `M` that leaves price and
    size unchanged (iceberg refresh, account change) loses it. A size increase or a price change loses it.
  - `decrease`: a size decrease or an unchanged size at the same price keeps priority (generic / Coinbase).
- `hidden_orders`: side-N prints are executions against non-displayed interest, which ranks behind every displayed
  order at the same price (Nasdaq price/display/time priority, ruling A3).
- `pair_by_sequence`: an `F` and the `C`/`M` that removes the executed size share one `sequence` (ITCH, one sequence
  per message); otherwise they are paired inside the event (CME).
- `cross_windows_s`: seconds after local midnight around the opening/closing crosses; side-N prints inside these
  windows are cross prints (bulk auction prints), never continuous-matching volume.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VenueFacts:
    name: str
    modify_present: bool
    modify_rule: str  # "cme" | "decrease"
    hidden_orders: bool
    pair_by_sequence: bool
    cross_windows_s: tuple[tuple[float, float], ...] = ()


_FACTS = {
    "nasdaq": VenueFacts("nasdaq", False, "decrease", True, True),
    "cme": VenueFacts("cme", True, "cme", False, False),
    "coinbase": VenueFacts("coinbase", True, "decrease", False, True),
    "generic": VenueFacts("generic", True, "decrease", True, True),
}

_ALIASES = {
    "itch": "nasdaq",
    "xnas": "nasdaq",
    "xnas.itch": "nasdaq",
    "glbx": "cme",
    "glbx.mdp3": "cme",
    "bitstamp": "generic",
    "bitfinex": "generic",
}


def venue_key(name: str) -> str:
    """The canonical venue name of an alias (SPEC 3.1), lower case; unknown names are returned lower-cased."""
    return _ALIASES.get(name.lower(), name.lower())


def venue_facts(
    name: str, *, open_s: int | None = None, close_s: int | None = None, cross_window_s: float = 1.0
) -> VenueFacts:
    """Facts for a venue name (SPEC 3.1 aliases). Nasdaq gets cross windows `[open, open + w)` and
    `[close, close + w)` when the session times are known."""
    key = venue_key(name)
    if key not in _FACTS:
        raise ValueError(f"unknown venue {name!r}")
    f = _FACTS[key]
    if key == "nasdaq" and open_s is not None and close_s is not None and cross_window_s > 0:
        f = VenueFacts(
            f.name,
            f.modify_present,
            f.modify_rule,
            f.hidden_orders,
            f.pair_by_sequence,
            ((float(open_s), open_s + cross_window_s), (float(close_s), close_s + cross_window_s)),
        )
    return f


def modify_keeps_priority(
    facts: VenueFacts, old_px: int, old_size: int, filled_pending: int, new_px: int, new_size: int
) -> bool:
    """Whether an `M` keeps time priority.

    `old_size` is the size the feed last showed for the order and `filled_pending` the executed quantity of the
    order reported (by `F`) in the same message/event but not yet removed. The order's true remaining size before
    the `M` is therefore `old_size - filled_pending`; an `M` to exactly that size is the book update of the
    execution itself and keeps priority. Anything larger is a refresh/increase and loses it."""
    if new_px != old_px:
        return False
    base = old_size - filled_pending
    if new_size > base:
        return False
    if new_size < base:
        return True
    # new_size == base
    if filled_pending > 0:
        return True  # pure execution update
    return facts.modify_rule != "cme"  # unchanged M: CME loses priority (A6), other venues keep it
