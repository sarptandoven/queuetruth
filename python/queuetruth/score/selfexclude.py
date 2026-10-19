"""Self-exclusion of the user's own live orders from the MBO tape (audit ruling A1).

On an MBO venue the live order is itself in the data; replaying its shadow on the full tape would place the shadow
behind its own twin. Each scheduled live order is matched to its MBO order id within its own instrument-day
(the partition holding the order's `session_date`, else the one whose time range holds its entry time; order ids
such as ITCH references restart daily, review F7):
1. by venue order id (`venue_order_id` parsed as an integer equal to the `order_id` of an `A` or `M` record of
   that instrument-day with the order's side, review F7): the record closest in time to the entry, accepted when
   it has the order's price (a modify keeps the venue order id, so an order entered at its re-priced level matches
   its `M`) or, at another price, when it is within `match_window_ns` of the entry: a display-slid order (Nasdaq
   price-to-comply / display-price sliding) rests at a displayed price other than its limit and executes at its
   limit. Such a twin's displayed price is kept (`TwinMatch.display_px`): the order's engine leg rests there
   (`twin_display_prices`), where the order queued, and its executions at the limit are its shadow's fills;
2. else heuristically: the unmatched `A` record of the instrument with the same side, price and size whose
   `ts_event` is closest to the order's entry time within `match_window_ns` (default 1 ms).
Matches are kept per instrument (`TwinMatch.ids`) and per instrument-day (`TwinMatch.days`); the exact arm's
`exclude_order_ids` items carry the session date (`exclusion_items`), so an order id that repeats on another day
(ITCH order references restart daily) is excluded on its own day only. An excluded id that also names an `A` record
on another session date of the instrument is a collision (`TwinMatch.collisions`, warned: an undated exclusion,
e.g. the zoo arm's, would remove that other order too); the filtered copy (`filtered_dataset(...,
by_day=TwinMatch.days)`) is day-scoped.
The matched ids are removed from the replayed stream: their `A`/`C`/`M`/`F` records are dropped (the `T` prints stay,
so the twin's executions become unattributed volume at the price, ahead of nothing the shadow does not already
see). When a dropped record carried `F_LAST`, the flag moves to the previous kept record of the same event, so
event atomicity is preserved, and sequence numbers are closed over fully dropped events (no false feed gaps).

The exact arm passes the ids to the engine (`queuetruth.api.replay(..., exclude_order_ids=...)`). The engine links
each excluded order to the shadow entering at its `A` / `M` record (same instrument, side and price, `ts_live` equal
to the record's time, or 1 ns before it after `align_to_twins`; the first such shadow in schedule order, an equal
quantity first): the twin stays on the tape for every other shadow, and its own shadow activates in its queue place
and is filled by each of its executions at the execution's price. Legs that the engine cannot tell apart (same
instrument, entry time, side, price and quantity) are put in their twins' tape order (`order_twin_ties`), so each
links to its own twin; the filtered copy cannot express that (under the exact fill rule the
twin's executed volume is lost and the arm underfills), so it is only a fallback for runners without native options
and for `calibrate-live` (the zoo arm takes `ZooSpec.exclude_order_ids` natively; L2 models read prints, which the
copy keeps).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.score._io import Row

LAST = 1 << 7
ACT_A = ord("A")
ACT_M = ord("M")
REMOVABLE = (ord("A"), ord("C"), ord("M"), ord("F"))
DEFAULT_WINDOW_NS = 1_000_000


@dataclass
class TwinMatch:
    """Matched MBO order ids per instrument and the match counts."""

    ids: dict[int, set[int]] = field(default_factory=dict)
    by_key: dict[str, tuple[int, int]] = field(default_factory=dict)
    by_id: int = 0
    by_heuristic: int = 0
    unmatched: int = 0
    #: (instrument_id, session_date) -> matched ids of that instrument-day (session_date 0 when unknown).
    days: dict[tuple[int, int], set[int]] = field(default_factory=dict)
    #: instrument_id -> excluded ids that also name an `A` record on another session date of the instrument.
    collisions: dict[int, set[int]] = field(default_factory=dict)
    #: order_key -> displayed price of a twin matched by id at another price than the order's (display-slid).
    display_px: dict[str, int] = field(default_factory=dict)

    @property
    def n(self) -> int:
        """Orders considered."""
        return self.by_id + self.by_heuristic + self.unmatched

    @property
    def rate(self) -> float | None:
        """Match rate (None with no orders)."""
        return None if self.n == 0 else (self.by_id + self.by_heuristic) / self.n

    def all_ids(self) -> list[int]:
        """Every excluded id, sorted."""
        return sorted({i for v in self.ids.values() for i in v})

    def collision_warning(self) -> str | None:
        """Warning when an excluded id also occurs on another session date (the native exclusion has no date)."""
        if not self.collisions:
            return None
        n = sum(len(v) for v in self.collisions.values())
        ex = ", ".join(f"{i}:{o}" for i in sorted(self.collisions) for o in sorted(self.collisions[i])[:5])
        return (
            f"self_exclusion: {n} excluded order id(s) also occur on other session dates of the instrument ({ex}); "
            "exclude_order_ids has no session date, so the engine removes those orders there too"
        )

    def summary(self) -> str:
        """Warning text with the match rate."""
        r = self.rate
        pct = "n/a" if r is None else f"{100 * r:.1f}%"
        return (
            f"self_exclusion: matched {self.by_id + self.by_heuristic}/{self.n} live orders to MBO order ids "
            f"({pct}; by id {self.by_id}, heuristic {self.by_heuristic})"
        )


def _partition_files(root: Path, manifest: dict[str, Any]) -> list[tuple[int, int, list[Path]]]:
    out = []
    for p in manifest.get("partitions", []) or []:
        files = [root / f["path"] for f in p.get("files", [])]
        out.append((int(p["instrument_id"]), int(p["session_date"]), files))
    if not out:
        for f in sorted((root / "mbo").rglob("*.parquet")):
            iid = next((int(x.split("=", 1)[1]) for x in f.parts if x.startswith("instrument_id=")), 0)
            out.append((iid, 0, [f]))
    return out


def _adds(files: list[Path]) -> pa.Table:
    t = (
        pa.concat_tables(
            [pq.read_table(f, columns=["ts_event", "action", "side", "price", "size", "order_id"]) for f in files]
        )
        if files
        else None
    )
    if t is None:
        return pa.table({})
    t = t.filter(pc.is_in(t["action"], pa.array([ACT_A, ACT_M], t["action"].type)))
    return t.set_column(0, "ts_event", t["ts_event"].cast(pa.int64()))


def _side_byte(side: Any) -> int:
    if isinstance(side, int):
        return side
    return ord("B") if str(side).lower() in ("buy", "b", str(ord("B"))) else ord("A")


def match_twins(dataset_root: str | Path, orders: list[Row], window_ns: int = DEFAULT_WINDOW_NS) -> TwinMatch:
    """Matches scheduled live orders (`instrument_id`, `side` B/A byte or buy/sell, `price`, `qty`, `ts_entry`,
    optional `venue_order_id`, optional `session_date` days) to MBO order ids of the dataset (module docstring)."""
    root = Path(dataset_root)
    manifest = (
        json.loads((root / "manifest.json").read_text(encoding="utf-8")) if (root / "manifest.json").exists() else {}
    )
    parts: dict[int, list[tuple[int, list[Path], int | None, int | None]]] = {}
    ranges = {
        (int(p["instrument_id"]), int(p["session_date"])): (p.get("ts_min"), p.get("ts_max"))
        for p in manifest.get("partitions", []) or []
        if "instrument_id" in p and "session_date" in p
    }
    for iid, sd, fs in _partition_files(root, manifest):
        lo, hi = ranges.get((iid, sd), (None, None))
        parts.setdefault(iid, []).append((sd, fs, None if lo is None else int(lo), None if hi is None else int(hi)))
    res = TwinMatch()
    cache: dict[int, dict[str, np.ndarray]] = {}
    used: dict[int, set[int]] = {}
    for o in sorted(orders, key=lambda r: (int(r["ts_entry"]), str(r.get("order_key", "")))):
        iid = int(o["instrument_id"])
        if iid not in cache:
            tabs = []
            for sd, fs, _, _ in parts.get(iid, []):
                t = _adds(fs)
                if t.num_columns:
                    tabs.append(t.append_column("session_date", pa.array([sd] * t.num_rows, pa.int64())))
            t = pa.concat_tables(tabs) if tabs else pa.table({})
            cache[iid] = (
                {k: np.asarray(t[k].to_numpy(zero_copy_only=False)) for k in t.column_names} if t.num_columns else {}
            )
        a = cache[iid]
        taken = used.setdefault(iid, set())
        t_entry = int(o["ts_entry"])
        sb = _side_byte(o["side"])
        day = o.get("session_date")
        if day is not None and not any(sd == int(day) for sd, _, _, _ in parts.get(iid, [])):
            day = None  # a date convention the partitions do not use: locate the partition by time instead
        if day is None:
            day = next(
                (
                    sd
                    for sd, _, lo, hi in parts.get(iid, [])
                    if lo is not None and hi is not None and lo <= t_entry <= hi
                ),
                None,
            )
        vid = o.get("venue_order_id")
        oid: int | None = None
        oid_day: int | None = None
        if vid is not None and a:
            try:
                cand = int(str(vid))
            except ValueError:
                cand = None
            if cand is not None and 0 <= cand < 2**64 and cand not in taken:
                m = (a["order_id"] == np.uint64(cand)) & (a["side"] == sb)
                if day is not None:
                    m &= a["session_date"] == int(day)
                hits = np.flatnonzero(m)
                px = int(o["price"])
                # same price first, then the closest in time
                ranked = sorted(hits, key=lambda i: (int(a["price"][i]) != px, abs(int(a["ts_event"][i]) - t_entry), i))
                best = next(
                    (
                        i
                        for i in ranked
                        if int(a["price"][i]) == px or abs(int(a["ts_event"][i]) - t_entry) <= window_ns
                    ),
                    None,
                )
                if best is not None:
                    oid, oid_day = cand, int(a["session_date"][best])
                    res.by_id += 1
                    if int(a["price"][best]) != px and o.get("order_key") is not None:
                        res.display_px[str(o["order_key"])] = int(a["price"][best])
        if oid is None and a:
            m = (a["action"] == ACT_A) & (a["side"] == sb) & (a["price"] == int(o["price"]))
            m &= a["size"] == int(o["qty"])
            m &= np.abs(a["ts_event"] - t_entry) <= window_ns
            idx = [i for i in np.flatnonzero(m) if int(a["order_id"][i]) not in taken]
            if idx:
                best = min(idx, key=lambda i: (abs(int(a["ts_event"][i]) - t_entry), i))
                oid, oid_day = int(a["order_id"][best]), int(a["session_date"][best])
                res.by_heuristic += 1
        if oid is None:
            res.unmatched += 1
            continue
        taken.add(oid)
        res.ids.setdefault(iid, set()).add(oid)
        res.days.setdefault((iid, int(oid_day or 0)), set()).add(oid)
        other = (a["order_id"] == np.uint64(oid)) & (a["action"] == ACT_A)
        if np.any(other & (a["session_date"] != int(oid_day or 0))):
            res.collisions.setdefault(iid, set()).add(oid)
        if o.get("order_key") is not None:
            res.by_key[str(o["order_key"])] = (iid, oid)
    return res


def twin_entry_points(dataset_root: str | Path, ids: dict[int, set[int]]) -> dict[tuple[int, int], dict[int, bool]]:
    """For each matched twin `(instrument_id, order_id)`: the `ts_event` of its `A`/`M` records (where it took a
    queue position) -> whether that record is the first record of its partition with that `ts_event` (no earlier
    record shares the timestamp)."""
    root = Path(dataset_root)
    manifest = (
        json.loads((root / "manifest.json").read_text(encoding="utf-8")) if (root / "manifest.json").exists() else {}
    )
    out: dict[tuple[int, int], dict[int, bool]] = {}
    for iid, _, fs in _partition_files(root, manifest):
        want = ids.get(iid)
        if not want or not fs:
            continue
        t = pa.concat_tables([pq.read_table(f, columns=["ts_event", "action", "order_id"]) for f in fs])
        ts = np.asarray(t["ts_event"].cast(pa.int64()).to_numpy())
        act = np.asarray(t["action"].to_numpy())
        oid = np.asarray(t["order_id"].to_numpy())
        hit = np.isin(oid, np.array(sorted(want), dtype=oid.dtype)) & np.isin(act, (ord("A"), ord("M")))
        for i in np.flatnonzero(hit):
            first = i == 0 or int(ts[i - 1]) < int(ts[i])
            out.setdefault((iid, int(oid[i])), {})[int(ts[i])] = bool(first)
    return out


def align_to_twins(
    schedule: list[Row], twins: TwinMatch, points: dict[tuple[int, int], dict[int, bool]]
) -> tuple[list[Row], int]:
    """Engine copy of the schedule where each leg that starts exactly at its twin's `A`/`M` record (exchange-ack
    entry) activates in the twin's place (ruling A1): activation is strict (first event with `ts_event > ts_live`,
    SPEC 5.2.1), so a leg at the twin's timestamp would join after every event sharing it, e.g. after a sweep in the
    same nanosecond that executed the twin. When the twin's record is the first of its timestamp, `ts_submit` moves
    1 ns earlier so the shadow activates at the start of that event; otherwise it is left as is (conservative).
    Returns (rows, number of legs moved)."""
    out, n = [], 0
    for r in schedule:
        tw = twins.by_key.get(str(r.get("order_key") or r["shadow_id"]))
        t = int(r["ts_submit"])
        if tw is not None and points.get(tw, {}).get(t) is True and int(r.get("latency_ns") or 0) == 0:
            r = {**r, "ts_submit": t - 1}
            n += 1
        out.append(r)
    return out, n


def twin_display_prices(schedule: list[Row], twins: TwinMatch) -> tuple[list[Row], int]:
    """Engine copy of the schedule in which the first leg of an order whose twin was matched at another displayed
    price (`TwinMatch.display_px`, a display-slid order) rests at that displayed price: the order queued there, so
    its shadow takes the twin's place there (and links to it), while its executions at the limit fill the shadow.
    Returns (rows, legs moved)."""
    if not twins.display_px:
        return schedule, 0
    first: dict[str, tuple[int, int]] = {}
    for i, r in enumerate(schedule):
        k = str(r.get("order_key") or r["shadow_id"])
        if k in twins.display_px and (k not in first or int(r["ts_submit"]) < first[k][0]):
            first[k] = (int(r["ts_submit"]), i)
    out, n = list(schedule), 0
    for k, (_, i) in first.items():
        px = twins.display_px[k]
        if out[i].get("price") != px:
            out[i] = {**out[i], "price": px, "price_mode": "abs"}
            n += 1
    return out, n


def order_twin_ties(dataset_root: str | Path, schedule: list[Row], twins: TwinMatch) -> tuple[list[Row], int]:
    """Engine copy of the schedule in which legs with equal (instrument_id, ts_submit, side, price, qty) and matched
    twins keep their positions as a group but are ordered by their twins' first `A` / `M` record on the tape: the
    engine links a twin to the first candidate shadow in schedule order (`selfexclude` module doc), so identical
    legs entered in the same nanosecond (e.g. the opening-cross release) each get their own twin. Returns (rows,
    number of legs in such groups)."""
    groups: dict[tuple, list[int]] = {}
    for i, r in enumerate(schedule):
        tw = twins.by_key.get(str(r.get("order_key") or r["shadow_id"]))
        if tw is None:
            continue
        k = (int(r["instrument_id"]), int(r["ts_submit"]), int(r["side"]), r.get("price"), int(r["qty"]))
        groups.setdefault(k, []).append(i)
    ties = [g for g in groups.values() if len(g) > 1]
    if not ties:
        return schedule, 0
    want: dict[int, set[int]] = {}
    for g in ties:
        for i in g:
            iid, oid = twins.by_key[str(schedule[i].get("order_key") or schedule[i]["shadow_id"])]
            want.setdefault(iid, set()).add(oid)
    root = Path(dataset_root)
    manifest = (
        json.loads((root / "manifest.json").read_text(encoding="utf-8")) if (root / "manifest.json").exists() else {}
    )
    pos: dict[tuple[int, int], tuple[int, int]] = {}
    for iid, sd, fs in _partition_files(root, manifest):
        ids = want.get(iid)
        if not ids or not fs:
            continue
        t = pa.concat_tables([pq.read_table(f, columns=["action", "order_id"]) for f in fs])
        act = np.asarray(t["action"].to_numpy())
        oid = np.asarray(t["order_id"].to_numpy())
        hit = np.isin(oid, np.array(sorted(ids), dtype=oid.dtype)) & np.isin(act, (ACT_A, ACT_M))
        for i in np.flatnonzero(hit):
            pos.setdefault((iid, int(oid[i])), (sd, int(i)))
    out = list(schedule)
    for g in ties:
        rows = [schedule[i] for i in g]
        rows.sort(key=lambda r: pos.get(twins.by_key[str(r.get("order_key") or r["shadow_id"])], (2**62, 2**62)))
        for i, r in zip(g, rows, strict=True):
            out[i] = r
    return out, sum(len(g) for g in ties)


def filter_mbo(table: pa.Table, exclude: set[int]) -> pa.Table:
    """Drops `A`/`C`/`M`/`F` records of `exclude` ids, moving a dropped `F_LAST` to the previous kept record of the
    same event. Events whose every record is dropped would leave a hole in the `sequence` numbers, which the engines
    read as a feed gap (every live shadow of the instrument ends `censored_gap`); so the kept records of each
    `channel_id` are renumbered `sequence - #(fully dropped sequence values below it)`, which closes exactly those
    holes and keeps every gap of the original tape."""
    if not exclude or table.num_rows == 0:
        return table
    oid = table["order_id"].to_numpy()
    act = table["action"].to_numpy()
    flags = table["flags"].to_numpy().copy()
    drop = np.isin(oid, np.array(sorted(exclude), dtype=oid.dtype)) & np.isin(act, REMOVABLE)
    if not drop.any():
        return table
    last_kept = -1
    for i in range(len(flags)):
        if drop[i]:
            if flags[i] & LAST and last_kept >= 0:
                flags[last_kept] |= LAST
        else:
            last_kept = i
        if flags[i] & LAST:
            last_kept = -1
    fi = table.schema.get_field_index("flags")
    t = table.set_column(fi, table.schema.field(fi), pa.array(flags, pa.uint8()))
    if "sequence" in t.column_names:
        seq = t["sequence"].to_numpy().astype(np.int64)
        ch = (
            t["channel_id"].to_numpy().astype(np.int64)
            if "channel_id" in t.column_names
            else np.zeros(len(seq), np.int64)
        )
        new_seq = seq.copy()
        for c in np.unique(ch):
            m = ch == c
            kept = set(seq[m & ~drop].tolist())
            gone = np.array(sorted(set(seq[m & drop].tolist()) - kept), dtype=np.int64)
            if gone.size:
                new_seq[m] = seq[m] - np.searchsorted(gone, seq[m], side="left")
        if (new_seq != seq).any():
            si = t.schema.get_field_index("sequence")
            t = t.set_column(si, t.schema.field(si), pa.array(new_seq, t.schema.field(si).type))
    return t.filter(pa.array(~drop))


def filtered_dataset(
    dataset_root: str | Path,
    out: str | Path,
    exclude: dict[int, set[int]],
    *,
    by_day: dict[tuple[int, int], set[int]] | None = None,
) -> Path:
    """Copy of the dataset with the excluded ids removed (manifest file hashes, row counts and `dataset_id` updated
    with the SPEC 3.10 rule). With `by_day` ((instrument_id, session_date) -> ids, `TwinMatch.days`) each partition
    drops only its own instrument-day's ids; else every partition of an instrument drops `exclude[instrument]`."""
    src, dst = Path(dataset_root), Path(out)
    dst.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    for name in ("instruments.parquet", "data_quality.json"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    if (src / "trades_source").exists():
        shutil.copytree(src / "trades_source", dst / "trades_source", dirs_exist_ok=True)
    shas: list[str] = []
    for p in manifest.get("partitions", []) or []:
        ex = (
            by_day.get((int(p["instrument_id"]), int(p["session_date"])), set())
            if by_day is not None
            else exclude.get(int(p["instrument_id"]), set())
        )
        total = 0
        for f in p.get("files", []):
            sp, dp = src / f["path"], dst / f["path"]
            dp.parent.mkdir(parents=True, exist_ok=True)
            if ex and f["path"].startswith("mbo/"):
                t = filter_mbo(pq.read_table(sp), ex)
                write_table(t, dp, SCHEMAS["qt.mbo"])
            else:
                shutil.copy2(sp, dp)
            f["sha256"] = sha256_file(dp)
            if ex and f["path"].startswith("mbo/"):
                f["rows"] = pq.read_metadata(dp).num_rows
            total += int(f.get("rows", 0))
            shas.append(f["sha256"])
        p["rows"] = total
    import hashlib

    ident = "\n".join(sorted(shas)) + "\n" + str(manifest.get("source_format", ""))
    manifest["dataset_id"] = hashlib.sha256(ident.encode()).hexdigest()
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return dst
