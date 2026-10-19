#!/usr/bin/env python3
"""Public reproduction of queuetruth's real-data numbers from the raw Nasdaq ITCH files alone.

    uv run --frozen --no-sync python scripts/repro/reproduce.py                 # everything, both days
    uv run --frozen --no-sync python scripts/repro/reproduce.py --steps subset,ingest,replay

Needs only: a built queuetruth (`make develop`), curl, network access to emi.nasdaq.com (unless the subsets are
already on disk) and about 1.5 GiB of free disk while it runs. No artifact of the earlier prototype (et) is read.

Steps, per public day (2019-12-30 and 2020-01-30), each recorded in `<out>/results.json`:

- `subset`: reuse `<date>.subset.<SYMS>.itch50.gz` from `--itch-dirs` when its framed content (the ITCH bytes inside
  the gzip) hashes to the value pinned in `PINNED_SUBSETS`; otherwise stream the public file once with
  `scripts/realdata/itch_subset.py` (the raw day is never stored) and verify the pinned sha256 of the compressed file
  and of the subset content.
- `ingest`: `qt.ingest(format="itch")` (queuetruth's own ITCH 5.0 reader); records, partitions, ts ranges and source
  prints against the committed `scripts/realdata/expected.json`; hidden (P) and cross (Q) prints against the raw-stream
  counts of the subset manifest.
- `replay`: `qt.replay` with the rank oracle and trade parity (the RD-2 / RD-6 counters: 8 invariants, FIFO rank
  histograms, rank-0 shares, sweeps, trade parity) against `expected.json`.
- `headline`: `scripts/realdata/headline.py`'s `run_day` (the published RD-8-extended leaderboard): 3,000 never-cancel
  shadows from `qt.sample_shadows` (seed 0), 17 zoo models in faithful and corrected mode, bias at 1 s / 10 s / 60 s
  overall, by queue-ahead decile and by liquidity bucket.
- `calib`: `validate_et.rd_calibrate` (RD-9) on the two-day dataset: 4 families fitted on day one blocks 0-3 of 5,
  evaluated on day two.
- `hazard`: `validate_et.rd_learned` (RD-10): 40,000 shadows, features, L2-only and privileged hazard models, Brier
  skill vs climatology on day two.
- `oracle`: `qt.mirror_real_orders` (3,000 shadows behind sampled real orders, seed 0) and
  `scripts/oracle/run_oracle.py --run-qt --cross-window-s 0` (the independent matching engine with the real ITCH
  cross prints) per day.

Outputs (gitignored) in `--out` (default `.realdata/repro`): `results.json` (every number, the sha256 of every input
and kept output, the queuetruth version, the sha256 of the loaded `_core` extension and a source-tree hash), small
JSON artifacts per step, and the subsets if they had to be streamed. Large intermediates (datasets, replay, zoo,
feature and oracle replay directories) are deleted unless `--keep`. Free space on / is checked before every step
(abort below `--min-free-gib`, default 2.5).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
REALDATA_SCRIPTS = REPO / "scripts" / "realdata"
sys.path.insert(0, str(REALDATA_SCRIPTS))

import headline as H  # noqa: E402  (the published leaderboard driver, unchanged)
import validate_et as V  # noqa: E402  (the published RD-9 / RD-10 drivers, unchanged)
from merge_datasets import merge  # noqa: E402

GIB = 1 << 30
DATES = {"12302019": "2019-12-30", "01302020": "2020-01-30"}
MIDNIGHT = {"2019-12-30": 1577682000000000000, "2020-01-30": 1580360400000000000}
SYMBOLS_DEFAULT = "AAPL,MSFT,SPY,TSLA"
# sha256 of the ITCH frames inside each published subset (independent of the local zlib build), with the gzip file
# sha256 this script's environment produced; from the subset manifests of 2026-10-18.
PINNED_SUBSETS = {
    ("12302019", "AAPL-MSFT-SPY-TSLA"): {
        "framed_sha256": "e85775e21d3a4cc4c3b59a1fb52636f0356856ef744e9cf3ebfdd8221d3185dc",
        "file_sha256": "af1fe00e4303de87dedf6ded8f1085b2a6008a7e4ff9ac76c0a043c41f5933d0",
        "messages": 5425849,
    },
    ("01302020", "AAPL-MSFT-SPY-TSLA"): {
        "framed_sha256": "8707219a75f2d78cc0fa412a6aa283729b624677090d9eff9d48987451fa879a",
        "file_sha256": "5ccd44d992a7dd60ba48059c8143e6d7f2bf4c58c96dc38dac740b47c4e682ca",
        "messages": 9363498,
    },
}
STEPS = ("subset", "ingest", "replay", "headline", "calib", "hazard", "oracle")
INV8 = V.INV8


class DiskGuard(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def free_gib() -> float:
    return shutil.disk_usage("/").free / GIB


def guard(min_free: float, where: str) -> float:
    fg = free_gib()
    if fg < min_free:
        raise DiskGuard(f"{where}: free space {fg:.2f} GiB < {min_free} GiB")
    return fg


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def framed_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with gzip.open(p, "rb") as g:
        for b in iter(lambda: g.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def tree_sha256() -> dict[str, Any]:
    """A build identifier while the tree has no commit: sha256 over (path, sha256) of the Rust and Python sources and
    the lock files, plus the sha256 of the loaded extension."""
    files: list[Path] = []
    for root, pats in (("crates", ("*.rs", "Cargo.toml")), ("python/queuetruth", ("*.py",))):
        for pat in pats:
            files += [p for p in (REPO / root).rglob(pat) if "__pycache__" not in p.parts]
    files += [REPO / "Cargo.lock", REPO / "uv.lock", REPO / "Cargo.toml", REPO / "pyproject.toml"]
    h = hashlib.sha256()
    for p in sorted(set(files)):
        if p.exists():
            h.update(f"{p.relative_to(REPO)}\0{sha256_file(p)}\n".encode())
    return {"source_tree_sha256": h.hexdigest(), "n_files": len(set(files))}


def build_info() -> dict[str, Any]:
    import queuetruth as qt

    so = Path(qt.__file__).parent / "_core.abi3.so"
    info: dict[str, Any] = {
        "queuetruth_version": getattr(qt, "__version__", None),
        "core_extension": {
            "path": str(so.relative_to(REPO)) if so.is_relative_to(REPO) else str(so),
            "sha256": sha256_file(so) if so.exists() else None,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(so.stat().st_mtime)) if so.exists() else None,
        },
        **tree_sha256(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    try:
        info["git_head"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        info["git_head"] = None
    return info


class Run:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.out: Path = a.out
        self.work = self.out / "work"
        self.doc: dict[str, Any] = {
            "schema": "qt.repro.results",
            "schema_version": 1,
            "generator": "scripts/repro/reproduce.py",
            "started_at": now_iso(),
            "args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(a).items()},
            "inputs": {},
            "steps": {},
        }
        self.ctx = V.Ctx(
            et=Path("/nonexistent-et-root"),
            out=self.work,
            exp=json.loads((REALDATA_SCRIPTS / "expected.json").read_text()),
            threads=a.threads,
            keep=a.keep,
        )
        self.subsets: dict[str, tuple[Path, dict]] = {}
        self.prev: dict[str, Any] = {}
        if a.resume and (self.out / "results.json").exists():
            self.prev = json.loads((self.out / "results.json").read_text()).get("steps", {})

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "results.json").write_text(V.jdump(self.doc) + "\n")

    # ---- subset ------------------------------------------------------------------------------------------------

    def find_subset(self, date: str, tag: str) -> tuple[Path, Path] | None:
        for d in self.a.itch_dirs:
            sub, man = d / f"{date}.subset.{tag}.itch50.gz", d / f"{date}.subset.{tag}.manifest.json"
            if sub.exists() and man.exists():
                return sub, man
        return None

    def step_subset(self, date: str) -> V.Check:
        tag = "-".join(self.a.symbols)
        c = V.Check("subset", f"public ITCH subset {date} ({tag})")
        pin = PINNED_SUBSETS.get((date, tag))
        found = self.find_subset(date, tag)
        streamed = False
        if found:
            sub, man_p = found
            fs = framed_sha256(sub)
            if pin and fs != pin["framed_sha256"]:
                c.notes.append(f"existing subset {sub} framed sha256 {fs} != pinned; re-streaming")
                found = None
        if not found:
            itch_out = self.out / "itch"
            cmd = [sys.executable, "-I", str(REALDATA_SCRIPTS / "itch_subset.py"), "--date", date]
            cmd += ["--symbols", ",".join(self.a.symbols), "--out", str(itch_out)]
            cmd += ["--min-free-gib", str(self.a.min_free_gib)]
            t0 = time.perf_counter()
            r = subprocess.run(cmd, capture_output=True, text=True)
            (self.out / f"subset-{date}.log").write_text(r.stdout + "\n--- stderr\n" + r.stderr[-20000:])
            c.eq("itch_subset.py exit code", r.returncode, 0)
            c.record(stream_wall_s=round(time.perf_counter() - t0, 1))
            if r.returncode == 4:
                raise DiskGuard(f"itch_subset.py disk guard for {date}")
            sub, man_p = itch_out / f"{date}.subset.{tag}.itch50.gz", itch_out / f"{date}.subset.{tag}.manifest.json"
            if not sub.exists():
                return c
            fs = framed_sha256(sub)
            streamed = True
        man = json.loads(man_p.read_text())
        file_sha = sha256_file(sub)
        c.eq("subset file sha256 == its manifest", file_sha, man["subset"]["file_sha256"])
        if pin:
            c.eq("subset framed sha256 == pinned", fs, pin["framed_sha256"])
            c.eq("subset messages == pinned", man["subset"]["messages"], pin["messages"])
            c.record(file_sha256_matches_pinned=file_sha == pin["file_sha256"])
        src = man["stream"]
        c.record(
            subset=str(sub.relative_to(REPO)) if sub.is_relative_to(REPO) else str(sub),
            streamed_now=streamed,
            source_url=man["source"]["url"],
            source_bytes=src["compressed_bytes"],
            source_md5=src["compressed_md5"],
            source_sha256=src["compressed_sha256"],
            source_decompressed_sha256=src["decompressed_sha256"],
            source_messages=src["messages"],
            subset_file_sha256=file_sha,
            subset_framed_sha256=fs,
            subset_messages=man["subset"]["messages"],
            manifest_checks_ok=man["ok"],
            manifest_sha256=sha256_file(man_p),
        )
        self.doc["inputs"][date] = {
            "url": man["source"]["url"],
            "sha256": src["compressed_sha256"],
            "bytes": src["compressed_bytes"],
            "subset_framed_sha256": fs,
        }
        self.subsets[date] = (sub, man)
        return c

    # ---- ingest ------------------------------------------------------------------------------------------------

    def step_ingest(self, date: str) -> V.Check:
        import pyarrow.parquet as pq

        import queuetruth as qt
        from queuetruth.schemas import specs as S

        day = DATES[date]
        c = V.Check("ingest", f"qt.ingest(format=itch) {day}")
        sub, sman = self.subsets[date]
        E = self.ctx.exp["days"][day]
        e = E["ingest"]
        ds = self.ctx.fresh("ds", f"nasdaq-{day}")
        spec = S.IngestSpec(
            format="itch",
            inputs=[{"path": str(sub)}],
            venue="nasdaq",
            out=str(ds),
            symbols=list(self.a.symbols),
            price_exp=-9,
            tick_size="0.01",
            lot_size="1",
        )
        t0 = time.perf_counter()
        qt.ingest(spec)
        wall = time.perf_counter() - t0
        self.ctx.ds[day] = ds
        man = json.loads((ds / "manifest.json").read_text())
        dq = json.loads((ds / "data_quality.json").read_text())
        parts = man["partitions"]
        c.eq("input sha256 == subset sha256", [i["sha256"] for i in man["inputs"]], [sman["subset"]["file_sha256"]])
        if tuple(self.a.symbols) == tuple(SYMBOLS_DEFAULT.split(",")):
            c.eq("partitions", len(parts), e["partitions"])
            c.eq("instrument_ids", sorted(p["instrument_id"] for p in parts), e["instrument_ids"])
            c.eq("records", sum(p["rows"] for p in parts), e["records"])
            c.eq("rows_by_instrument", {str(p["instrument_id"]): p["rows"] for p in parts}, e["rows_by_instrument"])
            c.eq(
                "ts_min_by_instrument", {str(p["instrument_id"]): p["ts_min"] for p in parts}, e["ts_min_by_instrument"]
            )
            c.eq(
                "ts_max_by_instrument", {str(p["instrument_id"]): p["ts_max"] for p in parts}, e["ts_max_by_instrument"]
            )
        inst = pq.read_table(ds / "instruments.parquet").to_pylist()
        c.eq("instruments midnight_utc_ns", sorted({r["midnight_utc_ns"] for r in inst}), [MIDNIGHT[day]])
        c.eq("instruments open_s/close_s", sorted({(r["open_s"], r["close_s"]) for r in inst}), [(34200, 57600)])
        kinds: dict[str, dict[str, int]] = {}
        files = sorted((ds / "trades_source").rglob("*.parquet"))
        for f in files:
            t = pq.read_table(f, columns=["instrument_id", "kind"]).to_pylist()
            for r in t:
                d = kinds.setdefault(str(r["instrument_id"]), {})
                d[r["kind"]] = d.get(r["kind"], 0) + 1
        loc2sym = {str(v): k for k, v in sman["selection"]["locates"].items()}
        pr = sman["selection"]["prints_by_symbol"]
        c.eq(
            "hidden prints by instrument == ITCH P messages (raw stream)",
            {k: v.get("hidden", 0) for k, v in sorted(kinds.items())},
            {k: pr[loc2sym[k]]["P"] for k in sorted(kinds)},
        )
        c.eq(
            "cross prints by instrument == ITCH Q messages with shares > 0 (raw stream)",
            {k: v.get("cross", 0) for k, v in sorted(kinds.items())},
            {k: pr[loc2sym[k]]["Q"] - pr[loc2sym[k]]["Q_zero_shares"] for k in sorted(kinds)},
        )
        if tuple(self.a.symbols) == tuple(SYMBOLS_DEFAULT.split(",")):
            c.eq(
                "trades_source rows by instrument",
                {k: sum(v.values()) for k, v in sorted(kinds.items())},
                e["trades_source_rows_by_instrument"],
            )
            c.eq(
                "hidden + cross prints == et side-N prints (expected.json)",
                {k: v.get("hidden", 0) + v.get("cross", 0) for k, v in sorted(kinds.items())},
                e["side_N_trade_prints_by_instrument"],
            )
        c.record(
            wall_s=round(wall, 2),
            records=sum(p["rows"] for p in parts),
            records_per_s=round(sum(p["rows"] for p in parts) / wall),
            dataset_id=man["dataset_id"],
            dataset_mb=V.du_mb(ds),
            verdict=dq.get("verdict"),
            reasons=dq.get("reasons"),
            source_specific={k: v for k, v in (dq.get("source_specific") or {}).items() if v},
            trades_kind_by_instrument=kinds,
            symbols={str(r["instrument_id"]): r["symbol"] for r in inst},
        )
        return c

    # ---- replay ------------------------------------------------------------------------------------------------

    def step_replay(self, date: str) -> V.Check:
        day = DATES[date]
        c = V.Check("replay", f"replay: invariants, rank oracle, trade parity {day}")
        E = self.ctx.exp["days"][day]["replay"]
        out, s = V.replay(
            self.ctx, f"exact-{day}", self.ctx.ds[day], rank_oracle=True, sweep_gap_ns=2_000_000, parity_trades=True
        )
        st, ro, p = s["stats"], s["rank_oracle"], s["parity"]
        default4 = tuple(self.a.symbols) == tuple(SYMBOLS_DEFAULT.split(","))
        if default4:
            for k in ("records", "events", "trades"):
                c.eq(f"stats.{k}", st[k], E[k])
            c.eq("invariants (8, all 0)", {k: s["invariants"][k] for k in INV8}, E["invariants"])
            eo = E["rank_oracle"]
            for k in (
                "executions_checked",
                "price_priority_violations",
                "fifo_rank_histogram",
                "fifo_rank_histogram_sweep_adjusted",
                "sweeps",
                "sweeps_multi_fill",
            ):
                c.eq(f"rank_oracle.{k}", ro[k], eo[k])
            c.eq("rank0_share", ro["rank0_share"], eo["rank0_share"], tol=1e-12)
            c.eq(
                "rank0_share_sweep_adjusted", ro["rank0_share_sweep_adjusted"], eo["rank0_share_sweep_adjusted"], 1e-12
            )
            for k, v in E["parity_trades"].items():
                c.eq(f"parity.{k}", p.get(k), v)
        else:
            c.eq("invariants (8, all 0)", {k: s["invariants"][k] for k in INV8}, dict.fromkeys(INV8, 0))
            c.eq("parity.pass", p.get("pass"), True)
        c.record(
            stats={k: st[k] for k in ("records", "events", "trades", "partitions")},
            invariants={k: s["invariants"][k] for k in INV8},
            rank_oracle={
                k: ro[k]
                for k in (
                    "executions_checked",
                    "price_priority_violations",
                    "rank0_share",
                    "rank0_share_sweep_adjusted",
                    "sweeps",
                    "sweeps_multi_fill",
                )
            },
            parity={k: v for k, v in p.items() if not isinstance(v, (list, dict))},
            wall_s=self.ctx.timings[f"replay_exact-{day}"],
        )
        if not self.a.keep:
            shutil.rmtree(out, ignore_errors=True)
        return c

    # ---- headline ----------------------------------------------------------------------------------------------

    def step_headline(self, date: str) -> V.Check:
        day = DATES[date]
        c = V.Check("headline", f"17-model zoo leaderboard, 3,000 sampled never-cancel shadows {day}")
        hout = self.out / "headline"
        res = H.run_day(self.ctx.ds[day], self.work / "headline", day, self.a.n_headline, 0, self.a.threads, False)
        res["rank_60s"] = {m: H.ranking(res, m, "60s") for m in ("faithful", "corrected")}
        hout.mkdir(parents=True, exist_ok=True)
        p = hout / f"headline-{day}.json"
        p.write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
        for mode in ("faithful", "corrected"):
            m = res["modes"][mode]
            c.true(f"{mode}: 17 models with rows", m["n_models"] == 17, m["n_models"])
        c.record(
            artifact=str(p.relative_to(REPO)),
            exact_fill_rate=res["modes"]["faithful"]["exact_fill_rate"],
            n_models={m: res["modes"][m]["n_models"] for m in res["modes"]},
            zoo_wall_s={m: res["modes"][m]["wall_s"] for m in res["modes"]},
            zoo_warnings={m: res["modes"][m]["zoo_warnings"][:20] for m in res["modes"]},
            wall_s=res["wall_s"],
        )
        if c.status == "pass":
            c.status = "recorded"
        return c

    # ---- calibration / hazard (two-day dataset) ----------------------------------------------------------------

    def two_day(self) -> Path:
        if "2d" not in self.ctx.ds:
            out = self.ctx.fresh("ds", "nasdaq-2d")
            merge([self.ctx.ds[DATES[d]] for d in self.a.dates], out)
            self.ctx.ds["2d"] = out
        return self.ctx.ds["2d"]

    def step_calib(self) -> V.Check:
        self.two_day()
        c = V.rd_calibrate(self.ctx, "RD-9")
        keep = self.out / "calib"
        keep.mkdir(parents=True, exist_ok=True)
        for d in (self.work / "calib").iterdir():
            cj = d / "calibration.json"
            if cj.exists():
                shutil.copy2(cj, keep / f"{d.name}.calibration.json")
        return c

    def step_hazard(self) -> V.Check:
        self.two_day()
        c = V.rd_learned(self.ctx, "RD-10")
        keep = self.out / "hazard"
        keep.mkdir(parents=True, exist_ok=True)
        for d in (self.work / "hazard").iterdir():
            for f in d.glob("*.json"):
                if f.stat().st_size < 8 << 20:
                    shutil.copy2(f, keep / f"{d.name}.{f.name}")
        return c

    # ---- oracle ------------------------------------------------------------------------------------------------

    def step_oracle(self, date: str) -> V.Check:
        import queuetruth as qt

        day = DATES[date]
        c = V.Check("oracle", f"independent matching-engine oracle vs queuetruth on mirror shadows {day}")
        sh = self.ctx.dir("shadows", f"mirror-{day}.parquet")
        ms = V.to_dict(
            qt.mirror_real_orders({"dataset": {"path": str(self.ctx.ds[day])}, "out": str(sh), "n": 3000, "seed": 0})
        )
        out = self.ctx.fresh("oracle", day)
        cmd = [sys.executable, str(REPO / "scripts" / "oracle" / "run_oracle.py"), "--dataset", str(self.ctx.ds[day])]
        cmd += ["--shadows", str(sh), "--run-qt", "--cross-window-s", "0", "--out", str(out)]
        t0 = time.perf_counter()
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
        wall = time.perf_counter() - t0
        c.eq("run_oracle.py exit code", r.returncode, 0)
        if r.returncode != 0:
            c.notes.append(r.stderr[-3000:])
            return c
        summ = json.loads((out / "oracle_summary.json").read_text())
        rep = json.loads((out / "discrepancy_report.json").read_text())
        s = rep["summary"]
        keep = self.out / "oracle"
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out / "discrepancy_report.json", keep / f"discrepancy_report-{day}.json")
        shutil.copy2(out / "oracle_summary.json", keep / f"oracle_summary-{day}.json")
        c.eq("n_compared", s["n_compared"], 3000)
        c.record(
            mirror_sample={k: ms.get(k) for k in ("n", "by_instrument")},
            shadows_sha256=sha256_file(sh),
            n_agree=s["n_agree"],
            n_disagree=s["n_disagree"],
            by_category=s["by_category"],
            filled_qty_total=s["filled_qty_total"],
            oracle_end_states=summ["end_states"],
            wall_s=round(wall, 1),
            stdout_tail=r.stdout.strip().splitlines()[-2:],
        )
        if not self.a.keep:
            shutil.rmtree(out / "qt_replay", ignore_errors=True)
            (out / "oracle_results.jsonl").unlink(missing_ok=True)
        if c.status == "pass":
            c.status = "recorded"
        return c

    # ---- driver ------------------------------------------------------------------------------------------------

    def run_step(self, key: str, fn) -> None:
        prev = self.prev.get(key)
        if prev and prev.get("status") in ("pass", "recorded") and not key.startswith(("subset/", "ingest/")):
            self.doc["steps"][key] = {**prev, "from_previous_run": True}
            print(f"[{key}] {prev['status']} (previous run, --resume)", file=sys.stderr, flush=True)
            return
        guard(self.a.min_free_gib, key)
        print(f"[{key}] ... (free {free_gib():.2f} GiB)", file=sys.stderr, flush=True)
        t0 = time.perf_counter()
        try:
            d = fn().doc()
        except DiskGuard:
            raise
        except Exception as e:
            d = {"status": "error", "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-4000:]}
        d["wall_s_step"] = round(time.perf_counter() - t0, 2)
        d["free_gib_after"] = round(free_gib(), 2)
        self.doc["steps"][key] = d
        print(f"[{key}] {d['status']} in {d['wall_s_step']} s", file=sys.stderr, flush=True)
        self.save()

    def main(self) -> int:
        steps = self.a.steps
        t_all = time.perf_counter()
        self.doc["build"] = build_info()
        self.save()
        try:
            for date in self.a.dates:
                self.run_step(f"subset/{date}", lambda d=date: self.step_subset(d))
                if date not in self.subsets:
                    raise RuntimeError(f"no subset for {date}")
                if {"ingest", "replay", "headline", "calib", "hazard", "oracle"} & set(steps):
                    self.run_step(f"ingest/{date}", lambda d=date: self.step_ingest(d))
                if "replay" in steps:
                    self.run_step(f"replay/{date}", lambda d=date: self.step_replay(d))
                if "headline" in steps:
                    self.run_step(f"headline/{date}", lambda d=date: self.step_headline(d))
                if "oracle" in steps:
                    self.run_step(f"oracle/{date}", lambda d=date: self.step_oracle(d))
            if "calib" in steps:
                self.run_step("calib", self.step_calib)
            if "hazard" in steps:
                self.run_step("hazard", self.step_hazard)
        except DiskGuard as e:
            self.doc["aborted"] = str(e)
            print(f"ABORT: {e}", file=sys.stderr)
        finally:
            self.doc["build_after"] = build_info()
            self.doc["build_changed_during_run"] = (
                self.doc["build_after"]["core_extension"]["sha256"] != self.doc["build"]["core_extension"]["sha256"]
                or self.doc["build_after"]["source_tree_sha256"] != self.doc["build"]["source_tree_sha256"]
            )
            self.doc["wall_s"] = round(time.perf_counter() - t_all, 1)
            self.doc["peak_rss"] = V.peak_rss_mb()
            self.doc["work_mb_end"] = V.du_mb(self.work) if self.work.exists() else 0
            if not self.a.keep and self.work.exists():
                shutil.rmtree(self.work, ignore_errors=True)
            outputs = {}
            for p in sorted(self.out.rglob("*")):
                if p.is_file() and p.name != "results.json" and "work" not in p.relative_to(self.out).parts:
                    outputs[str(p.relative_to(self.out))] = sha256_file(p)
            self.doc["outputs_sha256"] = outputs
            self.doc["finished_at"] = now_iso()
            self.doc["free_gib_end"] = round(free_gib(), 2)
            self.doc["status_counts"] = V._count(v.get("status") for v in self.doc["steps"].values())
            self.save()
        bad = [k for k, v in self.doc["steps"].items() if v.get("status") in ("fail", "error")]
        print(json.dumps({"statuses": {k: v["status"] for k, v in self.doc["steps"].items()}, "bad": bad}, indent=1))
        print(f"results: {self.out / 'results.json'} ({self.doc['wall_s']} s)")
        return 1 if bad or "aborted" in self.doc else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=REPO / ".realdata" / "repro")
    ap.add_argument("--dates", default=",".join(DATES), help="MMDDYYYY list (default both public days)")
    ap.add_argument("--symbols", default=SYMBOLS_DEFAULT)
    ap.add_argument(
        "--itch-dirs",
        default=f"{REPO / '.realdata' / 'itch'},{REPO / '.realdata' / 'repro' / 'itch'}",
        help="comma list of directories searched for existing subsets (verified against the pinned sha256)",
    )
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma list of {','.join(STEPS)}")
    ap.add_argument("--n-headline", type=int, default=3000)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--min-free-gib", type=float, default=2.5)
    ap.add_argument("--keep", action="store_true", help="keep datasets and run directories under <out>/work")
    ap.add_argument("--resume", action="store_true", help="reuse passed steps of an existing <out>/results.json")
    a = ap.parse_args(argv)
    a.out = a.out.resolve()
    a.dates = [d.strip() for d in a.dates.split(",") if d.strip()]
    a.symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    a.itch_dirs = [Path(x).resolve() for x in a.itch_dirs.split(",") if x.strip()]
    a.steps = [s.strip() for s in a.steps.split(",") if s.strip()]
    if bad := set(a.steps) - set(STEPS):
        ap.error(f"unknown steps {sorted(bad)}")
    if bad := set(a.dates) - set(DATES):
        ap.error(f"only the pinned public days are supported: {sorted(DATES)}")
    if ({"calib", "hazard"} & set(a.steps)) and len(a.dates) != 2:
        ap.error("calib and hazard need both days (train day one, test day two)")
    return Run(a).main()


if __name__ == "__main__":
    raise SystemExit(main())
