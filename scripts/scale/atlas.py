#!/usr/bin/env python3
"""The multi-day Fill-Model Bias Atlas from W1 shards: a per-day reducer and a cross-day combiner.

    uv run --frozen --no-sync python scripts/scale/atlas.py reduce-day --plan plan.json --shards DIR --out DAY_DIR \
        [--symbol-flags TRACE_DIR/symbol_flags.json] [--trading-state SPLIT/<date>/split_manifest.json]
    uv run --frozen --no-sync python scripts/scale/atlas.py combine --day DAY_DIR [--day ...] --out ATLAS_DIR \
        [--flag-policy exclude|label] [--allow-legacy-days]

`aggregate.py` holds a whole run in memory; a full-market day at symbol-day granularity is ~51M stats rows, so the
atlas reduces each day on its own first (streaming one shard at a time) and combines the small per-day tables.

reduce-day (one plan = one day): verifies every shard as `aggregate.py` does (leaf hash, unit spec hash, output
content digests), then writes
- `cells_all.parquet`: stratum `all`, summed over blocks, one row per (mode, shadow population, model, population,
  horizon, symbol-day) with the summable fields of `queuetruth.eval.blocks` plus the symbol's liquidity `tier`
  (`plan.py`: rank by the day's executions), `replay_ok` (the symbol-day's own replay checks raised no flag),
  `trading_ok` (no halt, LULD pause or quotation-only span in the regular session: the exact fill rule does not
  model them, finding F2) and `exact_ok` (both);
- `strata_by_tier.parquet`: the queue-decile and liquidity-bucket strata summed over the symbols of each tier, apart
  for `exact_ok` and other symbol-days;
- `pnl_cells.parquet` (when the plan's units computed the P&L error, `plan.py` without `--no-pnl`): the units'
  `pnl_stats` summed over blocks, one row per (mode, shadow population, model, markout horizon, symbol-day) with
  the `blocks.PNL_SUMS` and the same `tier`, `replay_ok`, `trading_ok` and `exact_ok`;
- `day_manifest.json`: run id, date, the day's Merkle root over its leaves (with proofs), invalid / missing shards,
  units whose replay checks flagged anything, `flagged_symbols` (per symbol-day that is not `exact_ok`: unit, replay
  flags, how they were attributed: `leaf`, `symbol_flags` or the whole `unit`, the causes a `trace_flags.py` sidecar
  found, the non-T spans that overlap the session), `trading_state` (whether the day's trading state is known and
  from where), summed exactness per tier, #312 abort counts, telemetry totals.

combine (any number of days; `--flag-policy label`, the default, keeps the flagged symbol-days and marks them,
`exclude` leaves them out of every table and refuses days that cannot say which symbol-days are flagged unless
`--allow-legacy-days`):
- `leaderboard_by_tier.parquet`: per (mode, shadow population, tier incl. `ALL`, model, horizon) the exact fill rate,
  model fill rate and fill-rate bias (never-cancel population) with 95% CIs from the two-way pigeonhole bootstrap
  over dates x symbols (`blocks.cluster_bootstrap`), `overfill_bias` at the last horizon, BH-adjusted p-values
  of the bias across models and horizons within (mode, population, tier), and `n_flagged_symbol_days`,
  `flagged_n`, `flagged_share` (the symbol-days that are not `exact_ok` and their share of `n`; 0 under `exclude`);
- `pnl_by_tier.parquet` (when some day has `pnl_cells`; `pnl_days` in the manifest lists them): per (mode, shadow
  population, tier incl. `ALL`, model, markout horizon) the P&L error of `zoo-toxicity` (`pnl_bias_ticks`: simulated
  minus exact P&L per order placed, in ticks at the mid `horizon_ns` after the first fill; positive = the model's
  P&L is too high), the model's and the exact P&L, its grant / deny / timing parts and `pnl_bias_bps`, with 95%
  CIs from the same two-way bootstrap and BH-adjusted p-values of `pnl_bias_ticks`;
- `per_symbol.parquet`: per symbol, model, horizon, mode and population: the cross-day bias with a day-cluster
  bootstrap CI (symbols seen on >= 2 days), the empirical-Bayes estimate shrunk toward its tier (DerSimonian-Laird),
  and BH-adjusted p-values across symbols per (mode, population, model, horizon); `n_flagged_days` and `flags`
  (date, flags and cause of each flagged day) label the symbols kept under `label`;
- `maps_by_tier.parquet`: bias per tier x queue decile / liquidity bucket with day-cluster bootstrap CIs, and
  `flagged_n`, `flagged_share` (strata are summed per tier, so symbol-days are not counted here);
- `atlas_manifest.json`: every day's root and the root over the day roots in date order (`common.merkle_root`), the
  flag policy and every flagged symbol-day with its flags and causes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402
import symbol_exactness as SE  # noqa: E402

SUMS = ("n", "sum_p", "n_y", "sum_d", "sum_sq", "sum_ll", "qty_n", "qty_num", "qty_den", "b", "c")
CELL_KEYS = ["mode", "shadow_pop", "model_id", "population", "horizon_ns", "symbol", "session_date"]
#: queuetruth.eval.blocks.PNL_SUMS (atlas does not import the engine at module level)
PNL_SUMS = (
    "pnl_n",
    "pnl_grant",
    "pnl_deny",
    "pnl_timing",
    "pnl_model",
    "pnl_exact",
    "pnl_bias",
    "pnl_n_bps",
    "pnl_bias_bps",
)
PNL_CELL_KEYS = ["mode", "shadow_pop", "model_id", "horizon_ns", "symbol", "session_date"]
#: the P&L metrics of `pnl_by_tier` (queuetruth.eval.blocks.METRICS); the first is the headline
PNL_METRICS = (
    "pnl_bias_ticks",
    "model_pnl_ticks",
    "exact_pnl_ticks",
    "pnl_grant_ticks",
    "pnl_deny_ticks",
    "pnl_timing_ticks",
    "pnl_bias_bps",
)
STRATA_KEYS = [
    "mode",
    "shadow_pop",
    "model_id",
    "population",
    "stratum_kind",
    "stratum",
    "horizon_ns",
    "tier",
    "session_date",
    "exact_ok",
]
FLAG_POLICIES = ("exclude", "label")
TF_SCHEMA = "qt.scale.symbol_flags"  # trace_flags.SYMBOL_FLAGS_SCHEMA (trace_flags imports the engine; atlas need not)
NS = 1_000_000_000
TRADING_RULE = (
    "a symbol-day trades normally (`trading_ok`) when no Stock Trading Action (`H`) span with a state other than T "
    "(halted, LULD paused, quotation only, IPO before its cross) overlaps the regular session; the exact fill rule "
    "does not model halts (finding F2 in scripts/scale/findings/README.md, docs/design/spec.md 1.2), so the fill "
    "truth of such a symbol-day is not exact whatever its replay checks say"
)


def _sum_by(t: Any, keys: list[str], sums: tuple[str, ...] = SUMS) -> Any:
    g = t.group_by(keys).aggregate([(f, "sum") for f in sums])
    return g.select([*keys, *[f"{f}_sum" for f in sums]]).rename_columns([*keys, *sums])


def symbol_exactness(
    ok: list[dict[str, Any]], symbol_flags: dict[str, Any] | None = None
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Which symbols of a day's valid shards raised replay-check flags, with their flags, how they were attributed
    and (from a `trace_flags.py` sidecar) their causes; and the problems found (a sidecar that disagrees with a leaf).

    A flagged unit names its flagged symbols in the leaf (`exactness_by_symbol`, `unit.py`); for a shard written
    before that, the sidecar's attribution is used when its entry for the unit traced the same leaf hash and
    reproduced it; with neither, or an incomplete attribution, every symbol of the unit is labelled flagged with the
    unit's flags (`attribution: "unit"`, flags `unit:<field>`), so nothing unverified passes as exact. Causes are
    taken from the sidecar only for a symbol of a unit entry that matches the shard in that way."""
    side_units = (symbol_flags or {}).get("units", {})
    side_syms = (symbol_flags or {}).get("symbols", {})
    status: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for m in ok:
        unit_flags = SE.flags_of(m["leaf"]["exactness"])
        if not unit_flags:
            continue
        attr, source = m["leaf"].get("exactness_by_symbol"), "leaf"
        side = side_units.get(m["unit_id"])
        side_ok = bool(side) and side.get("shard_leaf_hash") == m["leaf_hash"] and side.get("reproduced") is True
        if side_ok:
            if attr is None:
                attr, source = side["exactness_by_symbol"], "symbol_flags"
            elif attr.get("symbols") != side["exactness_by_symbol"].get("symbols"):
                problems.append(f"{m['unit_id']}: symbol_flags attribution differs from the leaf")
        elif side and side.get("shard_leaf_hash") != m["leaf_hash"]:
            problems.append(f"{m['unit_id']}: symbol_flags traced another leaf ({side.get('shard_leaf_hash')})")
        elif side:
            problems.append(f"{m['unit_id']}: symbol_flags did not reproduce the shard")
        if attr and attr.get("complete"):
            named = attr["symbols"]
        else:
            source = "unit"
            flat = {f"unit:{k}": v for k, v in unit_flags.items()}
            named = {p["symbol"]: flat for p in m["leaf"]["inputs"]["partitions"]}
        for sym, flags in named.items():
            info = side_syms.get(sym) if side_ok and source != "unit" else None
            if info is not None and info.get("unit_id") != m["unit_id"]:
                info = None
            status[sym] = {
                "unit_id": m["unit_id"],
                "flags": flags,
                "attribution": source,
                "primary_cause": (info or {}).get("primary_cause"),
                "causes": (info or {}).get("causes", {}),
            }
    return dict(sorted(status.items())), problems


def check_symbol_flags(plan: dict[str, Any], symbol_flags: dict[str, Any] | None) -> list[str]:
    """Why a `qt.scale.symbol_flags` sidecar cannot be used with `plan` (`[]` when it can): another schema, run id
    or date."""
    if symbol_flags is None:
        return []
    date = plan["sources"][0]["date"] if plan.get("sources") else None
    want = {"schema": TF_SCHEMA, "run_id": plan.get("run_id"), "date": date}
    return [
        f"symbol_flags {k} {symbol_flags.get(k)!r} != {v!r}: sidecar not used"
        for k, v in want.items()
        if symbol_flags.get(k) != v
    ]


def session_window_ns(source: dict[str, Any] | None) -> tuple[int, int]:
    """The regular session of a plan source in ns since midnight (`session_hours`, default 09:30-16:00)."""
    hours = (source or {}).get("session_hours") or {}
    return hours.get("open_s", 34_200) * NS, hours.get("close_s", 57_600) * NS


def trading_state(
    plan: dict[str, Any], split_manifest: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, list[list[Any]]]]:
    """The day's trading-state record and, per universe symbol whose `H` state was not T at some time in the regular
    session, the spans that overlap the session (as `itch_split.py` recorded them, unclipped).

    Source: `split_manifest` (a split of the same day and file, for plans made before plans carried the spans;
    checked by date and compressed sha256), else the plan's universe (`non_t_spans`, when the plan's source says the
    split recorded them); otherwise the state is unknown (`known: false`)."""
    src = plan["sources"][0] if plan.get("sources") else {}
    lo, hi = session_window_ns(src)
    info: dict[str, Any] = {"known": False, "source": None, "session_ns": [lo, hi], "rule": TRADING_RULE}
    spans: dict[str, list[list[Any]]] = {}
    if split_manifest is not None:
        got = (split_manifest.get("date"), (split_manifest.get("stream") or {}).get("compressed_sha256"))
        want = (src.get("date"), src.get("compressed_sha256"))
        if got != want:
            info["problem"] = f"trading-state split manifest {got} is not the plan's day {want}: not used"
        elif "trading_state" not in split_manifest:
            info["problem"] = "trading-state split manifest has no trading_state (made before it was recorded)"
        else:
            info |= {"known": True, "source": "split_manifest"}
            spans = split_manifest["trading_state"]["non_t_spans"]
    elif src.get("trading_state_recorded"):
        info |= {"known": True, "source": "plan"}
        spans = {u["symbol"]: u["non_t_spans"] for u in plan["universe"] if u.get("non_t_spans")}
    universe = {u["symbol"] for u in plan["universe"]}
    out = {}
    for sym, sp in sorted(spans.items()):
        hit = [x for x in sp if sym in universe and x[0] < hi and (x[1] is None or x[1] > lo)]
        if hit:
            out[sym] = hit
    info["symbols_not_trading"] = len(out)
    return info, out


def reduce_day(
    plan: dict[str, Any],
    shards: Path,
    out: Path,
    symbol_flags: dict[str, Any] | None = None,
    split_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`symbol_flags`: a `trace_flags.py` sidecar for shards written before units attributed their flags;
    `split_manifest`: the day's `itch_split.py` manifest, for a plan made before plans carried the trading state."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from aggregate import load_shards, sum_exactness

    ok, problems = load_shards(plan, shards, deep=True)
    side_problems = check_symbol_flags(plan, symbol_flags)
    replay_flagged, flag_problems = symbol_exactness(ok, None if side_problems else symbol_flags)
    flag_problems = side_problems + flag_problems
    ts_info, not_trading = trading_state(plan, split_manifest)
    tier = {u["symbol"]: u["tier"] for u in plan["universe"]}
    unit_of = {s: u["unit_id"] for u in plan["units"] for s in u["symbols"]}
    shadows_of = {u["symbol"]: sum(u["quotas"].values()) for u in plan["universe"]}
    flagged_syms: dict[str, dict[str, Any]] = {}
    for sym in sorted(set(replay_flagged) | set(not_trading)):
        rf = replay_flagged.get(sym)
        flagged_syms[sym] = {
            "unit_id": rf["unit_id"] if rf else unit_of.get(sym),
            "replay_ok": rf is None,
            "trading_ok": sym not in not_trading,
            "flags": rf["flags"] if rf else {},
            "attribution": rf["attribution"] if rf else None,
            "primary_cause": rf["primary_cause"] if rf else None,
            "causes": rf["causes"] if rf else {},
            "non_t_spans": not_trading.get(sym, []),
            "planned_shadows": shadows_of.get(sym, 0),
        }
    ts_info["symbols_not_trading_with_shadows"] = sum(1 for s in not_trading if shadows_of.get(s, 0))
    ts_info["symbols_not_trading_replay_ok"] = sum(1 for s in not_trading if s not in replay_flagged)

    def labelled(t: Any) -> Any:
        syms = t.column("symbol").to_pylist()
        t = t.append_column("tier", pa.array([tier.get(s, "?") for s in syms], pa.utf8()))
        t = t.append_column("replay_ok", pa.array([s not in replay_flagged for s in syms], pa.bool_()))
        t = t.append_column("trading_ok", pa.array([s not in not_trading for s in syms], pa.bool_()))
        return t.append_column("exact_ok", pa.array([s not in flagged_syms for s in syms], pa.bool_()))

    flag_cols = ["tier", "replay_ok", "trading_ok", "exact_ok"]
    cells, strata, pnl = [], [], []
    pnl_units, pnl_errors = 0, []
    for m in ok:
        if "pnl_stats" in m["files"]:
            pnl_units += 1
            pnl_errors += [
                {"unit_id": m["unit_id"], "run": k, "error": v["error"]}
                for k, v in sorted((m["leaf"].get("pnl") or {}).items())
                if "error" in v
            ]
            pt = pq.read_table(Path(m["_dir"]) / m["files"]["pnl_stats"]["file"])
            if pt.num_rows:
                pnl.append(_sum_by(labelled(pt), [*PNL_CELL_KEYS, *flag_cols], PNL_SUMS))
        t = pq.read_table(Path(m["_dir"]) / m["files"]["block_stats"]["file"])
        if not t.num_rows:
            continue
        t = labelled(t)
        is_all = pc.equal(t["stratum_kind"], "all")
        cells.append(_sum_by(t.filter(is_all), [*CELL_KEYS, *flag_cols]))
        strata.append(_sum_by(t.filter(pc.invert(is_all)), STRATA_KEYS))
    out.mkdir(parents=True, exist_ok=True)
    cells_t = pa.concat_tables(cells) if cells else pa.table({})
    strata_t = _sum_by(pa.concat_tables(strata), STRATA_KEYS) if strata else pa.table({})
    pq.write_table(cells_t, out / "cells_all.parquet", compression="zstd")
    pq.write_table(strata_t, out / "strata_by_tier.parquet", compression="zstd")
    if pnl_units:
        pnl_t = pa.concat_tables(pnl) if pnl else pa.table({})
        if pnl_t.num_rows:
            pnl_t = pnl_t.sort_by([(k, "ascending") for k in PNL_CELL_KEYS])
        pq.write_table(pnl_t, out / "pnl_cells.parquet", compression="zstd")
    leaves = [m["leaf_hash"] for m in ok]
    by_tier: dict[str, list] = defaultdict(list)
    aborts: dict[str, int] = defaultdict(int)
    flagged = []
    for m in ok:
        ex = m["leaf"]["exactness"]
        tiers = {tier.get(p["symbol"], "?") for p in m["leaf"]["inputs"]["partitions"]}
        for tr in tiers:
            by_tier[tr].append(ex)  # a unit spans tiers: its counts go to each (labelled per unit, see note)
        if SE.is_flagged(ex):
            flagged.append(
                {
                    "unit_id": m["unit_id"],
                    "flags": SE.flags_of(ex),
                    "invariants": {k: v for k, v in ex["invariants"].items() if v},
                    "price_priority_violations": ex["price_priority_violations"],
                    "trades_mismatches": ex["parity"].get("trades_mismatches"),
                    "symbols": m["leaf"]["inputs"]["partitions"]
                    and [p["symbol"] for p in m["leaf"]["inputs"]["partitions"]],
                    "flagged_symbols": sorted(s for s, v in replay_flagged.items() if v["unit_id"] == m["unit_id"]),
                }
            )
        for flags in m["leaf"]["zoo"].values():
            for model, n in flags["hbt_312_abort"].items():
                aborts[model] += n
    tel = [m["telemetry"] for m in ok]
    doc = {
        "schema": "qt.scale.day_manifest",
        "run_id": plan["run_id"],
        "date": plan["sources"][0]["date"] if plan["sources"] else None,
        "source": plan["sources"][0] if plan["sources"] else None,
        "merkle_root": K.merkle_root(leaves),
        "leaves": [
            {"unit_id": m["unit_id"], "leaf_hash": m["leaf_hash"], "proof": K.merkle_proof(leaves, i)}
            for i, m in enumerate(ok)
        ],
        "units_planned": len(plan["units"]),
        "units_valid": len(ok),
        "problems": problems,
        "flagged_units": flagged,
        "flagged_symbols": flagged_syms,
        "symbol_exactness_rule": "a symbol-day's replay checks are clean (`replay_ok`) when every replay-check flag "
        "of its own replay is zero (symbol_exactness.FLAG_FIELDS); it trades normally (`trading_ok`) when no non-T "
        "trading-state span overlaps the regular session (`trading_state.rule`); it is `exact_ok` when both hold. "
        "cells_all carries the three, strata_by_tier `exact_ok`; `atlas.py combine --flag-policy` labels or "
        "excludes the symbol-days that are not exact_ok",
        "symbol_flags_problems": flag_problems,
        "trading_state": ts_info,
        "symbol_days": len(plan["universe"]),
        "symbols_by_tier": {t: sum(1 for u in plan["universe"] if u["tier"] == t) for t in sorted(set(tier.values()))},
        "exclusions": len(plan["exclusions"]),
        "exactness_overall": sum_exactness([m["leaf"]["exactness"] for m in ok]),
        "exactness_note": "per-tier sums count a unit holding several tiers in each of them; overall is exact",
        "exactness_by_unit_tiers": {t: sum_exactness(v) for t, v in sorted(by_tier.items())},
        "zoo_hbt_312_abort": dict(sorted(aborts.items())),
        "pnl": {
            "units_with_pnl": pnl_units,
            "errors": pnl_errors,
            "rule": "pnl_cells.parquet: the P&L terms of zoo-toxicity (queuetruth.eval.toxicity) summed per symbol-day "
            "(blocks.pnl_block_stats, scripts/scale/unit.py); absent when no unit computed them",
        },
        "telemetry": {
            "records": sum(t["records"] for t in tel),
            "shadows": sum(t["shadows"] for t in tel),
            "cpu_s": round(sum(t["cpu_s"] for t in tel), 1),
            "wall_s": round(sum(t["wall_s"] for t in tel), 1),
            "max_unit_wall_s": max((t["wall_s"] for t in tel), default=None),
            "max_peak_rss_mb": max((t["peak_rss_mb"] for t in tel), default=None),
        },
    }
    (out / "day_manifest.json").write_text(json.dumps(doc, indent=1) + "\n")
    res = {k: doc[k] for k in ("run_id", "date", "merkle_root", "units_valid", "units_planned", "symbol_days")}
    return {
        **res,
        "flagged_symbols": len(flagged_syms),
        "replay_flagged_symbols": len(replay_flagged),
        "symbols_not_trading": len(not_trading),
        "trading_state_known": ts_info["known"],
    }


# ---- combine -----------------------------------------------------------------------------------------------------


def _day_bootstrap(num: Any, den: Any, b: int, seed: int, tag: str) -> tuple[Any, Any, Any, Any]:
    """Per row of `num` / `den` (rows x days): (value, ci_lo, ci_hi, se) from a day-cluster bootstrap shared by all
    rows (`block_counts` over the days, stream `bootstrap_seed(seed, "scale_day:<tag>")`)."""
    import numpy as np

    from queuetruth.stats import block_counts, bootstrap_seed

    d = num.shape[1]
    w = block_counts(d, b, bootstrap_seed(seed, f"scale_day:{tag}")).astype(float)
    tot_n, tot_d = num.sum(axis=1), den.sum(axis=1)
    value = np.where(tot_d > 0, tot_n / np.where(tot_d > 0, tot_d, 1.0), np.nan)
    rn, rd = w @ num.T, w @ den.T  # (b, rows)
    with np.errstate(invalid="ignore", divide="ignore"):
        reps = np.where(rd > 0, rn / rd, np.nan)
    lo = np.nanquantile(reps, 0.025, axis=0)
    hi = np.nanquantile(reps, 0.975, axis=0)
    se = np.nanstd(reps, axis=0, ddof=1)
    one = (den > 0).sum(axis=1) < 2  # a single day: no between-day variation to resample
    lo[one] = hi[one] = se[one] = np.nan
    return value, lo, hi, se


def _legacy_flagged(dm: dict[str, Any]) -> list[dict[str, Any]]:
    """A day reduced before per-symbol exactness: every symbol of its flagged units, with the unit's flags."""
    out = []
    for fu in dm.get("flagged_units") or []:
        flags = fu.get("flags") or {
            **(fu.get("invariants") or {}),
            **(
                {"price_priority_violations": fu["price_priority_violations"]}
                if fu.get("price_priority_violations")
                else {}
            ),
            **({"trades_mismatches": fu["trades_mismatches"]} if fu.get("trades_mismatches") else {}),
        }
        flat = {f"unit:{k}": v for k, v in sorted(flags.items())}
        for sym in fu.get("symbols") or []:
            out.append({"symbol": sym, "unit_id": fu["unit_id"], "flags": flat, "attribution": "unit"})
    return out


def _with_exact_ok(t: Any, dm: dict[str, Any]) -> Any:
    """A day table from before per-symbol exactness has no `exact_ok`: every symbol of the day's flagged units is
    marked not exact (the unit-level fallback of `reduce-day`), every other row exact."""
    import pyarrow as pa

    if "exact_ok" in t.column_names:
        return t
    bad = {f["symbol"] for f in _legacy_flagged(dm)}
    return t.append_column("exact_ok", pa.array([s not in bad for s in t.column("symbol").to_pylist()], pa.bool_()))


def _hms(ns: int | None) -> str:
    if ns is None:
        return "end"
    s = ns // NS
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def _label(f: dict[str, Any]) -> str:
    """One flagged symbol-day as text: `<date>: <field>=<count>, ... (<cause>), not trading <from>-<to> (<state>
    <reason>)`."""
    parts = []
    if f.get("flags"):
        cause = f" ({f['primary_cause']})" if f.get("primary_cause") else ""
        parts.append(", ".join(f"{k}={v}" for k, v in sorted(f["flags"].items())) + cause)
    for a, b, state, reason in f.get("non_t_spans") or []:
        parts.append(f"not trading {_hms(a)}-{_hms(b)} ({state}{' ' + reason if reason else ''})")
    return f"{f['date']}: " + ", ".join(parts)


def combine(
    day_dirs: list[Path],
    out: Path,
    bootstrap_b: int = 1000,
    seed: int = 0,
    flag_policy: str = "label",
    allow_legacy_days: bool = False,
) -> dict[str, Any]:
    """`flag_policy`: `label` (default) keeps the symbol-days that are not `exact_ok` (replay checks flagged, or not
    trading in the session) and marks them: `per_symbol.parquet` `n_flagged_days` and `flags`, and every
    `leaderboard_by_tier` / `maps_by_tier` row the flagged share of its shadows; `exclude` drops them from every
    table. `exclude` refuses a day reduced before per-symbol exactness or without a known trading state, unless
    `allow_legacy_days` (then such a day drops every symbol of its flagged units, and halts stay unknown). Either way
    `atlas_manifest.json` lists every flagged symbol-day with its flags, spans and causes."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    from queuetruth.eval import blocks as B

    if flag_policy not in FLAG_POLICIES:
        raise ValueError(f"flag_policy: one of {FLAG_POLICIES}, got {flag_policy!r}")
    days = [(json.loads((d / "day_manifest.json").read_text()), d) for d in day_dirs]
    days = sorted(days, key=lambda x: x[0]["date"][4:] + x[0]["date"][:4])  # MMDDYYYY -> chronological
    no_symbols = [dm["date"] for dm, _ in days if "flagged_symbols" not in dm]
    no_trading = [dm["date"] for dm, _ in days if not (dm.get("trading_state") or {}).get("known")]
    if flag_policy == "exclude" and (no_symbols or no_trading) and not allow_legacy_days:
        raise ValueError(
            f"flag_policy exclude: days without per-symbol exactness {no_symbols} or without a known trading state "
            f"{no_trading} would keep unverified symbol-days; re-reduce them (reduce-day --symbol-flags / "
            "--trading-state) or pass allow_legacy_days"
        )
    cells = pa.concat_tables(
        [_with_exact_ok(pq.read_table(d / "cells_all.parquet"), dm) for dm, d in days], promote_options="default"
    )
    never = pc.equal(cells["population"], "")
    cells = cells.filter(never)
    flagged_days = []
    for dm, _ in days:
        if "flagged_symbols" in dm:
            flagged_days += [{"date": dm["date"], "symbol": s, **v} for s, v in sorted(dm["flagged_symbols"].items())]
        else:
            flagged_days += [{"date": dm["date"], **f} for f in _legacy_flagged(dm)]
    labels: dict[str, list[str]] = defaultdict(list)
    for f in flagged_days:
        labels[f["symbol"]].append(_label(f))
    n_rows_flagged = int(pc.sum(pc.invert(cells["exact_ok"])).as_py() or 0)
    if flag_policy == "exclude":
        cells = cells.filter(cells["exact_ok"])
        if not cells.num_rows:
            raise ValueError("flag_policy exclude: every symbol-day with cells is flagged; nothing left to combine")
    h_max = int(pc.max(cells["horizon_ns"]).as_py())
    out.mkdir(parents=True, exist_ok=True)

    # 1. leaderboard by tier: two-way dates x symbols
    board = []
    tiers = [*sorted(set(cells.column("tier").to_pylist())), "ALL"]
    for mode in sorted(set(cells.column("mode").to_pylist())):
        for pop in sorted(set(cells.column("shadow_pop").to_pylist())):
            base = cells.filter(pc.and_(pc.equal(cells["mode"], mode), pc.equal(cells["shadow_pop"], pop)))
            for tr in tiers:
                st = base if tr == "ALL" else base.filter(pc.equal(base["tier"], tr))
                rows: dict[tuple, dict[str, Any]] = {}
                for metric in ("exact_fill_rate", "model_fill_rate", "fill_rate_bias", "overfill_bias"):
                    for r in B.cluster_bootstrap(
                        st,
                        metric=metric,
                        clusters=("session_date", "symbol"),
                        group=("model_id", "horizon_ns"),
                        bootstrap_b=bootstrap_b,
                        seed=seed,
                    ):
                        if metric == "overfill_bias" and r["horizon_ns"] != h_max:
                            continue
                        row = rows.setdefault(
                            (r["model_id"], r["horizon_ns"]),
                            {
                                "mode": mode,
                                "shadow_pop": pop,
                                "tier": tr,
                                "model_id": r["model_id"],
                                "horizon_ns": r["horizon_ns"],
                                "n": r["n"],
                                "n_dates": r["n_session_date"],
                                "n_symbols": r["n_symbol"],
                            },
                        )
                        for f in ("value", "ci_lo", "ci_hi", "se"):
                            row[f"{metric}_{f}"] = r[f]
                        if metric == "fill_rate_bias":
                            row["fill_rate_bias_p_boot"] = r["p_boot"]
                lb = list(rows.values())
                fl: dict[tuple, list] = defaultdict(lambda: [set(), 0])
                bad = st.filter(pc.invert(st["exact_ok"])).select(
                    ["model_id", "horizon_ns", "symbol", "session_date", "n"]
                )
                for r in bad.to_pylist():
                    e = fl[(r["model_id"], r["horizon_ns"])]
                    e[0].add((r["symbol"], r["session_date"]))
                    e[1] += r["n"]
                for r in lb:
                    days_n = fl.get((r["model_id"], r["horizon_ns"]), [set(), 0])
                    r["n_flagged_symbol_days"] = len(days_n[0])
                    r["flagged_n"] = int(days_n[1])
                    r["flagged_share"] = days_n[1] / r["n"] if r["n"] else None
                adj = B.bh(
                    [
                        r.get("fill_rate_bias_p_boot") if r.get("fill_rate_bias_p_boot") is not None else np.nan
                        for r in lb
                    ]
                )
                for r, q in zip(lb, adj, strict=True):
                    r["fill_rate_bias_p_bh"] = None if np.isnan(q) else float(q)
                board += lb
    pq.write_table(pa.Table.from_pylist(board), out / "leaderboard_by_tier.parquet", compression="zstd")

    # 1b. P&L error by tier (the days whose units computed it), the same two-way bootstrap
    pnl_days = [dm["date"] for dm, d in days if (d / "pnl_cells.parquet").exists()]
    pnl_tables = [pq.read_table(d / "pnl_cells.parquet") for dm, d in days if dm["date"] in pnl_days]
    pnl_cells = (
        pa.concat_tables([t for t in pnl_tables if t.num_rows], promote_options="default")
        if any(t.num_rows for t in pnl_tables)
        else None
    )
    if pnl_cells is not None and flag_policy == "exclude":
        pnl_cells = pnl_cells.filter(pnl_cells["exact_ok"])
    pnl_board = []
    if pnl_cells is not None and pnl_cells.num_rows:
        for mode in sorted(set(pnl_cells.column("mode").to_pylist())):
            for pop in sorted(set(pnl_cells.column("shadow_pop").to_pylist())):
                base = pnl_cells.filter(
                    pc.and_(pc.equal(pnl_cells["mode"], mode), pc.equal(pnl_cells["shadow_pop"], pop))
                )
                for tr in [*sorted(set(base.column("tier").to_pylist())), "ALL"]:
                    st = base if tr == "ALL" else base.filter(pc.equal(base["tier"], tr))
                    rows = {}
                    for metric in PNL_METRICS:
                        for r in B.cluster_bootstrap(
                            st,
                            metric=metric,
                            clusters=("session_date", "symbol"),
                            group=("model_id", "horizon_ns"),
                            bootstrap_b=bootstrap_b,
                            seed=seed,
                        ):
                            row = rows.setdefault(
                                (r["model_id"], r["horizon_ns"]),
                                {
                                    "mode": mode,
                                    "shadow_pop": pop,
                                    "tier": tr,
                                    "model_id": r["model_id"],
                                    "horizon_ns": r["horizon_ns"],
                                    "n": r["n"],
                                    "n_dates": r["n_session_date"],
                                    "n_symbols": r["n_symbol"],
                                },
                            )
                            for f in ("value", "ci_lo", "ci_hi", "se"):
                                row[f"{metric}_{f}"] = r[f]
                            if metric == "pnl_bias_ticks":
                                row["pnl_bias_ticks_p_boot"] = r["p_boot"]
                    lb = list(rows.values())
                    adj = B.bh([r["pnl_bias_ticks_p_boot"] if r["pnl_bias_ticks_p_boot"] is not None else np.nan
                                for r in lb])  # fmt: skip
                    for r, q in zip(lb, adj, strict=True):
                        r["pnl_bias_ticks_p_bh"] = None if np.isnan(q) else float(q)
                    pnl_board += lb
    if pnl_days:
        pq.write_table(pa.Table.from_pylist(pnl_board), out / "pnl_by_tier.parquet", compression="zstd")

    # 2. per symbol across days (vectorised day bootstrap per (mode, pop, model, horizon))
    date_list = sorted(set(cells.column("session_date").to_pylist()))
    di = {d: i for i, d in enumerate(date_list)}
    per = []
    keys = cells.select(["mode", "shadow_pop", "model_id", "horizon_ns"]).to_pylist()
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, k in enumerate(keys):
        groups[(k["mode"], k["shadow_pop"], k["model_id"], k["horizon_ns"])].append(i)
    sym_all = cells.column("symbol").to_pylist()
    day_all = cells.column("session_date").to_pylist()
    tier_all = cells.column("tier").to_pylist()
    ok_all = cells.column("exact_ok").to_pylist()
    flagged_cells: dict[str, set] = defaultdict(set)
    for s_, d_, ok_ in zip(sym_all, day_all, ok_all, strict=True):
        if not ok_:
            flagged_cells[s_].add(d_)
    sum_d = cells.column("sum_d").to_numpy()
    n_all = cells.column("n").to_numpy().astype(float)
    for (mode, pop, model, hz), idx in sorted(groups.items()):
        syms = sorted({sym_all[i] for i in idx})
        si = {s: j for j, s in enumerate(syms)}
        num = np.zeros((len(syms), len(date_list)))
        den = np.zeros((len(syms), len(date_list)))
        tiers_of: dict[str, list[str]] = defaultdict(list)
        for i in idx:
            num[si[sym_all[i]], di[day_all[i]]] += sum_d[i]
            den[si[sym_all[i]], di[day_all[i]]] += n_all[i]
            tiers_of[sym_all[i]].append(tier_all[i])
        value, lo, hi, se = _day_bootstrap(num, den, bootstrap_b, seed, f"{mode}:{pop}:{model}:{hz}")
        home = {s: max(set(v), key=v.count) for s, v in tiers_of.items()}
        post = np.full(len(syms), np.nan)
        for tr in set(home.values()):
            j = [si[s] for s in syms if home[s] == tr]
            post[j] = B.shrink(value[j], se[j])["posterior"]
        # p-values from the day-bootstrap SE (normal approximation; the replicates are not kept per symbol)
        from scipy.stats import norm

        z = np.where(se > 0, value / np.where(se > 0, se, 1.0), np.nan)
        p = 2 * norm.sf(np.abs(z))
        p_bh = B.bh(p)
        n_days = (den > 0).sum(axis=1)
        for s in syms:
            j = si[s]
            per.append(
                {
                    "mode": mode,
                    "shadow_pop": pop,
                    "model_id": model,
                    "horizon_ns": hz,
                    "symbol": s,
                    "tier": home[s],
                    "n": int(den[j].sum()),
                    "n_days": int(n_days[j]),
                    "bias": _f(value[j]),
                    "ci_lo": _f(lo[j]),
                    "ci_hi": _f(hi[j]),
                    "se": _f(se[j]),
                    "bias_shrunk": _f(post[j]),
                    "p_normal": _f(p[j]),
                    "p_bh": _f(p_bh[j]),
                    "n_flagged_days": len(flagged_cells.get(s, ())),
                    "flags": "; ".join(labels[s]) if s in flagged_cells else None,
                }
            )
    pq.write_table(pa.Table.from_pylist(per), out / "per_symbol.parquet", compression="zstd")

    # 3. maps: tier x queue decile / liquidity bucket, day-cluster bootstrap
    st = pa.concat_tables([pq.read_table(d / "strata_by_tier.parquet") for _, d in days], promote_options="default")
    if "exact_ok" not in st.column_names:  # legacy days only: per-tier strata cannot be split by symbol
        st = st.append_column("exact_ok", pa.array([True] * st.num_rows, pa.bool_()))
    st = st.filter(pc.equal(st["population"], ""))
    st = st.set_column(st.column_names.index("exact_ok"), "exact_ok", pc.fill_null(st["exact_ok"], True))
    if flag_policy == "exclude":
        st = st.filter(st["exact_ok"])
    mkeys = ["mode", "shadow_pop", "model_id", "stratum_kind", "stratum", "horizon_ns", "tier"]
    rows = st.select([*mkeys, "session_date", "sum_d", "n", "exact_ok"]).to_pylist()
    cell_idx: dict[tuple, int] = {}
    for r in rows:
        cell_idx.setdefault(tuple(r[k] for k in mkeys), len(cell_idx))
    num = np.zeros((len(cell_idx), len(date_list)))
    den = np.zeros((len(cell_idx), len(date_list)))
    flagged_n = np.zeros(len(cell_idx))
    for r in rows:
        j = cell_idx[tuple(r[k] for k in mkeys)]
        num[j, di[r["session_date"]]] += r["sum_d"]
        den[j, di[r["session_date"]]] += r["n"]
        if not r["exact_ok"]:
            flagged_n[j] += r["n"]
    value, lo, hi, se = _day_bootstrap(num, den, bootstrap_b, seed, "maps")
    maps = [
        dict(zip(mkeys, k, strict=True))
        | {
            "n": int(den[j].sum()),
            "n_days": int((den[j] > 0).sum()),
            "bias": _f(value[j]),
            "ci_lo": _f(lo[j]),
            "ci_hi": _f(hi[j]),
            "se": _f(se[j]),
            "flagged_n": int(flagged_n[j]),
            "flagged_share": float(flagged_n[j] / den[j].sum()) if den[j].sum() > 0 else None,
        }
        for k, j in cell_idx.items()
    ]
    pq.write_table(pa.Table.from_pylist(maps), out / "maps_by_tier.parquet", compression="zstd")

    roots = [d["merkle_root"] for d, _ in days]
    manifest = {
        "schema": "qt.scale.atlas_manifest",
        "schema_version": 1,
        "atlas_root": K.merkle_root(roots),
        "rule": "Merkle root over the day roots in date order; each day root covers that day's shard leaves",
        "days": [
            {
                "date": d["date"],
                "run_id": d["run_id"],
                "merkle_root": d["merkle_root"],
                "units_valid": d["units_valid"],
                "units_planned": d["units_planned"],
                "problems": len(d["problems"]),
                "flagged_units": len(d["flagged_units"]),
                "symbol_days": d["symbol_days"],
                "source_url": (d.get("source") or {}).get("url"),
                "source_sha256": (d.get("source") or {}).get("compressed_sha256"),
            }
            for d, _ in days
        ],
        "symbol_days": sum(d["symbol_days"] for d, _ in days),
        "distinct_symbols": len(set(sym_all)),
        "flag_policy": flag_policy,
        "pnl_days": pnl_days,
        "flagged_symbol_days": flagged_days,
        "flagged_cell_rows": n_rows_flagged,
        "days_without_symbol_exactness": no_symbols,
        "days_without_trading_state": no_trading,
        "allow_legacy_days": allow_legacy_days,
        "bootstrap_b": bootstrap_b,
        "seed": seed,
        "build": K.build_info(),
    }
    (out / "atlas_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return {
        "atlas_root": manifest["atlas_root"],
        "days": len(days),
        "symbol_days": manifest["symbol_days"],
        "distinct_symbols": manifest["distinct_symbols"],
        "flag_policy": flag_policy,
        "flagged_symbol_days": len(flagged_days),
        "pnl_days": len(pnl_days),
    }


def _f(x: Any) -> float | None:
    import math

    return None if x is None or not math.isfinite(float(x)) else float(x)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reduce-day")
    r.add_argument("--plan", type=Path, required=True)
    r.add_argument("--shards", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--symbol-flags", type=Path, help="trace_flags.py symbol_flags.json for shards without attribution")
    r.add_argument(
        "--trading-state",
        type=Path,
        help="the day's itch_split.py split_manifest.json with trading_state, for a plan made before plans carried it",
    )
    c = sub.add_parser("combine")
    c.add_argument("--day", type=Path, action="append", required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--bootstrap-b", type=int, default=1000)
    c.add_argument("--flag-policy", choices=FLAG_POLICIES, default="label")
    c.add_argument(
        "--allow-legacy-days",
        action="store_true",
        help="with --flag-policy exclude: accept days without per-symbol exactness or trading state",
    )
    a = ap.parse_args(argv)
    if a.cmd == "reduce-day":
        sf = json.loads(a.symbol_flags.read_text()) if a.symbol_flags else None
        sm = json.loads(a.trading_state.read_text()) if a.trading_state else None
        print(json.dumps(reduce_day(json.loads(a.plan.read_text()), a.shards, a.out, sf, sm)))
    else:
        r = combine(a.day, a.out, a.bootstrap_b, flag_policy=a.flag_policy, allow_legacy_days=a.allow_legacy_days)
        print(json.dumps(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
