"""The ledger file: `ledger.jsonl`, append-only and hash-chained, appended under an exclusive lock.

Line format (canonical JSON of `{"hash": H, "record": R}`, then `\\n`)::

    {"hash":"<64 hex>","record":<canonical JSON of R>}

`H` = sha256 of the exact bytes of `R` in the line, so a reader checks every line with one hash and no
re-serialization; `R.prev` = `H` of the previous line (64 zeros for the genesis record, seq 0). A line is committed
by its newline: bytes after the last newline are an append that was cut off and are never read as a record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from queuetruth.errors import QtError
from queuetruth.ledger import _series
from queuetruth.ledger._lock import exclusive
from queuetruth.ledger.models import Head, LedgerRecord, LedgerVerification, Problem
from queuetruth.schemas.runid import canonical_json

log = logging.getLogger("queuetruth.ledger")

LEDGER_FILE = "ledger.jsonl"
LOCK_SUFFIX = ".lock"
TORN_DIR = "torn"
GENESIS_PREV = "0" * 64
_PREFIX = b'{"hash":"'
_MID = b'","record":'
_BODY_AT = len(_PREFIX) + 64 + len(_MID)
_HEX = frozenset(b"0123456789abcdef")
_CHUNK = 1 << 16
#: Problems listed by `verify` (the rest are counted).
MAX_PROBLEMS = 100
GITIGNORE = "# Created by queuetruth: the trial ledger is ignored by git. Delete this file to commit the ledger.\n*\n"


class Draft(NamedTuple):
    """A record to append and the series files it references ({sha256: bytes})."""

    record: LedgerRecord
    blobs: Mapping[str, bytes] = {}


def now_ns() -> int:
    """Wall clock, UTC ns."""
    return time.time_ns()


def encode_line(record: dict[str, Any]) -> tuple[bytes, str]:
    """(line with newline, hash) of a record dict (JSON types only)."""
    body = canonical_json(record).encode("utf-8")
    h = hashlib.sha256(body).hexdigest()
    return _PREFIX + h.encode("ascii") + _MID + body + b"}\n", h


@dataclass
class _Line:
    """A committed line split into its parts; `error` set when it is not a well-formed ledger line."""

    hash: str | None = None
    body: bytes = b""
    record: dict[str, Any] | None = None
    error: tuple[str, str] | None = None


def _split(line: bytes) -> _Line:
    """Parses one committed line (without its newline): envelope, hash and JSON (not the record schema)."""
    if len(line) < _BODY_AT + 2 or not line.startswith(_PREFIX) or line[_BODY_AT - len(_MID) : _BODY_AT] != _MID:
        return _Line(error=("malformed", 'not a ledger line ({"hash":...,"record":...})'))
    if not line.endswith(b"}"):
        return _Line(error=("malformed", "the line does not end with '}'"))
    hx = line[len(_PREFIX) : len(_PREFIX) + 64]
    if not set(hx) <= _HEX:
        return _Line(error=("malformed", "hash is not 64 lower-case hex digits"))
    h = hx.decode("ascii")
    body = line[_BODY_AT:-1]
    try:
        rec = json.loads(body)
    except (UnicodeDecodeError, ValueError) as e:
        return _Line(hash=h, body=body, error=("malformed", f"record is not JSON: {e}"))
    if not isinstance(rec, dict):
        return _Line(hash=h, body=body, error=("malformed", "record is not a JSON object"))
    return _Line(hash=h, body=body, record=rec)


@dataclass
class Scan:
    """Every committed record of a ledger file with its hash, and the problems found."""

    records: list[LedgerRecord] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    n_problems: int = 0
    n_lines: int = 0
    tail_bytes: int = 0
    ledger_id: str | None = None
    #: 1-based line of each record's `seq`.
    line_of: dict[int, int] = field(default_factory=dict)

    def add(self, line: int, seq: int | None, reason: str, message: str) -> None:
        self.n_problems += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(Problem(line=line, seq=seq, reason=reason, message=message))  # type: ignore[arg-type]

    @property
    def head(self) -> Head | None:
        if not self.records:
            return None
        return Head(seq=self.records[-1].seq, hash=self.hashes[-1])


def scan_bytes(data: bytes, *, full: bool, stop_at_first: bool) -> Scan:
    """Checks every committed line of `data`: envelope, hash, JSON, record schema, seq continuity, `prev` links,
    one `ledger_id`, genesis first and only first; `full` also requires each record to be in canonical form."""
    cut = data.rfind(b"\n") + 1
    out = Scan(tail_bytes=len(data) - cut)
    lines = data[:cut].split(b"\n")[:-1] if cut else []
    out.n_lines = len(lines)
    prev_hash: str | None = GENESIS_PREV
    prev_seq: int | None = -1
    for i, raw in enumerate(lines, start=1):
        ln = _split(raw)
        if ln.error is not None:
            out.add(i, None, *ln.error)
            if stop_at_first:
                return out
            prev_hash, prev_seq = ln.hash, None
            continue
        assert ln.record is not None and ln.hash is not None
        seq = ln.record.get("seq") if isinstance(ln.record.get("seq"), int) else None
        n_before = out.n_problems
        if hashlib.sha256(ln.body).hexdigest() != ln.hash:
            out.add(i, seq, "hash_mismatch", "the record's bytes do not hash to the line's hash (record altered)")
        if full:
            try:
                canon = canonical_json(ln.record).encode("utf-8")
            except (TypeError, ValueError) as e:
                canon = None
                out.add(i, seq, "not_canonical", f"record is not canonical JSON: {e}")
            if canon is not None and canon != ln.body:
                out.add(i, seq, "not_canonical", "record bytes are not in canonical JSON form (RFC 8785)")
        try:
            rec = LedgerRecord.model_validate(ln.record)
        except ValidationError as e:
            first = e.errors(include_url=False)[0] if e.errors() else {}
            loc = ".".join(str(x) for x in first.get("loc", ()))
            out.add(i, seq, "invalid_record", f"{loc + ': ' if loc else ''}{first.get('msg', 'invalid record')}")
            if stop_at_first:
                return out
            prev_hash, prev_seq = ln.hash, seq
            continue
        if prev_seq is not None and rec.seq != prev_seq + 1:
            out.add(i, rec.seq, "seq_gap", f"seq {rec.seq} follows seq {prev_seq} (records removed, repeated or moved)")
        if prev_hash is not None and rec.prev != prev_hash:
            out.add(i, rec.seq, "broken_link", "prev does not match the previous line's hash (history changed)")
        if (i == 1) != (rec.kind == "genesis"):
            what = "the first record is not the genesis record" if i == 1 else "a genesis record after line 1"
            out.add(i, rec.seq, "genesis", what)
        if out.ledger_id is None:
            out.ledger_id = rec.ledger_id
        elif rec.ledger_id != out.ledger_id:
            out.add(i, rec.seq, "ledger_id_mismatch", f"ledger_id {rec.ledger_id} != {out.ledger_id} (spliced)")
        if stop_at_first and out.n_problems > n_before:
            return out
        out.records.append(rec)
        out.hashes.append(ln.hash)
        out.line_of.setdefault(rec.seq, i)
        prev_hash, prev_seq = ln.hash, rec.seq
    return out


def resolve_location(location: str | os.PathLike[str]) -> Path:
    """The ledger file of `location`: a `.jsonl` file as given, otherwise `<location>/ledger.jsonl`."""
    p = Path(location).expanduser()
    if p.suffix == ".jsonl" or p.is_file():
        return p
    return p / LEDGER_FILE


class Ledger:
    """A ledger file. `location` is a directory (holding `ledger.jsonl`) or a `.jsonl` file."""

    def __init__(self, location: str | os.PathLike[str], *, ignore_in_git: bool = False) -> None:
        self.path = resolve_location(location)
        self._ignore_in_git = ignore_in_git

    def __repr__(self) -> str:
        return f"Ledger({str(self.path)!r})"

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + LOCK_SUFFIX)

    def exists(self) -> bool:
        """The ledger file exists and holds at least one byte."""
        try:
            return self.path.stat().st_size > 0
        except OSError:
            return False

    # ---- writing ----------------------------------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        d = self.directory
        if d.is_dir():
            return
        try:
            d.mkdir(parents=True, exist_ok=True)
            if self._ignore_in_git:
                (d / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        except OSError as e:
            raise QtError(
                "QT-LEDGER-002",
                f"{d}: cannot create the ledger directory: {e.strerror or e}",
                "set QT_LEDGER_DIR to a writable directory, or QT_LEDGER=off",
            ) from None

    def append(self, record: LedgerRecord, blobs: Mapping[str, bytes] | None = None) -> LedgerRecord:
        """Appends `record` (its `ledger_id`, `seq`, `prev` and `recorded_at_ns` are set here) under the lock and
        fsyncs, after storing the series files it references (`blobs`); writes the genesis record first into a
        new ledger. Returns the record as stored."""
        return self.append_many([Draft(record, blobs or {})])[0]

    def append_many(self, drafts: Sequence[Draft | LedgerRecord]) -> list[LedgerRecord]:
        """Appends several records in order under one lock with one write and one fsync (a batch of variants).
        Every record is validated before anything is written; `QT-CONFIG-001` for an invalid record or a
        `genesis` record (the ledger writes its own)."""
        items = [d if isinstance(d, Draft) else Draft(d, {}) for d in drafts]
        checked: list[tuple[dict[str, Any], Mapping[str, bytes]]] = []
        for rec, blobs in items:
            if not isinstance(rec, LedgerRecord):
                raise QtError("QT-CONFIG-001", f"ledger append: expected a LedgerRecord, got {type(rec).__name__}")
            if rec.kind == "genesis":
                raise QtError("QT-CONFIG-001", "ledger append: the genesis record is written by the ledger itself")
            doc = rec.model_dump(mode="json")
            _revalidate(doc)  # model_copy and attribute assignment skip validation
            if rec.series is not None and rec.series.sha256 not in blobs:
                if _series.check(self.directory, rec.series) is not None:
                    raise QtError("QT-CONFIG-001", f"ledger append: series {rec.series.sha256[:12]} is not stored")
            checked.append((doc, blobs))
        if not checked:
            return []
        self._ensure_dir()
        for _, blobs in checked:
            for sha, blob in blobs.items():
                _series.write(self.directory, sha, blob)
        stored: list[LedgerRecord] = []
        with exclusive(self.lock_path):
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
            except OSError as e:
                raise QtError("QT-LEDGER-002", f"{self.path}: cannot open the ledger: {e.strerror or e}") from None
            try:
                last = self._last_committed(fd)
                lines: list[bytes] = []
                if last is None:
                    genesis = LedgerRecord(
                        ledger_id=secrets.token_hex(16),
                        seq=0,
                        prev=GENESIS_PREV,
                        kind="genesis",
                        status="ok",
                        code=items[0].record.code,
                        recorded_at_ns=now_ns(),
                    )
                    line, h = encode_line(genesis.model_dump(mode="json"))
                    lines.append(line)
                    last = (0, h, genesis.ledger_id)
                seq, prev, ledger_id = last
                for doc, _ in checked:
                    seq += 1
                    rec = LedgerRecord.model_validate(
                        {**doc, "ledger_id": ledger_id, "seq": seq, "prev": prev, "recorded_at_ns": now_ns()}
                    )
                    line, prev = encode_line(rec.model_dump(mode="json"))
                    lines.append(line)
                    stored.append(rec)
                self._write(fd, b"".join(lines))
            finally:
                os.close(fd)
        return stored

    def _write(self, fd: int, data: bytes) -> None:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        except OSError as e:
            raise QtError("QT-LEDGER-002", f"{self.path}: cannot append: {e.strerror or e}") from None

    def _last_committed(self, fd: int) -> tuple[int, str, str] | None:
        """(seq, hash, ledger_id) of the last committed record, None for an empty file. An uncommitted tail is
        committed when it is a complete record that extends the chain (only its newline was lost), else moved to
        `torn/` and cut off (it was never a record)."""
        size = os.fstat(fd).st_size
        if size == 0:
            return None
        nl = _rfind_newline(fd, size)
        if nl + 1 < size:
            tail = _pread(fd, size - nl - 1, nl + 1)
            if nl < 0 and not tail.startswith(_PREFIX[: len(tail)]):
                raise QtError(
                    "QT-LEDGER-001",
                    f"{self.path}: not a ledger (the file does not start with a ledger line); refusing to write to it",
                    "point QT_LEDGER_DIR or --ledger at a directory or a .jsonl ledger",
                )
            before = self._parse_last(fd, nl + 1) if nl >= 0 else None
            ln = _split(tail)
            whole = ln.error is None and ln.hash == hashlib.sha256(ln.body).hexdigest()
            rec = ln.record or {}
            extends = whole and (
                (before is None and rec.get("seq") == 0 and rec.get("prev") == GENESIS_PREV)
                or (before is not None and rec.get("seq") == before[0] + 1 and rec.get("prev") == before[1])
            )
            if extends:
                os.write(fd, b"\n")
                os.fsync(fd)
                return int(rec["seq"]), str(ln.hash), str(rec.get("ledger_id"))
            self._set_aside(tail, nl + 1)
            os.ftruncate(fd, nl + 1)
            os.fsync(fd)
            return before
        return self._parse_last(fd, size)

    def _parse_last(self, fd: int, end: int) -> tuple[int, str, str] | None:
        """(seq, hash, ledger_id) of the committed line ending at `end` (its newline at `end - 1`)."""
        if end <= 0:
            return None
        start = _rfind_newline(fd, end - 1) + 1
        ln = _split(_pread(fd, end - 1 - start, start))
        rec = ln.record or {}
        seq, lid = rec.get("seq"), rec.get("ledger_id")
        if ln.error is not None or not isinstance(seq, int) or not isinstance(lid, str):
            reason = ln.error[1] if ln.error else "no seq / ledger_id"
            raise QtError(
                "QT-LEDGER-001",
                f"{self.path}: the last record cannot be read ({reason}); refusing to extend a broken chain",
                "run `queuetruth ledger verify` and restore the file, or start a new ledger (QT_LEDGER_DIR)",
            )
        return seq, str(ln.hash), lid

    def _set_aside(self, tail: bytes, offset: int) -> None:
        d = self.directory / TORN_DIR
        d.mkdir(exist_ok=True)
        name = f"{offset}-{hashlib.sha256(tail).hexdigest()[:12]}.bin"
        (d / name).write_bytes(tail)
        log.warning(
            "ledger: %d byte(s) of an append that was cut off at offset %d moved to %s", len(tail), offset, d / name
        )

    # ---- reading ----------------------------------------------------------------------------------------------

    def _read(self) -> bytes:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            data = b""
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{self.path}: cannot read the ledger: {e.strerror or e}") from None
        if not data:
            raise QtError(
                "QT-INPUT-001",
                f"no ledger at {self.path}",
                "nothing has been recorded there yet; QT_LEDGER_DIR or --ledger selects another ledger",
            )
        return data

    def snapshot(self) -> tuple[Scan, bytes]:
        """(every committed record, each hash- and link-checked, and the committed bytes they were read from),
        from one read of the file; `QT-LEDGER-001` at the first problem."""
        data = self._read()
        out = scan_bytes(data, full=False, stop_at_first=True)
        if out.problems:
            p = out.problems[0]
            raise QtError(
                "QT-LEDGER-001",
                f"{self.path}: line {p.line}{f' (seq {p.seq})' if p.seq is not None else ''}: {p.reason}: {p.message}",
                "run `queuetruth ledger verify` for every problem; the ledger is not trusted until it passes",
                details={"problem": p.model_dump(mode="json")},
            )
        if not out.records:
            raise QtError("QT-LEDGER-001", f"{self.path}: no committed record (the genesis record is missing)")
        return out, data[: data.rfind(b"\n") + 1]

    def scan(self) -> Scan:
        """Every committed record, each hash- and link-checked; `QT-LEDGER-001` at the first problem."""
        return self.snapshot()[0]

    def records(self) -> list[LedgerRecord]:
        """Every committed record, oldest first (genesis included), checked as by `scan`."""
        return self.scan().records

    def head(self) -> Head:
        """The last committed record's `seq` and hash."""
        h = self.scan().head
        assert h is not None
        return h

    def verify(self, anchor: str | None = None) -> LedgerVerification:
        """Full integrity check (never raises on a problem; `QT-INPUT-001` for a missing ledger). `anchor` is
        `"<seq>:<hash>"` or a bare hash noted earlier: the history up to it must still be the same."""
        out = scan_bytes(self._read(), full=True, stop_at_first=False)
        seen: set[str] = set()
        for rec in out.records:
            if rec.series is None or rec.series.sha256 in seen:
                continue
            seen.add(rec.series.sha256)
            bad = _series.check(self.directory, rec.series)
            if bad is not None:
                out.add(out.line_of.get(rec.seq, 0), rec.seq, *bad)
        anchor_ok: bool | None = None
        if anchor is not None:
            anchor_ok = _check_anchor(out, anchor)
            if not anchor_ok:
                out.add(0, None, "anchor_mismatch", f"anchor {anchor} is not a record of this ledger (history changed)")
        if out.n_lines == 0:
            out.add(1, None, "genesis", "no committed record (the genesis record is missing)")
        return LedgerVerification(
            ledger=str(self.path),
            ok=out.n_problems == 0,
            ledger_id=out.ledger_id,
            n_records=out.n_lines,
            head=out.head,
            uncommitted_tail_bytes=out.tail_bytes,
            anchor=anchor,
            anchor_ok=anchor_ok,
            problems=out.problems,
            n_problems=out.n_problems,
        )


def _revalidate(doc: dict[str, Any]) -> None:
    try:
        LedgerRecord.model_validate(doc)
    except ValidationError as e:
        first = e.errors(include_url=False)[0] if e.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        raise QtError("QT-CONFIG-001", f"ledger record: {loc + ': ' if loc else ''}{first.get('msg', '')}") from None


def parse_anchor(anchor: str) -> tuple[int | None, str]:
    """(`seq` or None, hash) of `"<seq>:<hash>"` or `"<hash>"`; `QT-CONFIG-001` otherwise."""
    seq_s, sep, h = anchor.strip().rpartition(":")
    h = h.lower()
    ok_hash = len(h) == 64 and set(h.encode()) <= _HEX
    ok_seq = not sep or (seq_s.isdigit() and seq_s.isascii())
    if not (ok_hash and ok_seq):
        raise QtError("QT-CONFIG-001", f"anchor: {anchor!r} is not '<seq>:<64-hex hash>' or '<64-hex hash>'")
    return (int(seq_s) if sep else None), h


def _check_anchor(scan: Scan, anchor: str) -> bool:
    seq, h = parse_anchor(anchor)
    for rec, rh in zip(scan.records, scan.hashes, strict=True):
        if rh == h:
            return seq is None or rec.seq == seq
    return False


def _pread(fd: int, n: int, offset: int) -> bytes:
    if n <= 0:
        return b""
    if hasattr(os, "pread"):
        out = bytearray()
        while len(out) < n:
            chunk = os.pread(fd, n - len(out), offset + len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)
    os.lseek(fd, offset, os.SEEK_SET)  # pragma: no cover - Windows
    return os.read(fd, n)  # pragma: no cover - Windows


def _rfind_newline(fd: int, end: int) -> int:
    """Offset of the last newline before `end`, -1 if none."""
    pos = end
    while pos > 0:
        start = max(0, pos - _CHUNK)
        i = _pread(fd, pos - start, start).rfind(b"\n")
        if i >= 0:
            return start + i
        pos = start
    return -1
