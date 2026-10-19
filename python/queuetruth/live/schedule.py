"""Live log -> fixed shadow schedule (SPEC 5.7.2) and latency statistics (SPEC 5.4 "Latency").

Schedule rules:
- Scheduled: `order_type = limit` with a resting tif (`gtc`, `day`, `post_only`, `gtx`, and the 1.1 value `gtd`:
  a good-till-date order rests until its expiry, which arrives as an `expire` event), a limit price and an
  `instrument_id`. Everything else is excluded and counted by reason (market / IOC / FOK / unknown tif, no price,
  unmapped instrument, rejected before an ack, no usable entry time).
- `entry = exchange_ack`: `ts_submit` = first ack's `ts_exchange` when its kind is match_time or gateway_time,
  `latency_ns = 0`; otherwise the order falls back to `send_plus_latency` (counted, warned). `send_plus_latency`:
  `ts_submit` = first `submit_sent.ts_local_send`, `latency_ns` = the given latency or the median `entry_ns` of
  `latency_stats` (0 with a warning when there is none).
- End: the first cancel_ack / expire / done / reject after entry gives `ts_cancel` (exchange time in ack mode; the
  preceding `cancel_sent` local time in send mode, else the exchange time with a warning). Filled orders keep
  `ts_cancel` null.
- Amends (`amend_ack`, `restate`): a qty decrease at the same price that keeps priority (`amend_keeps_priority`
  true, or null on a venue whose documented rule keeps priority on size decrease: `KEEP_PRIORITY_VENUES`) becomes a
  `qt.shadow_amend` row of the current leg (`new_qty` = the instruction's new total qty);
  a price change, a qty increase, a priority-losing decrease, or on CME an `amend_ack` that changes neither price nor
  size (`UNCHANGED_MODIFY_LOSES_PRIORITY`, A6) cancels the current leg at the amend time and starts a new leg sized
  at the instruction's total qty (`amend_ack` / `restate` qty, else the preceding `amend_sent` qty, else the previous
  total). Audit ruling A8: legs follow the live order's instructions as sent and never quantities derived from live
  fills (an earlier version sized later legs as `new total - live cumulative fills`); each arm truncates leg `k` to
  its total minus that arm's own fills of earlier legs (`queuetruth.score.outcomes.merge_legs`). An amend to a total
  of 0 ends the order without a new leg.
- Leg ids: the first leg's `shadow_id` is the `order_key`; later legs are `<order_key>.leg<k>` (k = 2, 3, ...;
  SPEC writes `<order_key>#<k>`, but `#` is outside the id character set). `order_key` is set on every leg.
- Rows are sorted by (`order_key`, leg); amends by (`shadow_id`, `ts_send`).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.live.canonical import ENTRY_KINDS, RESTING_TIF, entry_fallback_warning, read_log, table_rows
from queuetruth.live.validate import check_tables, raise_on_errors
from queuetruth.schemas.arrow import DICT_VALUES, SCHEMAS

TERMINAL = ("cancel_ack", "expire", "done", "reject", "post_only_reject")
#: Venues whose documented amend rule keeps queue priority on a size decrease at the same price (used when
#: `amend_keeps_priority` is null): Binance keepPriority, Kraken, Deribit, and CME (A6: a modify keeps priority
#: only as a pure quantity decrease at the same price; a same-size or larger modify starts a new leg; canonical
#: venue `cme`, e.g. a FIX log imported with `fix.venue_name = cme`). Coinbase FIX (SPEC 5.7.2, E17) is handled by
#: the FIX importer, which writes `amend_keeps_priority = true` on such replaces when `fix.venue_name` is
#: `coinbase` (the native Coinbase importer's inferred amends stay "not kept").
KEEP_PRIORITY_VENUES: frozenset[str] = frozenset({"binance", "kraken", "deribit", "cme"})
#: Venues where a user modify (`amend_ack`) that leaves price and size unchanged still loses priority (A6, E27: CME
#: iceberg refresh / account change); elsewhere such an amend changes nothing in the schedule.
UNCHANGED_MODIFY_LOSES_PRIORITY: frozenset[str] = frozenset({"cme"})


class LiveScheduleWarning(UserWarning):
    """Warning emitted by `to_schedule` (fallbacks, exclusions, clock caveats)."""


@dataclass
class Schedule:
    """Shadow schedule of a live log with its accounting."""

    spec: pa.Table
    amends: pa.Table
    legs: dict[str, list[str]]
    n_orders: int
    n_scheduled: int
    n_entry_fallback: int
    excluded: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    latency_ns: int | None = None

    @property
    def entry_fallback_share(self) -> float | None:
        """Share of scheduled orders whose entry fell back to send_plus_latency."""
        return None if self.n_scheduled == 0 else self.n_entry_fallback / self.n_scheduled


def _dict_array(values: list[str | None], table: str, col: str) -> pa.Array:
    allowed = DICT_VALUES[table][col]
    idx = [None if v is None else allowed.index(v) for v in values]
    return pa.DictionaryArray.from_arrays(pa.array(idx, type=pa.int8()), pa.array(list(allowed), type=pa.utf8()))


def _spec_table(rows: list[dict[str, Any]]) -> pa.Table:
    name = "qt.shadow_spec"
    schema = SCHEMAS[name]
    cols = []
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_dictionary(f.type):
            cols.append(_dict_array(vals, name, f.name))
        else:
            cols.append(pa.array(vals, type=f.type))
    return pa.Table.from_arrays(cols, schema=schema)


def _amend_table(rows: list[dict[str, Any]]) -> pa.Table:
    schema = SCHEMAS["qt.shadow_amend"]
    return pa.Table.from_arrays([pa.array([r[f.name] for r in rows], type=f.type) for f in schema], schema=schema)


def _quantiles(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"p10": None, "p50": None, "p90": None, "p99": None, "n": 0}
    arr = np.sort(np.asarray(values, dtype=np.float64))
    q = np.percentile(arr, [10, 50, 90, 99])
    return {"p10": float(q[0]), "p50": float(q[1]), "p90": float(q[2]), "p99": float(q[3]), "n": int(arr.size)}


def latency_from_rows(orows: list[dict[str, Any]], erows: list[dict[str, Any]]) -> dict[str, Any]:
    """`latency_stats` on in-memory canonical rows."""
    per: dict[str, list[dict[str, Any]]] = {}
    for e in erows:
        per.setdefault(e["order_key"], []).append(e)
    samples: dict[str, list[int]] = {"entry_ns": [], "response_ns": [], "cancel_ns": [], "amend_ns": []}
    # single-clock round trips: local send (outbound log / SendingTime) -> local receive of the venue's answer
    rtt: dict[str, list[int]] = {"ack_rtt_ns": [], "cancel_rtt_ns": [], "amend_rtt_ns": []}
    negative = dict.fromkeys([*samples, *rtt], 0)  # reported as negative_dropped / rtt_negative_dropped
    wrong_kind = 0

    def add(metric: str, v: int) -> None:
        if v < 0:
            negative[metric] += 1
        else:
            (rtt if metric in rtt else samples)[metric].append(v)

    for k in sorted(per):
        evs = sorted(per[k], key=lambda r: r["event_seq"])
        sub = next((e for e in evs if e["event_type"] == "submit_sent" and e["ts_local_send"] is not None), None)
        ack = next((e for e in evs if e["event_type"] == "ack"), None)
        if ack is not None and ack["ts_exchange"] is not None:
            if ack["ts_exchange_kind"] in ENTRY_KINDS:
                if sub is not None:
                    add("entry_ns", ack["ts_exchange"] - sub["ts_local_send"])
                if ack["ts_local_recv"] is not None:
                    add("response_ns", ack["ts_local_recv"] - ack["ts_exchange"])
            else:
                wrong_kind += 1
        if sub is not None and ack is not None and ack["ts_local_recv"] is not None:
            add("ack_rtt_ns", ack["ts_local_recv"] - sub["ts_local_send"])
        for sent_t, ack_t, metric, rtt_metric in (
            ("cancel_sent", "cancel_ack", "cancel_ns", "cancel_rtt_ns"),
            ("amend_sent", "amend_ack", "amend_ns", "amend_rtt_ns"),
        ):
            pending: int | None = None
            for e in evs:
                if e["event_type"] == sent_t and e["ts_local_send"] is not None:
                    pending = e["ts_local_send"]
                elif e["event_type"] == ack_t and pending is not None:
                    if e["ts_exchange"] is not None and e["ts_exchange_kind"] in ENTRY_KINDS:
                        add(metric, e["ts_exchange"] - pending)
                    if e["ts_local_recv"] is not None:
                        add(rtt_metric, e["ts_local_recv"] - pending)
                    pending = None
    out: dict[str, Any] = {m: _quantiles(v) for m, v in samples.items()}
    out["clock_sync_assumed"] = True
    out.update({m: _quantiles(v) for m, v in rtt.items()})
    out["negative_dropped"] = {m: negative[m] for m in samples}
    out["rtt_negative_dropped"] = {m: negative[m] for m in rtt}
    out["ack_record_time_excluded"] = wrong_kind
    out["n_orders"] = len(orows)
    return out


def latency_stats(live_dir: str) -> dict[str, Any]:
    """Latency quantiles of a live log (SPEC 5.4): `entry_ns`, `response_ns`, `cancel_ns`, `amend_ns` (each
    `{p10, p50, p90, p99, n}`, ns), `clock_sync_assumed` (always true), plus `negative_dropped` (per metric),
    `ack_record_time_excluded` and `n_orders`. The first five keys are exactly `qt.calibration_result.latency`.

    Extra single-clock metrics (same quantile shape; no clock synchronisation assumed; negative values counted in
    `rtt_negative_dropped`): `ack_rtt_ns` = first ack `ts_local_recv` - `submit_sent.ts_local_send`,
    `cancel_rtt_ns` / `amend_rtt_ns` = ack `ts_local_recv` - the preceding `*_sent.ts_local_send`. FIX engine logs
    (QuickFIX) give true local send and receive times, so these are the order-entry round trips measured on the
    trading host's own clock."""
    orders, events = read_log(live_dir)
    return latency_from_rows(table_rows(orders), table_rows(events))


def _ts_of(e: dict[str, Any], mode: str, sent: dict[str, Any] | None, w: list[str], what: str) -> int | None:
    if mode == "send" and sent is not None and sent["ts_local_send"] is not None:
        return sent["ts_local_send"]
    if mode == "send":
        w.append(f"{what}: no matching *_sent local time; exchange time used")
    if e["ts_exchange"] is not None:
        return e["ts_exchange"]
    w.append(f"{what}: no exchange time; local receive time used")
    return e["ts_local_recv"]


def schedule_from_rows(
    orows: list[dict[str, Any]],
    erows: list[dict[str, Any]],
    *,
    entry: str = "exchange_ack",
    latency_ns: int | None = None,
    keep_priority_venues: frozenset[str] | set[str] | None = None,
) -> Schedule:
    """`build_schedule` on in-memory canonical rows (validated by the caller)."""
    if entry not in ("exchange_ack", "send_plus_latency"):
        raise QtError("QT-CONFIG-001", f"entry must be exchange_ack or send_plus_latency, got {entry!r}")
    # Venue names compare case-insensitively (`CME` gets the CME rules).
    keep = frozenset(
        str(v).lower() for v in (KEEP_PRIORITY_VENUES if keep_priority_venues is None else keep_priority_venues)
    )
    notes: list[str] = []
    excluded: dict[str, int] = {}
    lat = latency_ns
    if lat is None:
        stats = latency_from_rows(orows, erows)
        p50 = stats["entry_ns"]["p50"]
        lat_default = round(p50) if p50 is not None else 0
    else:
        lat_default = lat
    per: dict[str, list[dict[str, Any]]] = {}
    for e in erows:
        per.setdefault(e["order_key"], []).append(e)
    exps: dict[int, tuple[int, int]] = {}
    spec_rows: list[dict[str, Any]] = []
    amend_rows: list[dict[str, Any]] = []
    legs: dict[str, list[str]] = {}
    n_sched = 0
    n_fallback = 0
    no_latency_warned = False

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for o in sorted(orows, key=lambda r: r["order_key"]):
        k = o["order_key"]
        venue = str(o.get("venue") or "").lower()
        evs = sorted(per.get(k, []), key=lambda r: r["event_seq"])
        if o["order_type"] != "limit":
            exclude(f"order_type_{o['order_type']}")
            continue
        if o["tif"] not in RESTING_TIF:
            exclude(f"tif_{o['tif']}")
            continue
        if o["limit_px"] is None:
            exclude("no_limit_px")
            continue
        if o["instrument_id"] is None:
            exclude("no_instrument_id")
            continue
        iid = o["instrument_id"]
        if iid in exps and exps[iid] != (o["price_exp"], o["size_exp"]):
            raise QtError("QT-CONFIG-001", f"instrument_id {iid}: orders with different price_exp/size_exp")
        exps[iid] = (o["price_exp"], o["size_exp"])
        types = [e["event_type"] for e in evs]
        if "ack" not in types and any(t in ("reject", "post_only_reject") for t in types):
            exclude("rejected")
            continue
        ack_i = next((i for i, e in enumerate(evs) if e["event_type"] == "ack"), None)
        sub_i = next((i for i, e in enumerate(evs) if e["event_type"] == "submit_sent"), None)
        mode = "send"
        start_i = sub_i
        if entry == "exchange_ack" and ack_i is not None:
            a = evs[ack_i]
            if a["ts_exchange"] is not None and a["ts_exchange_kind"] in ENTRY_KINDS:
                mode, start_i = "ack", ack_i
        if mode == "send":
            if sub_i is None or evs[sub_i]["ts_local_send"] is None:
                exclude("no_entry_time")
                continue
            if entry == "exchange_ack":
                n_fallback += 1
            if lat is None and lat_default == 0 and not no_latency_warned:
                notes.append("send_plus_latency without entry latency samples: latency_ns = 0")
                no_latency_warned = True
        assert start_i is not None
        ts_submit = evs[start_i]["ts_exchange"] if mode == "ack" else evs[start_i]["ts_local_send"]
        leg_lat = 0 if mode == "ack" else lat_default
        total = o["orig_qty"]
        px = o["limit_px"]
        leg_list: list[dict[str, Any]] = [
            {"shadow_id": k, "qty": total, "px": px, "ts_submit": ts_submit, "ts_cancel": None, "cur": total}
        ]
        last_sent: dict[str, dict[str, Any]] = {}
        for i, e in enumerate(evs):
            t = e["event_type"]
            if t in ("cancel_sent", "amend_sent"):
                last_sent[t] = e
            if i <= start_i:
                continue
            leg = leg_list[-1]
            if t in ("amend_ack", "restate"):
                sent_qty = (last_sent.get("amend_sent") or {}).get("qty") if t == "amend_ack" else None
                # A8: the instruction's total as acknowledged, else as sent; never derived from live fills
                new_total = e["qty"] if e["qty"] is not None else (sent_qty if sent_qty is not None else total)
                new_px = e["px"] if e["px"] is not None else leg["px"]
                unchanged = new_total == total and new_px == leg["px"]
                if unchanged and not (
                    t == "amend_ack"
                    and venue in UNCHANGED_MODIFY_LOSES_PRIORITY
                    and e["amend_keeps_priority"] is not True
                ):
                    continue
                when = _ts_of(
                    e,
                    mode,
                    last_sent.pop("amend_sent", None) if t == "amend_ack" else None,
                    notes,
                    f"{k}#{e['event_seq']}",
                )
                if when is None:
                    exclude("amend_without_time")
                    continue
                when = max(when, leg["ts_submit"])
                keeps = e["amend_keeps_priority"]
                if keeps is None and t == "amend_ack" and venue in keep:
                    keeps = new_total < total and new_px == leg["px"]
                if keeps and new_px == leg["px"] and new_total < total:
                    new_leg_qty = max(new_total, 0)
                    if new_leg_qty < leg["cur"]:
                        amend_rows.append({"shadow_id": leg["shadow_id"], "ts_send": when, "new_qty": new_leg_qty})
                        leg["cur"] = new_leg_qty
                else:
                    leg["ts_cancel"] = when
                    # A8: the new leg carries the instruction total (each arm subtracts its own earlier fills)
                    leg_list.append(
                        {
                            "shadow_id": f"{k}.leg{len(leg_list) + 1}" if new_total > 0 else None,
                            "qty": max(new_total, 0),
                            "px": new_px,
                            "ts_submit": when,
                            "ts_cancel": None if new_total > 0 else when,
                            "cur": max(new_total, 0),
                        }
                    )
                total = new_total
            elif t in TERMINAL:
                sent = last_sent.get("cancel_sent") if t == "cancel_ack" else None
                when = (
                    _ts_of(e, mode, sent, notes, f"{k}#{e['event_seq']}")
                    if (mode == "send")
                    else (e["ts_exchange"] if e["ts_exchange"] is not None else e["ts_local_recv"])
                )
                if when is not None and leg["ts_cancel"] is None:
                    leg["ts_cancel"] = max(when, leg["ts_submit"])
                break
        n_sched += 1
        legs[k] = []
        for leg in leg_list:
            if leg["shadow_id"] is None or leg["qty"] <= 0:
                continue
            legs[k].append(leg["shadow_id"])
            spec_rows.append(
                {
                    "shadow_id": leg["shadow_id"],
                    "instrument_id": iid,
                    "side": ord("B") if o["side"] == "buy" else ord("A"),
                    "price_mode": "abs",
                    "price": leg["px"],
                    "offset_ticks": 0,
                    "qty": leg["qty"],
                    "ts_submit": leg["ts_submit"],
                    "latency_ns": leg_lat,
                    "ts_cancel": leg["ts_cancel"],
                    "group": None,
                    "source": "live_log",
                    "order_key": k,
                }
            )
    w = []
    fb_note = entry_fallback_warning(n_fallback, n_sched)
    if fb_note is not None:
        w.append(fb_note)
    for reason in sorted(excluded):
        w.append(f"excluded {excluded[reason]} order(s): {reason}")
    counts: dict[str, int] = {}
    for n in notes:
        head = n.split(": ", 1)[-1]
        counts[head] = counts.get(head, 0) + 1
    for head in sorted(counts):
        w.append(f"{counts[head]} event(s): {head}")
    amend_rows.sort(key=lambda r: (r["shadow_id"], r["ts_send"]))
    return Schedule(
        spec=_spec_table(spec_rows),
        amends=_amend_table(amend_rows),
        legs=legs,
        n_orders=len(orows),
        n_scheduled=n_sched,
        n_entry_fallback=n_fallback,
        excluded=excluded,
        warnings=w,
        latency_ns=lat_default if entry == "send_plus_latency" or n_fallback else None,
    )


def build_schedule(
    live_dir: str | Path,
    *,
    entry: str = "exchange_ack",
    latency_ns: int | None = None,
    keep_priority_venues: frozenset[str] | set[str] | None = None,
) -> Schedule:
    """Schedule of a canonical live-log directory with counts and warnings (validates the log first)."""
    orders, events = read_log(live_dir)
    errors, _, dup = check_tables(orders, events)
    raise_on_errors(errors, dup, str(live_dir))
    return schedule_from_rows(
        table_rows(orders),
        table_rows(events),
        entry=entry,
        latency_ns=latency_ns,
        keep_priority_venues=keep_priority_venues,
    )


def schedule_report(s: Schedule) -> dict[str, Any]:
    """The schedule's accounting as a plain dict: `{n_orders, n_scheduled, n_entry_fallback, entry_fallback_share,
    excluded, latency_ns, warnings}` (callers that need the counts use `build_schedule`; review L15)."""
    return {
        "n_orders": s.n_orders,
        "n_scheduled": s.n_scheduled,
        "n_entry_fallback": s.n_entry_fallback,
        "entry_fallback_share": s.entry_fallback_share,
        "excluded": dict(sorted(s.excluded.items())),
        "latency_ns": s.latency_ns,
        "warnings": list(s.warnings),
    }


def to_schedule(
    live_dir: str,
    *,
    entry: str,
    latency_ns: int | None,
    keep_priority_venues: frozenset[str] | set[str] | None = None,
) -> tuple[pa.Table, pa.Table]:
    """`qt.shadow_spec` and `qt.shadow_amend` tables of a live log (SPEC 5.7.2); fallbacks and exclusions are
    reported as `LiveScheduleWarning`s only: a caller that needs the counts (the scorer's summary) uses
    `build_schedule`, which returns the same tables with the counts (`schedule_report`)."""
    s = build_schedule(live_dir, entry=entry, latency_ns=latency_ns, keep_priority_venues=keep_priority_venues)
    for msg in s.warnings:
        warnings.warn(msg, LiveScheduleWarning, stacklevel=2)
    return s.spec, s.amends
