"""Real-data validation of queuetruth on et prototype artifacts: SPEC 6.8 RD-1 .. RD-13 (+ RD-7b).

et is the author's earlier private prototype engine; its artifacts are not public.

Driver for `make realdata` (`ET_ROOT=<et checkout>` or `QT_ET_ROOT`):

    uv run --frozen --no-sync python scripts/realdata/validate_et.py \
        --et-root <et checkout> --out .realdata

Inputs are read-only (`<et>/out`, `<et>/data`). Everything this writes stays under `--out` (gitignored); only the
aggregate numbers in `<out>/results.json` / `<out>/bench.json` may be copied to docs/RESULTS.md (SPEC 6.8).

Every call into queuetruth is marked `# QT-API` and uses only the public API of SPEC 4.3 / 4.4 (Python functions with
Spec models, the `queuetruth-core` binary for RD-13). Comparisons are written against `expected.json`
(built by et_reference.py + build_expected.py from the et artifacts) and, where a row-level diff is
needed, against the et CSVs directly (streamed, never copied).

Status per check: `pass` / `fail` (exact-match checks), `recorded` (RD-8 .. RD-12: runs and numbers recorded;
`fail` only if a stated property breaks), `skipped` (with a reason), `error` (exception text).
Exit code: 0 if no check is `fail`/`error`, else 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
UNDEF = 9223372036854775807
DAY1, DAY2 = "2019-12-30", "2020-01-30"
DAYS = {
    DAY1: {
        "itch": "out/itch-12302019",
        "replay": "out/replay-itch-12302019-v2",
        "fidelity": "out/fidelity-itch",
        "midnight": 1577682000000000000,
    },
    DAY2: {
        "itch": "out/itch-01302020",
        "replay": "out/replay-itch-01302020",
        "fidelity": "out/fidelity-01302020",
        "midnight": 1580360400000000000,
    },
}
H1S, H10S, H60S = 1_000_000_000, 10_000_000_000, 60_000_000_000
INV8 = [
    "cancels_exceeding_resting",
    "fills_exceeding_resting",
    "fills_unknown_order",
    "unknown_order_refs",
    "duplicate_adds",
    "unexpected_modifies",
    "crossed_books_after_last",
    "negative_spreads",
]


# ---- small utilities ------------------------------------------------------------------------------------------


def jdump(o: Any) -> str:
    return json.dumps(o, indent=1, default=str)


def to_dict(model: Any) -> dict:
    """Pydantic summary -> plain dict by alias (`schema`, `pass`)."""
    return model.model_dump(by_alias=True) if hasattr(model, "model_dump") else dict(model)


def find_table(out_dir: Path, schema_name: str) -> Path:
    """Locate the single Parquet file in `out_dir` whose `qt.schema` metadata is `schema_name`."""
    hits = []
    for p in sorted(out_dir.rglob("*.parquet")):
        md = pq.read_schema(p).metadata or {}
        if md.get(b"qt.schema") == schema_name.encode():
            hits.append(p)
    if len(hits) != 1:
        raise FileNotFoundError(f"{schema_name}: expected 1 file under {out_dir}, found {[str(h) for h in hits]}")
    return hits[0]


def tables(out_dir: Path, schema_name: str) -> list[Path]:
    return [
        p
        for p in sorted(out_dir.rglob("*.parquet"))
        if (pq.read_schema(p).metadata or {}).get(b"qt.schema") == schema_name.encode()
    ]


def ints(col: pa.ChunkedArray | pa.Array) -> list:
    if pa.types.is_timestamp(col.type):
        col = pc.cast(col, pa.int64())
    return col.to_pylist()


def strs(col) -> list:
    if pa.types.is_dictionary(col.type):
        col = pc.cast(col, pa.utf8())
    return col.to_pylist()


def peak_rss_mb() -> dict:
    div = 1 if platform.system() == "Darwin" else 1024  # ru_maxrss: bytes on macOS, KiB on Linux
    s = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1024 if div == 1024 else 1) / 2**20
    c = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * (1024 if div == 1024 else 1) / 2**20
    return {"self_mb": round(s, 1), "children_mb": round(c, 1)}


def du_mb(p: Path) -> float:
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 2**20, 1) if p.exists() else 0.0


class Check:
    """Accumulates sub-checks for one RD id."""

    def __init__(self, rd: str, title: str):
        self.rd, self.title = rd, title
        self.items: list[dict] = []
        self.numbers: dict[str, Any] = {}
        self.status = "pass"
        self.notes: list[str] = []

    def eq(self, name: str, got: Any, want: Any, tol: float | None = None) -> bool:
        if tol is not None and isinstance(got, (int, float)) and isinstance(want, (int, float)):
            ok = abs(float(got) - float(want)) <= tol
        else:
            ok = got == want
        self.items.append({"name": name, "ok": ok, "got": got if ok else _short(got), "want": _short(want)})
        if not ok:
            self.status = "fail"
        return ok

    def true(self, name: str, cond: bool, detail: Any = None) -> bool:
        self.items.append({"name": name, "ok": bool(cond), "detail": _short(detail)})
        if not cond:
            self.status = "fail"
        return bool(cond)

    def record(self, **kw: Any) -> None:
        self.numbers.update(kw)

    def doc(self) -> dict:
        return {
            "rd": self.rd,
            "title": self.title,
            "status": self.status,
            "checks": self.items,
            "numbers": self.numbers,
            "notes": self.notes,
        }


def _short(x: Any, n: int = 2000) -> Any:
    s = json.dumps(x, default=str)
    return x if len(s) <= n else s[:n] + "...(truncated)"


@dataclass
class Ctx:
    et: Path
    out: Path
    exp: dict
    threads: int
    keep: bool
    ds: dict[str, Path] = field(default_factory=dict)
    runs: dict[str, Path] = field(default_factory=dict)
    shadows: dict[str, Path] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)

    def et_path(self, date: str, key: str, *rest: str) -> Path:
        return self.et / DAYS[date][key] / Path(*rest)

    def dir(self, *parts: str) -> Path:
        p = self.out.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def fresh(self, *parts: str) -> Path:
        p = self.dir(*parts)
        if p.exists():
            shutil.rmtree(p)
        return p

    def timed(self, key: str, fn: Callable[[], Any]) -> Any:
        t0 = time.perf_counter()
        r = fn()
        self.timings[key] = round(time.perf_counter() - t0, 3)
        return r


# ---- queuetruth calls (the only places that touch the package) ---------------------------------------------------


def qt_mod():
    import queuetruth as qt  # QT-API
    from queuetruth.schemas import specs as S  # QT-API (Spec models, SPEC 4.2)

    return qt, S


def ingest_etcsv(ctx: Ctx, date: str) -> tuple[Path, dict]:
    qt, S = qt_mod()
    out = ctx.fresh("ds", f"nasdaq-{date}")
    spec = S.IngestSpec(  # QT-API: SPEC 4.2 IngestSpec; RD-1 parameters
        format="etcsv",
        inputs=[
            {"path": str(ctx.et_path(date, "itch", "mbo.csv"))},
            {"path": str(ctx.et_path(date, "itch", "trades.csv"))},
        ],
        instruments={"path": str(ctx.et_path(date, "itch", "instruments.csv"))},
        venue="nasdaq",
        out=str(out),
        session_date=date,
        midnight_utc_ns=DAYS[date]["midnight"],
        tz="America/New_York",
        price_exp=-9,
        tick_size="0.01",
        lot_size="1",
    )
    summ = ctx.timed(f"ingest_{date}", lambda: qt.ingest(spec))  # QT-API qt.ingest
    ctx.ds[date] = out
    return out, to_dict(summ)


ET_RULES = {"fill_rule": "et", "hidden_print_policy": "ignore"}  # SPEC 6.8: et-parity replays (E36)


def replay(ctx: Ctx, name: str, dataset: Path, *, et_rules: bool = True, **kw: Any) -> tuple[Path, dict]:
    """Runs `qt.replay`; et-parity runs use `fill_rule = et`, `hidden_print_policy = ignore` (SPEC 6.8), as
    ReplaySpec fields when the installed schema has them (schema 1.1), else as the `qt.replay` keyword options."""
    qt, S = qt_mod()
    out = ctx.fresh("replay", name)
    extra = dict(ET_RULES) if et_rules else {}
    as_fields = {k: v for k, v in extra.items() if k in S.ReplaySpec.model_fields}
    as_kwargs = {k: v for k, v in extra.items() if k not in as_fields}
    spec = S.ReplaySpec(  # QT-API ReplaySpec
        dataset={"path": str(dataset)}, out=str(out), threads=ctx.threads, **as_fields, **kw
    )
    summ = ctx.timed(f"replay_{name}", lambda: qt.replay(spec, **as_kwargs))  # QT-API qt.replay
    ctx.runs[name] = out
    return out, to_dict(summ)


# ---- renderers (qt tables -> et CSV lines) ---------------------------------------------------------------------


def iter_cols(paths: list[Path], cols: list[str], dict_cols: tuple[str, ...] = ()):
    """Stream rows (as tuples, timestamps as int ns, dictionaries as str) from Parquet files in the given order."""
    for path in paths:
        for b in pq.ParquetFile(path).iter_batches(batch_size=1 << 18, columns=cols):
            yield from zip(
                *(strs(b.column(k)) if cols[k] in dict_cols else ints(b.column(k)) for k in range(len(cols))),
                strict=False,
            )


def bbo_digests(bbo_path: Path) -> tuple[dict[str, str], dict[str, int]]:
    """Per-instrument sha256 of qt.bbo (mode full) rendered as et bbo.csv lines."""
    hs: dict[int, Any] = {}
    n: dict[int, int] = defaultdict(int)
    cols = ["ts_event", "instrument_id", "sequence", "bid_px", "bid_sz", "ask_px", "ask_sz"]
    for t, i, q, x1, x2, x3, x4 in iter_cols([bbo_path], cols):
        h = hs.get(i)
        if h is None:
            h = hs[i] = hashlib.sha256()
        h.update(f"{t},{i},{UNDEF if x1 is None else x1},{x2},{UNDEF if x3 is None else x3},{x4},{q}\n".encode())
        n[i] += 1
    return {str(k): v.hexdigest() for k, v in sorted(hs.items())}, {str(k): v for k, v in sorted(n.items())}


def iter_events(bbo_path: Path):
    """(instrument_id, ts_event, sequence) for every event, from qt.bbo mode full (one row per F_LAST event)."""
    return iter_cols([bbo_path], ["instrument_id", "ts_event", "sequence"])


def rank_digests(path: Path) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    rel: dict[int, Any] = {}
    nos: dict[int, Any] = {}
    sweep_ix: dict[int, dict[int, int]] = defaultdict(dict)
    n: dict[int, int] = defaultdict(int)
    cols = [
        "ts_event",
        "instrument_id",
        "sequence",
        "order_id",
        "side",
        "price",
        "rank",
        "rank_sweep_adjusted",
        "level_depth",
        "sweep_id",
        "exec_kind",
    ]
    for t, i, q, o, s, p, r, ra, ld, sw, ek in iter_cols([path], cols, dict_cols=("exec_kind",)):
        m = sweep_ix[i]
        if sw not in m:
            m[sw] = len(m) + 1
        head = f"{t},{i},{q},{o},{chr(s)},{p},{r},{ra},{ld}"
        if i not in rel:
            rel[i], nos[i] = hashlib.sha256(), hashlib.sha256()
        rel[i].update(f"{head},{m[sw]},{ek}\n".encode())
        nos[i].update(f"{head},{ek}\n".encode())
        n[i] += 1
    return (
        {str(k): v.hexdigest() for k, v in sorted(rel.items())},
        {str(k): v.hexdigest() for k, v in sorted(nos.items())},
        {str(k): v for k, v in sorted(n.items())},
    )


def l2_files_in_partition_order(l2_root: Path) -> list[Path]:
    def key(p: Path):
        parts = dict(x.split("=", 1) for x in p.relative_to(l2_root).parts[:-1] if "=" in x)
        return (parts.get("date", ""), int(parts.get("instrument_id", "0")), p.name)

    return sorted(
        (p for p in l2_root.rglob("*.parquet") if (pq.read_schema(p).metadata or {}).get(b"qt.schema") == b"qt.l2"),
        key=key,
    )


def render_wide(events, rows, levels_out: list[int], mode: str) -> dict:
    """Stream-merge the event list with qt.depth rows (mode "depth": (inst, seq, side, level, price, size)) or qt.l2
    rows (mode "l2": (inst, seq, kind, side, price, new_qty)); after every event render et's wide depth line for each
    N in `levels_out` and hash it per instrument. Both inputs are in partition order, rows in emission order."""
    nmax = max(levels_out)
    hs: dict[int, dict[int, Any]] = {n: {} for n in levels_out}
    state: dict[int, Any] = {}
    rows = iter(rows)
    nxt = next(rows, None)
    n_events = 0
    for i, t, q in events:
        n_events += 1
        if mode == "depth":
            st = state.setdefault(i, {66: [(UNDEF, 0)] * nmax, 65: [(UNDEF, 0)] * nmax})
            while nxt is not None and nxt[0] == i and nxt[1] == q:
                _, _, s, lv, p, sz = nxt
                if lv < nmax:
                    st[s][lv] = (UNDEF, 0) if p is None else (p, sz)
                nxt = next(rows, None)
            bids, asks = st[66], st[65]
        else:
            st = state.setdefault(i, {66: {}, 65: {}})
            while nxt is not None and nxt[0] == i and nxt[1] == q:
                _, _, k, s, p, nq = nxt
                if k == "clear":
                    st[66].clear()
                    st[65].clear()
                elif k != "trade" and p is not None:
                    if nq == 0:
                        st[s].pop(p, None)
                    else:
                        st[s][p] = nq
                nxt = next(rows, None)
            bids = sorted(st[66].items(), reverse=True)[:nmax]
            asks = sorted(st[65].items())[:nmax]
            bids += [(UNDEF, 0)] * (nmax - len(bids))
            asks += [(UNDEF, 0)] * (nmax - len(asks))
        for n in levels_out:
            body = "".join(f",{bids[k][0]},{bids[k][1]},{asks[k][0]},{asks[k][1]}" for k in range(n))
            h = hs[n].get(i)
            if h is None:
                h = hs[n][i] = hashlib.sha256()
            h.update(f"{t},{i},{q}{body}\n".encode())
    leftover = 0 if nxt is None else 1 + sum(1 for _ in rows)
    return {
        "digests": {n: {str(k): v.hexdigest() for k, v in sorted(hs[n].items())} for n in levels_out},
        "unconsumed_rows": leftover,
        "events": n_events,
    }


# ---- RD checks -------------------------------------------------------------------------------------------------


def rd_ingest(ctx: Ctx, date: str, rd: str) -> Check:
    c = Check(rd, f"ingest etcsv {date}")
    e = ctx.exp["days"][date]["ingest"]
    _, s = ingest_etcsv(ctx, date)
    man = s["manifest"]
    c.eq("partitions", len(man["partitions"]), e["partitions"])
    c.eq("instrument_ids", sorted(p["instrument_id"] for p in man["partitions"]), e["instrument_ids"])
    c.eq("records", sum(p["rows"] for p in man["partitions"]), e["records"])
    c.eq("rows_by_instrument", {str(p["instrument_id"]): p["rows"] for p in man["partitions"]}, e["rows_by_instrument"])
    c.eq(
        "ts_min_by_instrument",
        {str(p["instrument_id"]): p["ts_min"] for p in man["partitions"]},
        e["ts_min_by_instrument"],
    )
    c.eq(
        "ts_max_by_instrument",
        {str(p["instrument_id"]): p["ts_max"] for p in man["partitions"]},
        e["ts_max_by_instrument"],
    )
    c.eq("data_quality.records", s["data_quality"]["records"], e["records"])
    c.eq("venue", man["venue"], "nasdaq")
    c.eq("validation_status", man["validation_status"], "real_data")
    inst = pq.read_table(ctx.ds[date] / "instruments.parquet").to_pylist()
    c.eq("instruments midnight_utc_ns", sorted({r["midnight_utc_ns"] for r in inst}), [DAYS[date]["midnight"]])
    c.eq("instruments open_s/close_s", sorted({(r["open_s"], r["close_s"]) for r in inst}), [(34200, 57600)])
    c.eq("instruments tick_size", sorted({r["tick_size"] for r in inst}), [10_000_000])
    c.eq("instruments price_exp", sorted({r["price_exp"] for r in inst}), [-9])
    c.eq("symbols", {str(r["instrument_id"]): r["symbol"] for r in inst}, ctx.exp["days"][date]["instruments"])
    ts_files = tables(ctx.ds[date] / "trades_source", "qt.trades") if (ctx.ds[date] / "trades_source").exists() else []
    c.eq("trades_source rows", sum(pq.ParquetFile(p).metadata.num_rows for p in ts_files), e["trades_source_rows"])
    dq = s["data_quality"]
    c.record(
        data_quality={k: dq[k] for k in dq if k not in ("schema", "schema_version", "feed_gaps")},
        hidden_prints_expected_side_N=e["side_N_trade_prints"],
        dataset_id=man["dataset_id"],
        dataset_mb=du_mb(ctx.ds[date]),
        wall_s=ctx.timings[f"ingest_{date}"],
        records_per_s=round(e["records"] / ctx.timings[f"ingest_{date}"]),
    )
    if dq["verdict"] != "clean":
        c.notes.append(
            f"verdict {dq['verdict']}: {dq['reasons']} (et counters are all 0; side-N prints "
            f"{e['side_N_trade_prints']} may be counted as hidden_prints)"
        )
    return c


def rd_replay_exact(ctx: Ctx, date: str, rd: str) -> Check:
    """RD-2 / RD-6 counters + RD-3 BBO identity + RD-4 rank_trace identity (one replay)."""
    c = Check(rd, f"replay rank oracle + bbo full + trade parity + bbo parity {date}")
    E = ctx.exp["days"][date]
    out, s = replay(
        ctx,
        f"exact-{date}",
        ctx.ds[date],
        rank_oracle=True,
        sweep_gap_ns=2_000_000,
        bbo="full",
        parity_trades=True,
        parity_bbo={"path": str(ctx.et_path(date, "replay", "bbo.csv"))},
    )
    st, er = s["stats"], E["replay"]
    for k in ("records", "events", "trades"):
        c.eq(f"stats.{k}", st[k], er[k])
    c.eq("stats.partitions", st["partitions"], E["ingest"]["partitions"])
    c.eq("invariants (8, all 0)", {k: s["invariants"][k] for k in INV8}, er["invariants"])
    ro, eo = s["rank_oracle"], er["rank_oracle"]
    for k in (
        "executions_checked",
        "executions_unknown_order",
        "price_priority_violations",
        "fifo_rank_histogram",
        "fifo_rank_histogram_sweep_adjusted",
        "sweeps",
        "sweeps_multi_fill",
        "sweep_gap_ns",
    ):
        c.eq(f"rank_oracle.{k}", ro[k], eo[k])
    c.eq("rank0_share", ro["rank0_share"], eo["rank0_share"], tol=1e-12)
    c.eq("rank0_share_sweep_adjusted", ro["rank0_share_sweep_adjusted"], eo["rank0_share_sweep_adjusted"], tol=1e-12)
    f60 = {k: v for k, v in ro["first_60s_after_open"].items()}
    c.eq(
        "first_60s_after_open counts",
        {k: f60[k] for k in ("checked", "rank_violations", "rank_violations_sweep_adjusted")},
        {k: eo["first_60s_after_open"][k] for k in ("checked", "rank_violations", "rank_violations_sweep_adjusted")},
    )
    c.eq(
        "rank0_share_by_bucket (counts)",
        {k: (v["checked"], v["rank0"], v["rank0_sweep_adjusted"]) for k, v in ro["rank0_share_by_bucket"].items()},
        {k: (v["checked"], v["rank0"], v["rank0_sweep_adjusted"]) for k, v in eo["rank0_share_by_bucket"].items()},
    )
    p = s["parity"]
    for k, v in er["parity_trades"].items():
        c.eq(f"parity.{k}", p[k], v)
    c.eq("parity.bbo_compared (engine, et bbo.csv)", p["bbo_compared"], E["bbo_full"]["rows"])
    c.eq("parity.bbo_mismatches", p["bbo_mismatches"], 0)
    c.eq("parity.pass", p["pass"], True)
    # RD-3: independent Python rendering of qt.bbo full -> et rows, per-instrument sha256
    t0 = time.perf_counter()
    dig, n = bbo_digests(find_table(out, "qt.bbo"))
    c.eq("RD-3 bbo rows_by_instrument", n, E["bbo_full"]["rows_by_instrument"])
    c.eq("RD-3 bbo per-instrument sha256 (et render)", dig, E["bbo_full"]["bbo_rows_sha256"])
    # RD-4: rank_trace identity per instrument, sweep ids compared as a grouping
    rel, nos, rn = rank_digests(find_table(out, "qt.rank_trace"))
    c.eq("RD-4 rank_trace rows_by_instrument", rn, E["rank_trace"]["rows_by_instrument"])
    c.eq("RD-4 rank_trace sha256 (sweep ids relabelled)", rel, E["rank_trace"]["rank_rows_sha256_relabelled"])
    if rel != E["rank_trace"]["rank_rows_sha256_relabelled"]:
        c.eq(
            "RD-4 rank_trace sha256 without sweep_id (localizes the diff)",
            nos,
            E["rank_trace"]["rank_rows_sha256_no_sweep"],
        )
    c.record(
        render_check_s=round(time.perf_counter() - t0, 1),
        wall_s=ctx.timings[f"replay_exact-{date}"],
        replay_out_mb=du_mb(out),
        timings=s.get("timings"),
    )
    if not ctx.keep:
        find_table(out, "qt.bbo").unlink()  # full BBO is the bulk of the run; summary + rank trace stay
    return c


def trace_from_qt(traj_path: Path, fills_path: Path, results: dict[str, dict]) -> dict[str, list[tuple[int, int]]]:
    """Map qt.queue_trajectory (mode ahead) to et queue_trace semantics (see expected.json queue_trace_semantics):
    keep the live row unless the shadow filled at activation (crossed_at_live) or was cancelled at the start of its
    activating event (ts_cancel_effective < ts_activated_event), keep event rows except the event that fully filled
    the shadow, drop end rows, then drop consecutive repeats of queue_ahead."""
    full_seq: dict[str, int] = {}
    fcols = ["shadow_id", "sequence", "leaves_qty"]
    for b in pq.ParquetFile(fills_path).iter_batches(columns=fcols):
        for sid, q, lv in zip(strs(b.column(0)), ints(b.column(1)), ints(b.column(2)), strict=False):
            if lv == 0:
                full_seq[sid] = q
    rows: dict[str, list[tuple[int, int]]] = defaultdict(list)
    cols = ["shadow_id", "ts_event", "sequence", "kind", "queue_ahead_after"]
    for b in pq.ParquetFile(traj_path).iter_batches(batch_size=1 << 20, columns=cols):
        for sid, ts, q, k, qa in zip(
            strs(b.column(0)), ints(b.column(1)), ints(b.column(2)), strs(b.column(3)), ints(b.column(4)), strict=False
        ):
            r = results.get(sid)
            if k == "end":
                continue
            if (
                k == "live"
                and r is not None
                and (
                    r["first_fill_reason"] == "crossed_at_live"
                    or (
                        r["end_state"] == "cancelled"
                        and r.get("ts_cancel_effective") is not None
                        and r.get("ts_activated_event") is not None
                        and r["ts_cancel_effective"] < r["ts_activated_event"]
                    )
                )
            ):
                continue  # et never traces a shadow that is done at the end of its activating event start
            if k == "event" and full_seq.get(sid) == q:
                continue
            lst = rows[sid]
            if not lst or lst[-1][1] != qa:
                lst.append((ts, qa))
    return rows


def rd_shadows(ctx: Ctx, date: str, rd: str) -> Check:
    """RD-5 / RD-6 shadows: et's 3,000 mirror shadows replayed; per-shadow equality with et shadow_fills.csv and
    trajectory equality with et queue_trace.csv."""
    c = Check(rd, f"et mirror shadows {date}")
    E = ctx.exp["days"][date]["shadows_mirror_et"]
    shadows_csv = ctx.et_path(date, "fidelity", "shadows.csv")
    out, s = replay(
        ctx, f"shadows-{date}", ctx.ds[date], shadows={"path": str(shadows_csv)}, trajectory="ahead", bbo="none"
    )
    sh = s["shadows"]
    c.eq("shadows.n", sh["n"], E["n"])
    c.eq("shadows.n_live", sh["n_live"], E["n_live"])
    # et counts a shadow as filled on its first fill; qt splits fully filled (n_filled) and partial (n_partial_only)
    c.eq("shadows.n_filled + n_partial_only (et: any fill)", sh["n_filled"] + sh["n_partial_only"], E["n_filled"])
    c.eq("shadows.n_partial_only", sh["n_partial_only"], E["n_partial_only"])
    compare_shadow_tables(
        c,
        E,
        find_table(out, "qt.shadow_results"),
        find_table(out, "qt.shadow_fills"),
        find_table(out, "qt.queue_trajectory"),
        ctx.et_path(date, "fidelity", "engine", "shadow_fills.csv"),
        ctx.et_path(date, "fidelity", "engine", "queue_trace.csv"),
    )
    c.record(
        wall_s=ctx.timings[f"replay_shadows-{date}"],
        trajectory_rows_qt=sh["trajectory_rows"],
        fills_rows=sh["fills_rows"],
    )
    ctx.shadows[f"et-mirror-{date}"] = shadows_csv
    return c


def read_rows(path: Path, cols: list[str] | None = None) -> list[dict]:
    """Parquet -> list of dicts with timestamps as int ns and dictionary columns as str (to_pylist alone would turn
    timestamp[ns] into datetimes and lose nanoseconds)."""
    t = pq.read_table(path, columns=cols)
    arrs = {}
    for name in t.column_names:
        col = t[name]
        if pa.types.is_timestamp(col.type):
            col = pc.cast(col, pa.int64())
        elif pa.types.is_dictionary(col.type):
            col = pc.cast(col, pa.utf8())
        arrs[name] = col.to_pylist()
    return [dict(zip(arrs, vals, strict=False)) for vals in zip(*arrs.values(), strict=False)]


FILL_COLS = [
    "shadow_id",
    "fill_seq",
    "ts_event",
    "sequence",
    "fill_qty",
    "cum_qty",
    "leaves_qty",
    "reason",
    "queue_ahead_before",
    "traded_at_px_event",
    "trade_through_event",
]


def fills_digest(fills_path: Path) -> tuple[str, int]:
    """sha256 over qt.shadow_fills rows (FILL_COLS, comma-joined, sorted by shadow_id then fill_seq)."""
    rows = sorted(iter_cols([fills_path], FILL_COLS, dict_cols=("reason",)), key=lambda r: (r[0], r[1]))
    h = hashlib.sha256()
    for r in rows:
        h.update((",".join(map(str, r)) + "\n").encode())
    return h.hexdigest(), len(rows)


def compare_shadow_tables(
    c: Check, E: dict, results_path: Path, fills_path: Path, traj_path: Path, et_fills_csv: Path, et_trace_csv: Path
) -> None:
    """Per-shadow equality with et shadow_fills.csv, per-fill consistency, and trajectory equality with et
    queue_trace.csv (et semantics). Shared by RD-5/RD-6 and by et_shadow_reference.py (driver self-test)."""
    res = {r["shadow_id"]: r for r in read_rows(results_path)}
    et_rows = {}
    with open(et_fills_csv) as f:
        f.readline()
        for ln in f:
            p = ln.rstrip("\n").split(",")
            et_rows[p[0]] = p
    rc = {
        "queue_depleted": "queue_depleted",
        "trade_through": "crossed",
        "crossed": "crossed",
        "crossed_at_live": "crossed",
        "never": "never",
    }

    def ns(x):
        return x

    mism, h = [], hashlib.sha256()
    for sid in sorted(res):
        r, e = res[sid], et_rows.get(sid)
        q = {
            "ts_live": ns(r["ts_live"]),
            "ts_first_fill": ns(r["ts_first_fill"]),
            "filled_qty": r["filled_qty"],
            "queue_ahead_at_live": r["queue_ahead_at_live"],
            "queue_ahead_at_first_fill": r["queue_ahead_at_first_fill"],
            "reason": rc[r["first_fill_reason"]],
        }
        h.update(
            (
                f"{sid},{q['ts_live']},{'' if q['ts_first_fill'] is None else q['ts_first_fill']},{q['filled_qty']},"
                f"{'' if q['queue_ahead_at_live'] is None else q['queue_ahead_at_live']},"
                f"{'' if q['queue_ahead_at_first_fill'] is None else q['queue_ahead_at_first_fill']},{q['reason']}\n"
            ).encode()
        )
        if e is None:
            mism.append({"shadow_id": sid, "missing_in_et": True})
            continue
        w = {
            "ts_live": int(e[6]),
            "ts_first_fill": int(e[7]) if e[7] else None,
            "filled_qty": int(e[8]),
            "queue_ahead_at_live": int(e[9]) if e[9] else None,
            "queue_ahead_at_first_fill": int(e[10]) if e[10] else None,
            "reason": e[11],
        }
        if q != w:
            mism.append({"shadow_id": sid, "qt": q, "et": w})
    c.eq("per-shadow mismatches vs et shadow_fills.csv", len(mism), 0)
    if mism:
        c.notes.append(f"first mismatches: {mism[:20]}")
    c.eq("shadow digest (et convention)", h.hexdigest(), E["shadow_digest"])
    ft = pq.read_table(fills_path, columns=["shadow_id", "fill_seq", "fill_qty", "cum_qty"])
    sums: dict[str, int] = defaultdict(int)
    mono_bad = 0
    last: dict[str, int] = {}
    for sid, _fs, fq, cq in zip(
        strs(ft["shadow_id"]), ints(ft["fill_seq"]), ints(ft["fill_qty"]), ints(ft["cum_qty"]), strict=False
    ):
        sums[sid] += fq
        if cq <= last.get(sid, 0):
            mono_bad += 1
        last[sid] = cq
    c.eq(
        "sum(fill_qty) == filled_qty for every shadow",
        sum(1 for sid, r in res.items() if sums.get(sid, 0) != r["filled_qty"]),
        0,
    )
    c.eq("cum_qty strictly increasing", mono_bad, 0)
    c.eq("sum filled_qty", sum(r["filled_qty"] for r in res.values()), E["sum_filled_qty"])
    # trajectory vs et queue_trace.csv
    traj = trace_from_qt(traj_path, fills_path, res)
    et_tr: dict[str, list[tuple[int, int]]] = defaultdict(list)
    with open(et_trace_csv) as f:
        f.readline()
        for ln in f:
            ts, sid, qa = ln.rstrip("\n").split(",")
            et_tr[sid].append((int(ts), int(qa)))
    bad = [sid for sid in sorted(set(et_tr) | set(traj)) if et_tr.get(sid, []) != traj.get(sid, [])]
    c.eq("trajectory rows (et semantics)", sum(len(v) for v in traj.values()), E["queue_trace_rows"])
    c.eq("shadows whose trajectory differs from et queue_trace", len(bad), 0)
    if bad:
        c.notes.append(f"first trajectory diffs: {[(s, et_tr.get(s, [])[:6], traj.get(s, [])[:6]) for s in bad[:5]]}")
    ht = hashlib.sha256()
    for sid in sorted(traj):
        for ts, qa in traj[sid]:
            ht.update(f"{sid},{ts},{qa}\n".encode())
    c.eq("queue_trace digest", ht.hexdigest(), E["queue_trace_digest"])
    ref = E.get("spec_reference")
    if ref:  # per-fill timeline and end states predicted by et_shadow_reference.py (SPEC 5.2 in Python)
        dg, nrows = fills_digest(fills_path)
        c.eq("shadow_fills rows == SPEC reference", nrows, ref["fills_rows"])
        c.eq("shadow_fills digest == SPEC reference (per-fill timeline)", dg, ref["fills_digest"])
        c.eq("end_state counts == SPEC reference", _count(r["end_state"] for r in res.values()), ref["end_states"])
        c.eq(
            "first_fill_reason counts == SPEC reference",
            _count(r["first_fill_reason"] for r in res.values()),
            ref["first_fill_reasons"],
        )
    c.record(
        end_states=_count(r["end_state"] for r in res.values()),
        first_fill_reasons=_count(r["first_fill_reason"] for r in res.values()),
        fills_rows_total=ft.num_rows,
        n_partial_only=sum(1 for r in res.values() if 0 < r["filled_qty"] < r["qty"]),
    )


def _count(it) -> dict:
    d: dict[str, int] = defaultdict(int)
    for x in it:
        d[str(x)] += 1
    return dict(sorted(d.items()))


def rd_l2(ctx: Ctx, rd: str) -> Check:
    """RD-7 on real data, two ways:
    (a) qt.depth (from qt_book, depth 5) rendered after every event equals et depth.csv (top 3 both days from the
        fidelity pass-1 run; top 5 day two from the replay run): book top-N identical to et after every event;
    (b) the qt.l2 VisibleBook (L2Builder, independent of qt_book, depth 5, cadence 0) rendered the same way equals the
        same et digests: L2 view == exact book top-N after every event (O-L2-1/2 restricted to the visible levels).
    The full-depth O-L2-1 (aggregate_level == Book::level at every price) is a Rust-side check (`qt_l2` tests)."""
    c = Check(rd, "L2 view and depth vs et book depth on both days (16.75M records)")
    for date in (DAY1, DAY2):
        E = ctx.exp["days"][date]
        if "depth" not in E:
            c.status = "skipped"
            c.notes.append("expected.json built with --no-depth")
            return c
        out, _s = replay(
            ctx,
            f"depth-l2-{date}",
            ctx.ds[date],
            bbo="full",
            depth_levels=5,
            l2={
                "depth_levels": 5,
                "cadence_ns": 0,
                "trade_position": "before",
                "include_counts": True,
                "emit_hidden_trades": True,
            },
        )
        bbo = find_table(out, "qt.bbo")
        a = render_wide(
            iter_events(bbo),
            iter_cols([find_table(out, "qt.depth")], ["instrument_id", "sequence", "side", "level", "price", "size"]),
            [3, 5],
            mode="depth",
        )
        b = render_wide(
            iter_events(bbo),
            iter_cols(
                l2_files_in_partition_order(out / "l2"),
                ["instrument_id", "sequence", "kind", "side", "price", "new_qty"],
                dict_cols=("kind",),
            ),
            [3, 5],
            mode="l2",
        )
        c.eq(f"{date} qt.depth top-3 == et depth.csv (3 levels)", a["digests"][3], E["depth"]["depth3_rows_sha256"])
        c.eq(f"{date} qt.l2 view top-3 == et depth.csv (3 levels)", b["digests"][3], E["depth"]["depth3_rows_sha256"])
        if "depth5_rows_sha256" in E["depth"]:
            c.eq(f"{date} qt.depth top-5 == et depth.csv (5 levels)", a["digests"][5], E["depth"]["depth5_rows_sha256"])
            c.eq(
                f"{date} qt.l2 view top-5 == et depth.csv (5 levels)", b["digests"][5], E["depth"]["depth5_rows_sha256"]
            )
        c.eq(
            f"{date} unconsumed depth/l2 rows (sequence alignment)",
            (a["unconsumed_rows"], b["unconsumed_rows"]),
            (0, 0),
        )
        c.eq(f"{date} events rendered", a["events"], sum(E["replay"]["events_by_instrument"].values()))
        c.record(
            **{
                f"{date}_events": a["events"],
                f"{date}_wall_s": ctx.timings[f"replay_depth-l2-{date}"],
                f"{date}_out_mb": du_mb(out),
            }
        )
        if not ctx.keep:
            shutil.rmtree(out)
    return c


def merged_dataset(ctx: Ctx) -> Path:
    if "2d" in ctx.ds:
        return ctx.ds["2d"]
    sys.path.insert(0, str(HERE))
    from merge_datasets import merge  # local helper (scripts/realdata/merge_datasets.py)

    out = ctx.fresh("ds", "nasdaq-2d")
    merge([ctx.ds[DAY1], ctx.ds[DAY2]], out)
    ctx.ds["2d"] = out
    return out


def sample(ctx: Ctx, name: str, dataset: Path, n: int, seed: int = 0) -> Path:
    qt, S = qt_mod()
    out = ctx.dir("shadows", f"{name}.parquet")
    spec = S.ShadowSampleSpec(dataset={"path": str(dataset)}, out=str(out), n=n, seed=seed)  # QT-API
    summ = ctx.timed(f"sample_{name}", lambda: qt.sample_shadows(spec))  # QT-API qt.sample_shadows
    ctx.shadows[name] = out
    ctx.results.setdefault("_samples", {})[name] = {
        k: v for k, v in to_dict(summ).items() if k in ("n", "by_instrument", "by_placement")
    }
    return out


def run_zoo(
    ctx: Ctx, name: str, dataset: Path, shadows: Path, mode: str, models: list[str] | None = None, **kw: Any
) -> tuple[Path, dict]:
    qt, S = qt_mod()
    out = ctx.fresh("zoo", name)
    spec = S.ZooSpec(
        dataset={"path": str(dataset)},
        shadows={"path": str(shadows)},
        out=str(out),
        mode=mode,
        models=models or ["all"],
        threads=ctx.threads,
        **kw,
    )  # QT-API ZooSpec
    summ = ctx.timed(f"zoo_{name}", lambda: qt.run_zoo(spec))  # QT-API qt.run_zoo
    return out, to_dict(summ)


def rd_zoo(ctx: Ctx, rd: str) -> Check:
    c = Check(rd, "zoo day one: 3,000 sampled + 3,000 et mirror shadows, 16 models, faithful and corrected")
    s_samp = sample(ctx, "d1-sampled-3000", ctx.ds[DAY1], 3000, seed=0)
    s_mirror = ctx.et_path(DAY1, "fidelity", "shadows.csv")
    boards = {}
    for sname, spath in (("sampled", s_samp), ("et_mirror", s_mirror)):
        for mode in ("faithful", "corrected"):
            name = f"d1-{sname}-{mode}"
            out, s = run_zoo(ctx, name, ctx.ds[DAY1], spath, mode)
            pred = pq.read_table(find_table(out, "qt.zoo_predictions"))
            h_max = max(ints(pred["horizon_ns"]))
            mids = sorted(set(strs(pred["model_id"])))
            c.eq(f"{name}: 16 models", len(mids), 16)
            at = pred.filter(pc.equal(pred["horizon_ns"], h_max))
            m_ids, pf, tf = strs(at["model_id"]), at["p_fill"].to_pylist(), at["truth_filled"].to_pylist()
            sids = strs(at["shadow_id"])
            filled = defaultdict(set)
            truth = set()
            for mid, sid, p, t in zip(m_ids, sids, pf, tf, strict=False):
                if p is not None and p >= 0.5:
                    filled[mid.split("[")[0]].add(sid)
                if t:
                    truth.add(sid)
            fq, tt = filled.get("front_of_queue", set()), filled.get("trade_through_only", set())
            c.true(
                f"{name}: bracket counts front_of_queue >= exact >= trade_through_only",
                len(fq) >= len(truth) >= len(tt),
                (len(fq), len(truth), len(tt)),
            )
            c.record(
                **{
                    f"{name}_bracket_subset_violations": {
                        "exact_not_in_front": len(truth - fq),
                        "tto_not_in_exact": len(tt - truth),
                    }
                }
            )
            if mode == "corrected":
                l3 = pred.filter(pc.starts_with(pc.cast(pred["model_id"], pa.utf8()), "hbt_l3_fifo"))
                diff = 0
                for p, t, mt, tt_, eq_, tq in zip(
                    l3["p_fill"].to_pylist(),
                    l3["truth_filled"].to_pylist(),
                    ints(l3["model_ts_first_fill"]),
                    ints(l3["truth_ts_first_fill"]),
                    l3["e_fill_qty"].to_pylist(),
                    l3["truth_filled_qty"].to_pylist(),
                    strict=False,
                ):
                    if (p >= 0.5) != bool(t) or (t and mt != tt_) or (tq is not None and eq_ != tq):
                        diff += 1
                c.eq(f"{name}: hbt_l3_fifo corrected == exact (rows differing)", diff, 0)
            boards[name] = s["leaderboard"]
            err = find_table(out, "qt.zoo_errors")
            c.record(
                **{
                    f"{name}_wall_s": ctx.timings[f"zoo_{name}"],
                    f"{name}_out_mb": du_mb(out),
                    f"{name}_zoo_errors": str(err.relative_to(ctx.out)),
                }
            )
    c.record(leaderboards=boards, sample=ctx.results.get("_samples", {}).get("d1-sampled-3000"))
    if c.status == "pass":
        c.status = "recorded"
    return c


def rd_calibrate(ctx: Ctx, rd: str) -> Check:
    qt, S = qt_mod()
    c = Check(rd, "calibration: fit day one blocks 0-3 of 5, held-out day two")
    ds2 = merged_dataset(ctx)
    sh = ctx.shadows.get("2d-sampled-6000") or sample(ctx, "2d-sampled-6000", ds2, 6000, seed=0)
    train = {"dates": [DAY1], "blocks": {"index": [0, 1, 2, 3], "n_blocks": 5}}
    test = {"dates": [DAY2]}
    jobs = [
        ("alloc", "power3"),
        ("alloc", "databento"),
        ("nt_touch", "nt_prob_touch"),
        ("nt_touch", "nt_queue_l2_prob_touch"),
    ]
    if ctx.results.get("_with_survival"):
        jobs.append(("survival", "databento"))
    for kind, fam in jobs:
        out = ctx.fresh("calib", f"{kind}-{fam}")
        spec = S.CalibrationSpec(
            kind=kind,
            family=fam,
            dataset={"path": str(ds2)},
            shadows={"path": str(sh)},
            train=train,
            test=test,
            out=str(out),
            threads=ctx.threads,
        )  # QT-API CalibrationSpec
        r = to_dict(ctx.timed(f"calib_{kind}_{fam}", lambda: qt.calibrate(spec)))  # QT-API qt.calibrate  # noqa: B023
        c.true(f"{kind}/{fam}: finite estimate", r.get("estimate") is not None, r.get("estimate"))
        c.true(f"{kind}/{fam}: bootstrap CI present", r.get("ci_bootstrap") is not None, r.get("ci_bootstrap"))
        c.record(
            **{
                f"{kind}/{fam}": {
                    k: r.get(k)
                    for k in (
                        "param_name",
                        "estimate",
                        "ci_profile",
                        "ci_bootstrap",
                        "n_events",
                        "n_shadows",
                        "held_out",
                        "baseline_held_out",
                        "stratified",
                        "warnings",
                    )
                },
                f"{kind}/{fam}_wall_s": ctx.timings[f"calib_{kind}_{fam}"],
            }
        )
        if kind == "alloc" and fam == "power3":
            for tgt in ("hftbacktest", "nautilus"):
                cal_json = out / "calibration.json"
                es = S.ExportSpec(
                    target=tgt, calibration={"path": str(cal_json)}, out=str(out / f"export_{tgt}.py")
                )  # QT-API ExportSpec
                from queuetruth.errors import QtError

                try:
                    e = to_dict(qt.export(es))  # QT-API qt.export
                    c.record(**{f"export_{tgt}_refused": None})
                except QtError as err:  # QT-CALIB-001: fitted value does not beat the default out of sample
                    c.record(**{f"export_{tgt}_refused": {"code": err.code, "message": str(err)[:600]}})
                    e = to_dict(qt.export(es, force=True))  # QT-API qt.export (NOT VALIDATED header)
                c.record(**{f"export_{tgt}_files": e.get("files")})
    if c.status == "pass":
        c.status = "recorded"
    return c


def rd_learned(ctx: Ctx, rd: str) -> Check:
    qt, S = qt_mod()
    c = Check(rd, "learned hazard model: 20,000 shadows per day, split auto (test = day two)")
    ds2 = merged_dataset(ctx)
    sh = sample(ctx, "2d-sampled-40000", ds2, 40000, seed=0)
    fout = ctx.fresh("features", "2d-40000")
    fspec = S.FeatureSpec(
        dataset={"path": str(ds2)},
        out=str(fout),
        population="shadows",
        shadows={"path": str(sh)},
        privileged=True,
        threads=ctx.threads,
    )  # QT-API FeatureSpec
    fs = to_dict(ctx.timed("features_2d_40000", lambda: qt.extract_features(fspec)))  # QT-API qt.extract_features
    c.record(
        features={k: fs.get(k) for k in ("n_orders", "n_rows", "by_cause")},
        features_wall_s=ctx.timings["features_2d_40000"],
    )
    # zoo predictions on the same shadows for zoo_comparison (a few models; full 17 is RD-8)
    zmodels = ctx.results.get("_rd10_zoo_models") or [
        "hbt_risk_adverse",
        "hbt_power3[n=2]",
        "nt_queue_l2",
        "front_of_queue",
        "trade_through_only",
    ]
    zout, _ = run_zoo(
        ctx,
        "2d-40000-subset",
        ds2,
        sh,
        "faithful",
        models=zmodels,
        horizons_ns=[100_000_000, 250_000_000, 500_000_000, H1S, 2 * H1S, 5 * H1S, H10S, 30 * H1S, H60S],
    )
    zpred = find_table(zout, "qt.zoo_predictions")
    for priv in (False, True):
        hout = ctx.fresh("hazard", f"2d-40000-{'priv' if priv else 'l2'}")
        hspec = S.HazardFitSpec(
            features={"path": str(fout)}, out=str(hout), privileged=priv, zoo_predictions={"path": str(zpred)}
        )  # QT-API HazardFitSpec
        card = to_dict(
            ctx.timed(f"fit_hazard_{priv}", lambda: qt.fit_hazard(hspec))  # noqa: B023
        )  # QT-API qt.fit_hazard
        sc = card["scores_test"]
        bss = sc["bss_vs_climatology"]
        tag = "privileged" if priv else "l2_only"
        c.record(
            **{
                f"{tag}_split": card["split"],
                f"{tag}_n_orders": card["n_orders"],
                f"{tag}_scores_test": sc,
                f"{tag}_zoo_comparison": card.get("zoo_comparison"),
                f"{tag}_wall_s": ctx.timings[f"fit_hazard_{priv}"],
            }
        )
        if not priv:
            for h in (H1S, H10S):
                v = bss.get(str(h)) or {}
                c.true(
                    f"BSS vs climatology lower CI > 0 at {h // H1S} s (L2-only model)", (v.get("ci_lo") or -1) > 0, v
                )
            c.true(
                "split: test = day two",
                card["split"].get("test") in ([DAY2], {"dates": [DAY2]})
                or DAY2 in json.dumps(card["split"].get("test")),
                card["split"],
            )
    c.notes.append(
        "et comparable (real orders, different protocol): post-isotonic BSS +0.092/+0.094/+0.094/+0.091/+0.089 "
        "at 1/5/10/30/60 s on day one, +0.073..+0.076 on day two (expected.json learned_model_et_reference)"
    )
    if c.status == "pass":
        c.status = "recorded"
    return c


def rd_score(ctx: Ctx, rd: str) -> Check:
    """RD-11: each real order of et's 3,000-order sample becomes a live order; with self-exclusion (A1) the
    order's MBO twin is replaced by its own shadow, so the exact arm must reproduce the real order's own fills: zero
    discordance and, per order, the same filled quantity and first-fill time, on both days. et's fidelity 2x2 (et
    fill rule, its twin left in the tape, no evaluation window) is recorded next to the no-exclusion 2x2; it is not
    the target."""
    qt, S = qt_mod()
    c = Check(rd, "scorecard on 3,000 real orders per day (et sample), exact arm with self-exclusion")
    live = None
    for day, tag in ((DAY1, "d1"), (DAY2, "d2")):
        lv = ctx.fresh("live", f"{tag}-et3000")
        cmd = [
            sys.executable,
            str(HERE / "real_orders_to_live.py"),
            "--fidelity",
            str(ctx.et_path(day, "fidelity")),
            "--dataset",
            str(ctx.ds[day]),
            "--out",
            str(lv),
        ]
        info = json.loads(
            subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip().splitlines()[-1]
        )
        msgs = qt.validate_live(str(lv))  # QT-API qt.validate_live (raises on errors)
        se_field = {"self_exclude": True} if "self_exclude" in S.ScorecardSpec.model_fields else {}
        out = ctx.fresh("score", f"{tag}-exact")
        spec = S.ScorecardSpec(
            live={"path": str(lv)},
            sim={"kind": "exact"},
            dataset={"path": str(ctx.ds[day])},
            out=str(out),
            entry="exchange_ack",
            **se_field,
        )  # QT-API ScorecardSpec
        s = to_dict(ctx.timed(f"score_exact_{tag}", lambda: qt.score(spec, self_exclude=True)))  # noqa: B023
        off = to_dict(
            qt.score(
                spec.model_copy(update={"out": None, **({"self_exclude": False} if se_field else {})}),
                self_exclude=False,
            )
        )  # QT-API qt.score (twin left in the tape: et's mirror protocol)
        so = pq.read_table(find_table(out, "qt.score_orders")).to_pylist()
        same = sum(
            o["live_filled_qty"] == o["sim_filled_qty"] and o["live_ts_first_fill"] == o["sim_ts_first_fill"]
            for o in so
        )
        early = sum(
            1
            for o in so
            if o["live_ts_first_fill"] is not None
            and o["sim_ts_first_fill"] is not None
            and o["sim_ts_first_fill"] < o["live_ts_first_fill"]
        )
        tb = s["two_by_two"]
        c.eq(f"{day}: self-excluded exact arm discordant orders", tb["live_only"] + tb["sim_only"], 0)
        c.eq(f"{day}: orders with the live filled qty and first-fill time", same, len(so))
        c.eq(f"{day}: no sim first fill before the live first fill", early, 0)
        c.record(
            **{
                f"{tag}_live_log": info,
                f"{tag}_live_validate_warnings": msgs[:20],
                f"{tag}_exact_self_exclude_true": {
                    k: s.get(k) for k in ("n_orders", "n_matched", "two_by_two", "bias", "mcnemar", "verdict")
                },
                f"{tag}_self_exclusion_warnings": [
                    w for w in s.get("warnings", []) if "self_exclusion" in w or "exclude_order_ids" in w
                ][:6],
                f"{tag}_exact_self_exclude_false": {k: off.get(k) for k in ("two_by_two", "verdict")},
                f"{tag}_et_fidelity_2x2_reference": ctx.exp["days"][day]["fidelity_et"][
                    "two_by_two_expected_for_RD11_sim_exact"
                ],
                f"{tag}_exact_wall_s": ctx.timings[f"score_exact_{tag}"],
            }
        )
        if day == DAY1:
            live = lv
    c.notes.append(
        "et's fidelity 2x2 (235/2,739/26/0 and 195/2,789/16/0) uses et's fill rule with the twin left in the tape and "
        "no evaluation window; queuetruth's self-excluded exact arm must instead reproduce every real order's own "
        "fills (expected 250/2,750/0/0 and 195/2,805/0/0 under the 60 s window: the live 2x2 of the sample)"
    )
    models = ctx.results.get("_zoo_model_ids") or [
        "hbt_risk_adverse",
        "hbt_power[n=2]",
        "hbt_power3[n=2]",
        "hbt_log2",
        "nt_queue_l2",
        "nt_prob_touch[p=1]",
        "databento_mc[k=-0.8]",
        "front_of_queue",
        "trade_through_only",
    ]
    table = {}
    for mid in models:
        o2 = ctx.fresh("score", f"d1-zoo-{mid.replace('[', '_').replace(']', '').replace('=', '').replace(';', '_')}")
        sp = S.ScorecardSpec(
            live={"path": str(live)},
            sim={"kind": "zoo", "model": mid},
            dataset={"path": str(ctx.ds[DAY1])},
            out=str(o2),
            entry="exchange_ack",
        )  # QT-API
        r = to_dict(qt.score(sp))  # QT-API qt.score
        table[mid] = {
            k: r.get(k)
            for k in ("two_by_two", "bias", "mcnemar", "by_queue_decile", "by_liquidity", "markout_diff", "equivalence")
        }
        if not ctx.keep:
            shutil.rmtree(o2)
    c.record(zoo_bias_table=table)
    return c


def rd_crypto(ctx: Ctx, rd: str) -> Check:
    qt, S = qt_mod()
    c = Check(rd, "crypto captures: ingest + replay (Bitstamp, Bitfinex)")
    for venue, fmt, files, opts in (
        (
            "bitstamp",
            "bitstamp",
            ["live_orders.jsonl", "live_trades.jsonl", "detail_order_book.jsonl"],
            {"taker_window_us": 100},
        ),
        ("bitfinex", "bitfinex", ["book_R0.jsonl", "trades.jsonl"], {}),
    ):
        E = ctx.exp["crypto"][venue]
        cap = ctx.et / E["capture_dir"]  # relative to the et root (an absolute path is used as is)
        out = ctx.fresh("ds", venue)
        spec = S.IngestSpec(
            format=fmt,
            inputs=[{"path": str(cap / f)} for f in files],
            venue="generic",
            source_venue=venue,
            out=str(out),
            tz="UTC",
            price_exp=-9,
            size_exp=-8,
            options=opts,
        )  # QT-API IngestSpec (crypto)
        ing = to_dict(ctx.timed(f"ingest_{venue}", lambda: qt.ingest(spec)))  # QT-API qt.ingest  # noqa: B023
        rout, rs = replay(ctx, f"crypto-{venue}", out, bbo="change")
        inv = rs["invariants"]
        c.record(
            **{
                venue: {
                    "ingest_records": sum(p["rows"] for p in ing["manifest"]["partitions"]),
                    "replay_stats": rs["stats"],
                    "invariants": inv,
                    "data_quality": rs["data_quality"],
                    "et_converted": E["converted_mbo"],
                    "et_engine_stats": E["engine_stats"],
                    "et_verdict": E["verdict"],
                }
            }
        )
        if venue == "bitstamp":
            c.eq("bitstamp crossed_books_after_last == 0 (taker handling)", inv["crossed_books_after_last"], 0)
        same = rs["stats"]["records"] == E["converted_mbo"]["records"]
        c.notes.append(
            f"{venue}: qt records {rs['stats']['records']} vs et converted {E['converted_mbo']['records']}"
            f" ({'equal' if same else 'differs: converter port is not byte-faithful; record why'})"
        )
        if not ctx.keep:
            shutil.rmtree(rout)
    c.notes.append(
        "generic venue has sequence_meaningful = false: et's Bitstamp sequence_gaps 7,691 is not expected "
        "in qt counters"
    )
    if c.status == "pass":
        c.status = "recorded"
    return c


def rd_bench(ctx: Ctx, rd: str) -> Check:
    c = Check(rd, "performance on day one (SPEC 7.1)")
    E = ctx.exp["days"][DAY1]
    rec = E["replay"]["records"]
    b: dict[str, Any] = {
        "schema": "qtdev.bench",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "records_day1": rec,
        "timings_s": dict(ctx.timings),
    }
    target = Path(os.environ.get("CARGO_TARGET_DIR", HERE.parents[1] / "target"))
    built = [target / "release" / "queuetruth-core"]
    core = shutil.which("queuetruth-core") or next((str(b) for b in built if b.exists()), None)
    if core:
        r = subprocess.run(
            [core, "--format", "json", "bench", "replay", "--data", str(ctx.ds[DAY1]), "--repeat", "3"],
            capture_output=True,
            text=True,
        )  # QT-API CLI `bench replay` (SPEC 4.4)
        if r.returncode == 0:
            b["bench_replay"] = json.loads(r.stdout)
            c.true(
                "replay no sinks >= 8M rec/s",
                b["bench_replay"]["records_per_s_median"] >= 8e6,
                b["bench_replay"]["records_per_s_median"],
            )
        else:
            c.notes.append(f"bench replay failed: {r.stderr[-500:]}")
    else:
        c.notes.append("queuetruth-core not on PATH or in target/release: bench replay skipped")
    t = ctx.timings

    def rate(key: str) -> float | None:
        return round(rec / t[key]) if key in t and t[key] > 0 else None

    b["derived"] = {
        "etcsv_ingest_rec_per_s": rate(f"ingest_{DAY1}"),
        "replay_rank_oracle_bbo_full_parity_rec_per_s": rate(f"replay_exact-{DAY1}"),
        "replay_3000_shadows_trajectory_rec_per_s": rate(f"replay_shadows-{DAY1}"),
        "replay_depth5_l2_bbo_full_rec_per_s": rate(f"replay_depth-l2-{DAY1}"),
        "zoo_17x3000_faithful_s": t.get("zoo_d1-sampled-faithful"),
        "zoo_17x3000_corrected_s": t.get("zoo_d1-sampled-corrected"),
        "features_s": t.get("features_2d_40000"),
        "fit_hazard_s": t.get("fit_hazard_False"),
        "score_exact_s": t.get("score_exact"),
    }
    targets = {"etcsv_ingest_rec_per_s": 1.5e6}
    for k, v in targets.items():
        if b["derived"].get(k) is not None:
            c.true(f"{k} >= {v:g}", b["derived"][k] >= v, b["derived"][k])
    if (z := t.get("zoo_d1-sampled-faithful")) is not None:
        c.true(
            f"zoo 17 x 3,000 <= {'90' if ctx.threads >= 4 else '240'} s (threads={ctx.threads})",
            z <= (90 if ctx.threads >= 4 else 240),
            z,
        )
    b["peak_rss"] = peak_rss_mb()
    b["realdata_disk_mb"] = du_mb(ctx.out)
    b["et_reference"] = {
        "et_replay_day1_csv_rank_parity_s": 5.71,
        "et_replay_rec_per_s": 1.07e6,
        "et_run_fidelity_day1_s": 570,
        "et_run_fidelity_rss_gb": 3.0,
        "source": "et audit notes (not published), section 2.2",
    }
    b["note"] = (
        "SPEC 7.1 targets that need isolated runs (shadow overhead 3,000 vs 0 and 100,000 shadows, "
        "trajectory overhead, L2Builder alone, end-to-end <= 15 min / 2 GB RSS) are measured by `make bench`; "
        "this records what the RD pipeline itself observed"
    )
    (ctx.out / "rd13.json").write_text(jdump(b) + "\n")  # .realdata/bench.json belongs to `make bench`
    c.record(**b["derived"], peak_rss=b["peak_rss"], disk_mb=b["realdata_disk_mb"])
    if c.status == "pass":
        c.status = "recorded"
    return c


# ---- orchestration ---------------------------------------------------------------------------------------------

PLAN: list[tuple[str, Callable[[Ctx], Check], list[str]]] = [
    ("RD-1", lambda x: rd_ingest(x, DAY1, "RD-1"), []),
    ("RD-1b", lambda x: rd_ingest(x, DAY2, "RD-1b (day two ingest, part of RD-6)"), []),
    ("RD-2", lambda x: rd_replay_exact(x, DAY1, "RD-2 (+RD-3, RD-4)"), ["RD-1"]),
    ("RD-5", lambda x: rd_shadows(x, DAY1, "RD-5"), ["RD-1"]),
    ("RD-6", lambda x: rd_replay_exact(x, DAY2, "RD-6 (+RD-3, RD-4 day two)"), ["RD-1b"]),
    ("RD-6s", lambda x: rd_shadows(x, DAY2, "RD-6 shadows"), ["RD-1b"]),
    ("RD-7", lambda x: rd_l2(x, "RD-7"), ["RD-1", "RD-1b"]),
    ("RD-8", lambda x: rd_zoo(x, "RD-8"), ["RD-1"]),
    ("RD-9", lambda x: rd_calibrate(x, "RD-9"), ["RD-1", "RD-1b"]),
    ("RD-10", lambda x: rd_learned(x, "RD-10"), ["RD-1", "RD-1b"]),
    ("RD-11", lambda x: rd_score(x, "RD-11"), ["RD-1", "RD-1b"]),
    ("RD-12", lambda x: rd_crypto(x, "RD-12"), []),
    ("RD-13", lambda x: rd_bench(x, "RD-13"), ["RD-1"]),
]


def load_expected(path: Path | None, et: Path, out: Path) -> dict:
    p = path or HERE / "expected.json"
    if p.exists():
        return json.loads(p.read_text())
    # Regenerate from et (needs ~1 GB RAM, ~80 s on this Mac); scan output goes under --out.
    scan = out / "et_scan.json"
    subprocess.run(
        [sys.executable, str(HERE / "et_reference.py"), "--et-root", str(et), "--out", str(scan)], check=True
    )
    subprocess.run(
        [sys.executable, str(HERE / "build_expected.py"), "--scan", str(scan), "--out", str(out / "expected.json")],
        check=True,
    )
    return json.loads((out / "expected.json").read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--et-root",
        type=Path,
        default=os.environ.get("QT_ET_ROOT"),
        required=not os.environ.get("QT_ET_ROOT"),
        help="local checkout of the et prototype (default: $QT_ET_ROOT)",
    )
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--expected", type=Path, default=None)
    ap.add_argument("--only", default="", help="comma list of RD ids from the plan (dependencies are added)")
    ap.add_argument("--skip", default="", help="comma list of RD ids to skip")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--with-survival", action="store_true", help="also run the C2 survival fit in RD-9 (slow)")
    ap.add_argument("--keep", action="store_true", help="keep large intermediate outputs (depth/L2/zoo/score dirs)")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(et=a.et_root, out=a.out, exp=load_expected(a.expected, a.et_root, a.out), threads=a.threads, keep=a.keep)
    ctx.results["_with_survival"] = a.with_survival
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    skip = {x.strip() for x in a.skip.split(",") if x.strip()}
    if only:
        deps = dict((k, d) for k, _, d in PLAN)
        stack = list(only)
        while stack:
            for d in deps.get(stack.pop(), []):
                if d not in only:
                    only.add(d)
                    stack.append(d)
    docs: dict[str, dict] = {}
    t_all = time.perf_counter()
    for rd, fn, deps in PLAN:
        if (only and rd not in only) or rd in skip:
            docs[rd] = {"rd": rd, "status": "skipped", "notes": ["not selected"]}
            continue
        if any(docs.get(d, {}).get("status") in ("error", "skipped") for d in deps):
            docs[rd] = {"rd": rd, "status": "skipped", "notes": [f"dependency failed or skipped: {deps}"]}
            continue
        print(f"[{rd}] ...", file=sys.stderr, flush=True)
        t0 = time.perf_counter()
        try:
            docs[rd] = fn(ctx).doc()
        except Exception as e:
            docs[rd] = {
                "rd": rd,
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-4000:],
            }
        docs[rd]["wall_s"] = round(time.perf_counter() - t0, 2)
        print(f"[{rd}] {docs[rd]['status']} in {docs[rd]['wall_s']} s", file=sys.stderr, flush=True)
        (a.out / "results.json").write_text(jdump({"results": docs}) + "\n")  # checkpoint after every check
    summary = {
        "schema": "qtdev.realdata_results",
        "et_root": str(a.et_root),
        "expected_et_head": ctx.exp.get("et_head"),
        "wall_s": round(time.perf_counter() - t_all, 1),
        "peak_rss": peak_rss_mb(),
        "disk_mb": du_mb(a.out),
        "status_counts": _count(d["status"] for d in docs.values()),
        "results": docs,
    }
    (a.out / "results.json").write_text(jdump(summary) + "\n")
    print(jdump({k: v for k, v in summary.items() if k != "results"}))
    for rd, d in docs.items():
        print(f"{rd:6s} {d['status']:9s} {d.get('title', '')}")
    return 0 if not any(d["status"] in ("fail", "error") for d in docs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
