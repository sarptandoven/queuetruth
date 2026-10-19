"""Schema 1.1 in the live package (SPEC 3.11 #13-#17): canonical layouts in both directions, manifest / summary
`coverage` and `entry_fallback_share`, `replaces_order_key` validation, `tif = gtd` scheduling; plus the trader UX
review items (loud unmapped-instrument imports, per-instrument scale metadata)."""

from __future__ import annotations

import csv
import json
import warnings
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from live._helpers import FIXTURES

from queuetruth.errors import QtError
from queuetruth.live import (
    LiveImportWarning,
    build_log,
    build_schedule,
    check_tables,
    import_venue,
    instrument_scales,
    manifest,
    read_log,
    table_rows,
    validate,
    write_canonical_log,
)
from queuetruth.schemas.arrow import FULL_SCHEMAS, SCHEMAS
from queuetruth.schemas.io import write_table

T0 = 1_800_000_000_000_000_000


def _order(key: str, **kw: Any) -> dict[str, Any]:
    row = {
        "order_key": key,
        "venue": "v",
        "account": "a",
        "instrument": "X",
        "instrument_id": 7,
        "side": "buy",
        "order_type": "limit",
        "tif": "gtc",
        "orig_qty": 10,
        "qty_str": "10",
        "limit_px": 100,
        "px_str": "100",
        "price_exp": 0,
        "size_exp": 0,
    }
    row.update(kw)
    return row


def _events(key: str) -> list[dict[str, Any]]:
    base = {"order_key": key, "ts_precision": "ns", "source": "synthetic", "derived": False}
    return [
        {**base, "event_type": "submit_sent", "ts_local_send": T0 - 5, "ts_exchange_kind": "unknown", "qty": 10},
        {
            **base,
            "event_type": "ack",
            "ts_exchange": T0,
            "ts_exchange_kind": "match_time",
            "qty": 10,
            "cum_qty": 0,
            "leaves_qty": 10,
        },
        {
            **base,
            "event_type": "cancel_ack",
            "ts_exchange": T0 + 9,
            "ts_exchange_kind": "match_time",
            "cum_qty": 0,
            "leaves_qty": 0,
        },
    ]


def test_writer_produces_the_full_1_1_layout_and_manifest_fields(tmp_path: Path) -> None:
    write_canonical_log(
        tmp_path, [_order("v:a:1"), _order("v:a:2", replaces_order_key="v:a:1")], _events("v:a:1") + _events("v:a:2")
    )
    sch = pq.read_schema(tmp_path / "orders.parquet")
    assert sch.names == FULL_SCHEMAS["qt.live_orders"].names and sch.names[-1] == "replaces_order_key"
    assert sch.metadata[b"qt.schema_version"] == b"1.1"
    o, _ = read_log(tmp_path)
    assert table_rows(o)[1]["replaces_order_key"] == "v:a:1"
    m = manifest(tmp_path)
    assert m["coverage"] == {"fills_only": False, "n_fills_only_orders": 0} and m["entry_fallback_share"] == 0.0


def test_reader_accepts_v0_1_parquet_and_csv_logs(tmp_path: Path) -> None:
    o, e = build_log([_order("v:a:1")], _events("v:a:1"))
    v01 = o.select(SCHEMAS["qt.live_orders"].names)
    write_table(v01, tmp_path / "orders.parquet", SCHEMAS["qt.live_orders"])
    write_table(e, tmp_path / "events.parquet", SCHEMAS["qt.live_events"])
    orders, _ = read_log(tmp_path)
    assert orders.schema.names == FULL_SCHEMAS["qt.live_orders"].names
    assert table_rows(orders)[0]["replaces_order_key"] is None
    assert validate(tmp_path) == []
    d = tmp_path / "csv"
    d.mkdir()
    rows = table_rows(v01)
    with (d / "orders.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows({k: (json.dumps(v) if isinstance(v, list) else v) for k, v in r.items()} for r in rows)
    erows = table_rows(e)
    with (d / "events.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(erows[0]))
        w.writeheader()
        w.writerows(erows)
    assert table_rows(read_log(d)[0])[0]["replaces_order_key"] is None
    assert instrument_scales(d) == [
        {
            "venue": "v",
            "instrument": "X",
            "instrument_id": 7,
            "price_exp": 0,
            "size_exp": 0,
            "scale_source": "instrument_map",
            "n_orders": 1,
        }
    ]


def test_replaces_order_key_must_name_another_order(tmp_path: Path) -> None:
    o, e = build_log([_order("v:a:1", replaces_order_key="v:a:9")], _events("v:a:1"))
    errors, _, _ = check_tables(o, e)
    assert errors == ["v:a:1: replaces_order_key 'v:a:9' names no other order of the log"]
    with pytest.raises(QtError):
        write_canonical_log(tmp_path / "x", [_order("v:a:1", replaces_order_key="v:a:1")], _events("v:a:1"))


def test_gtd_orders_are_scheduled(tmp_path: Path) -> None:
    write_canonical_log(tmp_path, [_order("v:a:1", tif="gtd")], _events("v:a:1"))
    s = build_schedule(tmp_path)
    assert (s.n_scheduled, s.excluded) == (1, {})


def test_import_without_instrument_map_is_loud_and_names_the_instruments(tmp_path: Path) -> None:
    inp = FIXTURES / "binance" / "01_ws_execution_report_lifecycle" / "user_data_stream.jsonl"
    spec = {"venue": "binance", "inputs": [{"path": str(inp)}], "out": str(tmp_path / "a")}
    with pytest.warns(LiveImportWarning, match="unmapped_instruments: 2 instrument") as rec:
        s = import_venue(spec)
    assert "binance/BTCUSDT (4 orders), binance/ETHUSDT (1 orders)" in str(rec[0].message)
    assert s.warnings[0].startswith("unmapped_instruments: 2 instrument(s) have no instrument-map entry: binance/BTC")
    assert "price_exp -9, size_exp -8" in s.warnings[0]
    assert s.coverage is not None and s.coverage.n_fills_only_orders == 0
    assert instrument_scales(tmp_path / "a")[0]["scale_source"] == "default"
    (tmp_path / "im.csv").write_text(
        "venue,instrument,instrument_id,price_exp,size_exp,tick_size,lot_size\nbinance,BTCUSDT,1,-2,-5,0.01,0.00001\n"
        "binance,ETHUSDT,2,-2,-5,0.01,0.00001\n"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", LiveImportWarning)
        s2 = import_venue({**spec, "out": str(tmp_path / "b"), "instrument_map": {"path": str(tmp_path / "im.csv")}})
    assert not any(w.startswith("unmapped_instruments") for w in s2.warnings)
    meta = json.loads(pq.read_schema(tmp_path / "b" / "orders.parquet").metadata[b"qt.live.instrument_scales"])
    assert meta == instrument_scales(tmp_path / "b")
    assert [(m["instrument_id"], m["price_exp"], m["size_exp"], m["scale_source"]) for m in meta] == [
        (1, -2, -5, "instrument_map"),
        (2, -2, -5, "instrument_map"),
    ]


_DD = """<fix type="FIX" major="4" minor="4" servicepack="0"><header/><trailer/><messages/><fields>
 <field number="150" name="ExecType" type="CHAR"><value enum="0" description="NEW"/>
  <value enum="Z" description="TRADE"/></field>
 <field number="9730" name="LiquidityFlag" type="CHAR"><value enum="A" description="ADDED"/>
  <value enum="R" description="REMOVED"/></field>
</fields></fix>
"""


def _fix_session_files(tmp: Path) -> tuple[str, str, str]:
    """A QuickFIX C++ log that needs every 1.1 FixOptions field: instrument in SecurityDesc(107), a custom ExecType
    and liquidity tag (data dictionary), an ack with a wrong CheckSum (kept only with checksum=keep), and a
    counterparty mapped by session_venues. Returns (log, dictionary, instrument map)."""
    from live._fixgen import FixSession, _er, at, log_name, render_log, write

    s = FixSession(remote="CMEGW")
    s.out(
        at(0),
        "D",
        [
            ("11", "C1"),
            ("1", "ACCT1"),
            ("55", "ES"),
            ("107", "ACME"),
            ("54", "1"),
            ("38", "2"),
            ("40", "2"),
            ("44", "101.00"),
            ("59", "1"),
        ],
    )
    ack = s.inn(
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
        extra=[("9730", "R")],
    )
    s.inn(at(2), "8", [(t, "Z") if t == "150" else (t, v) for t, v in fill])
    good = int(ack.text[-4:-1])
    ack.text = ack.text[:-4] + f"{(good + 1) % 256:03d}\x01"
    log = write(tmp / "logs" / log_name(s, "cpp_ns"), render_log(s.records, "cpp_ns"))
    dd = write(tmp / "VENUE44.xml", _DD.encode())
    imap = write(
        tmp / "im.csv",
        b"venue,instrument,instrument_id,price_exp,size_exp,tick_size,lot_size\ncme,ACME,201,-4,0,0.01,1\n",
    )
    return log, dd, imap


def test_fix_options_from_the_spec_match_import_quickfix(tmp_path: Path) -> None:
    from queuetruth.live import import_quickfix

    log, dd, imap = _fix_session_files(tmp_path)
    fix = {"checksum": "keep", "data_dictionary": dd, "session_venues": {"CMEGW": "cme"}, "instrument_tag": 107}
    s1 = import_venue(
        {
            "venue": "fix44",
            "inputs": [{"path": str(Path(log).parent)}],
            "out": str(tmp_path / "a"),
            "instrument_map": {"path": imap},
            "fix": fix,
        }
    )
    s2 = import_quickfix([log], tmp_path / "b", fix=fix, instrument_map=imap)
    s3 = import_quickfix(
        [log],
        tmp_path / "c",
        instrument_map=imap,
        checksum="keep",
        data_dictionary=[dd],
        session_venues={"CMEGW": "cme"},
        instrument_tag=107,
    )
    rows = [table_rows(read_log(tmp_path / d)[1]) for d in ("a", "b", "c")]
    assert rows[0] == rows[1] == rows[2]
    assert [(e["order_key"], e["event_type"], e["liquidity"]) for e in rows[0]] == [
        ("cme:ACCT1:O1", "submit_sent", None),
        ("cme:ACCT1:O1", "ack", None),
        ("cme:ACCT1:O1", "fill", "taker"),
    ]
    assert s1.venue == s2.venue == s3.venue == "cme"
    assert s1.unmapped_instruments == [] and s1.instrument_scales is not None
    assert [x.model_dump() for x in s1.instrument_scales] == instrument_scales(tmp_path / "a")
    # the spec defaults (checksum exclude, no dictionary, Symbol(55)) give a different log
    with pytest.warns(LiveImportWarning):
        s4 = import_venue({"venue": "fix44", "inputs": [{"path": log}], "out": str(tmp_path / "d")})
    ev4 = table_rows(read_log(tmp_path / "d")[1])
    assert [e["event_type"] for e in ev4] == ["submit_sent"] and s4.venue == "fix44"
    assert [u.model_dump() for u in s4.unmapped_instruments] == [{"venue": "fix44", "instrument": "ES", "n_orders": 1}]
    assert s4.warnings[0].startswith("unmapped_instruments: 1 instrument(s)")
    assert [(x.instrument, x.scale_source) for x in s4.instrument_scales or []] == [("ES", "default")]


def test_import_quickfix_keywords_override_the_fix_spec(tmp_path: Path) -> None:
    from queuetruth.live import import_quickfix
    from queuetruth.schemas.specs import FixOptions

    log, dd, imap = _fix_session_files(tmp_path)
    fix = FixOptions(venue_name="spec_venue", checksum="keep", data_dictionary=dd, instrument_tag=107)
    s = import_quickfix([log], tmp_path / "o", fix=fix, venue_name="cme", checksum="exclude", instrument_map=imap)
    assert s.venue == "cme"
    ev = table_rows(read_log(tmp_path / "o")[1])
    assert "ack" not in [e["event_type"] for e in ev]  # keyword checksum=exclude won over the spec's keep
    assert any(e["event_type"] == "fill" and e["liquidity"] == "taker" for e in ev)  # the spec's dictionary kept
    for bad in ({"checksum": "maybe"}, {"instrument_tag": 0}, {"delimiter": "=="}):
        with pytest.raises(QtError) as ei:
            import_quickfix([log], tmp_path / "x", **bad)  # type: ignore[arg-type]
        assert ei.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as ei:
        import_venue(
            {"venue": "binance", "inputs": [{"path": log}], "out": str(tmp_path / "y"), "fix": {"checksum": "keep"}}
        )
    assert ei.value.code == "QT-CONFIG-001"
