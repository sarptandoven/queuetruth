"""`session` and `replay` stepping give identical episodes.

The sandbox can bring its orders to each step's visible cut in two ways (`queuetruth.sandbox.session`): `replay`
re-runs the exact replay of the episode's prefix at every step (quadratic in the episode length), `session` keeps one
`Session.with_orders` that takes every instruction as it is sent and is advanced by whole events to the cut (one pass
over the data). The same agent, deciding from what it sees, must receive byte-identical observations, acks and
rejections on both, commit the same log (entry hashes and root) and get the same scorecard and fills.

Agents: seeded random agents on both synthetic fixtures (passive, marketable, pegged, IOC and market orders, full and
partial cancels, with latency, jitter and feed latency), and scripted agents on hand-made datasets for the cases
where the two methods could part: a pending order whose arrival falls in a trading halt announced before the cut, an
event whose records straddle the cut, an unterminated event at the end of the data, orders sent after the data ended,
orders still open at the episode end, a book clear and a feed gap.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

import queuetruth.api
from queuetruth.errors import QtError
from queuetruth.sandbox import Sandbox
from queuetruth.schemas.runid import canonical_json
from sandbox.sbx_helpers import (
    LAST,
    MS,
    SYNTH,
    SYNTH_NASDAQ,
    Tape,
    at,
    base_spec,
    px,
    random_agent,
    write_dataset,
)

pytestmark = pytest.mark.requires_core


class Recorder:
    """A sandbox proxy that records, as canonical JSON, everything the agent receives: observations, acks and
    rejections (code and message). With `swallow` a rejected instruction returns None instead of raising (scripted
    agents carry on); the random agent catches its own."""

    def __init__(self, sb: Sandbox, *, swallow: bool) -> None:
        self.sb = sb
        self.swallow = swallow
        self.docs: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sb, name)

    def _call(self, fn: Any, *a: Any, swallow: bool = False, **k: Any) -> Any:
        try:
            out = fn(*a, **k)
        except QtError as e:
            self.docs.append(canonical_json({"error": e.code, "message": e.message}))
            if swallow:
                return None
            raise
        self.docs.append(canonical_json(out.model_dump(mode="json")))
        return out

    def observe(self) -> Any:
        return self._call(self.sb.observe)

    def advance(self, **k: Any) -> Any:
        return self._call(self.sb.advance, **k)

    def submit(self, *a: Any, **k: Any) -> Any:
        return self._call(self.sb.submit, *a, swallow=self.swallow, **k)

    def cancel(self, *a: Any, **k: Any) -> Any:
        return self._call(self.sb.cancel, *a, swallow=self.swallow, **k)


def _episode(spec: dict[str, Any], stepping: str, drive: Any, swallow: bool) -> dict[str, Any]:
    sb = Sandbox.open(spec, stepping=stepping)
    rec = Recorder(sb, swallow=swallow)
    drive(rec)
    reported = {o.order_id: [dict(f) for f in o.fills] for o in sb.orders}
    steps = {o.order_id: (o.working_step, o.final_step, o.shown_final) for o in sb.orders}
    card = sb.finish()
    body = card.model_dump(mode="json")
    body.pop("timings")
    return {
        "docs": rec.docs,
        "log": sb.order_log(),
        "card": canonical_json(body),
        "card_sha": card.certificate.scorecard_sha256,
        "reported": reported,
        "steps": steps,
        "card_obj": card,
        "sb": sb,
    }


def _same(spec: dict[str, Any], drive: Any, *, swallow: bool = True, consistent: bool = True) -> dict[str, Any]:
    """Runs `drive` on both steppings and asserts the episodes are identical; returns the `session` one."""
    a = _episode(spec, "replay", drive, swallow)
    b = _episode(spec, "session", drive, swallow)
    assert len(a["docs"]) == len(b["docs"])
    for k, (x, y) in enumerate(zip(a["docs"], b["docs"], strict=True)):
        assert x == y, f"document {k} differs"
    assert [e["hash"] for e in a["log"]["entries"]] == [e["hash"] for e in b["log"]["entries"]]
    assert a["log"]["root"] == b["log"]["root"]
    assert a["reported"] == b["reported"]
    assert a["steps"] == b["steps"]
    assert a["card"] == b["card"] and a["card_sha"] == b["card_sha"]
    assert b["card_obj"].consistency.equal is consistent
    assert a["sb"].window_replays == b["sb"].window_replays
    return b


# ---- random agents on the synthetic fixtures ---------------------------------------------------------------------

CASES = [
    # dataset, instrument, date, start, end, latency, jitter, feed latency, seed
    (SYNTH, 1, "2026-06-15", "13:31", "13:45", "250us", "500us", 0, 1),
    (SYNTH, 2, "2026-06-16", "16:00", "16:20", "2ms", 0, "300ms", 2),
    (SYNTH, 1, "2026-06-16", "13:35", "14:05", "1ms", "2ms", "20ms", 5),
    (SYNTH_NASDAQ, 1, "2026-06-15", "14:10", "14:25", "100us", "1ms", 0, 3),
    (SYNTH_NASDAQ, 2, "2026-06-16", "15:00", "15:15", 0, 0, "50ms", 4),
    (SYNTH_NASDAQ, 1, "2026-06-16", "13:30", "14:00", "500us", 0, "5ms", 6),
]


@pytest.mark.parametrize("case", CASES, ids=[f"{c[0].name}-{c[1]}-{c[2]}-{c[-1]}" for c in CASES])
def test_random_agents_see_the_same_episode_on_both_steppings(case: tuple) -> None:
    ds, iid, date, start, end, lat, jit, feed, seed = case
    spec = {
        "dataset": {"path": str(ds)},
        "instrument_id": iid,
        "session_date": date,
        "start": start,
        "end": end,
        "latency": lat,
        "jitter": jit,
        "feed_latency": feed,
        "seed": seed,
        "depth_levels": 3,
        "markout_horizons": ["1s", "5s"],
        "fees": {"maker_per_unit": -0.2, "taker_per_unit": 0.3, "taker_bps": 0.5},
    }
    run = _same(spec, lambda sb: random_agent(sb, random.Random(seed), steps=120), swallow=False)
    card = run["card_obj"]
    assert card.consistency.fills_reported > 0
    assert len(run["docs"]) > 120


# ---- scripted agents on hand-made datasets ------------------------------------------------------------------------


def _book(t: Tape) -> tuple[int, int]:
    a = t.add(at(1), "B", px(0), 5)
    b = t.add(at(1), "B", px(-1), 5)
    t.add(at(1), "A", px(5), 5)
    t.add(at(1), "A", px(6), 5)
    return a, b


def test_a_pending_order_arriving_in_a_halt_ends_at_the_same_step(tmp_path: Path) -> None:
    """A replay of a prefix ends a pending order whose arrival falls in a halt listed at the cut `censored_gap` at
    its arrival, even before any event after that arrival: the session view must do the same at the same step."""
    t = Tape()
    _book(t)
    for k in (11, 12, 13, 16, 30, 31, 32):
        t.noop(at(k))
    halts = [{"instrument_id": 1, "ts_start": at(15.5), "ts_end": at(25), "state": "halted", "reason": "test"}]
    ds = write_dataset(tmp_path / "ds", t, trading_halts=halts)

    def drive(sb: Any) -> None:
        sb.observe()
        sb.advance(to=at(12))
        sb.submit("buy", 1, price=px(0))  # rests before the halt: censored at its start
        sb.advance(to=at(15))
        sb.submit("sell", 2, price=px(7), client_order_id="in-halt")  # arrives at 17, inside the halt
        sb.advance(to=at(17.5))  # the event at 16 is new, none after the arrival yet
        sb.advance(to=at(20))
        sb.submit("buy", 1, price=px(-1))  # arrives in the halt, decided only at the event at 30
        sb.advance(to=at(31))
        sb.advance(to=at(33))

    run = _same(base_spec(ds, start=at(10), end=at(35), latency="2s"), drive, consistent=False)
    orders = {o.order_id: o for o in run["card_obj"].orders}
    assert orders["o2"].status == "censored" and orders["o2"].ts_done == at(17)
    assert run["sb"].orders[1].final_step == 3  # shown ended at the step to 17.5, before any later event
    # Both steppings show o2 ended one step before the certification's status check expects it (that check dates
    # the end of a halt-censored pending order at the first event after its arrival, here 30). This predates the
    # session stepping, which reproduces it exactly; the fills and the end itself agree.
    c = run["card_obj"].consistency
    assert (c.statuses_mismatched, c.fills_matched, c.fills_reported) == (1, c.fills_reported, c.fills_reported)


def _straddling(root: Path) -> Path:
    """A dataset with one event whose records carry two times: the bid at 100.00 is cancelled at 20 s and a new bid
    added at 22 s, `F_LAST` on the second."""
    t = Tape()
    a, _ = _book(t)
    t.noop(at(11))
    recs = [t._rec(at(20), "C", "B", px(0), 5, a), t._rec(at(22), "A", "B", px(0), 3, 7777)]
    recs[-1]["flags"] |= LAST
    t.events.append(recs)
    t.resting.pop(a)
    t.resting[7777] = ("B", px(0), 3)
    for k in (25, 26, 27):
        t.noop(at(k))
    t.trade(at(28), "A", [(7777, 3)])
    t.noop(at(29))
    return write_dataset(root, t)


def test_an_event_straddling_the_cut_waits_whole(tmp_path: Path) -> None:
    """An event whose first record is before the cut and whose `F_LAST` is after it is released at once, by the tape
    and by the session alike: an order arriving before its first record goes live at the step that covers its end."""
    ds = _straddling(tmp_path / "ds")

    def drive(sb: Any) -> None:
        sb.observe()
        sb.advance(to=at(19))
        sb.submit("buy", 2, price=px(0))  # arrives at 19.5, before the straddling event
        sb.submit("buy", 1, peg_offset_ticks=0)
        sb.advance(to=at(21))  # cut inside the event: still pending
        sb.advance(to=at(23))  # the whole event is in: working
        sb.cancel("o2", qty=None)
        sb.advance(to=at(30))

    # The certification's status check dates an event by its first record, exact only when an event's records share
    # one time; it says so (`timestamps_monotone` false) and flags both orders. Fills and every document agree.
    run = _same(base_spec(ds, start=at(10), end=at(31), latency="500ms"), drive, consistent=False)
    sb = run["sb"]
    assert sb.orders[0].working_step == 3
    c = run["card_obj"].consistency
    assert not c.timestamps_monotone and c.fills_matched == c.fills_reported


def test_an_unterminated_tail_and_orders_after_the_data(tmp_path: Path) -> None:
    """The data ends inside an event (no `F_LAST`): once the cut passes the end, the tail is applied and ended (here
    it trades through the order's price, a fill), then orders and cancels sent after the data still answer the same
    way, and orders open at the episode end end as the final replay ends them."""
    t = Tape()
    a, b = _book(t)
    for k in (11, 12, 13):
        t.noop(at(k))
    t.trade(at(20), "A", [(a, 5), (b, 5)])
    t.events[-1][-1]["flags"] &= ~LAST
    ds = write_dataset(tmp_path / "ds", t)

    def drive(sb: Any) -> None:
        sb.observe()
        sb.advance(to=at(12))
        sb.submit("buy", 2, price=px(0))
        sb.submit("buy", 3, price=px(-1))
        sb.submit("sell", 1, price=px(9))
        sb.advance(to=at(15))
        sb.cancel("o2", qty=1)
        sb.advance(to=at(21))  # past the end of the data: the tail goes in
        sb.submit("sell", 1, price=px(8))  # after the data: never goes live
        sb.cancel("o3")
        sb.advance(to=at(25))
        sb.submit("buy", 1, type="market")
        sb.advance(to=at(30))

    run = _same(base_spec(ds, start=at(10), end=at(40), latency="1ms"), drive)
    fills = run["card_obj"].fills
    assert {f.order_id for f in fills} >= {"o1"}
    assert any(f.step == 3 for f in fills)  # reported at the step whose cut passed the end of the data


def test_book_clear_feed_gap_rejections_and_open_orders_at_the_end(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "A", px(5), 5)  # no bid: a pegged buy is rejected (no touch)
    for k in (11, 12):
        t.noop(at(k))
    b = t.add(at(13), "B", px(0), 4)
    t.noop(at(14))
    t.clear(at(16))
    t.add(at(17), "B", px(0), 4)
    t.add(at(17), "A", px(4), 4)
    for k in (18, 19, 22, 23, 24):
        t.noop(at(k))
    gaps = [{"instrument_id": 1, "ts_start": at(20), "ts_end": at(21), "reason": "test gap"}]
    ds = write_dataset(tmp_path / "ds", t, feed_gaps=gaps)
    del b

    def drive(sb: Any) -> None:
        sb.observe()
        sb.submit("buy", 1, peg_offset_ticks=0)
        sb.advance(to=at(12.5))
        sb.submit("buy", 2, price=px(0))
        sb.advance(to=at(15))
        sb.advance(to=at(17.5))
        sb.submit("buy", 2, price=px(0))
        sb.submit("sell", 1, peg_offset_ticks=1)  # would cross the bid: rejected at arrival
        sb.submit("sell", 1, type="market", tif="ioc")
        sb.advance(to=at(19.5))
        sb.submit("sell", 3, price=px(4))
        sb.advance(to=at(22.5))
        sb.submit("buy", 1, price=px(-2))
        sb.advance(to=at(23.5))

    run = _same(base_spec(ds, start=at(10), end=at(30), latency="1ms"), drive)
    statuses = {(o.status, o.reason) for o in run["card_obj"].orders}
    assert ("censored", "book_clear") in statuses and ("censored", "feed_gap") in statuses
    assert ("rejected", "rejected_no_touch") in statuses
    assert any(s == "cancelled" and r == "finish_cancel" for s, r in statuses)


def test_an_episode_across_midnight(tmp_path: Path) -> None:
    """Orders sent after the session day's midnight belong to no partition: `replay` takes them and ends them
    `censored_gap` at their arrival without replaying them, and the session stepping does the same (it used to refuse
    them, unlogged). A log made with `replay` stepping verifies (`verify` re-executes with the default stepping)."""
    from queuetruth.sandbox import verify

    t = Tape()
    t.add(at(86_380), "B", px(0), 5)
    t.add(at(86_380), "A", px(5), 5)
    for k in (86_385, 86_390, 86_395):
        t.noop(at(k))
    ds = write_dataset(tmp_path / "ds", t)

    def drive(sb: Any) -> None:
        sb.observe()
        sb.submit("buy", 1, price=px(0))
        sb.advance(to=at(86_388))
        sb.advance(to=at(86_405))  # past midnight
        sb.submit("sell", 2, price=px(9))  # no partition covers it
        sb.cancel("o1")
        sb.submit("sell", 1, type="market")
        sb.advance(to=at(86_415))
        sb.cancel("o2", qty=1)
        sb.advance(to=at(86_425))

    spec = base_spec(ds, start=at(86_382), end="24:00:30", latency="1ms")
    run = _same(spec, drive, swallow=False)
    orders = {o.order_id: o for o in run["card_obj"].orders}
    assert orders["o2"].status == "censored" and orders["o2"].reason == "feed_gap"
    assert orders["o2"].ts_done == at(86_405) + 1_000_000
    assert not any('"error"' in d for d in run["docs"])
    with Sandbox.open(spec, stepping="replay") as sb:
        drive(Recorder(sb, swallow=False))
        sb.finish(out=tmp_path / "out")
    v = verify(tmp_path / "out" / "order_log.json", scorecard=tmp_path / "out" / "scorecard.json", workdir=tmp_path)
    assert v.ok, v.problems


def test_mixing_whole_event_and_record_advances(tmp_path: Path) -> None:
    """`advance_events_to` holds the records of an event not complete at the cut; a later `advance_to`, even to the
    same time, applies them first, so the position again means every record at or before it was applied."""
    ds = _straddling(tmp_path / "ds")
    spec = {"dataset": {"path": str(ds)}, "bbo": "none", "trajectory": "none"}
    ref = queuetruth.api.Session.with_orders(spec)
    ref.advance_to(at(21))
    s = queuetruth.api.Session.with_orders(spec)
    s.advance_events_to(at(21))
    assert s.position == at(21)
    assert s.bbo(1)["bid"] == (px(0), 5, 1)  # the cancel at 20 s waits with its event
    s.advance_to(at(21))
    assert s.depth(1, 5) == ref.depth(1, 5) and s.bbo(1)["bid"] == (px(-1), 5, 1)
    s.advance_events_to(at(23))
    ref.advance_to(at(23))
    assert s.depth(1, 5) == ref.depth(1, 5) and s.bbo(1)["bid"] == (px(0), 3, 1)
    s.finish()
    ref.finish()


# ---- what the session stepping reads -----------------------------------------------------------------------------


def test_session_stepping_replays_nothing_until_finish_and_writes_no_tape(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = queuetruth.api.replay

    def spy(spec: Any, **kw: Any) -> Any:
        calls.append(str(spec["dataset"]["path"]))
        return real(spec, **kw)

    monkeypatch.setattr(queuetruth.api, "replay", spy)
    spec = {
        "dataset": {"path": str(SYNTH)},
        "instrument_id": 1,
        "session_date": "2026-06-15",
        "start": "13:32",
        "end": "13:44",
        "latency": "500us",
        "seed": 7,
    }
    sb = Sandbox.open(spec)
    assert sb.stepping == "session"
    random_agent(sb, random.Random(7), steps=40)
    assert sb.orders and sb.window_replays > 0 and calls == []
    assert not (sb.workdir / "tape").exists() or not any((sb.workdir / "tape").rglob("*.parquet"))
    sb.finish()
    assert len(calls) == 1  # the certification replay


def test_the_session_engine_holds_exactly_the_released_records() -> None:
    """After every step the session engine's book equals the sandbox's own book of the records the tape released:
    the engine applied nothing past the cut (and nothing of an event not complete by it)."""
    spec = {
        "dataset": {"path": str(SYNTH_NASDAQ)},
        "instrument_id": 1,
        "session_date": "2026-06-15",
        "start": "10:02",
        "end": "10:14",
        "latency": "500us",
        "feed_latency": "200ms",
        "seed": 11,
    }
    sb = Sandbox.open(spec)
    rng = random.Random(5)
    for _ in range(60):
        random_agent(sb, rng, steps=1, step_ns=(100 * MS, 3_000 * MS))
        assert sb._qs.position == sb.visible_ns
        bids, asks = sb._qs.depth(1, 10)
        assert (list(bids), list(asks)) == tuple(list(x) for x in sb.book.depth(10))
    sb.close()


def test_stepping_is_validated() -> None:
    with pytest.raises(QtError) as e:
        Sandbox.open({"dataset": {"path": str(SYNTH)}, "instrument_id": 1, "session_date": "2026-06-15"}, stepping="x")
    assert e.value.code == "QT-CONFIG-001"
