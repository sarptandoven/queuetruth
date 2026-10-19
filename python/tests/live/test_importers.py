"""O-LIVE-1: every importer on format-exact synthetic fixtures (fake accounts) -> golden canonical rows.

Divergences from the fixture goldens are listed in `KNOWN` with the reason; everything else must match
exactly (all columns except `raw`)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from live._helpers import FIXTURES, diff_rows, run_scenario, scenarios

from queuetruth.live import build_log, check_tables

# (scenario, item) -> reason. Items: "summary.<field>", "reason@<order_key>#<seq>", "qty_str@<order_key|*>",
# "tif_gtd@<order_key>" (golden `other` -> 1.1 `gtd`), "modify_one_order" (expected rows transformed below).
KNOWN: dict[str, dict[str, str]] = {
    "okx/02_rest_fills_ts_vs_filltime": {
        "summary.unmatched_fills": "counts fills without an order record (3 fills); the golden counted orders only",
    },
    "ibkr_flex/01_flex_xml_executions": {"summary.unmatched_fills": "Flex rows are fills without order records"},
    "ibkr_flex/02_flex_csv_with_order_rows": {"summary.unmatched_fills": "as above"},
    "ibkr_flex/03_flex_xml_trade_cancel_UNVERIFIED": {"summary.unmatched_fills": "as above"},
    "coinbase/01_ws_user_channel_derived_fills": {
        "reason@coinbase:acct1:11111111-aaaa-4000-8000-000000000001#3": "free-text reason wording",
    },
    "coinbase/02_rest_list_fills": {"qty_str@coinbase:acct1:11111111-aaaa-4000-8000-000000000011": "derived text"},
    "hyperliquid/02_info_userfills_2000_cap": {"qty_str@*": "summed text keeps the exponent (0.0010)"},
    "hyperliquid/01_ws_userfills_orderupdates": {
        "modify_one_order": "a modify with a new oid stays one order per client-order chain",
    },
    "kraken/01_ws_executions": {
        "tif_gtd@kraken:acct1:OQTFFF-FFFFF-FFFFF6": "schema 1.1 (SPEC 3.11 #17) adds tif gtd; the golden predates it",
    },
}


def _ids() -> list[str]:
    return [f"{p.parent.name}/{p.name}" for p in scenarios()]


def _apply_modify_one_order(exp: dict[str, Any]) -> None:
    """HL modify (oid 91000000003 -> 91000000013, same cloid): one order with an amend_ack instead of a cancel_ack
    plus a second order."""
    old, new = "hyperliquid:acct1:91000000003", "hyperliquid:acct1:91000000013"
    exp["orders"] = [o for o in exp["orders"] if o["order_key"] != new]
    evs = []
    seq = 0
    for e in exp["events"]:
        if e["order_key"] == old and e["event_type"] == "cancel_ack":
            seq = e["event_seq"]
            e = dict(
                e,
                event_type="amend_ack",
                qty=100000000,
                px=2501500000000,
                leaves_qty=100000000,
                amend_keeps_priority=False,
                reason="modify: venue cancelled oid 91000000003 and placed oid 91000000013 (same cloid)",
            )
        elif e["order_key"] == new:
            if e["event_type"] == "ack":
                continue
            seq += 1
            e = dict(e, order_key=old, event_seq=seq)
        evs.append(e)
    exp["events"] = sorted(evs, key=lambda r: (r["order_key"], r["event_seq"]))
    exp["summary"] = dict(
        exp["summary"], n_orders=exp["summary"]["n_orders"] - 1, n_events=exp["summary"]["n_events"] - 1
    )


@pytest.mark.parametrize("scen", scenarios(), ids=_ids())
def test_o_live_1_importer_golden_rows(scen: Path) -> None:
    name = f"{scen.parent.name}/{scen.name}"
    known = KNOWN.get(name, {})
    exp, orders, events, res = run_scenario(scen)
    if "modify_one_order" in known:
        _apply_modify_one_order(exp)
    ignore = set(exp["compare_ignore_columns"])
    want_o, want_e = exp["orders"], exp["events"]
    if "qty_str@*" in known:
        ignore.add("qty_str")
    for item in known:
        if item.startswith("qty_str@") and item != "qty_str@*":
            key = item.split("@", 1)[1]
            want_o = [dict(o, qty_str=None) if o["order_key"] == key else o for o in want_o]
            orders = [dict(o, qty_str=None) if o["order_key"] == key else o for o in orders]
        if item.startswith("tif_gtd@"):
            key = item.split("@", 1)[1]
            assert any(o["order_key"] == key and o["tif"] == "other" for o in want_o), item
            want_o = [dict(o, tif="gtd") if o["order_key"] == key else o for o in want_o]
        if item.startswith("reason@"):
            key, seq = item.split("@", 1)[1].rsplit("#", 1)
            match = (key, int(seq))
            want_e = [dict(e, reason=None) if (e["order_key"], e["event_seq"]) == match else e for e in want_e]
            events = [dict(e, reason=None) if (e["order_key"], e["event_seq"]) == match else e for e in events]
    problems = diff_rows("order", orders, want_o, ignore) + diff_rows("event", events, want_e, ignore)
    got_summary = {
        "n_orders": len(orders),
        "n_events": len(events),
        "n_fills": res.n_fills,
        "dedupe_dropped": res.dedupe_dropped,
        "unmatched_fills": res.unmatched_fills,
    }
    for k, v in got_summary.items():
        if v != exp["summary"][k] and f"summary.{k}" not in known:
            problems.append(f"summary {k}: got {v}, expected {exp['summary'][k]}")
    for needle in exp["summary"]["warnings_contains"]:
        if not any(needle in w for w in res.warnings):
            problems.append(f"no warning contains {needle!r}: {res.warnings}")
    assert not problems, "\n".join(problems)
    o_t, e_t = build_log(res.orders, res.events)
    errors, _, _ = check_tables(o_t, e_t)
    assert errors == []


def test_o_live_1_fixture_manifest() -> None:
    lines = (FIXTURES / "MANIFEST.sha256").read_text().splitlines()
    listed = set()
    for line in lines:
        digest, rel = line.split(maxsplit=1)
        p = FIXTURES / rel
        listed.add(p.resolve())
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest, rel
    present = {p.resolve() for p in FIXTURES.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256"}
    assert present == listed


def _scenario(name: str) -> Path:
    return FIXTURES / name


def test_o_live_1_bybit_double_filled_and_exec_dedupe() -> None:
    _, _, events, res = run_scenario(_scenario("bybit/01_ws_order_execution_lifecycle"))
    a = [e for e in events if e["order_key"].endswith("000000000001")]
    assert [e["event_type"] for e in a] == ["ack", "fill", "amend_ack", "fill", "cancel_reject"]
    assert res.dedupe_dropped == 1  # the second delivery of execId ...0002
    assert len({e["fill_id"] for e in events if e["event_type"] == "fill"}) == res.n_fills == 4


def test_o_live_1_fix_bust_and_correct() -> None:
    _, _, events, _ = run_scenario(_scenario("fix44/01_soh_order_entry_log"))
    o1 = [e for e in events if e["order_key"] == "fix44:QTACCT01:OID-1"]
    bust = next(e for e in o1 if e["event_type"] == "fill_bust")
    corr = next(e for e in o1 if e["event_type"] == "fill_correct")
    assert (bust["ref_fill_id"], bust["cum_qty"]) == ("F-1-2", 40)
    assert (corr["ref_fill_id"], corr["px"], corr["cum_qty"]) == ("F-1-1", 1012000, 40)


def test_o_live_1_coinbase_cumulative_differencing() -> None:
    _, _, events, res = run_scenario(_scenario("coinbase/01_ws_user_channel_derived_fills"))
    fills = [e for e in events if e["event_type"] == "fill"]
    assert all(e["derived"] and e["fill_id"] is None and e["liquidity"] == "unknown" for e in fills)
    assert [e["qty"] for e in fills] == [400000, 600000]
    assert fills[0]["px"] == 60000000000000 and fills[1]["px"] is None  # merged executions: inexact VWAP
    assert any("derived" in w for w in res.warnings)


def test_o_live_1_coinbase_rest_replaces_derived() -> None:
    _, _, events, res = run_scenario(_scenario("coinbase/03_ws_plus_rest_fills_merge"))
    fills = [e for e in events if e["event_type"] == "fill"]
    assert not any(e["derived"] for e in fills) and res.dedupe_dropped == 2


def test_o_live_1_okx_fee_sign() -> None:
    _, _, events, _ = run_scenario(_scenario("okx/01_ws_orders_channel"))
    fees = [e["fee"] for e in events if e["event_type"] == "fill"]
    assert fees[0] == pytest.approx(8e-07) and fees[1] == pytest.approx(-4e-07)  # charged -> +, rebate -> -
