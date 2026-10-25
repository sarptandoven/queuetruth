"""Own-order matching (ids, signatures, failure cases), exact queue positions, and the canonical live log."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from livefill import canonical, matcher, synth
from livefill.stream import OrderEvent, SegmentStream, Snapshot
from livefill.units import PRICE_EXP, SIZE_EXP, to_fixed

from queuetruth import live
from queuetruth.errors import QtError


def test_synthetic_own_orders_match_the_engine(synth_default, tmp_path: Path) -> None:
    doc = matcher.match(synth_default.capture, [synth_default.log], out=tmp_path / "m")
    truth = {o["order_id"]: o for o in synth_default.own}
    assert doc["matched_by"] == {"id": len(truth)} and doc["n_in_feed"] == len(truth)
    assert doc["fills_inconsistent"] == 0 and doc["fifo_violations"] == 0
    for r in doc["orders"]:
        t = truth[r["order_id"]]
        assert r["filled_qty"] == t["filled_qty"], r["client_order_id"]
        assert r["end_state"] == t["end_state"], r["client_order_id"]
        assert (r["entry_ahead_orders"], r["entry_ahead_qty"]) == (t["entry_ahead_orders"], t["entry_ahead_qty"])
        assert r["entry_exact"] is True and r["fills_consistent"] is True and r["entry_level_rank"] >= 0
        assert r["amount_traded_consistent"] is True
    states = {r["end_state"] for r in doc["orders"]}
    assert {"filled", "cancelled"} <= states
    assert any(0 < o["filled_qty"] < o["orig"] for o in synth_default.own)  # a partial fill, then a cancel
    # FIFO: the quantity ahead of a resting order never grows
    q = pq.read_table(tmp_path / "m" / "own_queue.parquet").to_pylist()
    by: dict[int, list[int]] = {}
    for row in q:
        by.setdefault(row["order_id"], []).append(row["ahead_qty"])
    assert by and all(x == sorted(x, reverse=True) for x in by.values())
    fills = pq.read_table(tmp_path / "m" / "own_fills.parquet").to_pylist()
    assert fills and all(f["liquidity"] == "maker" for f in fills)
    assert all(f["ahead_qty_before"] == 0 for f in fills)
    summary = json.loads((tmp_path / "m" / "match.json").read_text())
    assert summary["schema"] == "livefill.match" and "orders" not in summary


def _rewrite_log(src: Path, dst: Path, edit) -> None:
    lines = [json.loads(x) for x in src.read_text().splitlines()]
    out = [edit(r) for r in lines]
    dst.write_text("".join(json.dumps(r) + "\n" for r in out if r is not None))


def test_signature_match_when_the_id_was_lost(synth_default, tmp_path: Path) -> None:
    def lose_first(r: dict) -> dict:
        if r.get("type") == "submit" and r["i"] == 0:
            r = {**r, "outcome": "unknown", "body": None, "recv_ns": None, "http_status": None}
        return r

    log = tmp_path / "log.jsonl"
    _rewrite_log(synth_default.log, log, lose_first)
    doc = matcher.match(synth_default.capture, [log])
    first = next(r for r in doc["orders"] if r["index"] == 0)
    assert first["matched_by"] == "signature" and first["order_id"] == synth_default.own[0]["order_id"]

    # a submit whose price never appeared cannot be matched
    def wrong_price(r: dict) -> dict:
        r = lose_first(r)
        if r.get("type") == "submit" and r["i"] == 0:
            r["params"] = {**r["params"], "price": "1.23"}
        return r

    _rewrite_log(synth_default.log, tmp_path / "w.jsonl", wrong_price)
    doc2 = matcher.match(synth_default.capture, [tmp_path / "w.jsonl"])
    assert next(r for r in doc2["orders"] if r["index"] == 0)["matched_by"] == "not_in_feed"


def test_rejected_dry_and_duplicate_ids(synth_default, tmp_path: Path) -> None:
    def edit(r: dict) -> dict:
        if r.get("type") == "submit" and r["i"] == 1:
            return {**r, "outcome": "error", "body": {"status": "error", "reason": "Insufficient funds"}}
        if r.get("type") == "submit" and r["i"] == 2:
            return {**r, "body": {**r["body"], "id": "dry-000003"}}
        return r

    _rewrite_log(synth_default.log, tmp_path / "e.jsonl", edit)
    doc = matcher.match(synth_default.capture, [tmp_path / "e.jsonl"])
    by = {r["index"]: r for r in doc["orders"]}
    assert by[1]["matched_by"] == "rejected" and by[1]["reject_reason"] == "Insufficient funds"
    assert by[2]["matched_by"] == "not_in_feed" and by[2]["end_state"] == "not_in_feed"
    with pytest.raises(QtError) as e:
        matcher.match(synth_default.capture, [synth_default.log, synth_default.log])
    assert e.value.code == "QT-INPUT-003"
    (tmp_path / "bad.jsonl").write_text('{"type":"submit"}\n')
    with pytest.raises(QtError):
        matcher.match(synth_default.capture, [tmp_path / "bad.jsonl"])


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: {**r, "params": {k: v for k, v in r["params"].items() if k != "price"}},
        lambda r: {**r, "i": "zero"},
        lambda r: {**r, "sent_ns": None},
        lambda r: {**r, "params": None},
    ],
)
def test_malformed_submit_records_are_input_errors(synth_default, tmp_path: Path, edit) -> None:
    _rewrite_log(synth_default.log, tmp_path / "m.jsonl", lambda r: edit(r) if r.get("type") == "submit" else r)
    with pytest.raises(QtError) as e:
        matcher.harness_orders([tmp_path / "m.jsonl"])
    assert e.value.code == "QT-INPUT-002"


def test_two_sessions_are_kept_apart(synth_default, tmp_path: Path) -> None:
    """A second run of the same plan (another nonce, so other client order ids) whose orders were all rejected: each
    order keeps its own outcome. A rerun that reused the client order ids is refused."""

    def rerun_rejected(r: dict) -> dict:
        if r.get("type") == "submit":
            coid = r["client_order_id"].replace("qtlf-", "qtlf-rerun-")
            return {
                **r,
                "client_order_id": coid,
                "params": {**r["params"], "client_order_id": coid},
                "outcome": "error",
                "body": {"status": "error", "reason": "Insufficient funds"},
            }
        return r if r.get("type") in ("run_start", "run_end") else None

    _rewrite_log(synth_default.log, tmp_path / "b.jsonl", rerun_rejected)
    doc = matcher.match(synth_default.capture, [synth_default.log, tmp_path / "b.jsonl"])
    n = len(synth_default.own)
    assert doc["matched_by"] == {"id": n, "rejected": n}
    first = [r for r in doc["orders"] if r["log_index"] == 0]
    truth = {o["order_id"]: o for o in synth_default.own}
    assert all(r["end_state"] == truth[r["order_id"]]["end_state"] for r in first)
    rows = canonical.build_rows(doc, "a")[0][0]
    assert sum(1 for e in rows[1] if e["event_type"] == "reject") == n

    def rerun_same_ids(r: dict) -> dict:
        return rerun_rejected(r) if r.get("type") != "submit" else {**r, "outcome": "error", "body": {}}

    _rewrite_log(synth_default.log, tmp_path / "c.jsonl", rerun_same_ids)
    with pytest.raises(QtError) as e:
        matcher.match(synth_default.capture, [synth_default.log, tmp_path / "c.jsonl"])
    assert e.value.code == "QT-INPUT-003" and "submitted twice" in e.value.message


def test_an_id_missing_from_the_feed_falls_back_to_the_signature(synth_default, tmp_path: Path) -> None:
    def wrong_id(r: dict) -> dict:
        if r.get("type") == "submit" and r["i"] == 0:
            return {**r, "body": {**r["body"], "id": "123456789"}}
        return r

    _rewrite_log(synth_default.log, tmp_path / "w.jsonl", wrong_id)
    doc = matcher.match(synth_default.capture, [tmp_path / "w.jsonl"])
    first = next(r for r in doc["orders"] if r["index"] == 0)
    assert first["matched_by"] == "signature_after_id_miss" and first["order_id"] == synth_default.own[0]["order_id"]
    assert doc["id_misses"] == 1 and doc["warnings"][0].startswith("id_misses: 1")


def test_fees_come_from_the_order_status_transactions(synth_default, tmp_path: Path) -> None:
    base = matcher.match(synth_default.capture, [synth_default.log])
    filled = next(r for r in base["orders"] if r["n_fills"])
    tids = [f["trade_id"] for f in base["fills"] if f["order_id"] == filled["order_id"]]
    lines = [json.loads(x) for x in synth_default.log.read_text().splitlines()]
    lines[0] = {**lines[0], "mode": "live"}
    status = {
        "type": "status",
        "i": filled["index"],
        "outcome": "ok",
        "endpoint": "order_status",
        "params": {"id": str(filled["order_id"])},
        "sent_ns": 1,
        "recv_ns": 2,
        "body": {
            "id": str(filled["order_id"]),
            "status": "Finished",
            "transactions": [{"tid": t, "fee": "0.05", "price": "1"} for t in tids],
        },
    }
    (tmp_path / "f.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [*lines[:-1], status, lines[-1]]))
    doc = matcher.match(synth_default.capture, [tmp_path / "f.jsonl"])
    own = [f for f in doc["fills"] if f["order_id"] == filled["order_id"]]
    assert own and all(f["fee"] == 0.05 and f["fee_ccy"] == "usd" for f in own)
    assert doc["fills_with_fee"] == len(own) and doc["live_fills_without_fee"] == len(doc["fills"]) - len(own)
    assert any(w.startswith("live_fills_without_fee") for w in doc["warnings"])
    res = canonical.write_logs(doc, tmp_path / "live", "acct1")
    ev = pq.read_table(Path(res["segments"]["0"]["path"]) / "events.parquet").to_pylist()
    fees = [e for e in ev if e["event_type"] == "fill" and e["fee"] is not None]
    assert len(fees) == len(own) and {e["fee_ccy"] for e in fees} == {"usd"}
    assert {
        o["validation_status"] for o in pq.read_table(Path(res["segments"]["0"]["path"]) / "orders.parquet").to_pylist()
    } == {"real"}


def test_queue_trace_sees_an_order_ahead_leaving_the_level() -> None:
    """An order ahead of an own order is re-priced away: the quantity ahead drops at that message."""
    px = {t: to_fixed(t, PRICE_EXP) for t in ("1.00", "0.99")}
    one = to_fixed("1", SIZE_EXP)
    seed = json.dumps(
        {
            "event": "data",
            "channel": "detail_order_book_tstusd",
            "data": {"microtimestamp": "1000", "bids": [["1.00", "1", "1"]], "asks": [["1.01", "1", "3"]]},
        }
    )
    st = SegmentStream(segment=0, market="tstusd", raws={0: seed})
    st.snapshots = [Snapshot(0, 1_000_000, 1000, "ws")]
    st.orders = [
        OrderEvent(1, 2_000_000, 2000, "order_created", 10, "buy", px["1.00"], one),
        OrderEvent(2, 3_000_000, 3000, "order_changed", 1, "buy", px["0.99"], one),
    ]
    local = matcher._replay_stream(st, {10}, {10}, "ws")
    assert [(q["cause"], q["ahead_orders"], q["ahead_qty"]) for q in local[10].trace] == [
        ("order_created", 1, one),
        ("order_changed", 0, 0),
    ]


def test_venue_status_cross_check(synth_default, tmp_path: Path) -> None:
    truth = {o["i"]: o for o in synth_default.own}
    lines = synth_default.log.read_text().splitlines()
    extra = []
    for i in (0, 1):
        oid = truth[i]["order_id"]
        qty = truth[i]["filled_qty"] + (0 if i == 0 else 1)
        tx = [{"tid": 1, "synusd": "0", "syn": f"{qty / 1e8:.8f}", "price": "1"}]
        extra.append(
            json.dumps(
                {
                    "type": "status",
                    "i": i,
                    "outcome": "ok",
                    "endpoint": "order_status",
                    "params": {"id": str(oid)},
                    "sent_ns": 1,
                    "recv_ns": 2,
                    "body": {"id": str(oid), "status": "Finished", "transactions": tx},
                }
            )
        )
    (tmp_path / "s.jsonl").write_text("\n".join(lines[:-1] + extra + lines[-1:]) + "\n")
    doc = matcher.match(synth_default.capture, [tmp_path / "s.jsonl"])
    by = {r["index"]: r for r in doc["orders"]}
    assert by[0]["venue_consistent"] is True and by[1]["venue_consistent"] is False
    assert doc["venue_inconsistent"] == 1


def test_canonical_log_validates_and_maps_events(synth_default, tmp_path: Path) -> None:
    doc = matcher.match(synth_default.capture, [synth_default.log])
    res = canonical.write_logs(doc, tmp_path / "live", "acct1")
    seg = res["segments"]["0"]
    assert seg["n_orders"] == len(synth_default.own) and res["excluded"]["not_in_feed"] == 0
    report = live.check(seg["path"])
    rep = report if isinstance(report, dict) else report.model_dump()
    assert rep["errors"] == []
    ev = pq.read_table(Path(seg["path"]) / "events.parquet").to_pylist()
    od = pq.read_table(Path(seg["path"]) / "orders.parquet").to_pylist()
    assert {o["venue"] for o in od} == {"bitstamp"} and all(o["post_only"] for o in od)
    assert all(o["order_key"] == f"bitstamp:acct1:{o['venue_order_id']}" for o in od)
    assert {o["validation_status"] for o in od} == {"fixture_only"}
    kinds = {e["event_type"] for e in ev}
    assert {"submit_sent", "ack", "fill", "cancel_sent", "cancel_ack"} <= kinds and "expire" not in kinds
    assert {e["ts_precision"] for e in ev if e["event_type"] in ("ack", "fill")} == {"ms"}
    fills = [e for e in ev if e["event_type"] == "fill"]
    assert sum(f["qty"] for f in fills) == sum(o["filled_qty"] for o in synth_default.own)
    with pytest.raises(QtError):
        canonical.write_logs(doc, tmp_path / "x", "a:b")


def test_canonical_rows_for_rejects_removed_and_gaps(synth_default, tmp_path: Path) -> None:
    doc = matcher.match(synth_default.capture, [synth_default.log])
    rows = [dict(r) for r in doc["orders"]]
    rows[0].update(matched_by="rejected", created_us=None, reject_reason="Minimum order size")
    rows[1].update(crosses_gap=True)
    rows[2].update(
        end_state="removed",
        cancel_sent_ns=None,
        deleted_us=rows[2]["deleted_us"] or rows[2]["created_us"],
        deleted_recv_ns=rows[2]["deleted_recv_ns"] or rows[2]["created_recv_ns"],
    )
    rows[3].update(created_us=None, matched_by="not_in_feed")
    rows[4].update(matched_by="ambiguous", created_us=None)
    per_seg, excluded = canonical.build_rows({**doc, "orders": rows}, "a")
    assert excluded == {"not_in_feed": 1, "crosses_gap": 1, "not_on_book_at_entry": 0, "ambiguous": 1}
    orders, events = per_seg[0]
    rej_key = f"bitstamp:a:{rows[0]['client_order_id']}"
    assert [e["event_type"] for e in events if e["order_key"] == rej_key] == ["submit_sent", "reject"]
    k2 = f"bitstamp:a:{rows[2]['order_id']}"
    assert [e["event_type"] for e in events if e["order_key"] == k2][-1] == "expire"
    live.write_canonical_log(tmp_path / "canon-check", orders, events)


def test_orders_spanning_a_gap_are_flagged(tmp_path: Path) -> None:
    r = synth.build(tmp_path / "g", seed=1, steps=600, n_own=20, reconnect_at=(300,))
    doc = matcher.match(r.capture, [r.log])
    spans = [x for x in doc["orders"] if x["crosses_gap"]]
    assert len(spans) >= 1
    per_seg, excluded = canonical.build_rows(doc, "a")
    assert excluded["crosses_gap"] == len(spans)
    keys = {o["order_key"] for orders, _ in per_seg.values() for o in orders}
    assert not keys & {f"bitstamp:a:{x['order_id']}" for x in spans}
    assert all(x["segment"] in (0, 1) for x in doc["orders"] if x["created_us"] is not None)


def test_stand_in_log_rehearses_the_matcher(synth_default, tmp_path: Path) -> None:
    from livefill.standin import write_stand_in_log

    res = write_stand_in_log(synth_default.capture, tmp_path / "s.jsonl", n=40, seed=1)
    assert res["orders"] == 40 and res["market"] == "synusd"
    coids = [r.client_order_id for r in matcher.harness_orders([tmp_path / "s.jsonl"])]
    assert all(c.startswith("standin-synusd-1-") for c in coids)
    rnd = write_stand_in_log(synth_default.capture, tmp_path / "r.jsonl", n=40, seed=1, sample="random")
    assert rnd["orders"] == 40 and rnd["sample"] == "random"
    doc = matcher.match(synth_default.capture, [tmp_path / "s.jsonl"])
    assert doc["matched_by"] == {"id": 40} and doc["fills_inconsistent"] == 0 and doc["fifo_violations"] == 0
    assert doc["n_filled_any"] > 0
    assert all(f["liquidity"] == "maker" for f in doc["fills"])
    assert all(r["amount_traded_consistent"] is True for r in doc["orders"])
    with pytest.raises(QtError):
        write_stand_in_log(synth_default.capture, tmp_path / "t.jsonl", market="nope")
