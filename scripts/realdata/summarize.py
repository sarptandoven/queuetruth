"""Aggregate-only summary of a `make realdata` run for docs/RESULTS.md: reads `<realdata>/results.json` (validate_et),
optionally `headline.json` (headline.py), the oracle summaries and the qtbench results, and prints Markdown tables with
the sha256 of every source file (provenance). Never prints event-level rows."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

KEY_CHECKS = {
    "RD-1": ["records", "rows_by_instrument", "instruments open_s/close_s", "trades_source rows"],
    "RD-1b": ["records", "rows_by_instrument"],
    "RD-2": [
        "stats.records",
        "stats.events",
        "stats.trades",
        "invariants (8, all 0)",
        "rank_oracle.executions_checked",
        "rank_oracle.price_priority_violations",
        "rank0_share",
        "rank0_share_sweep_adjusted",
        "rank_oracle.sweeps",
        "rank_oracle.sweeps_multi_fill",
        "parity.trades_mismatches",
        "parity.bbo_mismatches",
        "RD-3 bbo per-instrument sha256 (et render)",
        "RD-4 rank_trace sha256 (sweep ids relabelled)",
    ],
}
KEY_CHECKS["RD-6"] = KEY_CHECKS["RD-2"]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def short(v: Any) -> str:
    if isinstance(v, dict) and all(isinstance(x, str) and len(x) == 64 for x in v.values()):
        return f"{len(v)} sha256 digests"
    if isinstance(v, bool) or v is None:
        return json.dumps(v)
    if isinstance(v, float):
        return f"{v:,.0f}" if abs(v) >= 1e4 else repr(v)
    if isinstance(v, int):
        return f"{v:,}"
    s = json.dumps(v)
    return s if len(s) <= 90 else s[:87] + "..."


def rd_table(res: dict[str, Any]) -> list[str]:
    out = ["| RD | check | expected | got | ok |", "|---|---|---|---|---|"]
    for rd, d in res["results"].items():
        if rd.startswith("_"):
            continue
        st = d.get("status")
        if st in ("skipped", "error"):
            out.append(f"| {rd} | ({st}) {d.get('error', '') or ''} | | | {st} |")
            continue
        for c in d.get("checks", []):
            want = c.get("want")
            got = c.get("got", c.get("detail"))
            out.append(
                f"| {rd} | {c['name']} | {short(want) if want is not None else ''} | {short(got)} | "
                f"{'yes' if c['ok'] else '**no**'} |"
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--headline", type=Path)
    ap.add_argument("--oracle", type=Path, nargs="*", default=[])
    ap.add_argument("--bench", type=Path)
    a = ap.parse_args(argv)
    res = json.loads(a.results.read_text())
    print(
        f"source: {a.results} sha256 {sha(a.results)}; wall {res.get('wall_s')} s; status {res.get('status_counts')}\n"
    )
    print("\n".join(rd_table(res)))
    if a.headline and a.headline.exists():
        h = json.loads(a.headline.read_text())
        print(f"\nheadline: {a.headline} sha256 {sha(a.headline)}")
        for date, day in h["days"].items():
            for mode, m in day["modes"].items():
                ex = {k: f"{v['n_filled']}/{v['n_defined']}" for k, v in m["exact_fill_rate"].items()}
                print(f"\n{date} {mode}: exact filled {ex}; zoo wall {m['wall_s']} s")
                print("| model | 1 s | 10 s | 60 s | 60 s decile range | 60 s liquidity range |")
                print("|---|---|---|---|---|---|")
                for mid, st in sorted(m["models"].items()):
                    a_ = st["all"]["all"]
                    cells = [
                        f"{100 * a_[k]['bias']:+.2f} [{100 * a_[k]['ci_lo']:+.2f}, {100 * a_[k]['ci_hi']:+.2f}]"
                        for k in ("1s", "10s", "60s")
                    ]
                    rng = []
                    for kind in ("queue_decile", "liquidity_bucket"):
                        v = [
                            c["60s"]["bias"]
                            for c in st.get(kind, {}).values()
                            if "60s" in c and c["60s"]["bias"] is not None
                        ]
                        rng.append(f"{100 * min(v):+.2f} .. {100 * max(v):+.2f}" if v else "")
                    print(f"| `{mid}` | " + " | ".join(cells + rng) + " |")
    for p in a.oracle:
        s = p / "oracle_summary.json"
        r = p / "discrepancy_report.json"
        if r.exists():
            d = json.loads(r.read_text())
            keep = {
                k: v for k, v in d.items() if not isinstance(v, list | dict) or k in ("summary", "counts", "totals")
            }
            print(f"\noracle {p.name}: {r} sha256 {sha(r)}: {json.dumps(keep)[:1500]}")
        elif s.exists():
            print(f"\noracle {p.name}: {s} sha256 {sha(s)}: {s.read_text()[:1500]}")
    if a.bench and a.bench.exists():
        b = json.loads(a.bench.read_text())
        print(
            f"\nbench: {a.bench} sha256 {sha(a.bench)}; env {b.get('env', {}).get('env_class')}; "
            f"load {b.get('env', {}).get('load_avg_start')} -> {b.get('env', {}).get('load_avg_end')}"
        )
        print("| case | unit | median | p10 | p90 | target | met |")
        print("|---|---|---|---|---|---|---|")
        for c in b.get("cases", []):
            sm = c.get("summary") or {}
            t = c.get("target") or {}
            print(
                f"| {c['id']} | {c.get('unit')} | {sm.get('median')} | {sm.get('p10')} | {sm.get('p90')} | "
                f"{t.get('op', '')} {t.get('value', '')} | {t.get('met', '')} |"
            )
        for d in b.get("derived", []):
            sm = d.get("summary") or {}
            t = d.get("target") or {}
            print(
                f"| {d['id']} (derived) | {d.get('kind')} | {sm.get('median')} | {sm.get('p10')} | {sm.get('p90')} | "
                f"{t.get('op', '')} {t.get('value', '')} | {t.get('met', '')} |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
