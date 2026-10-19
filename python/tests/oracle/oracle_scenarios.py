"""Hand-built tapes for the oracle tests: SPEC 6.2 S1-S16 and one or more scenarios per audit category.

Each scenario carries:
- `oracle`: the facts the oracle must produce (normative for the oracle, derived by hand from price/display/time
  matching of the reconstructed taker flow),
- `as_written`: what SPEC 5.2 as written (et semantics, no audit fixes) produces, derived by hand; the
  discrepancy report against it must name `category`,
- `ruled`: "agree" when a queuetruth that implements the audit rulings A1-A6 must match the oracle exactly, or
  "contested" when the rulings as worded and the oracle differ (reported, not asserted).

Base book E0 (`ts = 10`): `A o1 B 100 x60`, `A o2 B 100 x40`, `A o9 A 101 x500`. Shadow X: bid 100, qty 10,
`ts_submit = 15`, `latency_ns = 0`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qt_oracle.engine import Amend, ShadowSpec
from qt_oracle.scenarios import amend, build_tape, meta_for, shadow

E0 = (10, ["A o1 B 100 60", "A o2 B 100 40", "A o9 A 101 500"])
E1 = (20, ["A o3 B 100 30"])
E2_S1 = (30, ["T A 100 60", "F o1 60", "C o1 60"])
E3_S1 = (40, ["T A 100 50", "F o2 40", "C o2 40", "F o3 10", "C o3 10"])
S1_EVENTS = [E0, E1, E2_S1, E3_S1]
S2_EVENTS = [E0, (20, ["A o3 B 100 50"]), E2_S1, E3_S1, (50, ["T A 100 25", "F o3 25", "C o3 25"])]


def itch(events: list[tuple[int, list[str]]]) -> list[tuple[int, list[str]]]:
    """Nasdaq ITCH style: every message is its own event (one sequence each): an `A`, `M`, `R` or stand-alone
    `C` line alone; an execution `T` + `F` + `C` (or `F` + `C` without a print); a lone `T` (hidden print)."""
    out: list[tuple[int, list[str]]] = []
    for ts, lines in events:
        cur: list[str] = []
        for ln in lines:
            k = ln[0]
            if k in "AMR":
                if cur:
                    out.append((ts, cur))
                    cur = []
                out.append((ts, [ln]))
            elif k == "T":
                if cur:
                    out.append((ts, cur))
                cur = [ln]
            elif k == "F":
                if cur and cur[-1][0] == "C":
                    out.append((ts, cur))
                    cur = []
                cur.append(ln)
            elif k == "C":
                if cur and cur[-1][0] == "F":
                    cur.append(ln)
                    out.append((ts, cur))
                    cur = []
                else:
                    if cur:
                        out.append((ts, cur))
                        cur = []
                    out.append((ts, [ln]))
        if cur:
            out.append((ts, cur))
    return out


@dataclass
class Scenario:
    name: str
    events: list[tuple[int, list[str]]]
    shadows: list[ShadowSpec]
    venue: str = "cme"
    amends: dict[str, list[Amend]] = field(default_factory=dict)
    exclude: frozenset[int] = frozenset()
    oracle: dict[str, dict[str, Any]] = field(default_factory=dict)  # shadow_id -> expected oracle facts
    as_written: dict[str, dict[str, Any]] = field(default_factory=dict)  # shadow_id -> SPEC-5.2-as-written facts
    category: str | None = None  # expected discrepancy category vs as_written (None = agreement)
    ruled: str = "agree"

    @property
    def seq_per_event(self) -> bool:
        return self.venue == "nasdaq"

    def records(self):
        return build_tape(self.events, seq_per_event=self.seq_per_event)

    def meta(self):
        return meta_for(self.venue)


def fills(*rows: tuple) -> list[dict[str, Any]]:
    """(ts, qty, reason[, px[, liquidity]])."""
    out = []
    for r in rows:
        d = {"ts_event": r[0], "fill_qty": r[1], "reason": r[2]}
        if len(r) > 3:
            d["fill_px"] = r[3]
        if len(r) > 4:
            d["liquidity"] = r[4]
        out.append(d)
    return out


def _w(filled: int, first: int | None, reason: str, fl: list[dict] | None = None, **kw) -> dict[str, Any]:
    d = {"filled_qty": filled, "ts_first_fill": first, "first_fill_reason": reason}
    if fl is not None:
        d["fills"] = [dict(f, liquidity=f.get("liquidity"), fill_px=f.get("fill_px", 100)) for f in fl]
    d.update(kw)
    return d


X = shadow()


def spec_scenarios() -> list[Scenario]:
    """SPEC 6.2 S1-S16 (S15 is an input-validation case of queuetruth, not a fill question)."""
    sc = [
        Scenario(
            "S1",
            S1_EVENTS,
            [X],
            oracle={
                "X": dict(
                    ts_activated_event=20,
                    queue_ahead_at_live=100,
                    rank_at_live=2,
                    level_qty_at_live=100,
                    end_state="filled",
                    queue_ahead_at_first_fill=40,
                    fills=fills((40, 10, "queue_depleted")),
                )
            },
        ),
        Scenario(
            "S2",
            S2_EVENTS,
            [shadow(qty=30)],
            oracle={"X": dict(fills=fills((40, 10, "queue_depleted"), (50, 20, "queue_depleted")), end_state="filled")},
        ),
        Scenario(
            "S3",
            [E0, E1, (30, ["C o1 60"]), (40, ["T A 100 45", "F o2 40", "C o2 40", "F o3 5", "C o3 5"])],
            [X],
            oracle={
                "X": dict(
                    fills=fills((40, 5, "queue_depleted")),
                    end_state="open_at_end",
                    traj=[(15, 100, 2), (30, 40, 1), (40, 0, 0)],
                )
            },
        ),
        Scenario("S4", [E0, E1, (30, ["C o3 30"])], [X], oracle={"X": dict(fills=[], traj=[(15, 100, 2)])}),
        Scenario(
            "S5", [E0, E1, (30, ["C o1 20"])], [X], oracle={"X": dict(fills=[], traj=[(15, 100, 2), (30, 80, 2)])}
        ),
        Scenario(
            "S6",
            [E0, E1, (30, ["M o1 B 100 70"]), (40, ["T A 100 45", "F o2 40", "C o2 40", "F o3 5", "C o3 5"])],
            [X],
            oracle={"X": dict(fills=fills((40, 5, "queue_depleted")), traj=[(15, 100, 2), (30, 40, 1), (40, 0, 0)])},
        ),
        Scenario(
            "S7",
            [
                (10, ["A o1 B 100 60", "A o2 B 100 40", "A o9 A 101 500", "A o4 B 99 50"]),
                (20, ["A o5 A 102 10"]),
                (30, ["T A 100 100", "F o1 60", "C o1 60", "F o2 40", "C o2 40", "T A 99 20", "F o4 20", "C o4 20"]),
            ],
            [X],
            oracle={"X": dict(fills=fills((30, 10, "trade_through")), queue_ahead_at_first_fill=100)},
        ),
        Scenario(
            "S10",
            S1_EVENTS,
            [shadow(latency_ns=5)],
            oracle={"X": dict(ts_activated_event=30, queue_ahead_at_live=130, rank_at_live=3, fills=[])},
        ),
        Scenario(
            "S11a",
            S1_EVENTS,
            [shadow(ts_cancel=35)],
            oracle={"X": dict(fills=[], end_state="cancelled", ts_cancel_effective=35)},
        ),
        Scenario(
            "S11b",
            S1_EVENTS,
            [shadow(ts_cancel=40)],
            oracle={"X": dict(fills=fills((40, 10, "queue_depleted")), end_state="filled")},
        ),
        Scenario("S12", itch([E0, E1, (30, ["T N 100 500"])]), [X], venue="nasdaq", oracle={"X": dict(fills=[])}),
        Scenario(
            "S13",
            S2_EVENTS,
            [shadow(qty=30)],
            amends={"X": [amend("X", 25, 10)]},
            oracle={"X": dict(fills=fills((40, 10, "queue_depleted")), end_state="filled")},
        ),
        Scenario("S14", [E0, E1, (30, ["R"])], [X], oracle={"X": dict(fills=[], end_state="censored_clear")}),
        Scenario(
            "S16",
            [E0, E1],
            [
                shadow("t0", price=None, price_mode="touch", offset_ticks=0),
                shadow("tm1", price=None, price_mode="touch", offset_ticks=-1),
                shadow("tp1", price=None, price_mode="touch", offset_ticks=1),
            ],
            oracle={
                "t0": dict(price=100, fills=[]),
                "tm1": dict(price=99, fills=[]),
                "tp1": dict(price=101, end_state="rejected_would_cross", fills=[]),
            },
        ),
        Scenario(
            "S16_no_touch",
            [(10, ["A o9 A 101 500"]), E1],
            [shadow("t0", price=None, price_mode="touch", offset_ticks=0)],
            oracle={"t0": dict(end_state="rejected_no_touch", fills=[])},
        ),
    ]
    # SPEC-as-written agrees with the oracle on all of these (the audit re-derived them; only S8/S9 differ).
    for s in sc:
        for sid, o in s.oracle.items():
            if "fills" in o:
                fl = o["fills"]
                s.as_written[sid] = {
                    "filled_qty": sum(f["fill_qty"] for f in fl),
                    "ts_first_fill": fl[0]["ts_event"] if fl else None,
                    "first_fill_reason": fl[0]["reason"] if fl else "never",
                }
    return sc


def audit_scenarios() -> list[Scenario]:
    """One or more scenarios per audit category."""
    e0_itch = itch([E0])
    return [
        # ---- hidden prints (audit 3, ruling A3), nasdaq
        Scenario(
            "S17_hidden_after_queue",
            e0_itch
            + itch([(30, ["T A 100 60", "F o1 60", "C o1 60", "T A 100 40", "F o2 40", "C o2 40"])])
            + [(30, ["T N 100 50"])],
            [X],
            venue="nasdaq",
            oracle={"X": dict(fills=fills((30, 10, "queue_depleted", 100, "maker")))},
            as_written={"X": _w(0, None, "never", [])},
            category="hidden_prints",
        ),
        Scenario(
            "S17b_hidden_with_display_behind",
            e0_itch
            + itch([E1])
            + itch([(30, ["T A 100 60", "F o1 60", "C o1 60", "T A 100 40", "F o2 40", "C o2 40"])])
            + [(30, ["T N 100 50"])],
            [X],
            venue="nasdaq",
            oracle={"X": dict(fills=[])},
            as_written={"X": _w(0, None, "never", [])},
            category=None,
        ),
        Scenario(
            "S17c_hidden_through",
            e0_itch
            + itch([(30, ["T A 100 60", "F o1 60", "C o1 60", "T A 100 40", "F o2 40", "C o2 40"])])
            + [(30, ["T N 99 30"])],
            [X],
            venue="nasdaq",
            oracle={"X": dict(fills=fills((30, 10, "trade_through", 100, "maker")))},
            as_written={"X": _w(0, None, "never", [])},
            category="hidden_prints",
        ),
        Scenario(
            "S17d_hidden_inside_spread_infer_side",
            [*itch([(10, ["A o1 B 99 60", "A o9 A 102 500"])]), (30, ["T N 100 40"])],
            [shadow(price=100)],
            venue="nasdaq",
            oracle={"X": dict(fills=fills((30, 10, "queue_depleted", 100, "maker")))},
            as_written={"X": _w(0, None, "never", [])},
            category="hidden_prints",
        ),
        # ---- off-price and non-printable executions (audit 4, ruling A4), nasdaq
        Scenario(
            "S18_nonprintable_ahead",
            e0_itch
            + itch([E1])
            + [(30, ["F o1 60", "C o1 60"])]
            + itch([(40, ["T A 100 40", "F o2 40", "C o2 40", "T A 100 5", "F o3 5", "C o3 5"])]),
            [X],
            venue="nasdaq",
            oracle={"X": dict(fills=fills((40, 5, "queue_depleted", 100, "maker")))},
            as_written={"X": _w(0, None, "never", [])},
            category="off_price_executions",
        ),
        Scenario(
            "S18b_off_price_print_not_trade_through",
            e0_itch + itch([E1]) + [(30, ["T A 99 60", "F o1 60 @99", "C o1 60"])],
            [X],
            venue="nasdaq",
            oracle={"X": dict(fills=[], traj=[(15, 100, 2), (30, 40, 1)])},
            as_written={"X": _w(10, 30, "trade_through", fills((30, 10, "trade_through")))},
            category="off_price_executions",
        ),
        Scenario(
            "S18c_price_improved_execution_behind_shadow",
            [*e0_itch, *itch([E1]), (30, ["T A 101 10", "F o3 10 @101", "C o3 10"])],
            [X],
            venue="nasdaq",
            oracle={"X": dict(fills=[], traj=[(15, 100, 2)])},
            as_written={"X": _w(0, None, "never", [])},
            category=None,
        ),
        # ---- crosses (audit 5, ruling A5), cme
        Scenario(
            "S8_tape_crossing_add",
            [*S1_EVENTS[:2], (30, ["A o6 A 100 5"])],
            [X],
            oracle={"X": dict(fills=[])},
            as_written={"X": _w(10, 30, "crossed", fills((30, 10, "crossed")))},
            category="crosses",
        ),
        Scenario(
            "S8b_add_crosses_only_shadow",
            [
                (10, ["A o1 B 100 60", "A o2 B 100 40", "A o9 A 102 500"]),
                (30, ["A o6 A 101 5"]),
                (40, ["A o7 B 99 10"]),
            ],
            [shadow(price=101)],
            oracle={"X": dict(fills=fills((30, 5, "crossed", 101, "maker")), queue_ahead_at_live=0)},
            as_written={"X": _w(10, 30, "crossed", fills((30, 10, "crossed", 101)))},
            category="crosses",
        ),
        Scenario(
            "S9_crossed_at_live_taker",
            S1_EVENTS[:2],
            [shadow(price=101)],
            oracle={"X": dict(fills=fills((15, 10, "crossed_at_live", 101, "taker")))},
            as_written={
                "X": _w(
                    10,
                    15,
                    "crossed_at_live",
                    fills((15, 10, "crossed_at_live", 101, "maker")),
                    first_fill_liquidity="maker",
                )
            },
            category="crosses",
        ),
        Scenario(
            "S9b_crossed_at_live_bigger_than_opposite",
            S1_EVENTS[:2],
            [shadow(price=101, qty=600)],
            oracle={"X": dict(fills=fills((15, 500, "crossed_at_live", 101, "taker")), queue_ahead_at_live=0)},
            as_written={"X": _w(600, 15, "crossed_at_live", fills((15, 600, "crossed_at_live", 101)))},
            category="crosses",
        ),
        # ---- CME modify priority (audit 6, ruling A6), cme
        Scenario(
            "S19_same_size_modify_loses_priority",
            [E0, E1, (30, ["M o1 B 100 60"]), E3_S1],
            [X],
            oracle={"X": dict(fills=fills((40, 10, "queue_depleted")), traj=[(15, 100, 2), (30, 40, 1), (40, 0, 0)])},
            as_written={"X": _w(0, None, "never", [])},
            category="cme_modify",
        ),
        Scenario(
            "S19b_iceberg_refresh_then_cancel",
            [
                E0,
                E1,
                (30, ["T A 100 60", "F o1 60", "M o1 B 100 60"]),
                (35, ["C o1 60"]),
                (40, ["T A 100 40", "F o2 40", "C o2 40"]),
            ],
            [X],
            oracle={"X": dict(fills=[], traj=[(15, 100, 2), (30, 40, 1), (40, 0, 0)])},
            as_written={"X": _w(10, 35, "queue_depleted", fills((35, 10, "queue_depleted")))},
            category="cme_modify",
        ),
        Scenario(
            "S19c_modify_above_post_fill_remaining",
            [E0, E1, (30, ["T A 100 20", "F o1 20", "M o1 B 100 50"]), E3_S1],
            [X],
            oracle={"X": dict(fills=fills((40, 10, "queue_depleted")))},
            as_written={"X": _w(0, None, "never", [])},
            category="cme_modify",
        ),
        # ---- own-order exclusion (audit 1, ruling A1), cme
        Scenario(
            "S20_own_order_excluded",
            [E0, (12, ["A o5 B 100 30"]), E2_S1, (40, ["T A 100 60", "F o2 40", "C o2 40", "F o5 20", "C o5 20"])],
            [shadow(qty=30, ts_submit=12)],
            exclude=frozenset({5}),
            oracle={"X": dict(fills=fills((40, 20, "queue_depleted")), queue_ahead_at_live=100)},
            as_written={"X": _w(0, None, "never", [], queue_ahead_at_live=130)},
            category="own_order_exclusion",
        ),
        *twin_scenarios(),
    ]


# Ruling A1 per-shadow twins: an excluded order stays on the tape for everyone except the shadow that enters at its
# record, which takes its exact queue place and is filled by its executions (at the execution price). Facts derived
# by hand from that definition; no SPEC-as-written expectations (category None).
T_ASK = (10, ["A o1 A 101 50", "A o9 B 98 500"])
T_OFF_DISPLAY = [T_ASK, (20, ["A o5 A 101 30"]), (30, ["T B 100 30", "F o5 30 @100", "C o5 30"])]


def twin_scenarios() -> list[Scenario]:
    burst = [
        (5, ["A o9 A 103 500"]),
        (10, ["A o1 B 100 7"]),
        (10, ["A o5 B 100 1"]),
        (10, ["A o2 B 100 5"]),
        (10, ["A o6 B 100 1"]),
        (10, ["A o3 B 100 9"]),
        (20, ["T A 100 7", "F o1 7", "C o1 7"]),
        (20, ["T A 100 1", "F o5 1", "C o5 1"]),
        (20, ["T A 100 5", "F o2 5", "C o2 5"]),
        (20, ["T A 100 1", "F o6 1", "C o6 1"]),
    ]
    return [
        Scenario(
            # The twin executes at its limit 100 below its displayed 101 while o1 is displayed ahead at 101: that
            # execution is its own shadow's fill at 100.
            "S20b_twin_executed_off_display",
            T_OFF_DISPLAY,
            [shadow(side="A", price=101, qty=30, ts_submit=20)],
            venue="nasdaq",
            exclude=frozenset({5}),
            oracle={
                "X": dict(
                    ts_activated_event=20,
                    queue_ahead_at_live=50,
                    fills=fills((30, 30, "queue_depleted", 100, "maker")),
                    end_state="filled",
                )
            },
        ),
        Scenario(
            # Live 1 ns before the twin's record that opens its timestamp: linked at that record.
            "S20c_twin_one_ns_alignment",
            T_OFF_DISPLAY,
            [shadow(side="A", price=101, qty=30, ts_submit=19)],
            venue="nasdaq",
            exclude=frozenset({5}),
            oracle={"X": dict(queue_ahead_at_live=50, fills=fills((30, 30, "queue_depleted", 100)))},
        ),
        Scenario(
            # Same-timestamp burst: each shadow takes its own twin's place; the other twin is a real order for it.
            "S20d_twins_in_a_same_timestamp_burst",
            burst,
            [shadow("A", qty=1, ts_submit=10), shadow("B", qty=1, ts_submit=10)],
            venue="nasdaq",
            exclude=frozenset({5, 6}),
            oracle={
                "A": dict(ts_activated_event=10, queue_ahead_at_live=7, fills=fills((20, 1, "queue_depleted"))),
                "B": dict(ts_activated_event=10, queue_ahead_at_live=13, fills=fills((20, 1, "queue_depleted"))),
            },
        ),
        Scenario(
            # A side-N print at the twin's displayed price: the twin is still displayed there, so it is unusable.
            "S20e_hidden_print_at_twin_price_declined",
            [
                (10, ["A o9 A 102 100", "A o8 B 99 100"]),
                (20, ["A o5 B 100 100"]),
                (30, ["T N 100 33"]),
                (40, ["C o5 100"]),
                (50, ["A o4 B 98 10"]),
            ],
            [shadow(qty=100, ts_submit=20, ts_cancel=40)],
            venue="nasdaq",
            exclude=frozenset({5}),
            oracle={"X": dict(fills=[], end_state="cancelled", queue_ahead_at_live=0)},
        ),
        Scenario(
            # Another shadow's twin stays in the queue: ahead of B, and its execution is not B's.
            "S20f_another_twin_stays_in_the_queue",
            [
                (5, ["A o9 A 103 500"]),
                (10, ["A o5 B 100 10"]),
                (20, ["A o6 B 100 10"]),
                (30, ["T A 100 10", "F o5 10", "C o5 10"]),
                (40, ["C o6 10"]),
                (50, ["A o4 B 98 10"]),
            ],
            [shadow("A", qty=10, ts_submit=10), shadow("B", qty=10, ts_submit=20, ts_cancel=40)],
            exclude=frozenset({5, 6}),
            oracle={
                "A": dict(fills=fills((30, 10, "queue_depleted"))),
                "B": dict(fills=[], queue_ahead_at_live=10, end_state="cancelled"),
            },
        ),
        Scenario(
            # A twin added by a replace (C of o7 + A of o5) inside an event: its shadow activates there, behind o1's
            # remaining 16; Y, live before, keeps its place ahead of the new order and is filled by its execution.
            "S20g_twin_added_by_replace_mid_event",
            [
                (5, ["A o9 A 103 500"]),
                (10, ["A o1 B 100 20", "A o7 B 100 10"]),
                (20, ["T A 100 4", "F o1 4", "C o1 4", "C o7 10", "A o5 B 100 10"]),
                (30, ["T A 100 16", "F o1 16", "C o1 16"]),
                (40, ["T A 100 10", "F o5 10", "C o5 10"]),
            ],
            [shadow("X", qty=10, ts_submit=20), shadow("Y", qty=10, ts_submit=15)],
            venue="nasdaq",
            exclude=frozenset({5}),
            oracle={
                "X": dict(ts_activated_event=20, queue_ahead_at_live=16, fills=fills((40, 10, "queue_depleted"))),
                "Y": dict(queue_ahead_at_live=30, fills=fills((40, 10, "queue_depleted"))),
            },
        ),
        Scenario(
            # Two shadows enter at the twin's time: the first in input order is the twin's; the other is a plain
            # shadow that goes live at the next event behind the twin and is not filled by its execution.
            "S20h_first_shadow_in_input_order_is_the_twin",
            [T_ASK, (20, ["A o5 A 101 30"]), (21, ["A o6 A 101 10"]), (30, ["T B 100 30", "F o5 30 @100", "C o5 30"])],
            [
                shadow("X", side="A", price=101, qty=30, ts_submit=20),
                shadow("P", side="A", price=101, qty=30, ts_submit=20),
            ],
            venue="nasdaq",
            exclude=frozenset({5}),
            oracle={
                "X": dict(fills=fills((30, 30, "queue_depleted", 100))),
                "P": dict(ts_activated_event=21, queue_ahead_at_live=80, fills=[]),
            },
        ),
        Scenario(
            # CME later leg: a size increase (loses priority) is a new leg entered at the M; the leg's shadow takes
            # the order's new place at the tail (the order's old resting volume is its own, never ahead of it).
            "S20i_twin_later_leg_at_a_priority_losing_modify",
            [
                (5, ["A o9 A 103 500"]),
                (10, ["A o5 B 100 10"]),
                (20, ["A o1 B 100 30"]),
                (30, ["M o5 B 100 15"]),
                (40, ["T A 100 30", "F o1 30", "C o1 30"]),
                (50, ["T A 100 15", "F o5 15", "C o5 15"]),
            ],
            [shadow("X1", qty=10, ts_submit=10, ts_cancel=30), shadow("X2", qty=15, ts_submit=30)],
            exclude=frozenset({5}),
            oracle={
                "X1": dict(fills=[], end_state="cancelled", queue_ahead_at_live=0),
                "X2": dict(
                    ts_activated_event=30,
                    queue_ahead_at_live=30,
                    fills=fills((50, 15, "queue_depleted")),
                    traj=[(30, 30, 1), (40, 0, 0), (50, 0, 0)],
                ),
            },
        ),
    ]
