#!/usr/bin/env python3
"""The README's real-data tables, and an aggregate-only summary, from a `reconcile.py` run (stdlib only).

Reads `reconciliation.json` (`qtint.hbt_reconciliation` v1). Prints the Markdown tables of README.md section 2
(engine parity, #312 stops, fill realism on both days and on each day) and, with `--json`, writes
`qtint.hbt_reconciliation_summary` v1: per model and build only counts, rates, CIs and stop times (no per-order
data), the file `evidence/results.json` is built from. `--synthetic` adds a `synthetic_regression.py` summary to it.

Stop times are minutes after the session open; medians are `statistics.median` (the mean of the two middle values
for an even count).

Usage: python integrations/hftbacktest/evidence/readme_tables.py out/recon/reconciliation.json \\
         [--synthetic out/synth/summary.json] [--json integrations/hftbacktest/evidence/results.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

TABLE_HORIZONS = ("1000000000", "10000000000", "60000000000", "300000000000")


def short(mid: str) -> str:
    """`hbt_power3[exch=partial;mode=faithful;n=2]` -> `hbt_power3[n=2]`; `hbt_log[exch=...]` -> `hbt_log`."""
    for k in ("exch=partial;", "exch=no_partial;"):
        mid = mid.replace(k, "")
    return mid.replace("[mode=faithful;", "[").replace("[mode=faithful]", "")


def stop_minutes(day: dict[str, Any]) -> float | None:
    """Minutes from the session open to the build's stop (None: ran to the end or open unknown)."""
    err, o = day.get("error"), day.get("session_open_ns")
    if not err or o is None:
        return None
    return (err["ts"] - o) / 60e9


def stops(eng: dict[str, Any]) -> dict[str, Any]:
    """Stop statistics of one build over its instrument-days."""
    mins = [m for m in (stop_minutes(d) for d in eng["days"].values()) if m is not None]
    ref = [d.get("fills_before_stop_equal_reference") for d in eng["days"].values() if d.get("error")]
    after_zombie = [
        d["error"]["ts"] >= d["first_qt_312_zombie_fill_ts"]
        for d in eng["days"].values()
        if d.get("error") and d.get("first_qt_312_zombie_fill_ts") is not None
    ]
    return {
        "days": len(eng["days"]),
        "days_stopped": sum(1 for d in eng["days"].values() if d.get("error")),
        "stop_minutes_min": min(mins) if mins else None,
        "stop_minutes_median": statistics.median(mins) if mins else None,
        "stop_minutes_max": max(mins) if mins else None,
        "orders_live_before_stop": sum(d["orders_live_before_stop"] for d in eng["days"].values() if d.get("error")),
        "stopped_days_equal_reference": sum(1 for x in ref if x),
        "stopped_days_at_or_after_first_qt_zombie": sum(after_zombie),
        "stopped_days_with_a_qt_zombie": len(after_zombie),
        "stops_at_qt_first_abort": (eng.get("qt_first_abort") or {}).get("engine_stops_at_it"),
    }


def _cell(c: dict[str, Any]) -> dict[str, Any]:
    keys = ("n", "both", "exact_only", "model_only", "neither", "exact_fill_rate", "model_fill_rate", "bias", "ci_lo",
            "ci_hi", "p_mcnemar_exact", "n_clusters")  # fmt: skip
    return {k: c.get(k) for k in keys}


def summary(doc: dict[str, Any], synthetic: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate-only summary of a reconciliation (and optionally the synthetic regression)."""
    out: dict[str, Any] = {
        "schema": "qtint.hbt_reconciliation_summary",
        "schema_version": 1,
        "dataset_id": doc.get("dataset_id"),
        "shadows_sha256": doc.get("shadows_sha256"),
        "qt_zoo_run_id": doc.get("qt_zoo_run_id"),
        "engines": {k: {f: v for f, v in e.items() if f != "python"} for k, e in (doc.get("engines") or {}).items()},
        "l2": doc.get("l2"),
        "n_orders": doc["n_orders"],
        "orders_per_day": doc["orders_per_day"],
        "skipped_shadows": doc["skipped_shadows"],
        "qt_hbt_312_aborts": doc.get("qt_hbt_312_aborts", {}),
        "models": {},
    }
    for mid, m in doc["models"].items():
        mo: dict[str, Any] = {}
        for label, e in m["engines"].items():
            p = e["parity_with_qt_faithful"]
            x: dict[str, Any] = {"parity": {k: v for k, v in p.items() if k != "differ"}, "stops": stops(e)}
            if "realism" in e:
                x["realism_first_fill"] = {h: _cell(c) for h, c in e["realism"].items()}
                x["realism_full_fill"] = {h: _cell(c) for h, c in e.get("realism_full_fill", {}).items()}
                x["realism_first_fill_by_date"] = {
                    d: {h: _cell(c) for h, c in r.items()} for d, r in e.get("realism_by_date", {}).items()
                }
                x["first_fill_delta"] = e.get("first_fill_delta")
            mo[label] = x
        out["models"][mid] = mo
    if synthetic is not None:
        out["synthetic_regression"] = synthetic
    return out


def _pp(c: dict[str, Any]) -> str:
    return f"{100 * c['bias']:+.2f} [{100 * c['ci_lo']:+.2f}, {100 * c['ci_hi']:+.2f}]"


def tables(doc: dict[str, Any], stopped: str = "stock", full: str = "patched") -> str:
    """README section 2 tables (builds named `stopped` and `full`)."""
    s = summary(doc)
    lines = ["ENGINE PARITY", ""]
    lines.append(
        f"| model | {full}: identical fills | {stopped}: orders live before its stop | of which with a fill before "
        f"the stop | identical fills before the stop | {stopped}: instrument-days stopped | zoo pairs that reach #312 |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for mid, mo in s["models"].items():
        pf, ps, st = mo[full]["parity"], mo[stopped]["parity"], mo[stopped]["stops"]
        lines.append(
            f"| `{short(mid)}` | {pf['identical_fills']:,} of {pf['orders_compared']:,} | {ps['orders_compared']:,} "
            f"of {ps['orders']:,} | {ps['orders_with_fills']:,} | {ps['identical_fills']:,} of "
            f"{ps['orders_compared']:,} | {st['days_stopped']} of {st['days']} | "
            f"{s['qt_hbt_312_aborts'].get(mid, '-')} |"
        )
    lines += ["", "STOPS (minutes after the open)", ""]
    lines.append(
        "| model | first stop | median | last stop | stopped days whose fills before the stop equal the reference | "
        "stops at queuetruth's first abort |"
    )
    lines.append("|---|---|---|---|---|---|")
    for mid, mo in s["models"].items():
        st = mo[stopped]["stops"]
        if not st["days_stopped"]:
            continue
        lines.append(
            f"| `{short(mid)}` | {st['stop_minutes_min']:.1f} | {st['stop_minutes_median']:.1f} | "
            f"{st['stop_minutes_max']:.1f} | {st['stopped_days_equal_reference']} of {st['days_stopped']} | "
            f"{st['stops_at_qt_first_abort']} |"
        )
    dates = sorted(next(iter(s["models"].values()))[full].get("realism_first_fill_by_date", {}))
    samples = [
        ("REALISM first fill, both days", "realism_first_fill", None),
        ("REALISM full fill, both days", "realism_full_fill", None),
        *((f"REALISM first fill, {d}", "realism_first_fill_by_date", d) for d in dates),
    ]
    for title, key, sub in samples:
        lines += ["", title, ""]
        lines.append("| model | " + " | ".join(f"{int(h) / 1e9:g} s" for h in TABLE_HORIZONS) + " |")
        lines.append("|---|" + "---|" * len(TABLE_HORIZONS))
        first = True
        for mid, mo in s["models"].items():
            r = mo[full].get(key, {})
            if sub is not None:
                r = r.get(sub, {})
            if not r:
                continue
            if first:
                lines.append(
                    "| exact rate (n) | "
                    + " | ".join(f"{100 * r[h]['exact_fill_rate']:.2f}% ({r[h]['n']:,})" for h in TABLE_HORIZONS)
                    + " |"
                )
                first = False
            lines.append(f"| `{short(mid)}` | " + " | ".join(_pp(r[h]) for h in TABLE_HORIZONS) + " |")
    lines += ["", "FIRST-FILL TIME, hftbacktest minus exact, orders both fill (ms)", ""]
    lines.append("| model | both fill | exact only | hbt only | same ts | p05 | p25 | p50 | p75 | p95 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for mid, mo in s["models"].items():
        f = mo[full].get("first_fill_delta")
        if not f or not f.get("quantiles_ns"):
            continue
        q = f["quantiles_ns"]
        lines.append(
            f"| `{short(mid)}` | {f['n_both']:,} | {f['n_exact_only']:,} | {f['n_model_only']:,} | "
            f"{f['n_same_ts']:,} | "
            + " | ".join(f"{q[k] / 1e6:+.1f}" for k in ("p05", "p25", "p50", "p75", "p95"))
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("reconciliation", type=Path)
    ap.add_argument("--synthetic", type=Path, default=None, help="synthetic_regression.py summary JSON")
    ap.add_argument("--json", type=Path, default=None, help="write the aggregate-only summary here")
    ap.add_argument("--stopped", default="stock", help="label of the build that stops (default stock)")
    ap.add_argument("--full", default="patched", help="label of the build that runs to the end (default patched)")
    args = ap.parse_args(argv)
    doc = json.loads(args.reconciliation.read_text())
    sys.stdout.write(tables(doc, args.stopped, args.full))
    if args.json:
        syn = json.loads(args.synthetic.read_text()) if args.synthetic else None
        args.json.write_text(json.dumps(summary(doc, syn), indent=1, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
