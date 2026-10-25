#!/usr/bin/env python3
"""Reconcile hftbacktest with queuetruth order by order on real data (queuetruth environment).

For a dataset, a shadow schedule and a `qtint.hbt_queue_config` (from `export_config.py`) this runs, with caching
under `--out`:

  exact/            `queuetruth replay`: exact FIFO truth per shadow (qt.shadow_results)
  zoo/              `queuetruth zoo --mode faithful` on the config's model ids: queuetruth's re-implementation of
                    each hftbacktest model on the same L2 view (qt.zoo_fills, qt.zoo_shadows)
  npz/<key>.npz     `queuetruth export l2-npz` per instrument-day (the same L2 view), plus `<key>.orders.json`:
                    every shadow the zoo ran, as a GTX limit order submitted at `ts_live` at its resolved price and
                    cancelled at its planned end (min of the planned cancel and the session close)
  hbt-<label>/      `hbt_driver.py` in each given hftbacktest venv (`--hbt name=python`, e.g. the released 2.4.4 and
                    the #312-patched build)

Cache: `manifest.json` records what the cached stages were computed from (dataset id, shadows sha256, L2 view,
exchange, model ids and, per `--hbt` label, the engine identity: hftbacktest version and the sha256 of its native
module). A later run with other inputs on the same `--out` is refused (`QT-USAGE-001`) unless `--fresh`, which
deletes the cached stages first; a new `--hbt` label is added to an existing cache.

It compares, per model:

  engine parity     every fill (timestamp, quantity in real units) of each order: hftbacktest vs queuetruth's
                    faithful model. A build that stopped (the #312 abort) is compared only on the orders live before
                    its stop, on their fills before the stop; `orders_compared` and `orders_with_fills` say how many
                    orders that is, and `same_first_fill` counts only orders with a fill on either side.
  #312 on real data per instrument-day: the code and time at which each hftbacktest build stopped, the orders
                    live before it, the first queuetruth `hbt_312` zombie fill, and whether the build's fills up to
                    its stop equal the reference build's fills up to that time.
  fill realism      for builds that ran to the end, at each horizon h, over shadows whose label is defined (planned
                    end and administrative end at or after ts_live + h; spec 5.3.2): the 2x2 of exact first fill
                    within h vs hftbacktest first fill within h, fill rates and their paired difference
                    (cluster-robust bootstrap CI and exact McNemar p, clusters = instrument x date x 30 min); the
                    same for the full fill; per session date; and the distribution of hftbacktest's first-fill time
                    minus the exact one.

Outputs: `reconciliation.json` (`qtint.hbt_reconciliation` v1, canonical, no wall times), `reconciliation.md`,
`per_order.parquet` (one row per model, build and order: exact, hftbacktest and queuetruth-faithful first fill, full
fill and filled quantity, and the differences) and `timings.json`.

Exit codes: 0 done, 2 error (`QT-*` on stderr).

Example (the documented run, see README.md):
  python integrations/hftbacktest/reconcile.py --data DATASET --shadows SHADOWS --config hbt_queue_config.json \\
     --out out/recon --hbt stock=.scratch/hbt-venv/bin/python --hbt patched=.scratch/hbt-venv-312/bin/python \\
     --reference patched
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hbt_config

from queuetruth.errors import EXIT_ERROR, QtError
from queuetruth.stats.clustered import clustered_mcnemar

HERE = Path(__file__).resolve().parent
SCHEMA = "qtint.hbt_reconciliation"
MANIFEST_SCHEMA = "qtint.hbt_reconcile_manifest"
DEFAULT_HORIZONS_NS = (100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000, 300_000_000_000)
BUCKET_NS = 1_800_000_000_000
REJECTED = ("rejected_would_cross", "rejected_no_touch")
SIDE_B = 66  # ord("B")
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


@dataclass(frozen=True)
class Order:
    """One shadow as an hftbacktest order (`px`, `qty`, `lot` in real units; `size_scale` = 10^size_exp)."""

    order_id: int
    shadow_id: str
    instrument_id: int
    session_date: str
    side: str
    px: float
    qty: float
    ts_live: int
    ts_end: int | None
    size_scale: float = 1.0
    lot: float = 1.0


def _rows(path: Path, columns: list[str] | None = None) -> list[dict[str, Any]]:
    t = pq.read_table(path, columns=columns)
    cols = {}
    for name in t.column_names:
        col = t.column(name)
        if str(col.type).startswith("timestamp"):
            cols[name] = [None if x is None else int(x) for x in col.cast("int64").to_pylist()]
        else:
            cols[name] = col.to_pylist()
    n = t.num_rows
    return [{k: cols[k][i] for k in cols} for i in range(n)]


CODE_LINE = re.compile(r"(QT-[A-Z]+-\d{3}): (.*)")


def child_error(what: str, rc: int, log: Path) -> QtError:
    """The first `QT-*` error a failed child process logged (its code kept), else `QT-ENGINE-001`."""
    text = log.read_text(errors="replace") if log.exists() else ""
    for line in text.splitlines():
        m = CODE_LINE.search(line)
        if m:
            return QtError(m.group(1), f"{what}: {m.group(2)} (see {log})")
    return QtError("QT-ENGINE-001", f"{what} failed with exit code {rc}; see {log}")


def run_cli(what: str, args: list[str], log: Path) -> None:
    """`python -m queuetruth.cli <args>` with stderr/stdout to `log`; on failure the child's `QtError`."""
    cmd = [sys.executable, "-m", "queuetruth.cli", *args]
    t0 = time.perf_counter()
    with log.open("w") as fh:
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=os.environ.copy()).returncode
    if rc != 0:
        raise child_error(f"queuetruth {what}", rc, log)
    with log.open("a") as fh:
        fh.write(f"\n# wall_s {time.perf_counter() - t0:.1f}\n")


def instruments(data: Path) -> dict[tuple[int, str], dict[str, Any]]:
    """(instrument_id, session_date) -> qt.instruments row."""
    out = {}
    for r in _rows(data / "instruments.parquet"):
        out[(int(r["instrument_id"]), str(r["session_date"]))] = r
    return out


def session_open_ns(row: dict[str, Any]) -> int | None:
    """UTC ns of the session open of an instruments row (None when the row does not say)."""
    if row.get("midnight_utc_ns") is None or row.get("open_s") is None:
        return None
    return int(row["midnight_utc_ns"]) + int(row["open_s"]) * 1_000_000_000


def build_orders(exact_dir: Path, zoo_dir: Path, inst: dict[tuple[int, str], dict[str, Any]]) -> dict[str, Any]:
    """Orders per instrument-day from the exact shadow results and the zoo's shadows.

    A shadow becomes an order when the zoo ran it (it is in qt.zoo_shadows), the exact engine resolved a price and
    did not reject it, and its planned end is after `ts_live` (`zero_life` otherwise: the zoo treats a shadow
    cancelled at or before activation as cancelled, so it never rests). `ts_end` = min(planned cancel, session
    close) (the A2 planned end).
    """
    zoo = {r["shadow_id"]: r for r in _rows(zoo_dir / "zoo_shadows.parquet")}
    res = _rows(
        exact_dir / "shadow_results.parquet",
        ["shadow_id", "instrument_id", "side", "price", "qty", "ts_live", "end_state"],
    )
    skipped: dict[str, int] = defaultdict(int)
    per_day: dict[str, list[Order]] = defaultdict(list)
    opens: dict[str, int | None] = {}
    for r in sorted(res, key=lambda r: (r["ts_live"], r["shadow_id"])):
        z = zoo.get(r["shadow_id"])
        if z is None:
            skipped["not_in_zoo"] += 1
            continue
        if r["price"] is None:
            skipped["no_price"] += 1
            continue
        if r["end_state"] in REJECTED:
            skipped[str(r["end_state"])] += 1
            continue
        date = str(z["session_date"])
        row = inst.get((int(r["instrument_id"]), date))
        if row is None:
            raise QtError("QT-DATA-001", f"no instruments row for {r['instrument_id']} on {date}")
        ends = [x for x in (z["ts_cancel_planned"], z["session_close"]) if x is not None]
        ts_end = min(ends) if ends else None
        if ts_end is not None and ts_end <= int(r["ts_live"]):
            skipped["zero_life"] += 1
            continue
        key = f"{int(r['instrument_id'])}_{date}"
        scale = 10.0 ** int(row["size_exp"])
        opens[key] = session_open_ns(row)
        per_day[key].append(
            Order(
                order_id=len(per_day[key]) + 1,
                shadow_id=r["shadow_id"],
                instrument_id=int(r["instrument_id"]),
                session_date=date,
                side="B" if int(r["side"]) == SIDE_B else "A",
                px=float(r["price"]) * 10.0 ** int(row["price_exp"]),
                qty=float(r["qty"]) * scale,
                ts_live=int(r["ts_live"]),
                ts_end=ts_end,
                size_scale=scale,
                lot=float(row["lot_size"]) * scale,
            )
        )
    return {
        "per_day": dict(sorted(per_day.items())),
        "skipped": dict(sorted(skipped.items())),
        "session_open_ns": dict(sorted(opens.items())),
    }


def sidecar(orders: list[Order], row: dict[str, Any]) -> dict[str, Any]:
    """SPEC 5.3.5 orders sidecar (plus `shadow_ids`)."""
    actions = []
    for o in orders:
        actions.append(
            {"ts": o.ts_live, "kind": "submit", "order_id": o.order_id, "side": o.side, "px": o.px, "qty": o.qty,
             "tif": "GTX"}
        )  # fmt: skip
        if o.ts_end is not None:
            actions.append({"ts": o.ts_end, "kind": "cancel", "order_id": o.order_id})
    actions.sort(key=lambda a: (a["ts"], 0 if a["kind"] == "cancel" else 1, a["order_id"]))
    return {
        "tick_size": float(row["tick_size"]) * 10.0 ** int(row["price_exp"]),
        "lot_size": float(row["lot_size"]) * 10.0 ** int(row["size_exp"]),
        "actions": actions,
        "shadow_ids": {str(o.order_id): o.shadow_id for o in orders},
    }


def fill_key(fills: list[tuple[int, float]]) -> tuple[tuple[int, float], ...]:
    """Fills aggregated per timestamp (two fills at one timestamp are one observation in the driver)."""
    agg: dict[int, float] = defaultdict(float)
    for ts, q in fills:
        agg[int(ts)] += float(q)
    return tuple(sorted((ts, round(q, 9)) for ts, q in agg.items()))


def qt_fills(zoo_dir: Path) -> tuple[dict[str, dict[str, list]], dict[str, dict[str, int]]]:
    """model -> shadow -> [(ts, qty in raw size units)], and model -> shadow -> ts of its `hbt_312` zombie fill."""
    fills: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    zombies: dict[str, dict[str, int]] = defaultdict(dict)
    for r in _rows(zoo_dir / "zoo_fills.parquet", ["model_id", "shadow_id", "ts_event", "fill_qty", "quirk"]):
        fills[r["model_id"]][r["shadow_id"]].append((int(r["ts_event"]), float(r["fill_qty"])))
        if r["quirk"] == "hbt_312":
            zombies[r["model_id"]].setdefault(r["shadow_id"], int(r["ts_event"]))
    return fills, zombies


def label_defined(o: Order, z: dict[str, Any], h: int) -> bool:
    """Spec 5.3.2: the truth label at h is defined iff the planned end and the administrative end are >= ts_live + h."""
    t = o.ts_live + h
    if o.ts_end is not None and o.ts_end < t:
        return False
    adm = z.get("ts_admin_end")
    return adm is None or adm >= t


def realism(
    orders: list[Order],
    zoo: dict[str, dict[str, Any]],
    exact_ts: dict[str, int | None],
    model_ts: dict[str, int | None],
    horizons: tuple[int, ...],
    seed: int,
) -> dict[str, Any]:
    """Per horizon: 2x2 of "exact event within h" vs "model event within h", rates and the paired difference model -
    exact with a cluster-robust CI (the event is the first fill or the full fill, whichever the dicts hold)."""
    out = {}
    for h in horizons:
        ex, mo, cl = [], [], []
        for o in orders:
            if not label_defined(o, zoo[o.shadow_id], h):
                continue
            e = exact_ts.get(o.shadow_id)
            m = model_ts.get(o.shadow_id)
            ex.append(e is not None and e - o.ts_live <= h)
            mo.append(m is not None and m - o.ts_live <= h)
            cl.append(f"{o.instrument_id}_{o.session_date}_{o.ts_live // BUCKET_NS}")
        n = len(ex)
        e_arr, m_arr = np.array(ex, bool), np.array(mo, bool)
        both = int(np.sum(e_arr & m_arr))
        exact_only = int(np.sum(e_arr & ~m_arr))
        model_only = int(np.sum(~e_arr & m_arr))
        cell: dict[str, Any] = {
            "n": n,
            "both": both,
            "exact_only": exact_only,
            "model_only": model_only,
            "neither": n - both - exact_only - model_only,
            "exact_fill_rate": (both + exact_only) / n if n else None,
            "model_fill_rate": (both + model_only) / n if n else None,
        }
        if n:
            d = clustered_mcnemar(e_arr, m_arr, np.array(cl), b=1000, seed=seed)
            cell |= {
                "bias": d.estimate,
                "ci_lo": d.ci_lo,
                "ci_hi": d.ci_hi,
                "p_mcnemar_exact": d.mcnemar.p_exact,
                "n_clusters": d.n_clusters,
            }
        out[str(h)] = cell
    return out


def first_fill_delta(orders: list[Order], exact_first: dict[str, int | None], model_first: dict[str, int | None]):
    """Over the whole order life: who fills at all, and the distribution of model first fill - exact first fill (ns)
    over the orders both fill (linear-interpolated quantiles)."""
    both, e_only, m_only, neither = [], 0, 0, 0
    for o in orders:
        e, m = exact_first.get(o.shadow_id), model_first.get(o.shadow_id)
        if e is not None and m is not None:
            both.append(m - e)
        elif e is not None:
            e_only += 1
        elif m is not None:
            m_only += 1
        else:
            neither += 1
    d = np.array(both, np.int64)
    out: dict[str, Any] = {
        "n_both": len(both),
        "n_exact_only": e_only,
        "n_model_only": m_only,
        "n_neither": neither,
        "n_same_ts": int(np.sum(d == 0)),
        "n_model_earlier": int(np.sum(d < 0)),
        "n_model_later": int(np.sum(d > 0)),
    }
    if len(both):
        out["quantiles_ns"] = {f"p{round(q * 100):02d}": float(np.quantile(d, q)) for q in QUANTILES}
        out["mean_abs_ns"] = float(np.mean(np.abs(d)))
    return out


def _full_fill_ts(fills: list[tuple[int, float, float]], lot: float) -> int | None:
    """ts of the driver fill after which the order's leaves round to zero lots (hftbacktest's `Filled` rule)."""
    for ts, _, leaves in fills:
        if round(leaves / lot) <= 0:
            return ts
    return None


def compare(
    plan: dict[str, Any],
    zoo_dir: Path,
    exact_dir: Path,
    hbt_runs: dict[str, dict[str, dict[str, Any]]],
    models: list[str],
    horizons: tuple[int, ...],
    reference: str | None,
    seed: int,
    per_order: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The reconciliation document (without provenance); appends one row per model, build and order to `per_order`.

    queuetruth's `qt.zoo_fills.fill_qty` is in raw size units and hftbacktest's in real units: the zoo's is scaled
    by the instrument-day's `10^size_exp` before the comparison."""
    zoo = {r["shadow_id"]: r for r in _rows(zoo_dir / "zoo_shadows.parquet")}
    res = {
        r["shadow_id"]: r
        for r in _rows(
            exact_dir / "shadow_results.parquet", ["shadow_id", "ts_first_fill", "ts_full_fill", "filled_qty"]
        )
    }
    exact_first = {k: r["ts_first_fill"] for k, r in res.items()}
    exact_full = {k: r["ts_full_fill"] for k, r in res.items()}
    q_fills, q_zombies = qt_fills(zoo_dir)
    all_orders = [o for os_ in plan["per_day"].values() for o in os_]
    dates = sorted({o.session_date for o in all_orders})
    result: dict[str, Any] = {"models": {}}
    for mid in models:
        m: dict[str, Any] = {"engines": {}}
        for label, per_day in hbt_runs.items():
            parity = {
                "orders": 0,
                "orders_compared": 0,
                "orders_with_fills": 0,
                "identical_fills": 0,
                "same_first_fill": 0,
                "differ": [],
            }
            days = {}
            h_first: dict[str, int | None] = {}
            h_full: dict[str, int | None] = {}
            for key, orders in plan["per_day"].items():
                run = per_day.get(key, {}).get(mid)
                if run is None:
                    raise QtError(
                        "QT-USAGE-001",
                        f"the {label} hftbacktest results for {key} have no run of {mid}; rerun with --fresh",
                    )
                by_oid: dict[int, list] = defaultdict(list)
                for f in run["fills"]:
                    by_oid[f["order_id"]].append((f["ts"], f["qty"], f["leaves_after"]))
                err = run["error"]
                stop = err["ts"] if err else None
                zombie_ts = [q_zombies.get(mid, {}).get(o.shadow_id) for o in orders]
                zombie_ts = [t for t in zombie_ts if t is not None]
                live_before = sum(1 for o in orders if stop is None or o.ts_live < stop)
                day = {
                    "orders": len(orders),
                    "orders_live_before_stop": live_before,
                    "fills": len(run["fills"]),
                    "error": err,
                    "session_open_ns": plan.get("session_open_ns", {}).get(key),
                    "first_qt_312_zombie_fill_ts": min(zombie_ts) if zombie_ts else None,
                    "qt_312_zombies": len(zombie_ts),
                }
                for o in orders:
                    hb_all = by_oid.get(o.order_id, [])
                    qf_all = [(t, q * o.size_scale) for t, q in q_fills.get(mid, {}).get(o.shadow_id, [])]
                    compared = stop is None or o.ts_live < stop
                    hb = [(t, q) for t, q, _ in hb_all if stop is None or t < stop]
                    qf = [(t, q) for t, q in qf_all if stop is None or t < stop]
                    a, b = fill_key(hb), fill_key(qf)
                    parity["orders"] += 1
                    if compared:
                        parity["orders_compared"] += 1
                        if a or b:
                            parity["orders_with_fills"] += 1
                            if (a[0][0] if a else None) == (b[0][0] if b else None):
                                parity["same_first_fill"] += 1
                        if a == b:
                            parity["identical_fills"] += 1
                        elif len(parity["differ"]) < 20:
                            parity["differ"].append({"shadow_id": o.shadow_id, "hbt": a[:5], "qt": b[:5]})
                    h_first[o.shadow_id] = min(t for t, _ in hb) if hb else None
                    h_full[o.shadow_id] = _full_fill_ts([x for x in hb_all if stop is None or x[0] < stop], o.lot)
                    if per_order is not None:
                        ex = res.get(o.shadow_id, {})
                        e_qty = None if ex.get("filled_qty") is None else float(ex["filled_qty"]) * o.size_scale
                        h_qty = round(sum(q for _, q in hb), 9)
                        per_order.append(
                            {
                                "model_id": mid,
                                "engine": label,
                                "shadow_id": o.shadow_id,
                                "order_id": o.order_id,
                                "instrument_id": o.instrument_id,
                                "session_date": o.session_date,
                                "side": o.side,
                                "px": o.px,
                                "qty": o.qty,
                                "ts_live": o.ts_live,
                                "ts_end": o.ts_end,
                                "engine_stop_ts": stop,
                                "compared": compared,
                                "exact_ts_first_fill": ex.get("ts_first_fill"),
                                "exact_ts_full_fill": ex.get("ts_full_fill"),
                                "exact_filled_qty": e_qty,
                                "hbt_ts_first_fill": h_first[o.shadow_id],
                                "hbt_ts_full_fill": h_full[o.shadow_id],
                                "hbt_filled_qty": h_qty,
                                "qt_ts_first_fill": min(t for t, _ in qf) if qf else None,
                                "qt_filled_qty": round(sum(q for _, q in qf), 9),
                                "hbt_equals_qt": a == b,
                                "d_first_fill_ns": (
                                    None
                                    if h_first[o.shadow_id] is None or ex.get("ts_first_fill") is None
                                    else h_first[o.shadow_id] - ex["ts_first_fill"]
                                ),
                                "d_filled_qty": None if e_qty is None else round(h_qty - e_qty, 9),
                            }
                        )
                days[key] = day
            eng: dict[str, Any] = {"parity_with_qt_faithful": parity, "days": days}
            if not any(d["error"] for d in days.values()):
                eng["realism"] = realism(all_orders, zoo, exact_first, h_first, horizons, seed)
                eng["realism_full_fill"] = realism(all_orders, zoo, exact_full, h_full, horizons, seed)
                eng["realism_by_date"] = {
                    d: realism(
                        [o for o in all_orders if o.session_date == d], zoo, exact_first, h_first, horizons, seed
                    )
                    for d in dates
                }
                eng["first_fill_delta"] = first_fill_delta(all_orders, exact_first, h_first)
            m["engines"][label] = eng
        if reference and reference in m["engines"]:
            ref_days = hbt_runs[reference]
            for label, eng in m["engines"].items():
                if label == reference:
                    continue
                for key, day in eng["days"].items():
                    if not day["error"]:
                        continue
                    stop = day["error"]["ts"]
                    mine = fill_key_all(hbt_runs[label][key][mid]["fills"], stop)
                    ref = fill_key_all(ref_days[key][mid]["fills"], stop)
                    day["fills_before_stop_equal_reference"] = mine == ref
        result["models"][mid] = m
    return result


def fill_key_all(fills: list[dict[str, Any]], stop: int) -> tuple:
    """Every fill strictly before `stop`, as (order_id, ts, qty) sorted."""
    return tuple(sorted((f["order_id"], f["ts"], round(f["qty"], 9)) for f in fills if f["ts"] < stop))


ABORT_LINE = re.compile(
    r"^hbt_312_abort: (\S+): (\d+) of \d+ shadow pair\(s\) aborted .*first at shadow (\S+) ts (\d+);"
)


def qt_first_aborts(warnings: list[str]) -> dict[str, dict[str, Any]]:
    """model -> {n_pairs, shadow_id, ts} from the zoo's `hbt_312_abort:` warning lines (first pair in shadow order)."""
    out = {}
    for w in warnings:
        m = ABORT_LINE.match(w)
        if m:
            out[m.group(1)] = {"n_pairs": int(m.group(2)), "shadow_id": m.group(3), "ts": int(m.group(4))}
    return out


def check_first_aborts(doc: dict[str, Any], plan: dict[str, Any], first: dict[str, dict[str, Any]]) -> None:
    """Per model and engine: does the engine stop on the first aborted pair's instrument-day at that pair's abort
    time? (Equal when that pair is the day's earliest abort; an engine stops at the day's earliest abort.)"""
    day_of = {o.shadow_id: key for key, orders in plan["per_day"].items() for o in orders}
    for mid, m in doc["models"].items():
        fa = first.get(mid)
        if fa is None:
            continue
        key = day_of.get(fa["shadow_id"])
        for eng in m["engines"].values():
            day = eng["days"].get(key) if key else None
            err = day["error"] if day else None
            eng["qt_first_abort"] = fa | {
                "day": key,
                "engine_stop_ts": None if err is None else err["ts"],
                "engine_stops_at_it": err is not None and err["ts"] == fa["ts"],
                "engine_stops_no_later": err is not None and err["ts"] <= fa["ts"],
            }


def _minutes(ts: int | None, open_ns: int | None) -> str:
    if ts is None:
        return "-"
    if open_ns is None:
        return str(ts)
    return f"{(ts - open_ns) / 60e9:.1f}"


def markdown(doc: dict[str, Any]) -> str:
    """A compact human summary of `reconciliation.json`."""
    lines = [f"# hftbacktest reconciliation ({doc['dataset']}, {doc['n_orders']} orders)", ""]
    lines.append("## Engine parity: hftbacktest vs queuetruth faithful, order by order")
    lines.append("")
    lines.append(
        "A build that stopped is compared on the orders live before its stop (`compared`), on their fills before it."
    )
    lines.append("")
    lines.append(
        "| model | engine | orders | compared | with a fill | identical fills | same first fill | runs stopped |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for mid, m in doc["models"].items():
        for label, e in m["engines"].items():
            p = e["parity_with_qt_faithful"]
            stopped = sum(1 for d in e["days"].values() if d["error"])
            lines.append(
                f"| `{mid}` | {label} | {p['orders']} | {p['orders_compared']} | {p['orders_with_fills']} | "
                f"{p['identical_fills']} | {p['same_first_fill']} | {stopped} of {len(e['days'])} |"
            )
    stops = [
        (mid, label, key, d)
        for mid, m in doc["models"].items()
        for label, e in m["engines"].items()
        for key, d in e["days"].items()
        if d["error"]
    ]
    if stops:
        lines += ["", "## Stopped runs (#312), minutes after the session open", ""]
        lines.append(
            "| model | engine | instrument-day | code | stop | first qt `hbt_312` zombie fill | orders live before "
            "the stop | fills before the stop equal the reference |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for mid, label, key, d in stops:
            o = d.get("session_open_ns")
            eq = d.get("fills_before_stop_equal_reference")
            lines.append(
                f"| `{mid}` | {label} | {key} | {d['error']['code']} | {_minutes(d['error']['ts'], o)} | "
                f"{_minutes(d['first_qt_312_zombie_fill_ts'], o)} | {d['orders_live_before_stop']} of {d['orders']} | "
                f"{'-' if eq is None else ('yes' if eq else 'NO')} |"
            )
    lines += ["", "## Fill realism vs exact truth (builds that ran to the end)", ""]
    finished = [
        (mid, label, e) for mid, m in doc["models"].items() for label, e in m["engines"].items() if "realism" in e
    ]
    if not finished:
        lines.append(
            "No build ran to the end: realism not computed (use a build with the #312 fix, or `no_partial` exchange)."
        )
        return "\n".join(lines) + "\n"
    lines.append("| model | engine | sample | horizon | n | exact fill rate | model fill rate | bias [95% CI] |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for mid, label, e in finished:
        samples = [("first fill", e["realism"]), ("full fill", e.get("realism_full_fill", {}))]
        samples += [(f"first fill, {d}", r) for d, r in e.get("realism_by_date", {}).items()]
        for name, rr in samples:
            for h, c in rr.items():
                if not c["n"]:
                    continue
                lines.append(
                    f"| `{mid}` | {label} | {name} | {int(h) / 1e9:g} s | {c['n']} | {c['exact_fill_rate']:.4f} | "
                    f"{c['model_fill_rate']:.4f} | {100 * c['bias']:+.2f} pp [{100 * c['ci_lo']:+.2f}, "
                    f"{100 * c['ci_hi']:+.2f}] |"
                )
    return "\n".join(lines) + "\n"


def parse_horizons(s: str | None) -> tuple[int, ...]:
    if not s:
        return DEFAULT_HORIZONS_NS
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        for suf, mult in (("ms", 1_000_000), ("us", 1_000), ("ns", 1), ("s", 1_000_000_000), ("m", 60_000_000_000)):
            if tok.endswith(suf):
                out.append(int(float(tok[: -len(suf)]) * mult))
                break
        else:
            out.append(int(tok))
    if any(h <= 0 for h in out):
        raise QtError("QT-CONFIG-001", f"horizons must be positive: {s}")
    return tuple(sorted(set(out)))


# ---- cache manifest -------------------------------------------------------------------------------------------


def sha256_path(p: Path) -> str:
    """sha256 of a file, or of a directory's files (relative path and content, sorted)."""
    h = hashlib.sha256()
    if p.is_dir():
        for f in sorted(x for x in p.rglob("*") if x.is_file()):
            h.update(str(f.relative_to(p)).encode() + b"\0")
            h.update(hashlib.sha256(f.read_bytes()).digest())
        return h.hexdigest()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dataset_id(data: Path) -> str:
    """The dataset's content id: `manifest.json` `dataset_id` (a hash of its inputs), else the instruments' sha256."""
    man = data / "manifest.json"
    if man.exists():
        try:
            did = json.loads(man.read_text()).get("dataset_id")
        except json.JSONDecodeError:
            did = None
        if isinstance(did, str) and did:
            return did
    inst = data / "instruments.parquet"
    if not inst.exists():
        raise QtError("QT-INPUT-001", f"{data}: not a queuetruth dataset (no instruments.parquet)")
    return "instruments-sha256:" + sha256_path(inst)


def engine_identity(py: Path) -> dict[str, Any]:
    """`hbt_driver.py --identify` in that venv."""
    r = subprocess.run([str(py), "-I", str(HERE / "hbt_driver.py"), "--identify"], capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise QtError("QT-USAGE-001", f"{py} cannot run hbt_driver.py: {msg[-1] if msg else r.returncode}")
    return json.loads(r.stdout)


STAGES = ("exact", "zoo", "npz")
OUTPUTS = ("reconciliation.json", "reconciliation.md", "per_order.parquet", "timings.json", "hbt_queue_config.json")


def _stage_paths(out: Path) -> list[Path]:
    return [out / s for s in STAGES if (out / s).exists()] + sorted(p for p in out.glob("hbt-*") if p.is_dir())


def check_cache(out: Path, inputs: dict[str, Any], idents: dict[str, dict[str, Any]], fresh: bool) -> dict[str, Any]:
    """Validate (or, with `fresh`, clear) the cache under `out`; returns the manifest to write."""
    man_path = out / "manifest.json"
    if fresh:
        for p in _stage_paths(out):
            shutil.rmtree(p)
        for name in (*OUTPUTS, "manifest.json", "exact.log", "zoo.log"):
            (out / name).unlink(missing_ok=True)
    prev = json.loads(man_path.read_text()) if man_path.exists() else None
    if prev is None:
        if _stage_paths(out):
            raise QtError(
                "QT-USAGE-001",
                f"{out} holds cached stages without a manifest (an older reconcile.py or another tool): pass --fresh "
                "to recompute them, or use a new --out",
            )
        return {"schema": MANIFEST_SCHEMA, "schema_version": 1, "inputs": inputs, "hbt": dict(idents)}
    if prev.get("schema") != MANIFEST_SCHEMA or prev.get("inputs") != inputs:
        old = prev.get("inputs", {})
        diff = sorted(k for k in set(old) | set(inputs) if old.get(k) != inputs.get(k))
        raise QtError(
            "QT-USAGE-001",
            f"{out} holds a run with different inputs ({', '.join(diff) or 'manifest schema'} differ): pass --fresh to "
            "recompute it, or use a new --out",
        )
    hbt = dict(prev.get("hbt", {}))
    for label, ident in idents.items():
        if label in hbt and hbt[label] != ident:
            raise QtError(
                "QT-USAGE-001",
                f"--hbt {label} is a different hftbacktest build than the one cached in {out}/hbt-{label} "
                f"({hbt[label].get('hftbacktest')} {hbt[label].get('native_sha256')} vs {ident.get('hftbacktest')} "
                f"{ident.get('native_sha256')}): pass --fresh, use another label or a new --out",
            )
        hbt[label] = ident
    return {**prev, "hbt": dict(sorted(hbt.items()))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--shadows", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--hbt", action="append", default=[], help="label=python of an hftbacktest venv (repeatable)")
    ap.add_argument("--reference", default=None, help="label of the build the others' stops are compared with")
    ap.add_argument("--models", default=None, help="comma list of config model ids (default: all)")
    ap.add_argument("--horizons", default=None, help="e.g. 100ms,1s,10s,60s,5m (default)")
    ap.add_argument("--l2-depth", type=int, default=10)
    ap.add_argument("--l2-cadence-ns", type=int, default=0)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fresh", action="store_true", help="delete the cached stages under --out and recompute them")
    args = ap.parse_args(argv)
    try:
        return _main(args)
    except QtError as e:
        print(str(e), file=sys.stderr)
        return e.exit_code
    except hbt_config.ConfigError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR


def _main(args: argparse.Namespace) -> int:
    cfg = hbt_config.load(args.config)
    entries = hbt_config.select(cfg, args.models.split(",") if args.models else None)
    exchanges = {e["exchange"] for e in entries}
    if len(exchanges) != 1:
        raise QtError("QT-CONFIG-001", f"one exchange per reconciliation run, got {sorted(exchanges)}")
    exchange = exchanges.pop()
    models = [e["model_id"] for e in entries]
    hbts = {}
    for spec in args.hbt:
        label, _, py = spec.partition("=")
        if not label or not py or not Path(py).exists() or not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
            raise QtError("QT-USAGE-001", f"--hbt wants label=/path/to/python of an existing venv, got {spec!r}")
        hbts[label] = Path(py)
    if not hbts:
        raise QtError("QT-USAGE-001", "give at least one --hbt label=python")
    if args.reference and args.reference not in hbts:
        raise QtError("QT-USAGE-001", f"--reference {args.reference} is not one of the --hbt labels")
    if not args.shadows.exists():
        raise QtError("QT-INPUT-001", f"{args.shadows}: shadows not found")
    horizons = parse_horizons(args.horizons)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    l2 = ["--l2-depth", str(args.l2_depth), "--l2-cadence-ns", str(args.l2_cadence_ns)]
    inputs = {
        "dataset_id": dataset_id(args.data),
        "shadows_sha256": sha256_path(args.shadows),
        "l2": {"depth_levels": args.l2_depth, "cadence_ns": args.l2_cadence_ns},
        "exchange": exchange,
        "models": sorted(models),
    }
    idents = {label: engine_identity(py) for label, py in hbts.items()}
    manifest = check_cache(out, inputs, idents, args.fresh)
    (out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=1) + "\n")
    tpath = out / "timings.json"
    timings: dict[str, Any] = json.loads(tpath.read_text()) if tpath.exists() else {}
    cached: list[str] = []

    exact_dir = out / "exact"
    if not (exact_dir / "shadow_results.parquet").exists():
        t0 = time.perf_counter()
        run_cli(
            "replay",
            ["--threads", str(args.threads), "--quiet", "replay", "--data", str(args.data), "--shadows",
             str(args.shadows), "--out", str(exact_dir), "--trajectory", "none", *l2],
            out / "exact.log",
        )  # fmt: skip
        timings["exact_replay_s"] = time.perf_counter() - t0
    else:
        cached.append("exact")
    zoo_dir = out / "zoo"
    if not (zoo_dir / "zoo_fills.parquet").exists():
        t0 = time.perf_counter()
        run_cli(
            "zoo",
            ["--threads", str(args.threads), "--quiet", "zoo", "--data", str(args.data), "--shadows",
             str(args.shadows), "--out", str(zoo_dir), "--models", ",".join(models), "--mode", "faithful",
             "--exchange", exchange.replace("_", "-"), "--bootstrap-b", "200", *l2],
            out / "zoo.log",
        )  # fmt: skip
        timings["qt_zoo_s"] = time.perf_counter() - t0
    else:
        cached.append("zoo")

    inst = instruments(args.data)
    plan = build_orders(exact_dir, zoo_dir, inst)
    npz_dir = out / "npz"
    npz_dir.mkdir(exist_ok=True)
    t0 = time.perf_counter()
    n_new = 0
    for key, orders in plan["per_day"].items():
        iid, date = key.split("_", 1)
        npz = npz_dir / f"{key}.npz"
        if not npz.exists():
            n_new += 1
            run_cli(
                "export l2-npz",
                ["--quiet", "export", "l2-npz", "--data", str(args.data), "--instrument-id", iid, "--date", date,
                 "--out", str(npz), *l2],
                npz_dir / f"{key}.log",
            )  # fmt: skip
        (npz_dir / f"{key}.orders.json").write_text(json.dumps(sidecar(orders, inst[(int(iid), date)])) + "\n")
    if n_new:
        timings["npz_export_s"] = time.perf_counter() - t0
    else:
        cached.append("npz")

    sub_cfg = out / "hbt_queue_config.json"
    sub_cfg.write_text(hbt_config.dumps(hbt_config.document(entries, cfg["queuetruth_version"])))
    hbt_runs: dict[str, dict[str, dict[str, Any]]] = {}
    engines = {}
    for label, py in hbts.items():
        d = out / f"hbt-{label}"
        d.mkdir(exist_ok=True)
        hbt_runs[label] = {}
        t0 = time.perf_counter()
        ran = False
        for key in plan["per_day"]:
            res = d / f"{key}.json"
            if not res.exists():
                ran = True
                with (d / f"{key}.log").open("w") as fh:
                    rc = subprocess.run(
                        [str(py), "-I", str(HERE / "hbt_driver.py"), "--npz", str(npz_dir / f"{key}.npz"),
                         "--orders", str(npz_dir / f"{key}.orders.json"), "--config", str(sub_cfg),
                         "--engine-label", label, "--out", str(res)],
                        stdout=fh, stderr=subprocess.STDOUT,
                    ).returncode  # fmt: skip
                if rc != 0:
                    raise child_error(f"hbt_driver.py ({label}, {key})", rc, d / f"{key}.log")
            doc = json.loads(res.read_text())
            got = sorted(r["model_id"] for r in doc["runs"])
            if got != sorted(models) or doc.get("engine_identity") != idents[label]:
                raise QtError(
                    "QT-USAGE-001",
                    f"{res} was computed for other models or another build than this run's: pass --fresh",
                )
            hbt_runs[label][key] = {r["model_id"]: r for r in doc["runs"]}
            timings[f"hbt_{label}_{key}_npz_load_s"] = doc["npz_load_s"]
            timings[f"hbt_{label}_{key}_compile_s"] = doc["compile_s"]
            for r in doc["runs"]:
                timings[f"hbt_{label}_{key}_{r['model_id']}_loop_s"] = r["wall_s"]
        engines[label] = {"label": label, "python": str(py), **idents[label]}
        if ran:
            timings[f"hbt_{label}_s"] = time.perf_counter() - t0
        else:
            cached.append(f"hbt-{label}")

    per_order: list[dict[str, Any]] = []
    rec = compare(plan, zoo_dir, exact_dir, hbt_runs, models, horizons, args.reference, args.seed, per_order)
    zoo_summary = json.loads((zoo_dir / "summary.json").read_text())
    check_first_aborts(rec, plan, qt_first_aborts(zoo_summary.get("warnings", [])))
    doc = {
        "schema": SCHEMA,
        "schema_version": 1,
        "dataset": str(args.data),
        "dataset_id": inputs["dataset_id"],
        "shadows": str(args.shadows),
        "shadows_sha256": inputs["shadows_sha256"],
        "config": hbt_config.load(sub_cfg),
        "exchange": exchange,
        "l2": inputs["l2"],
        "horizons_ns": list(horizons),
        "engines": engines,
        "n_orders": sum(len(v) for v in plan["per_day"].values()),
        "orders_per_day": {k: len(v) for k, v in plan["per_day"].items()},
        "skipped_shadows": plan["skipped"],
        "qt_zoo_run_id": zoo_summary.get("run_id"),
        "qt_hbt_312_aborts": zoo_summary.get("hbt_312_aborts", {}),
        **rec,
    }
    pq.write_table(pa.Table.from_pylist(per_order), out / "per_order.parquet")
    timings["cached_stages_last_run"] = cached
    tpath.write_text(
        json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in timings.items()}, indent=1) + "\n"
    )
    (out / "reconciliation.json").write_text(json.dumps(doc, sort_keys=True, indent=1, allow_nan=False) + "\n")
    (out / "reconciliation.md").write_text(markdown(doc))
    print(markdown(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
