"""One regression test per parser bug the fuzz suite or its reviews found (minimised by hand from the reproducers).

Each `test_bugNN_*` test fails on main: an exception other than `QtError` escaped, the parser hung or allocated
without bound (bug01, bug12, bug28 and bug29 end in the time or memory backstop there), or a malformed input was
silently accepted with a wrong value. Guards pass on main by design and say so: they pin behaviour a fix must keep
(the leap second, the Flex XML encoding, symlinked partitions, in-range offsets, the decimal range edges, the gzip
ratio of real logs). Everything runs under the properties' contract (`fuzz._harness.guarded`: QT-* codes only,
per-example time and memory limits)."""

from __future__ import annotations

import gzip
import io
import json
import struct
import time
import warnings
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from dq.helpers import make_dataset, midnight_ny, rec
from fuzz._harness import guarded
from hypothesis import given, settings
from hypothesis import strategies as st
from live._fixgen import SOH, T0, fix_message, fix_time

from queuetruth import dq
from queuetruth.errors import QtError
from queuetruth.live import _common as common
from queuetruth.live import import_sim, import_venue
from queuetruth.live.imap import load_instrument_map
from queuetruth.live.importers import ImportOptions, fixdict, fixwire, ibkr_flex, quickfix
from queuetruth.live.importers.ibkr_flex import import_ibkr_flex


def qt_error(fn: Any, code: str | None = None, what: str = "regression") -> QtError:
    """The QtError `fn` raises (any other outcome fails the test)."""
    out = guarded(what, fn)
    assert isinstance(out, QtError), f"expected a QtError, got {out!r:.200}"
    if code is not None:
        assert out.code == code, out
    return out


def venue_import(tmp_path: Path, venue: str, lines: list[Any], name: str = "x.jsonl", **spec: Any) -> Any:
    p = tmp_path / name
    p.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    full = {"venue": venue, "account": "acct1", "inputs": [{"path": str(p)}], "out": str(tmp_path / "out"), **spec}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return import_venue(full)


# ---- shared decimal / time helpers (`queuetruth.live._common`) ------------------------------------------------------


@pytest.mark.parametrize("text", ["1e999999999", "-1E+999999999", "1e-999999999"])
def test_bug01_decimal_exponent_bomb(text: str) -> None:
    """`dec` accepted any finite exponent; `to_fixed`, `epoch_ns` and the positional text of the raw value then built
    integers or strings of a billion digits (minutes of CPU and gigabytes of memory per field)."""
    qt_error(lambda: common.dec(text, "q"), "QT-INPUT-004")
    qt_error(lambda: common.epoch_ns(text, "auto", "T"), "QT-INPUT-004")
    assert len(common.plain_decimal(Decimal(text))) < 32


def test_bug01_decimal_exponent_bomb_through_an_importer(tmp_path: Path) -> None:
    line = (
        '{"e":"executionReport","E":1792071000001,"s":"BTCUSDT","c":"c1","S":"BUY","o":"LIMIT","f":"GTC",'
        '"q":1e999999999,"p":"60000","x":"NEW","X":"NEW","i":1,"l":"0","z":"0","L":"0","T":1792071000000}'
    )
    qt_error(lambda: venue_import(tmp_path, "binance", [line]), "QT-INPUT-004")


def test_bug01_zero_with_an_extreme_exponent_is_zero() -> None:
    assert common.dec("0E+999999999", "q") == 0
    assert common.to_fixed(common.dec("0e-999999999", "q"), -8, "q") == 0


def test_bug02_dmul_is_exact() -> None:
    """`dmul` multiplied in an 80-digit context, so products of long decimals were silently rounded."""
    a, b = Decimal("1." + "1" * 60), Decimal("3." + "7" * 60)
    with localcontext() as ctx:
        ctx.prec = 500
        exact = a * b
    assert common.dmul(a, b) == exact


@pytest.mark.parametrize(
    "text",
    [
        "2026-02-30T00:00:00Z",  # was accepted as 2026-03-02
        "2026-01-01T00:00:00+99:99",  # was accepted (offset of 4 days)
        "٢٠٢٦-01-01T00:00:00Z",  # Arabic-Indic digits were accepted
        "2026-13-01T00:00:00Z",  # ValueError escaped
        "0000-01-01T00:00:00Z",  # ValueError escaped
    ],
)
def test_bug03_rfc3339_impossible_times(text: str) -> None:
    qt_error(lambda: common.rfc3339_ns(text, "timestamp"), "QT-INPUT-002")


def test_bug03_rfc3339_leap_second_still_accepted() -> None:
    assert common.rfc3339_ns("2016-12-31T23:59:60Z", "t")[0] == common.rfc3339_ns("2017-01-01T00:00:00Z", "t")[0]


def test_bug04_local_time_impossible_date() -> None:
    """`local_to_utc_s` turned February 30 (UTC zone) into March 2."""
    qt_error(lambda: common.local_to_utc_s(2026, 2, 30, 0, 0, 0, "UTC", "t"), "QT-INPUT-002")


def test_bug05_date_days_value_error() -> None:
    qt_error(lambda: common.date_days("2026-13-01"), "QT-INPUT-002")


@pytest.mark.parametrize(
    "line",
    [
        '{"a": ' + "9" * 5000 + "}",  # ValueError: past Python's int-string digit limit
        "[" * 100_000,  # RecursionError
        '{"recv_ns": 1792071000000000000, "msg": "' + "[" * 100_000 + '"}',  # wrapped msg: RecursionError
        '{"recv_ns": ' + "9" * 30 + ', "msg": {}}',  # recv_ns past int64 was accepted
    ],
)
def test_bug06_json_lines_reader(tmp_path: Path, line: str) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text(line + "\n" + line + "\n")
    qt_error(lambda: common.read_json_records(p, 0), "QT-INPUT-002")


# ---- JSON venue importers -------------------------------------------------------------------------------------------


def test_bug07_binance_report_without_event_time(tmp_path: Path) -> None:
    """An executionReport with neither `T` nor `E` raised KeyError."""
    m = {"e": "executionReport", "s": "BTCUSDT", "c": "c1", "S": "BUY", "o": "LIMIT", "f": "GTC", "q": "1",
         "p": "1", "x": "NEW", "X": "NEW", "i": 1, "l": "0", "z": "0", "L": "0"}  # fmt: skip
    qt_error(lambda: venue_import(tmp_path, "binance", [m]), "QT-INPUT-002")


#: Nested fields of the wrong JSON type: each raised AttributeError (`.get` on a list, a string or a number).
WRONG_SHAPES: list[tuple[str, list[Any]]] = [
    (
        "binance",
        [{"dir": "out", "local_ns": 1792071300000000000, "msg": {"id": "1", "method": "order.place", "params": [1]}}],
    ),
    (
        "binance",
        [
            {"dir": "out", "local_ns": 1792071300000000000, "msg": {"id": "r1", "method": "order.place", "params": {}}},
            {"dir": "in", "local_ns": 1792071300000000001, "msg": {"id": "r1", "status": 400, "error": "boom"}},
        ],
    ),
    ("coinbase", [{"channel": "user", "timestamp": "2026-10-15T16:00:00Z", "events": ["x"]}]),
    (
        "coinbase",
        [{"channel": "user", "timestamp": "2026-10-15T16:00:00Z", "events": [{"type": "update", "orders": 5}]}],
    ),
    ("coinbase", [{"orders": [{"order_id": "o1", "order_configuration": {"limit_limit_gtc": 7}}]}]),
    ("coinbase", [{"orders": [1, "x"]}]),
    ("coinbase", [{"fills": [None, 2]}]),
    ("deribit", [{"jsonrpc": "2.0", "method": "subscription", "params": ["user.orders.BTC-PERPETUAL.raw"]}]),
    ("hyperliquid", [{"channel": "orderUpdates", "data": [{"order": [1, 2], "status": "open", "statusTimestamp": 1}]}]),
    ("hyperliquid", [{"channel": "orderUpdates", "data": [3, "x"]}]),
    ("hyperliquid", [{"channel": "userFills", "data": {"user": "0x1", "fills": [3, "x"]}}]),
    ("kraken", [{"channel": "executions", "type": "update", "data": [1, "x"]}]),
    (
        "kraken",
        [
            {
                "channel": "executions",
                "type": "update",
                "data": [
                    {
                        "order_id": "O1",
                        "exec_id": "E1",
                        "exec_type": "trade",
                        "symbol": "BTC/USD",
                        "side": "buy",
                        "last_qty": 1,
                        "last_price": 1,
                        "timestamp": "2026-10-15T17:00:04Z",
                        "cum_qty": 1,
                        "fees": [{"asset": "USD", "qty": 0.1}, 5],
                    }
                ],
            }
        ],
    ),
]


@pytest.mark.parametrize(("venue", "lines"), WRONG_SHAPES, ids=[f"{v}-{i}" for i, (v, _) in enumerate(WRONG_SHAPES)])
def test_bug08_nested_fields_of_the_wrong_type(tmp_path: Path, venue: str, lines: list[Any]) -> None:
    """Every shape either imports (a non-object list item is skipped and counted) or fails QT-INPUT-002."""
    out = guarded("regression", lambda: venue_import(tmp_path, venue, lines))
    if isinstance(out, QtError):
        assert out.code == "QT-INPUT-002", out


# ---- FIX wire, QuickFIX logs and stores, data dictionaries ----------------------------------------------------------

HUGE = "9" * 5000


def test_bug09_fix_integers_past_the_digit_limit() -> None:
    """BodyLength, data-field lengths and sequence numbers of more than 4300 digits raised ValueError from `int()`
    (in `frame` and `parse_fields`; the fix added `fixwire.fix_int`, checked last so the test fails on the bug)."""
    framed = guarded("regression", lambda: fixwire.frame(f"8=FIX.4.4{SOH}9={HUGE}{SOH}35=0{SOH}10=000{SOH}", 0))
    assert framed.problem == "bad_body_length"
    fields = guarded("regression", lambda: fixwire.parse_fields(f"95={HUGE}{SOH}96=x{SOH}"))
    assert fields == [("95", HUGE), ("96", "x")]
    assert fixwire.fix_int(HUGE) is None
    assert fixwire.fix_int("١٢") is None  # non-ASCII digits are not FIX integers


def test_bug09_seqnum_and_store_index_files(tmp_path: Path) -> None:
    seq = tmp_path / "x.seqnums"
    seq.write_bytes(HUGE.encode() + b" : 1")
    assert guarded("regression", lambda: quickfix.read_seqnums(str(seq), "store_seqnums")) == (None, 1)
    body = fix_message("FIX.4.4", [("35", "0"), ("49", "A"), ("56", "B"), ("34", "1")], [])
    (tmp_path / "x.body").write_text(body, encoding="latin-1")
    (tmp_path / "x.header").write_text(f"1,0,{len(body)} {HUGE},0,{len(body)} ")
    got = guarded("regression", lambda: quickfix.read_store(str(tmp_path / "x.body"), str(tmp_path / "x.header")))
    assert len(got.messages) == 1 and got.index_mismatches == 1


def test_bug10_store_index_negative_sequence_number(tmp_path: Path) -> None:
    """A binary (QuickFIX/J, signed int32) index record with a negative MsgSeqNum was taken as the message's
    sequence number (-7 here); negative offsets and sizes are refused the same way (the record is a mismatch and
    the message keeps no index sequence number)."""
    body = fix_message("FIX.4.4", [("35", "0"), ("49", "A"), ("56", "B"), ("34", "1")], [])
    (tmp_path / "x.body").write_text(body, encoding="latin-1")
    (tmp_path / "x.header").write_bytes(struct.pack(">iqi", -7, 0, len(body)))
    got = quickfix.read_store(str(tmp_path / "x.body"), str(tmp_path / "x.header"))
    assert [seq for seq, _, _ in got.messages] == [None] and got.index_mismatches == 1


def test_bug11_utc_timestamp_impossible_date() -> None:
    """`utc_ts` accepted February 30 (as March 2)."""
    assert fixwire.utc_ts("20260230-00:00:00") is None
    assert fixwire.utc_ts("20161231-23:59:60") is not None  # leap second still accepted


def _session_log(tmp_path: Path, seqs: list[int], extra: list[tuple[str, str]] | None = None, mt: str = "0") -> Path:
    lines = []
    for i, seq in enumerate(seqs):
        t = T0 + i * 10**6
        header = [("35", mt), ("49", "VENUE"), ("56", "QTCLIENT"), ("34", str(seq)), ("52", fix_time(t, 3))]
        lines.append(fix_message("FIX.4.4", header, extra or []) + "\n")
    p = tmp_path / "FIX.4.4-QTCLIENT-VENUE.messages.log"
    p.write_text("".join(lines), encoding="latin-1")
    return p


def test_bug12_msgseqnum_gap_of_1e18(tmp_path: Path) -> None:
    """A MsgSeqNum jump to 10**18 put every missing number in a set (`range(2, 10**18)`): unbounded time and memory.
    Gaps are now half-open ranges."""
    p = _session_log(tmp_path, [1, 10**18, 2])
    spec = {"venue": "fix44", "account": "acct1", "inputs": [{"path": str(p)}], "out": str(tmp_path / "out")}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = guarded("regression", lambda: import_venue(spec))
    assert not isinstance(out, QtError), out
    session = next(w for w in out.warnings if "FIX session" in w and "gap_msgs_in" in w)
    # 2 .. 10**18 - 1 missing; the late seq 2 leaves the gap as out of order (not a resend), and the rest is lost
    assert f"gap_msgs_in={10**18 - 2}" in session and "gaps_in=1" in session and "out_of_order=1" in session
    assert f"unrecovered_in={10**18 - 3}" in session


def test_bug13_unknown_tag_of_5000_digits(tmp_path: Path) -> None:
    """With a data dictionary, the unknown-tag report sorted tags by `int(tag)`: ValueError for a 5000-digit tag."""
    d = tmp_path / "FIX44.xml"
    d.write_text('<fix type="FIX" major="4" minor="4"><fields><field number="35" name="MsgType"/></fields></fix>')
    p = _session_log(tmp_path, [1, 2], extra=[(HUGE, "x")], mt="B")  # an application message (News)
    spec = {
        "venue": "fix44",
        "account": "acct1",
        "inputs": [{"path": str(p)}],
        "out": str(tmp_path / "out"),
        "fix": {"data_dictionary": [str(d)]},
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        guarded("regression", lambda: import_venue(spec))


def test_bug14_data_dictionary_dtd_hidden_by_utf16(tmp_path: Path) -> None:
    """The DTD check searched the raw bytes for `<!DOCTYPE`, which a UTF-16 document hides: entity declarations
    (and their expansion) reached the parser."""
    doc = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE fix [<!ENTITY a "aaaaaaaaaa">]>'
        '<fix type="FIX" major="4" minor="4"><fields><field number="1" name="&a;"/></fields></fix>'
    )
    p = tmp_path / "dict.xml"
    p.write_bytes(doc.encode("utf-16"))
    err = qt_error(lambda: fixdict.load_dictionary(str(p)), "QT-INPUT-002")
    assert "DTD" in err.message


def test_bug14_data_dictionary_non_ascii_field_number(tmp_path: Path) -> None:
    p = tmp_path / "dict.xml"
    p.write_text('<fix type="FIX" major="4" minor="4"><fields><field number="١٢" name="X"/></fields></fix>')
    qt_error(lambda: fixdict.load_dictionary(str(p)), "QT-INPUT-002")


def test_bug15_corrupt_gzip_stream(tmp_path: Path) -> None:
    """A gzip header followed by an invalid deflate block raised `zlib.error` (not an OSError)."""
    p = tmp_path / "x.log.gz"
    p.write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + b"\xff" * 20)
    qt_error(lambda: quickfix.read_bytes(str(p)), "QT-INPUT-001")


def test_bug15_gzip_bomb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A small gzip file was decompressed whole, whatever it expanded to (deflate reaches about 1000x)."""
    monkeypatch.setattr(quickfix, "GZIP_SLACK", 0)  # keep the test's bomb small: 2 MiB of zeros, about 2 KiB packed
    p = tmp_path / "x.log.gz"
    p.write_bytes(gzip.compress(b"\x00" * (2 << 20)))
    err = qt_error(lambda: quickfix.read_bytes(str(p)), "QT-INPUT-002")
    assert "gzip" in err.message


# ---- IBKR Flex and simulator importers ------------------------------------------------------------------------------


def test_bug16_flex_xml_nested_too_deep_to_serialise(tmp_path: Path) -> None:
    """A <Trade> element with 100 000 nested children parsed, then `ET.tostring` (the raw record text) raised
    RecursionError."""
    p = tmp_path / "flex.xml"
    p.write_text(
        '<FlexQueryResponse><Trade buySell="BUY">' + "<x>" * 100_000 + "</x>" * 100_000 + "</Trade></FlexQueryResponse>"
    )
    qt_error(lambda: import_ibkr_flex([str(p)], ImportOptions(account="acct1")), "QT-INPUT-002")


def test_flex_xml_doctype_refused(tmp_path: Path) -> None:
    """Hardening, not a found bug (expat already caps entity expansion): Flex XML never has a DTD, so one is refused
    in any encoding, as in data dictionaries."""
    p = tmp_path / "flex.xml"
    p.write_bytes('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "x">]><r/>'.encode("utf-16"))
    qt_error(lambda: import_ibkr_flex([str(p)], ImportOptions(account="acct1")), "QT-INPUT-002")


def _sim(tmp_path: Path, kind: str, name: str, data: bytes, **opts: Any) -> Any:
    p = tmp_path / name
    p.write_bytes(data)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return import_sim(kind, [str(p)], tmp_path / "out", account="acct1", **opts)


NT_CSV = b"client_order_id,type,ts_event\nO-1,OrderAccepted,1792071000000000000\n"


@pytest.mark.parametrize("value", ["x", "1.5", [1], 10**30, True, "\u00b2", "+-5", "-19"])
def test_bug17_sim_size_exp_option(tmp_path: Path, value: Any) -> None:
    """`size_exp` went through `int()`: ValueError / TypeError for text, lists and the like, and no range check."""
    qt_error(lambda: _sim(tmp_path, "nautilus", "x.csv", NT_CSV, size_exp=value), "QT-CONFIG-001")


def test_bug17_sim_order_key_map_option(tmp_path: Path) -> None:
    """`order_key_map` that is not an object raised AttributeError (`.items()` of a list)."""
    data = (
        b"order_id,side,price_tick,tick_size,qty,leaves_qty,status,exch_timestamp,local_timestamp\n"
        b"1,1,100,0.01,1,1,1,1792071000000000000,1792071000000000000\n"
    )
    qt_error(lambda: _sim(tmp_path, "hftbacktest", "x.csv", data, order_key_map=["a"]), "QT-CONFIG-001")


def _parquet_with_column_name(name: bytes) -> bytes:
    """A valid Parquet file whose column name is then overwritten in place by `name` (same length)."""
    sink = io.BytesIO()
    pq.write_table(pa.table({"client_order_id": ["O-1"], "zzzz": ["1"]}), sink)
    data = sink.getvalue()
    assert len(name) == 4 and data.count(b"zzzz") >= 1
    return data.replace(b"zzzz", name)


def test_bug18_sim_parquet_column_name_not_utf8(tmp_path: Path) -> None:
    """Arrow decodes column names lazily: invalid UTF-8 in one raised UnicodeDecodeError from `to_pylist`."""
    data = _parquet_with_column_name(b"z\xc3zz")
    qt_error(lambda: _sim(tmp_path, "nautilus", "x.parquet", data), "QT-INPUT-001")


# ---- dq dataset loader and checks -----------------------------------------------------------------------------------


DATE = "2026-06-15"


def _dataset(root: Path, records: list[dict[str, Any]] | None = None) -> Path:
    t = midnight_ny(DATE) + 34_200 * 10**9
    recs = (
        records
        if records is not None
        else [rec(t + i * 10**6, "A", "B", 100 * 10**7, 10, i + 1, i + 1) for i in range(5)]
    )
    return make_dataset(root, {(DATE, 1): recs}, {1: "ACME"})


def _dq(root: Path, *, replay: bool = False) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return dq.run({"dataset": {"path": str(root)}, "replay": replay})


def _edit_manifest(root: Path, fn: Any) -> None:
    p = root / "manifest.json"
    doc = json.loads(p.read_text())
    fn(doc)
    p.write_text(json.dumps(doc))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instrument_id", {"a": 1}),  # TypeError from int()
        ("session_date", [1]),  # TypeError
        ("ts_min", "soon"),  # TypeError when compared with record times
        ("rows", -5),  # accepted
        ("files", "mbo"),  # TypeError (iterating a string of paths)
    ],
)
def test_bug19_dq_manifest_field_types(tmp_path: Path, field: str, value: Any) -> None:
    root = _dataset(tmp_path / "ds")
    _edit_manifest(root, lambda d: d["partitions"][0].__setitem__(field, value))
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert field in err.message


@pytest.mark.parametrize("path", ["/etc/hosts", "../outside.parquet", "mbo/../../outside.parquet"])
def test_bug20_dq_manifest_path_outside_the_dataset(tmp_path: Path, path: str) -> None:
    """Partition paths were joined to the dataset root unchecked: a manifest could make dq read any file the
    process can read (another tenant's dataset in the service)."""
    root = _dataset(tmp_path / "ds")
    other = _dataset(tmp_path / "other")
    (tmp_path / "outside.parquet").write_bytes(next(other.glob("mbo/*/*/part-*.parquet")).read_bytes())
    _edit_manifest(root, lambda d: d["partitions"][0]["files"][0].__setitem__("path", path))
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "inside the dataset" in err.message or "outside the dataset" in err.message


def test_bug21_dq_partition_column_of_the_wrong_type(tmp_path: Path) -> None:
    """A partition whose `price` column holds text reached the numpy checks (TypeError / UFuncTypeError)."""
    root = _dataset(tmp_path / "ds")
    p = next(root.glob("mbo/*/*/part-*.parquet"))
    t = pq.read_table(p)
    t = t.set_column(t.schema.get_field_index("price"), "price", pa.array(["x"] * t.num_rows))
    pq.write_table(t, p)
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "price" in err.message


def test_bug21_dq_instruments_column_of_the_wrong_type(tmp_path: Path) -> None:
    root = _dataset(tmp_path / "ds")
    p = root / "instruments.parquet"
    t = pq.read_table(p)
    t = t.set_column(t.schema.get_field_index("tick_size"), "tick_size", pa.array(["a tick"] * t.num_rows))
    pq.write_table(t, p)
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "tick_size" in err.message


def test_bug22_dq_parquet_column_name_not_utf8(tmp_path: Path) -> None:
    """Invalid UTF-8 in a column name of `instruments.parquet` or a partition raised UnicodeDecodeError."""
    root = _dataset(tmp_path / "ds")
    p = root / "instruments.parquet"
    data = p.read_bytes()
    assert data.count(b"currency") >= 1
    p.write_bytes(data.replace(b"currency", b"curr\xc3ncy"))
    qt_error(lambda: _dq(root), "QT-INPUT-002")


@pytest.mark.requires_core
def test_bug23_dq_stale_book_prints_without_book_updates(tmp_path: Path) -> None:
    """A partition with trade prints but no book update (only snapshot records besides the prints) indexed an empty
    array in the stale-book check: IndexError."""
    t = midnight_ny(DATE) + 34_200 * 10**9
    records = [
        rec(t, "A", "B", 100 * 10**7, 10, 1, 1, flags=160),  # snapshot (F_SNAPSHOT | F_LAST)
        rec(t + 10**9, "T", "B", 100 * 10**7, 1, 0, 2),
    ]
    root = _dataset(tmp_path / "ds", records)
    out = guarded("regression", lambda: _dq(root, replay=True))
    assert not isinstance(out, QtError), out


def _set_price_exp(root: Path, exp: int) -> None:
    p = root / "instruments.parquet"
    t = pq.read_table(p)
    pq.write_table(pa.Table.from_pylist([{**r, "price_exp": exp} for r in t.to_pylist()], schema=t.schema), p)


def test_bug24_dq_sub_penny_rule_at_price_exp_minus_128(tmp_path: Path) -> None:
    """A US-equity instrument with `price_exp` -128 (int8 allows it) made the sub-penny test compare int64 prices
    with 10**128: OverflowError. `price_exp` outside the dataset range -12..0 is now refused when the instruments
    are loaded, and the sub-penny rule no longer builds integers it cannot compare."""
    root = _dataset(tmp_path / "ds")
    _set_price_exp(root, -128)
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "price_exp" in err.message


def test_bug24_dq_sub_penny_rule_at_the_finest_exponent(tmp_path: Path) -> None:
    """Guard: the finest dataset exponent still runs the rule."""
    root = _dataset(tmp_path / "ds")
    _set_price_exp(root, -12)
    rep = guarded("regression", lambda: _dq(root))
    assert not isinstance(rep, QtError), rep


@pytest.mark.parametrize(("exp_a", "exp_b", "code"), [(0, -12, "QT-INPUT-004"), (127, -128, "QT-INPUT-002")])
def test_bug25_dq_compare_rescale_overflow(tmp_path: Path, exp_a: int, exp_b: int, code: str) -> None:
    """Cross-source parity rescales prices to the finer exponent: a factor past int64 raised OverflowError, and a
    product past int64 wrapped around silently (10**9 at price_exp 0 is 10**21 at -12), so wrong prices were
    compared. The product now fails QT-INPUT-004; exponents outside -12..0 fail QT-INPUT-002 on load."""
    a, b = _dataset(tmp_path / "a"), _dataset(tmp_path / "b")
    _set_price_exp(a, exp_a)
    _set_price_exp(b, exp_b)

    def run() -> Any:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return dq.run({"dataset": {"path": str(a)}, "compare": {"path": str(b)}, "replay": False})

    qt_error(run, code)


def _order_flow(i: int, t: int) -> tuple[str, list[tuple[str, str]]]:
    """Message `i` of a repeating NewOrderSingle / ExecutionReport (new) / ExecutionReport (fill) cycle."""
    px = f"10.{i % 97:02d}"
    if i % 3 == 0:
        return "D", [("11", f"c{i}"), ("55", "ACME"), ("54", "1"), ("38", "100"), ("44", px), ("40", "2"),
                     ("59", "0"), ("60", fix_time(t, 3))]  # fmt: skip
    new = i % 3 == 1
    return "8", [
        ("37", f"o{i // 3}"), ("11", f"c{i - i % 3}"), ("17", f"e{i}"), ("150", "0" if new else "F"),
        ("39", "0" if new else "2"), ("55", "ACME"), ("54", "1"), ("38", "100"), ("14", "0" if new else "100"),
        ("151", "100" if new else "0"), ("31", px), ("32", "0" if new else "100"), ("60", fix_time(t, 3)),
    ]  # fmt: skip


def _quickfix_log(n: int, *, orders: bool) -> bytes:
    """`n` lines of a QuickFIX message log: heartbeats only, or order flow (NewOrderSingle and ExecutionReports)."""
    lines = []
    for i in range(n):
        t = T0 + i * 10**9
        mt, body = _order_flow(i, t) if orders else ("0", [])
        header = [("35", mt), ("49", "QTCLIENT"), ("56", "VENUE"), ("34", str(i + 1)), ("52", fix_time(t, 3))]
        lines.append(f"{fix_time(t, 3)} : {fix_message('FIX.4.4', header, body)}\n")
    return "".join(lines).encode("latin-1")


@pytest.mark.parametrize(("orders", "lo", "hi"), [(False, 10, 13), (True, 8, 11)], ids=["heartbeats", "order_flow"])
def test_gzip_limit_leaves_room_for_real_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orders: bool, lo: float, hi: float
) -> None:
    """Guard for the bug15 fix: synthetic QuickFIX logs of 20 000 lines compress 11.7x (heartbeats only, 2.1 MB) and
    9.6x (order flow, 4.1 MB) at gzip level 9, far below `GZIP_MAX_RATIO`, and read back whole without the slack.
    The bands pin the ratios the extension doc quotes."""
    monkeypatch.setattr(quickfix, "GZIP_SLACK", 0)
    data = _quickfix_log(20_000, orders=orders)
    packed = gzip.compress(data, 9)
    assert lo < len(data) / len(packed) < hi
    p = tmp_path / "x.messages.log.gz"
    p.write_bytes(packed)
    assert quickfix.read_bytes(str(p)) == data


@pytest.mark.parametrize("doc", [{"source_specific": [1, 2]}, {"feed_gaps": [1]}])
def test_bug26_dq_ingest_data_quality_field_types(tmp_path: Path, doc: dict[str, Any]) -> None:
    """`data_quality.json` fields of the wrong JSON type raised AttributeError in the checks (`.items()` of a list,
    `.get` of a number)."""
    root = _dataset(tmp_path / "ds")
    (root / "data_quality.json").write_text(json.dumps(doc))
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert next(iter(doc)) in err.message


# ---- found by review of the first fuzz run (input classes the generators did not reach) ----------------------------

HBT_HEAD = (
    b"order_id,side,price_tick,tick_size,qty,leaves_qty,status,time_in_force,order_type,exch_timestamp,"
    b"local_timestamp\n"
)


@pytest.mark.parametrize("column", ["status", "time_in_force", "order_type"])
@pytest.mark.parametrize("value", ["--1", "---2", "9" * 5000])
def test_bug27_sim_enum_not_an_integer(tmp_path: Path, column: str, value: str) -> None:
    """hftbacktest enums passed `s.lstrip("-").isdecimal()` and went to `int()`: ValueError for `--1` and for more
    than 4300 digits. A value that is not a short integer is now read as an enum name."""
    row = {"status": "1", "time_in_force": "0", "order_type": "0", column: value}
    enums = f"{row['status']},{row['time_in_force']},{row['order_type']}"
    line = f"1,1,100,0.01,1,1,{enums},1792071000000000000,1792071000000000000\n"
    out = guarded("regression", lambda: _sim(tmp_path, "hftbacktest", "x.csv", HBT_HEAD + line.encode()))
    assert not isinstance(out, QtError) or out.code.startswith("QT-INPUT-"), out


@pytest.mark.parametrize("name", ["x.header", "x.seqnums"])
def test_bug28_store_files_with_a_long_digit_run(tmp_path: Path, name: str) -> None:
    """The store index and seqnums regexes could start a match inside a digit run, so a run without a separator was
    rescanned from every position: quadratic (40 000 digits took 13 s; 1 MB would take hours)."""
    (tmp_path / name).write_bytes(b"7" * 2**20)
    body = fix_message("FIX.4.4", [("35", "0"), ("49", "A"), ("56", "B"), ("34", "1")], [])
    (tmp_path / "x.body").write_text(body, encoding="latin-1")
    t0 = time.perf_counter()
    if name == "x.header":
        got = guarded("regression", lambda: quickfix.read_store(str(tmp_path / "x.body"), str(tmp_path / name)))
        assert not isinstance(got, QtError) and len(got.messages) == 1
    else:
        assert guarded("regression", lambda: quickfix.read_seqnums(str(tmp_path / name), "store_seqnums")) == (
            None,
            None,
        )
    assert time.perf_counter() - t0 < 1.5


def _parquet_bytes(table: pa.Table, **kw: Any) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink, **kw)
    return sink.getvalue()


def test_bug29_sim_parquet_row_bomb(tmp_path: Path) -> None:
    """Parquet had no expansion limit: a few KB of dictionary/RLE-encoded rows decoded to millions of records (a
    4.3 KB file with 300 000 rows grew the import by 832 MiB)."""
    n = 2_000_000
    cols = {c: pa.array(["O-1"]).take(pa.array(np.zeros(n, dtype=np.int32))) for c in ("client_order_id", "type")}
    data = _parquet_bytes(pa.table(cols), compression="zstd")
    assert len(data) < 64_000
    err = qt_error(lambda: _sim(tmp_path, "nautilus", "x.parquet", data), "QT-INPUT-002")
    assert "decodes to more than" in err.message


def test_bug29_sim_parquet_dictionary_value_bomb(tmp_path: Path) -> None:
    """One dictionary entry of 4 MiB repeated on 20 000 rows: 80 GiB once decoded, from a file of a few KB. The
    decoded size is summed from the dictionary indices before any value is copied."""
    big = pa.DictionaryArray.from_arrays(pa.array(np.zeros(20_000, dtype=np.int32)), pa.array(["x" * (4 << 20)]))
    # without the stored Arrow schema the column reads back as plain strings: the dangerous case
    data = _parquet_bytes(pa.table({"client_order_id": big}), compression="zstd", store_schema=False)
    assert len(data) < 64_000
    err = qt_error(lambda: _sim(tmp_path, "nautilus", "x.parquet", data), "QT-INPUT-002")
    assert "decodes to more than" in err.message


def test_bug29_dq_partition_row_bomb(tmp_path: Path) -> None:
    """The same for a dq partition: 3 000 000 constant rows in a file of a few KB."""
    root = _dataset(tmp_path / "ds")
    p = next(root.glob("mbo/*/*/part-*.parquet"))
    t = pq.read_table(p)
    idx = pa.array(np.zeros(3_000_000, dtype=np.int64))
    pq.write_table(t.take(idx), p, compression="zstd")
    assert p.stat().st_size < 200_000
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "decodes to more than" in err.message


CSV_BAD = [
    pytest.param("cr", id="bare_cr_in_unquoted_field"),
    pytest.param("long", id="field_over_128_KiB"),
]


def _bad_csv(header: str, row: str, kind: str) -> bytes:
    cells = row.split(",")
    cells[1] = cells[1][:1] + "\r" + cells[1][1:] if kind == "cr" else "x" * 140_000
    return (header + "\n" + ",".join(cells) + "\n").encode()


@pytest.mark.parametrize("kind", CSV_BAD)
def test_bug30_flex_csv_reader_errors(tmp_path: Path, kind: str) -> None:
    """`csv.Error` escaped from every CSV importer (a bare CR inside an unquoted field, a field past the reader's
    131 072-character limit)."""
    p = tmp_path / "flex.csv"
    p.write_bytes(_bad_csv("accountId,symbol,buySell", "U1,ACME,BUY", kind))
    qt_error(lambda: import_ibkr_flex([str(p)], ImportOptions(account="acct1")), "QT-INPUT-002")


@pytest.mark.parametrize("kind", CSV_BAD)
def test_bug30_sim_csv_reader_errors(tmp_path: Path, kind: str) -> None:
    data = _bad_csv("client_order_id,type,ts_event", "O-1,OrderAccepted,1792071000000000000", kind)
    qt_error(lambda: _sim(tmp_path, "nautilus", "x.csv", data), "QT-INPUT-002")


@pytest.mark.parametrize("kind", CSV_BAD)
def test_bug30_instrument_map_csv_reader_errors(tmp_path: Path, kind: str) -> None:
    p = tmp_path / "instrument_map.csv"
    p.write_bytes(
        _bad_csv("venue,instrument,instrument_id,price_exp,size_exp,tick_size,lot_size", "x,ACME,1,-2,0,0.01,1", kind)
    )
    qt_error(lambda: load_instrument_map(p), "QT-INPUT-002")


@pytest.mark.parametrize(
    "array",
    [
        pa.array([2**62], pa.date64()),
        pa.array([2**31 - 1], pa.date32()),
        pa.array([2**62], pa.timestamp("us")),
        pa.array([-(2**62)], pa.timestamp("ms")),
    ],
    ids=["date64", "date32", "timestamp_us", "timestamp_ms"],
)
def test_bug31_sim_parquet_temporal_out_of_python_range(tmp_path: Path, array: pa.Array) -> None:
    """A temporal Parquet column outside Python's date range raised OverflowError / ValueError from `to_pylist`."""
    data = _parquet_bytes(pa.table({"client_order_id": ["O-1"], "ts_event": array}))
    qt_error(lambda: _sim(tmp_path, "nautilus", "x.parquet", data), "QT-INPUT-002")


@pytest.mark.parametrize("day", [2**31 - 1, -(2**31), 2_932_897])
def test_bug32_dq_instruments_session_date_out_of_range(tmp_path: Path, day: int) -> None:
    """`instruments.parquet` with a date32 session_date at the int32 limits raised OverflowError in `to_pylist`."""
    root = _dataset(tmp_path / "ds")
    p = root / "instruments.parquet"
    t = pq.read_table(p)
    i = t.schema.get_field_index("session_date")
    pq.write_table(t.set_column(i, "session_date", pa.array([day] * t.num_rows, pa.date32())), p)
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "session_date" in err.message


@pytest.mark.parametrize("declared", ["ISO-8859-1", "windows-1252", "UTF-16", "UTF-8"])
def test_flex_xml_declared_encoding_is_not_applied_twice(tmp_path: Path, declared: str) -> None:
    """Guard (passes on main): the first version of the bug14 / bug16 hardening re-encoded the decoded statement
    to UTF-8 bytes and let expat apply the declaration again, so `SOCIÉTÉ` came back as `SOCIÃ‰TÃ‰` under an
    ISO-8859-1 or windows-1252 declaration and a UTF-16 declaration failed. The file is read as UTF-8, whatever it
    declares, as on main."""
    p = tmp_path / "flex.xml"
    p.write_text(
        f'<?xml version="1.0" encoding="{declared}"?><FlexQueryResponse><Trade symbol="SOCIÉTÉ" orderReference="ü"/>'
        "</FlexQueryResponse>",
        encoding="utf-8",
    )
    rows = guarded("regression", lambda: ibkr_flex._rows(p, 0))
    assert not isinstance(rows, QtError), rows
    assert rows[0][0]["symbol"] == "SOCIÉTÉ" and rows[0][0]["orderReference"] == "ü"


@pytest.mark.parametrize(
    "text",
    [
        "20261015;133000 +99:99",  # was accepted: four days and four hours of shift
        "20261015;133000 +24:00",
        "20261015;133000 -05:60",
        "٢٠٢٦1015;133000",  # Arabic-Indic digits were accepted
        "10/15/٢٠٢٦ 13:30:00",
    ],
)
def test_bug33_flex_wall_time_offsets_and_digits(text: str) -> None:
    qt_error(lambda: ibkr_flex._flex_ts(text, "UTC", "loc"), "QT-INPUT-002")


def test_bug33_flex_wall_time_offset_in_range_still_accepted() -> None:
    """Guard: a real offset still shifts the wall time."""
    assert ibkr_flex._flex_ts("20261015;133000 -0500", "UTC", "loc") == ibkr_flex._flex_ts(
        "20261015;183000", "UTC", "loc"
    )


@pytest.mark.parametrize(
    "text", ["٢٠٢٦1015-13:30:00", "2026/10/١٥ 13:30:00", "١٧٩٢٠٧١٠٠٠", "20261015-13:30:00 +99:99", "20260230-00:00:00"]
)
def test_bug34_fix_local_ts_regex_times(text: str) -> None:
    """Times captured by `fix.local_ts_regex` were read with Unicode `\\d` and `str.isdecimal`, so Arabic-Indic
    digits made a timestamp; they are refused like every other timestamp text (and impossible dates too)."""
    from queuetruth.live.importers.fix44 import _regex_ts

    qt_error(lambda: _regex_ts(text, "loc", None), "QT-INPUT-002")


# ---- dq dataset rules the fixes must not tighten past the engine ---------------------------------------------------


def test_dq_partition_directory_may_be_a_symlink(tmp_path: Path) -> None:
    """Guard for the bug20 fix (passes on main; the first version of the fix failed it): containment is lexical,
    as in the engine (`check_rel_path`), so a dataset whose `mbo/` directory is a symlink to another volume still
    passes."""
    root = _dataset(tmp_path / "ds")
    (root / "mbo").rename(tmp_path / "elsewhere")
    (root / "mbo").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    rep = guarded("regression", lambda: _dq(root))
    assert not isinstance(rep, QtError), rep


@pytest.mark.parametrize("value", ["2026-06-15", "20619", 20619.0, True])
def test_bug35_dq_manifest_session_date_must_be_a_day_number(tmp_path: Path, value: Any) -> None:
    """Rust reads `session_date` as i32: an ISO string raised ValueError on main, and a digit string, a float or a
    bool passed `int()`; dq must not pass manifests the engine refuses."""
    root = _dataset(tmp_path / "ds")
    _edit_manifest(root, lambda d: d["partitions"][0].__setitem__("session_date", value))
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "session_date" in err.message


@pytest.mark.parametrize("value", [-13, 1, 127])
def test_bug35_dq_manifest_price_exp_outside_the_dataset_range(tmp_path: Path, value: int) -> None:
    """dq accepted `price_exp_by_instrument` values the engine and SPEC 1.2 refuse (outside -12..0)."""
    root = _dataset(tmp_path / "ds")
    _edit_manifest(root, lambda d: d.__setitem__("price_exp_by_instrument", {"1": value}))
    err = qt_error(lambda: _dq(root), "QT-INPUT-002")
    assert "price_exp" in err.message


# ---- decimal range rule (bug01) at its edges -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "9.99e30",
        "-9" * 1 + "9" * 30,  # 31 integer digits: the largest magnitude
        "1e-40",
        "0." + "0" * 39 + "1",
        "1." + "0" * 38 + "12" + "0" * 20,  # 40 fractional digits plus trailing zeros
        "1.3877787807814457e-17",  # float noise in a fee: 33 fractional digits
        "0e-999999999",
    ],
)
def test_decimal_range_accepted(text: str) -> None:
    """Guard: the edges of the bug01 range that must still import."""
    assert common.dec(text, "q") == Decimal(text)


@pytest.mark.parametrize("text", ["1e31", "-1" + "0" * 31, "1.5e-40", "0." + "0" * 40 + "1", "1." + "1" * 41])
def test_bug01_decimal_range_refused(text: str) -> None:
    qt_error(lambda: common.dec(text, "q"), "QT-INPUT-004")


@pytest.mark.parametrize(
    ("text", "exp", "unsigned", "want"),
    [
        ("9223372036854775807", 0, False, 2**63 - 1),
        ("-9223372036854775808", 0, False, -(2**63)),
        ("9223372036854775808", 0, False, None),
        ("9223372036854775808", 0, True, 2**63),
        ("18446744073709551615", 0, True, 2**64 - 1),
        ("18446744073709551616", 0, True, None),
        ("9.223372036854775807", -18, False, 2**63 - 1),
        ("9.223372036854775808", -18, False, None),
        ("-1", 0, True, None),
        ("1.5", 0, False, None),
        ("1" + "0" * 21, 0, False, None),
        ("0.000000001", -9, False, 1),
        ("1E+3", -2, False, 100_000),
    ],
)
def test_to_fixed_edges(text: str, exp: int, unsigned: bool, want: int | None) -> None:
    """Guard: the int64 / uint64 edges of the fixed-point conversion the bug01 rewrite of `to_fixed` must keep."""
    if want is None:
        qt_error(lambda: common.to_fixed(Decimal(text), exp, "q", unsigned=unsigned), "QT-INPUT-004")
    else:
        assert common.to_fixed(Decimal(text), exp, "q", unsigned=unsigned) == want


def test_to_fixed_of_a_product_wider_than_80_digits() -> None:
    """`dmul` products can need more than 80 digits (they are then computed wider, not rounded); `to_fixed` refuses
    them, since they have non-zero digits far below the units place, and converts trailing-zero-only products."""
    assert common.to_fixed(common.dmul(Decimal("1." + "0" * 50), Decimal("2." + "0" * 40)), -2, "q") == 200
    a = Decimal("1." + "0" * 59 + "1")
    qt_error(lambda: common.to_fixed(common.dmul(a, a), -2, "q"), "QT-INPUT-004")


# ---- the MsgSeqNum gap set of bug12, against a plain set ------------------------------------------------------------


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(
    st.lists(
        st.tuples(st.sampled_from(["add", "remove", "discard", "contains"]), st.integers(-5, 60), st.integers(0, 12)),
        max_size=60,
    )
)
def test_bug12_gaps_behave_like_a_set(ops: list[tuple[str, int, int]]) -> None:
    from queuetruth.live.importers.fix44 import _Gaps  # new with the bug12 fix: imported here so the file runs on main

    gaps = _Gaps()
    model: set[int] = set()
    for op, a, w in ops:
        b = a + w
        if op == "add":
            gaps.add(a, b)
            model |= set(range(a, b))
        elif op == "remove":
            assert gaps.remove(a, b) == len(model & set(range(a, b)))
            model -= set(range(a, b))
        elif op == "discard":
            gaps.discard(a)
            model.discard(a)
        else:
            assert (a in gaps) == (a in model)
        assert gaps.count() == len(model) and bool(gaps) == bool(model)
        assert all(lo < hi for lo, hi in zip(gaps.lo, gaps.hi, strict=True))  # non-empty ranges
        assert all(h < lo for h, lo in zip(gaps.hi, gaps.lo[1:], strict=False))  # sorted, disjoint, not adjacent
        assert set().union(*(range(lo, hi) for lo, hi in zip(gaps.lo, gaps.hi, strict=True))) == model
