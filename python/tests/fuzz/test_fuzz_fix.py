"""Fuzz properties of the FIX stack: the wire format (framing, fields, UTCTimestamp), QuickFIX data dictionaries,
QuickFIX FileLog / FileStore / sequence-number / event files, and the full FIX import (`import_quickfix`) over
generated sessions with sequence anomalies, corruption and hostile options."""

from __future__ import annotations

import datetime as dt
import warnings
from typing import Any

from fuzz._fix import (
    ENGINES,
    EVENT_LINES,
    HEADER_HOSTILE,
    SEQNUMS,
    conversation,
    data_dictionary,
    gz,
    log_file,
    store_files,
)
from fuzz._harness import FUZZ, guarded, scaled, workdir, write_files
from fuzz._mutate import mutated, seeds
from hypothesis import given
from hypothesis import strategies as st
from live._fixgen import SOH, fix_message

from queuetruth.errors import QtError
from queuetruth.live import import_quickfix
from queuetruth.live.importers.fix44 import _Gaps
from queuetruth.live.importers.fixdict import load_dictionary
from queuetruth.live.importers.fixwire import frame, parse_fields, utc_ts
from queuetruth.live.importers.quickfix import (
    LogStats,
    classify,
    first_time,
    iter_log,
    read_event_log,
    read_seqnums,
    read_store,
)

latin1 = st.text(st.characters(min_codepoint=0, max_codepoint=255), max_size=200)
fixish = st.lists(
    st.one_of(latin1, st.sampled_from(["8=FIX.4.4", SOH, "9=", "10=", "=", "|", "35=8", "95=3", "96=", "000"])),
    max_size=30,
).map("".join)


# ---- wire format ----------------------------------------------------------------------------------------------------


@scaled(4)
@given(fixish, st.booleans())
def test_frame_always_progresses(s: str, pipe: bool) -> None:
    """`frame` returns, consumes at least one character from its start and never runs past the input (the log
    reader's loop relies on it to terminate)."""
    start = s.find("8=FIX")
    if start < 0:
        s, start = "8=FIX" + s, 0
    fr = guarded("fixwire.frame", lambda: frame(s, start, pipe_delims=pipe), input_bytes=len(s))
    assert not isinstance(fr, QtError)
    assert start < fr.end <= len(s)
    assert fr.text == s[start : fr.end]


@scaled(4)
@given(
    st.sampled_from(["FIX.4.2", "FIX.4.4", "FIXT.1.1"]),
    st.lists(
        st.tuples(
            st.integers(1, 99999).map(str), st.text(st.characters(min_codepoint=32, max_codepoint=255), min_size=1)
        ),
        max_size=12,
    ),
)
def test_frame_round_trip(begin: str, body: list[tuple[str, str]]) -> None:
    """A well-formed message (correct BodyLength and CheckSum) frames without a problem and parses back to its
    fields in wire order (values without `=`/SOH; data fields excluded)."""
    body = [
        (t, v.replace(SOH, "").replace("=", "")) for t, v in body if t not in ("8", "9", "10", "90", "91", "95", "96")
    ]
    body = [(t, v) for t, v in body if v]
    msg = fix_message(begin, [("35", "8")], body)
    fr = frame(msg, 0)
    assert fr.problem is None and fr.text == msg and fr.end == len(msg)
    fields = parse_fields(fr.text)
    assert fields is not None
    assert fields[0] == ("8", begin) and fields[2] == ("35", "8") and fields[3:-1] == body


@scaled(4)
@given(fixish)
def test_parse_fields_total(text: str) -> None:
    out = guarded("fixwire.parse_fields", lambda: parse_fields(text), input_bytes=len(text))
    assert out is None or all(t.isascii() and t.isdigit() for t, _ in out)


@scaled(4)
@given(
    st.one_of(
        latin1,
        st.builds(
            lambda y, mo, d, h, mi, s, f: f"{y:04d}{mo:02d}{d:02d}-{h:02d}:{mi:02d}:{s:02d}{f}",
            st.integers(0, 9999),
            st.integers(0, 99),
            st.integers(0, 99),
            st.integers(0, 99),
            st.integers(0, 99),
            st.integers(0, 99),
            st.sampled_from(["", ".1", ".123456789", ".123456789012"]),
        ),
    )
)
def test_utc_ts_matches_the_calendar(text: str) -> None:
    """`utc_ts` returns None or the exact POSIX time of a real date-time (second 60 = next minute's 0)."""
    r = guarded("fixwire.utc_ts", lambda: utc_ts(text), input_bytes=len(text))
    if r is None:
        return
    assert not isinstance(r, QtError)
    ns, prec, _ = r
    core = text.strip()
    y, mo, d = int(core[0:4]), int(core[4:6]), int(core[6:8])
    h, mi, s = int(core[9:11]), int(core[12:14]), int(core[15:17])
    base = dt.datetime(y, mo, d, h, mi, min(s, 59), tzinfo=dt.UTC)
    want = int(base.timestamp()) + (1 if s == 60 else 0)
    assert ns // 10**9 == want
    assert prec in ("s", "ms", "us", "ns")


@scaled(4)
@given(st.lists(st.tuples(st.sampled_from(["add", "remove", "discard"]), st.integers(0, 60), st.integers(0, 60))))
def test_gap_ranges_match_a_set(ops: list[tuple[str, int, int]]) -> None:
    """The MsgSeqNum gap ranges of the FIX importer behave exactly like the set of missing numbers they replace."""
    g, ref = _Gaps(), set()
    for op, a, b in ops:
        if op == "add":
            g.add(a, b)
            ref |= set(range(a, b))
        elif op == "remove":
            assert g.remove(a, b) == len({x for x in ref if a <= x < b})
            ref -= set(range(a, b))
        else:
            g.discard(a)
            ref.discard(a)
        assert g.count() == len(ref) and bool(g) == bool(ref)
        assert all((x in g) == (x in ref) for x in range(-1, 62))
        assert all(lo < hi for lo, hi in zip(g.lo, g.hi, strict=True))
        assert all(h1 < l2 for h1, l2 in zip(g.hi, g.lo[1:], strict=False))  # disjoint, sorted, not adjacent


# ---- data dictionaries ----------------------------------------------------------------------------------------------


@scaled(2)
@given(st.one_of(data_dictionary(hostile=False), data_dictionary(), st.binary(max_size=300)))
def test_data_dictionary(xml: bytes) -> None:
    with workdir() as d:
        p = d / "FIX44.xml"
        p.write_bytes(xml)
        out = guarded("fixdict.load_dictionary", lambda: load_dictionary([str(p)]), input_bytes=len(xml))
        if not isinstance(out, QtError):
            assert out is not None
            for name in ("ExecType", "LastLiquidityInd", "Symbol"):
                assert out.tag(name).isdigit()
            out.liquidity_fields()
            for v in ("Z", "Y", "X", "F", "0", "nope"):
                out.exec_type(v)


# ---- QuickFIX files -------------------------------------------------------------------------------------------------


@FUZZ
@given(
    st.one_of(conversation().flatmap(log_file), mutated(seeds("fix44/*/*.log"))),
    st.sampled_from(["auto", "soh", "|", "^A", ";"]),
    st.booleans(),
)
def test_log_reader(data: bytes, delimiter: str, compress: bool) -> None:
    """`iter_log` (all prefixes, delimiters, continuation lines, gzip) and `first_time` on generated and mutated
    logs."""
    with workdir() as d:
        p = d / ("x.messages.current.log" + (".gz" if compress else ""))
        p.write_bytes(gz(data) if compress else data)

        def run() -> Any:
            lines = list(iter_log(str(p), delimiter=delimiter, regex=None, parse_regex_ts=None, stats=LogStats()))
            first_time(str(p))
            return lines

        out = guarded("quickfix.iter_log", run, input_bytes=len(data))
        if not isinstance(out, QtError):
            for ll in out:
                assert ll.problem in (None, "bad_checksum", "bad_body_length", "truncated", "malformed")


@FUZZ
@given(
    conversation(),
    st.sampled_from(ENGINES),
    st.one_of(st.none(), st.sampled_from(HEADER_HOSTILE)),
    st.integers(0, 3),
)
def test_store_reader(msgs: Any, engine: str, header: bytes | None, cut: int) -> None:
    """`read_store` over a FileStore body with its (C++ / J binary / n / Go) index, or a hostile index."""
    body, hdr = store_files(msgs, engine)
    if header is not None:
        hdr = header
    body = body[: len(body) - cut * 7] if cut else body
    with workdir() as d:
        bp, hp = d / "s.body", d / "s.header"
        bp.write_bytes(body)
        hp.write_bytes(hdr)
        out = guarded("quickfix.read_store", lambda: read_store(str(bp), str(hp)), input_bytes=len(body) + len(hdr))
        if not isinstance(out, QtError):
            for seq, fr, off in out.messages:
                assert off >= 0 and fr.text == body.decode("latin-1")[off : off + len(fr.text)]
                assert seq is None or seq >= 0


@FUZZ
@given(
    st.one_of(st.sampled_from(SEQNUMS), st.binary(max_size=64)),
    st.sampled_from(["store_seqnums", "store_senderseqnums", "store_targetseqnums"]),
    st.lists(st.sampled_from(EVENT_LINES), max_size=8).map(lambda ls: "\n".join(ls).encode()),
    st.binary(max_size=64),
)
def test_seqnums_and_event_logs(seq: bytes, kind: str, events: bytes, junk: bytes) -> None:
    with workdir() as d:
        (d / "a.seqnums").write_bytes(seq)
        (d / "a.event.current.log").write_bytes(events + junk)
        guarded("quickfix.read_seqnums", lambda: read_seqnums(str(d / "a.seqnums"), kind), input_bytes=len(seq))
        guarded(
            "quickfix.read_event_log", lambda: read_event_log(str(d / "a.event.current.log")), input_bytes=len(events)
        )


@scaled(2)
@given(st.text(max_size=80))
def test_classify_any_name(name: str) -> None:
    out = guarded("quickfix.classify", lambda: classify("/x/" + name.replace("/", "_")), input_bytes=len(name))
    assert not isinstance(out, QtError)


# ---- the full import ------------------------------------------------------------------------------------------------

STEMS = ["FIX.4.4-QTCLIENT-VENUE", "FIX.4.2-QTCLIENT-VENUE-Q1", "FIXT.1.1-A-B-C-D", "FIX.4.4-QTCLIENT_SUB-VENUE", "x"]
LOG_NAMES = [
    "{stem}.messages.current.log",
    "{stem}.messages.log",
    "{stem}.messages.backup.{n}.log",
    "{stem}.messages.current.log.1",
    "{stem}.messages.current.log-20261015.gz",
    "session.log",
]
OPTIONS: dict[str, Any] = {
    "checksum": st.sampled_from(["exclude", "keep", "bogus"]),
    "delimiter": st.sampled_from(["auto", "soh", "|", "^A", "==", "=", "7"]),
    "venue_name": st.sampled_from(["coinbase", "cme", "sim:x", "", "a" * 40]),
    "session_venues": st.sampled_from([{"VENUE": "cme"}, {"FIX.4.4-QTCLIENT-VENUE": "coinbase"}, {"nope": "x"}]),
    "instrument_tag": st.sampled_from([107, 48, 0, -1]),
    "local_ts_regex": st.sampled_from([r"^(\S+) : ", r"^(?P<ts>\d+)", r"(", r"no groups", r"^(.*?)8=FIX"]),
    "tz": st.sampled_from(["UTC", "America/Chicago", "Not/AZone", "../../etc/passwd", ""]),
}


@st.composite
def fix_inputs(draw: st.DrawFn, *, hostile: bool) -> tuple[dict[str, bytes], dict[str, Any]]:
    files: dict[str, bytes] = {}
    for k in range(draw(st.integers(1, 2))):
        msgs = draw(conversation(hostile=hostile))
        stem = draw(st.sampled_from(STEMS))
        name = draw(st.sampled_from(LOG_NAMES)).format(stem=stem, n=draw(st.integers(1, 3)))
        data = draw(log_file(msgs, hostile=hostile))
        files[f"d{k}/{name}"] = gz(data) if name.endswith(".gz") else data
        if draw(st.integers(0, 3)) == 0:
            body, hdr = store_files(msgs, draw(st.sampled_from(ENGINES)))
            files[f"d{k}/{stem}.body"] = body
            files[f"d{k}/{stem}.header"] = (
                draw(st.sampled_from(HEADER_HOSTILE)) if hostile and draw(st.booleans()) else hdr
            )
        if draw(st.integers(0, 5)) == 0:
            files[f"d{k}/{stem}.seqnums"] = draw(st.sampled_from(SEQNUMS))
        if draw(st.integers(0, 5)) == 0:
            files[f"d{k}/{stem}.event.current.log"] = "\n".join(draw(st.lists(st.sampled_from(EVENT_LINES)))).encode()
    opts: dict[str, Any] = {}
    if draw(st.integers(0, 3)) == 0:
        files["FIX44.xml"] = draw(data_dictionary(hostile=hostile))
        opts["data_dictionary"] = "FIX44.xml"
    keys = ["checksum"]
    if hostile and draw(st.integers(0, 3)) == 0:  # hostile options mostly stop the import up front: keep them rare
        keys = draw(st.lists(st.sampled_from(sorted(OPTIONS)), min_size=1, max_size=3, unique=True))
    for key in keys:
        opts[key] = draw(OPTIONS[key]) if hostile else "keep"
    opts["_as_dirs"] = draw(st.booleans())
    return files, opts


def run_import(files: dict[str, bytes], opts: dict[str, Any], what: str) -> Any:
    with workdir() as d:
        paths, total = write_files(d, files)
        kw = dict(opts)
        as_dirs = kw.pop("_as_dirs", False)
        if "data_dictionary" in kw:
            kw["data_dictionary"] = str(d / kw["data_dictionary"])
        if as_dirs:  # FileLogPath / FileStorePath directories (expanded to the QuickFIX files inside)
            inputs = sorted({str(d / name.split("/")[0]) for name in files if "/" in name}) or paths
        else:
            inputs = [p for p, name in zip(paths, files, strict=True) if name != "FIX44.xml"]

        def run() -> Any:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return import_quickfix(inputs, d / "out", **kw)

        return guarded(what, run, input_bytes=total)


@FUZZ
@given(fix_inputs(hostile=True))
def test_fix_import_hostile(case: tuple[dict[str, bytes], dict[str, Any]]) -> None:
    run_import(*case, what="fix44:hostile")


@FUZZ
@given(fix_inputs(hostile=False))
def test_fix_import_plausible(case: tuple[dict[str, bytes], dict[str, Any]]) -> None:
    """Uncorrupted sessions (sequence anomalies and odd message orders only): imports mostly succeed."""
    run_import(*case, what="fix44:plausible")


@FUZZ
@given(st.lists(mutated(seeds("fix44/*/*.log")), min_size=1, max_size=2), st.sampled_from(["exclude", "keep"]))
def test_fix_import_mutated(logs: list[bytes], checksum: str) -> None:
    files = {f"d/FIX.4.4-QTCLIENT-SYNTHVENUE.messages.backup.{i + 1}.log": b for i, b in enumerate(logs)}
    run_import(files, {"checksum": checksum}, what="fix44:mutated")
