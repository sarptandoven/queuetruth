"""Venue names in the schedule rules compare case-insensitively: a log whose venue is `CME` (e.g. a FIX drop copy
imported with `fix.venue_name = CME`) gets the CME amend rules (A6, E27), and `keep_priority_venues` may be given in
any case."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from queuetruth.live import build_schedule, write_canonical_log

T0 = 1_800_000_000_000_000_000


def _order(key: str, venue: str) -> dict[str, Any]:
    return {
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


def _ev(key: str, event_type: str, ts: int, **kw: Any) -> dict[str, Any]:
    return {
        "order_key": key,
        "event_type": event_type,
        "ts_exchange": T0 + ts,
        "ts_exchange_kind": "gateway_time",
        "ts_precision": "ns",
        "source": "synthetic",
        **kw,
    }


def _log(tmp: Path, venue: str) -> str:
    dec, same = f"{venue}:a:1", f"{venue}:a:2"
    write_canonical_log(
        tmp,
        [_order(dec, venue), _order(same, venue)],
        [
            _ev(dec, "ack", 100, qty=10, cum_qty=0, leaves_qty=10),
            _ev(dec, "amend_ack", 200, qty=6, cum_qty=0, leaves_qty=6),  # null keeps-priority, size decrease
            _ev(same, "ack", 100, qty=10, cum_qty=0, leaves_qty=10),
            _ev(same, "amend_ack", 200, qty=10, cum_qty=0, leaves_qty=10),  # same price and size
        ],
    )
    return str(tmp)


def _shape(d: str, **kw: Any) -> tuple[list[str], list[tuple[str, int]]]:
    s = build_schedule(d, entry="exchange_ack", **kw)
    ids = sorted(r["shadow_id"] for r in s.spec.to_pylist())
    amends = [(r["shadow_id"], r["new_qty"]) for r in s.amends.to_pylist()]
    return ids, amends


def test_upper_case_cme_gets_the_cme_rules(tmp_path: Path) -> None:
    lower = _shape(_log(tmp_path / "lower", "cme"))
    upper = _shape(_log(tmp_path / "upper", "CME"))
    # CME: a pure size decrease keeps priority (an amend); a same-size modify loses it (a new leg).
    assert lower == (["cme:a:1", "cme:a:2", "cme:a:2.leg2"], [("cme:a:1", 6)])
    assert upper == (["CME:a:1", "CME:a:2", "CME:a:2.leg2"], [("CME:a:1", 6)])


def test_keep_priority_venues_given_in_any_case(tmp_path: Path) -> None:
    d = _log(tmp_path / "x", "Kraken")
    ids, amends = _shape(d, keep_priority_venues={"KRAKEN"})
    assert amends == [("Kraken:a:1", 6)]
    # Not a CME rule set: the unchanged modify changes nothing.
    assert ids == ["Kraken:a:1", "Kraken:a:2"]
    ids, amends = _shape(d, keep_priority_venues=set())
    assert amends == [] and "Kraken:a:1.leg2" in ids
