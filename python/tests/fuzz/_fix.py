"""FIX strategies: order-entry conversations (outbound 35=D/F/G, inbound 35=8/9/j/3, session messages) with tag
values from the FIX 4.2-5.0SP2 vocabulary the importer reads, MsgSeqNum gaps, resends, resets and regressions;
rendered as QuickFIX FileLog lines (every engine's prefix), FileStore body + index, sequence-number files, event
logs and data dictionaries, with corruption (bad CheckSum / BodyLength, truncation, data fields carrying SOH or a
newline, hostile prefixes) mixed in."""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass
from typing import Any

from fuzz._values import EDGE_DECIMALS, amounts
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy
from live._fixgen import SOH, T0, fix_message, fix_time, go_time

BEGINS = ["FIX.4.2", "FIX.4.4", "FIXT.1.1", "FIX.5.0SP2", "FIX.4.0"]
LOCAL, REMOTE = "QTCLIENT", "VENUE"
MSG_TYPES_OUT = ["D", "D", "F", "G", "q", "CA", "A", "0", "4", "5", "2", "3", "8"]
MSG_TYPES_IN = ["8", "8", "8", "8", "9", "j", "3", "A", "0", "4", "5", "1", "r", "BZ", "D", "AE", ""]
EXEC_TYPES = [
    "0",
    "1",
    "2",
    "F",
    "3",
    "4",
    "5",
    "6",
    "A",
    "E",
    "8",
    "C",
    "D",
    "G",
    "H",
    "I",
    "7",
    "9",
    "B",
    "Z",
    "TRADE",
]
ORD_STATUS = ["0", "1", "2", "3", "4", "5", "6", "8", "A", "C", "E", ""]
UTC_EDGE = [
    "20261015-13:30:00",
    "20261015-13:30:00.123",
    "20261015-13:30:00.123456",
    "20261015-13:30:00.123456789",
    "20261015-13:30:00.123456789012",
    "20261231-23:59:60",
    "00000101-00:00:00",
    "20261301-00:00:00",
    "20260230-00:00:00",
    "20260100-00:00:00",
    "20261015-24:00:00",
    "20261015-13:61:00",
    "20261015-13:30:61",
    "99991231-23:59:59.999999999",
    "2026-10-15T13:30:00Z",
    "",
    "now",
]


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def _num(hostile: bool) -> SearchStrategy[str]:
    edge = st.sampled_from(EDGE_DECIMALS).map(_latin1)
    return st.one_of(amounts, amounts, edge) if hostile else amounts


def _ts(hostile: bool) -> SearchStrategy[str]:
    good = st.builds(lambda t, d: fix_time(t, d), st.integers(T0 - 10**12, T0 + 10**12), st.sampled_from([0, 3, 6, 9]))
    return st.one_of(good, good, st.sampled_from(UTC_EDGE)) if hostile else good


def _seq_text(hostile: bool) -> SearchStrategy[str]:
    edge = ["0", "-1", "", "x", "1e3", "9" * 21, "9" * 5000, "18446744073709551615", "000000000000000000007"]
    return st.sampled_from(edge) if hostile else st.just("1")


@dataclass
class Msg:
    """One message of a generated conversation."""

    t: int  # local log time (ns)
    out: bool
    mt: str
    seq: int
    text: str


@st.composite
def conversation(draw: st.DrawFn, *, hostile: bool = True, max_messages: int = 24) -> list[Msg]:
    """A client-side session: requests and the venue's reports in log order, with sequence anomalies."""
    begin = draw(st.sampled_from(BEGINS))
    n = draw(st.integers(0, max_messages))
    cids = [f"C{i}" for i in range(4)]
    oids = ["OID-1", "OID-2", "NONE", "0", ""]
    execs = [f"X{i}" for i in range(6)]
    num, ts = _num(hostile), _ts(hostile)
    seq = {True: 0, False: 0}
    t = T0
    out: list[Msg] = []
    for _ in range(n):
        is_out = draw(st.booleans())
        mt = draw(st.sampled_from(MSG_TYPES_OUT if is_out else MSG_TYPES_IN))
        t += draw(st.integers(0, 10**9))
        # MsgSeqNum: next, a gap, a repeat (PossDup or not), a regression, a reset, or hostile text
        step = draw(st.sampled_from(["next"] * 8 + ["gap", "repeat", "back", "reset", "bad"]))
        possdup = False
        if step == "gap":
            seq[is_out] += draw(st.sampled_from([2, 3, 10, 10**6, 10**15]))
            seq_txt = str(seq[is_out])
        elif step == "repeat":
            possdup = draw(st.booleans())
            seq_txt = str(max(1, seq[is_out]))
        elif step == "back":
            seq_txt = str(max(1, seq[is_out] - draw(st.integers(1, 3))))
        elif step == "reset":
            seq[is_out] = 1
            seq_txt = "1"
        elif step == "bad" and hostile:
            seq_txt = draw(_seq_text(True))
        else:
            seq[is_out] += 1
            seq_txt = str(seq[is_out])
        sender, target = (LOCAL, REMOTE) if is_out else (REMOTE, LOCAL)
        if draw(st.integers(0, 15)) == 0:
            sender, target = target, sender  # acceptor-side or misrouted message
        header: list[tuple[str, str]] = [("35", mt), ("49", sender), ("56", target), ("34", seq_txt)]
        if possdup or draw(st.integers(0, 12)) == 0:
            header.append(("43", "Y"))
            if draw(st.booleans()):
                header.append(("122", draw(ts)))
        if draw(st.integers(0, 10)):
            header.append(("52", draw(ts)))
        body: list[tuple[str, str]] = []

        def maybe(tag: str, strat: SearchStrategy[Any], p: int = 4, body: list[tuple[str, str]] = body) -> None:
            if draw(st.integers(0, p)):
                body.append((tag, str(draw(strat))))

        cid = st.sampled_from(cids)
        if mt in ("D", "F", "G"):
            maybe("11", cid, 20)
            if mt in ("F", "G"):
                maybe("41", cid)
                maybe("37", st.sampled_from(oids), 2)
            maybe("1", st.sampled_from(["ACCT1", ""]), 2)
            maybe("55", st.sampled_from(["ACME", "XYZ", ""]), 20)
            maybe("54", st.sampled_from(["1", "2", "5", "9", ""]), 20)
            maybe("38", num, 20)
            maybe("40", st.sampled_from(["1", "2", "3", "4", "P"]))
            maybe("44", num)
            maybe("59", st.sampled_from(["0", "1", "3", "4", "5", "6", "9"]), 2)
            maybe("18", st.sampled_from(["6", "6 A", "A", ""]), 1)
            maybe("60", ts)
        elif mt == "8":
            maybe("37", st.sampled_from(oids), 20)
            maybe("11", cid, 20)
            maybe("41", cid, 1)
            maybe("17", st.sampled_from(execs), 20)
            maybe("19", st.sampled_from(execs), 1)
            maybe("20", st.sampled_from(["0", "1", "2", "3"]), 1)
            maybe("150", st.sampled_from(EXEC_TYPES), 20)
            maybe("39", st.sampled_from(ORD_STATUS), 4)
            maybe("1", st.sampled_from(["ACCT1"]), 2)
            maybe("55", st.sampled_from(["ACME", "XYZ"]), 20)
            maybe("48", st.sampled_from(["US0000001", ""]), 1)
            maybe("107", st.sampled_from(["ACME Corp"]), 1)
            maybe("54", st.sampled_from(["1", "2", "5"]), 20)
            for tag in ("38", "44", "32", "31", "14", "151", "6"):
                maybe(tag, num, 4)
            maybe("12", num, 1)
            maybe("13", st.sampled_from(["1", "2", "3"]), 1)
            maybe("479", st.sampled_from(["USD"]), 1)
            maybe("851", st.sampled_from(["1", "2", "3", ""]), 2)
            maybe("1057", st.sampled_from(["Y", "N", "x"]), 1)
            maybe("59", st.sampled_from(["0", "1", "3", "6"]), 2)
            maybe("18", st.sampled_from(["6", ""]), 1)
            maybe("40", st.sampled_from(["1", "2"]), 2)
            maybe("58", st.sampled_from(["post only would take", "STP", "", "Text with = sign"]), 1)
            maybe("103", st.sampled_from(["0", "3"]), 1)
            maybe("378", st.sampled_from(["1"]), 1)
            maybe("60", ts, 20)
            maybe("9001", st.sampled_from(["A", "R", "1"]), 1)
        elif mt == "9":
            maybe("11", cid, 20)
            maybe("41", cid, 20)
            maybe("37", st.sampled_from(oids))
            maybe("434", st.sampled_from(["1", "2", "3"]), 4)
            maybe("102", st.sampled_from(["0", "1"]), 1)
            maybe("58", st.sampled_from(["too late"]), 1)
            maybe("60", ts)
        elif mt in ("j", "3"):
            maybe("45", st.one_of(st.integers(0, 30).map(str), _seq_text(hostile)), 4)
            maybe("372", st.sampled_from(["D", "F", "G", "8", "Z"]), 4)
            maybe("379", cid, 2)
            maybe("380", st.sampled_from(["0", "3"]), 1)
            maybe("373", st.sampled_from(["1", "5"]), 1)
        elif mt == "4":
            maybe("36", st.one_of(st.integers(0, 40).map(str), _seq_text(hostile)), 20)
            maybe("123", st.sampled_from(["Y", "N"]), 2)
        elif mt == "A":
            maybe("98", st.just("0"), 2)
            maybe("108", st.just("30"), 2)
            maybe("141", st.sampled_from(["Y", "N"]), 1)
            maybe("1137", st.sampled_from(["9", "7", "X"]), 1)
        if hostile and draw(st.integers(0, 10)) == 0:  # a data field: declared length vs real length
            data = draw(st.sampled_from(["abc", "a\x01b", "line\nbreak", "", "x" * 40]))
            declared = draw(st.sampled_from([len(data), len(data) + 3, 0, 10**25]))
            body += [("95", str(declared)), ("96", data)]
        text = fix_message(begin, header, body)
        if hostile and draw(st.integers(0, 8)) == 0:  # corruption after framing
            kind = draw(st.sampled_from(["checksum", "bodylength", "byte", "truncate"]))
            if kind == "checksum":
                text = text[:-4] + f"{draw(st.integers(0, 999)):03d}" + SOH
            elif kind == "bodylength":
                i = text.index("9=") + 2
                j = text.index(SOH, i)
                text = text[:i] + draw(st.sampled_from(["0", "x", "99999", "9" * 30, ""])) + text[j:]
            elif kind == "byte":
                i = draw(st.integers(0, len(text) - 1))
                text = text[:i] + draw(st.sampled_from(["\x01", "=", "|", "\xff", "8=FIX.4.4"])) + text[i + 1 :]
            else:
                text = text[: draw(st.integers(0, len(text) - 1))]
        out.append(Msg(t, is_out, mt, seq[is_out], text))
    return out


ENGINES = ["cpp_ns", "cpp_ms", "cpp_s", "qfj_plain", "qfj_ms", "qfn", "go"]
HOSTILE_PREFIXES = [
    "00000101-00:00:00 : ",
    "20261301-00:00:00.000 : ",
    "20260230-12:00:00.000: ",
    "0000/01/01 00:00:00.000000 ",
    "2026/13/45 25:61:61.000000 ",
    "garbage prefix ",
    "20261015-13:30:00.1234567890 : ",
]


def _prefix(t: int, engine: str) -> tuple[str, str]:
    if engine == "cpp_ns":
        return f"{fix_time(t, 9)} : ", "\n"
    if engine == "cpp_ms":
        return f"{fix_time(t, 3)} : ", "\n"
    if engine == "cpp_s":
        return f"{fix_time(t, 0)} : ", "\n"
    if engine == "qfj_plain":
        return "", "\n"
    if engine == "qfj_ms":
        return f"{fix_time(t, 3)}: ", "\n"
    if engine == "qfn":
        return f"{fix_time(t, 3)} : ", "\r\n"
    return f"{go_time(t)} ", "\n"


@st.composite
def log_file(draw: st.DrawFn, msgs: list[Msg], *, hostile: bool = True) -> bytes:
    """A FileLog messages file of the conversation in one engine's format and delimiter."""
    engine = draw(st.sampled_from(ENGINES))
    delim = draw(st.sampled_from([SOH, SOH, "|", "^A"]))
    lines = []
    for m in msgs:
        pre, end = _prefix(m.t, engine)
        if hostile and draw(st.integers(0, 15)) == 0:
            pre = draw(st.sampled_from(HOSTILE_PREFIXES))
        text = m.text if delim == SOH else m.text.replace(SOH, delim)
        lines.append(pre + text + end)
        if hostile and draw(st.integers(0, 20)) == 0:
            lines.append(draw(st.sampled_from(["\n", "not a fix line\n", "8=FIX\n", "8=FIX.4.4\x019=5\n"])))
    data = "".join(lines).encode("latin-1", "replace")
    if hostile and draw(st.integers(0, 10)) == 0:
        data = data[: draw(st.integers(0, len(data)))]
    return data


def store_files(msgs: list[Msg], engine: str) -> tuple[bytes, bytes]:
    """FileStore `.body` + `.header` of the outbound messages (C++ text, QuickFIX/J binary, /n and /Go lines)."""
    body = b""
    header = b""
    for m in msgs:
        if not m.out:
            continue
        data = m.text.encode("latin-1", "replace")
        off = len(body)
        if engine.startswith("qfj"):
            header += struct.pack(">iqi", m.seq % 2**31, off, len(data))
        elif engine in ("qfn", "go"):
            header += f"{m.seq},{off},{len(data)}\n".encode()
        else:
            header += f"{m.seq},{off},{len(data)} ".encode()
        body += data
    return body, header


HEADER_HOSTILE = [
    b"",
    b"1,0,10 ",
    b"1,-5,10\n",
    b"9" * 30 + b",0,10\n",
    b"1,0," + b"9" * 5000 + b"\n",
    b"\x00" * 16,
    b"\xff" * 16,
    b"\x7f\xff\xff\xff\x7f\xff\xff\xff\xff\xff\xff\xff\x7f\xff\xff\xff",
    b"\x00\x00\x00\x01\xff\xff\xff\xff\xff\xff\xff\x00\x00\x00\x00\x10",
    b"garbage",
]
SEQNUMS = [
    b"00000000000000000002 : 00000000000000000003",
    b"0000000002 : 0000000003",
    b"0000000000000000002",
    b"\x00\x13" + b"2",
    b"9" * 5000 + b" : 1",
    b"",
    b"x : y",
]
EVENT_LINES = [
    "20261015-13:30:00.000 : Created session",
    "20261015-13:30:00.000 : MsgSeqNum too high, expecting 3 but received 7",
    "20261015-13:30:00.000 : Sent ResendRequest FROM: 3 TO: 6",
    "20261015-13:30:00.000: Logon contains ResetSeqNumFlag=Y, resetting sequence numbers to 1",
    "2026/10/15 13:30:00.000000 Disconnected",
    "00000101-00:00:00 : Invalid message: Garbled",
    "plain line",
    "",
]


def gz(data: bytes) -> bytes:
    """Deterministic gzip (mtime 0)."""
    return gzip.compress(data, mtime=0)


# ---- data dictionaries ----------------------------------------------------------------------------------------------

DICT_FIELDS = [
    ("1", "Account", []),
    ("11", "ClOrdID", []),
    ("150", "ExecType", [("0", "NEW"), ("F", "TRADE"), ("Z", "TRADE_CANCEL"), ("Y", "CORRECTED"), ("X", "CANCELED")]),
    ("851", "LastLiquidityInd", [("1", "ADDED_LIQUIDITY"), ("2", "REMOVED_LIQUIDITY")]),
    ("9001", "MakerTakerFlag", [("A", "ADDED"), ("R", "REMOVED"), ("1", "PASSIVE")]),
    ("9002", "AggressorSide", [("Y", "YES"), ("N", "NO")]),
    ("55", "Symbol", []),
    ("38", "OrderQty", []),
]


@st.composite
def data_dictionary(draw: st.DrawFn, *, hostile: bool = True) -> bytes:
    """A QuickFIX data dictionary (`<fix>` with fields, enum values and messages), sometimes hostile."""
    fields = draw(st.lists(st.sampled_from(DICT_FIELDS), max_size=len(DICT_FIELDS), unique_by=lambda f: f[0]))
    parts = []
    for num, name, values in fields:
        if hostile and draw(st.integers(0, 12)) == 0:
            num = draw(st.sampled_from(["", "x", "9" * 30, "1e3", "-1", chr(0x661)]))
        vals = "".join(f'<value enum="{e}" description="{d}"/>' for e, d in values)
        parts.append(f'<field number="{num}" name="{name}" type="STRING">{vals}</field>')
    msgs = (
        '<messages><message name="ExecutionReport" msgtype="8" msgcat="app"/><message name="X" msgtype=""/></messages>'
    )
    major = draw(st.sampled_from(["4", "5", "", "x"]))
    root = draw(st.sampled_from(["fix", "fix", "fix", "notfix"])) if hostile else "fix"
    doc = (
        f'<{root} type="FIX" major="{major}" minor="4" servicepack="0"><fields>{"".join(parts)}</fields>{msgs}</{root}>'
    )
    if hostile:
        mode = draw(st.integers(0, 12))
        if mode == 0:
            doc = '<?xml version="1.0"?><!DOCTYPE fix [<!ENTITY a "aaaa">]>' + doc
        elif mode == 1:
            return ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE fix [<!ENTITY a "aaaa">]>' + doc).encode("utf-16")
        elif mode == 2:
            return ('<?xml version="1.0" encoding="UTF-16"?>' + doc).encode("utf-16")
        elif mode == 3:
            doc = '<?xml version="1.0" encoding="no-such-codec"?>' + doc
        elif mode == 4:
            doc = doc[: draw(st.integers(0, len(doc)))]
        elif mode == 5:
            doc = "<a>" * 50_000 + "</a>" * 50_000
        elif mode == 6:
            doc = doc.replace("<fields>", "<fields><field/>")
    return doc.encode("utf-8")
