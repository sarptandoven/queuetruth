"""No look-ahead, enforced structurally and checked from outside.

1. Structure: after every step the scratch dataset (the only input of every replay the session runs) holds exactly
   the source records of the cut at the visible time, its source prints and feed gaps end there too (a gap is listed
   once it has started, its end clamped to the cut), and every replay of the session reads the scratch dataset, never
   the original files.
2. Behaviour: two datasets that agree up to `T*` and differ after it give byte-identical observations, acks and log
   entries for every step before `T*` to an agent whose decisions depend on what it sees; the difference shows
   after `T*`. A future price spike never appears in anything the agent receives before it.
3. Interface: instructions carry the agent's clock and nothing else in time; nothing fills from an event at or
   before the decision; markouts appear only once their horizon is visible; with feed latency the agent sees the
   market exactly `feed_latency_ns` late.
"""

from __future__ import annotations

import inspect
import json
import random
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import queuetruth.api
from queuetruth.sandbox import Sandbox
from queuetruth.sandbox.tape import GAP_IN_PROGRESS
from queuetruth.schemas.io import write_table
from queuetruth.schemas.runid import canonical_json
from sandbox.sbx_helpers import (
    MS,
    SYNTH,
    SYNTH_NASDAQ,
    Tape,
    at,
    base_spec,
    prefix_rows,
    px,
    random_agent,
    write_dataset,
)

pytestmark = pytest.mark.requires_core

SPEC = {
    "dataset": {"path": str(SYNTH)},
    "instrument_id": 1,
    "session_date": "2026-06-15",
    "start": "13:32",
    "end": "13:44",
    "latency": "500us",
    "feed_latency": "200ms",
    "seed": 11,
}
DAY = 20619


def _tape_state(sb: Sandbox) -> tuple[int, int | None, int | None]:
    t = sb.tape.root
    files = [t / f["path"] for p in json.loads((t / "manifest.json").read_text())["partitions"] for f in p["files"]]
    rows = 0
    ts_max = None
    for f in files:
        ts = pq.read_table(f, columns=["ts_event"]).column("ts_event").cast(pa.int64()).to_numpy()
        rows += len(ts)
        ts_max = int(ts.max()) if ts_max is None else max(ts_max, int(ts.max()))
    pr_max = None
    for f in sorted((t / "trades_source").rglob("part-*.parquet")):
        ts = pq.read_table(f, columns=["ts_event"]).column("ts_event").cast(pa.int64()).to_numpy()
        if len(ts):
            pr_max = int(ts.max()) if pr_max is None else max(pr_max, int(ts.max()))
    return rows, ts_max, pr_max


def test_the_tape_holds_exactly_the_cut_and_every_replay_reads_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = queuetruth.api.replay

    def spy(spec: Any, **kw: Any) -> Any:
        calls.append(str(spec["dataset"]["path"]))
        return real(spec, **kw)

    monkeypatch.setattr(queuetruth.api, "replay", spy)
    sb = Sandbox.open(SPEC)
    obs = [sb.observe()]
    rng = random.Random(3)
    for _ in range(40):
        obs.extend(random_agent(sb, rng, steps=1)[1:])
        rows, ts_max, pr_max = _tape_state(sb)
        cut = sb.visible_ns
        assert cut == sb.clock_ns - 200 * MS
        assert rows == prefix_rows(SYNTH, 1, DAY, cut) == sb.tape.rows
        assert ts_max is None or ts_max <= cut
        assert pr_max is None or pr_max <= cut
        assert all(o.visible_ns <= o.clock_ns for o in obs)
    in_session = list(calls)
    assert in_session and set(in_session) == {str(sb.tape.root)}
    sb.finish()
    assert len(calls) == len(in_session) + 1 and calls[-1] != str(sb.tape.root)  # only the final replay reads more


def test_feed_gaps_enter_the_tape_once_started_and_end_at_the_cut(tmp_path: Path) -> None:
    """The engine acts on a gap at the first event after its start, so the tape lists a gap once it has started (a
    replay of the prefix then censors at the same event as the full replay); its end and reason never carry a time
    past the cut."""
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    for k in range(11, 20):
        t.noop(at(k))
    gaps = [
        {"instrument_id": 1, "ts_start": at(13), "ts_end": at(15), "reason": "sequence gap 108 -> 110"},
        {"instrument_id": 2, "ts_start": at(11), "ts_end": at(12), "reason": "another instrument"},
    ]
    ds = write_dataset(tmp_path / "ds", t, feed_gaps=gaps)
    with Sandbox.open(base_spec(ds, start=at(10), end=at(19))) as sb:
        for to, want in (
            (12, []),
            (13, []),
            (14.9, [(at(13), at(14.9), GAP_IN_PROGRESS)]),
            (15, [(at(13), at(15), "sequence gap 108 -> 110")]),
            (18, [(at(13), at(15), "sequence gap 108 -> 110")]),
        ):
            sb.advance(to=at(to))
            dq = json.loads((sb.tape.root / "data_quality.json").read_text())
            assert [(g["ts_start"], g["ts_end"], g["reason"]) for g in dq["feed_gaps"]] == want, to
            assert all(g["ts_end"] <= sb.visible_ns for g in dq["feed_gaps"])
            assert dq["records"] == 0 and dq["verdict"] == "clean"


def _perturbed(src: Path, out: Path, iid: int, day: int, t_star: int) -> Path:
    """A copy of the dataset whose records of one partition after `t_star` are changed: every price moved 4 ticks
    up, every add doubled. Event structure, ids and everything up to `t_star` unchanged."""
    shutil.copytree(src, out)
    man = json.loads((out / "manifest.json").read_text())
    p = next(x for x in man["partitions"] if x["instrument_id"] == iid and x["session_date"] == day)
    f = out / p["files"][0]["path"]
    tab = pq.read_table(f)
    ts = tab.column("ts_event").cast(pa.int64()).to_numpy()
    after = ts > t_star
    tick = int(pq.read_table(out / "instruments.parquet").column("tick_size")[0].as_py())
    price = tab.column("price").to_numpy().copy()
    undef = price == 2**63 - 1
    price[after & ~undef] += 4 * tick
    size = tab.column("size").to_numpy().copy()
    adds = tab.column("action").to_numpy() == ord("A")
    size[after & adds] *= 2
    tab = tab.set_column(tab.schema.get_field_index("price"), "price", pa.array(price, pa.int64()))
    tab = tab.set_column(tab.schema.get_field_index("size"), "size", pa.array(size, pa.uint64()))
    f.unlink()
    write_table(tab, f, tab.schema)
    return out


@pytest.mark.parametrize(
    ("dataset", "date", "start", "end"),
    [(SYNTH, "2026-06-15", "13:32", "13:44"), (SYNTH_NASDAQ, "2026-06-15", "10:02", "10:14")],
)
def test_the_future_cannot_change_what_the_agent_saw(
    tmp_path: Path, dataset: Path, date: str, start: str, end: str
) -> None:
    spec = {**SPEC, "dataset": {"path": str(dataset)}, "session_date": date, "start": start, "end": end}
    probe = Sandbox.open(spec)
    t0 = probe.start_ns
    probe.close()
    t_star = t0 + 6 * 60 * 1_000_000_000
    day = (np.datetime64(date) - np.datetime64("1970-01-01")).astype(int)
    alt = _perturbed(dataset, tmp_path / "alt", 1, int(day), t_star)
    runs = []
    for ds in (dataset, alt):
        sb = Sandbox.open({**spec, "dataset": {"path": str(ds)}})
        seen = random_agent(sb, random.Random(17), steps=200, step_ns=(500 * MS, 9_000 * MS))
        runs.append(([o.model_dump(mode="json") for o in seen], sb.order_log()["entries"]))
        sb.close()
    (obs_a, log_a), (obs_b, log_b) = runs
    before = [k for k, o in enumerate(obs_a) if o["clock_ns"] < t_star]
    assert len(before) >= 10
    for k in before:
        assert canonical_json(obs_a[k]) == canonical_json(obs_b[k]), k
    early = [e for e in log_a if e["clock_ns"] < t_star]
    assert [e["hash"] for e in early] == [e["hash"] for e in log_b[: len(early)]]
    # the change is real: after t_star the two runs see different markets
    assert any(
        canonical_json(a) != canonical_json(b) for a, b in zip(obs_a[len(before) :], obs_b[len(before) :], strict=False)
    )


def test_a_future_price_spike_never_reaches_the_agent_early(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    for k in range(11, 40):
        t.noop(at(k))
    t.add(at(40), "A", px(-77), 1)  # a crossing ask at 99.23 after the spike time
    s = t.add(at(40.5), "B", px(777), 9)  # a bid at 107.77
    t.cancel(at(41), s)
    t.noop(at(50))
    ds = write_dataset(tmp_path / "ds", t)
    marks = (str(px(-77)), str(px(777)), "99.23", "107.77")
    with Sandbox.open(base_spec(ds, start=at(10), end=at(49), latency="1ms")) as sb:
        texts = [sb.session.model_dump_json()]
        rng = random.Random(1)
        while sb.clock_ns < at(39):
            for o in random_agent(sb, rng, steps=1, step_ns=(300 * MS, 2_000 * MS)):
                if o.clock_ns < at(40):
                    texts.append(o.model_dump_json())
            texts.append(json.dumps(sb.order_log()))
        assert not any(m in txt for txt in texts for m in marks)
        obs = sb.advance(to=at(41.5))
        assert any(m in obs.model_dump_json() for m in marks[:2])


def test_instructions_carry_only_the_clock() -> None:
    params = set(inspect.signature(Sandbox.submit).parameters) | set(inspect.signature(Sandbox.cancel).parameters)
    assert not {p for p in params if "ts" in p or "time" in p or "clock" in p}
    sb = Sandbox.open(SPEC)
    seen = random_agent(sb, random.Random(2), steps=40)
    card = sb.finish()
    entries = sb.order_log()["entries"]
    clocks = [e["clock_ns"] for e in entries]
    assert clocks == sorted(clocks)
    assert all(e["visible_ns"] == e["clock_ns"] - 200 * MS for e in entries)
    submits = {e["data"]["order_id"]: e for e in entries if e["kind"] == "submit"}
    for o in card.orders:
        e = submits[o.order_id]
        assert o.ts_submit == e["clock_ns"]
        if o.ts_activated is not None:  # strict activation: the first event after arrival
            assert o.ts_activated > e["clock_ns"] + e["data"]["latency_ns"]
    for f in card.fills:
        assert f.ts_known > submits[f.order_id]["clock_ns"]
        assert f.ts_event >= submits[f.order_id]["clock_ns"] + submits[f.order_id]["data"]["latency_ns"]
    assert seen


def test_markouts_appear_only_when_their_horizon_is_visible() -> None:
    sb = Sandbox.open({**SPEC, "markout_horizons": ["1s", "10s", "60s"]})
    seen = random_agent(sb, random.Random(4), steps=60)
    fills = {(f.order_id, f.fill_seq): f for o in seen for f in o.fills}
    n = 0
    for o in seen:
        for m in o.markouts:
            f = fills[(m.order_id, m.fill_seq)]
            assert f.ts_event + m.horizon_ns <= o.visible_ns
            n += 1
    assert n >= 10
    sb.close()


def test_feed_latency_shows_the_market_exactly_that_late() -> None:
    lagged = Sandbox.open({**SPEC, "feed_latency": "750ms"})
    prompt = Sandbox.open({**SPEC, "feed_latency": 0})
    for k in range(1, 30):
        a = lagged.advance(to=lagged.start_ns + k * 1_300 * MS)
        b = prompt.advance(to=lagged.start_ns + k * 1_300 * MS - 750 * MS)
        assert a.visible_ns == b.visible_ns == b.clock_ns
        assert a.book == b.book
        assert a.session_trades == b.session_trades
    lagged.close()
    prompt.close()
