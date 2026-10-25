"""The committed order log and `verify`: the hash chain, the header the open entry commits to, deterministic
re-execution of every entry (instructions, rejections of arguments of any Python type, steps, finish), the dataset
identity (manifest and file hashes), the scorecard digest, and what tampering is caught where."""

from __future__ import annotations

import copy
import json
import random
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from queuetruth.errors import QtError
from queuetruth.sandbox import Sandbox, verify
from queuetruth.sandbox.log import GENESIS, CommitLog, check_chain, entry_hash
from sandbox.sbx_helpers import SYNTH, random_agent

pytestmark = pytest.mark.requires_core

SPEC = {
    "dataset": {"path": str(SYNTH)},
    "instrument_id": 2,
    "session_date": "2026-06-16",
    "start": "14:00",
    "end": "14:10",
    "latency": "400us",
    "jitter": "200us",
    "feed_latency": "20ms",
    "seed": 5,
    "fees": {"taker_bps": 0.4},
}


@pytest.fixture(scope="module")
def episode(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], dict[str, Any], Path]:
    out = tmp_path_factory.mktemp("ep")
    with Sandbox.open(SPEC) as sb:
        random_agent(sb, random.Random(8), steps=60)
        try:
            sb.submit("buy", 1, price="1.001")  # rejected (off the grid): rejections are logged and re-executed
        except QtError:
            pass
        card = sb.finish(out=out)
    log = json.loads((out / "order_log.json").read_text())
    return log, card.model_dump(mode="json"), out


def _rehash(log: dict[str, Any]) -> dict[str, Any]:
    """Recomputes every hash and link after an edit (what a forger would do)."""
    prev = GENESIS
    for e in log["entries"]:
        e["prev"] = prev
        e["hash"] = entry_hash(e)
        prev = e["hash"]
    log["root"] = prev
    return log


def test_the_log_records_every_instruction_with_its_information_set(episode: tuple) -> None:
    log, card, _ = episode
    kinds = [e["kind"] for e in log["entries"]]
    assert kinds[0] == "open" and kinds[-1] == "finish" and "reject" in kinds and "advance" in kinds
    assert check_chain(log) == [] and log["root"] == card["certificate"]["log_root"]
    assert [e["seq"] for e in log["entries"]] == list(range(len(kinds)))
    rows = [e["rows_seen"] for e in log["entries"]]
    assert rows == sorted(rows)
    for e in log["entries"]:
        if e["kind"] == "advance":
            assert len(e["data"]["observation_sha256"]) == 64
        if e["kind"] == "submit":
            assert e["data"]["order_id"].startswith("o") and e["data"]["latency_ns"] >= 400_000


def test_verify_re_executes_the_episode(episode: tuple, tmp_path: Path) -> None:
    log, card, out = episode
    v = verify(out / "order_log.json", scorecard=out / "scorecard.json", workdir=tmp_path)
    assert v.ok and v.chain_ok and v.header_ok and v.dataset_ok and v.reexecution_ok and v.scorecard_ok
    assert v.run_id == card["run_id"] and v.first_divergence is None and v.problems == []
    # a moved dataset verifies too: identity is the dataset_id, not the path
    moved = tmp_path / "moved"
    shutil.copytree(SYNTH, moved)
    assert verify(log, dataset=moved, scorecard=card).ok


def test_editing_an_entry_breaks_the_chain(episode: tuple) -> None:
    log = copy.deepcopy(episode[0])
    k = next(i for i, e in enumerate(log["entries"]) if e["kind"] == "submit")
    log["entries"][k]["data"]["request"]["qty"] += 1
    problems = check_chain(log)
    assert problems and f"entry {k}: hash" in problems[0]
    v = verify(log)
    assert not v.ok and not v.chain_ok


@pytest.mark.parametrize("target", ["submit", "advance", "cancel"])
def test_a_rehashed_forgery_fails_re_execution_at_the_forged_entry(episode: tuple, target: str) -> None:
    log = copy.deepcopy(episode[0])
    k = next(i for i, e in enumerate(log["entries"]) if e["kind"] == target and e["data"].get("by", "agent") == "agent")
    e = log["entries"][k]
    if target == "submit":
        e["data"]["latency_ns"] -= 1  # claim a faster order entry than the spec gives
    elif target == "advance":
        e["data"]["observation_sha256"] = "0" * 64  # claim to have seen something else
    else:
        e["clock_ns"] -= 1_000  # backdate a cancel
    _rehash(log)
    assert check_chain(log) == []
    v = verify(log)
    assert v.chain_ok and v.dataset_ok and not v.reexecution_ok and not v.ok
    assert v.first_divergence is not None and v.first_divergence <= k


def test_another_dataset_or_another_scorecard_is_detected(episode: tuple, tmp_path: Path) -> None:
    log, card, _ = episode
    other = tmp_path / "other"
    shutil.copytree(SYNTH, other)
    man = json.loads((other / "manifest.json").read_text())
    man["dataset_id"] = "f" * 64
    (other / "manifest.json").write_text(json.dumps(man))
    v = verify(log, dataset=other)
    assert not v.dataset_ok and not v.ok and any("dataset_id" in p for p in v.problems)
    forged = copy.deepcopy(card)
    forged["pnl"]["pnl_net"] = 1e9
    v = verify(log, scorecard=forged)
    assert v.reexecution_ok and v.scorecard_ok is False and not v.ok


def test_a_log_prefix_verifies_and_unknown_inputs_are_refused(episode: tuple, tmp_path: Path) -> None:
    log = copy.deepcopy(episode[0])
    k = max(i for i, e in enumerate(log["entries"]) if e["kind"] == "advance")
    log["entries"] = log["entries"][: k - 3]
    log["root"] = log["entries"][-1]["hash"]
    v = verify(log)
    assert v.ok and v.run_id is None  # an unfinished session: every entry reproduces, no scorecard
    with pytest.raises(QtError) as e:
        verify({"schema": "qt.something"})
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        verify(tmp_path / "nope.json")
    assert e.value.code == "QT-INPUT-001"
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(QtError) as e:
        verify(tmp_path / "bad.json")
    assert e.value.code == "QT-INPUT-002"


def test_commit_log_basics() -> None:
    c = CommitLog(engine_version="x", dataset_id="0" * 64, spec_id="0" * 16, spec={})
    assert c.root == GENESIS
    e1 = c.append("open", clock_ns=1, visible_ns=1, rows_seen=0, data={})
    e2 = c.append("advance", clock_ns=2, visible_ns=2, rows_seen=3, data={"to_ns": 2})
    assert e2["prev"] == e1["hash"] and c.root == e2["hash"]
    doc = c.document().model_dump(mode="json")
    assert check_chain(doc) == []
    doc["root"] = e1["hash"]
    assert check_chain(doc) == ["root is not the last entry's hash"]
    doc = c.document().model_dump(mode="json")
    doc["entries"][1]["seq"] = 5
    assert any("seq" in p for p in check_chain(doc))
    with pytest.raises(ValidationError):
        c.append("teleport", clock_ns=3, visible_ns=3, rows_seen=0, data={})
    assert len(c.entries) == 2


def test_rejected_arguments_of_any_python_type_re_execute_to_the_same_rejection(tmp_path: Path) -> None:
    """Rejected requests are logged in the form they were validated in (`wire`), so `verify` meets every rejection
    again with the same message: float sizes and prices (a common slip in Python agents), a float partial cancel,
    non-finite floats, numpy numbers and Decimals, through a JSON round trip of the log."""
    import numpy as np

    with Sandbox.open(SPEC) as sb:
        bid = sb.observe().book.bid[0]
        accepted = sb.submit("buy", np.int64(1), price=np.int64(bid - 4 * sb.instrument["tick_size"]))
        assert accepted.order.price == bid - 4 * sb.instrument["tick_size"]  # numpy ints are fixed-point integers
        sb.submit("buy", 1, price=sb.to_price(bid - 5 * sb.instrument["tick_size"]))  # a Decimal price
        attempts = [
            lambda: sb.submit("buy", 2.0, price=bid),
            lambda: sb.submit("buy", 1, price=float(sb.to_price(bid))),
            lambda: sb.submit("buy", float("nan"), price=bid),
            lambda: sb.submit("buy", 1, price=float("inf")),
            lambda: sb.submit("buy", np.float64(1.0), price=bid),
            lambda: sb.submit(1.0, 1, price=bid),
            lambda: sb.submit("buy", 1, price=bid, peg_offset_ticks=1.0),
            lambda: sb.cancel(accepted.order.order_id, 1.0),
            lambda: sb.cancel(np.int64(1)),
            lambda: sb.cancel(accepted.order.order_id, np.float32(0.5)),
        ]
        for attempt in attempts:
            with pytest.raises(QtError):
                attempt()
        sb.advance(by="2s")
        card = sb.finish(out=tmp_path / "out")
    log = json.loads((tmp_path / "out" / "order_log.json").read_text())
    rejects = [e["data"] for e in log["entries"] if e["kind"] == "reject"]
    assert len(rejects) == len(attempts)
    assert rejects[0]["request"]["qty"] == 2.0 and isinstance(rejects[0]["request"]["qty"], float)
    assert rejects[2]["request"]["qty"] == "nan" and rejects[7]["request"]["qty"] == 1.0
    v = verify(tmp_path / "out" / "order_log.json", scorecard=tmp_path / "out" / "scorecard.json", workdir=tmp_path)
    assert v.ok, v.problems
    assert card.diagnostics.rejected_instructions == len(attempts)


def test_the_header_is_committed_by_the_open_entry(episode: tuple) -> None:
    """The open entry carries the engine version, dataset id, spec id and full spec digest, so the root commits to
    every setting; a header whose spec id or spec was edited (even with the chain recomputed) fails `header_ok`."""
    log = copy.deepcopy(episode[0])
    opened = log["entries"][0]["data"]
    assert {k: opened[k] for k in ("engine_version", "dataset_id", "spec_id")} == {
        k: log[k] for k in ("engine_version", "dataset_id", "spec_id")
    }
    assert len(opened["spec_sha256"]) == 64 and opened["spec_sha256"].startswith(log["spec_id"])
    edited = copy.deepcopy(log)
    edited["spec_id"] = "0" * 16
    v = verify(edited)
    assert v.chain_ok and not v.header_ok and not v.ok
    edited = copy.deepcopy(log)
    edited["spec"]["fees"]["taker_bps"] = 0.0  # a cheaper episode than the one run, header only
    v = verify(edited)
    assert v.chain_ok and not v.header_ok and not v.reexecution_ok and v.first_divergence == 0
    assert any("spec_id" in p for p in v.problems)
    forged = _rehash(copy.deepcopy(edited))  # the same edit with every hash recomputed: the open entry still differs
    v = verify(forged)
    assert v.chain_ok and not v.ok and v.first_divergence == 0


def test_an_engine_version_mismatch_is_reported_as_such(episode: tuple) -> None:
    log = copy.deepcopy(episode[0])
    log["engine_version"] = "0.0.0"
    log["entries"][0]["data"]["engine_version"] = "0.0.0"
    _rehash(log)
    v = verify(log)
    assert not v.header_ok and not v.ok and any("made with queuetruth 0.0.0" in p for p in v.problems)


def test_changed_data_under_an_unchanged_manifest_fails_the_dataset_check(episode: tuple, tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    from queuetruth.schemas.io import write_table

    log, _, _ = episode
    other = tmp_path / "other"
    shutil.copytree(SYNTH, other)
    man = json.loads((other / "manifest.json").read_text())
    part = next(p for p in man["partitions"] if p["instrument_id"] == 2 and p["session_date"] == 20620)
    f = other / part["files"][0]["path"]
    tab = pq.read_table(f)
    write_table(tab.slice(0, tab.num_rows - 1), f, tab.schema)  # the last record dropped, the manifest untouched
    v = verify(log, dataset=other)
    assert not v.dataset_ok and not v.ok and any("sha256" in p for p in v.problems)
