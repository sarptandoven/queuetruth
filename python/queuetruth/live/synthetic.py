"""Deterministic synthetic canonical live logs (tests, examples, scorer fixtures).

Each order: `submit_sent` (local clock), `ack` (gateway_time) after an entry latency, optionally one amend
(qty down keeping priority, or a price change losing it), zero or more fills (match_time, maker/taker), and an end:
full fill, user cancel (`cancel_sent` + `cancel_ack`) or nothing (still open). A share of orders are IOC (ack,
optional fill, `expire`). RNG: `numpy.random.default_rng(stable_seed(seed, [b"synthetic_log"]))`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.live.canonical import build_log
from queuetruth.stats import stable_seed


def synthetic_log(seed: int, n_orders: int, **kw: Any) -> tuple[pa.Table, pa.Table]:
    """A synthetic canonical live log (`orders`, `events`) that passes `validate`.

    Keyword options (defaults): `venue` ("synthetic"), `account` ("acct1"), `instrument` ("SYN"),
    `instrument_id` (1), `t0_ns` (2026-01-05 14:30 UTC), `price_exp` (-9), `size_exp` (0), `tick` (10_000_000 =
    0.01), `base_px` (100_000_000_000 = 100.00), `max_qty` (10), `spacing_ns` (1 s), `entry_latency_ns`
    (300 us mean), `response_ns` (200 us mean), `p_ioc` (0.1), `p_amend` (0.2), `p_fill` (0.5), `p_cancel` (0.6),
    `account_dup` (False: no two orders share account/side/price).
    """
    venue = str(kw.get("venue", "synthetic"))
    account = str(kw.get("account", "acct1"))
    instrument = str(kw.get("instrument", "SYN"))
    iid = kw.get("instrument_id", 1)
    t0 = int(kw.get("t0_ns", 1_767_623_400_000_000_000))
    pexp, sexp = int(kw.get("price_exp", -9)), int(kw.get("size_exp", 0))
    tick, base = int(kw.get("tick", 10_000_000)), int(kw.get("base_px", 100_000_000_000))
    max_qty = int(kw.get("max_qty", 10))
    spacing = int(kw.get("spacing_ns", 1_000_000_000))
    lat_e, lat_r = float(kw.get("entry_latency_ns", 300_000)), float(kw.get("response_ns", 200_000))
    p_ioc, p_amend = float(kw.get("p_ioc", 0.1)), float(kw.get("p_amend", 0.2))
    p_fill, p_cancel = float(kw.get("p_fill", 0.5)), float(kw.get("p_cancel", 0.6))
    rng = np.random.default_rng(stable_seed(seed, [b"synthetic_log"]))
    orders: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for i in range(n_orders):
        key = f"{venue}:{account}:S{i:06d}"
        side = "buy" if rng.random() < 0.5 else "sell"
        offset = int(rng.integers(0, 5))
        px = base - offset * tick if side == "buy" else base + offset * tick
        qty = int(rng.integers(1, max_qty + 1))
        ioc = bool(rng.random() < p_ioc)
        orders.append(
            {
                "order_key": key,
                "venue": venue,
                "account": account,
                "instrument": instrument,
                "instrument_id": iid,
                "venue_order_id": f"V{i:06d}",
                "client_order_id": f"C{i:06d}",
                "side": side,
                "order_type": "limit",
                "tif": "ioc" if ioc else "gtc",
                "post_only": False,
                "orig_qty": qty,
                "qty_str": format(Decimal(qty).scaleb(sexp), "f"),
                "limit_px": px,
                "px_str": format(Decimal(px).scaleb(pexp), "f"),
                "price_exp": pexp,
                "size_exp": sexp,
                "validation_status": "fixture_only",
            }
        )
        t = t0 + i * spacing
        ev: list[dict[str, Any]] = []

        def add(event_type: str, key: str = key, ev: list[dict[str, Any]] = ev, **v: Any) -> None:
            row = {"order_key": key, "event_type": event_type, "source": "synthetic", "ts_precision": "ns"}
            row.setdefault("ts_exchange_kind", v.pop("kind", "unknown"))
            row.update(v)
            ev.append(row)

        ack_ex = t + int(rng.exponential(lat_e)) + 1
        add("submit_sent", qty=qty, px=px, ts_local_send=t)
        add(
            "ack",
            qty=qty,
            px=px,
            leaves_qty=qty,
            cum_qty=0,
            ts_exchange=ack_ex,
            ts_local_recv=ack_ex + int(rng.exponential(lat_r)) + 1,
            kind="gateway_time",
        )
        now = ack_ex
        total, cum, nfill = qty, 0, 0
        if not ioc and qty > 1 and rng.random() < p_amend:
            now += int(rng.integers(10_000_000, 200_000_000))
            if rng.random() < 0.5:
                total = int(rng.integers(1, qty))
                keeps, new_px = True, px
            else:
                keeps, new_px = False, px - tick if side == "buy" else px + tick
                px = new_px
            add("amend_sent", qty=total, px=new_px, ts_local_send=now)
            now += int(rng.exponential(lat_e)) + 1
            add(
                "amend_ack",
                qty=total,
                px=new_px,
                leaves_qty=total,
                cum_qty=0,
                amend_keeps_priority=keeps,
                ts_exchange=now,
                ts_local_recv=now + int(rng.exponential(lat_r)) + 1,
                kind="gateway_time",
            )
        while cum < total and rng.random() < p_fill:
            now += int(rng.integers(1_000_000, 300_000_000)) if not ioc else 0
            q = int(rng.integers(1, total - cum + 1))
            cum += q
            nfill += 1
            add(
                "fill",
                qty=q,
                px=px,
                leaves_qty=total - cum,
                cum_qty=cum,
                fill_id=f"{key}:F{nfill}",
                liquidity="taker" if ioc else "maker",
                fee=round(q * 0.001, 6),
                fee_ccy="USD",
                ts_exchange=now,
                ts_local_recv=now + int(rng.exponential(lat_r)) + 1,
                kind="match_time",
            )
            if ioc:
                break
        if ioc and cum < total:
            add("expire", leaves_qty=0, cum_qty=cum, ts_exchange=now, reason="ioc remainder", kind="gateway_time")
        elif cum < total and rng.random() < p_cancel:
            now += int(rng.integers(10_000_000, 2_000_000_000))
            add("cancel_sent", ts_local_send=now)
            now += int(rng.exponential(lat_e)) + 1
            add(
                "cancel_ack",
                leaves_qty=0,
                cum_qty=cum,
                ts_exchange=now,
                ts_local_recv=now + int(rng.exponential(lat_r)) + 1,
                kind="gateway_time",
            )
        events.extend(ev)
    return build_log(orders, events)
