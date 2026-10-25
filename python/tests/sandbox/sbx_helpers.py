"""Test-local builders for the sandbox tests: hand-made MBO datasets (`Tape`), the committed fixtures, a seeded
random agent, an independent prefix builder and an independent replay of a session's committed orders.

Everything here is independent of `queuetruth.sandbox`'s own implementation except where a test says so: the prefix
of a partition is cut with plain pyarrow, and the reference replay is `queuetruth.replay` on the original dataset
directory (all partitions), never the sandbox's tape or certification view.
"""

from __future__ import annotations

import json
import os
import random
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import queuetruth as qt
from queuetruth.errors import QtError
from queuetruth.schemas.io import write_table
from queuetruth.score._io import make_table
from queuetruth.score._io import rows as table_rows

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "fixtures"
SYNTH = FIXTURES / "synth_small"
SYNTH_NASDAQ = FIXTURES / "synth_small_nasdaq"
S = 1_000_000_000
MS = 1_000_000
MIDNIGHT = 1_767_225_600_000_000_000  # 2026-01-01 00:00:00 UTC
DAY0 = MIDNIGHT // (86_400 * S)
TICK = 10_000_000  # 0.01 at price_exp -9
PX = 100 * 1_000_000_000  # 100.00
LAST = 128


def px(ticks: int) -> int:
    """100.00 + ticks * 0.01 as a raw price."""
    return PX + ticks * TICK


@dataclass
class Tape:
    """Builder of one partition's MBO events (records in feed order; `F_LAST` on each event's last record)."""

    instrument_id: int = 1
    events: list[list[dict[str, Any]]] = field(default_factory=list)
    prints: list[dict[str, Any]] = field(default_factory=list)
    resting: dict[int, tuple[str, int, int]] = field(default_factory=dict)
    next_id: int = 1000
    seq: int = 0

    def _rec(
        self, ts: int, action: str, side: str, price: int, size: int, oid: int, *, same_seq: bool = False
    ) -> dict[str, Any]:
        if not same_seq:
            self.seq += 1
        return {
            "ts_event": ts,
            "ts_recv": ts,
            "instrument_id": self.instrument_id,
            "action": ord(action),
            "side": ord(side),
            "price": price,
            "size": size,
            "order_id": oid,
            "flags": 0,
            "sequence": self.seq,
            "channel_id": 0,
            "publisher_id": 0,
        }

    def event(self, recs: list[dict[str, Any]]) -> None:
        """Appends one event (sets `F_LAST` on its last record)."""
        recs[-1]["flags"] |= LAST
        self.events.append(recs)

    def add(self, ts: int, side: str, price: int, size: int, oid: int | None = None) -> int:
        """One `A` event; returns the order id."""
        if oid is None:
            self.next_id += 1
            oid = self.next_id
        self.resting[oid] = (side, price, size)
        self.event([self._rec(ts, "A", side, price, size, oid)])
        return oid

    def cancel(self, ts: int, oid: int, size: int | None = None) -> None:
        """One `C` event (whole order by default)."""
        side, price, rest = self.resting[oid]
        q = rest if size is None else size
        self.resting[oid] = (side, price, rest - q)
        self.event([self._rec(ts, "C", side, price, q, oid)])

    def modify(self, ts: int, oid: int, price: int, size: int) -> None:
        """One `M` event."""
        side, _, _ = self.resting[oid]
        self.resting[oid] = (side, price, size)
        self.event([self._rec(ts, "M", side, price, size, oid)])

    def trade(self, ts: int, aggressor: str, fills: list[tuple[int, int]]) -> None:
        """An aggressive order executing against resting orders `[(order_id, size)]` in one event: per price level a
        `T` print (aggressor side, the level's executed size), then per resting order an `F` and the `C` that removes
        the executed size, sharing one sequence number (the Nasdaq pairing rule; DBN pairs within the event)."""
        recs: list[dict[str, Any]] = []
        i = 0
        while i < len(fills):
            price = self.resting[fills[i][0]][1]
            group = []
            while i < len(fills) and self.resting[fills[i][0]][1] == price:
                group.append(fills[i])
                i += 1
            recs.append(self._rec(ts, "T", aggressor, price, sum(q for _, q in group), 0))
            self.prints.append(
                {"ts": ts, "price": price, "size": recs[-1]["size"], "seq": self.seq, "kind": "displayed"}
            )
            for oid, q in group:
                side, p, rest = self.resting[oid]
                recs.append(self._rec(ts, "F", side, p, q, oid))
                recs.append(self._rec(ts, "C", side, p, q, oid, same_seq=True))
                self.resting[oid] = (side, p, rest - q)
        self.event(recs)

    def hidden_print(self, ts: int, price: int, size: int, *, cross: bool = False) -> None:
        """A side-`N` print (hidden execution, or an ITCH cross when `cross`)."""
        self.event([self._rec(ts, "T", "N", price, size, 0)])
        self.prints.append(
            {"ts": ts, "price": price, "size": size, "seq": self.seq, "kind": "cross" if cross else "hidden"}
        )

    def clear(self, ts: int) -> None:
        """An `R` event (book clear)."""
        self.resting.clear()
        self.event([self._rec(ts, "R", "N", 2**63 - 1, 0, 0)])

    def noop(self, ts: int) -> None:
        """An event that changes nothing (`N` record): a clock tick in the data."""
        self.event([self._rec(ts, "N", "N", 2**63 - 1, 0, 0)])

    def records(self) -> list[dict[str, Any]]:
        """All records in feed order."""
        return [r for ev in self.events for r in ev]


def write_dataset(
    root: Path,
    tape: Tape,
    *,
    venue: str = "cme",
    tick: int = TICK,
    lot: int = 1,
    multiplier: float = 1.0,
    open_s: int = 0,
    close_s: int = 86_400,
    feed_gaps: list[dict[str, Any]] | None = None,
    with_prints: bool = True,
    symbol: str = "TST",
    size_exp: int = 0,
) -> Path:
    """A dataset directory (SPEC 3.3) holding `tape` as its only partition."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    iid = tape.instrument_id
    inst = [
        {
            "instrument_id": iid,
            "session_date": DAY0,
            "symbol": symbol,
            "venue": venue if venue in ("nasdaq", "cme", "coinbase", "generic") else "generic",
            "source_venue": venue,
            "price_exp": -9,
            "tick_size": tick,
            "lot_size": lot,
            "size_exp": size_exp,
            "contract_multiplier": multiplier,
            "currency": "USD",
            "session_tz": "UTC",
            "midnight_utc_ns": MIDNIGHT,
            "open_s": open_s,
            "close_s": close_s,
            "source": "test",
        }
    ]
    write_table(make_table("qt.instruments", inst), root / "instruments.parquet")
    recs = tape.records()
    rel = f"mbo/date=2026-01-01/instrument_id={iid}/part-00000.parquet"
    fe = write_table(make_table("qt.mbo", recs), root / rel)
    if with_prints and tape.prints:
        rows = [
            {
                "ts_event": p["ts"],
                "instrument_id": iid,
                "sequence": p["seq"],
                "price": p["price"],
                "size": p["size"],
                "aggressor_side": ord("N"),
                "kind": p["kind"],
                "maker_order_id": None,
            }
            for p in tape.prints
        ]
        write_table(
            make_table("qt.trades", rows),
            root / f"trades_source/date=2026-01-01/instrument_id={iid}/part-00000.parquet",
        )
    dq = {
        "schema": "qt.data_quality",
        "schema_version": 1,
        "records": len(recs),
        "maybe_bad_book_records": 0,
        "bad_ts_recv_records": 0,
        "snapshot_records": 0,
        "pre_snapshot_rows_skipped": 0,
        "unknown_order_refs": 0,
        "duplicate_adds": 0,
        "unexpected_modifies": 0,
        "sequence_gaps": 0,
        "sequence_regressions": 0,
        "out_of_order_ts_event": 0,
        "crossed_books_after_last": 0,
        "hidden_prints": 0,
        "off_price_executions": 0,
        "feed_gaps": feed_gaps or [],
        "source_specific": {},
        "verdict": "clean",
        "reasons": [],
    }
    (root / "data_quality.json").write_text(json.dumps(dq, indent=2) + "\n")
    man = {
        "schema": "qt.dataset_manifest",
        "schema_version": 1,
        "dataset_id": fe["sha256"],
        "kind": "mbo",
        "source_format": "test",
        "venue": venue,
        "source_venue": venue,
        "inputs": [],
        "partitions": [
            {
                "instrument_id": iid,
                "session_date": DAY0,
                "files": [{"path": rel, "rows": fe["rows"], "sha256": fe["sha256"]}],
                "rows": fe["rows"],
                "ts_min": recs[0]["ts_event"],
                "ts_max": recs[-1]["ts_event"],
            }
        ],
        "price_exp_by_instrument": {str(iid): -9},
        "created_by": "test",
        "validation_status": "fixture_only",
        "rule_set": None,
        "source_vintage": None,
    }
    (root / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return root


def realdata_dataset() -> Path | None:
    """The day-one Nasdaq dataset (`QT_REALDATA_DS`, else `.realdata/ds/nasdaq-2019-12-30` of the repository), or
    None when it is not there."""
    for cand in (os.environ.get("QT_REALDATA_DS"), REPO / ".realdata" / "ds" / "nasdaq-2019-12-30"):
        if cand and (Path(cand) / "manifest.json").exists():
            return Path(cand)
    return None


def at(seconds: float) -> int:
    """Absolute ns of a time of day (seconds after midnight) of the test day."""
    return MIDNIGHT + round(seconds * S)


def base_spec(dataset: Path, **kw: Any) -> dict[str, Any]:
    """A sandbox spec on a hand-made dataset (instrument 1)."""
    return {"dataset": {"path": str(dataset)}, "instrument_id": 1, **kw}


# ---- partitions and prefixes, independent of the sandbox ---------------------------------------------------------


def partition_files(dataset: Path, instrument_id: int, session_date: int) -> list[Path]:
    """The partition's `qt.mbo` files from manifest.json."""
    man = json.loads((dataset / "manifest.json").read_text())
    p = next(x for x in man["partitions"] if x["instrument_id"] == instrument_id and x["session_date"] == session_date)
    return [dataset / f["path"] for f in p["files"]]


def prefix_rows(dataset: Path, instrument_id: int, session_date: int, cut: int) -> int:
    """Records in the cut at `cut` (complete events whose records all have ts <= cut; the whole tail at the end of
    the data), computed with plain numpy from the partition files."""
    t = pa.concat_tables(
        [pq.read_table(f, columns=["ts_event", "flags"]) for f in partition_files(dataset, instrument_id, session_date)]
    )
    ts = t.column("ts_event").cast(pa.int64()).to_numpy()
    fl = t.column("flags").to_numpy()
    over = np.flatnonzero(ts > cut)
    if not over.size:
        return len(ts)
    lasts = np.flatnonzero(fl[: over[0]] & LAST)
    return int(lasts[-1]) + 1 if lasts.size else 0


def write_prefix(dataset: Path, out: Path, instrument_id: int, session_date: int, cut: int) -> Path:
    """An independent prefix dataset of one partition at `cut` (plain pyarrow; source prints cut the same way). The
    source `data_quality.json` is copied unchanged: the engine acts on a feed gap only at the first event after its
    start, so a gap that starts after the prefix's last event cannot act on it, and nothing here restates the
    sandbox's own rule for gaps."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    n = prefix_rows(dataset, instrument_id, session_date, cut)
    t = pa.concat_tables([pq.read_table(f) for f in partition_files(dataset, instrument_id, session_date)]).slice(0, n)
    man = json.loads((dataset / "manifest.json").read_text())
    part = next(
        x for x in man["partitions"] if x["instrument_id"] == instrument_id and x["session_date"] == session_date
    )
    rel = part["files"][0]["path"]
    fe = write_table(t, out / rel, qt.schemas.arrow.SCHEMAS["qt.mbo"]) if n else None
    ts = t.column("ts_event").cast(pa.int64()).to_numpy() if n else np.zeros(0, dtype=np.int64)
    part = dict(part)
    part["files"] = [{"path": rel, "rows": n, "sha256": fe["sha256"]}] if fe else []
    part["rows"], part["ts_min"], part["ts_max"] = n, int(ts.min()) if n else 0, int(ts.max()) if n else 0
    man["partitions"] = [part] if n else []
    (out / "manifest.json").write_text(json.dumps(man))
    shutil.copy2(dataset / "instruments.parquet", out / "instruments.parquet")
    date = rel.split("/")[1]
    tdir = dataset / "trades_source" / date / f"instrument_id={instrument_id}"
    if tdir.is_dir():
        tr = pa.concat_tables([pq.read_table(f) for f in sorted(tdir.glob("part-*.parquet"))])
        tts = tr.column("ts_event").cast(pa.int64()).to_numpy()
        k = int(np.flatnonzero(tts > cut)[0]) if (tts > cut).any() else len(tts)
        dst = out / "trades_source" / date / f"instrument_id={instrument_id}"
        dst.mkdir(parents=True)
        write_table(tr.slice(0, k), dst / "part-00000.parquet", qt.schemas.arrow.SCHEMAS["qt.trades"])
    dq_path = dataset / "data_quality.json"
    if dq_path.exists():
        shutil.copy2(dq_path, out / "data_quality.json")
    return out


# ---- the committed schedule, rebuilt from the log alone ----------------------------------------------------------


def schedule_from_log(log: dict[str, Any], instrument_id: int) -> tuple[pa.Table, pa.Table, dict[str, dict[str, Any]]]:
    """`qt.shadow_spec` and `qt.shadow_amend` of every accepted order of a committed log, built from the log entries
    with the documented mapping (abs or touch price, latency, IOC cancel at ts_submit + 1 ns, cancels, amends), and
    each order's request."""
    orders: dict[str, dict[str, Any]] = {}
    amends = []
    for e in log["entries"]:
        d = e["data"]
        if e["kind"] == "submit":
            r = d["request"]
            ioc = r["type"] == "market" or r["tif"] == "ioc"
            orders[d["order_id"]] = {
                "shadow_id": d["order_id"],
                "instrument_id": instrument_id,
                "side": ord("B") if r["side"] == "buy" else ord("A"),
                "price_mode": "touch" if r["peg_offset_ticks"] is not None else "abs",
                "price": d["price"],
                "offset_ticks": r["peg_offset_ticks"] or 0,
                "qty": r["qty"],
                "ts_submit": e["clock_ns"],
                "latency_ns": d["latency_ns"],
                "ts_cancel": e["clock_ns"] + 1 if ioc else None,
                "group": None,
                "source": "user",
                "order_key": None,
                "_ioc": ioc,
            }
        elif e["kind"] == "cancel":
            if d["new_qty"] is None:
                orders[d["order_id"]]["ts_cancel"] = e["clock_ns"]
            else:
                amends.append({"shadow_id": d["order_id"], "ts_send": e["clock_ns"], "new_qty": d["new_qty"]})
    rows = [{k: v for k, v in o.items() if k != "_ioc"} for o in orders.values()]
    return make_table("qt.shadow_spec", rows), make_table("qt.shadow_amend", amends), orders


def reference_replay(dataset: Path, log: dict[str, Any], instrument_id: int, out: Path, **spec: Any) -> dict[str, Any]:
    """`queuetruth.replay` of the log's orders on the original dataset directory (every partition); returns results by
    shadow id and fills by shadow id (IOC orders: arrival fills only)."""
    shadows, amends, orders = schedule_from_log(log, instrument_id)
    out.mkdir(parents=True, exist_ok=True)
    write_table(shadows, out / "shadows.parquet")
    rs: dict[str, Any] = {
        "dataset": {"path": str(dataset)},
        "out": str(out / "run"),
        "shadows": {"path": str(out / "shadows.parquet")},
    }
    if amends.num_rows:
        write_table(amends, out / "amends.parquet")
        rs["amends"] = {"path": str(out / "amends.parquet")}
    rs.update(spec)
    qt.replay(rs)
    res = {r["shadow_id"]: r for r in table_rows(pq.read_table(out / "run" / "shadow_results.parquet"))}
    fills: dict[str, list[dict[str, Any]]] = {}
    for r in table_rows(pq.read_table(out / "run" / "shadow_fills.parquet")):
        if orders[r["shadow_id"]]["_ioc"] and r["reason"] != "crossed_at_live":
            continue
        fills.setdefault(r["shadow_id"], []).append(r)
    return {"results": res, "fills": fills, "orders": orders}


# ---- a seeded random agent ---------------------------------------------------------------------------------------


def random_agent(sb: Any, rng: random.Random, steps: int, *, step_ns: tuple[int, int] = (200 * MS, 8 * S)) -> list[Any]:
    """Drives a session with random but valid-looking instructions decided from what it observes: passive and
    marketable limit orders, pegged orders, IOC and market orders, full and partial cancels. Rejections are kept (they
    are part of the log). Returns every observation it received."""
    tick = sb.instrument["tick_size"]
    seen = [sb.observe()]
    for _ in range(steps):
        obs = seen[-1]
        if obs.done:
            break
        bid, ask = obs.book.bid, obs.book.ask
        for _ in range(rng.randint(0, 3)):
            kind = rng.random()
            side = rng.choice(["buy", "sell"])
            qty = rng.randint(1, 5)
            try:
                if kind < 0.45 and bid and ask:
                    ref = bid[0] if side == "buy" else ask[0]
                    off = rng.choice([0, 0, -1, -2, 1])
                    p = ref + off * tick if side == "buy" else ref - off * tick
                    sb.submit(side, qty, price=p)
                elif kind < 0.6:
                    sb.submit(side, qty, peg_offset_ticks=rng.choice([0, -1, 1]))
                elif kind < 0.7 and bid and ask:
                    p = (ask[0] + tick) if side == "buy" else (bid[0] - tick)
                    sb.submit(side, qty, price=p, tif=rng.choice(["ioc", "day"]))
                elif kind < 0.78:
                    sb.submit(side, qty, type="market")
                else:
                    open_ = [o for o in obs.orders if o.status in ("pending", "working") and o.tif == "day"]
                    if open_:
                        o = rng.choice(open_)
                        if o.qty > 1 and rng.random() < 0.4:
                            sb.cancel(o.order_id, qty=1)
                        else:
                            sb.cancel(o.order_id)
            except QtError:
                pass
        seen.append(sb.advance(by=rng.randint(*step_ns)))
    return seen


__all__ = [
    "DAY0",
    "FIXTURES",
    "MIDNIGHT",
    "MS",
    "REPO",
    "SYNTH",
    "SYNTH_NASDAQ",
    "TICK",
    "S",
    "Tape",
    "at",
    "base_spec",
    "partition_files",
    "prefix_rows",
    "px",
    "random_agent",
    "realdata_dataset",
    "reference_replay",
    "schedule_from_log",
    "write_dataset",
    "write_prefix",
]
