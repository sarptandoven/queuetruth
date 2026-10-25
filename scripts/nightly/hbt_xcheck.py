#!/usr/bin/env python3
"""hftbacktest cross-check for the nightly runner: the released hftbacktest wheel vs queuetruth's faithful hbt models.

Three steps, each in the interpreter that has its dependencies (the nightly runner chains them):

  prepare   (queuetruth venv)  seeded never-cancel shadows on a dataset, each resolved to an absolute price on the
                               zoo's L2 view at its live time (rule of `scripts/nt_crosscheck/run.py prepare`; shadows
                               with no price or that would cross on arrival are dropped, since hftbacktest expires a
                               marketable GTX order), the zoo run of the faithful hbt models on those absolute-price
                               shadows (`exchange = no_partial`), and per instrument-day the hftbacktest event file
                               (`export l2_npz`, same L2 configuration, zero feed latency) with an orders sidecar
                               (submit GTX at the live time, cancel at the planned cancel) and the zoo's first fill per
                               order (`<stem>.zoo_first_fills.json`). The directory is also a valid `QT_ZOO3_DIR` for
                               `python/tests/zoo/test_hbt_crosscheck.py`.
  driver    (hftbacktest venv) `python/tests/zoo/hbt_crosscheck.py <work>/npz --exch no_partial --coarse` (unchanged).
  compare   (queuetruth venv)  order by order: hftbacktest's first fill timestamp against the zoo's, and the filled
                               flag at each zoo horizon; writes `<work>/hbt_report.json`.

Exit codes of `compare`: 0 every compared order agrees, 1 at least one disagreement (or nothing was compared),
2 unusable inputs. hftbacktest is MIT; this script only calls queuetruth and reads the driver's JSON.

Usage (paths relative to the repository root):
  <qt python> scripts/nightly/hbt_xcheck.py prepare --dataset DS --work W [--n-shadows 200] [--seed 0]
  <hbt python> python/tests/zoo/hbt_crosscheck.py W/npz --out W/hbt_runs.json --models M --exch no_partial --coarse
  <qt python> scripts/nightly/hbt_xcheck.py compare --work W
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]
NT_DIR = ROOT / "scripts" / "nt_crosscheck"

# the zoo models compared (short names as hbt_crosscheck.py --models takes them) and their full zoo model ids
MODELS = {
    "hbt_risk_adverse": "hbt_risk_adverse[exch=no_partial;mode=faithful]",
    "hbt_power3[n=2]": "hbt_power3[exch=no_partial;mode=faithful;n=2]",
}
HORIZONS_NS = [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000]
HNAME = {100_000_000: "0.1s", 1_000_000_000: "1s", 10_000_000_000: "10s", 60_000_000_000: "60s"}
# hftbacktest status names written by the driver for orders that never rested
NOT_RESTING = {"expired", "rejected"}
EXIT_OK, EXIT_DISAGREE, EXIT_INPUT = 0, 1, 2
# why a sampled shadow is not handed to hftbacktest (`resolve_price`), counted separately in prepare.json
DROP_REASONS = ("no_abs_price", "no_same_side_best", "improving_offset_crosses", "crosses_at_live")


def _nt_helpers():
    """`load_tape`, `View`, `L2_CFG` and the side codes of the NautilusTrader harness (one price rule for both)."""
    sys.path.insert(0, str(NT_DIR))
    try:
        import run as ntx
    finally:
        sys.path.remove(str(NT_DIR))
    return ntx


def resolve_price(
    side: int, mode: str, price: int | None, offset_ticks: int, tick: int, best_bid, best_ask, s_bid: int
) -> tuple[int | None, str | None]:
    """Absolute price of a shadow on the L2 view at its live time, or None and why it has none.

    `abs` keeps its price; `touch` is the same-side best moved `offset_ticks` toward the spread (a positive offset
    improves); an improving offset that would cross is rejected, as `resolve_price` does in the engine. Reasons:
    `no_abs_price` (an `abs` shadow without a price), `no_same_side_best` (a `touch` shadow on an empty side),
    `improving_offset_crosses` (rejected), `crosses_at_live` (a price was found but it would cross on arrival, where
    hftbacktest expires a GTX order; the price is still returned).
    """
    same, opp = (best_bid, best_ask) if side == s_bid else (best_ask, best_bid)
    if mode == "abs":
        if price is None:
            return None, "no_abs_price"
        px = int(price)
    elif same is None:
        return None, "no_same_side_best"
    else:
        off = int(offset_ticks) * tick
        px = same + off if side == s_bid else same - off
        if offset_ticks > 0 and opp is not None and ((side == s_bid and px >= opp) or (side != s_bid and px <= opp)):
            return None, "improving_offset_crosses"
    crosses = opp is not None and ((side == s_bid and px >= opp) or (side != s_bid and px <= opp))
    return px, ("crosses_at_live" if crosses else None)


def scale(v: int, exp: int) -> float:
    """Integer price or size to real units, bit for bit as `qt_l2::npz::scale` (the npz writer) computes it."""
    return v / 10.0 ** (-exp) if exp < 0 else v * 10.0**exp


def cmd_prepare(a: argparse.Namespace) -> int:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    import queuetruth as qt
    from queuetruth import _core

    ntx = _nt_helpers()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    ds = Path(a.dataset).resolve()
    sampled = work / "shadows_sampled.parquet"
    qt.sample_shadows({"dataset": {"path": str(ds)}, "out": str(sampled), "n": a.n_shadows, "seed": a.seed})
    l2_dir = work / "l2"
    if not l2_dir.exists():
        _core.synthesize_l2(
            json.dumps({"dataset": {"path": str(ds)}, "out": str(l2_dir), "threads": a.threads, "l2": ntx.L2_CFG})
        )
    inst = {
        (r["instrument_id"], str(r["session_date"])): r for r in pq.read_table(ds / "instruments.parquet").to_pylist()
    }

    spec = pq.read_table(sampled)
    rows = spec.to_pylist()
    for r in rows:
        r["_ts_live"] = int(r["ts_submit"].value) + int(r["latency_ns"])
        r["_date"] = str(r["ts_submit"].tz_convert(inst_tz(inst, r["instrument_id"])).date())
    by_part: dict[tuple[int, str], list[dict]] = {}
    for r in rows:
        by_part.setdefault((r["instrument_id"], r["_date"]), []).append(r)

    keep: list[dict] = []
    dropped = dict.fromkeys(DROP_REASONS, 0)
    for (iid, date), items in sorted(by_part.items()):
        tick = int(inst[(iid, date)]["tick_size"])
        tp = ntx.load_tape(partition_path(l2_dir, iid, date))
        view = ntx.View()
        items.sort(key=lambda r: (r["_ts_live"], r["shadow_id"]))
        j = 0
        for r in items:
            while j < len(tp) and tp.ts[j] <= r["_ts_live"]:
                view.apply(tp, j)
                j += 1
            px, why = resolve_price(
                r["side"],
                r["price_mode"],
                r["price"],
                r["offset_ticks"],
                tick,
                view.best_bid(),
                view.best_ask(),
                ntx.S_BID,
            )
            if why is None:
                keep.append({**r, "_px": px})
            else:
                dropped[why] += 1
    if not keep:
        print("[prepare] no shadow rests at its live time", file=sys.stderr)
        return EXIT_INPUT
    keep.sort(key=lambda r: r["shadow_id"])
    ids = {r["shadow_id"] for r in keep}
    abs_spec = spec.filter(pc.is_in(spec["shadow_id"], pa.array(sorted(ids))))
    order = {r["shadow_id"]: r for r in keep}
    sid_col = abs_spec["shadow_id"].to_pylist()
    abs_spec = abs_spec.set_column(
        abs_spec.schema.get_field_index("price"),
        abs_spec.schema.field("price"),
        pa.array([order[s]["_px"] for s in sid_col], abs_spec.schema.field("price").type),
    )
    pm_field = abs_spec.schema.field("price_mode")
    abs_spec = abs_spec.set_column(
        abs_spec.schema.get_field_index("price_mode"),
        pm_field,
        pa.array(["abs"] * len(sid_col), pa.string()).dictionary_encode().cast(pm_field.type),
    )
    abs_spec = abs_spec.set_column(
        abs_spec.schema.get_field_index("offset_ticks"),
        abs_spec.schema.field("offset_ticks"),
        pa.array([0] * len(sid_col), abs_spec.schema.field("offset_ticks").type),
    )
    abs_path = work / "shadows_abs.parquet"
    pq.write_table(abs_spec, abs_path)

    zoo_dir = work / "zoo"
    qt.run_zoo(
        {
            "dataset": {"path": str(ds)},
            "shadows": {"path": str(abs_path)},
            "out": str(zoo_dir),
            "models": list(MODELS),
            "mode": "faithful",
            "exchange": "no_partial",
            "horizons_ns": HORIZONS_NS,
            "l2": ntx.L2_CFG,
            "write_fills": True,
            "threads": a.threads,
        }
    )
    zs = {r["shadow_id"]: r for r in pq.read_table(zoo_dir / "zoo_shadows.parquet").to_pylist()}
    live_mismatch = [s for s in ids if s in zs and int(zs[s]["ts_live"].value) != order[s]["_ts_live"]]
    if live_mismatch:
        print(
            f"[prepare] zoo ts_live differs from ts_submit + latency for {sorted(live_mismatch)[:5]}", file=sys.stderr
        )
        return EXIT_INPUT
    first: dict[str, dict[str, int]] = {}
    for f in pq.read_table(zoo_dir / "zoo_fills.parquet").to_pylist():
        ts = int(f["ts_event"].value)
        cur = first.setdefault(f["model_id"], {})
        if f["shadow_id"] not in cur or ts < cur[f["shadow_id"]]:
            cur[f["shadow_id"]] = ts

    npz_dir = work / "npz"
    npz_dir.mkdir(exist_ok=True)
    orders_map: dict[str, dict[str, Any]] = {}
    oid = 0
    for (iid, date), items in sorted(by_part.items()):
        mine = sorted((order[r["shadow_id"]] for r in items if r["shadow_id"] in ids), key=lambda r: r["shadow_id"])
        if not mine:
            continue
        stem = f"{date}_{iid}"
        npz = npz_dir / f"{stem}.npz"
        qt.export(
            {
                "target": "l2_npz",
                "dataset": {"path": str(ds)},
                "instrument_id": iid,
                "session_date": date,
                "l2": ntx.L2_CFG,
                "feed_latency_ns": 0,
                "out": str(npz),
            }
        )
        meta = inst[(iid, date)]
        actions = []
        zoo_ff: dict[str, dict[str, int | None]] = {m: {} for m in MODELS.values()}
        for r in mine:
            oid += 1
            side = "B" if r["side"] == ntx.S_BID else "A"
            px = scale(r["_px"], meta["price_exp"])
            qty = scale(int(r["qty"]), meta["size_exp"])
            actions.append(
                {
                    "ts": r["_ts_live"],
                    "kind": "submit",
                    "order_id": oid,
                    "side": side,
                    "px": px,
                    "qty": qty,
                    "tif": "GTX",
                }
            )
            cancel = r["ts_cancel"]
            if cancel is not None:
                actions.append(
                    {
                        "ts": int(cancel.value),
                        "kind": "cancel",
                        "order_id": oid,
                        "side": side,
                        "px": px,
                        "qty": qty,
                        "tif": "GTX",
                    }
                )
            orders_map[str(oid)] = {"shadow_id": r["shadow_id"], "scenario": stem, "ts_live": r["_ts_live"]}
            for m in MODELS.values():
                zoo_ff[m][str(oid)] = first.get(m, {}).get(r["shadow_id"])
        actions.sort(key=lambda x: (x["ts"], 0 if x["kind"] == "submit" else 1, x["order_id"]))
        doc = {
            "id": stem,
            "tick_size": scale(int(meta["tick_size"]), meta["price_exp"]),
            "lot_size": scale(int(meta["lot_size"]), meta["size_exp"]),
            "actions": actions,
        }
        (npz_dir / f"{stem}.orders.json").write_text(json.dumps(doc, indent=1) + "\n")
        (npz_dir / f"{stem}.zoo_first_fills.json").write_text(json.dumps(zoo_ff, indent=1, sort_keys=True) + "\n")
    (work / "orders_map.json").write_text(json.dumps(orders_map, indent=1, sort_keys=True) + "\n")
    summary = {
        "n_sampled": len(rows),
        "n_orders": len(ids),
        "dropped": dropped,
        "scenarios": sorted({v["scenario"] for v in orders_map.values()}),
        "models": MODELS,
    }
    (work / "prepare.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(f"[prepare] {len(ids)} orders in {len(summary['scenarios'])} instrument-days; dropped {dropped}")
    return EXIT_OK


def inst_tz(inst: dict, iid: int) -> str:
    for (i, _), r in inst.items():
        if i == iid:
            return r["session_tz"]
    raise SystemExit(f"instrument {iid} is not in instruments.parquet")


def partition_path(l2_dir: Path, iid: int, date: str) -> Path:
    hits = sorted(l2_dir.rglob(f"date={date}/instrument_id={iid}/*.parquet"))
    if len(hits) != 1:
        raise SystemExit(f"expected one L2 partition for instrument {iid} on {date} under {l2_dir}, found {hits}")
    return hits[0]


def compare_runs(runs: list[dict], orders_map: dict[str, dict], zoo_ff: dict[str, dict[str, dict]]) -> dict:
    """Order-by-order comparison of the driver's runs with the zoo's first fills.

    `zoo_ff[scenario][model_id][order_id]` is the zoo's first fill ts (or None). An order that hftbacktest expired or
    rejected on arrival is not compared (it never rested; the price rule should have dropped it, so it is counted).
    A run that stopped on an hftbacktest error (#312, code 14) compares only orders whose live time precedes the
    error, and counts the rest as `excluded_after_error`.
    """
    models: dict[str, dict[str, Any]] = {}
    for r in runs:
        scen, mid = r["scenario"], r["model_id"]
        ours = zoo_ff.get(scen, {}).get(mid)
        if ours is None:
            raise ValueError(f"no zoo first fills for {scen} / {mid}")
        m = models.setdefault(
            mid,
            {
                "orders": 0,
                "compared": 0,
                "agree_first_fill_ts": 0,
                "not_resting_in_hbt": 0,
                "excluded_after_error": 0,
                "hbt_errors": [],
                "horizons": {HNAME[h]: {"agree": 0, "hbt_filled": 0, "zoo_filled": 0} for h in HORIZONS_NS},
                "mismatches": [],
            },
        )
        not_resting = {str(e["order_id"]) for e in r.get("order_events", []) if e.get("status") in NOT_RESTING}
        err_ts = r["error"]["ts"] if r.get("error") else None
        if r.get("error"):
            m["hbt_errors"].append({"scenario": scen, **r["error"]})
        for oid, ff_zoo in sorted(ours.items(), key=lambda kv: int(kv[0])):
            info = orders_map[oid]
            m["orders"] += 1
            if oid in not_resting:
                m["not_resting_in_hbt"] += 1
                continue
            if err_ts is not None and info["ts_live"] >= err_ts:
                m["excluded_after_error"] += 1
                continue
            ff_hbt = r["first_fill"].get(oid)
            if err_ts is not None and ff_zoo is not None and ff_zoo >= err_ts and ff_hbt is None:
                m["excluded_after_error"] += 1
                continue
            m["compared"] += 1
            same = ff_hbt == ff_zoo
            m["agree_first_fill_ts"] += int(same)
            tl = info["ts_live"]
            for h in HORIZONS_NS:
                hb = ff_hbt is not None and ff_hbt <= tl + h
                zb = ff_zoo is not None and ff_zoo <= tl + h
                hz = m["horizons"][HNAME[h]]
                hz["agree"] += int(hb == zb)
                hz["hbt_filled"] += int(hb)
                hz["zoo_filled"] += int(zb)
            if not same and len(m["mismatches"]) < 10:
                m["mismatches"].append(
                    {
                        "scenario": scen,
                        "order_id": int(oid),
                        "shadow_id": info["shadow_id"],
                        "ts_live": tl,
                        "hbt_first_fill": ff_hbt,
                        "zoo_first_fill": ff_zoo,
                    }
                )
    ok = bool(models) and all(m["compared"] > 0 and m["agree_first_fill_ts"] == m["compared"] for m in models.values())
    return {"ok": ok, "models": models}


def cmd_compare(a: argparse.Namespace) -> int:
    work = Path(a.work).resolve()
    try:
        runs_doc = json.loads((work / "hbt_runs.json").read_text())
        orders_map = json.loads((work / "orders_map.json").read_text())
        zoo_ff = {
            p.name.removesuffix(".zoo_first_fills.json"): json.loads(p.read_text())
            for p in sorted((work / "npz").glob("*.zoo_first_fills.json"))
        }
        prep = json.loads((work / "prepare.json").read_text())
        res = compare_runs(runs_doc["runs"], orders_map, zoo_ff)
    except (OSError, ValueError, KeyError) as e:
        print(f"[compare] unusable inputs: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_INPUT
    report = {
        "schema": "qt.nightly.hbt_crosscheck",
        "schema_version": 1,
        "engine": runs_doc.get("engine"),
        "coarse": runs_doc.get("coarse"),
        "n_orders": prep["n_orders"],
        "dropped": prep["dropped"],
        **res,
    }
    (work / "hbt_report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    for mid, m in sorted(res["models"].items()):
        print(
            f"[compare] {mid}: {m['agree_first_fill_ts']}/{m['compared']} first fills agree "
            f"(not resting {m['not_resting_in_hbt']}, after hbt error {m['excluded_after_error']})"
        )
    return EXIT_OK if res["ok"] else EXIT_DISAGREE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--dataset", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--n-shadows", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--threads", type=int, default=2)
    p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("compare")
    p.add_argument("--work", required=True)
    p.set_defaults(fn=cmd_compare)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
