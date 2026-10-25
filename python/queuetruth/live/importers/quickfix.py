"""QuickFIX engine files as the engines write them: FileLog message / event logs (current, backup and
logrotate-rotated, optionally gzip-compressed) and FileStore body / header / sequence-number files, for QuickFIX
C++ (and its Python and Ruby bindings), QuickFIX/J, QuickFIX/n and QuickFIX/Go.

Formats, from the engines' sources (fetched 2026-10-18; commit ids pinned):

- QuickFIX C++ `src/C++/FileLog.{h,cpp}` (quickfix/quickfix 386ce46e, same in tag v1.15.1 which the `quickfix`
  Python package wraps): files `<prefix>.messages.current.log` / `<prefix>.event.current.log` with
  `<prefix> = BeginString-SenderCompID-TargetCompID[-SessionQualifier]`; `backup()` renames them to
  `<prefix>.messages.backup.<n>.log` / `.event.backup.<n>.log` (n = 1, 2, ...: higher is newer). Every line is
  `UtcTimeStampConvertor::convert(now, 9) + " : " + message` (`YYYYMMDD-HH:MM:SS.nnnnnnnnn : `). Releases up to
  v1.14.4 write `YYYYMMDD-HH:MM:SS : ` or, with `FileLogMillisecondsInTimeStamp`, `.sss`. Incoming and outgoing
  messages are written the same way (direction comes from the CompIDs). `FileStore.cpp`: `<prefix>.body` holds
  the sent messages back to back, `<prefix>.header` the index as `"%seq,%offset,%size "` records,
  `<prefix>.seqnums` `"%020llu : %020llu"` (next sender / target MsgSeqNum; older releases `%010d`), and
  `<prefix>.session` the creation time.
- QuickFIX/J `quickfixj-core/.../FileLog.java`, `FileLogFactory.java`, `FileStore.java`,
  `quickfixj-base/.../FileUtil.java` (quickfix-j/quickfixj 149869e8): files `<prefix>.messages.log` /
  `<prefix>.event.log`, `<prefix>` = `FileUtil.sessionIdFileName` (BeginString-Sender[_SubID][_LocationID]-
  Target[_SubID][_LocationID][-Qualifier]). Message lines carry no time stamp unless
  `FileIncludeTimeStampForMessages=Y` (default N); the stamp is `yyyyMMdd-HH:mm:ss` or, with
  `FileIncludeMilliseconds=Y`, `yyyyMMdd-HH:mm:ss.SSS`, followed by `": "` (no space before the colon). Event lines
  always carry the stamp. FileStore / CachedFileStore: `.body` as above, `.header` as big-endian binary records
  (`int32 seq, int64 offset, int32 size`), `.senderseqnums` / `.targetseqnums` written with `writeUTF`.
- QuickFIX/n `QuickFIXn/Logger/FileLog.cs`, `QuickFIXn/Store/FileStore.cs` (connamara/quickfixn 03081287):
  `<prefix>.messages.current.log` / `.event.current.log`, lines
  `DateTimeConverter.ToFIX(DateTime.UtcNow, TimePrecision.Millisecond) + " : " + msg` (`yyyyMMdd-HH:mm:ss.fff : `,
  CRLF on Windows); `.header` text lines `seq,offset,size`; `.seqnums` `D20 : D20`.
- QuickFIX/Go `log/file/file_log.go`, `log/file/file_util.go`, `store/file/file_store.go` (quickfixgo/quickfix
  eff945dd): `<prefix>.messages.current.log` / `.event.current.log` written by Go's `log.Logger` with
  `Ldate|Ltime|Lmicroseconds|LUTC` (`YYYY/MM/DD HH:MM:SS.uuuuuu ` then the message); `.header` lines
  `%d,%d,%d\\n`; `.senderseqnums` / `.targetseqnums` `%019d`.

Every engine stamps UTC. A line whose prefix is not one of these (another logger, `fix.local_ts_regex`) is read
by the importer's generic rules (`queuetruth.live.importers.fix44`). One message per line; a message whose data
field contains a newline continues on the next lines and is re-joined by its BodyLength.
"""

from __future__ import annotations

import gzip
import io
import re
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from queuetruth.errors import QtError
from queuetruth.live.importers._parquet import MAX_RATIO, SLACK
from queuetruth.live.importers.fixwire import SOH, Framed, detect_delimiter, fix_int, frame, normalise, utc_ts

#: File kinds recognised by name.
LOG_KINDS = ("messages_log", "event_log")
STORE_KINDS = ("store_body", "store_header", "store_seqnums", "store_senderseqnums", "store_targetseqnums")

_ROT = r"(?P<rot>(?:[.-][A-Za-z0-9_.-]*)?)"
_NAME_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^(?P<stem>.+)\.messages\.current\.log" + _ROT + r"$"), "messages_log", "current"),
    (re.compile(r"^(?P<stem>.+)\.messages\.backup\.(?P<n>\d+)\.log" + _ROT + r"$"), "messages_log", "backup"),
    (re.compile(r"^(?P<stem>.+)\.messages\.log" + _ROT + r"$"), "messages_log", "quickfixj"),
    (re.compile(r"^(?P<stem>.+)\.event\.current\.log" + _ROT + r"$"), "event_log", "current"),
    (re.compile(r"^(?P<stem>.+)\.event\.backup\.(?P<n>\d+)\.log" + _ROT + r"$"), "event_log", "backup"),
    (re.compile(r"^(?P<stem>.+)\.event\.log" + _ROT + r"$"), "event_log", "quickfixj"),
    (re.compile(r"^(?P<stem>.+)\.body$"), "store_body", "store"),
    (re.compile(r"^(?P<stem>.+)\.header$"), "store_header", "store"),
    (re.compile(r"^(?P<stem>.+)\.seqnums$"), "store_seqnums", "store"),
    (re.compile(r"^(?P<stem>.+)\.senderseqnums$"), "store_senderseqnums", "store"),
    (re.compile(r"^(?P<stem>.+)\.targetseqnums$"), "store_targetseqnums", "store"),
    (re.compile(r"^(?P<stem>.+)\.session$"), "store_session", "store"),
)
_STEM = re.compile(r"^(?P<begin>FIXT?\.\d+\.\d+)-(?P<rest>.+)$")

#: Line prefixes: (format id, regex). The time is group `ts`; the message follows the match.
PREFIXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("quickfix", re.compile(r"^(?P<ts>\d{8}-\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?) : ")),
    ("quickfixj", re.compile(r"^(?P<ts>\d{8}-\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?): ")),
    ("quickfixgo", re.compile(r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,9})?) ")),
)
_GO_TS = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)$")


@dataclass(frozen=True)
class SessionHint:
    """Session id parsed from a QuickFIX file name (`stem` = the full prefix). `sender` / `target` are the name
    parts (may carry `_SubID_LocationID` suffixes); None when the stem cannot be split unambiguously."""

    stem: str
    begin: str | None
    sender: str | None
    target: str | None
    qualifier: str | None


@dataclass(frozen=True)
class SourceFile:
    """One input file classified by name."""

    path: str
    kind: str  # messages_log, event_log, store_*, or "messages" (unrecognised name: read as a message log)
    naming: str  # current, backup, quickfixj, store, generic
    hint: SessionHint | None
    backup_n: int | None


def parse_stem(stem: str) -> SessionHint:
    """`FIX.4.4-SENDER-TARGET[-QUALIFIER]` -> hint (sender/target None when ambiguous, e.g. CompIDs with `-`)."""
    m = _STEM.match(stem)
    if not m:
        return SessionHint(stem, None, None, None, None)
    parts = m.group("rest").split("-")
    if len(parts) == 2:
        return SessionHint(stem, m.group("begin"), parts[0], parts[1], None)
    if len(parts) == 3:
        return SessionHint(stem, m.group("begin"), parts[0], parts[1], parts[2])
    return SessionHint(stem, m.group("begin"), None, None, None)


def classify(path: str) -> SourceFile:
    """Classifies a file by its QuickFIX name (gzip suffix ignored)."""
    name = Path(path).name
    if name.endswith(".gz"):
        name = name[:-3]
    for rx, kind, naming in _NAME_RULES:
        m = rx.match(name)
        if m:
            n = m.groupdict().get("n")
            return SourceFile(path, kind, naming, parse_stem(m.group("stem")), fix_int(n) if n else None)
    return SourceFile(path, "messages", "generic", None, None)


def expand_inputs(paths: list[str]) -> list[str]:
    """Input paths with directories (a `FileLogPath` / `FileStorePath`) expanded to the QuickFIX files inside
    (recursively, sorted), and the `.header` next to every `.body` added. A directory without QuickFIX files fails
    `QT-INPUT-001`."""
    out: list[str] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            found = [
                str(f)
                for f in sorted(pp.rglob("*"))
                if f.is_file() and classify(str(f)).kind in (*LOG_KINDS, *STORE_KINDS)
            ]
            if not found:
                raise QtError(
                    "QT-INPUT-001", f"{p}: no QuickFIX log or store files (*.messages*.log, *.event*.log, *.body)"
                )
            out.extend(found)
        else:
            out.append(str(pp))
    for p in list(out):
        sf = classify(p)
        if sf.kind == "store_body":
            hdr = Path(p).with_name(Path(p).name[: -len(".body")] + ".header")
            if hdr.is_file() and str(hdr) not in out:
                out.append(str(hdr))
    seen: set[str] = set()
    uniq = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


#: A gzip-compressed input may expand to at most this many times its compressed size (plus `GZIP_SLACK` bytes):
#: the policy of `_parquet.MAX_RATIO`. Synthetic QuickFIX logs compress about 10.5x (gzip level 6) to 11.8x (level
#: 9), heartbeat-only and order flow alike (`test_gzip_limit_leaves_room_for_real_logs`); deflate's ceiling is about
#: 1032x, which a decompression bomb uses. There is no absolute cap: a 100 MB file may expand to 20 GB, and the
#: memory limit of the process (the service's import child) is the backstop for that.
GZIP_MAX_RATIO = MAX_RATIO
GZIP_SLACK = SLACK


def read_bytes(path: str) -> bytes | bytearray:
    """File bytes, gunzipped when the file is gzip-compressed (magic 1f 8b); `QT-INPUT-001` when unreadable or not
    valid gzip, `QT-INPUT-002` when it decompresses past `GZIP_MAX_RATIO` (a decompression bomb). Decompressed data
    is accumulated in one buffer and returned as is (no joined copy), so the peak is the decompressed size plus one
    chunk."""
    data: bytes | bytearray
    try:
        data = Path(path).read_bytes()
        if data[:2] == b"\x1f\x8b":
            limit = GZIP_MAX_RATIO * len(data) + GZIP_SLACK
            buf = bytearray()
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                while len(buf) <= limit:  # chunked: `read(limit)` would allocate the whole limit up front
                    chunk = g.read(1 << 20)
                    if not chunk:
                        break
                    buf += chunk
            if len(buf) > limit:
                raise QtError(
                    "QT-INPUT-002",
                    f"{path}: gzip input expands to more than {limit} bytes ({GZIP_MAX_RATIO}x its size): refused",
                )
            data = buf
    except (OSError, EOFError, zlib.error) as e:  # gzip.BadGzipFile is an OSError
        raise QtError("QT-INPUT-001", f"{path}: {e}") from e
    return data


def prefix_time(text: str) -> tuple[int, str] | None:
    """(UTC ns, precision) of a QuickFIX / QuickFIX/Go prefix time."""
    m = _GO_TS.match(text)
    if m:
        text = f"{m.group(1)}{m.group(2)}{m.group(3)}-{m.group(4)}"
    r = utc_ts(text)
    return None if r is None else (r[0], r[1])


def match_prefix(line: str) -> tuple[str, int, int, str] | None:
    """(format id, UTC ns, end of prefix, precision) of a QuickFIX line prefix, or None."""
    for fmt, rx in PREFIXES:
        m = rx.match(line)
        if m:
            t = prefix_time(m.group("ts"))
            if t is None:
                return None
            frac = m.group("ts").partition(".")[2]
            label = fmt
            if fmt == "quickfix":
                label = {0: "quickfix_cpp_s", 3: "quickfix_ms", 9: "quickfix_cpp_ns"}.get(len(frac), "quickfix")
            elif fmt == "quickfixj":
                label = "quickfixj_ms" if frac else "quickfixj_s"
            return label, t[0], m.end(), t[1]
    return None


@dataclass
class LogLine:
    """One framed message read from a log: SOH `text`, `problem` (see `fixwire.PROBLEMS`), the log-line time and
    its precision (None without a prefix), the line number, the prefix format id and the delimiter."""

    text: str
    problem: str | None
    local_ns: int | None
    local_prec: str | None
    line_no: int
    fmt: str
    pipe: bool


@dataclass
class LogStats:
    """Per-file reading counters."""

    formats: dict[str, int] = field(default_factory=dict)
    non_fix_lines: int = 0
    continued: int = 0
    ts_backwards: int = 0
    max_backwards_ns: int = 0


def iter_log(
    path: str, *, delimiter: str, regex: re.Pattern[str] | None, parse_regex_ts, stats: LogStats
) -> Iterator[LogLine]:
    """Framed messages of a FIX message log (any QuickFIX FileLog format, a plain one-message-per-line log, or a
    log described by `fix.local_ts_regex`, whose captured time `parse_regex_ts(text, location)` converts)."""
    data = read_bytes(path).decode("latin-1")
    lines = data.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    last_ts: int | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip("\r")
        line_no = i + 1
        i += 1
        start = line.find("8=FIX")
        if start < 0:
            if line.strip():
                stats.non_fix_lines += 1
            continue
        local: int | None = None
        prec: str | None = None
        fmt = "plain"
        if regex is not None:
            m = regex.search(line)
            if m is None:
                raise QtError("QT-INPUT-002", f"{path}:{line_no}: local_ts_regex does not match the log line")
            group = m.group("ts") if "ts" in regex.groupindex else m.group(1)
            local, prec = parse_regex_ts(group, f"{path}:{line_no}")
            fmt = "local_ts_regex"
        else:
            pm = match_prefix(line)
            if pm is not None and line.startswith("8=FIX", pm[2]):
                fmt, local, _, prec = pm
            elif start > 0:
                fmt = "unrecognised_prefix"
        if local is not None:
            if last_ts is not None and local < last_ts:
                stats.ts_backwards += 1
                stats.max_backwards_ns = max(stats.max_backwards_ns, last_ts - local)
            last_ts = local if last_ts is None else max(last_ts, local)
        stats.formats[fmt] = stats.formats.get(fmt, 0) + 1
        delim = detect_delimiter(line[start:], delimiter)
        pipe = delim == "|"
        s = normalise(line[start:], delim)
        pos = 0
        while True:
            fr: Framed = frame(s, pos, pipe_delims=pipe)
            extra = 0
            while fr.problem == "truncated" and i < n and extra < 64:
                nxt = lines[i].rstrip("\r")
                if nxt.lstrip().startswith("8=FIX") or match_prefix(nxt) is not None:
                    break
                if regex is not None and regex.search(nxt) is not None and "8=FIX" in nxt:
                    break
                s = s + "\n" + normalise(nxt, delim)
                i += 1
                extra += 1
                stats.continued += 1
                fr = frame(s, pos, pipe_delims=pipe)
            yield LogLine(fr.text, fr.problem, local, prec, line_no, fmt, pipe)
            nxt_start = s.find("8=FIX", fr.end)
            if fr.problem in ("truncated", "malformed") or nxt_start < 0:
                break
            pos = nxt_start


def first_time(path: str) -> int | None:
    """Sort key of a message log: the first line's prefix time, else the first message's SendingTime(52)."""
    try:
        data = read_bytes(path).decode("latin-1")
    except QtError:
        return None
    for line in data.split("\n"):
        start = line.find("8=FIX")
        if start < 0:
            continue
        pm = match_prefix(line)
        if pm is not None:
            return pm[1]
        m = re.search(r"(?:\x01|\||\^A)52=([0-9:.\-]+)", line)
        if m:
            t = utc_ts(m.group(1))
            if t is not None:
                return t[0]
        return None
    return None


@dataclass
class StoreRead:
    """Messages of a FileStore `.body`: (MsgSeqNum from the index or None, framed message, byte offset)."""

    messages: list[tuple[int | None, Framed, int]]
    header_format: str  # text_space (C++), text_lines (QuickFIX/n, QuickFIX/Go), binary (QuickFIX/J), none
    index_mismatches: int = 0


#: Store index and sequence-number records. A match starts only where a digit run starts (the lookbehind): from
#: inside a run, the engine would rescan the rest of the run at every position, quadratic in a long digit run with
#: no separator (a 40 000-digit `.header` took 13 s).
_INDEX_RECORD = re.compile(rb"(?<![0-9])([0-9]+),([0-9]+),([0-9]+)")
_SEQNUMS = re.compile(rb"(?<![0-9])([0-9]+)[ \t\r\n\f\v]*:[ \t\r\n\f\v]*([0-9]+)")


def _parse_header(data: bytes | bytearray) -> tuple[str, list[tuple[int, int, int]], int]:
    """(format, `(seq, offset, size)` records, unusable records) of a FileStore index."""
    if not data:
        return "none", [], 0
    if all(c in b"0123456789,\r\n\t " for c in data):
        recs = []
        bad = 0
        for a, b, c in _INDEX_RECORD.findall(data):
            seq, off, size = (fix_int(x.decode("ascii")) for x in (a, b, c))
            if seq is None or off is None or size is None:
                bad += 1  # a number of 21+ digits points at no message: counted as an index mismatch
            else:
                recs.append((seq, off, size))
        return ("text_lines" if b"\n" in data else "text_space"), recs, bad
    if len(data) % 16 == 0:
        return "binary", [(s, o, z) for s, o, z in struct.iter_unpack(">iqi", data)], 0
    return "unparseable", [], 0


def read_store(body_path: str, header_path: str | None) -> StoreRead:
    """Sent messages of a FileStore (QuickFIX C++, /J, /n, /Go). The index (`.header`) gives MsgSeqNum and the
    byte range; later index records for a sequence number win (resends overwrite). Without a usable index the
    body is scanned by BodyLength."""
    body = read_bytes(body_path).decode("latin-1")
    fmt, recs, mismatches = ("none", [], 0)
    if header_path is not None and Path(header_path).is_file():
        fmt, recs, mismatches = _parse_header(read_bytes(header_path))
    out: list[tuple[int | None, Framed, int]] = []
    if recs:
        by_seq: dict[int, tuple[int, int]] = {}
        for seq, off, size in recs:
            by_seq[seq] = (off, size)
        for seq, (off, size) in sorted(by_seq.items(), key=lambda kv: kv[1][0]):
            chunk = body[off : off + size] if off >= 0 and size > 0 and seq >= 0 else ""  # never a slice from the end
            if not chunk.startswith("8=") or len(chunk) != size:
                mismatches += 1
                continue
            out.append((seq, frame(chunk, 0), off))
        if out or not mismatches:
            return StoreRead(out, fmt, mismatches)
        out = []
    pos = body.find("8=FIX")
    while pos >= 0:
        fr = frame(body, pos)
        out.append((None, fr, pos))
        if fr.problem in ("truncated", "malformed"):
            break
        pos = body.find("8=FIX", fr.end)
    return StoreRead(out, fmt if fmt != "unparseable" else "unparseable", mismatches)


def read_seqnums(path: str, kind: str) -> tuple[int | None, int | None]:
    """(next sender MsgSeqNum, next target MsgSeqNum) from a FileStore sequence-number file (`.seqnums` holds
    both; `.senderseqnums` / `.targetseqnums` one each)."""
    data = read_bytes(path)
    if kind == "store_seqnums":
        m = _SEQNUMS.search(data)
        return (fix_int(m.group(1).decode("ascii")), fix_int(m.group(2).decode("ascii"))) if m else (None, None)
    m = re.search(rb"\d+", data)
    v = fix_int(m.group(0).decode("ascii")) if m else None
    return (v, None) if kind == "store_senderseqnums" else (None, v)


#: Event-log line categories, first match wins (case-insensitive substrings of the engines' event texts).
EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invalid_message", ("invalid message", "garbled")),
    ("seq_reset", ("resetseqnumflag", "resetting sequence numbers", "reseting sequence numbers", "sequence reset")),
    ("seq_too_high", ("msgseqnum too high",)),
    ("seq_too_low", ("msgseqnum too low",)),
    ("resend_request", ("resendrequest", "resend request", "resending")),
    ("logon", ("logon",)),
    ("logout", ("logout",)),
    ("disconnect", ("disconnect",)),
    ("reject", ("rejected", "reject")),
    ("timeout", ("timed out", "timeout")),
)


def read_event_log(path: str) -> tuple[dict[str, int], int]:
    """(category -> count, lines) of a QuickFIX event log (`EVENT_RULES`; other lines count as `other`)."""
    text = read_bytes(path).decode("latin-1")
    counts: dict[str, int] = {}
    lines = 0
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        lines += 1
        pm = match_prefix(line)
        body = line[pm[2] :] if pm is not None else line
        low = body.lower()
        cat = "other"
        for name, keys in EVENT_RULES:
            if any(k in low for k in keys):
                cat = name
                break
        counts[cat] = counts.get(cat, 0) + 1
    return counts, lines


__all__ = [
    "LOG_KINDS",
    "PREFIXES",
    "SOH",
    "STORE_KINDS",
    "LogLine",
    "LogStats",
    "SessionHint",
    "SourceFile",
    "StoreRead",
    "classify",
    "expand_inputs",
    "first_time",
    "iter_log",
    "match_prefix",
    "parse_stem",
    "prefix_time",
    "read_bytes",
    "read_event_log",
    "read_seqnums",
    "read_store",
]
