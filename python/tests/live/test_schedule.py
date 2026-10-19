"""Schedule (SPEC 5.7.2) and latency statistics (SPEC 5.4 "Latency") on constructed canonical logs."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from queuetruth.live import (
    LiveScheduleWarning,
    build_schedule,
    latency_stats,
    to_schedule,
    write_canonical_log,
)
from queuetruth.schemas.arrow import SCHEMAS

T0 = 1_800_000_000_000_000_000


def _order(key: str, venue: str = "v", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_key": key,
        "venue": venue,
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


def _ev(key: str, event_type: str, ts: int | None = None, kind: str = "gateway_time", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_key": key,
        "event_type": event_type,
        "ts_exchange": None if ts is None else T0 + ts,
        "ts_exchange_kind": kind,
        "ts_precision": "ns",
        "source": "synthetic",
    }
    for k in ("ts_local_send", "ts_local_recv"):
        if k in kw and kw[k] is not None:
            kw[k] = T0 + kw[k]
    row.update(kw)
    return row


def _write(tmp: Path, orders: list[dict[str, Any]], events: list[dict[str, Any]]) -> str:
    write_canonical_log(tmp, orders, events)
    return str(tmp)


def _rows(t: Any) -> list[dict[str, Any]]:
    return t.cast(t.schema).to_pylist()


def _ns(ts: Any) -> int:
    return int(ts.value)


def test_keep_priority_amend_becomes_shadow_amend(tmp_path: Path) -> None:
    k = "v:a:1"
    d = _write(
        tmp_path,
        [_order(k)],
        [
            _ev(k, "submit_sent", None, "unknown", ts_local_send=0),
            _ev(k, "ack", 100, qty=10, cum_qty=0, leaves_qty=10),
            _ev(k, "fill", 200, "match_time", qty=2, cum_qty=2, leaves_qty=8, fill_id="f1"),
            _ev(k, "amend_ack", 300, qty=6, cum_qty=2, leaves_qty=4, amend_keeps_priority=True),
            _ev(k, "cancel_ack", 900, cum_qty=2, leaves_qty=0),
        ],
    )
    spec, amends = to_schedule(d, entry="exchange_ack", latency_ns=None)
    assert spec.schema.equals(SCHEMAS["qt.shadow_spec"], check_metadata=True)
    assert amends.schema.equals(SCHEMAS["qt.shadow_amend"], check_metadata=True)
    s = _rows(spec)
    assert len(s) == 1 and s[0]["shadow_id"] == k and s[0]["order_key"] == k
    assert (_ns(s[0]["ts_submit"]), s[0]["latency_ns"], _ns(s[0]["ts_cancel"]), s[0]["qty"]) == (
        T0 + 100,
        0,
        T0 + 900,
        10,
    )
    assert s[0]["side"] == ord("B") and s[0]["price"] == 100 and s[0]["source"] == "live_log"
    a = _rows(amends)
    assert [(r["shadow_id"], _ns(r["ts_send"]), r["new_qty"]) for r in a] == [(k, T0 + 300, 6)]


def test_price_change_starts_new_leg(tmp_path: Path) -> None:
    k = "v:a:1"
    d = _write(
        tmp_path,
        [_order(k)],
        [
            _ev(k, "ack", 100, qty=10, cum_qty=0, leaves_qty=10),
            _ev(k, "fill", 200, "match_time", qty=3, cum_qty=3, leaves_qty=7, fill_id="f1"),
            _ev(k, "amend_ack", 300, qty=10, px=101, cum_qty=3, leaves_qty=7, amend_keeps_priority=False),
            _ev(k, "fill", 400, "match_time", qty=7, cum_qty=10, leaves_qty=0, fill_id="f2"),
        ],
    )
    sched = build_schedule(d, entry="exchange_ack")
    s = _rows(sched.spec)
    assert [r["shadow_id"] for r in s] == [k, f"{k}.leg2"]
    assert (_ns(s[0]["ts_cancel"]), s[0]["qty"], s[0]["price"]) == (T0 + 300, 10, 100)
    # A8 (open conflict 7): the re-priced leg carries the instruction's total (10), not 10 - 3 live fills
    assert (_ns(s[1]["ts_submit"]), s[1]["qty"], s[1]["price"], s[1]["ts_cancel"]) == (T0 + 300, 10, 101, None)
    assert sched.legs[k] == [k, f"{k}.leg2"] and sched.amends.num_rows == 0


def test_a8_legs_follow_instructions_not_live_fills(tmp_path: Path) -> None:
    """Open conflict 7 (ruling A8): an order partially filled before a re-price and a later priority-keeping size
    decrease. Every leg and amend is the instruction's total as sent / acknowledged; live fills never enter."""
    k = "v:a:9"
    d = _write(
        tmp_path,
        [_order(k)],
        [
            _ev(k, "ack", 100, qty=10, cum_qty=0, leaves_qty=10),
            _ev(k, "fill", 150, "match_time", qty=4, cum_qty=4, leaves_qty=6, fill_id="f1"),
            _ev(k, "amend_ack", 300, qty=12, px=101, cum_qty=4, leaves_qty=8, amend_keeps_priority=False),
            _ev(k, "fill", 350, "match_time", qty=2, cum_qty=6, leaves_qty=6, fill_id="f2"),
            _ev(k, "amend_ack", 500, qty=9, px=101, cum_qty=6, leaves_qty=3, amend_keeps_priority=True),
        ],
    )
    sched = build_schedule(d, entry="exchange_ack")
    s = _rows(sched.spec)
    assert [(r["shadow_id"], r["qty"], r["price"]) for r in s] == [(k, 10, 100), (f"{k}.leg2", 12, 101)]
    a = _rows(sched.amends)
    assert [(r["shadow_id"], _ns(r["ts_send"]), r["new_qty"]) for r in a] == [(f"{k}.leg2", T0 + 500, 9)]
    # the same log without the fills gives the same schedule: sizes never depend on live fills
    d2 = _write(
        tmp_path / "nofills",
        [_order(k)],
        [
            _ev(k, "ack", 100, qty=10, cum_qty=0, leaves_qty=10),
            _ev(k, "amend_ack", 300, qty=12, px=101, cum_qty=0, leaves_qty=12, amend_keeps_priority=False),
            _ev(k, "amend_ack", 500, qty=9, px=101, cum_qty=0, leaves_qty=9, amend_keeps_priority=True),
        ],
    )
    s2 = build_schedule(d2, entry="exchange_ack")
    assert _rows(s2.spec) == s and _rows(s2.amends) == a


def test_qty_increase_and_null_priority(tmp_path: Path) -> None:
    k1, k2, k3 = "v:a:1", "binance:a:2", "v:a:3"
    d = _write(
        tmp_path,
        [_order(k1), _order(k2, venue="binance"), _order(k3)],
        [
            _ev(k1, "ack", 100),
            _ev(k1, "amend_ack", 200, qty=12),  # qty up -> new leg of 12
            _ev(k2, "ack", 100),
            _ev(k2, "amend_ack", 200, qty=6),  # null + documented venue rule -> amend
            _ev(k3, "ack", 100),
            _ev(k3, "amend_ack", 200, qty=6),  # null, undocumented venue -> new leg
        ],
    )
    sched = build_schedule(d, entry="exchange_ack")
    by = {r["shadow_id"]: r for r in _rows(sched.spec)}
    assert by[f"{k1}.leg2"]["qty"] == 12 and f"{k2}.leg2" not in by and by[f"{k3}.leg2"]["qty"] == 6
    assert [(r["shadow_id"], r["new_qty"]) for r in _rows(sched.amends)] == [(k2, 6)]


def test_ioc_market_excluded_and_counted(tmp_path: Path) -> None:
    d = _write(
        tmp_path,
        [
            _order("v:a:1", tif="ioc"),
            _order("v:a:2", order_type="market", limit_px=None, px_str=None),
            _order("v:a:3", tif="fok"),
            _order("v:a:4", instrument_id=None),
            _order("v:a:5", tif="post_only"),
            _order("v:a:6"),
        ],
        [_ev(f"v:a:{i}", "ack", 10) for i in range(1, 6)] + [_ev("v:a:6", "reject", 10)],
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        spec, _ = to_schedule(d, entry="exchange_ack", latency_ns=None)
    assert [r["shadow_id"] for r in _rows(spec)] == ["v:a:5"]
    msgs = [str(x.message) for x in w if issubclass(x.category, LiveScheduleWarning)]
    for reason in ("tif_ioc", "tif_fok", "order_type_market", "no_instrument_id", "rejected"):
        assert any(reason in m for m in msgs), reason
    sched = build_schedule(d, entry="exchange_ack")
    assert sched.excluded == {"tif_ioc": 1, "tif_fok": 1, "order_type_market": 1, "no_instrument_id": 1, "rejected": 1}


def test_record_time_ack_falls_back_with_warning(tmp_path: Path) -> None:
    k1, k2, k3 = "v:a:1", "v:a:2", "v:a:3"
    d = _write(
        tmp_path,
        [_order(k1), _order(k2), _order(k3)],
        [
            _ev(k1, "submit_sent", None, "unknown", ts_local_send=50),
            _ev(k1, "ack", 1000, "match_time", ts_local_recv=1100),
            _ev(k2, "submit_sent", None, "unknown", ts_local_send=2000),
            _ev(k2, "ack", 2300, "record_time"),
            _ev(k2, "cancel_sent", None, "unknown", ts_local_send=5000),
            _ev(k2, "cancel_ack", 5400, "record_time"),
            _ev(k3, "ack", 3000, "record_time"),  # record_time ack and no submit_sent: no entry time
        ],
    )
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        spec, _ = to_schedule(d, entry="exchange_ack", latency_ns=None)
    msgs = [str(x.message) for x in w]
    assert any("entry_fallback_share=1/2" in m for m in msgs)
    assert any("no_entry_time" in m for m in msgs)
    by = {r["shadow_id"]: r for r in _rows(spec)}
    assert (_ns(by[k1]["ts_submit"]), by[k1]["latency_ns"]) == (T0 + 1000, 0)
    # fallback: send time + median entry latency (only k1 has a usable sample: 1000 - 50 = 950)
    assert (_ns(by[k2]["ts_submit"]), by[k2]["latency_ns"], _ns(by[k2]["ts_cancel"])) == (T0 + 2000, 950, T0 + 5000)
    sched = build_schedule(d, entry="exchange_ack", latency_ns=123)
    assert sched.n_entry_fallback == 1 and sched.entry_fallback_share == 0.5
    assert {r["shadow_id"]: r["latency_ns"] for r in _rows(sched.spec)}[k2] == 123


def test_send_plus_latency_mode(tmp_path: Path) -> None:
    k = "v:a:1"
    d = _write(
        tmp_path,
        [_order(k)],
        [
            _ev(k, "submit_sent", None, "unknown", ts_local_send=0),
            _ev(k, "ack", 400),
            _ev(k, "amend_sent", None, "unknown", ts_local_send=600),
            _ev(k, "amend_ack", 900, qty=5, amend_keeps_priority=True),
            _ev(k, "cancel_sent", None, "unknown", ts_local_send=1500),
            _ev(k, "cancel_ack", 1800),
        ],
    )
    sched = build_schedule(d, entry="send_plus_latency", latency_ns=250)
    s = _rows(sched.spec)[0]
    assert (_ns(s["ts_submit"]), s["latency_ns"], _ns(s["ts_cancel"])) == (T0, 250, T0 + 1500)
    assert [(_ns(r["ts_send"]), r["new_qty"]) for r in _rows(sched.amends)] == [(T0 + 600, 5)]


def _quant(v: list[int]) -> dict[str, Any]:
    q = np.percentile(np.asarray(sorted(v), dtype=float), [10, 50, 90, 99])
    return {"p10": q[0], "p50": q[1], "p90": q[2], "p99": q[3], "n": len(v)}


def test_latency_quantiles_and_negative_values(tmp_path: Path) -> None:
    orders, events = [], []
    entries = [100, 250, 300, 1000, -40]
    for i, ent in enumerate(entries):
        k = f"v:a:{i}"
        orders.append(_order(k))
        events += [
            _ev(k, "submit_sent", None, "unknown", ts_local_send=10_000 * i),
            _ev(k, "ack", 10_000 * i + ent, ts_local_recv=10_000 * i + ent + 30 + i),
            _ev(k, "cancel_sent", None, "unknown", ts_local_send=10_000 * i + 5000),
            _ev(k, "cancel_ack", 10_000 * i + 5000 + 70 * (i + 1)),
        ]
    orders.append(_order("v:a:rt"))
    events += [
        _ev("v:a:rt", "submit_sent", None, "unknown", ts_local_send=900_000),
        _ev("v:a:rt", "ack", 900_100, "record_time"),
    ]
    d = _write(tmp_path, orders, events)
    st = latency_stats(d)
    assert st["entry_ns"] == pytest.approx(_quant([100, 250, 300, 1000]))
    assert st["negative_dropped"] == {"entry_ns": 1, "response_ns": 0, "cancel_ns": 0, "amend_ns": 0}
    assert st["response_ns"] == pytest.approx(_quant([30, 31, 32, 33, 34]))
    assert st["cancel_ns"] == pytest.approx(_quant([70, 140, 210, 280, 350]))
    assert st["amend_ns"] == {"p10": None, "p50": None, "p90": None, "p99": None, "n": 0}
    assert st["clock_sync_assumed"] is True and st["ack_record_time_excluded"] == 1
    from queuetruth.schemas.models import LatencyFit

    LatencyFit(**{k: st[k] for k in ("entry_ns", "response_ns", "cancel_ns", "amend_ns", "clock_sync_assumed")})
