#!/usr/bin/env python3
"""Compare a raw-bytes reproduction (`scripts/repro/reproduce.py`) with the published CSV-based numbers.

    uv run --frozen --no-sync python scripts/repro/compare_published.py \
        --repro .realdata/repro/phase1 --published .realdata --out .realdata/repro/phase1/compare.json

The published numbers (docs/RESULTS.md sections 4 and 5) were computed on the earlier prototype's (et) CSV conversion
of the same ITCH days; their artifacts live in the gitignored `.realdata/` of the machine that produced them
(`headline/headline.json`, `rd9/results.json`, `results.json` RD-10, `oracle/<day>/discrepancy_report.json`). This
prints markdown tables (bias in pp) and writes the full cell-by-cell comparison as JSON.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DAYS = ("2019-12-30", "2020-01-30")
HS = ("1s", "10s", "60s")


def pp(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.2f}"


def cell(models: dict, mid: str, kind: str, stratum: str, h: str) -> dict | None:
    return (((models.get(mid) or {}).get(kind) or {}).get(stratum) or {}).get(h)


def compare_headline(pub: dict, rep: dict[str, dict]) -> dict[str, Any]:
    out: dict[str, Any] = {"cells": [], "exact_fill_rate": {}, "strata": []}
    for day in DAYS:
        if day not in rep or day not in pub["days"]:
            continue
        for mode in ("faithful", "corrected"):
            P, R = pub["days"][day]["modes"][mode], rep[day]["modes"][mode]
            out["exact_fill_rate"][f"{day}/{mode}"] = {
                h: {"published": P["exact_fill_rate"][h], "raw_bytes": R["exact_fill_rate"][h]} for h in HS
            }
            for mid in sorted(set(P["models"]) | set(R["models"])):
                for h in HS:
                    a, b = cell(P["models"], mid, "all", "all", h), cell(R["models"], mid, "all", "all", h)
                    row = {"day": day, "mode": mode, "model": mid, "h": h}
                    row["published"] = a and {k: a[k] for k in ("bias", "ci_lo", "ci_hi", "n")}
                    row["raw_bytes"] = b and {k: b[k] for k in ("bias", "ci_lo", "ci_hi", "n")}
                    if a and b and a["bias"] is not None and b["bias"] is not None:
                        row["diff"] = b["bias"] - a["bias"]
                        row["ci_overlap"] = not (b["ci_hi"] < a["ci_lo"] or a["ci_hi"] < b["ci_lo"])
                        row["raw_inside_published_ci"] = a["ci_lo"] <= b["bias"] <= a["ci_hi"]
                    out["cells"].append(row)
                for kind in ("queue_decile", "liquidity_bucket"):
                    pa_ = (P["models"].get(mid) or {}).get(kind) or {}
                    ra_ = (R["models"].get(mid) or {}).get(kind) or {}
                    diffs = []
                    for s in sorted(set(pa_) | set(ra_), key=lambda x: (len(x), x)):
                        a, b = (pa_.get(s) or {}).get("60s"), (ra_.get(s) or {}).get("60s")
                        if a and b and a["bias"] is not None and b["bias"] is not None:
                            diffs.append({"stratum": s, "published": a["bias"], "raw_bytes": b["bias"],
                                          "n_pub": a["n"], "n_raw": b["n"]})  # fmt: skip
                    out["strata"].append({"day": day, "mode": mode, "model": mid, "kind": kind, "cells": diffs})
    d = [c["diff"] for c in out["cells"] if "diff" in c]
    out["summary"] = {
        "n_cells": len(out["cells"]),
        "n_compared": len(d),
        "max_abs_diff_pp": round(100 * max(map(abs, d)), 3) if d else None,
        "mean_abs_diff_pp": round(100 * sum(map(abs, d)) / len(d), 3) if d else None,
        "n_abs_diff_gt_0_5pp": sum(abs(x) > 0.005 for x in d),
        "n_abs_diff_gt_1pp": sum(abs(x) > 0.01 for x in d),
        "n_ci_overlap": sum(bool(c.get("ci_overlap")) for c in out["cells"] if "diff" in c),
        "n_raw_inside_published_ci": sum(bool(c.get("raw_inside_published_ci")) for c in out["cells"] if "diff" in c),
        "models_missing_in_raw_bytes": sorted(
            {c["model"] for c in out["cells"] if c["published"] and not c["raw_bytes"]}
        ),
    }
    return out


def calib_rows(numbers: dict) -> dict[str, Any]:
    rows = {}
    for key in ("alloc/power3", "alloc/databento", "nt_touch/nt_prob_touch", "nt_touch/nt_queue_l2_prob_touch"):
        r = numbers.get(key)
        if not r:
            continue
        ho = next(iter((r.get("held_out") or {}).values()), {})
        bl = next(iter((r.get("baseline_held_out") or {}).values()), {})
        rows[key] = {
            "estimate": r.get("estimate"),
            "ci_bootstrap": r.get("ci_bootstrap"),
            "n_events": r.get("n_events"),
            "brier_fitted": {h: (ho.get("brier") or {}).get(h) for h in ("1000000000", "10000000000", "60000000000")},
            "brier_default": {h: (bl.get("brier") or {}).get(h) for h in ("1000000000", "10000000000", "60000000000")},
            "bias_1s_fitted": (ho.get("fill_rate_bias") or {}).get("1000000000"),
            "bias_1s_default": (bl.get("fill_rate_bias") or {}).get("1000000000"),
        }
    return rows


def bss_rows(numbers: dict) -> dict[str, Any]:
    out = {}
    for tag in ("l2_only", "privileged"):
        sc = numbers.get(f"{tag}_scores_test") or {}
        b = sc.get("bss_vs_climatology") or {}
        out[tag] = {h: b.get(k) for h, k in (("1s", "1000000000"), ("10s", "10000000000"), ("60s", "60000000000"))}
        out[tag]["n_orders"] = numbers.get(f"{tag}_n_orders")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repro", type=Path, required=True)
    ap.add_argument("--published", type=Path, default=Path(".realdata"))
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    res = json.loads((a.repro / "results.json").read_text())
    doc: dict[str, Any] = {"repro": str(a.repro / "results.json"), "build": res.get("build")}
    # leaderboard
    pub_h = json.loads((a.published / "headline" / "headline.json").read_text())
    rep_h = {}
    for day in DAYS:
        p = a.repro / "headline" / f"headline-{day}.json"
        if p.exists():
            rep_h[day] = json.loads(p.read_text())
    doc["headline"] = compare_headline(pub_h, rep_h)
    # calibration
    st = res["steps"]
    if "calib" in st:
        pub9 = json.loads((a.published / "rd9" / "results.json").read_text())["results"]["RD-9"]["numbers"]
        doc["calib"] = {"published": calib_rows(pub9), "raw_bytes": calib_rows(st["calib"].get("numbers") or {})}
        doc["calib"]["export_refused"] = {
            "published": pub9.get("export_hftbacktest_refused") is not None,
            "raw_bytes": (st["calib"].get("numbers") or {}).get("export_hftbacktest_refused") is not None,
        }
    if "hazard" in st:
        pub10 = json.loads((a.published / "results.json").read_text())["results"]["RD-10"]["numbers"]
        doc["hazard"] = {"published": bss_rows(pub10), "raw_bytes": bss_rows(st["hazard"].get("numbers") or {})}
    # oracle
    doc["oracle"] = {}
    for day in DAYS:
        mmddyyyy = f"{day[5:7]}{day[8:10]}{day[:4]}"
        k = f"oracle/{mmddyyyy}"
        pub_rep = a.published / "oracle" / day / "discrepancy_report.json"
        pub = json.loads(pub_rep.read_text())["summary"] if pub_rep.exists() else None
        rb = None
        rb_p = a.published / "itch" / f"realbytes-{mmddyyyy}.json"
        if rb_p.exists():
            o = json.loads(rb_p.read_text())["results"].get("ITCH-O0") or {}
            rb = {x: (o.get("numbers") or {}).get(x) for x in ("n_agree", "n_disagree", "by_category")}
        n = (st.get(k) or {}).get("numbers") or {}
        doc["oracle"][day] = {
            "published_csv_et_mirror": pub
            and {x: pub[x] for x in ("n_compared", "n_agree", "n_disagree", "by_category")},
            "earlier_raw_bytes_et_mirror": rb,
            "raw_bytes_qt_mirror": {x: n.get(x) for x in ("n_agree", "n_disagree", "by_category", "filled_qty_total")},
        }
    if a.out:
        a.out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    # ---- print
    s = doc["headline"]["summary"]
    print("## Leaderboard: raw bytes vs published (CSV), fill-rate bias in pp\n")
    print(json.dumps(s))
    print("\n| day | mode | model | h | published | raw bytes | diff |")
    print("|---|---|---|---|---|---|---|")
    for c in doc["headline"]["cells"]:
        a_, b_ = c["published"], c["raw_bytes"]
        print(
            f"| {c['day']} | {c['mode']} | `{c['model']}` | {c['h']} | {pp(a_ and a_['bias'])} | "
            f"{pp(b_ and b_['bias'])} | {pp(c.get('diff'))} |"
        )
    print("\nexact fill rate:", json.dumps(doc["headline"]["exact_fill_rate"]))
    for k in ("calib", "hazard", "oracle"):
        if k in doc:
            print(f"\n## {k}\n")
            print(json.dumps(doc[k], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
