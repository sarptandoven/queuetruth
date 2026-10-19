#!/usr/bin/env python3
"""Print the docs/RESULTS.md tables of a public reproduction (`reproduce.py`) and a breadth run (`breadth.py`).

    uv run --frozen --no-sync python scripts/repro/render_tables.py --repro .realdata/repro/phase1 \
        --breadth .realdata/repro/breadth/breadth.json

Leaderboard tables: fill-rate bias in pp (model mean p_fill minus exact fill rate) with the zoo's day-block bootstrap
95% CI; per-decile / per-bucket cells at 60 s. Breadth tables: per tier and shadow population, bias at 60 s (and the
exact fill rate), both days side by side.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

DAYS = ("2019-12-30", "2020-01-30")
HS = ("1s", "10s", "60s")


def name(mid: str) -> str:
    """`hbt_power2[exch=partial;mode=faithful;n=2]` -> `hbt_power2[n=2]`."""
    m = re.match(r"^([^\[]+)(?:\[(.*)\])?$", mid)
    if not m:
        return mid
    base, params = m.group(1), m.group(2) or ""
    keep = [p for p in params.split(";") if p and not p.startswith(("mode=", "exch="))]
    return f"{base}[{';'.join(keep)}]" if keep else base


def fmt(c: dict | None, ci: bool = True) -> str:
    if not c or c.get("bias") is None:
        return "n/a"
    s = f"{100 * c['bias']:+.2f}"
    if ci and c.get("ci_lo") is not None:
        s += f" [{100 * c['ci_lo']:+.2f}, {100 * c['ci_hi']:+.2f}]"
    return s


def cell(models: dict, mid: str, kind: str, stratum: str, h: str) -> dict | None:
    return (((models.get(mid) or {}).get(kind) or {}).get(stratum) or {}).get(h)


def aborts(mode_doc: dict) -> dict[str, int]:
    out = {}
    for w in mode_doc.get("zoo_warnings") or []:
        if w.startswith("hbt_312_abort:") and " of " in w:
            out[name(w.split(":")[1].strip())] = int(w.split(":")[2].split(" of ")[0])
    return out


def leaderboard(days: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for mode in ("faithful", "corrected"):
        out += [f"\n**{mode} mode** (fill-rate bias in pp, 95% CI)\n"]
        hdr = " | ".join(f"{d} {h.replace('s', ' s')}" for d in DAYS for h in HS)
        out += [f"| model | {hdr} |", "|---|" + "---|" * (len(DAYS) * len(HS))]
        mids = sorted({m for d in DAYS for m in days[d]["modes"][mode]["models"]}, key=name)
        for mid in mids:
            cells = [fmt(cell(days[d]["modes"][mode]["models"], mid, "all", "all", h)) for d in DAYS for h in HS]
            out.append(f"| `{name(mid)}` | " + " | ".join(cells) + " |")
    out += [
        "\n**By queue-ahead decile and liquidity bucket** (faithful mode, 60 s, bias in pp; decile 0 = least queue "
        "ahead)\n",
        "| model | day | queue decile 0 .. 9 | liquidity bucket 0 / 1 / 2 |",
        "|---|---|---|---|",
    ]
    mids = sorted({m for d in DAYS for m in days[d]["modes"]["faithful"]["models"]}, key=name)
    for mid in mids:
        for d in DAYS:
            M = days[d]["modes"]["faithful"]["models"]
            dec = " ".join(
                (f"{100 * c['bias']:+.1f}" if (c := cell(M, mid, "queue_decile", str(i), "60s")) else "n/a")
                for i in range(10)
            )
            liq = " / ".join(
                (f"{100 * c['bias']:+.1f}" if (c := cell(M, mid, "liquidity_bucket", str(i), "60s")) else "n/a")
                for i in range(3)
            )
            out.append(f"| `{name(mid)}` | {d} | {dec} | {liq} |")
    ab = {d: aborts(days[d]["modes"]["faithful"]) for d in DAYS}
    if any(ab.values()):
        out += ["\n**hftbacktest #312 aborts** (faithful mode, shadows of 3,000 left out of the model's metrics)\n"]
        out += ["| model | " + " | ".join(DAYS) + " |", "|---|" + "---|" * len(DAYS)]
        for m in sorted({k for v in ab.values() for k in v}):
            out.append(f"| `{m}` | " + " | ".join(str(ab[d].get(m, 0)) for d in DAYS) + " |")
    return out


def breadth(doc: dict[str, Any], models: list[str], mode: str) -> list[str]:
    out: list[str] = []
    days = [d for d in DAYS if d in doc["days"]]
    for pop in doc["populations"]:
        out += [f"\n**{pop} shadows, {mode} mode, 60 s** (bias in pp, 95% CI; exact = exact fill rate)\n"]
        cols = []
        for d in days:
            cols += [f"{d} exact"] + [f"{d} `{m}`" for m in models]
        out += ["| tier | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        for tier in doc["tiers"]:
            row = []
            for d in days:
                t = (doc["days"][d]["tiers"].get(tier) or {}).get("populations", {}).get(pop) or {}
                md = t.get(mode) or {}
                ex = (md.get("exact_fill_rate") or {}).get("60s") or {}
                row.append(f"{100 * ex['rate']:.1f}% (n {ex['n_defined']})" if ex.get("rate") is not None else "n/a")
                M = md.get("models") or {}
                for m in models:
                    mid = next((k for k in M if name(k) == m), None)
                    row.append(fmt(cell(M, mid, "all", "all", "60s")) if mid else "n/a")
            out.append(f"| {tier} | " + " | ".join(row) + " |")
    return out


def breadth_exactness(doc: dict[str, Any]) -> list[str]:
    out = [
        "\n| tier | day | names | records | events | executions checked | price-priority violations | rank-0 share "
        "(raw / sweep-adj.) | invariants | trade parity mismatches |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for tier in doc["tiers"]:
        for d in DAYS:
            t = (doc["days"].get(d) or {}).get("tiers", {}).get(tier)
            if not t or "replay" not in t:
                continue
            r = t["replay"]
            out.append(
                f"| {tier} | {d} | {len(t['symbols'])} | {r['stats']['records']:,} | {r['stats']['events']:,} | "
                f"{r['executions_checked']:,} | {r['price_priority_violations']} | {r['rank0_share']:.4f} / "
                f"{r['rank0_share_sweep_adjusted']:.4f} | {'all 0' if r['invariants_all_zero'] else r['invariants']} | "
                f"{r['parity'].get('trades_mismatches')} of {r['parity'].get('trades_compared'):,} |"
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repro", type=Path)
    ap.add_argument("--breadth", type=Path)
    ap.add_argument(
        "--breadth-models",
        default="front_of_queue,nt_prob_touch[p=1],nt_queue_l2,hbt_power3[n=2],hbt_risk_adverse,"
        "databento_mc[k=-0.8;particles=100],trade_through_only",
    )
    ap.add_argument("--breadth-mode", default="faithful")
    a = ap.parse_args(argv)
    lines: list[str] = []
    if a.repro:
        days = {d: json.loads((a.repro / "headline" / f"headline-{d}.json").read_text()) for d in DAYS}
        lines += leaderboard(days)
    if a.breadth:
        doc = json.loads(a.breadth.read_text())
        lines += breadth_exactness(doc)
        lines += breadth(doc, a.breadth_models.split(","), a.breadth_mode)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
