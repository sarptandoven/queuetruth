#!/usr/bin/env python3
"""Run one W1 work unit: a few symbol-days of one split bucket -> summable statistics and a shard manifest.

    uv run --frozen --no-sync python scripts/scale/unit.py --unit UNIT.json --split DIR --out SHARD_DIR

A unit spec (`qt.scale.unit`, written by `plan.py`) names the bucket file of `itch_split.py` (with its sha256), the
symbols (with their locates), per-population shadow quotas per symbol, the populations, modes, horizons, models
and seed. The unit:

1. checks the bucket's sha256 and ingests the unit's symbols from it (`qt.ingest`, format itch, or the unit's
   `ingest.format`, `iex_deep_plus` for an `iex_split.py` bucket);
2. runs the replay exactness checks (`qt.replay` with the rank oracle and trade parity) and keeps the summable
   counts (records, events, trades, the invariants, executions checked, price-priority violations, FIFO rank
   histograms, parity counts) and the data-quality verdict; when any flag is non-zero, replays each symbol on its
   own and records which symbols raised which flags (`symbol_exactness.py`, leaf `exactness_by_symbol`; absent
   from exact units, whose leaves are unchanged; a one-symbol unit reuses its own counts; an attribution that
   raises is recorded as incomplete, with the exception type, and the unit still completes);
3. per population draws the shadows with `allocation: per_partition` and the unit's `partition_quotas` (keyed by
   locate), so the shadows of a symbol-day are the same in whichever unit it runs (`crates/replay/src/sample.rs`);
4. per population and mode runs the native zoo (no per-unit bootstrap: the evaluation happens once, in the aggregator)
   and reduces its predictions with `queuetruth.eval.blocks.block_stats` (cell sums per model, population, stratum,
   horizon, symbol-day and `block_minutes` block, 0 = whole day), plus a deterministic 1% shadow sample of the
   predictions for audits (`stable_seed(seed, ["audit_sample", shadow_id]) mod 10000 < frac x 10000`). With
   `pnl_horizons_ns` in the spec, per population one exact replay of its shadows with quotes and trades (`bbo: change`,
   as `zoo-toxicity` replays) and per mode `queuetruth.eval.toxicity.evaluate_toxicity` over the zoo's predictions and
   fill rows (fill window `h_max_ns`, markouts at `pnl_horizons_ns`), its per-shadow PnL terms summed with
   `blocks.pnl_block_stats`; leaf `pnl` holds per population and mode the evaluated shadows, the exclusions and
   warnings, or the error when the step raised (the unit still completes);
5. writes `block_stats.parquet`, `shadows.parquet`, `exact.parquet`, `predictions_sample.parquet` (and
   `pnl_stats.parquet` with `pnl_horizons_ns`) and `shard_manifest.json`. The manifest's `leaf` (unit spec hash, input
   hashes, shadow-file hashes, zoo run ids, content digests of the outputs, exactness and zoo-abort counts) is what the
   run's Merkle root covers; `build`, `telemetry` (records, shadows, CPU s, wall s, peak RSS per step: the planner's
   cost model input) and `timings` are recorded next to it and not hashed.

Everything else (the dataset, shadow files, zoo outputs) lives in a temporary work directory that is deleted.
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402
import symbol_exactness as SE  # noqa: E402

UNIT_SCHEMA = "qt.scale.unit"
SHARD_SCHEMA = "qt.scale.shard_manifest"
INV8 = SE.INV8
SAMPLE_COLS = [
    "model_id",
    "shadow_id",
    "instrument_id",
    "session_date",
    "horizon_ns",
    "p_fill",
    "truth_filled",
    "censored",
    "queue_ahead_at_live",
    "level_qty_at_live",
    "trade_vol_60s",
    "ts_live",
    "qty",
]
STAT_KEYS = [
    "mode",
    "shadow_pop",
    "model_id",
    "population",
    "stratum_kind",
    "stratum",
    "horizon_ns",
    "symbol",
    "session_date",
    "block",
]
PNL_STAT_KEYS = ["mode", "shadow_pop", "model_id", "horizon_ns", "symbol", "session_date", "block"]
#: sort and content-digest keys of every shard output
OUTPUT_KEYS = {
    "block_stats": STAT_KEYS,
    "shadows": ["mode", "shadow_pop", "symbol", "session_date", "block"],
    "exact": ["mode", "shadow_pop", "model_id", "horizon_ns", "symbol", "session_date"],
    "predictions_sample": ["mode", "shadow_pop", "model_id", "shadow_id", "horizon_ns"],
    "pnl_stats": PNL_STAT_KEYS,
}


class Telemetry:
    def __init__(self) -> None:
        self.steps: dict[str, dict[str, float]] = {}

    def step(self, name: str, t0: float, c0: float) -> None:
        s = self.steps.setdefault(name, {"wall_s": 0.0, "cpu_s": 0.0})
        s["wall_s"] = round(s["wall_s"] + time.perf_counter() - t0, 3)
        s["cpu_s"] = round(s["cpu_s"] + time.process_time() - c0, 3)


def _peak_rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1 << 20) if sys.platform == "darwin" else r / 1024, 1)


def _audit_keep(shadow_ids: list[str], seed: int, frac: float) -> list[bool]:
    from queuetruth.stats import stable_seed

    cut = round(frac * 10_000)
    keep = {s: stable_seed(seed, [b"audit_sample", s]) % 10_000 < cut for s in set(shadow_ids)}
    return [keep[s] for s in shadow_ids]


def _zoo_native(spec: dict[str, Any]) -> Any:
    """The Rust zoo run without the per-run Python evaluation (`qt.run_zoo` minus `_evaluate`)."""
    from queuetruth import api
    from queuetruth.schemas import models as m
    from queuetruth.schemas import specs as s

    sp = api._validate(s.ZooSpec, spec)
    return api._native("run_zoo", sp, m.ZooSummary)


def _parse_warnings(warns: list[str]) -> dict[str, Any]:
    aborts: dict[str, int] = {}
    failed: list[str] = []
    for w in warns:
        if w.startswith("hbt_312_abort:") and " of " in w:
            parts = w.split(":")
            aborts[parts[1].strip()] = int(parts[2].split(" of ")[0])
        elif w.startswith("model_failed"):
            failed.append(w.split(":")[1].strip())
    return {"hbt_312_abort": dict(sorted(aborts.items())), "model_failed": sorted(failed), "n_warnings": len(warns)}


def exactness_counts(rs: dict[str, Any]) -> dict[str, Any]:
    """The summable part of a replay summary (exact integer counts; shares are recomputed from them)."""
    ro, par = rs.get("rank_oracle") or {}, rs.get("parity") or {}
    return {
        "stats": {k: rs["stats"][k] for k in ("records", "events", "trades")},
        "invariants": {k: rs["invariants"][k] for k in INV8},
        "executions_checked": ro.get("executions_checked"),
        "executions_unknown_order": ro.get("executions_unknown_order"),
        "price_priority_violations": ro.get("price_priority_violations"),
        "fifo_rank_histogram": ro.get("fifo_rank_histogram"),
        "fifo_rank_histogram_sweep_adjusted": ro.get("fifo_rank_histogram_sweep_adjusted"),
        "sweeps": ro.get("sweeps"),
        "parity": {
            k: par.get(k)
            for k in (
                "trades_compared",
                "trades_mismatches",
                "trades_missing_derived",
                "trades_extra_derived",
                "bbo_compared",
                "bbo_mismatches",
            )
        },
    }


def run_unit(unit: dict[str, Any], split: Path, out: Path, threads: int = 1, work: Path | None = None) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    import queuetruth as qt
    from queuetruth.api import _planned_life
    from queuetruth.eval import blocks as B
    from queuetruth.eval import toxicity as TX

    if unit.get("schema") != UNIT_SCHEMA:
        raise ValueError(f"not a {UNIT_SCHEMA} document")
    t_start, tel = time.perf_counter(), Telemetry()
    out.mkdir(parents=True, exist_ok=True)
    wd = Path(tempfile.mkdtemp(prefix=f"qt-unit-{unit['unit_id']}-", dir=work))
    try:
        bucket = split / unit["bucket"]["file"]
        t0, c0 = time.perf_counter(), time.process_time()
        got = K.sha256_file(bucket)
        if unit["bucket"].get("file_sha256") and got != unit["bucket"]["file_sha256"]:
            raise ValueError(f"{bucket.name}: sha256 {got} != unit spec {unit['bucket']['file_sha256']}")
        tel.step("verify_bucket", t0, c0)

        locates: dict[str, int] = unit["locates"]
        loc2sym = {v: k for k, v in locates.items()}
        ds = wd / "ds"
        t0, c0 = time.perf_counter(), time.process_time()
        qt.ingest(
            {
                "format": "itch",
                "inputs": [{"path": str(bucket)}],
                "out": str(ds),
                "symbols": unit["symbols"],
                **unit.get("ingest", K.INGEST_DEFAULTS),
            }
        )
        tel.step("ingest", t0, c0)
        man = json.loads((ds / "manifest.json").read_text())
        dq = json.loads((ds / "data_quality.json").read_text())
        partitions = [
            {
                "symbol": loc2sym.get(p["instrument_id"], str(p["instrument_id"])),
                "instrument_id": p["instrument_id"],
                "session_date": p["session_date"],
                "rows": p["rows"],
                "sha256": [f["sha256"] for f in p["files"]],
            }
            for p in man["partitions"]
        ]
        present = {p["instrument_id"] for p in partitions}
        records = sum(p["rows"] for p in partitions)

        exactness: dict[str, Any] | None = None
        by_symbol: dict[str, Any] | None = None
        if unit.get("replay_checks", True) and partitions:
            t0, c0 = time.perf_counter(), time.process_time()
            rs = qt.replay(
                {
                    "dataset": {"path": str(ds)},
                    "out": str(wd / "replay"),
                    "threads": threads,
                    "rank_oracle": True,
                    "sweep_gap_ns": 2_000_000,
                    "parity_trades": True,
                }
            )
            tel.step("replay", t0, c0)
            exactness = exactness_counts(rs.model_dump(by_alias=True))
            exactness["dq_verdict"] = dq.get("verdict")
            exactness["dq_reasons"] = dq.get("reasons")
            shutil.rmtree(wd / "replay", ignore_errors=True)
            if SE.is_flagged(exactness):
                t0, c0 = time.perf_counter(), time.process_time()
                try:
                    by_symbol = SE.attribute(ds, exactness, loc2sym, threads, wd)
                except Exception as e:  # the unit's results stand; reduce-day labels all its symbols instead
                    print(f"{unit['unit_id']}: per-symbol attribution failed: {e!r}", file=sys.stderr)
                    by_symbol = SE.failed_attribution(e)
                tel.step("attribute", t0, c0)

        stats, shadows, exact, sample = [], [], [], []
        shadow_files: dict[str, str] = {}
        samples: dict[str, dict[str, int]] = {}
        zoo_ids: dict[str, str] = {}
        zoo_flags: dict[str, Any] = {}
        pnl_stats: list[Any] = []
        pnl_leaf: dict[str, Any] = {}
        pnl_h = [int(x) for x in unit.get("pnl_horizons_ns") or []]
        n_shadows = 0
        h_max = int(unit.get("h_max_ns", K.H_MAX_NS))
        bm = int(unit.get("block_minutes", 30))
        for pop, spec in unit["populations"].items():
            quotas = {
                str(locates[s]): int(n)
                for s, n in unit["quotas"].get(pop, {}).items()
                if s in locates and locates[s] in present
            }
            if not any(quotas.values()):
                continue
            sh = wd / f"shadows-{pop}.parquet"
            t0, c0 = time.perf_counter(), time.process_time()
            samp = qt.sample_shadows(
                {
                    "dataset": {"path": str(ds)},
                    "out": str(sh),
                    "n": 0,
                    "seed": unit["seed"],
                    "allocation": "per_partition",
                    "partition_quotas": quotas,
                    "placements": spec["placements"],
                    "qty": spec["qty"],
                }
            )
            tel.step("sample", t0, c0)
            n_shadows += samp.n
            samples[pop] = {loc2sym.get(int(k), k): int(v) for k, v in samp.by_instrument.items()}
            shadow_files[pop] = K.sha256_file(sh)
            pnl_in: dict[str, Any] | None = None
            if pnl_h:
                # the exact arm's fill rows, trades and quotes of these shadows, once for every mode (zoo-toxicity)
                t0, c0 = time.perf_counter(), time.process_time()
                rp = wd / f"pnl-replay-{pop}"
                try:
                    qt.replay(
                        {
                            "dataset": {"path": str(ds)},
                            "shadows": {"path": str(sh)},
                            "out": str(rp),
                            "seed": unit["seed"],
                            "bbo": "change",
                            "trajectory": "none",
                            "threads": threads,
                        }
                    )
                    pnl_in = {
                        name: pq.read_table(rp / f"{name}.parquet")
                        for name in ("shadow_results", "shadow_fills", "trades", "bbo")
                    }
                    pnl_in["instruments"] = pq.read_table(ds / "instruments.parquet")
                except Exception as e:  # the unit's fill statistics stand; reduce-day counts the gap
                    print(f"{unit['unit_id']}: {pop}: P&L replay failed: {e!r}", file=sys.stderr)
                    for mode in unit["modes"]:
                        pnl_leaf[f"{pop}/{mode}"] = {"error": f"{type(e).__name__}: {e}"}
                shutil.rmtree(rp, ignore_errors=True)
                tel.step("pnl_replay", t0, c0)
            for mode in unit["modes"]:
                zout = wd / f"zoo-{pop}-{mode}"
                t0, c0 = time.perf_counter(), time.process_time()
                summ = _zoo_native(
                    {
                        "dataset": {"path": str(ds)},
                        "shadows": {"path": str(sh)},
                        "out": str(zout),
                        "models": unit["models"],
                        "mode": mode,
                        "threads": threads,
                        "seed": unit["seed"],
                        "horizons_ns": unit["horizons_ns"],
                        "h_max_ns": h_max,
                    }
                )
                tel.step(f"zoo_{mode}", t0, c0)
                t0, c0 = time.perf_counter(), time.process_time()
                pred = pq.read_table(zout / "zoo_predictions.parquet")
                planned, _notes = _planned_life(zout)
                extra = {"mode": mode, "shadow_pop": pop}
                bs = B.block_stats(
                    pred, planned, h_max_ns=h_max, symbols=loc2sym, extra=extra, block_ns=bm * 60 * K.S1 or None
                )
                stats.append(bs["stats"])
                shadows.append(bs["shadows"])
                exact.append(bs["exact"])
                keep = _audit_keep(pred.column("shadow_id").to_pylist(), unit["seed"], unit.get("sample_frac", 0.01))
                smp = pred.filter(pa.array(keep)).select([c for c in SAMPLE_COLS if c in pred.column_names])
                for k, v in extra.items():
                    smp = smp.append_column(k, pa.array([v] * smp.num_rows, pa.utf8()))
                sample.append(smp)
                zoo_ids[f"{pop}/{mode}"] = summ.run_id
                zoo_flags[f"{pop}/{mode}"] = _parse_warnings([str(w) for w in summ.warnings])
                tel.step("block_stats", t0, c0)
                if pnl_in is not None:
                    t0, c0 = time.perf_counter(), time.process_time()
                    try:
                        res = TX.evaluate_toxicity(
                            pred,
                            planned,
                            pq.read_table(zout / "zoo_fills.parquet"),
                            pnl_in["shadow_results"],
                            pnl_in["shadow_fills"],
                            pnl_in["trades"],
                            pnl_in["bbo"],
                            pnl_in["instruments"],
                            window_ns=h_max,
                            horizons_ns=pnl_h,
                            rank_horizon_ns=max(pnl_h),
                            bootstrap_b=1,
                            seed=unit["seed"],
                            last_event_ts=TX._last_event_ts(man),
                            allow_truth_mismatch=True,
                        )
                        pnl_stats.append(
                            B.pnl_block_stats(res.pnl, symbols=loc2sym, extra=extra, block_ns=bm * 60 * K.S1 or None)
                        )
                        pnl_leaf[f"{pop}/{mode}"] = {
                            "n_shadows": res.doc["n_shadows"],
                            "n_excluded": res.doc["n_excluded"],
                            "warnings": res.warnings,
                        }
                    except Exception as e:  # as above: the fill statistics stand, the gap is recorded
                        print(f"{unit['unit_id']}: {pop}/{mode}: P&L failed: {e!r}", file=sys.stderr)
                        pnl_leaf[f"{pop}/{mode}"] = {"error": f"{type(e).__name__}: {e}"}
                    tel.step(f"pnl_{mode}", t0, c0)
                shutil.rmtree(zout, ignore_errors=True)
            sh.unlink(missing_ok=True)

        t0, c0 = time.perf_counter(), time.process_time()
        tables = {
            "block_stats": (B.concat(stats), OUTPUT_KEYS["block_stats"]),
            "shadows": (B.concat(shadows), OUTPUT_KEYS["shadows"]),
            "exact": (B.concat(exact), OUTPUT_KEYS["exact"]),
            "predictions_sample": (B.concat(sample), OUTPUT_KEYS["predictions_sample"]),
        }
        if pnl_h:  # a unit without P&L horizons writes what it always wrote, so its leaf is unchanged
            tables["pnl_stats"] = (B.concat(pnl_stats), OUTPUT_KEYS["pnl_stats"])
        outputs, files = {}, {}
        for name, (t, keys) in tables.items():
            p = out / f"{name}.parquet"
            if t.num_columns:
                t = t.sort_by([(k, "ascending") for k in keys if k in t.column_names])
            pq.write_table(t, p, compression="zstd")
            outputs[name] = {"rows": t.num_rows, "digest": K.table_digest(t, keys)}
            files[name] = {"file": p.name, "bytes": p.stat().st_size, "sha256": K.sha256_file(p)}
        tel.step("write", t0, c0)

        leaf = {
            "unit_id": unit["unit_id"],
            "unit_spec_sha256": K.sha256_hex(K.canonical_json(unit)),
            "inputs": {"bucket_file_sha256": got, "partitions": partitions},
            "missing_symbols": sorted(s for s in unit["symbols"] if locates.get(s) not in present),
            "shadow_files_sha256": shadow_files,
            "zoo_run_ids": zoo_ids,
            "outputs": outputs,
            "exactness": exactness,
            "zoo": zoo_flags,
            "samples": samples,
        }
        if by_symbol is not None:  # only flagged units carry it, so an exact unit's leaf is unchanged
            leaf["exactness_by_symbol"] = by_symbol
        if pnl_h:
            leaf["pnl"] = pnl_leaf
        manifest = {
            "schema": SHARD_SCHEMA,
            "schema_version": 1,
            "unit_id": unit["unit_id"],
            "run_id": unit.get("run_id"),
            "leaf": leaf,
            "leaf_hash": K.leaf_hash(leaf),
            "files": files,
            "build": K.build_info(),
            "telemetry": {
                "records": records,
                "symbols": len(unit["symbols"]),
                "shadows": n_shadows,
                "threads": threads,
                "peak_rss_mb": _peak_rss_mb(),
                "steps": tel.steps,
                "cpu_s": round(sum(s["cpu_s"] for s in tel.steps.values()), 3),
                "wall_s": round(time.perf_counter() - t_start, 3),
            },
        }
        (out / "shard_manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        return manifest
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unit", type=Path, required=True, help="unit spec JSON (one unit, or a plan with --unit-id)")
    ap.add_argument("--unit-id", help="pick this unit from a plan file (`qt.scale.plan`)")
    ap.add_argument("--split", type=Path, required=True, help="directory holding the bucket files")
    ap.add_argument("--out", type=Path, required=True, help="shard directory (outputs + shard_manifest.json)")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--work", type=Path, default=None, help="parent of the temporary work directory")
    a = ap.parse_args(argv)
    doc = json.loads(a.unit.read_text())
    if doc.get("schema") == "qt.scale.plan":
        units = {u["unit_id"]: u for u in doc["units"]}
        if a.unit_id not in units:
            ap.error(f"--unit-id: one of the plan's {len(units)} units")
        doc = units[a.unit_id]
    m = run_unit(doc, a.split, a.out, a.threads, a.work)
    print(json.dumps({"unit_id": m["unit_id"], "leaf_hash": m["leaf_hash"], "telemetry": m["telemetry"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
