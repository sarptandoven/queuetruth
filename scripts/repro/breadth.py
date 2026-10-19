#!/usr/bin/env python3
"""Breadth run: the L2 queue-model leaderboard by liquidity tier on 36 more Nasdaq ITCH symbols, from the public files.

    uv run --frozen --no-sync python scripts/repro/breadth.py                       # both public days
    uv run --frozen --no-sync python scripts/repro/breadth.py --dates 12302019 --tiers mid,small

Per public day: one streaming pass over the day file keeps the messages of all `TIERS` symbols
(`scripts/realdata/itch_subset.py --tag breadth36 --allow-missing`; the raw day is never stored). Then, per tier:

- `qt.ingest(format="itch", symbols=<tier>)` and `qt.replay` with the rank oracle and trade parity (8 invariants,
  price-priority violations, rank-0 share, trade parity), i.e. the exactness checks on names never checked before;
- three never-cancel shadow populations from `qt.sample_shadows` (seed 0, equal allocation over the tier's names,
  submit window and 300 s planned life as in the published run):
  `standard` (the published defaults: 1 lot or the median order size at touch offsets 0 / -1 / -2 / +1),
  `large` (1,000 or 2,000 shares, i.e. 10x / 20x the 100-share median of US stocks, same offsets) and
  `deep` (the standard sizes 3 and 5 ticks behind the touch);
- the 17-model zoo in faithful and corrected mode at 1 s / 10 s / 60 s / 300 s; fill-rate bias vs the exact arm with
  the zoo's day-block bootstrap CI, overall, by queue-ahead decile and by liquidity bucket.

The tier of a name is fixed in advance (`TIERS`: mega-cap, low-priced large caps with deep queues, ETFs, mid-cap,
small-cap); the run also records each name's measured activity (full-day ITCH messages, records, prints, mean
displayed depth is not computed) so the labels can be checked. Outputs: `<out>/breadth.json` (aggregates only,
written after every tier) and the subset manifests / directory files. Datasets and zoo directories are deleted after
each tier; the day's subset is deleted after the day unless `--keep-subsets` (its manifest pins the sha256, so it can
be re-streamed). Free space on / is checked before every step (abort below `--min-free-gib`, default 2.5).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "scripts" / "realdata"))
sys.path.insert(0, str(HERE))

import validate_et as V  # noqa: E402
from reproduce import DiskGuard, build_info, free_gib, guard, sha256_file  # noqa: E402

DATES = {"12302019": "2019-12-30", "01302020": "2020-01-30"}
TIERS: dict[str, list[str]] = {
    "mega": ["AMZN", "GOOGL", "FB", "NVDA", "INTC"],
    "deep_queue": ["BAC", "F", "GE", "SIRI", "T", "PFE"],
    "etf": ["QQQ", "IWM", "XLF", "EEM", "TLT"],
    "mid": ["ROKU", "ETSY", "DOCU", "TTD", "ULTA", "FFIV", "JBHT", "CHRW", "ZBRA", "LULU"],
    "small": ["PLUG", "GPRO", "SPWR", "OSTK", "FCEL", "MARA", "RIOT", "HIMX", "KTOS", "AMRS"],
}
TAG = "breadth36"
STD_PLACEMENTS = [{"offset_ticks": 0, "weight": 0.6}, {"offset_ticks": -1, "weight": 0.2},
                  {"offset_ticks": -2, "weight": 0.1}, {"offset_ticks": 1, "weight": 0.1}]  # fmt: skip
STD_QTY = [{"kind": "lots", "lots": 1, "weight": 0.5}, {"kind": "median_order_size", "weight": 0.5}]
POPULATIONS: dict[str, dict[str, Any]] = {
    "standard": {"n": 3000, "placements": STD_PLACEMENTS, "qty": STD_QTY},
    "large": {
        "n": 2000,
        "placements": STD_PLACEMENTS,
        "qty": [{"kind": "lots", "lots": 1000, "weight": 0.5}, {"kind": "lots", "lots": 2000, "weight": 0.5}],
    },
    "deep": {
        "n": 2000,
        "placements": [{"offset_ticks": -3, "weight": 0.5}, {"offset_ticks": -5, "weight": 0.5}],
        "qty": STD_QTY,
    },
}
S1 = 1_000_000_000
HORIZONS = {S1: "1s", 10 * S1: "10s", 60 * S1: "60s", 300 * S1: "300s"}
STRATA = ("all", "queue_decile", "liquidity_bucket", "instrument")


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def zoo_board(ds: Path, sh: Path, zout: Path, mode: str, threads: int) -> dict[str, Any]:
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    import queuetruth as qt

    t0 = time.perf_counter()
    summ = qt.run_zoo(
        {
            "dataset": {"path": str(ds)},
            "shadows": {"path": str(sh)},
            "out": str(zout),
            "models": ["all"],
            "mode": mode,
            "threads": threads,
            "seed": 0,
            "horizons_ns": sorted(HORIZONS),
        }
    )
    wall = round(time.perf_counter() - t0, 2)
    err = pq.read_table(zout / "zoo_errors.parquet").to_pylist()
    pred = pq.read_table(
        zout / "zoo_predictions.parquet", columns=["model_id", "shadow_id", "horizon_ns", "truth_filled", "censored"]
    )
    mids = sorted(set(pred.column("model_id").to_pylist()))
    truth: dict[str, Any] = {}
    if mids:
        one = pred.filter(pc.equal(pred["model_id"], mids[0]))
        for h, lab in HORIZONS.items():
            rows = one.filter(pc.equal(one["horizon_ns"], h)).to_pylist()
            defined = [r for r in rows if not r["censored"] and r["truth_filled"] is not None]
            k = sum(bool(r["truth_filled"]) for r in defined)
            truth[lab] = {"n_defined": len(defined), "n_filled": k, "rate": (k / len(defined)) if defined else None}
    models: dict[str, Any] = defaultdict(lambda: defaultdict(dict))
    for r in err:
        if r["metric"] != "fill_rate_bias" or r["horizon_ns"] not in HORIZONS or r["stratum_kind"] not in STRATA:
            continue
        cell = {"bias": r["value"], "ci_lo": r["ci_lo"], "ci_hi": r["ci_hi"], "n": r["n"]}
        models[r["model_id"]][r["stratum_kind"]].setdefault(r["stratum"], {})[HORIZONS[r["horizon_ns"]]] = cell
    warns = [str(w) for w in (getattr(summ, "warnings", []) or [])]
    return {
        "wall_s": wall,
        "n_models": len(models),
        "exact_fill_rate": truth,
        "models": json.loads(json.dumps(models)),
        "model_failed": sorted({w.split(":")[1].strip() for w in warns if w.startswith("model_failed")}),
        "hbt_312_abort": {
            w.split(":")[1].strip(): int(w.split(":")[2].split(" of ")[0])
            for w in warns
            if w.startswith("hbt_312_abort:") and " of " in w
        },
        "zoo_warnings_n": len(warns),
    }


class Breadth:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.out: Path = a.out
        self.work = self.out / "work"
        self.path = self.out / "breadth.json"
        self.msg_rank: dict[str, dict[str, int]] = {}
        self.doc: dict[str, Any] = (
            json.loads(self.path.read_text())
            if a.resume and self.path.exists()
            else {
                "schema": "qt.repro.breadth",
                "schema_version": 1,
                "generator": "scripts/repro/breadth.py",
                "tiers": TIERS,
                "populations": POPULATIONS,
                "horizons": list(HORIZONS.values()),
                "days": {},
            }
        )
        self.doc.setdefault("runs", []).append({"started_at": now_iso(), "build": build_info(), "args": {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(a).items()}})  # fmt: skip

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        self.path.write_text(V.jdump(self.doc) + "\n")

    def symbols(self) -> list[str]:
        return [s for t in TIERS.values() for s in t]

    def subset(self, date: str) -> tuple[Path, dict] | None:
        itch = self.out / "itch"
        sub, man = itch / f"{date}.subset.{TAG}.itch50.gz", itch / f"{date}.subset.{TAG}.manifest.json"
        if sub.exists() and man.exists():
            m = json.loads(man.read_text())
            if sha256_file(sub) == m["subset"]["file_sha256"]:
                return sub, m
        guard(self.a.min_free_gib + 0.5, f"stream {date}")
        cmd = [sys.executable, "-I", str(REPO / "scripts" / "realdata" / "itch_subset.py"), "--date", date]
        cmd += ["--symbols", ",".join(self.symbols()), "--tag", TAG, "--allow-missing", "--out", str(itch)]
        cmd += ["--min-free-gib", str(self.a.min_free_gib)]
        print(f"[stream {date}] {len(self.symbols())} symbols ...", file=sys.stderr, flush=True)
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True)
        (self.out / f"stream-{date}.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr[-20000:])
        print(f"[stream {date}] exit {r.returncode} in {time.perf_counter() - t0:.0f} s", file=sys.stderr, flush=True)
        if not sub.exists():
            return None
        return sub, json.loads(man.read_text())

    def load_rank(self, date: str) -> None:
        p = self.out / "itch" / f"{date}.directory.json"
        if p.exists():
            d = json.loads(p.read_text())["symbols"]
            order = sorted(d, key=lambda s: -d[s][1])
            self.msg_rank[date] = {s: i + 1 for i, s in enumerate(order)}
            self.doc["days"][DATES[date]]["n_directory_symbols"] = len(d)

    def tier(self, date: str, tier: str, sub: Path, sman: dict) -> dict[str, Any]:
        import queuetruth as qt

        day = DATES[date]
        found = sman["selection"]["locates"]
        syms = [s for s in TIERS[tier] if s in found]
        res: dict[str, Any] = {"symbols": syms, "missing": [s for s in TIERS[tier] if s not in found]}
        ds = self.work / f"ds-{tier}"
        shutil.rmtree(ds, ignore_errors=True)
        t0 = time.perf_counter()
        qt.ingest(
            {
                "format": "itch",
                "inputs": [{"path": str(sub)}],
                "venue": "nasdaq",
                "out": str(ds),
                "symbols": syms,
                "price_exp": -9,
                "tick_size": "0.01",
                "lot_size": "1",
            }
        )
        man = json.loads((ds / "manifest.json").read_text())
        dq = json.loads((ds / "data_quality.json").read_text())
        res["ingest"] = {
            "wall_s": round(time.perf_counter() - t0, 2),
            "records": sum(p["rows"] for p in man["partitions"]),
            "records_by_symbol": {},
            "dataset_mb": V.du_mb(ds),
            "verdict": dq.get("verdict"),
            "reasons": dq.get("reasons"),
        }
        loc2sym = {v: k for k, v in found.items()}
        for p in man["partitions"]:
            res["ingest"]["records_by_symbol"][loc2sym.get(p["instrument_id"], str(p["instrument_id"]))] = p["rows"]
        rank = self.msg_rank.get(date, {})
        res["activity"] = {
            s: {
                "itch_messages": sman["selection"]["messages_by_symbol"].get(s),
                "message_rank_of_all_symbols": rank.get(s),
                "hidden_prints": (sman["selection"]["prints_by_symbol"].get(s) or {}).get("P"),
            }
            for s in syms
        }
        guard(self.a.min_free_gib, f"replay {tier}")
        rout = self.work / f"replay-{tier}"
        shutil.rmtree(rout, ignore_errors=True)
        t0 = time.perf_counter()
        rs = V.to_dict(
            qt.replay(
                {
                    "dataset": {"path": str(ds)},
                    "out": str(rout),
                    "threads": self.a.threads,
                    "rank_oracle": True,
                    "sweep_gap_ns": 2_000_000,
                    "parity_trades": True,
                }
            )
        )
        ro, par = rs["rank_oracle"], rs["parity"]
        res["replay"] = {
            "wall_s": round(time.perf_counter() - t0, 2),
            "stats": {k: rs["stats"][k] for k in ("records", "events", "trades")},
            "invariants": {k: rs["invariants"][k] for k in V.INV8},
            "invariants_all_zero": all(rs["invariants"][k] == 0 for k in V.INV8),
            "executions_checked": ro["executions_checked"],
            "price_priority_violations": ro["price_priority_violations"],
            "rank0_share": ro["rank0_share"],
            "rank0_share_sweep_adjusted": ro["rank0_share_sweep_adjusted"],
            "parity": {k: v for k, v in par.items() if not isinstance(v, (list, dict))},
        }
        shutil.rmtree(rout, ignore_errors=True)
        res["populations"] = {}
        for pop in self.a.populations:
            spec = POPULATIONS[pop]
            sh = self.work / f"shadows-{tier}-{pop}.parquet"
            pr: dict[str, Any] = {}
            try:
                samp = qt.sample_shadows(
                    {
                        "dataset": {"path": str(ds)},
                        "out": str(sh),
                        "n": spec["n"],
                        "seed": 0,
                        "placements": spec["placements"],
                        "qty": spec["qty"],
                    }
                )
                pr["sample"] = {k: v for k, v in V.to_dict(samp).items() if k in ("n", "by_instrument", "by_placement")}
                pr["shadows_sha256"] = sha256_file(sh)
                for mode in self.a.modes:
                    guard(self.a.min_free_gib, f"zoo {tier}/{pop}/{mode}")
                    zout = self.work / f"zoo-{tier}-{pop}-{mode}"
                    shutil.rmtree(zout, ignore_errors=True)
                    pr[mode] = zoo_board(ds, sh, zout, mode, self.a.threads)
                    shutil.rmtree(zout, ignore_errors=True)
                    print(
                        f"  [{day} {tier} {pop} {mode}] {pr[mode]['n_models']} models, exact 60s "
                        f"{(pr[mode]['exact_fill_rate'].get('60s') or {}).get('rate')}, {pr[mode]['wall_s']} s",
                        file=sys.stderr,
                        flush=True,
                    )
            except DiskGuard:
                raise
            except Exception as e:
                pr["error"] = f"{type(e).__name__}: {e}"
                pr["traceback"] = traceback.format_exc()[-3000:]
            finally:
                sh.unlink(missing_ok=True)
            res["populations"][pop] = pr
        shutil.rmtree(ds, ignore_errors=True)
        return res

    def main(self) -> int:
        self.work.mkdir(parents=True, exist_ok=True)
        rc = 0
        try:
            for date in self.a.dates:
                day = DATES[date]
                dd = self.doc["days"].setdefault(day, {"tiers": {}})
                todo = [
                    t
                    for t in self.a.tiers
                    if not (self.a.resume and t in dd["tiers"] and "error" not in dd["tiers"][t])
                ]
                if not todo:
                    continue
                got = self.subset(date)
                if got is None:
                    dd["error"] = "stream failed (see stream log)"
                    rc = 1
                    self.save()
                    continue
                sub, sman = got
                self.load_rank(date)
                dd["subset"] = {
                    "file_sha256": sman["subset"]["file_sha256"],
                    "framed_sha256": sman["subset"]["framed_sha256"],
                    "messages": sman["subset"]["messages"],
                    "file_bytes": sman["subset"]["file_bytes"],
                    "source_sha256": sman["stream"]["compressed_sha256"],
                    "source_url": sman["source"]["url"],
                    "manifest_ok": sman["ok"],
                    "missing_symbols": sman["selection"].get("missing_symbols"),
                    "stream_wall_s": sman["timings"]["wall_s"],
                }
                for tier in todo:
                    guard(self.a.min_free_gib, f"tier {tier}")
                    print(f"[{day} {tier}] ... (free {free_gib():.2f} GiB)", file=sys.stderr, flush=True)
                    t0 = time.perf_counter()
                    try:
                        dd["tiers"][tier] = self.tier(date, tier, sub, sman)
                    except DiskGuard:
                        raise
                    except Exception as e:
                        dd["tiers"][tier] = {
                            "error": f"{type(e).__name__}: {e}",
                            "traceback": traceback.format_exc()[-3000:],
                        }
                        rc = 1
                    dd["tiers"][tier]["wall_s"] = round(time.perf_counter() - t0, 1)
                    self.save()
                if not self.a.keep_subsets:
                    sub.unlink(missing_ok=True)
                    dd["subset"]["deleted_after_run"] = True
                self.save()
        except RuntimeError as e:
            self.doc["aborted"] = str(e)
            print(f"ABORT: {e}", file=sys.stderr)
            rc = 1
        finally:
            shutil.rmtree(self.work, ignore_errors=True)
            self.doc["runs"][-1]["finished_at"] = now_iso()
            self.doc["runs"][-1]["free_gib_end"] = round(free_gib(), 2)
            self.save()
        print(f"breadth: {self.path}")
        return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=REPO / ".realdata" / "repro" / "breadth")
    ap.add_argument("--dates", default=",".join(DATES))
    ap.add_argument("--tiers", default=",".join(TIERS))
    ap.add_argument("--populations", default=",".join(POPULATIONS))
    ap.add_argument("--modes", default="faithful,corrected")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--min-free-gib", type=float, default=2.5)
    ap.add_argument("--keep-subsets", action="store_true")
    ap.add_argument("--resume", action="store_true", help="keep finished tiers of an existing breadth.json")
    a = ap.parse_args(argv)
    a.out = a.out.resolve()
    a.dates = [x for x in a.dates.split(",") if x]
    a.tiers = [x for x in a.tiers.split(",") if x]
    a.populations = [x for x in a.populations.split(",") if x]
    a.modes = [x for x in a.modes.split(",") if x]
    return Breadth(a).main()


if __name__ == "__main__":
    raise SystemExit(main())
