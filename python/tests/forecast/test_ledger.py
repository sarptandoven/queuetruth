"""The hash-chained ledger: hashing rule, append, tamper detection, duplicates, clocks, commitments, input
normalisation and timestamps."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from forecast.helpers import HOUR, SEC, T0, Clock, commit, fc, nonce, resolve
from queuetruth.errors import QtError
from queuetruth.forecast import ledger as lg
from queuetruth.forecast.io import format_ts, parse_ts, read_quotes, read_records
from queuetruth.forecast.schemas import ForecastCommitment, ForecastEntry
from queuetruth.schemas.runid import canonical_json


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def _write_lines(path: Path, docs: list[dict]) -> None:
    path.write_text("".join(canonical_json(d) + "\n" for d in docs))


def test_entries_follow_the_hashing_rule(tmp_path: Path) -> None:
    led = tmp_path / "ledger.jsonl"
    clock = Clock()
    es = commit(led, [fc("q1", "alice", 1, p=0.7), fc("q2", "alice", 2, p=0.2)], clock)
    lines = led.read_text().splitlines()
    assert len(lines) == 2
    for e, line in zip(es, lines, strict=True):
        doc = json.loads(line)
        assert line == canonical_json(doc)  # canonical JSON on disk
        assert doc["payload_sha256"] == hashlib.sha256(canonical_json(doc["payload"]).encode()).hexdigest()
        body = {k: v for k, v in doc.items() if k != "entry_sha256"}
        assert doc["entry_sha256"] == hashlib.sha256(canonical_json(body).encode()).hexdigest()
        assert ForecastEntry.model_validate(doc) == e
    assert es[0].prev_sha256 == lg.GENESIS
    assert es[1].prev_sha256 == es[0].entry_sha256
    assert [e.seq for e in es] == [0, 1]
    assert {e.clock for e in es} == {"system"}
    assert es[0].recorded_at_ns == T0


def test_known_answer(tmp_path: Path) -> None:
    """A frozen entry hash: changing canonical JSON or the entry layout changes it."""
    led = tmp_path / "l.jsonl"
    (e,) = commit(led, [fc("q1", "alice", 1, p=0.7)], Clock())
    assert e.payload_sha256 == lg.payload_digest(e.payload)
    doc = json.loads(led.read_text())
    assert doc["payload"] == {
        "type": "forecast",
        "question_id": "q1",
        "forecaster": "alice",
        "forecast": {"kind": "binary", "p": 0.7},
        "as_of_ns": None,
        "nonce": nonce(1),
    }
    assert e.entry_sha256 == lg.entry_digest(doc)
    assert lg.sha256_hex(canonical_json({"a": 1})) == hashlib.sha256(b'{"a":1}').hexdigest()


def test_verify_clean_and_commitment(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.6)], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": T0 + 2 * HOUR}], clock)
    v = lg.verify_ledger(led)
    assert v.ok and len(v.entries) == 2
    c = lg.commitment(v.entries)
    assert c.ledger_id == v.entries[0].entry_sha256
    assert (c.head_seq, c.head_sha256, c.n_entries, c.n_forecasts, c.n_resolutions) == (
        1,
        v.entries[1].entry_sha256,
        2,
        1,
        1,
    )
    assert c.head_recorded_at_ns == T0 + HOUR
    assert lg.check_commitment(v.entries, c) == []
    assert lg.commitment_digest(c) == hashlib.sha256(canonical_json(c.model_dump(mode="json")).encode()).hexdigest()
    # the commitment carries no forecast content
    text = c.model_dump_json()
    assert "0.6" not in text and "q1" not in text
    # still valid after more entries are appended
    commit(led, [fc("q2", "a", 2, p=0.1)], clock)
    assert lg.check_commitment(lg.verify_ledger(led).entries, c) == []
    with pytest.raises(QtError, match="empty"):
        lg.commitment([])


def _tamper_cases() -> list[str]:
    return ["payload", "payload_rehashed", "reorder", "delete", "insert_copy", "truncate", "garbage", "clock_back"]


@pytest.mark.parametrize("case", _tamper_cases())
def test_tampering_is_detected(tmp_path: Path, case: str) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    for i in range(4):
        commit(led, [fc(f"q{i}", "a", i, p=0.5)], clock)
        clock.now += SEC
    c = lg.commitment(lg.verify_ledger(led).entries)
    docs = _lines(led)
    if case == "payload":
        docs[1]["payload"]["forecast"]["p"] = 0.9
        _write_lines(led, docs)
    elif case == "payload_rehashed":  # payload and both hashes recomputed: the next link breaks
        docs[1]["payload"]["forecast"]["p"] = 0.9
        docs[1]["payload_sha256"] = lg.payload_digest(docs[1]["payload"])
        docs[1]["entry_sha256"] = lg.entry_digest(docs[1])
        _write_lines(led, docs)
    elif case == "reorder":
        docs[1], docs[2] = docs[2], docs[1]
        _write_lines(led, docs)
    elif case == "delete":
        del docs[2]
        _write_lines(led, docs)
    elif case == "insert_copy":
        docs.insert(2, docs[1])
        _write_lines(led, docs)
    elif case == "truncate":
        led.write_text(led.read_text()[:-10])
    elif case == "garbage":
        led.write_text(led.read_text() + "{not json}\n")
    elif case == "clock_back":  # an attacker re-chains everything with an earlier time on entry 2
        docs[2]["recorded_at_ns"] = T0 - HOUR
        prev = docs[1]["entry_sha256"]
        for d in docs[2:]:
            d["prev_sha256"] = prev
            d["entry_sha256"] = lg.entry_digest(d)
            prev = d["entry_sha256"]
        _write_lines(led, docs)
    v = lg.verify_ledger(led)
    assert not v.ok, case
    with pytest.raises(QtError) as ei:
        commit(led, [fc("qx", "a", 99, p=0.5)], clock)
    assert ei.value.code == "QT-INPUT-002"
    assert "integrity" in ei.value.message
    # the chain check catches every case; the commitment alone also catches those that change the head's hash or
    # the entry count (an edit before the head that leaves the head intact is the chain check's job)
    if case in ("delete", "insert_copy", "clock_back"):
        assert lg.check_commitment(v.entries, c), case
    elif case in ("payload", "payload_rehashed", "reorder"):
        assert lg.check_commitment(v.entries, c) == [], case


def test_full_rewrite_is_caught_by_a_published_commitment(tmp_path: Path) -> None:
    """Re-chaining the whole file passes the local check; the earlier published commitment exposes it."""
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q0", "a", 0, p=0.5), fc("q1", "a", 1, p=0.5)], clock)
    published = lg.commitment(lg.verify_ledger(led).entries)
    docs = _lines(led)
    docs[0]["payload"]["forecast"]["p"] = 0.99
    prev = lg.GENESIS
    for d in docs:
        d["prev_sha256"] = prev
        d["payload_sha256"] = lg.payload_digest(d["payload"])
        d["entry_sha256"] = lg.entry_digest(d)
        prev = d["entry_sha256"]
    _write_lines(led, docs)
    v = lg.verify_ledger(led)
    assert v.ok
    problems = lg.check_commitment(v.entries, published)
    assert any("ledger_id" in p for p in problems)
    assert any("differs from the committed head" in p for p in problems)


def test_commitment_beyond_head(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    commit(led, [fc("q0", "a", 0, p=0.5)], Clock())
    es = lg.verify_ledger(led).entries
    c = lg.commitment(es).model_copy(update={"head_seq": 5})
    assert "entries" in lg.check_commitment(es, c)[0]
    assert lg.check_commitment([], c) == ["the ledger is empty"]


def test_duplicate_payload_refused(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.5)], clock)
    with pytest.raises(QtError) as ei:
        commit(led, [fc("q1", "a", 1, p=0.5)], clock)
    assert ei.value.code == "QT-INPUT-003"
    with pytest.raises(QtError):  # also within one batch
        commit(led, [fc("q2", "a", 2, p=0.5), fc("q2", "a", 2, p=0.5)], clock)
    assert len(lg.verify_ledger(led).entries) == 1  # a refused batch writes nothing
    commit(led, [fc("q1", "a", 3, p=0.5)], clock)  # a fresh nonce is a new forecast


def test_duplicate_resolution_and_correction(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    row = {"question_id": "q1", "outcome": 1, "resolved_at": T0}
    resolve(led, [row], clock)
    with pytest.raises(QtError) as ei:
        resolve(led, [{**row, "outcome": 0}], clock)
    assert ei.value.code == "QT-INPUT-003"
    resolve(led, [{**row, "outcome": 0}], clock, allow_correction=True)
    assert len(lg.verify_ledger(led).entries) == 2


def test_system_clock_never_goes_backwards_and_declared_clock(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.5)], clock)
    clock.now -= HOUR  # the machine clock steps back
    (e,) = commit(led, [fc("q2", "a", 2, p=0.5)], clock)
    assert e.recorded_at_ns == T0 and e.clock == "system"
    (d,) = commit(led, [fc("q3", "a", 3, p=0.5)], clock, declared_at_ns=T0 - 10 * HOUR)
    assert (d.clock, d.recorded_at_ns) == ("declared", T0 - 10 * HOUR)
    clock.now = T0 + SEC
    (e2,) = commit(led, [fc("q4", "a", 4, p=0.5)], clock)
    assert e2.recorded_at_ns == T0 + SEC
    assert lg.verify_ledger(led).ok  # declared times do not take part in the clock-order check


def test_concurrent_appends_keep_one_chain(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    errors: list[BaseException] = []

    def worker(k: int) -> None:
        try:
            for i in range(10):
                commit(led, [fc(f"q{k}-{i}", f"w{k}", 1000 * k + i, p=0.5)], Clock(T0 + k))
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    v = lg.verify_ledger(led)
    assert v.ok and len(v.entries) == 40


def test_missing_ledger(tmp_path: Path) -> None:
    with pytest.raises(QtError) as ei:
        lg.verify_ledger(tmp_path / "none.jsonl")
    assert ei.value.code == "QT-INPUT-001"


# ---- normalisation ----------------------------------------------------------------------------------------------


def test_normalize_forecast_shapes() -> None:
    b = lg.normalize_forecast({"question_id": "q", "p": "0.25"}, "r", forecaster="me")
    assert (b.forecaster, b.forecast.kind, b.forecast.p) == ("me", "binary", 0.25)
    assert len(b.nonce) == 64
    c = lg.normalize_forecast({"question_id": "q", "forecaster": "x", "probs": '{"b": 0.6, "a": 0.3999}'}, "r")
    assert list(c.forecast.probs) == ["a", "b"]
    assert sum(c.forecast.probs.values()) == pytest.approx(1.0, abs=1e-15)
    n = lg.normalize_forecast({"question_id": "q", "forecaster": "x", "mean": 1, "sd": 2}, "r")
    assert (n.forecast.dist.family, n.forecast.dist.sd) == ("normal", 2.0)
    q = lg.normalize_forecast({"question_id": "q", "forecaster": "x", "quantiles": {"0.9": 3, "0.1": 1}}, "r")
    assert (q.forecast.dist.levels, q.forecast.dist.values) == ([0.1, 0.9], [1.0, 3.0])
    lv = lg.normalize_forecast({"question_id": "q", "forecaster": "x", "levels": [0.5], "values": [2]}, "r")
    assert lv.forecast.dist.family == "quantiles"
    s = lg.normalize_forecast({"question_id": "q", "forecaster": "x", "samples": "[1, 2, 3]", "as_of": str(T0)}, "r")
    assert (s.forecast.dist.samples, s.as_of_ns) == ([1.0, 2.0, 3.0], T0)


@pytest.mark.parametrize(
    ("row", "msg"),
    [
        ({"forecaster": "x", "p": 0.5}, "question_id is required"),
        ({"question_id": "q", "p": 0.5}, "forecaster is required"),
        ({"question_id": "q", "forecaster": "x"}, "exactly one of"),
        ({"question_id": "q", "forecaster": "x", "p": 0.5, "samples": [1]}, "exactly one of"),
        ({"question_id": "q", "forecaster": "x", "p": 1.5}, "outside"),
        ({"question_id": "q", "forecaster": "x", "p": True}, "expected a probability"),
        ({"question_id": "q", "forecaster": "x", "p": 0.5, "kind": "categorical"}, "does not match"),
        ({"question_id": "q", "forecaster": "x", "probs": {"a": 0.5, "b": 0.4}}, "sum to"),
        ({"question_id": "q", "forecaster": "x", "probs": {"a": 1.0}}, "two or more"),
        ({"question_id": "q", "forecaster": "x", "mean": 1}, "needs mean and sd"),
        ({"question_id": "q", "forecaster": "x", "mean": 1, "sd": 0}, "sd must be positive"),
        ({"question_id": "q", "forecaster": "x", "quantiles": {"0.1": 3, "0.9": 1}}, "quantiles cross"),
        ({"question_id": "q", "forecaster": "x", "quantiles": {"1.5": 3}}, r"levels must lie in \(0, 1\)"),
        ({"question_id": "q", "forecaster": "x", "samples": []}, "non-empty"),
        ({"question_id": "q", "forecaster": "x", "samples": [1, "inf"]}, "non-finite"),
        ({"question_id": "q", "forecaster": "x", "p": 0.5, "nonce": "zz"}, "nonce"),
        ({"question_id": "q", "forecaster": "x", "p": 0.5, "as_of": "2026-01-01"}, "ISO 8601"),
    ],
)
def test_normalize_forecast_errors(row: dict, msg: str) -> None:
    with pytest.raises(QtError, match=msg) as ei:
        lg.normalize_forecast(row, "f.csv: row 3")
    assert ei.value.code == "QT-INPUT-002"
    assert "f.csv: row 3" in ei.value.message


def test_normalize_resolution_kinds() -> None:
    b = lg.normalize_resolution({"question_id": "q", "outcome": "Yes", "resolved_at": "2026-11-04T00:00:00Z"}, "r")
    assert (b.kind, b.outcome, b.info_cutoff_ns) == ("binary", 1.0, None)
    assert lg.normalize_resolution({"question_id": "q", "outcome": False, "resolved_at": T0 + 0}, "r").outcome == 0.0
    assert lg.normalize_resolution({"question_id": "q", "outcome": 1.0, "resolved_at": T0 + 0}, "r").kind == "binary"
    c = lg.normalize_resolution(
        {"question_id": "q", "outcome": "b", "outcomes": "a|b|c", "ordered": "true", "resolved_at": T0 + 5, "group": 7},
        "r",
    )
    assert (c.kind, c.outcomes, c.ordered, c.group) == ("categorical", ["a", "b", "c"], True, "7")
    x = lg.normalize_resolution(
        {"question_id": "q", "outcome": 3.25, "resolved_at": T0 + 9, "info_cutoff": T0 + 8}, "r"
    )
    assert (x.kind, x.outcome, x.info_cutoff_ns) == ("continuous", 3.25, T0 + 8)
    v = lg.normalize_resolution({"question_id": "q", "void": "yes", "resolved_at": T0 + 9, "kind": "binary"}, "r")
    assert (v.void, v.outcome) == (True, None)


@pytest.mark.parametrize(
    ("row", "msg"),
    [
        ({"outcome": 1, "resolved_at": T0 + 0}, "question_id is required"),
        ({"question_id": "q", "outcome": 1}, "expected a timestamp"),
        ({"question_id": "q", "outcome": 1, "resolved_at": T0 + 5, "info_cutoff": T0 + 6}, "after resolved_at"),
        ({"question_id": "q", "outcome": "maybe", "kind": "binary", "resolved_at": T0 + 0}, "binary outcome"),
        ({"question_id": "q", "outcome": "z", "outcomes": ["a", "b"], "resolved_at": T0 + 0}, "not one of"),
        ({"question_id": "q", "outcome": "a", "outcomes": ["a", "a"], "resolved_at": T0 + 0}, "duplicate"),
        ({"question_id": "q", "outcome": "a", "outcomes": ["a"], "resolved_at": T0 + 0}, "two or more"),
        ({"question_id": "q", "outcome": "a", "kind": "categorical", "resolved_at": T0 + 0}, "needs outcomes"),
        ({"question_id": "q", "outcome": "x", "kind": "continuous", "resolved_at": T0 + 0}, "expected numbers"),
        ({"question_id": "q", "outcome": 1, "kind": "weird", "resolved_at": T0 + 0}, "unknown kind"),
    ],
)
def test_normalize_resolution_errors(row: dict, msg: str) -> None:
    with pytest.raises(QtError, match=msg) as ei:
        lg.normalize_resolution(row, "r.csv: row 1")
    assert ei.value.code == "QT-INPUT-002"


# ---- timestamps and files ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "ns"),
    [
        ("1970-01-01T00:00:00Z", 0),
        ("1970-01-01T00:00:01.5Z", 1_500_000_000),
        ("1970-01-01T00:00:00.000000001Z", 1),
        ("1970-01-01T01:00:00+01:00", 0),
        ("1969-12-31T19:00:00-0500", 0),
        ("2026-11-03 23:59:00z", 1_793_750_340_000_000_000),
        (str(T0), T0),
        (T0 + 456, T0 + 456),
        (float(T0), T0),
        ("631152000000000000", 631_152_000_000_000_000),
    ],
)
def test_parse_ts(text: object, ns: int) -> None:
    assert parse_ts(text, "t") == ns


@pytest.mark.parametrize(
    "bad",
    ["2026-11-03", "2026-11-03T10:00:00", "soon", None, True, 1.5, "2026-13-01T00:00:00Z", 0, 123, -5, 2**63],
)
def test_parse_ts_refuses(bad: object) -> None:
    with pytest.raises(QtError) as ei:
        parse_ts(bad, "field")
    assert ei.value.code == "QT-INPUT-002" and "field" in ei.value.message


@pytest.mark.parametrize(
    ("value", "unit"), [(1_793_750_340, "s"), ("1793750340000", "ms"), (1_793_750_340_000_000, "us")]
)
def test_parse_ts_names_the_likely_unit(value: object, unit: str) -> None:
    """Epoch seconds, ms or us read as ns would land in January 1970: refused, with the probable unit."""
    with pytest.raises(QtError, match=f"looks like {unit} since the epoch"):
        parse_ts(value, "ts")


def test_format_ts_round_trip() -> None:
    for ns in (0, 1, T0, T0 + 123_456_789):
        assert parse_ts(format_ts(ns), "t") == ns
    assert format_ts(1) == "1970-01-01T00:00:00.000000001Z"


def test_read_records_formats(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text('question_id,p,probs\n0001,0.5,\nq2,,"{""a"": 1}"\n')
    rows = read_records(tmp_path / "a.csv")
    assert rows == [
        {"question_id": "0001", "p": "0.5", "probs": None},
        {"question_id": "q2", "p": None, "probs": {"a": 1}},
    ]
    (tmp_path / "a.jsonl").write_text('{"x": 1}\n\n{"x": 2}\n')
    assert read_records(tmp_path / "a.jsonl") == [{"x": 1}, {"x": 2}]
    (tmp_path / "a.json").write_text('[{"x": 1}]')
    assert read_records(tmp_path / "a.json") == [{"x": 1}]
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.table({"x": [1, 2]}), tmp_path / "a.parquet")
    assert read_records(tmp_path / "a.parquet") == [{"x": 1}, {"x": 2}]
    for name, text, code in [
        ("b.jsonl", "[1]\n", "QT-INPUT-002"),
        ("c.jsonl", "{bad\n", "QT-INPUT-002"),
        ("d.json", "3", "QT-INPUT-002"),
        ("e.txt", "x", "QT-INPUT-002"),
        ("f.parquet", "not parquet", "QT-INPUT-002"),
    ]:
        (tmp_path / name).write_text(text)
        with pytest.raises(QtError) as ei:
            read_records(tmp_path / name)
        assert ei.value.code == code, name
    with pytest.raises(QtError) as ei:
        read_records(tmp_path / "missing.csv")
    assert ei.value.code == "QT-INPUT-001"


def test_read_quotes(tmp_path: Path) -> None:
    f = tmp_path / "q.csv"
    f.write_text(
        "question_id,ts,best_bid,best_ask,bid_depth,ask_depth\n"
        f"q1,{T0 + 20},0.40,0.45,10,20\n"
        f"q1,{T0 + 10},0.30,0.35,5,5\n"
        f"q1,{T0 + 20},0.41,0.44,1,2\n"
        f"q2,{T0 + 5},,0.2,,7\n"
    )
    qs = read_quotes(f)
    q1 = qs["q1"]
    assert q1.ts.tolist() == [T0 + 10, T0 + 20, T0 + 20]
    assert q1.asof(T0 + 9) is None and q1.asof(T0 + 10) == 0
    assert q1.asof(T0 + 25) == 2 and q1.mid(2) == pytest.approx(0.425)  # the last of equal timestamps wins
    assert qs["q2"].mid(0) is None
    for body, msg in [
        (f"q1,{T0},0.5,0.4,1,1", "crossed"),
        (f"q1,{T0},1.2,1.3,1,1", "outside"),
        (f"q1,{T0},0.2,0.3,-1,1", "negative"),
        (f",{T0},0.2,0.3,1,1", "question_id"),
        (f"q1,{T0},abc,0.3,1,1", "must be a number"),
    ]:
        f.write_text("question_id,ts,best_bid,best_ask,bid_depth,ask_depth\n" + body + "\n")
        with pytest.raises(QtError, match=msg):
            read_quotes(f)
    f.write_text(f"question_id,ts,best_bid\nq1,{T0},0.5\n")
    with pytest.raises(QtError, match="missing column"):
        read_quotes(f)


def test_commitment_document_round_trip(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    commit(led, [fc("q0", "a", 0, p=0.5)], Clock())
    c = lg.commitment(lg.verify_ledger(led).entries)
    back = ForecastCommitment.model_validate_json(c.model_dump_json())
    assert back == c
    assert json.loads(c.model_dump_json())["schema"] == "qt.forecast_commitment"


def test_fast_canonical_json_matches_runid() -> None:
    """The ledger's canonical JSON is byte-identical to `queuetruth.schemas.runid.canonical_json`."""
    from queuetruth.schemas import runid

    kat = json.loads((Path(__file__).resolve().parents[3] / "schemas" / "run_id_kat.json").read_text())
    docs: list[object] = [c["spec"] for c in kat["cases"]]
    docs += [json.loads(text) for text, _ in kat["numbers"]]
    every_char = "".join(chr(c) for c in range(0x10000) if not 0xD800 <= c < 0xE000) + "\U0001f600\U00010000"
    docs += [
        every_char,
        {every_char[i : i + 7]: i for i in range(0, 2000, 7)},
        {"": 1, "\U0001f600": 2, "a": [None, True, False, 0, -1, 1e-7, 1e21, 0.1, 2**70]},
        [{"z": {"y": [1.5, "x\u0000\u001f\u007f"]}}],
    ]
    rng = np.random.default_rng(1)
    for _ in range(200):
        docs.append({"p": float(rng.random()), "n": int(rng.integers(-(10**12), 10**12)), "s": rng.bytes(8).hex()})
    for d in docs:
        assert lg.canonical_json(d) == runid.canonical_json(d)
    with pytest.raises(TypeError):
        lg.canonical_json({1: 2})
    with pytest.raises(TypeError):
        lg.canonical_json({"a": object()})
    with pytest.raises(ValueError):
        lg.canonical_json(float("nan"))


def test_csv_identifiers_keep_their_text(tmp_path: Path) -> None:
    """Numeric-looking ids and all-digit nonces survive a CSV round trip unchanged."""
    f = tmp_path / "f.csv"
    digits = "0" * 63 + "7"
    f.write_text(f"question_id,forecaster,p,nonce,as_of\n0001,007,0.25,{digits},{T0}\n")
    (row,) = read_records(f)
    pl = lg.normalize_forecast(row, "f")
    assert (pl.question_id, pl.forecaster, pl.nonce, pl.as_of_ns) == ("0001", "007", digits, T0)
    r = tmp_path / "r.csv"
    r.write_text(f"question_id,outcome,resolved_at,outcomes,void\n0001,02,{T0},01|02,false\n")
    (rrow,) = read_records(r)
    res = lg.normalize_resolution(rrow, "r")
    assert (res.question_id, res.kind, res.outcome, res.void) == ("0001", "categorical", "02", False)


# ---- untrusted ledger content, repeats, declared times, readers ---------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "msg"),
    [
        ("[1,2]", "[1,NaN]", "non-finite number NaN"),
        ("[1,2]", "[1,Infinity]", "non-finite number Infinity"),
        ("[1,2]", "[1,1e400]", "finite number"),
    ],
)
def test_non_finite_numbers_are_ledger_problems(tmp_path: Path, old: str, new: str, msg: str) -> None:
    """NaN, Infinity and overflowing literals are reported as integrity problems, never a crash."""
    led = tmp_path / "l.jsonl"
    commit(led, [fc("q", "a", 1, samples=[1, 2])], Clock())
    text = led.read_text()
    assert old in text
    led.write_text(text.replace(old, new))
    v = lg.verify_ledger(led)
    assert not v.ok and msg in v.problems[0].problem
    assert v.problems[0].problem.startswith("not a qt.forecast_entry")


@pytest.mark.parametrize(
    ("fields", "msg"),
    [
        ({"kind": "binary", "outcome": 0.5}, "binary outcome must be 0 or 1"),
        ({"kind": "categorical", "outcome": "z", "outcomes": ["a", "b"]}, "one of outcomes"),
        ({"kind": "categorical", "outcome": "a"}, "one of outcomes"),
        ({"kind": "binary", "outcome": None}, "required unless"),
        ({"kind": "continuous", "outcome": "x"}, "must be a number"),
        ({"kind": "binary", "outcome": 1.0, "info_cutoff_ns": T0 + 1}, "info_cutoff_ns is after"),
        ({"kind": "categorical", "outcome": "a", "outcomes": ["a", "a"]}, "distinct labels"),
    ],
)
def test_resolution_payload_is_validated(tmp_path: Path, fields: dict, msg: str) -> None:
    """The rules of normalize_resolution hold for every resolution entry, built through the API or read back from a
    hand-written (and re-chained) ledger, so `score` never meets an impossible outcome."""
    from pydantic import ValidationError

    from queuetruth.forecast.schemas import ResolutionPayload

    with pytest.raises(ValidationError, match=msg):
        ResolutionPayload(question_id="q", resolved_at_ns=T0, **fields)
    led = tmp_path / "l.jsonl"
    resolve(led, [{"question_id": "q", "outcome": 1, "resolved_at": T0}], Clock())
    (doc,) = _lines(led)
    doc["payload"] = {**doc["payload"], **fields}
    doc["payload_sha256"] = lg.payload_digest(doc["payload"])
    doc["entry_sha256"] = lg.entry_digest(doc)
    _write_lines(led, [doc])
    v = lg.verify_ledger(led)
    assert not v.ok and msg in v.problems[0].problem


def test_repeated_forecast_without_nonce_is_refused(tmp_path: Path) -> None:
    """Re-running a commit of a file without nonces draws new nonces; the same content is refused unless allowed."""
    led = tmp_path / "l.jsonl"
    rows = [{"question_id": "q1", "forecaster": "a", "p": 0.4}, {"question_id": "q2", "forecaster": "a", "p": 0.4}]
    pls = [lg.normalize_forecast(r, "r") for r in rows]
    lg.append(led, pls, generated_nonce=[True, True], clock=Clock())
    again = [lg.normalize_forecast(r, "r") for r in rows]
    with pytest.raises(QtError, match="repeated forecast: q1 by a") as ei:
        lg.append(led, again, generated_nonce=[True, True], clock=Clock())
    assert ei.value.code == "QT-INPUT-003"
    with pytest.raises(QtError, match="repeated forecast"):  # also within one batch
        lg.append(
            led,
            [lg.normalize_forecast(rows[0] | {"question_id": "q3"}, "r")] * 1
            + [lg.normalize_forecast(rows[0] | {"question_id": "q3"}, "r")],
            generated_nonce=[True, True],
            clock=Clock(),
        )
    assert len(lg.verify_ledger(led).entries) == 2
    lg.append(led, again, generated_nonce=None, clock=Clock())  # allowed explicitly
    changed = [lg.normalize_forecast(rows[0] | {"p": 0.5}, "r")]
    lg.append(led, changed, generated_nonce=[True], clock=Clock())  # an update is a new forecast
    assert len(lg.verify_ledger(led).entries) == 5


def test_per_entry_declared_times(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    pls = [lg.normalize_forecast(fc(f"q{i}", "a", i, p=0.5), "r") for i in range(3)]
    es = lg.append(led, pls, declared_at_ns=[T0 - 2 * HOUR, None, T0 - HOUR], clock=Clock())
    assert [(e.clock, e.recorded_at_ns) for e in es] == [
        ("declared", T0 - 2 * HOUR),
        ("system", T0),
        ("declared", T0 - HOUR),
    ]
    with pytest.raises(ValueError):
        lg.append(led, pls[:1], declared_at_ns=[T0, T0], clock=Clock())


def test_readers_wait_for_an_append_in_progress(tmp_path: Path) -> None:
    """verify_ledger takes a shared lock, so it never reads half of an append (which would look truncated)."""
    led = tmp_path / "l.jsonl"
    commit(led, [fc("q0", "a", 0, p=0.5)], Clock())
    started = threading.Event()
    seen: list[int] = []

    def reader() -> None:
        started.set()
        seen.append(len(lg.verify_ledger(led).entries))

    with lg._locked(led) as fh:
        fh.seek(0, 2)
        fh.write(b'{"partial":')  # a writer in the middle of an append
        fh.flush()
        t = threading.Thread(target=reader)
        t.start()
        started.wait()
        t.join(0.3)
        assert t.is_alive() and not seen  # blocked on the exclusive lock
        fh.truncate(len(led.read_bytes()) - len(b'{"partial":'))
    t.join(5)
    assert seen == [1]


def test_csv_quoted_line_breaks_are_data(tmp_path: Path) -> None:
    """A quoted field keeps its newline, and U+2028 or \\x85 inside a field never splits a row."""
    f = tmp_path / "f.csv"
    f.write_text('question_id,forecaster,p,note\n"q\n1",a,0.25,"line1\nline2"\nq1,b c,0.5,x\x85y\n', encoding="utf-8")
    rows = read_records(f)
    assert [r["question_id"] for r in rows] == ["q\n1", "q1"]
    assert rows[0]["note"] == "line1\nline2"
    assert (rows[1]["forecaster"], rows[1]["note"]) == ("b c", "x\x85y")
