"""Cross-source parity: two datasets of the same session(s) (for example Nasdaq ITCH and Databento XNAS.ITCH, or
two captures of one venue), matched by `(session_date, symbol)` and compared record by record.

Records are compared as multisets of `(ts_event, action, side, price, size, order_id)` (strict) and of the same
without `ts_event` (loose), snapshot records excluded. Prices are rescaled to the finer `price_exp` of the pair;
`ts_recv`, `sequence`, `flags`, `channel_id`, `publisher_id` and `instrument_id` are feed-specific and never
compared. Strict differences that the loose match resolves are timestamp disagreements."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.dq.data import F_SNAPSHOT, UNDEF_PRICE, Dataset, PartRef
from queuetruth.dq.framework import Check, chr_or, level, ratio
from queuetruth.dq.model import DqCheck
from queuetruth.errors import QtError

STRICT = ("ts_event", "action", "side", "price", "size", "order_id")
LOOSE = STRICT[1:]


def _price_exp(ds: Dataset, ref: PartRef) -> int:
    inst = ref.instrument or {}
    if inst.get("price_exp") is not None:
        return int(inst["price_exp"])
    pe = (ds.manifest.get("price_exp_by_instrument") or {}).get(str(ref.instrument_id))
    return int(pe) if pe is not None else -9


def _table(ds: Dataset, ref: PartRef, exp: int, common: int) -> pa.Table:
    p = ds.read(ref, ("ts_event", "action", "side", "price", "size", "order_id", "flags"))
    keep = (p["flags"] & F_SNAPSHOT) == 0
    px = p["price"][keep].astype(np.int64)
    if exp != common:
        px = _rescale(px, exp, common, ref)
    return pa.table(
        {
            "ts_event": p["ts_event"][keep].astype(np.int64),
            "action": p["action"][keep],
            "side": p["side"][keep],
            "price": px,
            "size": p["size"][keep].astype(np.uint64),
            "order_id": p["order_id"][keep].astype(np.uint64),
        }
    )


def _rescale(px: np.ndarray, exp: int, common: int, ref: PartRef) -> np.ndarray:
    """Prices at `exp` -> prices at the finer `common` exponent; `QT-INPUT-004` when a defined price would leave
    int64 (numpy multiplication wraps around silently; a factor past int64 does not even convert)."""
    scale = 10 ** (exp - common)
    undef = px == UNDEF_PRICE
    lim = np.iinfo(np.int64).max // scale  # 0 when the factor itself is past int64
    over = ~undef & ((px > lim) | (px < -lim))
    if over.any():
        raise QtError(
            "QT-INPUT-004",
            f"{ref.date} {ref.symbol}: price {int(px[over][0])} at price_exp {exp} does not fit int64 at the "
            f"comparison's price_exp {common}",
        )
    if lim == 0:  # only zero (and undefined) prices remain
        return np.where(undef, UNDEF_PRICE, 0).astype(np.int64)
    return np.where(undef, UNDEF_PRICE, px * np.int64(scale))


def _diff(a: pa.Table, b: pa.Table, keys: tuple[str, ...]) -> pa.Table:
    """Joined multiset counts: key columns, `n_a`, `n_b` (missing = 0)."""
    ga = a.group_by(list(keys)).aggregate([(keys[0], "count")]).rename_columns([*keys, "n_a"])
    gb = b.group_by(list(keys)).aggregate([(keys[0], "count")]).rename_columns([*keys, "n_b"])
    j = ga.join(gb, keys=list(keys), join_type="full outer")
    return j.set_column(j.schema.get_field_index("n_a"), "n_a", pc.fill_null(j.column("n_a"), 0)).set_column(
        j.schema.get_field_index("n_b"), "n_b", pc.fill_null(j.column("n_b"), 0)
    )


class CrossSourceCheck(Check):
    id = "cross_source"
    title = "Cross-source parity"

    def finish(self) -> DqCheck:
        other = self.ctx.compare
        if other is None:
            return self.skipped("no second dataset (--compare DIR)")
        if self.skip_reason:
            return self.skipped()
        if other.kind != "mbo":
            return self.skipped(f"the compared dataset is kind {other.kind!r}; record parity needs MBO on both sides")
        a_parts = {(r.session_date, r.symbol): r for r in self.ds.parts}
        b_parts = {(r.session_date, r.symbol): r for r in other.parts}
        common = sorted(set(a_parts) & set(b_parts))
        ev_parts = self.evidence(
            "partitions",
            [
                "date",
                "symbol",
                "records_a",
                "records_b",
                "matched",
                "only_a",
                "only_b",
                "timestamp_only",
                "instrument_a",
                "instrument_b",
            ],
        )
        ev_parts.cap = max(self.cap, len(common))
        ev_un = self.evidence("unmatched_partitions", ["side", "date", "symbol", "instrument_id", "records"])
        for side, mine, theirs in (("a", a_parts, b_parts), ("b", b_parts, a_parts)):
            for k in sorted(set(mine) - set(theirs)):
                r = mine[k]
                ev_un.add((side, r.date, r.symbol, r.instrument_id, r.rows))
        ev_a = self.evidence("only_in_a", ["date", "symbol", *STRICT, "count"])
        ev_b = self.evidence("only_in_b", ["date", "symbol", *STRICT, "count"])
        by_action: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # matched, only_a, only_b
        tot = {
            "records_a": 0,
            "records_b": 0,
            "matched": 0,
            "only_a": 0,
            "only_b": 0,
            "loose_only_a": 0,
            "loose_only_b": 0,
        }
        for key in common:
            ra, rb = a_parts[key], b_parts[key]
            ea, eb = _price_exp(self.ds, ra), _price_exp(other, rb)
            cexp = min(ea, eb)
            ta, tb = _table(self.ds, ra, ea, cexp), _table(other, rb, eb, cexp)
            j = _diff(ta, tb, STRICT)
            na = j.column("n_a").to_numpy().astype(np.int64)
            nb = j.column("n_b").to_numpy().astype(np.int64)
            matched = int(np.minimum(na, nb).sum())
            oa = int(np.maximum(na - nb, 0).sum())
            ob = int(np.maximum(nb - na, 0).sum())
            jl = _diff(ta, tb, LOOSE)
            la = jl.column("n_a").to_numpy().astype(np.int64)
            lb = jl.column("n_b").to_numpy().astype(np.int64)
            loa = int(np.maximum(la - lb, 0).sum())
            lob = int(np.maximum(lb - la, 0).sum())
            acts = j.column("action").to_numpy()
            for act in np.unique(acts):
                s = acts == act
                ba = by_action[chr_or(act)]
                ba[0] += int(np.minimum(na[s], nb[s]).sum())
                ba[1] += int(np.maximum(na[s] - nb[s], 0).sum())
                ba[2] += int(np.maximum(nb[s] - na[s], 0).sum())
            tot["records_a"] += ta.num_rows
            tot["records_b"] += tb.num_rows
            tot["matched"] += matched
            tot["only_a"] += oa
            tot["only_b"] += ob
            tot["loose_only_a"] += loa
            tot["loose_only_b"] += lob
            date, sym = ra.date, ra.symbol
            ts_only = (oa - loa) + (ob - lob)
            ev_parts.add(
                (date, sym, ta.num_rows, tb.num_rows, matched, oa, ob, ts_only, ra.instrument_id, rb.instrument_id)
            )
            for ev, mask, cnt in ((ev_a, na > nb, na - nb), (ev_b, nb > na, nb - na)):
                idx = np.nonzero(mask)[0]
                if idx.size == 0:
                    continue
                order = idx[np.argsort(j.column("ts_event").to_numpy()[idx], kind="stable")]
                cols = {c: j.column(c).to_numpy() for c in STRICT}
                ev.extend(
                    (
                        (
                            date,
                            sym,
                            *(chr_or(cols[c][i]) if c in ("action", "side") else cols[c][i] for c in STRICT),
                            int(cnt[i]),
                        )
                        for i in order[: ev.room]
                    ),
                    total=int(cnt[idx].sum()),
                )
        if not common:
            return self.result(
                "fail",
                "no (session_date, symbol) in common: the datasets do not cover the same sessions",
                {"partitions_a": len(a_parts), "partitions_b": len(b_parts)},
                [ev_un],
            )
        diff = tot["only_a"] + tot["only_b"]
        status = level(diff, tot["records_a"] + tot["records_b"])
        if status == "pass" and ev_un.total:
            status = "warn"
        ts_only = (tot["only_a"] - tot["loose_only_a"]) + (tot["only_b"] - tot["loose_only_b"])
        summary = (
            f"{len(common)} common partition(s): {tot['matched']} record(s) match, {tot['only_a']} only in A, "
            f"{tot['only_b']} only in B ({ts_only} of the differences are timestamp-only)"
        )
        if ev_un.total:
            summary += f"; {ev_un.total} partition(s) without a counterpart"
        metrics: dict[str, Any] = {
            "common_partitions": len(common),
            "unmatched_partitions": ev_un.total,
            **tot,
            "timestamp_only_differences": ts_only,
            "difference_share": ratio(diff, tot["records_a"] + tot["records_b"]),
        }
        for act, (mt, oa, ob) in sorted(by_action.items()):
            metrics[f"action_{act}"] = f"matched {mt}, only A {oa}, only B {ob}"
        notes = [
            f"A = {self.ds.root} ({self.ds.source_format}), B = {other.root} ({other.source_format}); partitions "
            "matched by (session_date, symbol)",
            "records compared on (ts_event, action, side, price, size, order_id); ts_recv, sequence, flags, channel "
            "and instrument ids are feed-specific and not compared; snapshot records excluded",
        ]
        return self.result(status, summary, metrics, [ev_parts, ev_un, ev_a, ev_b], notes)


__all__ = ["CrossSourceCheck"]
