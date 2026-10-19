"""Synthetic FIX sessions written in the exact file formats of the QuickFIX engines (test data only; no real data).

`FixSession` builds a client-side order-entry conversation (outbound requests, inbound reports) with correct
BodyLength(9) / CheckSum(10); `render_log` writes it as one engine's FileLog messages file, `render_store` as a
FileStore `.body` + `.header`, `render_events` as an event log. Formats (see `queuetruth.live.importers.quickfix`):

- `cpp_ns`: QuickFIX C++ >= 1.15 / Python binding, `YYYYMMDD-HH:MM:SS.nnnnnnnnn : <msg>`
- `cpp_s`, `cpp_ms`: QuickFIX C++ <= 1.14 (`FileLogMillisecondsInTimeStamp` N / Y), `... : <msg>`
- `qfj_plain`: QuickFIX/J default (no time stamp), `qfj_ms`: `FileIncludeTimeStampForMessages=Y`,
  `FileIncludeMilliseconds=Y`: `YYYYMMDD-HH:MM:SS.SSS: <msg>`
- `qfn`: QuickFIX/n, `YYYYMMDD-HH:MM:SS.fff : <msg>` with CRLF line ends
- `go`: QuickFIX/Go, Go `log` flags Ldate|Ltime|Lmicroseconds|LUTC: `YYYY/MM/DD HH:MM:SS.uuuuuu <msg>`
"""

from __future__ import annotations

import calendar
import struct
import time
from dataclasses import dataclass, field
from typing import Any

SOH = "\x01"
#: 2026-10-15 13:30:00 UTC in ns.
T0 = calendar.timegm((2026, 10, 15, 13, 30, 0, 0, 0, 0)) * 10**9


def at(ms: int, us: int = 0, ns: int = 0) -> int:
    """T0 + offset."""
    return T0 + ms * 10**6 + us * 10**3 + ns


def trunc(t: int, digits: int) -> int:
    """`t` truncated to `digits` fractional second digits."""
    q = 10 ** (9 - digits)
    return t - t % q


def fix_time(t: int, digits: int) -> str:
    """FIX UTCTimestamp with `digits` (0, 3, 6, 9) fractional digits (truncated)."""
    secs, frac = divmod(t, 10**9)
    y, mo, d, h, mi, s = time.gmtime(secs)[:6]
    base = f"{y:04d}{mo:02d}{d:02d}-{h:02d}:{mi:02d}:{s:02d}"
    return base if digits == 0 else base + "." + f"{frac:09d}"[:digits]


def go_time(t: int) -> str:
    secs, frac = divmod(t, 10**9)
    y, mo, d, h, mi, s = time.gmtime(secs)[:6]
    return f"{y:04d}/{mo:02d}/{d:02d} {h:02d}:{mi:02d}:{s:02d}.{frac:09d}"[:26]


def fix_message(begin: str, header: list[tuple[str, str]], body: list[tuple[str, str]], *, delim: str = SOH) -> str:
    """A complete FIX message with correct BodyLength and CheckSum (computed on the SOH form)."""
    inner = "".join(f"{t}={v}{SOH}" for t, v in [*header, *body])
    head = f"8={begin}{SOH}9={len(inner.encode('latin-1'))}{SOH}"
    cs = sum((head + inner).encode("latin-1")) % 256
    msg = f"{head}{inner}10={cs:03d}{SOH}"
    return msg if delim == SOH else msg.replace(SOH, delim)


@dataclass
class Rec:
    """One logged message: local log time, direction, MsgSeqNum, message text, type."""

    t: int
    out: bool
    seq: int
    text: str
    mt: str


@dataclass
class FixSession:
    """A client-side FIX session (`local` sends 35=D/F/G, receives 35=8/9)."""

    begin: str = "FIX.4.4"
    local: str = "QTCLIENT"
    remote: str = "SYNTHVENUE"
    appl_ver: str | None = None  # FIXT.1.1: ApplVerID(1128) of application messages
    sending_digits: int = 3
    records: list[Rec] = field(default_factory=list)
    out_seq: int = 0
    in_seq: int = 0

    def _header(self, mt: str, sender: str, target: str, seq: int, sending: int, extra: list[tuple[str, str]]):
        h = [("35", mt), ("49", sender), ("56", target), ("34", str(seq))]
        h += extra
        h.append(("52", fix_time(sending, self.sending_digits)))
        if self.appl_ver and mt not in ("0", "1", "2", "3", "4", "5", "A"):
            h.append(("1128", self.appl_ver))
        return h

    def out(
        self,
        t: int,
        mt: str,
        body: list[tuple[str, Any]],
        *,
        seq: int | None = None,
        possdup_of: int | None = None,
        log_delay_ns: int = 15_000,
    ) -> Rec:
        """Outbound message: SendingTime = `t`, logged at `t + log_delay_ns`."""
        if seq is None:
            self.out_seq += 1
            seq = self.out_seq
        extra = [("43", "Y"), ("122", fix_time(possdup_of, self.sending_digits))] if possdup_of is not None else []
        text = fix_message(
            self.begin,
            self._header(mt, self.local, self.remote, seq, t, extra),
            [(str(k), str(v)) for k, v in body],
        )
        r = Rec(t + log_delay_ns, True, seq, text, mt)
        self.records.append(r)
        return r

    def inn(
        self,
        t_log: int,
        mt: str,
        body: list[tuple[str, Any]],
        *,
        seq: int | None = None,
        possdup: bool = False,
        sending: int | None = None,
    ) -> Rec:
        """Inbound message received (logged) at `t_log`; venue SendingTime = `sending` (default t_log - 200 us)."""
        if seq is None:
            self.in_seq += 1
            seq = self.in_seq
        extra = [("43", "Y")] if possdup else []
        text = fix_message(
            self.begin,
            self._header(mt, self.remote, self.local, seq, sending or (t_log - 200_000), extra),
            [(str(k), str(v)) for k, v in body],
        )
        r = Rec(t_log, False, seq, text, mt)
        self.records.append(r)
        return r


def stem(s: FixSession) -> str:
    return f"{s.begin}-{s.local}-{s.remote}"


def log_line(r: Rec, engine: str, *, delim: str = SOH) -> str:
    """One FileLog line (with its line end)."""
    text = r.text if delim == SOH else r.text.replace(SOH, delim)
    if engine == "cpp_ns":
        return f"{fix_time(r.t, 9)} : {text}\n"
    if engine == "cpp_ms":
        return f"{fix_time(r.t, 3)} : {text}\n"
    if engine == "cpp_s":
        return f"{fix_time(r.t, 0)} : {text}\n"
    if engine == "qfj_plain":
        return f"{text}\n"
    if engine == "qfj_ms":
        return f"{fix_time(r.t, 3)}: {text}\n"
    if engine == "qfn":
        return f"{fix_time(r.t, 3)} : {text}\r\n"
    if engine == "go":
        return f"{go_time(r.t)} {text}\n"
    raise ValueError(engine)


#: Fractional digits of each engine's log time (None: no log time).
LOG_DIGITS: dict[str, int | None] = {
    "cpp_ns": 9,
    "cpp_ms": 3,
    "cpp_s": 0,
    "qfj_plain": None,
    "qfj_ms": 3,
    "qfn": 3,
    "go": 6,
}


def log_name(s: FixSession, engine: str) -> str:
    """The engine's messages-log file name."""
    return f"{stem(s)}.messages.log" if engine.startswith("qfj") else f"{stem(s)}.messages.current.log"


def event_name(s: FixSession, engine: str) -> str:
    return f"{stem(s)}.event.log" if engine.startswith("qfj") else f"{stem(s)}.event.current.log"


def render_log(records: list[Rec], engine: str, *, delim: str = SOH) -> bytes:
    return "".join(log_line(r, engine, delim=delim) for r in records).encode("latin-1")


def render_events(lines: list[tuple[int, str]], engine: str) -> bytes:
    """Event-log lines `(time, text)` in the engine's format (QuickFIX/J always stamps events)."""
    out = []
    for t, text in lines:
        if engine == "go":
            out.append(f"{go_time(t)} {text}\n")
        elif engine.startswith("qfj"):
            out.append(f"{fix_time(t, 3 if engine == 'qfj_ms' else 0)}: {text}\n")
        elif engine == "qfn":
            out.append(f"{fix_time(t, 3)} : {text}\r\n")
        else:
            out.append(f"{fix_time(t, LOG_DIGITS[engine] or 0)} : {text}\n")
    return "".join(out).encode("latin-1")


def render_store(records: list[Rec], engine: str) -> tuple[bytes, bytes]:
    """FileStore (`.body`, `.header`) of the outbound messages: C++ `seq,off,size ` text, QuickFIX/J binary
    big-endian (int32, int64, int32), QuickFIX/n and QuickFIX/Go `seq,off,size\\n` lines."""
    body = b""
    header = b""
    for r in records:
        if not r.out:
            continue
        data = r.text.encode("latin-1")
        off = len(body)
        if engine.startswith("qfj"):
            header += struct.pack(">iqi", r.seq, off, len(data))
        elif engine in ("qfn", "go"):
            header += f"{r.seq},{off},{len(data)}\n".encode()
        else:
            header += f"{r.seq},{off},{len(data)} ".encode()
        body += data
    return body, header


ACCT = "ACCT1"
SYM = "ACME"


def _er(
    s: FixSession,
    kind: str,
    *,
    oid: str,
    cid: str,
    side: str = "1",
    orig: str | None = None,
    qty: str,
    px: str,
    cum: str,
    leaves: str,
    tt: int,
    extra: list[tuple[str, Any]] = (),  # type: ignore[assignment]
    exec_id: str,
    last: tuple[str, str] | None = None,
    liq: str | None = None,
    ref: str | None = None,
    leaves_after_fill_zero: bool = False,
) -> list[tuple[str, Any]]:
    """ExecutionReport body in the session's FIX version (`kind`: new, fill, replace, cancel, reject, expire, done,
    bust)."""
    v42 = s.begin == "FIX.4.2"
    ordstatus = {"new": "0", "replace": "0", "cancel": "4", "reject": "8", "expire": "C", "done": "3"}.get(kind, "1")
    if kind == "fill":
        ordstatus = "2" if leaves == "0" else "1"
        et = ("2" if leaves == "0" else "1") if v42 else "F"
    elif kind == "bust":
        et = "1" if v42 else "H"
        ordstatus = "4"
    else:
        et = {"new": "0", "replace": "5", "cancel": "4", "reject": "8", "expire": "C", "done": "3"}[kind]
    b: list[tuple[str, Any]] = [("37", oid), ("11", cid)]
    if orig:
        b.append(("41", orig))
    b += [("17", exec_id)]
    if v42:
        b.append(("20", "1" if kind == "bust" else "0"))
    if ref:
        b.append(("19", ref))
    b += [("150", et), ("39", ordstatus), ("1", ACCT), ("55", SYM), ("54", side), ("38", qty), ("40", "2")]
    b += [("44", px)]
    if last:
        b += [("32", last[0]), ("31", last[1])]
    b += [("151", leaves), ("14", cum), ("6", px if cum != "0" else "0")]
    if liq and not v42:
        if s.begin == "FIXT.1.1":
            b.append(("1057", "N" if liq == "maker" else "Y"))
        else:
            b.append(("851", "1" if liq == "maker" else "2"))
    b += list(extra)
    b.append(("60", fix_time(tt, 6)))
    return b


def day_scenario(begin: str = "FIX.4.4") -> FixSession:
    """The standard test day (orders QT-1..QT-5; see python/tests/live/test_quickfix.py for the expected rows)."""
    s = FixSession(begin=begin, appl_ver="9" if begin == "FIXT.1.1" else None)
    logon = [("98", "0"), ("108", "30")] + ([("1137", "9")] if begin == "FIXT.1.1" else [])
    s.out(at(0), "A", logon)
    s.inn(at(2), "A", logon)
    nos = [("1", ACCT), ("55", SYM), ("40", "2")]
    # order 1: ack, partial fill (maker), replace qty down, cancel
    s.out(at(1000), "D", [("11", "QT-1"), *nos, ("54", "1"), ("38", "100"), ("44", "101.25"), ("59", "0")])
    s.inn(
        at(1000, 650, 123),
        "8",
        _er(
            s,
            "new",
            oid="OID-1",
            cid="QT-1",
            qty="100",
            px="101.25",
            cum="0",
            leaves="100",
            tt=at(1000, 400),
            exec_id="X-1-0",
        ),
    )
    s.inn(
        at(2000, 210),
        "8",
        _er(
            s,
            "fill",
            oid="OID-1",
            cid="QT-1",
            qty="100",
            px="101.25",
            cum="40",
            leaves="60",
            tt=at(2000, 10),
            exec_id="E-1-1",
            last=("40", "101.25"),
            liq="maker",
            extra=[("12", "0.40"), ("13", "3"), ("479", "USD")],
        ),
    )
    s.out(
        at(3000),
        "G",
        [
            ("11", "QT-1b"),
            ("41", "QT-1"),
            ("37", "OID-1"),
            *nos,
            ("54", "1"),
            ("38", "80"),
            ("44", "101.25"),
            ("59", "0"),
        ],
    )
    s.inn(
        at(3000, 520),
        "8",
        _er(
            s,
            "replace",
            oid="OID-1",
            cid="QT-1b",
            orig="QT-1",
            qty="80",
            px="101.25",
            cum="40",
            leaves="40",
            tt=at(3000, 300),
            exec_id="X-1-2",
        ),
    )
    s.out(at(4000), "F", [("11", "QT-1c"), ("41", "QT-1b"), ("37", "OID-1"), ("55", SYM), ("54", "1"), ("38", "80")])
    s.inn(
        at(4000, 530),
        "8",
        _er(
            s,
            "cancel",
            oid="OID-1",
            cid="QT-1c",
            orig="QT-1b",
            qty="80",
            px="101.25",
            cum="40",
            leaves="0",
            tt=at(4000, 310),
            exec_id="X-1-3",
        ),
    )
    # order 2: post-only reject before an OrderID exists
    s.out(at(5000), "D", [("11", "QT-2"), *nos, ("54", "2"), ("38", "50"), ("44", "101.50"), ("59", "0"), ("18", "6")])
    s.inn(
        at(5000, 470),
        "8",
        _er(
            s,
            "reject",
            oid="NONE",
            cid="QT-2",
            side="2",
            qty="50",
            px="101.50",
            cum="0",
            leaves="0",
            tt=at(5000, 250),
            exec_id="X-2-0",
            extra=[("103", "99"), ("58", "Post only order would take liquidity")],
        ),
    )
    # order 3: IOC, taker fill, remainder expires, fill busted later
    s.out(at(6000), "D", [("11", "QT-3"), *nos, ("54", "1"), ("38", "30"), ("44", "101.40"), ("59", "3")])
    s.inn(
        at(6000, 500),
        "8",
        _er(
            s,
            "new",
            oid="OID-3",
            cid="QT-3",
            qty="30",
            px="101.40",
            cum="0",
            leaves="30",
            tt=at(6000, 300),
            exec_id="X-3-0",
        ),
    )
    s.inn(
        at(6000, 501),
        "8",
        _er(
            s,
            "fill",
            oid="OID-3",
            cid="QT-3",
            qty="30",
            px="101.40",
            cum="10",
            leaves="20",
            tt=at(6000, 300),
            exec_id="E-3-1",
            last=("10", "101.40"),
            liq="taker",
        ),
    )
    s.inn(
        at(6000, 502),
        "8",
        _er(
            s,
            "expire",
            oid="OID-3",
            cid="QT-3",
            qty="30",
            px="101.40",
            cum="10",
            leaves="0",
            tt=at(6000, 300),
            exec_id="X-3-2",
        ),
    )
    # order 4: no TimeInForce (Day by default), cancel rejected, done for day later
    s.out(at(7000), "D", [("11", "QT-4"), *nos, ("54", "1"), ("38", "10"), ("44", "101.00")])
    s.inn(
        at(7000, 480),
        "8",
        _er(
            s,
            "new",
            oid="OID-4",
            cid="QT-4",
            qty="10",
            px="101.00",
            cum="0",
            leaves="10",
            tt=at(7000, 300),
            exec_id="X-4-0",
        ),
    )
    s.out(at(7500), "F", [("11", "QT-4b"), ("41", "QT-4"), ("37", "OID-4"), ("55", SYM), ("54", "1"), ("38", "10")])
    s.inn(
        at(7500, 490),
        "9",
        [
            ("37", "OID-4"),
            ("11", "QT-4b"),
            ("41", "QT-4"),
            ("39", "0"),
            ("434", "1"),
            ("102", "0"),
            ("58", "too late"),
            ("60", fix_time(at(7500, 300), 6)),
        ],
    )
    # order 5: GTC, cancelled by the venue without a request (self-trade prevention) -> expire
    s.out(at(8000), "D", [("11", "QT-5"), *nos, ("54", "1"), ("38", "5"), ("44", "100.90"), ("59", "1")])
    s.inn(
        at(8000, 470),
        "8",
        _er(
            s,
            "new",
            oid="OID-5",
            cid="QT-5",
            qty="5",
            px="100.90",
            cum="0",
            leaves="5",
            tt=at(8000, 300),
            exec_id="X-5-0",
        ),
    )
    s.inn(
        at(8500, 300),
        "8",
        _er(
            s,
            "cancel",
            oid="OID-5",
            cid="QT-5",
            qty="5",
            px="100.90",
            cum="0",
            leaves="0",
            tt=at(8500, 100),
            exec_id="X-5-1",
            extra=[("58", "self-trade prevention")],
        ),
    )
    # bust of order 3's fill, heartbeats, done for day of order 4
    s.inn(
        at(9000, 200),
        "8",
        _er(
            s,
            "bust",
            oid="OID-3",
            cid="QT-3",
            qty="30",
            px="101.40",
            cum="0",
            leaves="0",
            tt=at(9000),
            exec_id="E-3-1x",
            ref="E-3-1",
            last=("10", "101.40"),
            extra=[("58", "trade bust")],
        ),
    )
    s.out(at(9500), "0", [])
    s.inn(at(9600), "0", [])
    s.inn(
        at(10000, 200),
        "8",
        _er(
            s,
            "done",
            oid="OID-4",
            cid="QT-4",
            qty="10",
            px="101.00",
            cum="0",
            leaves="0",
            tt=at(10000),
            exec_id="X-4-3",
        ),
    )
    return s


def write(path: Any, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


IMAP = "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n"


def instrument_map(path: Any, venue: str = "fix44") -> str:
    return write(path, (IMAP + f"{venue},{SYM},201,,-4,0,0.01,1\n").encode())
