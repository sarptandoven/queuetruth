"""The ledger file: line format, hash chain, tamper detection, torn appends, locking and concurrency."""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from queuetruth.errors import QtError
from queuetruth.ledger import Ledger, LedgerRecord, register_variant, verify_chain
from queuetruth.ledger._capture import variant_record
from queuetruth.ledger._lock import exclusive, lock_timeout_s
from queuetruth.ledger._store import GENESIS_PREV, encode_line, parse_anchor
from queuetruth.schemas.runid import canonical_json

BODY_AT = len('{"hash":"') + 64 + len('","record":')


def _fill(led: Ledger, n: int) -> list[LedgerRecord]:
    return [led.append(*variant_record(f"v{i}", {"i": i}, returns=[0.01 * i, -0.002])) for i in range(n)]


def _lines(led: Ledger) -> list[bytes]:
    return led.path.read_bytes().splitlines()


def _write(led: Ledger, lines: list[bytes]) -> None:
    led.path.write_bytes(b"".join(x + b"\n" for x in lines))


def _rehash(record: dict) -> bytes:
    return encode_line(record)[0].rstrip(b"\n")


def _record(line: bytes) -> dict:
    return json.loads(line)["record"]


def test_line_format_and_chain_checked_independently(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "L")
    stored = _fill(led, 3)
    lines = _lines(led)
    assert len(lines) == 4  # genesis + 3
    prev = GENESIS_PREV
    for i, line in enumerate(lines):
        env = json.loads(line)
        assert list(env) == ["hash", "record"]
        body = line[BODY_AT:-1]
        assert hashlib.sha256(body).hexdigest() == env["hash"]  # the hash covers the exact bytes of the record
        assert canonical_json(env).encode() == line  # the whole line is canonical JSON
        rec = env["record"]
        assert (rec["seq"], rec["prev"], rec["schema"], rec["schema_version"]) == (i, prev, "qt.ledger_record", 1)
        assert rec["kind"] == ("genesis" if i == 0 else "variant")
        prev = env["hash"]
    ids = {_record(x)["ledger_id"] for x in lines}
    assert len(ids) == 1 and len(next(iter(ids))) == 32
    assert [r.seq for r in stored] == [1, 2, 3]
    assert led.records()[1:] == stored
    assert led.head().hash == prev and led.head().seq == 3
    v = led.verify()
    assert v.ok and v.n_records == 4 and v.problems == [] and v.uncommitted_tail_bytes == 0
    assert v.head is not None and v.head.hash == prev


def test_location_forms(tmp_path: Path) -> None:
    assert Ledger(tmp_path / "d").path == tmp_path / "d" / "ledger.jsonl"
    assert Ledger(tmp_path / "x.jsonl").path == tmp_path / "x.jsonl"
    led = Ledger(tmp_path / "x.jsonl")
    _fill(led, 1)
    assert Ledger(led.path).records() == led.records()
    assert led.lock_path == tmp_path / "x.jsonl.lock"
    assert not (tmp_path / ".gitignore").exists()  # only the default location is git-ignored


def test_recorded_at_is_set_under_the_lock_and_monotonic_with_seq(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "L")
    recs = _fill(led, 5)
    times = [r.recorded_at_ns for r in led.records()]
    assert times == sorted(times)
    assert [r.recorded_at_ns for r in recs] == times[1:]


# ---- tampering ----------------------------------------------------------------------------------------------------


@pytest.fixture
def chain(tmp_path: Path) -> Ledger:
    led = Ledger(tmp_path / "L")
    _fill(led, 5)
    return led


def _reasons(led: Ledger, anchor: str | None = None) -> list[tuple[int, str]]:
    v = led.verify(anchor)
    assert v.ok is (v.n_problems == 0)
    return [(p.line, p.reason) for p in v.problems]


def _read_fails(led: Ledger, reason: str) -> None:
    with pytest.raises(QtError) as e:
        led.records()
    assert e.value.code == "QT-LEDGER-001" and e.value.exit_code == 2
    assert reason in e.value.message and e.value.details["problem"]["reason"] == reason


def test_edited_value_is_a_hash_mismatch_on_that_line_only(chain: Ledger) -> None:
    lines = _lines(chain)
    assert b'"params":{"i":3}' in lines[4]
    lines[4] = lines[4].replace(b'"params":{"i":3}', b'"params":{"i":9}')
    _write(chain, lines)
    assert _reasons(chain) == [(5, "hash_mismatch")]
    _read_fails(chain, "hash_mismatch")
    # appending needs only the last line, which is intact: recording goes on and the problem stays reported
    assert register_variant("after", ledger=chain) is not None
    assert _reasons(chain) == [(5, "hash_mismatch")]


def test_edited_and_rehashed_record_breaks_the_next_link(chain: Ledger) -> None:
    lines = _lines(chain)
    rec = _record(lines[2])
    rec["params"] = {"i": 99}
    lines[2] = _rehash(rec)
    _write(chain, lines)
    assert _reasons(chain) == [(4, "broken_link")]
    _read_fails(chain, "broken_link")


def test_full_rewrite_passes_alone_but_fails_against_an_anchor(chain: Ledger) -> None:
    anchor_head = f"{chain.head().seq}:{chain.head().hash}"
    early = chain.records()[2]
    early_anchor = f"2:{chain.scan().hashes[2]}"
    assert early.seq == 2
    lines = _lines(chain)
    rec = _record(lines[2])
    rec["params"]["i"] = 99
    lines[2] = _rehash(rec)
    _rechain(lines, 3)  # rewrite from seq 2 on, re-chaining every line
    _write(chain, lines)
    assert chain.verify().ok  # a consistent rewrite is undetectable from the file alone
    assert _reasons(chain, anchor_head) == [(0, "anchor_mismatch")]
    assert _reasons(chain, early_anchor) == [(0, "anchor_mismatch")]
    assert chain.verify(f"1:{json.loads(lines[1])['hash']}").ok  # history before the rewrite is intact
    v = chain.verify(anchor_head)
    assert v.anchor_ok is False and v.anchor == anchor_head


def test_anchor_without_seq_and_wrong_seq(chain: Ledger) -> None:
    h = chain.scan().hashes[3]
    assert chain.verify(h).anchor_ok is True
    assert chain.verify(f"3:{h.upper()}").anchor_ok is True
    assert chain.verify(f"4:{h}").anchor_ok is False


@pytest.mark.parametrize("bad", ["", "3:", "x:" + "a" * 64, "a" * 63, "3:" + "g" * 64, "-1:" + "a" * 64])
def test_bad_anchor_is_a_config_error(chain: Ledger, bad: str) -> None:
    with pytest.raises(QtError) as e:
        chain.verify(bad)
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError):
        parse_anchor(bad)


def test_deleted_record(chain: Ledger) -> None:
    lines = _lines(chain)
    del lines[3]
    _write(chain, lines)
    assert _reasons(chain) == [(4, "seq_gap"), (4, "broken_link")]
    _read_fails(chain, "seq_gap")


def test_reordered_records(chain: Ledger) -> None:
    lines = _lines(chain)
    lines[2], lines[3] = lines[3], lines[2]
    _write(chain, lines)
    got = _reasons(chain)
    assert (3, "seq_gap") in got and (3, "broken_link") in got and (4, "seq_gap") in got


def test_repeated_record(chain: Ledger) -> None:
    lines = _lines(chain)
    lines.insert(3, lines[2])
    _write(chain, lines)
    assert _reasons(chain) == [(4, "seq_gap"), (4, "broken_link")]  # the next line links to the copy


def test_inserted_forged_record(chain: Ledger) -> None:
    lines = _lines(chain)
    forged = _record(lines[2])
    forged["seq"] = 3
    forged["prev"] = json.loads(lines[2])["hash"]
    forged["name"] = "forged"
    lines.insert(3, _rehash(forged))
    _write(chain, lines)
    assert _reasons(chain) == [(5, "seq_gap"), (5, "broken_link")]


def test_spliced_ledger_id(chain: Ledger, tmp_path: Path) -> None:
    other = Ledger(tmp_path / "other")
    _fill(other, 1)
    lines = _lines(chain)
    rec = _record(lines[3])
    rec["ledger_id"] = other.records()[0].ledger_id
    lines[3] = _rehash(rec)
    _rechain(lines, 4)
    _write(chain, lines)
    assert _reasons(chain) == [(4, "ledger_id_mismatch")]


def _rechain(lines: list[bytes], start: int) -> None:
    """Re-links lines[start:] to the line before them (an attacker rewriting the rest of the history)."""
    prev = json.loads(lines[start - 1])["hash"]
    for i in range(start, len(lines)):
        r = _record(lines[i])
        r["prev"] = prev
        lines[i] = _rehash(r)
        prev = json.loads(lines[i])["hash"]


def test_non_canonical_bytes_with_a_valid_hash(chain: Ledger) -> None:
    lines = _lines(chain)
    body = json.dumps(_record(lines[2]), sort_keys=True, separators=(", ", ": ")).encode()  # same data, not canonical
    lines[2] = b'{"hash":"' + hashlib.sha256(body).hexdigest().encode() + b'","record":' + body + b"}"
    _rechain(lines, 3)
    _write(chain, lines)
    assert len(chain.records()) == 6  # hash-valid, so reads accept it
    assert _reasons(chain) == [(3, "not_canonical")]


def test_malformed_and_invalid_lines(chain: Ledger) -> None:
    lines = _lines(chain)
    lines[2] = b"not json at all"
    _write(chain, lines)
    assert _reasons(chain)[0] == (3, "malformed")
    _read_fails(chain, "malformed")
    lines = _lines(chain)
    rec = _record(lines[1])
    rec["kind"] = "bogus"
    lines[2] = _rehash({**rec, "seq": 2})
    _write(chain, lines)
    assert (3, "invalid_record") in _reasons(chain)


def test_missing_genesis(chain: Ledger) -> None:
    lines = _lines(chain)
    _write(chain, lines[1:])
    got = _reasons(chain)
    assert (1, "genesis") in got and (1, "broken_link") in got and (1, "seq_gap") in got


def test_second_genesis(chain: Ledger) -> None:
    lines = _lines(chain)
    g = _record(lines[0])
    lines.append(_rehash({**g, "seq": 6, "prev": json.loads(lines[-1])["hash"]}))
    _write(chain, lines)
    assert _reasons(chain) == [(7, "genesis")]


def test_problem_list_is_capped(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "L")
    _fill(led, 1)
    lines = _lines(led) + [b"junk"] * 150
    _write(led, lines)
    v = led.verify()
    assert not v.ok and v.n_problems == 150 and len(v.problems) == 100


def test_missing_and_empty_ledger(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "none")
    for fn in (led.records, led.verify, led.head):
        with pytest.raises(QtError) as e:
            fn()
        assert e.value.code == "QT-INPUT-001" and "no ledger at" in e.value.message
    led.directory.mkdir()
    led.path.write_bytes(b"")
    with pytest.raises(QtError, match="QT-INPUT-001"):
        led.records()
    assert not led.exists()


def test_only_an_uncommitted_tail(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "L")
    led.directory.mkdir()
    led.path.write_bytes(b'{"hash":"ab')
    with pytest.raises(QtError, match="QT-LEDGER-001"):
        led.records()
    v = led.verify()
    assert not v.ok and v.uncommitted_tail_bytes == 11 and v.problems[0].reason == "genesis"
    led.append(*variant_record("a"))  # the torn bytes are set aside; a new chain starts
    assert led.verify().ok and len(led.records()) == 2
    assert [p.read_bytes() for p in (led.directory / "torn").iterdir()] == [b'{"hash":"ab']


# ---- torn appends -------------------------------------------------------------------------------------------------


def test_torn_tail_is_ignored_by_reads_and_set_aside_by_the_next_append(chain: Ledger) -> None:
    before = chain.path.read_bytes()
    with chain.path.open("ab") as fh:
        fh.write(b'{"hash":"0123456789abcdef","record":{"seq":')
    assert len(chain.records()) == 6
    v = chain.verify()
    assert v.ok and v.uncommitted_tail_bytes == 43
    rec = chain.append(*variant_record("next"))
    assert rec.seq == 6
    data = chain.path.read_bytes()
    assert data.startswith(before) and data.endswith(b"\n") and chain.verify().ok
    torn = list((chain.directory / "torn").iterdir())
    assert len(torn) == 1 and torn[0].name.startswith(f"{len(before)}-")
    assert torn[0].read_bytes() == b'{"hash":"0123456789abcdef","record":{"seq":'


def test_complete_record_missing_only_its_newline_is_committed(chain: Ledger) -> None:
    last = chain.scan()
    rec = variant_record("cut").record.model_copy(
        update={"ledger_id": last.ledger_id, "seq": 6, "prev": last.hashes[-1], "recorded_at_ns": 1}
    )
    line, h = encode_line(rec.model_dump(mode="json"))
    with chain.path.open("ab") as fh:
        fh.write(line[:-1])
    nxt = chain.append(*variant_record("after"))
    assert nxt.seq == 7 and nxt.prev == h
    assert [r.name for r in chain.records()[-2:]] == ["cut", "after"]
    assert chain.verify().ok and not (chain.directory / "torn").exists()


def test_unreadable_last_record_refuses_to_extend(chain: Ledger) -> None:
    with chain.path.open("ab") as fh:
        fh.write(b"garbage\n")
    before = chain.path.read_bytes()
    with pytest.raises(QtError) as e:
        chain.append(*variant_record("x"))
    assert e.value.code == "QT-LEDGER-001" and "refusing to extend" in e.value.message
    assert chain.path.read_bytes() == before


def test_append_extends_a_head_whose_hash_was_altered(chain: Ledger) -> None:
    """A tampered head does not stop recording: the new record links to its stated hash and verify reports the
    tampered record."""
    lines = _lines(chain)
    lines[-1] = lines[-1].replace(b'"name":"v4"', b'"name":"v9"')
    _write(chain, lines)
    chain.append(*variant_record("new"))
    assert _reasons(chain) == [(6, "hash_mismatch")]


# ---- locking and concurrency --------------------------------------------------------------------------------------


def test_lock_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_LEDGER_LOCK_TIMEOUT_S", "2.5")
    assert lock_timeout_s() == 2.5
    for bad in ("-1", "soon", "nan"):
        monkeypatch.setenv("QT_LEDGER_LOCK_TIMEOUT_S", bad)
        with pytest.raises(QtError, match="QT-CONFIG-001"):
            lock_timeout_s()
    monkeypatch.setenv("QT_LEDGER_LOCK_TIMEOUT_S", " ")
    assert lock_timeout_s() == 30.0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock holder")
def test_lock_held_by_another_process_times_out(chain: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    holder = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "sys.stdin.read()\n"
    )
    p = subprocess.Popen(
        [sys.executable, "-c", holder, str(chain.lock_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert p.stdout is not None and p.stdout.readline().strip() == "locked"
        before = chain.path.read_bytes()
        monkeypatch.setenv("QT_LEDGER_LOCK_TIMEOUT_S", "0.2")
        with pytest.raises(QtError) as e:
            chain.append(*variant_record("blocked"))
        assert e.value.code == "QT-LEDGER-002" and "0.2 s" in e.value.message
        assert chain.path.read_bytes() == before
    finally:
        assert p.stdin is not None
        p.stdin.close()
        p.wait(timeout=10)
    assert chain.append(*variant_record("free")).seq == 6


def test_lock_is_released_after_an_error(chain: Ledger) -> None:
    with pytest.raises(RuntimeError), exclusive(chain.lock_path):
        raise RuntimeError("boom")
    assert chain.append(*variant_record("ok")).seq == 6


# Each worker is a separate interpreter running this snippet (not multiprocessing with a target in this module: a
# spawned child would re-import the test module by its dotted path, which only resolves under `python -m pytest`).
_WORKER = """
import sys
from queuetruth.ledger import Ledger
from queuetruth.ledger._capture import variant_record
path, who, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
led = Ledger(path)
for i in range(n):
    led.append(*variant_record(f"p{who}", {"i": i}, scores=[float(who), float(i)]))
"""


def test_parallel_processes_append_one_chain(tmp_path: Path) -> None:
    path = str(tmp_path / "L")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    procs = [
        subprocess.Popen([sys.executable, "-c", _WORKER, path, str(w), "25"], env=env, stderr=subprocess.PIPE)
        for w in range(4)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode(errors="replace")
    led = Ledger(path)
    v = led.verify()
    assert v.ok and v.n_records == 101
    recs = led.records()
    assert [r.seq for r in recs] == list(range(101))
    for w in range(4):  # each process's own appends stay in its order
        mine = [r.params["i"] for r in recs if r.name == f"p{w}"]
        assert mine == list(range(25))


def test_parallel_threads_append_one_chain(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "L")
    errors: list[BaseException] = []

    def go(w: int) -> None:
        try:
            for i in range(20):
                led.append(*variant_record(f"t{w}", {"i": i}))
        except BaseException as e:
            errors.append(e)

    ts = [threading.Thread(target=go, args=(w,)) for w in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    v = led.verify()
    assert v.ok and v.n_records == 121


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, not root")
def test_unwritable_location(tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(QtError) as e:
            Ledger(ro / "sub").append(*variant_record("x"))
        assert e.value.code == "QT-LEDGER-002" and "cannot create" in e.value.message
        with pytest.raises(QtError) as e:
            Ledger(ro).append(*variant_record("x"))
        assert e.value.code == "QT-LEDGER-002"
    finally:
        ro.chmod(0o700)


def test_verify_chain_api_uses_the_default_ledger(ledger_dir: Path) -> None:
    register_variant("a", {"k": 1})
    v = verify_chain()
    assert v.ok and v.ledger == str(ledger_dir / "ledger.jsonl") and v.n_records == 2


# ---- appends that are refused, batches, series files and forks ----------------------------------------------------


def test_append_refuses_a_genesis_record_and_revalidates(chain: Ledger) -> None:
    before = chain.path.read_bytes()
    g = chain.records()[0]
    with pytest.raises(QtError, match="genesis record is written by the ledger itself"):
        chain.append(g)
    bad = variant_record("ok").record.model_copy(update={"study": "has space"})  # model_copy skips validation
    with pytest.raises(QtError) as e:
        chain.append(bad)
    assert e.value.code == "QT-CONFIG-001" and "study" in e.value.message
    assert chain.path.read_bytes() == before and chain.verify().ok


@pytest.mark.parametrize("content", [b"1.2.3", b"1.2.3\n", b"# notes\nmore"])
def test_a_file_that_is_not_a_ledger_is_never_overwritten(tmp_path: Path, content: bytes) -> None:
    f = tmp_path / "VERSION"
    f.write_bytes(content)
    with pytest.raises(QtError) as e:
        Ledger(f).append(*variant_record("x"))
    assert e.value.code == "QT-LEDGER-001"
    assert f.read_bytes() == content and not (tmp_path / "torn").exists()


def test_append_many_is_one_write(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "L")
    drafts = [variant_record(f"b{i}", {"i": i}, returns=[0.1 * i, 0.2]) for i in range(4)]
    stored = led.append_many(drafts)
    assert [r.seq for r in stored] == [1, 2, 3, 4] and led.verify().ok
    assert led.append_many([]) == []
    bad = variant_record("ok").record.model_copy(update={"kind": "genesis"})
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        led.append_many([variant_record("fine"), bad])
    assert len(led.records()) == 5  # nothing of a refused batch was written


def test_series_files_are_content_addressed_and_checked(tmp_path: Path) -> None:
    from queuetruth.ledger import _series

    led = Ledger(tmp_path / "L")
    a = led.append(*variant_record("a", returns=[0.1, -0.2], index=[10, 20]))
    b = led.append(*variant_record("b", returns=[0.1, -0.2], index=[10, 20]))
    assert a.series is not None and b.series is not None and a.series.sha256 == b.series.sha256
    files = list((led.directory / "series").iterdir())
    assert [f.name for f in files] == [f"{a.series.sha256}.qts"]
    assert hashlib.sha256(files[0].read_bytes()).hexdigest() == a.series.sha256
    data = _series.load(led.directory, a.series)
    assert (data.values, data.index) == ([0.1, -0.2], [10, 20])
    assert b'"values"' not in led.path.read_bytes()  # the chain holds the sha256, not the values
    # an edited series file: verify names the record, reads of that series refuse
    blob = files[0].read_bytes()
    files[0].write_bytes(blob[:-8] + bytes(8))
    v = led.verify()
    assert not v.ok and [(p.line, p.seq, p.reason) for p in v.problems] == [(2, 1, "series_mismatch")]
    with pytest.raises(QtError, match="QT-LEDGER-001"):
        _series.load(led.directory, a.series)
    files[0].unlink()
    assert [p.reason for p in led.verify().problems] == ["series_missing"]
    assert len(led.records()) == 3  # counting never reads series files
    with pytest.raises(ValueError, match="not a queuetruth series"):
        _series.decode(b"XXXX" + bytes(9))


def _fork_append(path: str, q: mp.Queue) -> None:  # type: ignore[type-arg]
    try:
        q.put(Ledger(path).append(*variant_record("child")).seq)
    except Exception as e:  # reported to the parent
        q.put(repr(e))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")  # the case under test
def test_a_fork_while_another_thread_appends_does_not_deadlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child forked while a thread holds the in-process lock gets a fresh lock (os.register_at_fork) and only
    waits for the file lock, which the parent releases when its append ends."""
    from queuetruth.ledger import _lock

    monkeypatch.setenv("QT_LEDGER_LOCK_TIMEOUT_S", "20")
    led = Ledger(tmp_path / "L")
    led.append(*variant_record("first"))
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with _lock.exclusive(led.lock_path):
            held.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(10)
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    child = ctx.Process(target=_fork_append, args=(str(led.path), q))
    child.start()
    threading.Timer(0.5, release.set).start()
    got = q.get(timeout=30)
    child.join(30)
    t.join(10)
    assert got == 2 and child.exitcode == 0 and led.verify().ok
