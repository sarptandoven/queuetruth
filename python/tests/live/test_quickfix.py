"""QuickFIX FileLog / FileStore import (venue `fix44`, `queuetruth.live.import_quickfix`): synthetic sessions written
in each engine's exact format (`live._fixgen`), golden canonical rows, FIX 4.2 / 4.4 / 5.0SP2 semantics, rotation,
FileStores, sequence gaps, resets, PossDup resends, bad checksums, out-of-order lines, multi-session logs, data
dictionaries and venue naming. Test data only (fake CompIDs and accounts)."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pytest
from live._fixgen import (
    LOG_DIGITS,
    SOH,
    FixSession,
    _er,
    at,
    day_scenario,
    event_name,
    fix_time,
    instrument_map,
    log_name,
    render_events,
    render_log,
    render_store,
    stem,
    trunc,
    write,
)

from queuetruth.errors import QtError
from queuetruth.live import (
    IMPORTERS,
    ImportOptions,
    build_log,
    check_tables,
    import_quickfix,
    import_venue,
    instrument_scales,
    latency_stats,
    load_instrument_map,
    manifest,
    read_log,
    table_rows,
    to_schedule,
)
from queuetruth.live.importers.fixdict import STANDARD_TAGS

K1, K2, K3, K4, K5 = (f"fix44:ACCT1:{x}" for x in ("OID-1", "QT-2", "OID-3", "OID-4", "OID-5"))
_US = 1000
_SEND = 15_000  # outbound log line written 15 us after SendingTime


def _events(**_: Any) -> list[tuple[Any, ...]]:
    """Golden events of `day_scenario` for QuickFIX C++ ns logs, FIX 4.4 (columns of `_ev`)."""
    u, g, m = "unknown", "gateway_time", "match_time"
    rows = [
        (K1, 1, "submit_sent", 100, 1012500, None, None, None, None, None, at(1000) + _SEND, None, None, u, "ms"),
        (K1, 2, "ack", 100, 1012500, 0, 100, None, None, None, None, at(1000, 400), at(1000, 650, 123), g, "us"),
        (K1, 3, "fill", 40, 1012500, 40, 60, "E-1-1", None, "maker", None, at(2000, 10), at(2000, 210), m, "us"),
        (K1, 4, "amend_sent", 80, 1012500, None, None, None, None, None, at(3000) + _SEND, None, None, u, "ms"),
        (K1, 5, "amend_ack", 80, 1012500, 40, 40, None, None, None, None, at(3000, 300), at(3000, 520), g, "us"),
        (K1, 6, "cancel_sent", None, None, None, None, None, None, None, at(4000) + _SEND, None, None, u, "ms"),
        (K1, 7, "cancel_ack", None, None, 40, 0, None, None, None, None, at(4000, 310), at(4000, 530), g, "us"),
        (K2, 1, "submit_sent", 50, 1015000, None, None, None, None, None, at(5000) + _SEND, None, None, u, "ms"),
        (K2, 2, "post_only_reject", None, None, 0, 0, None, None, None, None, at(5000, 250), at(5000, 470), g, "us"),
        (K3, 1, "submit_sent", 30, 1014000, None, None, None, None, None, at(6000) + _SEND, None, None, u, "ms"),
        (K3, 2, "ack", 30, 1014000, 0, 30, None, None, None, None, at(6000, 300), at(6000, 500), g, "us"),
        (K3, 3, "fill", 10, 1014000, 10, 20, "E-3-1", None, "taker", None, at(6000, 300), at(6000, 501), m, "us"),
        (K3, 4, "expire", None, None, 10, 0, None, None, None, None, at(6000, 300), at(6000, 502), g, "us"),
        (K3, 5, "fill_bust", 10, 1014000, 0, 0, "E-3-1x", "E-3-1", None, None, at(9000), at(9000, 200), g, "us"),
        (K4, 1, "submit_sent", 10, 1010000, None, None, None, None, None, at(7000) + _SEND, None, None, u, "ms"),
        (K4, 2, "ack", 10, 1010000, 0, 10, None, None, None, None, at(7000, 300), at(7000, 480), g, "us"),
        (K4, 3, "cancel_sent", None, None, None, None, None, None, None, at(7500) + _SEND, None, None, u, "ms"),
        (K4, 4, "cancel_reject", None, None, None, None, None, None, None, None, at(7500, 300), at(7500, 490), g, "us"),
        (K4, 5, "done", None, None, 0, 0, None, None, None, None, at(10000), at(10000, 200), g, "us"),
        (K5, 1, "submit_sent", 5, 1009000, None, None, None, None, None, at(8000) + _SEND, None, None, u, "ms"),
        (K5, 2, "ack", 5, 1009000, 0, 5, None, None, None, None, at(8000, 300), at(8000, 470), g, "us"),
        (K5, 3, "expire", None, None, 0, 0, None, None, None, None, at(8500, 100), at(8500, 300), g, "us"),
    ]
    return sorted(rows, key=lambda r: (r[0], r[1]))


_EV_COLS = (
    "order_key",
    "event_seq",
    "event_type",
    "qty",
    "px",
    "cum_qty",
    "leaves_qty",
    "fill_id",
    "ref_fill_id",
    "liquidity",
    "ts_local_send",
    "ts_exchange",
    "ts_local_recv",
    "ts_exchange_kind",
    "ts_precision",
)
_REASONS = {
    (K2, 2): "103=99 Post only order would take liquidity",
    (K3, 5): "trade bust",
    (K4, 4): "35=9 434=1 102=0 too late",
    (K4, 5): "150=3 done for day",
    (K5, 3): "150=4 unsolicited cancel (no OrderCancelRequest or mass cancel sent): self-trade prevention",
}
_ORDERS = {
    K1: ("OID-1", ["QT-1", "QT-1b", "QT-1c"], "buy", "day", False, 100, 1012500, "101.25"),
    K2: (None, ["QT-2"], "sell", "post_only", True, 50, 1015000, "101.50"),
    K3: ("OID-3", ["QT-3"], "buy", "ioc", False, 30, 1014000, "101.40"),
    K4: ("OID-4", ["QT-4", "QT-4b"], "buy", "day", False, 10, 1010000, "101.00"),
    K5: ("OID-5", ["QT-5"], "buy", "gtc", False, 5, 1009000, "100.90"),
}


def _ev(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [tuple(r[c] for c in _EV_COLS) for r in rows]


def _write_session(tmp: Path, s: FixSession, engine: str, *, name: str | None = None) -> str:
    return write(tmp / (name or log_name(s, engine)), render_log(s.records, engine))


def _run(paths: list[str], tmp: Path, *, imap_venue: str = "fix44", **fix: Any):
    imap = load_instrument_map(instrument_map(tmp / "im.csv", imap_venue))
    res = IMPORTERS["fix44"](paths, ImportOptions(imap=imap, extra={"fix": fix}))
    orders, events = build_log(res.orders, res.events)
    errors, _, _ = check_tables(orders, events)
    assert errors == []
    return table_rows(orders), table_rows(events), res


def _warn(res: Any, needle: str) -> str:
    hits = [w for w in res.warnings if needle in w]
    assert hits, f"no warning contains {needle!r}: {res.warnings}"
    return hits[0]


def _counters(res: Any, needle: str) -> dict[str, int]:
    """`key=value` counters of the first warning containing `needle`."""
    w = _warn(res, needle)
    out = {}
    for part in w.split():
        k, sep, v = part.partition("=")
        if sep and v.isdigit():
            out[k] = int(v)
    return out


# ---- golden rows ----------------------------------------------------------------------------------------------


def test_quickfix_cpp_ns_fix44_golden_rows(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    orders, events, res = _run([_write_session(tmp_path, s, "cpp_ns")], tmp_path)
    assert _ev(events) == _events()
    for e in events:
        assert e["reason"] == _REASONS.get((e["order_key"], e["event_seq"])), (e["order_key"], e["event_seq"])
        assert e["source"] == "fix_dropcopy" and e["raw"] and SOH not in e["raw"]
    fill = next(e for e in events if e["fill_id"] == "E-1-1")
    assert (fill["fee"], fill["fee_ccy"]) == (pytest.approx(0.4), "USD")
    assert {o["order_key"] for o in orders} == set(_ORDERS)
    for o in orders:
        oid, chain, side, tif, po, qty, px, px_str = _ORDERS[o["order_key"]]
        got = (o["venue_order_id"], o["client_order_id_chain"], o["side"], o["tif"], o["post_only"], o["orig_qty"])
        assert got == (oid, chain, side, tif, po, qty)
        assert (o["limit_px"], o["px_str"], o["price_exp"], o["size_exp"], o["instrument_id"]) == (
            px,
            px_str,
            -4,
            0,
            201,
        )
        assert (o["venue"], o["account"], o["instrument"], o["order_type"], o["replaces_order_key"]) == (
            "fix44",
            "ACCT1",
            "ACME",
            "limit",
            None,
        )
    assert res.n_fills == 2 and res.dedupe_dropped == 0 and res.unmatched_fills == 0
    c = _counters(res, "FIX session FIX.4.4:QTCLIENT->SYNTHVENUE")
    assert c["app_messages"] == 22 and len(c) == 1  # nothing else to report: no gaps, resets or duplicates
    assert "unsolicited_cancels=1" in _warn(res, "FIX import counters") and "tif_default_day=1" in _warn(
        res, "counters"
    )
    assert "quickfix_cpp_ns=" in _warn(res, "inputs: 1 message log(s)")


_MATRIX = [
    ("cpp_ns", "FIX.4.2"),
    ("cpp_s", "FIX.4.2"),
    ("cpp_ms", "FIX.4.4"),
    ("qfj_plain", "FIX.4.4"),
    ("qfj_ms", "FIX.4.4"),
    ("qfn", "FIX.4.4"),
    ("go", "FIXT.1.1"),
    ("go", "FIX.4.4"),
]
_PREC = {0: "s", 3: "ms", 6: "us", 9: "ns"}


def _expected_for(engine: str, begin: str) -> list[tuple[Any, ...]]:
    d = LOG_DIGITS[engine]
    rows = []
    for r in _events():
        r = list(r)
        out = r[2] in ("submit_sent", "amend_sent", "cancel_sent")
        if out:
            r[10] = r[10] - _SEND if d is None else trunc(r[10], d)
            r[14] = "ms" if d is None or d >= 3 else _PREC[d]
        else:
            r[12] = None if d is None else trunc(r[12], d)
            r[14] = "us" if d is None or d >= 6 else _PREC[d]
        if begin == "FIX.4.2" and r[2] == "fill":
            r[9] = "unknown"  # FIX 4.2 has no LastLiquidityInd
        rows.append(tuple(r))
    return rows


@pytest.mark.parametrize(("engine", "begin"), _MATRIX, ids=[f"{e}-{b}" for e, b in _MATRIX])
def test_quickfix_engines_and_versions_match_the_golden(tmp_path: Path, engine: str, begin: str) -> None:
    s = day_scenario(begin)
    orders, events, res = _run([_write_session(tmp_path, s, engine)], tmp_path)
    assert _ev(events) == _expected_for(engine, begin)
    assert {(e["order_key"], e["event_seq"]): e["reason"] for e in events if e["reason"]} == _REASONS
    assert [o["tif"] for o in sorted(orders, key=lambda o: o["order_key"])] == ["day", "ioc", "day", "gtc", "post_only"]
    if begin == "FIXT.1.1":
        assert "DefaultApplVerID FIX.5.0SP2" in _warn(res, "DefaultApplVerID")


def test_fix42_exec_trans_type_correct_and_status(tmp_path: Path) -> None:
    s = FixSession(begin="FIX.4.2")
    s.out(
        at(0),
        "D",
        [
            ("11", "C1"),
            ("1", "ACCT1"),
            ("55", "ACME"),
            ("54", "1"),
            ("38", "10"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "1"),
        ],
    )
    common = dict(oid="O1", cid="C1", qty="10", px="101.00")
    s.inn(at(1), "8", _er(s, "new", **common, cum="0", leaves="10", tt=at(1), exec_id="X0"))
    s.inn(at(2), "8", _er(s, "fill", **common, cum="10", leaves="0", tt=at(2), exec_id="E1", last=("10", "101.00")))
    corr = _er(s, "fill", **common, cum="10", leaves="0", tt=at(3), exec_id="E1c", ref="E1", last=("10", "100.99"))
    corr = [(t, "2") if t == "20" else (t, v) for t, v in corr]  # ExecTransType 2 = correct
    s.inn(at(3), "8", corr)
    status = [
        (t, "3") if t == "20" else (t, v)
        for t, v in _er(s, "new", **common, cum="10", leaves="0", tt=at(4), exec_id="S1")
    ]
    s.inn(at(4), "8", status)
    _, events, res = _run([_write_session(tmp_path, s, "cpp_ns")], tmp_path)
    assert [(e["event_type"], e["px"], e["ref_fill_id"]) for e in events] == [
        ("submit_sent", 1010000, None),
        ("ack", 1010000, None),
        ("fill", 1010000, None),
        ("fill_correct", 1009900, "E1"),
    ]
    assert res.skipped == {"ExecTransType 20=3 (status) (no canonical event)": 1}


def test_fix40_style_reports_without_exec_type_use_ordstatus(tmp_path: Path) -> None:
    s = FixSession(begin="FIX.4.4")
    s.out(
        at(0),
        "D",
        [
            ("11", "C1"),
            ("1", "ACCT1"),
            ("55", "ACME"),
            ("54", "2"),
            ("38", "4"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "0"),
        ],
    )
    for i, (status, cum, last) in enumerate(
        [("0", "0", None), ("1", "1", ("1", "101.00")), ("2", "4", ("3", "101.00"))]
    ):
        body = [
            ("37", "O1"),
            ("11", "C1"),
            ("17", f"E{i}"),
            ("39", status),
            ("54", "2"),
            ("55", "ACME"),
            ("38", "4"),
            ("44", "101.00"),
            ("14", cum),
            ("151", str(4 - int(cum))),
        ]
        body += [("32", last[0]), ("31", last[1])] if last else []
        s.inn(at(10 + i), "8", [*body, ("60", fix_time(at(10 + i), 3))])
    _, events, res = _run([_write_session(tmp_path, s, "qfj_ms")], tmp_path)
    assert [(e["event_type"], e["qty"], e["cum_qty"]) for e in events] == [
        ("submit_sent", 4, None),
        ("ack", 4, 0),
        ("fill", 1, 1),
        ("fill", 3, 4),
    ]
    assert "exec_type_from_ordstatus=3" in _warn(res, "FIX import counters")


# ---- files: names, rotation, compression, stores, event logs ------------------------------------------------------


def test_rotated_backup_logs_any_order_and_directory_input(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    recs = s.records
    d = tmp_path / "FileLogPath"
    a = write(d / f"{stem(s)}.messages.backup.1.log", render_log(recs[:9], "cpp_ns"))
    b = write(d / f"{stem(s)}.messages.backup.2.log", render_log(recs[9:20], "cpp_ns"))
    c = write(d / f"{stem(s)}.messages.current.log", render_log(recs[20:], "cpp_ns"))
    write(
        d / f"{stem(s)}.event.current.log",
        render_events([(at(0), "Created session"), (at(1), "Received logon")], "cpp_ns"),
    )
    _, ev_files, _ = _run([c, a, b], tmp_path)
    assert _ev(ev_files) == _events()
    out = tmp_path / "out"
    summ = import_quickfix([str(d)], out, instrument_map=instrument_map(tmp_path / "im2.csv"))
    assert sorted(Path(i.path).name for i in summ.inputs) == sorted(p.name for p in d.iterdir())
    assert _ev(table_rows(read_log(out)[1])) == _events()
    assert any("QuickFIX event log FIX.4.4-QTCLIENT-SYNTHVENUE: lines=2 logon=1 other=1" in w for w in summ.warnings)


def test_logrotate_gzip_copy_and_overlapping_files_are_deduped(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    full = render_log(s.records, "cpp_ns")
    lines = full.split(b"\n")
    rotated = write(tmp_path / f"{stem(s)}.messages.current.log.1.gz", gzip.compress(b"\n".join(lines[:15]) + b"\n"))
    current = write(tmp_path / f"{stem(s)}.messages.current.log", b"\n".join(lines[10:]))
    _, events, res = _run([current, rotated], tmp_path)
    assert _ev(events) == _events()
    assert res.dedupe_dropped == 5
    assert "5 byte-identical duplicate message line(s) dropped" in _warn(res, "byte-identical")


@pytest.mark.parametrize("engine", ["cpp_ns", "qfj_plain", "go", "qfn"])
def test_filestore_body_header_fill_outbound_messages_missing_from_the_log(tmp_path: Path, engine: str) -> None:
    s = day_scenario("FIX.4.4")
    lost = next(r for r in s.records if r.mt == "D" and "11=QT-3" in r.text)
    log = _write_session(
        tmp_path, FixSession(records=[r for r in s.records if r is not lost]), engine, name=log_name(s, engine)
    )
    body, header = render_store(s.records, engine)
    store = write(tmp_path / "store" / f"{stem(s)}.body", body)
    write(tmp_path / "store" / f"{stem(s)}.header", header)
    _, events, res = _run([log, store], tmp_path)
    sub = next(e for e in events if e["order_key"] == K3 and e["event_type"] == "submit_sent")
    assert sub["event_seq"] == 1 and sub["ts_local_send"] == at(6000)  # the store copy: SendingTime(52)
    assert [e["event_type"] for e in events if e["order_key"] == K3][:2] == ["submit_sent", "ack"]
    fmt = {"cpp_ns": "text_space", "qfj_plain": "binary", "go": "text_lines", "qfn": "text_lines"}[engine]
    assert f"header {fmt}=1" in _warn(res, "FileStore(s)")
    n_out = sum(1 for r in s.records if r.out)
    assert f"{n_out - 1} FileStore message(s) already in the message logs" in _warn(res, "already in the message logs")
    assert "recovered_from_store=1" in _warn(res, "FIX session")


def test_filestore_alone_gives_the_send_side(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    body, header = render_store(s.records, "cpp_ns")
    store = write(tmp_path / f"{stem(s)}.body", body)
    write(tmp_path / f"{stem(s)}.header", header)
    _, events, _ = _run([store], tmp_path)
    assert {e["event_type"] for e in events} == {"submit_sent", "amend_sent", "cancel_sent"}
    body_only = write(tmp_path / "b" / f"{stem(s)}.body", body)  # no index: scanned by BodyLength
    _, events2, _ = _run([body_only], tmp_path)
    assert _ev(events2) == _ev(events)


def test_seqnums_file_reports_messages_after_the_end_of_the_logs(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    log = _write_session(tmp_path, s, "cpp_ns")
    write(tmp_path / f"{stem(s)}.seqnums", f"{s.out_seq + 4:020d} : {s.in_seq + 1:020d}".encode())
    _, _, res = _run([log, str(tmp_path / f"{stem(s)}.seqnums")], tmp_path)
    assert "3 outbound message(s) after the end of the logs" in _warn(res, "next outbound MsgSeqNum")
    assert not any("inbound message(s) after the end" in w for w in res.warnings)


# ---- validation: checksum, body length, truncation, delimiters, continuation ---------------------------------------


def test_bad_checksum_body_length_and_truncated_lines_are_counted(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    raw = render_log(s.records, "cpp_ns").decode("latin-1").split("\n")[:-1]
    i_fill = next(i for i, ln in enumerate(raw) if "17=E-1-1" in ln)
    good = int(raw[i_fill][-4:-1])
    raw[i_fill] = raw[i_fill][:-4] + f"{(good + 1) % 256:03d}" + SOH  # wrong CheckSum
    i_ack = next(i for i, ln in enumerate(raw) if "17=X-5-0" in ln)
    raw[i_ack] = raw[i_ack].replace("\x019=", "\x019=1", 1)  # wrong BodyLength
    raw.append(raw[0][: len(raw[0]) // 2])  # crashed writer: truncated last line
    log = write(tmp_path / log_name(s, "cpp_ns"), ("\n".join(raw) + "\n").encode("latin-1"))
    _, events, res = _run([log], tmp_path)
    assert not any(e["fill_id"] == "E-1-1" and e["event_type"] == "fill" for e in events if not e["derived"])
    derived = next(e for e in events if e["derived"])  # CumQty 0 -> 40 at the replace: the lost fill comes back
    assert (derived["order_key"], derived["qty"], derived["px"], derived["liquidity"]) == (K1, 40, 1012500, "unknown")
    assert "bad_body_length=1 bad_checksum=1 truncated=1" in _warn(res, "excluded 3 message(s) failing FIX validation")
    _, events_keep, res_keep = _run([log], tmp_path, checksum="keep")
    assert any(e["fill_id"] == "E-1-1" for e in events_keep) and not any(e["derived"] for e in events_keep)
    assert "imported 2 message(s)" in _warn(res_keep, "fix.checksum=keep")


@pytest.mark.parametrize("delim", ["|", "^A"])
def test_pipe_and_caret_a_delimited_logs_are_checksummed(tmp_path: Path, delim: str) -> None:
    s = day_scenario("FIX.4.4")
    log = write(tmp_path / log_name(s, "cpp_ns"), render_log(s.records, "cpp_ns", delim=delim))
    _, events, res = _run([log], tmp_path)
    assert _ev(events) == _events()
    assert not any("failing FIX validation" in w for w in res.warnings)


def test_message_with_an_embedded_newline_is_rejoined(tmp_path: Path) -> None:
    s = FixSession()
    s.out(
        at(0),
        "D",
        [
            ("11", "C1"),
            ("1", "ACCT1"),
            ("55", "ACME"),
            ("54", "1"),
            ("38", "1"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "1"),
        ],
    )
    s.inn(
        at(1),
        "8",
        _er(
            s,
            "reject",
            oid="NONE",
            cid="C1",
            qty="1",
            px="101.00",
            cum="0",
            leaves="0",
            tt=at(1),
            exec_id="X",
            extra=[("58", "line one\nline two")],
        ),
    )
    _, events, res = _run([_write_session(tmp_path, s, "cpp_ns")], tmp_path)
    assert events[-1]["event_type"] == "reject" and events[-1]["reason"] == "line one\nline two"
    assert not any("failing FIX validation" in w for w in res.warnings)


# ---- session layer -------------------------------------------------------------------------------------------------


def _scenario_records(begin: str = "FIX.4.4") -> tuple[FixSession, list[Any]]:
    s = day_scenario(begin)
    return s, list(s.records)


def test_inbound_gap_recovered_by_possdup_resend(tmp_path: Path) -> None:
    s, recs = _scenario_records()
    gap = [r for r in recs if not r.out and r.mt == "8" and ("17=E-1-1" in r.text or "17=X-1-2" in r.text)]
    kept = [r for r in recs if r not in gap]
    resend = FixSession(begin=s.begin)
    for r in gap:  # the venue resends them (43=Y) after our ResendRequest, at the end of the log
        txt = r.text.replace(f"{SOH}34={r.seq}{SOH}", f"{SOH}34={r.seq}{SOH}43=Y{SOH}")
        from live._fixgen import fix_message

        fields = [tuple(p.split("=", 1)) for p in txt.split(SOH)[2:-2]]
        resend.records.append(type(r)(at(11000), False, r.seq, fix_message(s.begin, [], fields), "8"))
    dup = type(recs[0])(at(11500), False, gap[0].seq, resend.records[0].text, "8")  # second copy of a resend
    dup.text = dup.text.replace(fix_time(at(11000), 3), fix_time(at(11000), 3))
    log = _write_session(tmp_path, FixSession(records=kept + resend.records), "cpp_ns", name=log_name(s, "cpp_ns"))
    _, events, res = _run([log], tmp_path)
    c = _counters(res, "FIX session")
    assert (c["gaps_in"], c["gap_msgs_in"], c["recovered_by_resend"]) == (1, 2, 2)
    assert "unrecovered_in" not in c
    k1 = [(e["event_type"], e["cum_qty"]) for e in events if e["order_key"] == K1]
    assert k1 == [
        ("submit_sent", None),
        ("ack", 0),
        ("fill", 40),
        ("amend_sent", None),
        ("amend_ack", 40),
        ("cancel_sent", None),
        ("cancel_ack", 40),
    ]
    fill = next(e for e in events if e["fill_id"] == "E-1-1")
    assert fill["ts_local_recv"] == at(11000) and fill["reason"] == "PossDup resend (43=Y) received after a gap"
    # a PossDup copy of a message already received is dropped
    log2 = write(tmp_path / "b" / log_name(s, "cpp_ns"), render_log(recs + resend.records[:1], "cpp_ns"))
    _, events2, res2 = _run([log2], tmp_path)
    assert _ev(events2) == _events() and "possdup_dropped=1" in _warn(res2, "FIX session")


def test_unrecovered_gap_yields_a_derived_fill_and_a_warning(tmp_path: Path) -> None:
    s, recs = _scenario_records()
    lost = next(r for r in recs if "17=E-3-1" in r.text and "150=F" in r.text)
    log = _write_session(
        tmp_path, FixSession(records=[r for r in recs if r is not lost]), "cpp_ns", name=log_name(s, "cpp_ns")
    )
    _, events, res = _run([log], tmp_path)
    k3 = [e for e in events if e["order_key"] == K3]
    assert [e["event_type"] for e in k3] == ["submit_sent", "ack", "fill", "expire"]
    d = k3[2]
    assert (d["derived"], d["qty"], d["px"], d["fill_id"], d["ts_exchange_kind"]) == (
        True,
        10,
        1014000,
        None,
        "unknown",
    )
    assert d["reason"].startswith("not in the log: CumQty(14) 0 -> 10")
    assert "bust_correct_of_unknown_fill=1" in _warn(res, "counters")  # the later bust references the lost fill
    assert "1 inbound message(s) lost in sequence gaps" in _warn(res, "never resent")


def test_session_reset_and_clordid_reuse_next_day(tmp_path: Path) -> None:
    s = FixSession()
    nos = [("1", "ACCT1"), ("55", "ACME"), ("40", "2"), ("54", "1"), ("38", "1"), ("44", "101.00"), ("59", "0")]
    for day, oid in ((0, "OA"), (1, "OB")):
        base = day * 86_400_000
        if day:
            s.out_seq = s.in_seq = 0
        logon = [("98", "0"), ("108", "30")] + ([("141", "Y")] if day else [])
        s.out(at(base), "A", logon)
        s.inn(at(base + 1), "A", logon)
        s.out(at(base + 100), "D", [("11", "1"), *nos])
        s.inn(
            at(base + 101),
            "8",
            _er(
                s,
                "new",
                oid=oid,
                cid="1",
                qty="1",
                px="101.00",
                cum="0",
                leaves="1",
                tt=at(base + 101),
                exec_id=f"X{day}",
            ),
        )
    orders, _, res = _run([_write_session(tmp_path, s, "cpp_ns")], tmp_path)
    assert sorted(o["order_key"] for o in orders) == ["fix44:ACCT1:OA", "fix44:ACCT1:OB"]
    assert all(o["client_order_id"] == "1" for o in orders)
    c = _counters(res, "FIX session")
    assert c["resets"] == 1 and "seq_regressions" not in c and "possdup_dropped" not in c
    assert "clordid_reused=1" in _warn(res, "counters")


def test_out_of_order_lines_are_put_back_in_msgseqnum_order(tmp_path: Path) -> None:
    s, recs = _scenario_records()
    i = next(i for i, r in enumerate(recs) if "17=X-3-0" in r.text)  # ack of order 3, then its fill
    recs[i], recs[i + 1] = recs[i + 1], recs[i]
    log = _write_session(tmp_path, FixSession(records=recs), "cpp_ns", name=log_name(s, "cpp_ns"))
    _, events, res = _run([log], tmp_path)
    assert [e["event_type"] for e in events if e["order_key"] == K3] == [
        "submit_sent",
        "ack",
        "fill",
        "expire",
        "fill_bust",
    ]
    c = _counters(res, "FIX session")
    assert (c["gaps_in"], c["out_of_order"]) == (1, 1) and "unrecovered_in" not in c
    assert "log time went backwards 1 time(s)" in _warn(res, "went backwards")


def test_sequence_reset_gap_fill_closes_the_gap(tmp_path: Path) -> None:
    s = FixSession()
    s.out(at(0), "A", [("98", "0"), ("108", "30")])
    s.inn(at(1), "A", [("98", "0"), ("108", "30")])
    s.inn(at(5), "0", [], seq=5)  # gap 2..4
    s.inn(at(6), "4", [("123", "Y"), ("36", "5")], seq=2, possdup=True)  # admin messages 2..4 gap-filled
    _, _, res = _run([_write_session(tmp_path, s, "cpp_ns")], tmp_path)
    c = _counters(res, "FIX session")
    assert (c["gaps_in"], c["gap_msgs_in"], c["recovered_by_resend"], c["gap_filled_admin"]) == (1, 3, 1, 2)
    assert "unrecovered_in" not in c


# ---- semantics: rejects, replaces, multi-session, venues, dictionaries ---------------------------------------------


def test_business_and_session_rejects_of_requests(tmp_path: Path) -> None:
    s = FixSession()
    nos = [("1", "ACCT1"), ("55", "ACME"), ("40", "2"), ("54", "1"), ("38", "1"), ("44", "101.00"), ("59", "0")]
    s.out(at(0), "D", [("11", "B1"), *nos])
    s.inn(at(1), "j", [("45", "1"), ("372", "D"), ("379", "B1"), ("380", "3"), ("58", "unsupported")])
    d2 = s.out(at(10), "D", [("11", "S1"), *nos])
    s.inn(at(11), "3", [("45", str(d2.seq)), ("372", "D"), ("373", "1"), ("58", "Required tag missing")])
    s.inn(
        at(20),
        "8",
        _er(s, "new", oid="O9", cid="Z9", qty="1", px="101.00", cum="0", leaves="1", tt=at(20), exec_id="Y"),
    )
    s.out(at(21), "3", [("45", str(s.in_seq)), ("373", "5")])  # the local engine rejects that report
    _, events, res = _run([_write_session(tmp_path, s, "cpp_ns")], tmp_path)
    assert [(e["order_key"], e["event_type"], e["reason"]) for e in events if e["event_type"] == "reject"] == [
        ("fix44:ACCT1:B1", "reject", "35=j 372=D 380=3 unsupported"),
        ("fix44:ACCT1:S1", "reject", "35=3 372=D 373=1 Required tag missing"),
    ]
    assert "inbound_reports_rejected_by_local_engine=1" in _warn(res, "FIX session")


def test_replace_answered_with_a_new_order_links_replaces_order_key(tmp_path: Path) -> None:
    s = FixSession()
    nos = [("1", "ACCT1"), ("55", "ACME"), ("40", "2"), ("54", "1"), ("59", "0")]
    s.out(at(0), "D", [("11", "A"), *nos, ("38", "5"), ("44", "101.00")])
    s.inn(
        at(1), "8", _er(s, "new", oid="O1", cid="A", qty="5", px="101.00", cum="0", leaves="5", tt=at(1), exec_id="X1")
    )
    s.out(at(2), "G", [("11", "B"), ("41", "A"), ("37", "O1"), *nos, ("38", "5"), ("44", "101.01")])
    s.inn(
        at(3),
        "8",
        _er(
            s, "cancel", oid="O1", cid="B", orig="A", qty="5", px="101.00", cum="0", leaves="0", tt=at(3), exec_id="X2"
        ),
    )
    s.inn(
        at(4),
        "8",
        _er(s, "new", oid="O2", cid="B", orig="A", qty="5", px="101.01", cum="0", leaves="5", tt=at(4), exec_id="X3"),
    )
    out = tmp_path / "out"
    summ = import_quickfix(_write_session(tmp_path, s, "cpp_ns"), out, instrument_map=instrument_map(tmp_path / "m"))
    orders, events = (table_rows(t) for t in read_log(out))
    by = {o["order_key"]: o for o in orders}
    assert by["fix44:ACCT1:O2"]["replaces_order_key"] == "fix44:ACCT1:O1"
    assert by["fix44:ACCT1:O1"]["replaces_order_key"] is None
    assert [e["event_type"] for e in events if e["order_key"] == "fix44:ACCT1:O1"] == [
        "submit_sent",
        "ack",
        "amend_sent",
        "cancel_ack",
    ]
    assert summ.n_orders == 2 and "replaced_by_new_order=1" in " ".join(summ.warnings)


def test_order_entry_plus_drop_copy_sessions_are_deduped(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    dc = FixSession(local="QTDROP", remote="SYNTHVENUE")
    for r in s.records:
        if not r.out and r.mt == "8":
            body = [
                tuple(p.split("=", 1))
                for p in r.text.split(SOH)[2:-2]
                if p.split("=", 1)[0] not in ("35", "49", "56", "34", "52")
            ]
            new_id = [(k, (v + "-dc") if k == "17" and "E-" not in v else v) for k, v in body]  # own ExecIDs on acks
            dc.inn(r.t + 50_000, "8", new_id)
    a = _write_session(tmp_path, s, "cpp_ns")
    b = _write_session(tmp_path, dc, "cpp_ns")
    _, events, res = _run([b, a], tmp_path)
    assert _ev(events) == _events()
    assert "cross_session_duplicates=13" in _warn(res, "FIX import counters")  # every drop-copy report
    assert "repeated_reports_dropped" not in _warn(res, "FIX import counters")
    assert res.dedupe_dropped == 13


def test_venue_name_session_venues_and_instrument_map(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    log = _write_session(tmp_path, s, "cpp_ns")
    out = tmp_path / "out"
    summ = import_venue(
        {
            "venue": "fix44",
            "inputs": [{"path": str(tmp_path)}],
            "out": str(out),
            "instrument_map": {"path": instrument_map(tmp_path / "x" / "map.csv", "cme")},
            "fix": {"venue_name": "cme"},
        }
    )
    assert summ.venue == "cme" and manifest(out)["venues"] == ["cme"]
    orders = table_rows(read_log(out)[0])
    assert {o["venue"] for o in orders} == {"cme"} and all(o["order_key"].startswith("cme:ACCT1:") for o in orders)
    assert {o["instrument_id"] for o in orders} == {201}
    assert instrument_scales(out) == [
        {
            "venue": "cme",
            "instrument": "ACME",
            "instrument_id": 201,
            "price_exp": -4,
            "size_exp": 0,
            "scale_source": "instrument_map",
            "n_orders": 5,
        }
    ]
    assert summ.coverage is not None and summ.coverage.fills_only is False
    # a second venue's session in the same import
    s2 = FixSession(local="QTCLIENT", remote="OTHERX")
    s2.out(
        at(0),
        "D",
        [
            ("11", "Z1"),
            ("1", "ACCT1"),
            ("55", "ACME"),
            ("54", "1"),
            ("38", "1"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "0"),
        ],
    )
    log2 = _write_session(tmp_path, s2, "cpp_ns")
    out2 = tmp_path / "out2"
    with pytest.warns(UserWarning, match="unmapped_instruments"):
        summ2 = import_quickfix([log, log2], out2, venue_name="cme", session_venues={"OTHERX": "otherx"})
    assert summ2.venue == "cme,otherx" and manifest(out2)["venues"] == ["cme", "otherx"]
    assert summ2.warnings[0].startswith("unmapped_instruments: 2 instrument(s)")
    with pytest.raises(QtError, match="no session matches"):
        import_quickfix([log], tmp_path / "o3", session_venues={"NOPE": "x"})
    with pytest.raises(QtError, match="sim:"):
        import_quickfix([log], tmp_path / "o4", venue_name="sim:x")


def test_venue_names_are_lowercased_at_import_and_in_the_instrument_map(tmp_path: Path) -> None:
    """Open conflict 12 (E53): `fix.venue_name = CME` and an instrument map written with `Cme` give the canonical
    venue `cme` (order keys, manifest) and the map row is found case-insensitively."""
    s = day_scenario("FIX.4.4")
    _write_session(tmp_path, s, "cpp_ns")
    out = tmp_path / "out"
    summ = import_venue(
        {
            "venue": "fix44",
            "inputs": [{"path": str(tmp_path)}],
            "out": str(out),
            "instrument_map": {"path": instrument_map(tmp_path / "x" / "map.csv", "Cme")},
            "fix": {"venue_name": "CME"},
        }
    )
    assert summ.venue == "cme" and manifest(out)["venues"] == ["cme"]
    orders = table_rows(read_log(out)[0])
    assert {o["venue"] for o in orders} == {"cme"} and all(o["order_key"].startswith("cme:ACCT1:") for o in orders)
    assert {o["instrument_id"] for o in orders} == {201}
    imap = load_instrument_map(tmp_path / "x" / "map.csv")
    assert imap.lookup("CME", "ACME", None) is imap.lookup("cme", "ACME", None) is not None


def test_coinbase_fix_size_decrease_keeps_priority(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    log = _write_session(tmp_path, s, "cpp_ns")
    out = tmp_path / "cb"
    import_quickfix([log], out, venue_name="coinbase", instrument_map=instrument_map(tmp_path / "m.csv", "coinbase"))
    ev = table_rows(read_log(out)[1])
    amend = next(e for e in ev if e["event_type"] == "amend_ack")
    assert amend["amend_keeps_priority"] is True
    spec, amends = to_schedule(str(out), entry="exchange_ack", latency_ns=None)
    assert table_rows(amends) and not any(".leg" in r for r in spec.column("shadow_id").to_pylist())
    out2 = tmp_path / "plain"
    import_quickfix([log], out2, instrument_map=instrument_map(tmp_path / "m2.csv"))
    assert (
        next(e for e in table_rows(read_log(out2)[1]) if e["event_type"] == "amend_ack")["amend_keeps_priority"] is None
    )


_STD_FIELDS = "".join(
    f'  <field number="{n}" name="{name}" type="STRING"/>\n' for name, n in STANDARD_TAGS.items() if name != "ExecType"
)
_DICT = (
    """<fix type="FIX" major="4" minor="4" servicepack="0">
 <header/><trailer/>
 <messages><message name="ExecutionReport" msgtype="8" msgcat="app"/></messages>
 <fields>
"""
    + _STD_FIELDS
    + """
  <field number="150" name="ExecType" type="CHAR">
   <value enum="0" description="NEW"/><value enum="Z" description="TRADE"/>
  </field>
  <field number="9730" name="LiquidityFlag" type="CHAR">
   <value enum="A" description="ADDED"/><value enum="R" description="REMOVED"/>
  </field>
 </fields>
</fix>
"""
)


def test_data_dictionary_maps_custom_exec_type_and_liquidity(tmp_path: Path) -> None:
    s = FixSession()
    s.out(
        at(0),
        "D",
        [
            ("11", "C1"),
            ("1", "ACCT1"),
            ("55", "ACME"),
            ("54", "1"),
            ("38", "2"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "1"),
        ],
    )
    s.inn(
        at(1), "8", _er(s, "new", oid="O1", cid="C1", qty="2", px="101.00", cum="0", leaves="2", tt=at(1), exec_id="X")
    )
    fill = _er(
        s,
        "fill",
        oid="O1",
        cid="C1",
        qty="2",
        px="101.00",
        cum="2",
        leaves="0",
        tt=at(2),
        exec_id="E",
        last=("2", "101.00"),
        extra=[("9730", "R"), ("5999", "venue-tag")],
    )
    s.inn(at(2), "8", [(t, "Z") if t == "150" else (t, v) for t, v in fill])
    dd = write(tmp_path / "VENUE44.xml", _DICT.encode())
    log = _write_session(tmp_path, s, "cpp_ns")
    _, events, res = _run([log], tmp_path, data_dictionary=dd)
    f = next(e for e in events if e["event_type"] == "fill")
    assert (f["qty"], f["liquidity"]) == (2, "taker") and "venue-tag" in f["raw"]
    assert "exec_type_from_dictionary=1" in _warn(res, "counters")
    assert "1 tag(s) not in the data dictionary (kept in raw): 5999x1" in _warn(res, "not in the data dictionary")
    _, events0, res0 = _run([log], tmp_path)
    assert not any(e["event_type"] == "fill" for e in events0)  # without the dictionary 150=Z is unknown
    assert any("ExecType 150=Z" in k for k in res0.skipped)
    bad = write(tmp_path / "bad.xml", b'<!DOCTYPE x [<!ENTITY a "b">]><fix/>')
    with pytest.raises(QtError, match="entity"):
        _run([log], tmp_path, data_dictionary=bad)


def test_picosecond_transacttime_and_acceptor_side_messages(tmp_path: Path) -> None:
    s = FixSession(begin="FIXT.1.1", appl_ver="9")
    s.out(
        at(0),
        "D",
        [
            ("11", "C1"),
            ("1", "ACCT1"),
            ("55", "ACME"),
            ("54", "1"),
            ("38", "1"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "1"),
        ],
    )
    er = _er(s, "new", oid="O1", cid="C1", qty="1", px="101.00", cum="0", leaves="1", tt=at(1), exec_id="X")
    s.inn(at(2), "8", [(t, fix_time(at(1), 9) + "123") if t == "60" else (t, v) for t, v in er])
    s.inn(at(3), "D", [("11", "THEIRS"), ("55", "ACME"), ("54", "1"), ("38", "1"), ("40", "2"), ("44", "1")])
    _, events, res = _run([_write_session(tmp_path, s, "cpp_ns")], tmp_path)
    ack = next(e for e in events if e["event_type"] == "ack")
    assert (ack["ts_exchange"], ack["ts_precision"]) == (at(1), "ns")
    assert "sub_ns_truncated=1" in _warn(res, "counters")
    assert "acceptor_side_messages=1" in _warn(res, "FIX session")


# ---- latency, options --------------------------------------------------------------------------------------------


def test_latency_stats_use_true_local_send_and_receive_times(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    out = tmp_path / "out"
    import_quickfix(_write_session(tmp_path, s, "cpp_ns"), out, instrument_map=instrument_map(tmp_path / "m.csv"))
    st = latency_stats(str(out))
    # acks of orders 1, 3, 4, 5: venue TransactTime - local send, local receive - venue time, local round trips
    entry = sorted(
        [
            at(1000, 400) - at(1000) - _SEND,
            at(6000, 300) - at(6000) - _SEND,
            at(7000, 300) - at(7000) - _SEND,
            at(8000, 300) - at(8000) - _SEND,
        ]
    )
    assert st["entry_ns"]["n"] == 4 and st["entry_ns"]["p50"] == pytest.approx((entry[1] + entry[2]) / 2)
    rtt = sorted(
        [
            at(1000, 650, 123) - at(1000) - _SEND,
            at(6000, 500) - at(6000) - _SEND,
            at(7000, 480) - at(7000) - _SEND,
            at(8000, 470) - at(8000) - _SEND,
        ]
    )
    assert st["ack_rtt_ns"]["n"] == 4 and st["ack_rtt_ns"]["p50"] == pytest.approx((rtt[1] + rtt[2]) / 2)
    assert st["cancel_rtt_ns"]["n"] == 1 and st["cancel_rtt_ns"]["p50"] == at(4000, 530) - at(4000) - _SEND
    assert st["amend_rtt_ns"]["n"] == 1 and st["amend_ns"]["p50"] == at(3000, 300) - at(3000) - _SEND


def test_local_ts_regex_needs_tz_for_offsetless_times(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    ms = [(r.t - at(0)) // 10**6 for r in s.records]
    lines = [
        f"2026-10-15 09:30:{q // 1000:02d}.{q % 1000:03d} INFO {r.text}" for q, r in zip(ms, s.records, strict=True)
    ]
    log = write(tmp_path / "app.log", ("\n".join(lines) + "\n").encode("latin-1"))
    rx = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) INFO "
    with pytest.raises(QtError, match="no UTC offset"):
        _run([log], tmp_path, local_ts_regex=rx)
    imap = load_instrument_map(instrument_map(tmp_path / "im.csv"))
    res = IMPORTERS["fix44"](
        [log], ImportOptions(imap=imap, extra={"fix": {"local_ts_regex": rx}, "tz": "America/New_York"})
    )
    ack = next(e for e in res.events if e["event_type"] == "ack" and e["order_key"] == K1)
    assert ack["ts_local_recv"] == trunc(at(1000, 650, 123), 3)  # 09:30 New York (EDT) = 13:30 UTC
    for bad, msg in (
        ({"delimiter": "=="}, "fix.delimiter"),
        ({"local_ts_regex": "("}, "fix.local_ts_regex"),
        ({"checksum": "maybe"}, "fix.checksum"),
        ({"bogus": 1}, "unknown option"),
        ({"instrument_tag": "x"}, "instrument_tag"),
    ):
        with pytest.raises(QtError, match=msg) as ei:
            IMPORTERS["fix44"]([log], ImportOptions(extra={"fix": bad}))
        assert ei.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError, match="tz applies only") as ei:
        import_venue({"venue": "fix44", "inputs": [{"path": log}], "out": str(tmp_path / "o"), "tz": "UTC"})
    assert ei.value.code == "QT-CONFIG-001"


def test_instrument_tag_option(tmp_path: Path) -> None:
    s = FixSession()
    s.out(
        at(0),
        "D",
        [
            ("11", "C1"),
            ("1", "ACCT1"),
            ("55", "ES"),
            ("107", "ACME"),
            ("54", "1"),
            ("38", "1"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "0"),
        ],
    )
    log = _write_session(tmp_path, s, "cpp_ns")
    orders, _, _ = _run([log], tmp_path, instrument_tag=107)
    assert (orders[0]["instrument"], orders[0]["instrument_id"]) == ("ACME", 201)


def test_event_log_and_global_log_are_summarised(tmp_path: Path) -> None:
    s = day_scenario("FIX.4.4")
    log = _write_session(tmp_path, s, "qfj_plain")
    ev = write(
        tmp_path / event_name(s, "qfj_plain"),
        render_events(
            [
                (at(0), "Created session: FIX.4.4:QTCLIENT->SYNTHVENUE"),
                (at(1), "Initiated logon request"),
                (at(2), "Received logon"),
                (at(3), "MsgSeqNum too high, expecting 5 but received 9"),
                (at(4), "Sent ResendRequest FROM: 5 TO: 8"),
                (at(5), "Invalid message: Expected CheckSum=12, Received CheckSum=0"),
                (at(6), "Disconnecting"),
            ],
            "qfj_plain",
        ),
    )
    _, _, res = _run([log, ev], tmp_path)
    w = _warn(res, "QuickFIX event log FIX.4.4-QTCLIENT-SYNTHVENUE")
    for k in (
        "lines=7",
        "logon=2",
        "seq_too_high=1",
        "resend_request=1",
        "invalid_message=1",
        "disconnect=1",
        "other=1",
    ):
        assert k in w, w
