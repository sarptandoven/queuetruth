"""Matched own orders -> canonical live log (`qt.live_orders` + `qt.live_events`, the directory `queuetruth live
import` writes), one log per capture segment, through `queuetruth.live.write_canonical_log` (validated on write).

Per order (`order_key` = `bitstamp:<account>:<venue order id>`; venue `bitstamp`; limit, `gtc`; `post_only` =
maker-or-cancel):

| event | clocks | from |
|---|---|---|
| `submit_sent` | `ts_local_send` = harness send time | harness log |
| `ack` | `ts_exchange` = `order_created` stamp (`match_time`), `ts_local_recv` = REST response time | feed + harness |
| `fill` | `ts_exchange` = trade stamp (`match_time`), `ts_local_recv` = feed receive time | `live_trades` |
| `cancel_sent` | `ts_local_send` = harness cancel time | harness log |
| `cancel_ack` / `expire` | `ts_exchange` = `order_deleted` stamp, `ts_local_recv` = feed receive time | `live_orders` |

`cancel_ack` when the harness had sent a cancel before the delete arrived, `expire` otherwise (the venue removed the
order). A fully filled order has no terminal event (the scheduler keeps `ts_cancel` null). `fill_id` is the venue
trade id; `cum_qty` / `leaves_qty` are running totals; `fee` / `fee_ccy` come from the order's final `order_status`
transaction with the same trade id (live runs; null otherwise: the public feed carries no fees).
`ts_precision` is `ms` when every feed stamp of the log is a whole millisecond (what Bitstamp sends in practice),
else `us`. Rejected submits become `submit_sent` + `reject` under `bitstamp:<account>:<client_order_id>`, in the
segment that was being recorded when they were sent.
`validation_status` is `real` for orders of a live harness run, `fixture_only` otherwise.

Excluded (counted in the summary): orders not found in the feed, orders whose life spans a capture gap, and orders
not on the replayed book at entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from livefill.units import PRICE_EXP, SIZE_EXP, to_fixed
from queuetruth.errors import QtError

VENUE = "bitstamp"


def _key(account: str, ident: str) -> str:
    return f"{VENUE}:{account}:{ident}"


def _segment_at(match_doc: dict[str, Any], t_ns: int) -> int:
    """The capture segment whose receive-time range holds `t_ns` (the last one starting before it, else 0)."""
    best = 0
    for s in match_doc.get("segments", []):
        if s.get("first_recv_ns") is not None and s["first_recv_ns"] <= t_ns:
            best = int(s["segment"])
    return best


def build_rows(
    match_doc: dict[str, Any], account: str
) -> tuple[dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]], dict[str, int]]:
    """Canonical order and event rows per segment, and the exclusion counts."""
    if not account or ":" in account:
        raise QtError("QT-USAGE-001", f"account {account!r}: a non-empty label without ':'")
    fills_by: dict[int, list[dict[str, Any]]] = {}
    for f in match_doc.get("fills", []):
        fills_by.setdefault(int(f["order_id"]), []).append(f)
    excluded = {"not_in_feed": 0, "crosses_gap": 0, "not_on_book_at_entry": 0, "ambiguous": 0}
    per_seg: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    stamps: list[int] = []
    rows = match_doc["orders"]
    for r in rows:
        if r["created_us"] is not None:
            stamps.append(r["created_us"])
            if r["deleted_us"] is not None:
                stamps.append(r["deleted_us"])
            stamps += [f["us"] for f in fills_by.get(r["order_id"], [])]
    precision = "ms" if stamps and all(s % 1000 == 0 for s in stamps) else "us"
    for r in rows:
        validation = "real" if r["mode"] == "live" else "fixture_only"
        common_order = {
            "venue": VENUE,
            "account": account,
            "instrument": r["market"],
            "instrument_id": r["instrument_id"],
            "client_order_id": r["client_order_id"],
            "side": r["side"],
            "order_type": "limit",
            "tif": "gtc",
            "post_only": bool(r.get("post_only")),
            "orig_qty": to_fixed(r["amount_str"], SIZE_EXP, "amount"),
            "qty_str": r["amount_str"],
            "limit_px": to_fixed(r["price_str"], PRICE_EXP, "price"),
            "px_str": r["price_str"],
            "price_exp": PRICE_EXP,
            "size_exp": SIZE_EXP,
            "strategy_tag": f"livefill:{r['run']}",
            "validation_status": validation,
        }
        if r["matched_by"] == "rejected":
            key = _key(account, r["client_order_id"])
            seg_rows = per_seg.setdefault(_segment_at(match_doc, r["submit_sent_ns"]), ([], []))
            seg_rows[0].append({**common_order, "order_key": key})
            seg_rows[1].extend(
                [
                    {
                        "order_key": key,
                        "event_type": "submit_sent",
                        "ts_local_send": r["submit_sent_ns"],
                        "ts_exchange_kind": "unknown",
                        "ts_precision": "ns",
                        "source": "rest",
                    },
                    {
                        "order_key": key,
                        "event_type": "reject",
                        "ts_local_recv": r["submit_recv_ns"],
                        "ts_exchange_kind": "unknown",
                        "ts_precision": "ns",
                        "source": "rest",
                        "reason": r.get("reject_reason"),
                    },
                ]
            )
            continue
        if r["matched_by"] == "ambiguous":
            excluded["ambiguous"] += 1
            continue
        if r["created_us"] is None:
            excluded["not_in_feed"] += 1
            continue
        if r.get("crosses_gap"):
            excluded["crosses_gap"] += 1
            continue
        if r["entry_ahead_orders"] is None:
            excluded["not_on_book_at_entry"] += 1
            continue
        key = _key(account, str(r["order_id"]))
        orders, events = per_seg.setdefault(int(r["segment"]), ([], []))
        orders.append({**common_order, "order_key": key, "venue_order_id": str(r["order_id"])})
        ev = {"order_key": key}
        events.append(
            {
                **ev,
                "event_type": "submit_sent",
                "ts_local_send": r["submit_sent_ns"],
                "ts_exchange_kind": "unknown",
                "ts_precision": "ns",
                "source": "rest",
            }
        )
        events.append(
            {
                **ev,
                "event_type": "ack",
                "ts_exchange": r["created_us"] * 1000,
                "ts_local_recv": r["submit_recv_ns"] if r["submit_recv_ns"] is not None else r["created_recv_ns"],
                "ts_exchange_kind": "match_time",
                "ts_precision": precision,
                "source": "ws",
                "reason": f"live_orders order_created (feed seq {r['created_seq']}, matched by {r['matched_by']})",
            }
        )
        orig = common_order["orig_qty"]
        cum = 0
        timeline: list[tuple[int, int, dict[str, Any]]] = []
        for f in sorted(fills_by.get(r["order_id"], []), key=lambda f: f["seq"]):
            cum += f["qty"]
            timeline.append(
                (
                    f["recv_ns"],
                    f["seq"],
                    {
                        **ev,
                        "event_type": "fill",
                        "qty": f["qty"],
                        "px": f["px"],
                        "fill_id": str(f["trade_id"]),
                        "liquidity": f["liquidity"],
                        "fee": f.get("fee"),
                        "fee_ccy": f.get("fee_ccy"),
                        "cum_qty": cum,
                        "leaves_qty": max(orig - cum, 0),
                        "ts_exchange": f["us"] * 1000,
                        "ts_local_recv": f["recv_ns"],
                        "ts_exchange_kind": "match_time",
                        "ts_precision": precision,
                        "source": "ws",
                    },
                )
            )
        if r["cancel_sent_ns"] is not None:
            timeline.append(
                (
                    r["cancel_sent_ns"],
                    -1,
                    {
                        **ev,
                        "event_type": "cancel_sent",
                        "ts_local_send": r["cancel_sent_ns"],
                        "ts_exchange_kind": "unknown",
                        "ts_precision": "ns",
                        "source": "rest",
                    },
                )
            )
        if r["end_state"] in ("cancelled", "removed"):
            timeline.append(
                (
                    r["deleted_recv_ns"],
                    10**18,
                    {
                        **ev,
                        "event_type": "cancel_ack" if r["end_state"] == "cancelled" else "expire",
                        "ts_exchange": r["deleted_us"] * 1000,
                        "ts_local_recv": r["deleted_recv_ns"],
                        "ts_exchange_kind": "match_time",
                        "ts_precision": precision,
                        "source": "ws",
                        "leaves_qty": r["remaining_qty"],
                        "cum_qty": cum,
                        "reason": None if r["end_state"] == "cancelled" else "removed by the venue (no cancel sent)",
                    },
                )
            )
        timeline.sort(key=lambda x: (x[0], x[1]))
        events += [e for _, _, e in timeline]
    return per_seg, excluded


def write_logs(match_doc: dict[str, Any], out: str | Path, account: str) -> dict[str, Any]:
    """Writes `out/seg-NNNN/` canonical logs; returns `{segments: {seg: {path, n_orders, n_events}}, excluded}`."""
    from queuetruth import live

    per_seg, excluded = build_rows(match_doc, account)
    out = Path(out)
    segs: dict[str, Any] = {}
    for seg, (orders, events) in sorted(per_seg.items()):
        d = out / f"seg-{seg:04d}"
        live.write_canonical_log(d, orders, events)
        segs[str(seg)] = {"path": str(d), "n_orders": len(orders), "n_events": len(events)}
    return {"segments": segs, "excluded": excluded}
