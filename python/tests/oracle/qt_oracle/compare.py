"""Comparison of oracle results with queuetruth (or et, or hand-derived JSON) shadow results, and the categorised
discrepancy report.

Normalised shadow record (both sides):
  shadow_id, price, filled_qty, end_state, first_fill_reason, first_fill_liquidity (or None), ts_first_fill,
  ts_activated_event, queue_ahead_at_live,
  fills: [{ts_event, fill_qty, reason, liquidity (or None), fill_px}] or None when the source has no fill timeline,
  trajectory: [{ts_event, queue_ahead_after}] (live + event rows) or None.

Trajectories are compared up to the shadow's end; a filled shadow has no queue position after the event that
completed it, so that event's queue-ahead value (a convention: 0 here, the accounting value in queuetruth) is not
compared (its fills are).

Each disagreeing shadow is attributed to the audit category of the oracle notes at its earliest divergence (the
first timestamp where fills, queue ahead, or the first-fill facts differ); if no note sits at that timestamp, the
latest earlier note is used (marked `earlier`), else the shadow is `unexplained`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

CATEGORY_PRIORITY = (
    "own_order_exclusion",
    "cme_modify",
    "hidden_prints",
    "off_price_executions",
    "crosses",
    "oracle_divergence",
)
ET_REASON = {
    "queue_depleted": "queue_depleted",
    "trade_through": "crossed",
    "crossed": "crossed",
    "crossed_at_live": "crossed",
    "never": "never",
}


def _ns(v) -> int | None:
    if v is None:
        return None
    if hasattr(v, "value"):
        return int(v.value)
    if isinstance(v, str):
        return None if v == "" else int(v)
    return int(v)


def _side(v) -> str:
    return v if isinstance(v, str) else chr(int(v))


def normalize_oracle(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for r in results:
        out[r["shadow_id"]] = {
            "shadow_id": r["shadow_id"],
            "price": r["price"],
            "filled_qty": r["filled_qty"],
            "end_state": r["end_state"],
            "first_fill_reason": r["first_fill_reason"],
            "first_fill_liquidity": r["first_fill_liquidity"],
            "ts_first_fill": r["ts_first_fill"],
            "ts_activated_event": r["ts_activated_event"],
            "queue_ahead_at_live": r["queue_ahead_at_live"],
            "ts_end": r["ts_end"],
            "fills": [
                {k: f[k] for k in ("ts_event", "fill_qty", "reason", "liquidity", "fill_px")} for f in r["fills"]
            ],
            "trajectory": [
                {"ts_event": t["ts_event"], "queue_ahead_after": t["queue_ahead_after"]}
                for t in r["trajectory"]
                if t["kind"] in ("live", "event")
            ],
        }
    return out


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    t = pq.read_table(path)
    cols = {}
    for name in t.column_names:
        col = t.column(name)
        if pa.types.is_timestamp(col.type):
            col = col.cast(pa.int64())
        elif pa.types.is_dictionary(col.type):
            col = col.cast(col.type.value_type)
        cols[name] = col.to_pylist()
    n = t.num_rows
    return [{k: v[i] for k, v in cols.items()} for i in range(n)]


def read_qt_results(d: str | Path) -> dict[str, dict[str, Any]]:
    """A queuetruth replay output directory (shadow_results / shadow_fills / queue_trajectory Parquet), or the
    testkit `truth/` directory with the same files."""
    d = Path(d)
    res = _read_parquet_rows(d / "shadow_results.parquet")
    fills: dict[str, list] = {}
    fpath = d / "shadow_fills.parquet"
    if fpath.exists():
        for f in _read_parquet_rows(fpath):
            fills.setdefault(f["shadow_id"], []).append(f)
    traj: dict[str, list] | None = None
    tpath = d / "queue_trajectory.parquet"
    if tpath.exists():
        traj = {}
        for t in _read_parquet_rows(tpath):
            if t["kind"] in ("live", "event"):
                traj.setdefault(t["shadow_id"], []).append(
                    {"ts_event": t["ts_event"], "queue_ahead_after": t["queue_ahead_after"]}
                )
    out = {}
    for r in res:
        sid = r["shadow_id"]
        fl = sorted(fills.get(sid, []), key=lambda f: f["fill_seq"])
        first = fl[0] if fl else None
        out[sid] = {
            "shadow_id": sid,
            "price": r["price"],
            "filled_qty": r["filled_qty"],
            "end_state": r["end_state"],
            "first_fill_reason": r["first_fill_reason"],
            "first_fill_liquidity": first.get("liquidity") if first else None,
            "ts_first_fill": r["ts_first_fill"],
            "ts_activated_event": r["ts_activated_event"],
            "queue_ahead_at_live": r["queue_ahead_at_live"],
            "fills": [
                {
                    "ts_event": f["ts_event"],
                    "fill_qty": f["fill_qty"],
                    "reason": f["reason"],
                    "liquidity": f.get("liquidity"),
                    "fill_px": f["fill_px"],
                }
                for f in fl
            ]
            if fpath.exists()
            else None,
            "trajectory": None if traj is None else traj.get(sid, []),
            "_given": {
                "price",
                "filled_qty",
                "end_state",
                "first_fill_reason",
                "ts_first_fill",
                "ts_activated_event",
                "queue_ahead_at_live",
            },
        }
    return out


def read_et_results(d: str | Path) -> dict[str, dict[str, Any]]:
    """An et engine output directory: shadow_fills.csv (first fill + total) and queue_trace.csv."""
    d = Path(d)
    out = {}
    with open(d / "shadow_fills.csv", newline="") as f:
        for r in csv.DictReader(f):
            ts_fill = _ns(r["ts_fill"])
            out[r["shadow_id"]] = {
                "shadow_id": r["shadow_id"],
                "price": int(r["price"]),
                "filled_qty": int(r["filled_qty"]),
                "end_state": None,
                "first_fill_reason": r["fill_reason"],
                "first_fill_liquidity": None,
                "ts_first_fill": ts_fill,
                "ts_activated_event": None,
                "queue_ahead_at_live": _ns(r["queue_ahead_at_submit"]),
                "fills": None,
                "trajectory": [],
                "_given": {"price", "filled_qty", "first_fill_reason", "ts_first_fill", "queue_ahead_at_live"},
            }
    qpath = d / "queue_trace.csv"
    if qpath.exists():
        with open(qpath, newline="") as f:
            for r in csv.DictReader(f):
                s = out.get(r["shadow_id"])
                if s is not None:
                    s["trajectory"].append({"ts_event": int(r["ts_event"]), "queue_ahead_after": int(r["queue_ahead"])})
    else:
        for s in out.values():
            s["trajectory"] = None
    return out


def read_json_expectations(path: str | Path) -> dict[str, dict[str, Any]]:
    """{"shadows": {shadow_id: {<normalised fields, missing = not compared>}}}."""
    doc = json.loads(Path(path).read_text())
    out = {}
    for sid, s in doc["shadows"].items():
        rec = {
            "shadow_id": sid,
            "price": None,
            "filled_qty": None,
            "end_state": None,
            "first_fill_reason": None,
            "first_fill_liquidity": None,
            "ts_first_fill": None,
            "ts_activated_event": None,
            "queue_ahead_at_live": None,
            "fills": None,
            "trajectory": None,
            "_given": set(s),
        }
        rec.update(s)
        out[sid] = rec
    return out


# ------------------------------------------------------------------------------------------------ comparison


def _cum_by_ts(fills: list[dict]) -> dict[int, int]:
    cum = 0
    out: dict[int, int] = {}
    for f in sorted(fills, key=lambda f: f["ts_event"]):
        cum += f["fill_qty"]
        out[f["ts_event"]] = cum
    return out


def _first_step_divergence(
    a: dict[int, int], b: dict[int, int], start_a: int = 0, start_b: int = 0, until: int | None = None
) -> int | None:
    va, vb = start_a, start_b
    for ts in sorted(set(a) | set(b)):
        if until is not None and ts > until:
            break
        va = a.get(ts, va)
        vb = b.get(ts, vb)
        if va != vb:
            return ts
    return None


def _traj_map(traj: list[dict]) -> dict[int, int]:
    m: dict[int, int] = {}
    for t in traj:
        m[t["ts_event"]] = t["queue_ahead_after"]
    return m


def compare_shadow(
    o: dict, r: dict, *, reason_map: dict[str, str] | None = None, ts_end: int | None = None
) -> list[dict[str, Any]]:
    """Discrepancy items {kind, ts, oracle, reference} for one shadow (empty list = agreement)."""
    given = r.get("_given") or set()

    def has(k: str) -> bool:
        return k in given

    items = []
    if has("price") and r.get("price") is not None and o["price"] != r["price"]:
        items.append(
            {"kind": "lifecycle_price", "ts": o["ts_activated_event"], "oracle": o["price"], "reference": r["price"]}
        )
    if (
        has("ts_activated_event")
        and r.get("ts_activated_event") is not None
        and o["ts_activated_event"] != r["ts_activated_event"]
    ):
        items.append(
            {
                "kind": "lifecycle_activation",
                "ts": o["ts_activated_event"],
                "oracle": o["ts_activated_event"],
                "reference": r["ts_activated_event"],
            }
        )
    if has("queue_ahead_at_live") and o["queue_ahead_at_live"] != r["queue_ahead_at_live"]:
        items.append(
            {
                "kind": "queue_ahead_at_live",
                "ts": o["ts_activated_event"],
                "oracle": o["queue_ahead_at_live"],
                "reference": r["queue_ahead_at_live"],
            }
        )
    if has("filled_qty") and o["filled_qty"] != r["filled_qty"]:
        ts = min([t for t in (o["ts_first_fill"], r["ts_first_fill"]) if t is not None], default=None)
        if r.get("fills") is not None:
            ts = _first_step_divergence(_cum_by_ts(o["fills"]), _cum_by_ts(r["fills"])) or ts
        elif o["ts_first_fill"] == r["ts_first_fill"]:
            # No reference timeline (et): the totals part after the shared first fill. If the oracle filled
            # more, it diverged at the fill that took it past the reference total; otherwise the time is unknown.
            ts = None
            if o["filled_qty"] > r["filled_qty"]:
                ts = next((t for t, c in sorted(_cum_by_ts(o["fills"]).items()) if c > r["filled_qty"]), None)
        items.append({"kind": "filled_qty", "ts": ts, "oracle": o["filled_qty"], "reference": r["filled_qty"]})
    if has("ts_first_fill") and o["ts_first_fill"] != r["ts_first_fill"]:
        ts = min([t for t in (o["ts_first_fill"], r["ts_first_fill"]) if t is not None], default=None)
        items.append({"kind": "first_fill_ts", "ts": ts, "oracle": o["ts_first_fill"], "reference": r["ts_first_fill"]})
    if has("end_state") and r.get("end_state") is not None and o["end_state"] != r["end_state"]:
        items.append({"kind": "end_state", "ts": None, "oracle": o["end_state"], "reference": r["end_state"]})
    if r.get("fills") is not None:
        d = _first_step_divergence(_cum_by_ts(o["fills"]), _cum_by_ts(r["fills"]))
        if d is not None:
            items.append(
                {
                    "kind": "fill_timeline",
                    "ts": d,
                    "oracle": _cum_by_ts(o["fills"]).get(d),
                    "reference": _cum_by_ts(r["fills"]).get(d),
                }
            )
    if has("first_fill_reason") and r.get("first_fill_reason") is not None:
        orr = o["first_fill_reason"]
        rr = r["first_fill_reason"]
        if reason_map:
            orr, rr = reason_map.get(orr, orr), reason_map.get(rr, rr)
        if orr != rr and o["ts_first_fill"] == r["ts_first_fill"]:
            items.append(
                {
                    "kind": "first_fill_reason",
                    "ts": o["ts_first_fill"],
                    "oracle": o["first_fill_reason"],
                    "reference": r["first_fill_reason"],
                }
            )
    rl = r.get("first_fill_liquidity")
    if rl is not None and o["first_fill_liquidity"] != rl:
        items.append(
            {"kind": "liquidity", "ts": o["ts_first_fill"], "oracle": o["first_fill_liquidity"], "reference": rl}
        )
    if r.get("fills") is not None and o["fills"] and r["fills"]:
        opx = [f["fill_px"] for f in o["fills"]]
        rpx = [f["fill_px"] for f in r["fills"]]
        if opx[:1] != rpx[:1] and o["ts_first_fill"] == r["ts_first_fill"]:
            items.append({"kind": "fill_px", "ts": o["ts_first_fill"], "oracle": opx[0], "reference": rpx[0]})
    if r.get("trajectory") is not None and o["trajectory"] and r["trajectory"]:
        ot, rt = _traj_map(o["trajectory"]), _traj_map(r["trajectory"])
        ends = [t for t in (ts_end,) if t is not None]
        until = min(ends) if ends else None
        if until is not None and o.get("end_state") == "filled":
            until -= 1
        d = _first_step_divergence(ot, rt, -1, -1, until)
        if d is not None:
            items.append({"kind": "queue_ahead_trajectory", "ts": d, "oracle": ot.get(d), "reference": rt.get(d)})
    return items


def categorize(notes: list[dict], ts: int | None) -> tuple[str, list[str], bool]:
    """(primary category, all categories at the divergence, earlier) from the oracle notes."""
    if ts is None:
        cats = sorted({n["category"] for n in notes})
        if not cats:
            return "unexplained", [], False
        return _primary(cats), cats, True
    at = sorted({n["category"] for n in notes if n["ts_event"] == ts})
    if at:
        return _primary(at), at, False
    before = [n for n in notes if n["ts_event"] < ts]
    if before:
        last_ts = max(n["ts_event"] for n in before)
        cats = sorted({n["category"] for n in before if n["ts_event"] == last_ts})
        return _primary(cats), cats, True
    return "unexplained", [], False


def _primary(cats: list[str]) -> str:
    for c in CATEGORY_PRIORITY:
        if c in cats:
            return c
    return cats[0]


def build_report(
    oracle_results: list[dict[str, Any]],
    reference: dict[str, dict[str, Any]],
    *,
    reference_kind: str,
    oracle_stats: dict | None = None,
    config: dict | None = None,
    max_rows: int = 2000,
) -> dict[str, Any]:
    reason_map = ET_REASON if reference_kind == "et" else None
    norm = normalize_oracle(oracle_results)
    raw = {r["shadow_id"]: r for r in oracle_results}
    by_cat: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    direction: dict[str, dict[str, int]] = {}
    rows = []
    n_cmp = n_agree = 0
    missing_ref = sorted(set(norm) - set(reference))
    missing_oracle = sorted(set(reference) - set(norm))
    qty_o = qty_r = 0
    for sid in sorted(set(norm) & set(reference)):
        o, r = norm[sid], reference[sid]
        n_cmp += 1
        if r.get("filled_qty") is not None:
            qty_o += o["filled_qty"]
            qty_r += r["filled_qty"]
        items = compare_shadow(o, r, reason_map=reason_map, ts_end=raw[sid]["ts_end"])
        if not items:
            n_agree += 1
            continue
        ts_div = min([i["ts"] for i in items if i["ts"] is not None], default=None)
        cat, cats, earlier = categorize(raw[sid]["notes"], ts_div)
        by_cat[cat] = by_cat.get(cat, 0) + 1
        for i in items:
            by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
        if r.get("filled_qty") is not None and o["filled_qty"] != r["filled_qty"]:
            key = "reference_fills_more" if r["filled_qty"] > o["filled_qty"] else "reference_fills_less"
            direction.setdefault(cat, {}).setdefault(key, 0)
            direction[cat][key] += 1
        if len(rows) < max_rows:
            rows.append(
                {
                    "shadow_id": sid,
                    "category": cat,
                    "categories_at_divergence": cats,
                    "category_from_earlier_note": earlier,
                    "ts_divergence": ts_div,
                    "items": items,
                    "oracle": {
                        k: o[k]
                        for k in (
                            "price",
                            "filled_qty",
                            "ts_first_fill",
                            "first_fill_reason",
                            "first_fill_liquidity",
                            "end_state",
                            "queue_ahead_at_live",
                        )
                    },
                    "reference": {
                        k: r.get(k)
                        for k in (
                            "price",
                            "filled_qty",
                            "ts_first_fill",
                            "first_fill_reason",
                            "first_fill_liquidity",
                            "end_state",
                            "queue_ahead_at_live",
                        )
                    },
                    "notes_at_divergence": [n for n in raw[sid]["notes"] if n["ts_event"] == ts_div][:20],
                }
            )
    from . import LIMITS, ORACLE_VERSION

    return {
        "schema": "qt_oracle.discrepancy_report",
        "oracle_version": ORACLE_VERSION,
        "reference_kind": reference_kind,
        "config": config or {},
        "oracle_limits": list(LIMITS),
        "summary": {
            "n_oracle": len(norm),
            "n_reference": len(reference),
            "n_compared": n_cmp,
            "n_agree": n_agree,
            "n_disagree": n_cmp - n_agree,
            "by_category": dict(sorted(by_cat.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "filled_qty_direction_by_category": direction,
            "filled_qty_total": {"oracle": qty_o, "reference": qty_r},
            "missing_in_reference": len(missing_ref),
            "missing_in_oracle": len(missing_oracle),
        },
        "oracle_stats": oracle_stats or {},
        "discrepancies": rows,
    }


def render_markdown(rep: dict[str, Any], max_rows: int = 40) -> str:
    s = rep["summary"]
    lines = [
        f"# Oracle discrepancy report (reference: {rep['reference_kind']})",
        "",
        f"- shadows compared: {s['n_compared']} (oracle {s['n_oracle']}, reference {s['n_reference']})",
        f"- agree: {s['n_agree']}, disagree: {s['n_disagree']}",
        f"- filled qty total: oracle {s['filled_qty_total']['oracle']}, reference {s['filled_qty_total']['reference']}",
        "",
        "## By category (primary, per shadow)",
        "",
    ]
    lines += [f"- {k}: {v}" for k, v in s["by_category"].items()] or ["- none"]
    lines += ["", "## By discrepancy kind", ""]
    lines += [f"- {k}: {v}" for k, v in s["by_kind"].items()] or ["- none"]
    lines += ["", "## Filled-quantity direction by category", ""]
    lines += [f"- {k}: {v}" for k, v in s["filled_qty_direction_by_category"].items()] or ["- none"]
    if rep.get("oracle_stats"):
        lines += ["", "## Oracle tape statistics", ""]
        lines += [f"- {k}: {v}" for k, v in rep["oracle_stats"].items()]
    if rep.get("oracle_limits"):
        lines += ["", "## Oracle limits", ""]
        lines += [f"- {x}" for x in rep["oracle_limits"]]
    lines += ["", f"## First {max_rows} discrepancies", ""]
    for d in rep["discrepancies"][:max_rows]:
        kinds = ", ".join(f"{i['kind']} (oracle {i['oracle']} vs ref {i['reference']})" for i in d["items"])
        earlier = " (earlier note)" if d["category_from_earlier_note"] else ""
        lines.append(f"- `{d['shadow_id']}` [{d['category']}{earlier}] at {d['ts_divergence']}: {kinds}")
    return "\n".join(lines) + "\n"
