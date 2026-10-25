#!/usr/bin/env python3
"""Evidence for the oracle disagreements (docs/design/extensions/oracle-disagreements.md).

    uv run --frozen --no-sync python scripts/oracle/disagreements.py priority --dataset DS --out OUT.json
    uv run --frozen --no-sync python scripts/oracle/disagreements.py classify --dataset DS --shadows SH \
        --report BASE/discrepancy_report.json --out OUT.json
    uv run --frozen --no-sync python scripts/oracle/disagreements.py matrix --old-oracle O0 --new-oracle O1 \
        --old-qt Q0 --new-qt Q1 --out OUT.json
    uv run --frozen --no-sync python scripts/oracle/disagreements.py bound --dataset DS --results O1 --out OUT.json

`priority` measures, per instrument, the two facts behind rule D1 on a Nasdaq TotalView-ITCH dataset: `A` records
whose order reference is below the largest reference added before them (orders released out of reference order,
by time from the open, and how many land at a level that already holds a larger reference), and, for every
execution of a displayed order that is not the front of its level in arrival order, whether it is the front in
reference order.

`classify` lists, for each disagreeing shadow of a baseline discrepancy report, the tape evidence between its
activation and one second after its divergence: D1 (an `A` at its level with a reference below the largest
reference added before it went live), O1 (executions that hit the reference-order front of a level at or better
than its price while the arrival-order front was another order), D2 (a side-`N` print at or through its price at
the `ts_event` of an open execution sweep of the opposite resting side) and D3 (such a print off the tick grid,
sweeping its own side).

`bound` measures the receipt bound both engines give a shadow: the largest reference added on its own instrument
before it went live. ITCH references are not one feed-wide counter: they come from several counters, each shared
by a group of symbols. `counters` finds the groups of the dataset's instruments (two instruments share a counter
when their `A` records after the first minute of the session, merged in time, never go below the running maximum
reference). For every live shadow of an oracle run (`oracle_results.jsonl`: activation and end times), `bound`
counts the `A` records at its level while it was live whose reference is below the per-instrument bound (released
held orders, ranked ahead by both engines) or between it and the counter-group bound (the largest reference added
on any instrument sharing its counter before it went live): those were received before the shadow, yet both
engines rank them behind it (the per-instrument bound is a lower bound on the counter).

`matrix` compares {old, new} oracle results with {old, new} queuetruth replay outputs and says, per baseline
disagreement, which combinations agree: the attribution (queuetruth bug, oracle bug, or both).

Every subcommand writes one JSON file (`--out` is a file path, unlike `run_oracle.py`, whose `--out` is a
directory) of the form {"provenance": ..., "result": ...}: the provenance names each input with its sha256 (a
dataset by its manifest's `dataset_id` and the manifest's sha256), the oracle version, the queuetruth version and
the sha256 of the queuetruth extension that was importable when the evidence was computed.

Real-data inputs and outputs belong under `.realdata/` or `.scratch/` (never committed).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "tests" / "oracle"))

from qt_oracle import ORACLE_VERSION  # noqa: E402
from qt_oracle.compare import build_report, read_qt_results  # noqa: E402
from qt_oracle.runner import read_shadows  # noqa: E402
from qt_oracle.tape import InstrumentMeta, Rec, group_events, read_qt_dataset  # noqa: E402

NS = 1_000_000_000

# ---------------------------------------------------------------------------------------------------- priority


def _bucket(t_s: float) -> str:
    if t_s < 0:
        return "pre_open"
    if t_s < 60:
        return "first_60s"
    return "after_60s"


class _Levels:
    """Arrival-ordered levels of the displayed book from A / M / C / R records (F: no change; C removes; an `M`
    keeps the order's place when it only lowers its size at the same price, else moves it to the tail; `R` clears)."""

    def __init__(self) -> None:
        self.lv: dict[tuple[str, int], dict[int, int]] = collections.defaultdict(dict)
        self.where: dict[int, tuple[str, int]] = {}

    def add(self, r: Rec) -> None:
        old = self.where.get(r.order_id)
        if old is not None:
            self.lv[old].pop(r.order_id, None)
        self.lv[(r.side, r.price)][r.order_id] = r.size
        self.where[r.order_id] = (r.side, r.price)

    def modify(self, r: Rec) -> None:
        k = self.where.get(r.order_id)
        if k is not None and k == (r.side, r.price) and r.size <= self.lv[k][r.order_id]:
            if r.size > 0:
                self.lv[k][r.order_id] = r.size
            else:
                del self.lv[k][r.order_id]
                del self.where[r.order_id]
            return
        if k is not None:
            self.lv[k].pop(r.order_id, None)
            del self.where[r.order_id]
        if r.size > 0:
            self.add(r)

    def clear(self) -> None:
        self.lv.clear()
        self.where.clear()

    def cancel(self, r: Rec) -> None:
        k = self.where.get(r.order_id)
        if k is None:
            return
        d = self.lv[k]
        d[r.order_id] -= r.size
        if d[r.order_id] <= 0:
            del d[r.order_id]
            del self.where[r.order_id]

    def level_of(self, oid: int) -> dict[int, int] | None:
        k = self.where.get(oid)
        return None if k is None else self.lv[k]


def priority_evidence(meta: InstrumentMeta, records: Iterable[Rec]) -> dict[str, Any]:
    """Release-order and execution-order facts of one instrument (see the module docstring)."""
    open_ns = meta.midnight_utc_ns + meta.open_s * NS
    book = _Levels()
    max_ref = 0
    late: collections.Counter = collections.Counter()
    late_misplaced: collections.Counter = collections.Counter()
    late_first_s: float | None = None
    late_last_s: float | None = None
    execs: collections.Counter = collections.Counter()
    for r in records:
        a = r.action
        if a == "A":
            if r.order_id < max_ref:
                t = (r.ts_event - open_ns) / NS
                b = _bucket(t)
                late[b] += 1
                if any(o > r.order_id for o in book.lv[(r.side, r.price)]):
                    late_misplaced[b] += 1
                late_first_s = t if late_first_s is None else min(late_first_s, t)
                late_last_s = t if late_last_s is None else max(late_last_s, t)
            max_ref = max(max_ref, r.order_id)
            book.add(r)
        elif a == "M":
            book.modify(r)
        elif a == "R":
            book.clear()
        elif a == "C":
            book.cancel(r)
        elif a == "F":
            d = book.level_of(r.order_id)
            if not d:
                continue
            b = _bucket((r.ts_event - open_ns) / NS)
            arrival_front = next(iter(d))
            execs[(b, "all")] += 1
            if r.order_id == arrival_front:
                continue
            execs[(b, "not_arrival_front")] += 1
            if r.order_id == min(d):
                execs[(b, "not_arrival_front_but_reference_front")] += 1
    buckets = ("pre_open", "first_60s", "after_60s")
    return {
        "instrument_id": meta.instrument_id,
        "late_adds": {b: late[b] for b in buckets},
        "late_adds_at_a_level_with_a_larger_reference": {b: late_misplaced[b] for b in buckets},
        "late_adds_first_s_after_open": late_first_s,
        "late_adds_last_s_after_open": late_last_s,
        "executions": {
            b: {k: execs[(b, k)] for k in ("all", "not_arrival_front", "not_arrival_front_but_reference_front")}
            for b in buckets
        },
    }


# ---------------------------------------------------------------------------------------------------- classify


_NO_END = 1 << 62  # a window to the end of the tape


def _at_or_better(side: str, px: int, ref: int) -> bool:
    return px >= ref if side == "B" else px <= ref


def classify_evidence(
    meta: InstrumentMeta, records: Iterable[Rec], shadows: list, divergence_ns: dict[str, int | None]
) -> dict[str, dict[str, Any]]:
    """Per shadow of one instrument: D1 / O1 / D2 / D3 evidence from activation to divergence + 1 s (to the end
    of the tape when the report gives no divergence time)."""
    open_ns = meta.midnight_utc_ns + meta.open_s * NS
    st = {
        s.shadow_id: {
            "spec": s,
            "live": False,
            "bound": None,
            "t_live": None,
            "end": _NO_END if divergence_ns[s.shadow_id] is None else divergence_ns[s.shadow_id] + NS,
            "d1": [],
            "d1_exec": [],
            "o1_at": [],
            "o1_better": [],
            "d2": [],
            "d3": [],
        }
        for s in shadows
    }
    book = _Levels()
    max_ref = 0
    # resting side -> (ts_event, {(order id, sequence)} of the sweep's executions)
    sweep: dict[str, tuple[int, set[tuple[int, int]]]] = {}
    for ev in group_events(records):
        t0 = ev[0].ts_event
        for x in st.values():
            s = x["spec"]
            if not x["live"] and t0 > s.ts_submit + s.latency_ns:
                x["live"], x["bound"], x["t_live"] = True, max_ref, t0
        if all(t0 > x["end"] for x in st.values()):
            break
        open_now = [x for x in st.values() if x["live"] and t0 <= x["end"]]
        for r in ev:
            a = r.action
            if a == "A":
                for x in open_now:
                    s = x["spec"]
                    if r.side == s.side and r.price == s.price and r.order_id < x["bound"]:
                        x["d1"].append((r.ts_event, r.order_id, r.size))
                max_ref = max(max_ref, r.order_id)
                book.add(r)
                sweep.pop(r.side, None)
            elif a == "M":
                sweep.pop(r.side, None)
                book.modify(r)
            elif a == "R":
                sweep.clear()
                book.clear()
            elif a == "F":
                k = book.where.get(r.order_id)
                if k is None:
                    continue
                cur = sweep.get(k[0])
                if cur and cur[0] == r.ts_event:
                    cur[1].add((r.order_id, r.sequence))
                else:
                    sweep[k[0]] = (r.ts_event, {(r.order_id, r.sequence)})
                d = book.lv[k]
                front, ref_front = next(iter(d)), min(d)
                for x in open_now:
                    s = x["spec"]
                    if k[0] != s.side or not _at_or_better(s.side, k[1], s.price):
                        continue
                    if any(r.order_id == o for _, o, _ in x["d1"]):
                        x["d1_exec"].append((r.ts_event, r.order_id, r.size))
                    if r.order_id != front and r.order_id == ref_front:
                        x["o1_at" if k[1] == s.price else "o1_better"].append((r.ts_event, r.order_id, front))
            elif a == "C":
                k = book.where.get(r.order_id)
                if k is None:
                    continue
                cur = sweep.get(k[0])
                if cur and (r.order_id, r.sequence) not in cur[1]:
                    sweep.pop(k[0], None)
                book.cancel(r)
            elif a == "T" and r.side == "N":
                sides = [sd for sd, (ts, _) in sweep.items() if ts == r.ts_event]
                if len(sides) != 1:
                    continue
                for x in open_now:
                    s = x["spec"]
                    if not _at_or_better(s.side, s.price, r.price):
                        continue
                    if sides[0] != s.side:
                        x["d2"].append((r.ts_event, sides[0], r.price, r.size))
                    elif r.price % meta.tick_size:
                        x["d3"].append((r.ts_event, sides[0], r.price, r.size))
    out = {}
    for sid, x in st.items():
        s = x["spec"]
        out[sid] = {
            "instrument_id": meta.instrument_id,
            "side": s.side,
            "price": s.price,
            "qty": s.qty,
            "live_s_after_open": None if x["t_live"] is None else round((x["t_live"] - open_ns) / NS, 3),
            "divergence_s_after_open": None
            if divergence_ns[sid] is None
            else round((divergence_ns[sid] - open_ns) / NS, 6),
            "bound": x["bound"],
            **{k: {"n": len(x[k]), "first": x[k][:3]} for k in ("d1", "d1_exec", "o1_at", "o1_better", "d2", "d3")},
        }
    return out


def classify(dataset: str, shadows_path: str, report_path: str) -> dict[str, dict[str, Any]]:
    rep = json.loads(Path(report_path).read_text())
    div = {d["shadow_id"]: d["ts_divergence"] for d in rep["discrepancies"]}
    cat = {d["shadow_id"]: d["category"] for d in rep["discrepancies"]}
    by_inst: dict[int, list] = collections.defaultdict(list)
    for s in read_shadows(shadows_path):
        if s.shadow_id in div:
            by_inst[s.instrument_id].append(s)
    out: dict[str, dict[str, Any]] = {}
    for part in read_qt_dataset(dataset):
        shs = by_inst.get(part.meta.instrument_id)
        if shs:
            for sid, v in classify_evidence(part.meta, part.iter_records(), shs, div).items():
                out[sid] = {"report_category": cat[sid], **v}
    return out


# ---------------------------------------------------------------------------------------------------- bound


def _adds(part) -> dict[str, Any]:
    """The `A` records of one partition as numpy arrays (ts, reference, side byte, price), in tape order."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    cols = ["ts_event", "action", "side", "price", "order_id"]
    if part.records is not None:
        recs = [r for r in part.records if r.action == "A"]
        return {
            "ts": np.array([r.ts_event for r in recs], dtype=np.int64),
            "ref": np.array([r.order_id for r in recs], dtype=np.uint64),
            "side": np.array([ord(r.side) for r in recs], dtype=np.uint8),
            "price": np.array([r.price for r in recs], dtype=np.int64),
        }
    t = pa.concat_tables([pq.read_table(p, columns=cols) for p in part.paths])
    t = t.filter(pc.equal(t["action"], ord("A")))
    return {
        "ts": t["ts_event"].cast("int64").to_numpy(),
        "ref": t["order_id"].to_numpy().astype(np.uint64),
        "side": t["side"].to_numpy().astype(np.uint8),
        "price": t["price"].to_numpy().astype(np.int64),
    }


def _bound_at(ts: Any, cummax: Any, t: int) -> int:
    """Largest reference added strictly before `t` (0 when none)."""
    import numpy as np

    i = int(np.searchsorted(ts, t, side="left"))
    return int(cummax[i - 1]) if i > 0 else 0


def _merged(adds: dict[int, dict[str, Any]], ids: Iterable[int], key: str = "") -> tuple[Any, Any]:
    """(ts, running maximum reference) of the `A` records of `ids` merged in time (ties by reference)."""
    import numpy as np

    ts = np.concatenate([adds[i]["ts" + key] for i in ids])
    ref = np.concatenate([adds[i]["ref" + key] for i in ids])
    o = np.lexsort((ref, ts))
    return ts[o], ref[o]


def counter_groups(parts: list, adds: dict[int, dict[str, Any]] | None = None) -> list[list[int]]:
    """Instruments grouped by the ITCH reference counter they share (see the module docstring); an instrument with
    no `A` after the first minute is alone."""
    import numpy as np

    adds = adds if adds is not None else {p.meta.instrument_id: _adds(p) for p in parts}
    for p in parts:
        a = adds[p.meta.instrument_id]
        late = a["ts"] >= p.meta.midnight_utc_ns + (p.meta.open_s + 60) * NS
        a["ts_late"], a["ref_late"] = a["ts"][late], a["ref"][late]
    groups: list[list[int]] = []
    for i in sorted(adds):
        for g in groups:
            if not len(adds[i]["ref_late"]) or not len(adds[g[0]]["ref_late"]):
                continue
            _, ref = _merged(adds, [*g, i], "_late")
            if not (ref[1:] < np.maximum.accumulate(ref)[:-1]).any():
                g.append(i)
                break
        else:
            groups.append([i])
    return groups


def bound_evidence(parts: list, results: list[dict]) -> dict[str, Any]:
    """`A` records at a live shadow's level received before it: below the per-instrument bound at activation, or
    between it and the counter-group bound (see the module docstring)."""
    import numpy as np

    adds = {p.meta.instrument_id: _adds(p) for p in parts}
    for a in adds.values():
        a["cummax"] = np.maximum.accumulate(a["ref"]) if len(a["ref"]) else a["ref"]
    groups = counter_groups(parts, adds)
    grp: dict[int, tuple[Any, Any]] = {}
    for g in groups:
        g_ts, g_ref = _merged(adds, g)
        for i in g:
            grp[i] = (g_ts, np.maximum.accumulate(g_ref) if len(g_ref) else g_ref)
    per_inst: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    between_shadows: list[dict[str, Any]] = []
    for r in results:
        t_act = r.get("ts_activated_event")
        a = adds.get(r["instrument_id"])
        if t_act is None or a is None or r.get("price") is None:
            continue
        t_end = r.get("ts_end") or (1 << 62)
        b_inst = _bound_at(a["ts"], a["cummax"], t_act)
        b_glob = _bound_at(*grp[r["instrument_id"]], t_act)
        m = (a["side"] == ord(r["side"])) & (a["price"] == r["price"]) & (a["ts"] >= t_act) & (a["ts"] < t_end)
        refs = a["ref"][m]
        c = per_inst[r["instrument_id"]]
        c["shadows_live"] += 1
        below = int((refs < b_inst).sum())
        between = int(((refs > b_inst) & (refs < b_glob)).sum())
        c["below_instrument_bound"] += below
        c["between_bounds"] += between
        if between:
            c["shadows_with_between"] += 1
            between_shadows.append(
                {
                    "shadow_id": r["shadow_id"],
                    "instrument_id": r["instrument_id"],
                    "between": between,
                    "qty_filled": r.get("filled_qty"),
                    "bound_instrument": b_inst,
                    "bound_counter": b_glob,
                }
            )
    keys = (
        "shadows_live",
        "below_instrument_bound",
        "between_bounds",
        "shadows_with_between",
    )
    total = {k: sum(c[k] for c in per_inst.values()) for k in keys}
    return {
        "counter_groups": groups,
        "by_instrument": {str(i): {k: c[k] for k in keys} for i, c in sorted(per_inst.items())},
        "total": total,
        "shadows_with_between": between_shadows,
    }


# ---------------------------------------------------------------------------------------------------- matrix


def matrix(old_oracle: str, new_oracle: str, old_qt: str, new_qt: str) -> dict[str, Any]:
    """Agreement of every {oracle} x {queuetruth} pair, and per baseline (old, old) disagreement."""

    def oracle(d: str) -> list[dict]:
        return [json.loads(line) for line in open(Path(d) / "oracle_results.jsonl")]

    o = {"old": oracle(old_oracle), "new": oracle(new_oracle)}
    q = {"old": read_qt_results(old_qt), "new": read_qt_results(new_qt)}
    reps = {
        (ok, qk): build_report(o[ok], q[qk], reference_kind="queuetruth", oracle_stats={}, config={})
        for ok in o
        for qk in q
    }
    dis = {k: {x["shadow_id"] for x in r["discrepancies"]} for k, r in reps.items()}
    base = sorted(dis[("old", "old")])
    return {
        "summary": {
            f"oracle_{ok}+qt_{qk}": {
                k: reps[(ok, qk)]["summary"][k] for k in ("n_compared", "n_agree", "n_disagree", "by_category")
            }
            for ok, qk in reps
        },
        "baseline": {sid: {f"oracle_{ok}+qt_{qk}": sid not in dis[(ok, qk)] for ok, qk in reps} for sid in base},
        "remaining": sorted(dis[("new", "new")]),
    }


# ---------------------------------------------------------------------------------------------------- provenance


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _input(path: str) -> dict[str, Any]:
    """One input: a file by its sha256; a dataset by its manifest; another directory (an oracle or replay output)
    by the sha256 of its top-level files, in name order."""
    p = Path(path)
    if p.is_file():
        return {"path": str(p), "sha256": _sha256(p)}
    man = p / "manifest.json"
    if man.is_file():
        return {
            "path": str(p),
            "dataset_id": json.loads(man.read_text()).get("dataset_id"),
            "manifest_sha256": _sha256(man),
        }
    if p.is_dir():
        files = {f.name: _sha256(f) for f in sorted(p.iterdir()) if f.is_file()}
        return {"path": str(p), "files_sha256": files}
    return {"path": str(p), "missing": True}


def provenance(inputs: dict[str, str]) -> dict[str, Any]:
    """Inputs with their hashes, the oracle version, and the queuetruth version and extension in this process."""
    qt: dict[str, Any] = {"version": None, "extension_sha256": None}
    try:
        import queuetruth
        from queuetruth import _core

        qt["version"] = getattr(queuetruth, "__version__", None)
        qt["extension_sha256"] = _sha256(Path(_core.__file__))
    except Exception as e:  # pragma: no cover - environment dependent
        qt["error"] = str(e)
    return {
        "oracle_version": ORACLE_VERSION,
        "queuetruth": qt,
        "inputs": {k: _input(v) for k, v in inputs.items()},
    }


# ---------------------------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("priority")
    p.add_argument("--dataset", required=True)
    p.add_argument("--out", required=True)
    c = sub.add_parser("classify")
    c.add_argument("--dataset", required=True)
    c.add_argument("--shadows", required=True)
    c.add_argument("--report", required=True)
    c.add_argument("--out", required=True)
    m = sub.add_parser("matrix")
    for k in ("--old-oracle", "--new-oracle", "--old-qt", "--new-qt", "--out"):
        m.add_argument(k, required=True)
    b = sub.add_parser("bound")
    b.add_argument("--dataset", required=True)
    b.add_argument("--results", required=True, help="an oracle output directory (oracle_results.jsonl)")
    b.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "bound":
        inputs = {"dataset": a.dataset, "results": str(Path(a.results) / "oracle_results.jsonl")}
        rows = [json.loads(line) for line in open(Path(a.results) / "oracle_results.jsonl")]
        res: Any = bound_evidence(read_qt_dataset(a.dataset), rows)
    elif a.cmd == "priority":
        inputs = {"dataset": a.dataset}
        res = [priority_evidence(p.meta, p.iter_records()) for p in read_qt_dataset(a.dataset)]
    elif a.cmd == "classify":
        inputs = {"dataset": a.dataset, "shadows": a.shadows, "report": a.report}
        res = classify(a.dataset, a.shadows, a.report)
    else:
        inputs = {"old_oracle": a.old_oracle, "new_oracle": a.new_oracle, "old_qt": a.old_qt, "new_qt": a.new_qt}
        res = matrix(a.old_oracle, a.new_oracle, a.old_qt, a.new_qt)
    doc = {"provenance": provenance(inputs), "result": res}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
