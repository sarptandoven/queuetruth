"""Reading back a `pm ingest` output directory, and the own-order legs both `pm calibrate` and `pm score` replay.

`load` reads only what `pm_ingest.json` lists (its `files`; `book_own_free/` and `live/` only when the summary says
they were written), so a file left in the directory by anything else is never read. `resolve_ref` gives the content
identity that the `run_id` of `pm calibrate` and `pm score` hashes for their inputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.pm.book import ASK, BID
from queuetruth.pm.dataset import L2Arrays, feed_gaps, read_l2
from queuetruth.schemas.io import sha256_file
from queuetruth.score._io import Row, read_live_log
from queuetruth.score.l2_own import OwnOrder, own_orders_from_log

#: Key suffix of an own order's re-entry after a size increase (`<order key>#up<k>`, `qt.pm_own_entries`).
REENTRY = "#up"


@dataclass
class Leg:
    """One own order at one price: entry point, instruction amends (priority-keeping totals), end, live fills."""

    key: str
    order_key: str
    instrument_id: int
    side: int
    price: int
    qty: int
    entry_seq: int
    inside: bool
    entry_ts: int
    how: str
    end_ts: int | None
    amends: list[tuple[int, int]] = field(default_factory=list)
    fills: list[tuple[int, int, int]] = field(default_factory=list)  # (ts, qty, px) maker fills
    taker_qty: int = 0
    post_only: bool = False
    #: Where the leg left the queue in the published series (`own_entries`), when it ended by a cancel, an
    #: expiry, a re-price or an increase (`end_ts` set); the replay stops it there rather than by time.
    end_seq: int | None = None
    end_inside: bool = False

    def sim_end(self) -> dict[str, Any]:
        """`SimOrder` keyword arguments of the leg's live end (`ts_end`, and `end_seq` / `end_inside` when placed)."""
        if self.end_ts is None:
            return {"ts_end": None}
        return {"ts_end": self.end_ts, "end_seq": self.end_seq, "end_inside": self.end_inside}


@dataclass
class PmDir:
    """A loaded `pm ingest` directory."""

    root: Path
    summary: dict[str, Any]
    markets: list[dict[str, Any]]
    orders: list[Row]
    events: list[Row]
    entries: list[dict[str, Any]]
    samples: list[dict[str, Any]]
    chain_fills: list[dict[str, Any]]
    chain_queue: list[dict[str, Any]] = field(default_factory=list)

    @property
    def venue(self) -> str:
        """`kalshi` or `polymarket`."""
        return str(self.summary["venue"])

    def market(self, instrument_id: int) -> dict[str, Any]:
        """The `markets.json` entry of an instrument."""
        for m in self.markets:
            if int(m["instrument_id"]) == instrument_id:
                return m
        raise QtError("QT-INPUT-002", f"{self.root}: instrument {instrument_id} is not in markets.json")

    def _sub(self, own_free: bool) -> str:
        return "book_own_free" if own_free and self.summary.get("book_own_free") else "book"

    def book(self, instrument_id: int, own_free: bool = True) -> L2Arrays:
        """The instrument's L2 rows (own-free when the ingest wrote that dataset)."""
        return read_l2(self.root / self._sub(own_free), instrument_id)

    def gaps(self, instrument_id: int, own_free: bool = True) -> list[tuple[int, int]]:
        """Feed gaps of the instrument."""
        return feed_gaps(self.root / self._sub(own_free)).get(instrument_id, [])

    def instrument_ids(self) -> list[int]:
        """Every instrument of the ingest."""
        return [int(i["instrument_id"]) for i in self.summary["instruments"]]


def _json(p: Path) -> dict[str, Any]:
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: not found (run `queuetruth pm ingest` first)")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise QtError("QT-INPUT-002", f"{p}: invalid JSON: {e}") from None


def _parquet_rows(p: Path) -> list[dict[str, Any]]:
    if not p.is_file():
        return []
    t = pq.read_table(p)
    out = []
    for r in t.to_pylist():
        for k, v in r.items():
            if hasattr(v, "value") and hasattr(v, "tz_convert"):
                r[k] = int(v.value)
        out.append(r)
    return out


def load(root: str | Path) -> PmDir:
    """Loads a `pm ingest` output directory (`QT-INPUT-001` when it is not one), reading only the files its summary
    lists."""
    p = Path(root)
    summary = _json(p / "pm_ingest.json")
    if summary.get("schema") != "qt.pm_ingest_summary":
        raise QtError("QT-INPUT-002", f"{p / 'pm_ingest.json'}: not a qt.pm_ingest_summary")
    listed = {str(f.get("name")) for f in summary.get("files") or []}
    markets = _json(p / "markets.json").get("markets", [])
    orders: list[Row] = []
    events: list[Row] = []
    if summary.get("live"):
        orders, events = read_live_log(p / "live")

    def table(name: str) -> list[dict[str, Any]]:
        rel = f"pm/{name}.parquet"
        return _parquet_rows(p / rel) if rel in listed else []

    return PmDir(
        p,
        summary,
        markets,
        orders,
        events,
        table("own_entries"),
        table("queue_samples"),
        table("chain_fills"),
        table("chain_queue"),
    )


def resolve_ref(ref: dict[str, Any]) -> str:
    """Content identity of an input of `pm calibrate` or `pm score` for its `run_id` (SPEC 7.3): a `pm ingest`
    directory by its `run_id` (the ingest spec without paths and its inputs by sha256), a `pm calibrate` output
    (directory or document) by its `run_id`, any other file by its sha256. The directories hold input paths (spec
    echoes, manifests, sample locations), so hashing their files would tie the identity to where the capture was."""
    p = Path(ref["path"])
    docs = [p / "pm_ingest.json", p / "pm_calibration.json"] if p.is_dir() else [p]
    for f in docs:
        if not f.is_file():
            continue
        if f.name in ("pm_ingest.json", "pm_calibration.json"):
            doc = _json(f)
            if doc.get("schema") in ("qt.pm_ingest_summary", "qt.pm_calibration") and doc.get("run_id"):
                return f"{doc['schema']}:{doc['run_id']}"
            raise QtError("QT-INPUT-002", f"{f}: not a pm ingest summary or calibration with a run_id")
        return sha256_file(f)
    raise QtError("QT-INPUT-001", f"{p}: not found")


def base_key(key: str) -> str:
    """The order key of a leg key (`<order key>#leg<k>`, `<order key>#up<k>`)."""
    return key.split("#leg")[0].split(REENTRY)[0]


def legs(d: PmDir) -> list[Leg]:
    """Own-order legs with their entry points (from `own_entries`), instruction amends and live maker fills.

    Built on `queuetruth.score.l2_own.own_orders_from_log` (one record per price): a record's resting-quantity steps
    are split into live fills (a step that a maker fill at the same time accounts for), priority-keeping amends (a
    decrease without a fill; the new instructed total is the leg's live fills so far plus the new resting size) and
    the end (the step to 0 that is not a fill: cancel, expiry or the next leg's re-price). A size increase at the
    same price (an amend up, which loses priority) ends the leg (`how` gets `+increase`) and starts the next one at
    the order's re-entry point (`<key>#up<k>` in `own_entries`), at the back of the queue with the new size."""
    if not d.orders:
        return []
    own, _warn = own_orders_from_log(d.orders, d.events)
    entries = {str(e["order_key"]): e for e in d.entries}
    post = {str(o["order_key"]): bool(o.get("post_only")) for o in d.orders}
    out: list[Leg] = []
    for o in own:
        if o.key not in entries:
            continue
        out += _legs(o, entries, post.get(base_key(o.key), False))
    out.sort(key=lambda g: (g.entry_ts, g.key))
    return out


def _new_leg(o: OwnOrder, key: str, e: dict[str, Any], qty: int, post_only: bool) -> Leg:
    return Leg(
        key=key,
        order_key=base_key(o.key),
        instrument_id=o.instrument_id,
        side=o.side,
        price=o.price,
        qty=qty,
        entry_seq=int(e["seq"]),
        inside=bool(e["inside"]),
        entry_ts=int(e["ts_event"]),
        how=str(e["how"]),
        end_ts=None,
        post_only=post_only,
        end_seq=None if e.get("end_seq") is None else int(e["end_seq"]),
        end_inside=bool(e.get("end_inside")),
    )


def _legs(o: OwnOrder, entries: dict[str, dict[str, Any]], post_only: bool) -> list[Leg]:
    fills = sorted((f.ts, f.qty, f.px) for f in o.fills)
    steps = list(o.resting)
    g = _new_leg(o, o.key, entries[o.key], steps[0][1], post_only)
    out = [g]
    live_filled = 0
    fi = 0
    prev = steps[0][1]
    n_up = 0
    for t, q in steps[1:]:
        used = 0
        while fi < len(fills) and fills[fi][0] <= t:
            if fills[fi][0] == t:
                used += fills[fi][1]
                live_filled += fills[fi][1]
            g.fills.append(fills[fi])
            fi += 1
        rest = (prev - q) - used
        if rest < 0:
            # a size increase loses priority: this leg ends; the order rejoins at the back of the queue
            g.end_ts = t
            g.how = g.how + "+increase"
            n_up += 1
            e = entries.get(f"{o.key}{REENTRY}{n_up}")
            if e is None:  # no re-entry point was placed (an ingest from before re-entries): the history ends
                break
            g = _new_leg(o, f"{o.key}{REENTRY}{n_up}", e, q, post_only)
            out.append(g)
            live_filled = 0
            prev = q
            continue
        if rest > 0 and q == 0:
            g.end_ts = t
            break
        if rest > 0:
            g.amends.append((t, live_filled + q))
        prev = q
    g.fills.extend(fills[fi:])
    return out


def side_name(side: int) -> str:
    """`bid` / `ask`."""
    return "bid" if side == BID else "ask" if side == ASK else "none"


__all__ = ["REENTRY", "Leg", "PmDir", "base_key", "legs", "load", "resolve_ref", "side_name"]
