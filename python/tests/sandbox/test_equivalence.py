"""The sandbox's fills equal one exact replay of the same orders.

The reference here is independent of the sandbox's own machinery: the committed schedule is rebuilt from the order
log alone (`schedule_from_log`), replayed with `queuetruth.replay` on the original dataset directory (every partition,
not the tape or the certification view), and prefixes are cut with plain pyarrow (`write_prefix`). Seeded random
agents send passive, marketable, pegged, IOC and market orders, full and partial cancels, with latency, jitter and
feed latency, on both synthetic fixtures (CME rules with modifies and book clears; Nasdaq rules with hidden prints).
"""

from __future__ import annotations

import datetime
import random
from pathlib import Path
from typing import Any

import pytest

from queuetruth.sandbox import Sandbox
from queuetruth.sandbox import tape as tape_mod
from queuetruth.sandbox.orders import FILL_COLUMNS
from sandbox.sbx_helpers import (
    DAY0,
    SYNTH,
    SYNTH_NASDAQ,
    Tape,
    at,
    base_spec,
    prefix_rows,
    px,
    random_agent,
    reference_replay,
    schedule_from_log,
    write_dataset,
    write_prefix,
)

pytestmark = pytest.mark.requires_core

CASES = [
    # dataset, instrument, date, start, end, latency, jitter, feed latency, seed
    (SYNTH, 1, "2026-06-15", "13:31", "13:45", "250us", "500us", 0, 1),
    (SYNTH, 2, "2026-06-16", "16:00", "16:20", "2ms", 0, "300ms", 2),
    (SYNTH_NASDAQ, 1, "2026-06-15", "14:10", "14:25", "100us", "1ms", 0, 3),
    (SYNTH_NASDAQ, 2, "2026-06-16", "15:00", "15:15", 0, 0, "50ms", 4),
]


def _spec(case: tuple) -> dict[str, Any]:
    ds, iid, date, start, end, lat, jit, feed, seed = case
    return {
        "dataset": {"path": str(ds)},
        "instrument_id": iid,
        "session_date": date,
        "start": start,
        "end": end,
        "latency": lat,
        "jitter": jit,
        "feed_latency": feed,
        "seed": seed,
        "fees": {"maker_per_unit": -0.2, "taker_per_unit": 0.3, "taker_bps": 0.5},
    }


def _run(case: tuple) -> tuple[Sandbox, list[Any], dict[str, list[dict[str, Any]]], Any]:
    sb = Sandbox.open(_spec(case))
    seen = random_agent(sb, random.Random(case[-1]), steps=80)
    reported_rows = {o.order_id: [dict(f) for f in o.fills] for o in sb.orders}
    card = sb.finish()
    return sb, seen, reported_rows, card


def _known(ref: dict[str, Any], oid: str, row: dict[str, Any]) -> int:
    if row["reason"] == "crossed_at_live":
        return int(ref["results"][oid]["ts_activated_event"])
    return int(row["ts_event"])


@pytest.mark.parametrize("case", CASES, ids=[f"{c[0].name}-{c[1]}-{c[2]}" for c in CASES])
def test_session_fills_equal_one_full_replay_of_the_committed_orders(tmp_path: Path, case: tuple) -> None:
    sb, seen, reported_rows, card = _run(case)
    log = sb.order_log()
    ref = reference_replay(case[0], log, case[1], tmp_path / "ref")
    ref_rows = {oid: [{k: r.get(k) for k in FILL_COLUMNS} for r in rows] for oid, rows in ref["fills"].items()}
    n_reported = sum(len(v) for v in reported_rows.values())
    # non-trivial episodes: many fills, both liquidities, several fill reasons, rejections and cancels in the log
    assert n_reported >= 15
    reasons = {r["reason"] for rows in ref_rows.values() for r in rows}
    assert {"crossed_at_live", "queue_depleted"} <= reasons
    assert {r["liquidity"] for rows in ref_rows.values() for r in rows} == {"maker", "taker"}
    kinds = [e["kind"] for e in log["entries"]]
    assert kinds.count("cancel") >= 3 and "submit" in kinds
    # 1. every fill reported during the session is the reference's row, column for column, in order
    for oid, rows in reported_rows.items():
        assert rows == ref_rows.get(oid, [])[: len(rows)], oid
    # 2. step attribution: step k reported exactly the reference fills knowable in (cut_{k-1}, cut_k]
    cuts = [(o.step, o.visible_ns) for o in seen]
    by_step: dict[int, set[tuple[str, int]]] = {}
    for obs in seen:
        for f in obs.fills:
            assert f.step == obs.step
            by_step.setdefault(obs.step, set()).add((f.order_id, f.fill_seq))
    for k, (step, cut) in enumerate(cuts):
        lo = cuts[k - 1][1] if k else None
        want = {
            (oid, int(r["fill_seq"]))
            for oid, rows in ref["fills"].items()
            for r in rows
            if _known(ref, oid, r) <= cut and (lo is None or _known(ref, oid, r) > lo)
        }
        assert by_step.get(step, set()) == want, step
    # 3. the scorecard holds exactly the reference fills (including those after the last visible cut)
    got = sorted((f.order_id, f.fill_seq, f.ts_event, f.price, f.qty, f.reason, f.liquidity) for f in card.fills)
    want_all = sorted(
        (
            oid,
            int(r["fill_seq"]),
            int(r["ts_event"]),
            int(r["fill_px"]),
            int(r["fill_qty"]),
            r["reason"],
            r["liquidity"],
        )
        for oid, rows in ref["fills"].items()
        for r in rows
    )
    assert got == want_all
    # 4. final order states agree with the reference end states
    for o in card.orders:
        res = ref["results"][o.order_id]
        if ref["orders"][o.order_id]["_ioc"]:
            continue
        expect = {
            "filled": "filled",
            "amended_to_filled": "filled",
            "cancelled": "cancelled",
            "rejected_would_cross": "rejected",
            "rejected_no_touch": "rejected",
            "censored_clear": "censored",
            "censored_gap": "censored",
            "open_at_end": "expired",
            "censored_session_end": "expired",
        }[res["end_state"]]
        assert o.status == expect, (o.order_id, res["end_state"])
    # 5. the sandbox's own runtime check agrees, for fills and for every order's shown states
    c = card.consistency
    assert c.equal and c.fills_reported == n_reported and c.fills_matched == n_reported
    assert c.timestamps_monotone and c.fills_unreported == 0 and c.fills_misattributed == 0
    assert c.statuses_checked == len(card.orders) and c.statuses_mismatched == 0


def _expected_status(order: dict[str, Any], res: dict[str, Any], arrival_qty: int, qty: int) -> str:
    """The status a prefix replay implies (the sandbox's documented mapping, restated)."""
    act = res.get("ts_activated_event")
    end = res["end_state"]
    if order["_ioc"]:
        if act is None:
            return "pending"
        return "filled" if arrival_qty >= qty else "cancelled"
    table = {
        "filled": "filled",
        "amended_to_filled": "filled",
        "cancelled": "cancelled",
        "rejected_would_cross": "rejected",
        "rejected_no_touch": "rejected",
        "censored_clear": "censored",
        "censored_gap": "censored",
    }
    if end in table:
        return table[end]
    return "working" if act is not None else "pending"


def _snapshots(seen: list[Any]) -> list[tuple[int, int, dict[str, Any], dict[str, list[tuple[int, int, int]]]]]:
    """(step, visible cut, every order as last shown, every fill reported so far) after each observation."""
    state: dict[str, Any] = {}
    snaps = []
    fills_upto: dict[str, list[tuple[int, int, int]]] = {}
    for obs in seen:
        for o in obs.orders:
            state[o.order_id] = o
        for f in obs.fills:
            fills_upto.setdefault(f.order_id, []).append((f.fill_seq, f.qty, f.price))
        snaps.append((obs.step, obs.visible_ns, dict(state), {k: list(v) for k, v in fills_upto.items()}))
    return snaps


def _check_against_prefix_replays(
    tmp_path: Path, dataset: Path, iid: int, session_date: int, snaps: list, log: dict[str, Any]
) -> int:
    """Each snapshot's orders and fills equal a fresh replay of an independently cut prefix at the snapshot's cut
    with the episode's final schedule; returns the (step, order) pairs checked."""
    _, _, orders = schedule_from_log(log, iid)
    checked = 0
    for step, cut, st, fu in snaps:
        if prefix_rows(dataset, iid, session_date, cut) == 0:
            continue
        prefix = write_prefix(dataset, tmp_path / f"p{step}", iid, session_date, cut)
        ref = reference_replay(prefix, log, iid, tmp_path / f"r{step}")
        for oid, view in st.items():
            rows = ref["fills"].get(oid, [])
            assert [(int(r["fill_seq"]), int(r["fill_qty"]), int(r["fill_px"])) for r in rows] == fu.get(oid, []), (
                step,
                oid,
            )
            arrival = sum(int(r["fill_qty"]) for r in rows)
            status = _expected_status(orders[oid], ref["results"][oid], arrival, view.qty)
            assert view.status == status, (step, oid, ref["results"][oid]["end_state"])
            checked += 1
    return checked


@pytest.mark.parametrize("case", [CASES[0], CASES[1], CASES[2]], ids=["cme-1", "cme-feedlag", "nasdaq-1"])
def test_every_step_equals_a_fresh_prefix_replay_with_the_final_orders(tmp_path: Path, case: tuple) -> None:
    """What the agent saw at step k equals a replay of the data up to the step's cut with every order of the whole
    episode, including the orders and cancels it sent later: later instructions cannot reach back."""
    sb = Sandbox.open(_spec(case))
    seen = random_agent(sb, random.Random(case[-1] + 100), steps=30)
    snaps = _snapshots(seen)
    sb.finish()
    session_date = (datetime.date.fromisoformat(case[2]) - datetime.date(1970, 1, 1)).days
    checked = _check_against_prefix_replays(tmp_path, case[0], case[1], session_date, snaps[1::3], sb.order_log())
    assert checked >= 20


def gap_scenario(tmp_path: Path, *, trade: bool) -> Path:
    """A feed gap from 13 s to 15 s with records of the instrument inside it (ingest writes such gaps: crypto warmup
    gaps, Hyperliquid partitions without an L4 snapshot, sequence gaps over non-monotone times). Bids b0 5 (1 s) and
    b1 3 (12 s) at 100.00, events at 13 and 14 s, a third bid b2 4 at 100.00 at 14.2 s, then (`trade`) a sell at
    14.5 s that executes b0, b1 and 1 of b2; the clock ticks to 17 s."""
    t = Tape()
    b0 = t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    t.noop(at(11))
    b1 = t.add(at(12), "B", px(0), 3)
    t.noop(at(13))
    t.noop(at(14))
    b2 = t.add(at(14.2), "B", px(0), 4)
    if trade:
        t.trade(at(14.5), "A", [(b0, 5), (b1, 3), (b2, 1)])
    for k in (15.5, 16, 17):
        t.noop(at(k))
    gaps = [{"instrument_id": 1, "ts_start": at(13), "ts_end": at(15), "reason": "sequence gap"}]
    return write_dataset(tmp_path / f"ds-{trade}", t, feed_gaps=gaps)


def _gap_episode(ds: Path) -> tuple[Sandbox, list[Any], Any]:
    """o1 rests from 11 s, ahead of b1, into the gap; o2 is sent inside the gap (13.5 s) and goes live at the 14 s
    event, after the gap has censored o1 there (the engine runs a due gap before activations)."""
    sb = Sandbox.open(base_spec(ds, start=at(10), end=at(17.5), latency="1ms"))
    sb.submit("buy", 1, price="100.00")
    seen = [sb.observe(), sb.advance(to=at(12)), sb.advance(to=at(13.5))]
    sb.submit("buy", 1, price="100.00")
    for to in (14.1, 14.3, 14.8, 15.2, 16.5):
        seen.append(sb.advance(to=at(to)))
    return sb, seen, sb.finish()


@pytest.mark.parametrize("trade", [True, False], ids=["fill-inside-gap", "working-inside-gap"])
def test_feed_gaps_with_records_inside_them_equal_the_full_replay(tmp_path: Path, trade: bool) -> None:
    ds = gap_scenario(tmp_path, trade=trade)
    sb, seen, card = _gap_episode(ds)
    log = sb.order_log()
    by_step = {obs.step: {o.order_id: o for o in obs.orders} for obs in seen}
    # o1 is censored at the 14 s event, the first after the gap's start: shown so at the 14.1 s step, never filled
    o1 = by_step[3]["o1"]
    assert (o1.status, o1.reason, o1.ts_done) == ("censored", "feed_gap", at(13))
    assert by_step[2]["o1"].status == "working"
    assert all(f.order_id != "o1" for obs in seen for f in obs.fills)
    # o2 went live inside the gap and is not censored by it; with the trade it fills at 14.5 s
    assert by_step[3]["o2"].status == "working"
    fills = [(f.order_id, f.ts_event, f.qty, f.reason) for obs in seen for f in obs.fills]
    assert fills == ([("o2", at(14.5), 1, "queue_depleted")] if trade else [])
    # every step equals an independent prefix replay with the final schedule, the end equals one full replay
    assert _check_against_prefix_replays(tmp_path, ds, 1, DAY0, _snapshots(seen), log) >= 10
    ref = reference_replay(ds, log, 1, tmp_path / "full")
    assert ref["results"]["o1"]["end_state"] == "censored_gap"
    assert [int(r["fill_qty"]) for r in ref["fills"].get("o2", [])] == ([1] if trade else [])
    c = card.consistency
    assert c.equal and c.statuses_checked == 2 and c.statuses_mismatched == 0 and c.fills_matched == int(trade)


def test_the_runtime_check_catches_gaps_that_enter_the_tape_too_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the earlier rule (a gap entered the tape only once it had ended), o1 was reported filled inside the gap
    and shown working after the full replay had censored it; `finish` must report that as a difference."""
    real = tape_mod._gap_doc

    def ended_only(src: Any, cut: int | None) -> Any:
        doc = real(src, cut)
        if doc is not None and cut is not None:
            full = real(src, None) or {}
            doc["feed_gaps"] = [g for g in full.get("feed_gaps") or [] if int(g["ts_end"]) <= cut]
        return doc

    monkeypatch.setattr(tape_mod, "_gap_doc", ended_only)
    for trade in (True, False):
        _, _, card = _gap_episode(gap_scenario(tmp_path, trade=trade))
        c = card.consistency
        assert not c.equal and c.statuses_mismatched >= 1, trade
        assert any(w.startswith("consistency:") and "o1" in w for w in card.warnings)
        if trade:  # the in-session fill of o1 is not a fill of the full replay
            assert c.fills_matched < c.fills_reported


def test_runtime_consistency_detects_a_changed_order_state(tmp_path: Path) -> None:
    """Orders are checked too: an order shown ended at another step, or with another end, is a mismatch."""
    for tamper in ("step", "end"):
        sb = Sandbox.open(_spec(CASES[0]))
        random_agent(sb, random.Random(5), steps=40)
        victim = next(o for o in sb.orders if o.final_step is not None)
        if tamper == "step":
            victim.final_step += 1
        else:
            status, reason, ts_done, filled = victim.shown_final
            victim.shown_final = (status, reason, (ts_done or 0) + 1, filled)
        card = sb.finish()
        c = card.consistency
        assert not c.equal and c.statuses_mismatched == 1 and c.fills_matched == c.fills_reported, tamper
        assert any(victim.order_id in w for w in card.warnings if w.startswith("consistency:"))


def test_runtime_consistency_detects_a_changed_fill(tmp_path: Path) -> None:
    """The finish-time check is not vacuous: a reported fill altered in memory is caught as a mismatch."""
    sb = Sandbox.open(_spec(CASES[0]))
    random_agent(sb, random.Random(5), steps=40)
    victim = next(o for o in sb.orders if o.fills)
    victim.fills[0] = {**victim.fills[0], "queue_ahead_before": victim.fills[0]["queue_ahead_before"] + 1}
    card = sb.finish()
    assert not card.consistency.equal
    assert card.consistency.fills_matched == card.consistency.fills_reported - 1
    assert any(w.startswith("consistency:") for w in card.warnings)


def test_prefix_stability_violation_raises(tmp_path: Path) -> None:
    """A fill already reported that a longer prefix would change is an engine error, never silently replaced."""
    from queuetruth.errors import QtError

    sb = Sandbox.open(_spec(CASES[0]))
    rng = random.Random(9)
    for _ in range(20):
        random_agent(sb, rng, steps=1)
        live = [o for o in sb.orders if o.fills and not o.final]
        if live:
            break
    else:
        pytest.skip("no partially filled working order in this run")
    o = live[0]
    o.fills[0] = {**o.fills[0], "fill_px": o.fills[0]["fill_px"] + 1}
    with pytest.raises(QtError) as e:
        for _ in range(50):
            sb.advance(by="5s")
    assert e.value.code == "QT-ENGINE-001" and "prefix stability" in e.value.message
    sb.close()
