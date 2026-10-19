"""Validator of the canonical live log (SPEC 3.8).

Errors (`QT-INPUT-003` when any is a duplicate, else `QT-INPUT-002`): table schema mismatch, duplicate or invalid
`order_key`, events of unknown orders, `event_seq` not contiguous from 1, duplicate `fill_id` per (venue,
instrument, order side) after dedupe (the side is part of the key so both sides of a self-trade keep the shared
venue trade id; SPEC 3.8 says (venue, instrument, fill_id), review F10/L6), `cum_qty` different from the running
net fill sum (busts subtract the referenced fill, corrections replace it), a bust / correction referencing no fill
of the order, an event without any timestamp, missing `ts_exchange_kind` / `ts_precision`, a 1.1
`replaces_order_key` naming no other order of the log.

Warnings: `cum_qty + leaves_qty` different from the order's current total qty after a fill (the total follows
`amend_ack` / `restate` quantities, so legitimate amends do not warn), fills of a `post_only` order with
`liquidity = taker`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.live._common import valid_id
from queuetruth.live.canonical import EVENTS, ORDERS, read_log, table_rows
from queuetruth.schemas.arrow import FULL_SCHEMAS, SCHEMAS
from queuetruth.schemas.models import LiveValidation


def _schema_errors(table: pa.Table, name: str) -> list[str]:
    """Columns and types must equal the v0.1 or the full 1.1 layout of `name`."""
    got = [(f.name, f.type) for f in table.schema]
    for expect in (SCHEMAS[name], FULL_SCHEMAS[name]):
        if got == [(f.name, f.type) for f in expect]:
            return []
    return [f"{name}: columns or types differ from the schema (v0.1 or 1.1 layout)"]


def check_tables(orders: pa.Table, events: pa.Table) -> tuple[list[str], list[str], bool]:
    """(errors, warnings, any_duplicate) of a canonical log held in memory."""
    errors = _schema_errors(orders, ORDERS) + _schema_errors(events, EVENTS)
    warnings: list[str] = []
    dup = False
    if errors:
        return errors, warnings, dup
    orows = table_rows(orders)
    erows = table_rows(events)
    by_key: dict[str, dict[str, Any]] = {}
    for o in orows:
        k = o["order_key"]
        if not valid_id(k):
            errors.append(f"invalid order_key {k!r}")
        if k in by_key:
            errors.append(f"duplicate order_key {k}")
            dup = True
        by_key[k] = o
    for o in orows:
        rk = o.get("replaces_order_key")
        if rk is not None and (rk == o["order_key"] or rk not in by_key):
            errors.append(f"{o['order_key']}: replaces_order_key {rk!r} names no other order of the log")
    per_order: dict[str, list[dict[str, Any]]] = {}
    for e in erows:
        per_order.setdefault(e["order_key"], []).append(e)
    fill_ids: dict[tuple[str, str, str, str], str] = {}
    for k in sorted(per_order):
        evs = sorted(per_order[k], key=lambda r: r["event_seq"])
        o = by_key.get(k)
        if o is None:
            errors.append(f"{k}: events for an order missing from orders")
            continue
        seqs = [e["event_seq"] for e in evs]
        if seqs != list(range(1, len(evs) + 1)):
            errors.append(f"{k}: event_seq not contiguous from 1 ({seqs[:10]})")
        cum = 0
        total = o["orig_qty"]
        fills: dict[str, int] = {}
        for e in evs:
            where = f"{k}#{e['event_seq']}"
            t = e["event_type"]
            if e["ts_local_send"] is None and e["ts_exchange"] is None and e["ts_local_recv"] is None:
                errors.append(f"{where}: no timestamp")
            if e["ts_exchange_kind"] is None or e["ts_precision"] is None:
                errors.append(f"{where}: ts_exchange_kind / ts_precision not set")
            if t in ("amend_ack", "restate") and e["qty"] is not None:
                total = e["qty"]
            if t == "fill":
                q = e["qty"] or 0
                cum += q
                fid = e["fill_id"]
                if fid is not None:
                    key = (o["venue"], o["instrument"], o["side"], fid)
                    if key in fill_ids:
                        errors.append(f"{where}: duplicate fill_id {fid} (also {fill_ids[key]})")
                        dup = True
                    else:
                        fill_ids[key] = where
                    fills[fid] = q
                if o["post_only"] and e["liquidity"] == "taker":
                    warnings.append(f"{where}: taker fill on a post_only order")
            elif t in ("fill_bust", "fill_correct"):
                ref = e["ref_fill_id"]
                if ref is None or ref not in fills:
                    errors.append(f"{where}: {t} references no fill of the order ({ref!r})")
                elif t == "fill_bust":
                    cum -= fills.pop(ref)
                else:
                    new = e["qty"] if e["qty"] is not None else fills[ref]
                    cum += new - fills[ref]
                    fills[ref] = new
            if e["cum_qty"] is not None and e["cum_qty"] != cum:
                errors.append(f"{where}: cum_qty {e['cum_qty']} != running net fill qty {cum}")
            if t == "fill" and e["cum_qty"] is not None and e["leaves_qty"] is not None:
                if e["cum_qty"] + e["leaves_qty"] != total:
                    warnings.append(
                        f"{where}: cum_qty + leaves_qty = {e['cum_qty'] + e['leaves_qty']} != order qty {total}"
                    )
    return errors, warnings, dup


def check(path: str | Path) -> LiveValidation:
    """`qt.live_validation` document of a canonical live-log directory (never raises on validation errors)."""
    orders, events = read_log(path)
    errors, warnings, _ = check_tables(orders, events)
    return LiveValidation(
        path=str(path), n_orders=orders.num_rows, n_events=events.num_rows, errors=errors, warnings=warnings
    )


def raise_on_errors(errors: list[str], dup: bool, where: str) -> None:
    """Raises the SPEC 3.8 validator error for a non-empty error list."""
    if errors:
        code = "QT-INPUT-003" if dup else "QT-INPUT-002"
        shown = "; ".join(errors[:10]) + (f"; ... ({len(errors)} errors)" if len(errors) > 10 else "")
        raise QtError(code, f"{where}: {shown}", details={"errors": errors})


def validate(path: str | Path) -> list[str]:
    """Validates a canonical live-log directory: returns the warnings, raises `QtError` on errors."""
    orders, events = read_log(path)
    errors, warnings, dup = check_tables(orders, events)
    raise_on_errors(errors, dup, str(path))
    return warnings
