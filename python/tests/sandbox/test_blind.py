"""Blind episodes (`queuetruth.sandbox.blind`): a full blind episode through the Python API and through the MCP tools
shows the agent nothing that names the instrument-day (the leak scan below walks every string and number it
received), the operator's scorecard equals the one of a non-blind episode with the same instructions mapped back,
the certificate records the blind commitment and verifies, and the pin, the book file and the transform behave."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import mcp.types as types
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from mcp import Client as McpClient

from queuetruth.errors import QtError
from queuetruth.mcp.sandbox_tools import SANDBOX_TOOL_NAMES, register_sandbox_tools
from queuetruth.mcp.server import build_server
from queuetruth.sandbox import Sandbox, render, verify
from queuetruth.sandbox.blind import (
    BASE_MAX_TICKS,
    BASE_MIN_TICKS,
    BlindSandbox,
    BlindTransform,
    EpisodeBook,
    check_reveal,
)
from queuetruth.sandbox.models import json_schemas
from sandbox.sbx_helpers import SYNTH

pytestmark = pytest.mark.requires_core

DATE = "2026-06-15"
SPEC: dict[str, Any] = {
    "dataset": {"path": str(SYNTH)},
    "instrument_id": 1,
    "session_date": DATE,
    "start": "13:35",
    "end": "13:50",
    "latency": "1ms",
    "jitter": "200us",
    "fees": {"maker_per_unit": -0.2, "taker_per_unit": 0.3},
    "markout_horizons": ["1s", "10s"],
}
SECRET = "5e" * 32
NOT_IGNORED = ("schema", "schema_version")


# ---- the leak scan ------------------------------------------------------------------------------------------------


class Hidden:
    """Everything of the fixture day an agent must not see: names, ids, absolute times and the true prices."""

    def __init__(self, sb: Sandbox) -> None:
        i = sb.instrument
        part = f"date={DATE}/instrument_id=1/part-00000.parquet"
        px = pq.read_table(SYNTH / "mbo" / part, columns=["price"]).column("price")
        px = pc.filter(px, pc.less(px, 2**62))
        trades = pq.read_table(SYNTH / "trades_source" / part, columns=["price"]).column("price")
        self.prices = set(px.to_pylist()) | set(trades.to_pylist())
        self.exp = i["price_exp"]
        real = [Decimal(p).scaleb(self.exp) for p in self.prices]
        # every true price the day had, and a margin: a float price in this band would give the level away
        self.lo, self.hi = float(min(real)) * 0.98, float(max(real)) * 1.02
        self.strings = [
            i["symbol"],
            i["venue"],
            i["session_tz"],
            DATE,
            DATE.replace("-", ""),
            "2026",
            sb.dataset_id,
            sb.spec_id,
            str(SYNTH),
            SYNTH.name,
            *(e["hash"] for e in sb.log.entries),
        ]
        self.midnight = i["midnight_utc_ns"]

    def problems(self, value: Any, where: str = "$") -> list[str]:
        out: list[str] = []
        if where.endswith(".request"):
            return out  # the agent's own request, echoed in its blind log: nothing it did not send
        if isinstance(value, dict):
            for k, v in value.items():
                out += self.problems(str(k), f"{where}.<key>")
                out += self.problems(v, f"{where}.{k}")
        elif isinstance(value, list | tuple):
            for k, v in enumerate(value):
                out += self.problems(v, f"{where}[{k}]")
        elif isinstance(value, bool) or value is None:
            pass
        elif isinstance(value, int):
            if abs(value) >= 10**15:
                out.append(f"{where}: {value} looks like an absolute timestamp")
            if value in self.prices:
                out.append(f"{where}: {value} is a true price of the day")
        elif isinstance(value, float):
            if self.lo <= abs(value) <= self.hi:
                out.append(f"{where}: {value} lies in the day's true price range")
        elif isinstance(value, str):
            out += self._text(value, where)
        return out

    def _text(self, text: str, where: str) -> list[str]:
        out = [
            f"{where}: names {s!r}"
            for s in self.strings
            if s and re.search(rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])", text, re.IGNORECASE)
        ]
        for m in re.finditer(r"(?<![\w.+-])([0-9]+(?:\.[0-9]+)?)(?!\w)", text):
            tok = m.group(1)
            if len(tok.replace(".", "")) >= 15:
                out.append(f"{where}: {tok} looks like an absolute timestamp")
            d = Decimal(tok)
            if "." in tok and self.lo <= float(d) <= self.hi:
                out.append(f"{where}: {tok} lies in the day's true price range")
            raw = d.scaleb(-self.exp)
            if raw == raw.to_integral_value() and int(raw) in self.prices and "." in tok:
                out.append(f"{where}: {tok} is a true price of the day")
            if "." not in tok and int(tok) in self.prices:
                out.append(f"{where}: {tok} is a true raw price of the day")
        for m in re.finditer(r"(.)?\b[0-9]{1,2}:[0-9]{2}:[0-9]{2}", text):
            if m.group(1) not in ("+", "-"):
                out.append(f"{where}: {m.group(0)!r} is a time of day, not a time since the open")
        return out


# ---- a scripted agent that records what it receives -----------------------------------------------------------


class Recorder:
    """Every value the agent receives (documents, error code, message, hint and details, texts)."""

    def __init__(self) -> None:
        self.seen: list[Any] = []
        self.instructions: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def call(self, sb: BlindSandbox, name: str, *args: Any, **kw: Any) -> Any:
        self.instructions.append((name, args, kw))
        try:
            out = getattr(sb, name)(*args, **kw)
        except QtError as e:
            self.seen.append({"code": e.code, "message": e.message, "hint": e.hint, "details": e.details})
            return None
        doc = out.model_dump(mode="json") if hasattr(out, "model_dump") else out
        self.seen.append(doc)
        return doc


def play(sb: BlindSandbox, rec: Recorder) -> None:
    """Exercises every instruction and error path; prices from the blind book only."""
    inst = render.blind_instrument(sb.instrument)
    rec.seen.append(sb.session.model_dump(mode="json"))
    rec.seen.append(render.blind_session_text(sb.session.model_dump(mode="json")))
    obs = rec.call(sb, "observe")
    tick = sb.instrument["tick_size"]
    bid, ask = obs["book"]["bid"][0], obs["book"]["ask"][0]
    rec.call(sb, "submit", "buy", 2, price=str(sb.to_price(bid)), client_order_id="a-1")
    rec.call(sb, "submit", "buy", 2, price=str(sb.to_price(bid)), client_order_id="a-1")  # duplicate
    rec.call(sb, "submit", "buy", 2, price=str(sb.to_price(bid - tick)), client_order_id="a-1")  # other request
    rec.call(sb, "submit", "sell", 3, peg_offset_ticks=-1)
    rec.call(sb, "submit", "buy", 1, type="market")
    rec.call(sb, "submit", "sell", 1, type="market")  # self-trade prevention
    rec.call(sb, "submit", "buy", 1, price=bid + 1)  # off the tick grid (raw integer)
    rec.call(sb, "submit", "buy", 1, price=str(sb.to_price(bid)) + "0000000001")  # finer than price_exp
    rec.call(sb, "submit", "buy", 1, price=2**62)  # out of range
    rec.call(sb, "submit", "buy", 1, price=5000.0)  # a float
    rec.call(sb, "submit", "buy", 1, price=str(sb.to_price(ask + 3 * tick)), tif="ioc")
    rec.call(sb, "cancel", "o9")  # unknown order
    for k in range(6):
        doc = rec.call(sb, "advance", by="20s")
        rec.seen.append(render.observation_text(doc, inst, sb.end_ns))
        working = [o for o in doc["orders"] if o["status"] == "working" and o["tif"] == "day"]
        if k == 1 and working:
            ack = rec.call(sb, "cancel", working[0]["order_id"], 1 if working[0]["qty"] > 1 else None)
            rec.seen.append(render.ack_text(ack, inst))
        if k == 2:
            b = doc["book"]["bid"][0]
            ack = rec.call(sb, "submit", "buy", 1, price=str(sb.to_price(b - 2 * tick)))
            rec.seen.append(render.ack_text(ack, inst))
    rec.call(sb, "advance", to="+00:04:00")  # before the clock
    rec.call(sb, "advance", to=sb.clock_ns + 30_000_000_000)
    rec.call(sb, "advance", to="+00:15:30")
    rec.call(sb, "observe")
    rec.seen.append(sb.order_log())
    fin = rec.call(sb, "finish")
    rec.seen.append(render.blind_finish_text(fin))
    rec.call(sb, "observe")  # finished


def test_a_blind_episode_shows_the_agent_nothing_of_the_instrument_day(tmp_path: Path) -> None:
    book = EpisodeBook()
    eid = book.pin(SPEC, secret=SECRET)
    rec = Recorder()
    sb = book.start(eid)
    play(sb, rec)
    hidden = Hidden(sb.inner)
    problems = [p for k, v in enumerate(rec.seen) for p in hidden.problems(v, f"[{k}]")]
    assert not problems, "\n".join(problems[:30])
    # the scan is not vacuous: the same scan of what a non-blind session returns finds the day everywhere
    assert hidden.problems(sb.inner.session.model_dump(mode="json"))
    assert hidden.problems(render.scorecard_text(sb.scorecard.model_dump(mode="json")))
    # and the episode exercised the paths it is meant to
    errors = [d for d in rec.seen if isinstance(d, dict) and "code" in d and "message" in d]
    assert {e["code"] for e in errors} >= {"QT-CONFIG-001", "QT-INPUT-003", "QT-INPUT-004", "QT-USAGE-001"}
    assert any(e["details"] == {"reason": "self_trade", "order_id": "o1"} for e in errors)
    grid = next(e for e in errors if "tick grid" in e["message"])
    assert str(sb.transform.p_back(int(grid["message"].split()[1]))) not in grid["message"]
    card = sb.scorecard
    assert card is not None and card.consistency.equal and card.diagnostics.rejected_instructions >= 6
    assert len(card.fills) >= 2


def instructions_mapped_back(rec: Recorder, tr: BlindTransform, exp: int) -> list[tuple[str, tuple, dict]]:
    """The agent's instructions with every price and time in true values."""
    out = []
    for name, args, kw in rec.instructions:
        kw = dict(kw)
        if name == "submit" and kw.get("price") is not None and not isinstance(kw["price"], float):
            p = kw["price"]
            try:
                raw = p if isinstance(p, int) else int(Decimal(p).scaleb(-exp))
                exact = isinstance(p, int) or Decimal(raw).scaleb(exp) == Decimal(p)
            except ArithmeticError:
                exact = False
            if exact:
                kw["price"] = tr.p_back(raw)
        if name == "advance" and kw.get("to") is not None:
            t = kw["to"]
            rel = t if isinstance(t, int) else None
            if rel is None:
                hh, mm, ss = (int(x) for x in t.lstrip("+").split(":"))
                rel = (hh * 3600 + mm * 60 + ss) * 1_000_000_000
            kw["to"] = tr.t_back(rel)
        out.append((name, args, kw))
    return out


def test_a_non_blind_episode_with_the_same_instructions_gives_the_same_scorecard(tmp_path: Path) -> None:
    book = EpisodeBook()
    eid = book.pin(SPEC, secret=SECRET)
    rec = Recorder()
    sb = book.start(eid)
    play(sb, rec)
    blind_card = sb.scorecard.model_dump(mode="json")
    plain = Sandbox.open(SPEC)
    for name, args, kw in instructions_mapped_back(rec, sb.transform, sb.instrument["price_exp"]):
        try:
            out = getattr(plain, name)(*args, **kw)
        except QtError:
            continue
        if name == "finish":
            card = out.model_dump(mode="json")
    skip = ("run_id", "certificate", "timings", "files")
    assert {k: v for k, v in card.items() if k not in skip} == {k: v for k, v in blind_card.items() if k not in skip}
    assert card["certificate"]["blind"] is None
    assert blind_card["certificate"]["blind"] == {
        "episode_id": eid,
        "transform_sha256": sb.transform.commitment(),
    }
    # the same orders, cancels, rejections and observations: every log entry but the open entry (which carries the
    # blind commitment) has the same content
    a, b = sb.inner.log.entries, plain.log.entries
    assert len(a) == len(b)
    for x, y in zip(a[1:], b[1:], strict=True):
        assert {k: x[k] for k in ("kind", "clock_ns", "visible_ns", "rows_seen", "data")} == {
            k: y[k] for k in ("kind", "clock_ns", "visible_ns", "rows_seen", "data")
        }
    assert {k: v for k, v in a[0]["data"].items() if k != "blind"} == b[0]["data"]


def test_the_blind_certificate_verifies_and_commits_to_the_transform(tmp_path: Path) -> None:
    book = EpisodeBook()
    eid = book.pin(SPEC, secret=SECRET)
    sb = book.start(eid)
    obs = sb.observe()
    sb.submit("buy", 1, price=str(sb.to_price(obs.book.bid[0])))
    sb.advance(by="1m")
    out = tmp_path / "ep"
    fin = sb.finish(out=out)
    card = json.loads((out / "scorecard.json").read_text())
    log = json.loads((out / "order_log.json").read_text())
    assert log["entries"][0]["data"]["blind"] == card["certificate"]["blind"]
    v = verify(log, scorecard=card)
    assert v.ok, v.problems
    # the reveal the operator keeps opens the commitment; another transform does not
    reveal = json.loads((out / "blind.json").read_text())["transform"]
    assert check_reveal(reveal, card["certificate"]["blind"]["transform_sha256"])
    assert check_reveal(reveal, fin.transform_sha256)
    assert not check_reveal({**reveal, "price_offset_ticks": reveal["price_offset_ticks"] + 1}, fin.transform_sha256)
    # a certificate whose blind field was changed or dropped does not verify
    for blind in (None, {**card["certificate"]["blind"], "transform_sha256": "0" * 64}):
        bad = {**card, "certificate": {**card["certificate"], "blind": blind}}
        assert not verify(log, scorecard=bad).ok
    # nor does a log whose open entry lost its commitment (the chain breaks)
    tampered = json.loads(json.dumps(log))
    del tampered["entries"][0]["data"]["blind"]
    assert not verify(tampered).ok


def test_the_transform_keeps_ticks_and_maps_back_exactly() -> None:
    book = EpisodeBook()
    eid = book.pin(SPEC, secret=SECRET)
    with book.start(eid) as sb:
        tr = sb.transform
        true_bid = sb.inner.observe().book.bid[0]
        blind_bid = sb.observe().book.bid[0]
        assert blind_bid == true_bid + tr.price_offset_ticks * tr.tick_size and tr.p_back(blind_bid) == true_bid
        assert BASE_MIN_TICKS <= blind_bid // tr.tick_size <= BASE_MAX_TICKS
        assert blind_bid % sb.instrument["tick_size"] == true_bid % sb.instrument["tick_size"]
        assert sb.observe().clock_ns == sb.inner.clock_ns - sb.inner.instrument["open_ns"]
        assert sb.observe().time_local == "+00:05:00.000000"
    # the same secret gives the same transform; another one another level
    again = EpisodeBook()
    again.pin(SPEC, secret=SECRET, episode_id=eid)
    with again.start(eid) as sb2:
        assert sb2.transform.reveal() == tr.reveal()
        assert sb2.session.model_dump(mode="json") == sb.session.model_dump(mode="json")
    other = EpisodeBook()
    e2 = other.pin(SPEC, secret="77" * 32)
    with other.start(e2) as sb3:
        assert sb3.transform.price_offset_ticks != tr.price_offset_ticks
        assert sb3.transform.commitment() != tr.commitment()


def test_pins_and_the_book_file(tmp_path: Path) -> None:
    book = EpisodeBook()
    with pytest.raises(QtError, match="basis points"):
        book.pin({**SPEC, "fees": {"taker_bps": 0.5}})
    with pytest.raises(QtError):
        book.pin({**SPEC, "session_date": "2030-01-01"})
    eid = book.pin(SPEC)
    assert re.fullmatch(r"ep_[0-9a-f]{16}", eid) and book.episode_ids == [eid]
    with pytest.raises(QtError, match="already pinned"):
        book.pin(SPEC, episode_id=eid)
    p = book.save(tmp_path / "episodes.json")
    assert p.stat().st_mode & 0o777 == 0o600
    loaded = EpisodeBook.load(p)
    assert loaded.pins == book.pins
    with pytest.raises(QtError) as e:
        loaded.start("ep_0000000000000000")
    assert e.value.code == "QT-USAGE-001" and eid not in e.value.message


# ---- through the MCP tools ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_blind_episode_through_the_mcp_tools(tmp_path: Path) -> None:
    book = EpisodeBook()
    eid = book.pin(SPEC, secret=SECRET)
    out = tmp_path / "out"
    app = build_server(local=True, root=SYNTH.parent)
    tools = register_sandbox_tools(app, episodes=book, out_dir=out)
    schemas = json_schemas()
    seen: list[Any] = []

    def record(res: types.CallToolResult) -> types.CallToolResult:
        seen.append(res.structured_content)
        for b in res.content:
            if isinstance(b, types.TextContent):
                seen.append(b.text)
            elif isinstance(b, types.EmbeddedResource):
                seen.append(json.loads(b.resource.text))
            else:
                seen.append(b.model_dump(mode="json"))
        return res

    async with McpClient(app.server) as c:
        listed = (await c.list_tools()).tools
        seen.append(c.instructions)
        seen.extend(t.model_dump(mode="json") for t in listed)
        assert [t.name for t in listed] == list(SANDBOX_TOOL_NAMES)  # the server's own tools are gone

        async def call(name: str, args: dict[str, Any], *, error: bool = False) -> types.CallToolResult:
            res = record(await c.call_tool(name, args))
            assert res.is_error is error, res.content
            if not error:
                schema = schemas[res.structured_content["schema"]]
                assert next(t for t in listed if t.name == name).output_schema == schema
            return res

        await call("qt_sandbox_start", {"dataset_path": "synth_small", "instrument_id": 1}, error=True)
        await call("qt_sandbox_start", {"episode_id": "ep_0000000000000000"}, error=True)
        start = await call("qt_sandbox_start", {"episode_id": eid})
        sid = start.structured_content["session_id"]
        tick = start.structured_content["instrument"]["tick_size"]
        bid = start.structured_content["observation"]["book"]["bid"][0]
        to_px = tools.sessions[sid].to_price
        await call("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 2, "price": str(to_px(bid))})
        await call("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "type": "market"})
        await call("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "peg_offset_ticks": -1})
        await call("qt_sandbox_submit", {"session_id": sid, "side": "sell", "qty": 1, "type": "market"}, error=True)
        off = str(to_px(bid) + to_px(1))
        await call("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "price": off}, error=True)
        await call("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "price": "1e3"}, error=True)
        obs = await call("qt_sandbox_advance", {"session_id": sid, "by": "30s"})
        working = [o for o in obs.structured_content["orders"] if o["status"] == "working"]
        if working:
            await call("qt_sandbox_cancel", {"session_id": sid, "order_id": working[0]["order_id"]})
        await call("qt_sandbox_cancel", {"session_id": sid, "order_id": "o99"}, error=True)
        await call("qt_sandbox_observe", {"session_id": sid})
        await call("qt_sandbox_advance", {"session_id": sid, "to": "+00:04:00"}, error=True)
        await call("qt_sandbox_list", {})
        low = str(to_px(bid - 4 * tick))
        b2 = await call("qt_sandbox_submit", {"session_id": sid, "side": "buy", "qty": 1, "price": low})
        assert b2.structured_content["order"]["price"] == bid - 4 * tick
        await call("qt_sandbox_advance", {"session_id": sid, "to": "+00:12:00"})
        fin = await call("qt_sandbox_finish", {"session_id": sid, "include_log": True})
        assert fin.structured_content["schema"] == "qt.sandbox_blind_finish"
        assert not any(isinstance(b, types.ResourceLink) for b in fin.content)
        await call("qt_sandbox_observe", {"session_id": sid}, error=True)
        s2 = (await call("qt_sandbox_start", {"episode_id": eid})).structured_content["session_id"]
        await call("qt_sandbox_close", {"session_id": s2})

    plain = Sandbox.open(SPEC)
    hidden = Hidden(plain)
    plain.close()
    problems = [p for k, v in enumerate(seen) for p in hidden.problems(v, f"[{k}]")]
    assert not problems, "\n".join(problems[:30])
    # the operator's files: true values, a blind certificate, and they verify
    card = json.loads((out / sid / "scorecard.json").read_text())
    assert card["instrument"]["symbol"] == plain.instrument["symbol"]
    assert card["certificate"]["blind"]["episode_id"] == eid
    assert verify(out / sid / "order_log.json", scorecard=card).ok
