"""Validator (SPEC 3.8): negative cases from the canonical fixtures and constructed logs; warnings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from live._helpers import FIXTURES

from queuetruth.errors import QtError
from queuetruth.live import build_log, check, synthetic_log, validate, write_canonical_log

CANON = FIXTURES / "canonical"
INDEX = json.loads((CANON / "INDEX.json").read_text())


def test_valid_multi_venue_dir() -> None:
    assert validate(str(CANON / "valid_multi_venue")) == []


@pytest.mark.parametrize("case", sorted(INDEX))
def test_o_live_1_validator_negative_cases(case: str) -> None:
    code = INDEX[case]["expect_error_code"]
    if code is None:
        warnings = validate(str(CANON / case))
        assert any("taker fill on a post_only order" in w for w in warnings)
        return
    with pytest.raises(QtError) as ei:
        validate(str(CANON / case))
    assert ei.value.code == code
    doc = check(CANON / case)
    assert doc.errors and doc.n_orders > 0


def _order(key: str = "v:a:1", **kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "order_key": key,
        "venue": "v",
        "account": "a",
        "instrument": "X",
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


def _ev(event_type: str, ts: int, key: str = "v:a:1", **kw: object) -> dict[str, object]:
    row: dict[str, object] = {
        "order_key": key,
        "event_type": event_type,
        "ts_exchange": ts,
        "ts_exchange_kind": "match_time",
        "ts_precision": "ns",
        "source": "synthetic",
    }
    row.update(kw)
    return row


def test_no_warning_after_legit_amend_and_warning_on_mismatch(tmp_path: Path) -> None:
    evs = [
        _ev("ack", 1, qty=10, leaves_qty=10, cum_qty=0),
        _ev("fill", 2, qty=4, cum_qty=4, leaves_qty=6, fill_id="f1"),
        _ev("amend_ack", 3, qty=8, cum_qty=4, leaves_qty=4),
        _ev("fill", 4, qty=4, cum_qty=8, leaves_qty=0, fill_id="f2"),
    ]
    write_canonical_log(tmp_path / "ok", [_order()], evs)
    assert validate(str(tmp_path / "ok")) == []
    evs[3]["leaves_qty"] = 1
    write_canonical_log(tmp_path / "bad", [_order()], evs)
    assert any("cum_qty + leaves_qty" in w for w in validate(str(tmp_path / "bad")))


def test_bust_and_correct_running_sum(tmp_path: Path) -> None:
    evs = [
        _ev("fill", 1, qty=4, cum_qty=4, fill_id="f1"),
        _ev("fill", 2, qty=6, cum_qty=10, fill_id="f2"),
        _ev("fill_bust", 3, ref_fill_id="f2", cum_qty=4),
        _ev("fill_correct", 4, ref_fill_id="f1", qty=3, cum_qty=3),
    ]
    write_canonical_log(tmp_path / "x", [_order()], evs)
    assert validate(str(tmp_path / "x")) == []
    evs[3]["cum_qty"] = 4
    with pytest.raises(QtError) as ei:
        write_canonical_log(tmp_path / "y", [_order()], evs)
    assert ei.value.code == "QT-INPUT-002"


def test_duplicate_fill_is_per_instrument() -> None:
    orders = [_order("v:a:1"), _order("v:a:2", instrument="Y")]
    evs = [_ev("fill", 1, "v:a:1", qty=1, fill_id="t1"), _ev("fill", 2, "v:a:2", qty=1, fill_id="t1")]
    from queuetruth.live import check_tables

    assert check_tables(*build_log(orders, evs))[0] == []
    orders[1]["instrument"] = "X"
    errors, _, dup = check_tables(*build_log(orders, evs))
    assert dup and any("duplicate fill_id" in e for e in errors)


def test_synthetic_log_validates(tmp_path: Path) -> None:
    o, e = synthetic_log(11, 300)
    write_canonical_log(tmp_path, o, e)
    assert validate(str(tmp_path)) == []
    o2, e2 = synthetic_log(11, 300)
    assert o.equals(o2) and e.equals(e2)
