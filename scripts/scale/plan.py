#!/usr/bin/env python3
"""Plan a W1 run: universe, exclusions, per-symbol shadow quotas and bin-packed work units from split manifests.

    uv run --frozen --no-sync python scripts/scale/plan.py --split .realdata/scale/split/12302019 --out plan.json
    uv run --frozen --no-sync python scripts/scale/plan.py --split DIR1 --split DIR2 --quota-rule breadth \\
        --one-symbol-per-unit --out plan.json                                  # the P0 breadth equivalence run

Input: one or more `itch_split.py` output directories (one per day); each bucket lists its symbols with
`[locate, messages, executions]`. A universe entry carries `non_t_spans` (the split's `trading_state`: the spans in
which the symbol's `H` state was not T) when it has any; a source records `trading_state_recorded` (false for a split
made before the split recorded it) and `exclusions_sha256` (the day's exclusions, so a different universe of the same
day gets a different `run_id`). Output: one `qt.scale.plan` document holding the run spec (populations, modes,
horizons, models, seed, quota rule, cost constants), its `run_id` (first 16 hex of the sha256 of the canonical run
spec plus the split sources), the universe and every exclusion with its reason, and the unit specs (`qt.scale.unit`)
that `unit.py` runs.

Quota rules (shadows per symbol-day and population; the sample is `allocation: per_partition`, so a symbol's draws do
not depend on its unit):
- `scale` (default, 07_scale_validation.md 1.5): `min(N_pop, round(N_pop x 20 x executions / 1000))`, with N = 1,000
  standard, 500 large, 500 deep: thin names get about 20 shadows per execution (E, C or P message) and no shadow
  without executions (their exactness checks still run);
- `fixed`: `N_pop` per symbol-day;
- `breadth`: the published breadth run (`scripts/repro/breadth.py`): its populations (3,000 / 2,000 / 2,000 per tier)
  split equally over each tier's symbols in locate order with the remainder to the first ones (the
  `allocation: equal` rule of `sample_shadows` on a tier dataset), symbols outside `TIERS` excluded.

Cost model (CPU seconds per symbol-day, x1.5 safety for other cores): least-squares fit (R^2 0.994) to the telemetry
of the 72 units of the P0 proof (M1 Pro, 2026-10-22; refit after the pilot): records = 1.13 x messages; ingest and
replay checks `1.6e-6 x records`; per population and mode `1.45e-6 x records + 15.3e-9 x shadows x 0.0128 x records
+ 0.8e-3 x shadows` (each shadow sees ~1.28% of a 300 s life in a 23,400 s session; the per-shadow term, the zoo's
17 model pairs plus the block statistics, dominates a full-market day). Units hold the symbols of one bucket,
first-fit decreasing by cost up to `--unit-core-s`; a symbol above it gets a unit of its own.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402

PLAN_SCHEMA = "qt.scale.plan"
RECORDS_PER_MESSAGE = 1.13
COST = {
    "ingest_replay_per_record_s": 1.6e-6,
    "zoo_per_record_run_s": 1.45e-6,
    "zoo_per_shadow_event_mode_s": 15.3e-9,
    "zoo_per_shadow_mode_s": 0.8e-3,
    "shadow_window_share": 300 / 23_400,
    "safety": 1.5,
}
#: Liquidity tiers of a symbol-day: rank by executions (E, C, P messages) among the day's universe.
TIERS_BY_RANK = ((100, "T1_top100"), (500, "T2_101_500"), (1500, "T3_501_1500"), (4000, "T4_1501_4000"))
TIER_REST = "T5_rest"
DEFAULT_HOURS = (34_200, 57_600)
MODAL_USD_PER_CORE_H = 0.063  # preemptible CPU incl. 2 GiB (modal.com/pricing, fetched 2026-10-21)
OVERHEAD = 1.3


def symbol_cost(messages: int, shadows: dict[str, int], modes: int) -> float:
    rec = RECORDS_PER_MESSAGE * messages
    c = rec * COST["ingest_replay_per_record_s"]
    for n in shadows.values():
        if n:
            c += modes * (
                COST["zoo_per_record_run_s"] * rec
                + COST["zoo_per_shadow_event_mode_s"] * n * COST["shadow_window_share"] * rec
                + COST["zoo_per_shadow_mode_s"] * n
            )
    return c * COST["safety"]


def tier_of(rank: int) -> str:
    """Liquidity tier of the symbol ranked `rank` (1 = most executions) on its day."""
    return next((name for cut, name in TIERS_BY_RANK if rank <= cut), TIER_REST)


def session_hours(sm: dict[str, Any]) -> dict[str, int]:
    """`open_s` / `close_s` from the day's system events (`Q` start, `M` end of market hours) when they differ from
    09:30-16:00 (half days); empty otherwise, so a regular day's ingest spec stays the default."""
    ev = sm["stream"].get("system_events_ns_since_midnight") or {}
    if "Q" not in ev or "M" not in ev:
        return {}
    hours = (ev["Q"] // 1_000_000_000, ev["M"] // 1_000_000_000)
    return {} if hours == DEFAULT_HOURS else {"open_s": hours[0], "close_s": hours[1]}


def is_test_symbol(sym: str) -> bool:
    return sym.startswith(K.TEST_SYMBOL_PREFIXES)


def quotas_for(rule: str, pops: dict[str, dict[str, Any]], day: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """{pop: {symbol: n}} for one day; `day` = [{symbol, locate, messages, executions}] of the day's universe."""
    out: dict[str, dict[str, int]] = {p: {} for p in pops}
    if rule == "breadth":
        loc = {d["symbol"]: d["locate"] for d in day}
        for names in K.TIERS.values():
            syms = sorted((s for s in names if s in loc), key=lambda s: loc[s])
            for pop, spec in pops.items():
                q, r = divmod(spec["n"], len(syms)) if syms else (0, 0)
                for i, s in enumerate(syms):
                    out[pop][s] = q + (1 if i < r else 0)
        return out
    for d in day:
        for pop, spec in pops.items():
            n = spec["n"]
            if rule == "scale":
                n = min(n, round(n * 20 * d["executions"] / 1000))
            out[pop][d["symbol"]] = n
    return out


def pack(items: list[tuple[str, float]], cap: float) -> list[list[str]]:
    """First-fit decreasing; ties by name."""
    bins: list[tuple[float, list[str]]] = []
    for name, c in sorted(items, key=lambda x: (-x[1], x[0])):
        for i, (used, names) in enumerate(bins):
            if used + c <= cap:
                bins[i] = (used + c, [*names, name])
                break
        else:
            bins.append((c, [name]))
    return [sorted(b[1]) for b in bins]


def make_plan(a: argparse.Namespace) -> dict[str, Any]:
    pops = K.POPULATIONS if a.quota_rule == "breadth" else K.SCALE_POPULATIONS
    pops = {p: pops[p] for p in a.populations}
    if a.fixed_n:
        pops = {p: {**v, "n": a.fixed_n} for p, v in pops.items()}
    run_spec = {
        "populations": pops,
        "modes": a.modes,
        "horizons_ns": sorted(K.HORIZONS),
        "h_max_ns": K.H_MAX_NS,
        "models": ["all"],
        "seed": a.seed,
        "sample_frac": a.sample_frac,
        "quota_rule": a.quota_rule,
        "block_minutes": a.block_minutes if a.block_minutes is not None else (30 if a.quota_rule == "breadth" else 0),
        "ingest": K.INGEST_DEFAULTS,
        "replay_checks": True,
    }
    sources, universe, exclusions, units = [], [], [], []
    total_cost = 0.0
    for split in a.split:
        sm = json.loads((split / "split_manifest.json").read_text())
        date = sm["date"]
        hours = session_hours(sm)
        trading = sm.get("trading_state")
        spans = (trading or {}).get("non_t_spans") or {}
        day, bucket_of, day_exclusions = [], {}, []
        test_issues = set(sm.get("routing", {}).get("test_issues") or [])
        for b in sm["buckets"]:
            for sym, (loc, msgs, ex) in b["symbols"].items():
                reason = None
                if is_test_symbol(sym):
                    reason = "Nasdaq test symbol"
                elif sym in test_issues:
                    reason = "test issue (Stock Directory authenticity T)"
                elif sym in a.exclude:
                    reason = "excluded by --exclude"
                elif a.quota_rule == "breadth" and not any(sym in t for t in K.TIERS.values()):
                    reason = "not in the breadth TIERS"
                if reason:
                    day_exclusions.append({"date": date, "symbol": sym, "reason": reason})
                    continue
                day.append({"symbol": sym, "locate": loc, "messages": msgs, "executions": ex})
                bucket_of[sym] = b
        exclusions += day_exclusions
        sources.append(
            {
                "date": date,
                "compressed_sha256": sm["stream"]["compressed_sha256"],
                "url": sm["source"]["url"],
                "split_ok": sm["ok"],
                "buckets": len(sm["buckets"]),
                "session_hours": hours or None,
                # the universe depends on the exclusion rules (prefixes, test issues, --exclude), so they enter the
                # run id: a different universe of the same day gets a different run id
                "exclusions_sha256": K.sha256_hex(K.canonical_json(sorted(day_exclusions, key=lambda e: e["symbol"]))),
                "trading_state_recorded": trading is not None,
            }
        )
        quotas = quotas_for(a.quota_rule, pops, day)
        by_bucket: dict[int, list[tuple[str, float]]] = {}
        info = {d["symbol"]: d for d in day}
        ranked = sorted(day, key=lambda d: (-d["executions"], d["symbol"]))
        tier = {d["symbol"]: tier_of(i + 1) for i, d in enumerate(ranked)}
        for d in day:
            sq = {p: quotas[p].get(d["symbol"], 0) for p in pops}
            c = symbol_cost(d["messages"], sq, len(a.modes))
            total_cost += c
            by_bucket.setdefault(bucket_of[d["symbol"]]["bucket"], []).append((d["symbol"], c))
            universe.append(
                {
                    "date": date,
                    "symbol": d["symbol"],
                    "locate": d["locate"],
                    "messages": d["messages"],
                    "executions": d["executions"],
                    "tier": tier[d["symbol"]],
                    "quotas": sq,
                    "cost_core_s": round(c, 3),
                    **({"non_t_spans": spans[d["symbol"]]} if d["symbol"] in spans else {}),
                }
            )
        for bk in sorted(by_bucket):
            items = by_bucket[bk]
            groups = [[n] for n, _ in sorted(items)] if a.one_symbol_per_unit else pack(items, a.unit_core_s)
            b = next(x for x in sm["buckets"] if x["bucket"] == bk)
            for j, syms in enumerate(groups):
                units.append(
                    {
                        "schema": "qt.scale.unit",
                        "unit_id": f"{date}-b{bk:03d}-u{j:04d}",
                        "date": date,
                        "bucket": {"file": b["file"], "file_sha256": b["file_sha256"]},
                        "symbols": syms,
                        "locates": {s: info[s]["locate"] for s in syms},
                        "quotas": {p: {s: quotas[p].get(s, 0) for s in syms} for p in pops},
                        "predicted_core_s": round(sum(c for n, c in items if n in syms), 3),
                        **run_spec,
                        "ingest": {**K.INGEST_DEFAULTS, **hours},
                    }
                )
    run_id = K.sha256_hex(K.canonical_json({"run": run_spec, "sources": sources}))[:16]
    for u in units:
        u["run_id"] = run_id
    core_h = total_cost / 3600
    return {
        "schema": PLAN_SCHEMA,
        "schema_version": 1,
        "run_id": run_id,
        "run": run_spec,
        "sources": sources,
        "cost_model": {**COST, "records_per_message": RECORDS_PER_MESSAGE, "unit_core_s": a.unit_core_s},
        "estimate": {
            "core_h": round(core_h, 3),
            "modal_usd": round(core_h * MODAL_USD_PER_CORE_H * OVERHEAD, 2),
            "note": f"x{COST['safety']} safety in core_h; dollars x{OVERHEAD} overhead at ${MODAL_USD_PER_CORE_H}"
            "/core-h (preemptible, 2 GiB); split and aggregation not included",
            "units": len(units),
            "symbol_days": len(universe),
        },
        "universe": universe,
        "exclusions": exclusions,
        "units": units,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", type=Path, action="append", required=True, help="itch_split.py output dir (per day)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--quota-rule", choices=["scale", "fixed", "breadth"], default="scale")
    ap.add_argument("--fixed-n", type=int, default=None, help="with --quota-rule fixed: shadows per population")
    ap.add_argument("--populations", default="standard,large,deep")
    ap.add_argument("--modes", default="faithful,corrected")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sample-frac", type=float, default=0.01)
    ap.add_argument("--unit-core-s", type=float, default=180.0)
    ap.add_argument(
        "--block-minutes",
        type=int,
        default=None,
        help="block of the summed statistics (default 30 with --quota-rule breadth: the zoo's within-day bootstrap "
        "blocks; else 0 = whole symbol-days, evaluated by the cluster bootstrap)",
    )
    ap.add_argument("--one-symbol-per-unit", action="store_true")
    ap.add_argument("--exclude", default="", help="symbols to exclude (comma separated)")
    a = ap.parse_args(argv)
    a.populations = [p for p in a.populations.split(",") if p]
    a.modes = [m for m in a.modes.split(",") if m]
    a.exclude = {s for s in a.exclude.split(",") if s}
    plan = make_plan(a)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(plan, indent=1) + "\n")
    e = plan["estimate"]
    print(
        f"plan {plan['run_id']}: {e['symbol_days']} symbol-days in {e['units']} units, ~{e['core_h']} core-h, "
        f"~${e['modal_usd']} on Modal; {len(plan['exclusions'])} exclusions -> {a.out}"
    )
    return 0 if math.isfinite(e["core_h"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
