#!/usr/bin/env python3
"""One full-market day of the W1 atlas on this machine: split, plan, every unit, reduce, single-day combine.

    uv run --frozen --no-sync python scripts/scale/local_day.py --date 01302020 --workers 4 --threads 2
    uv run --frozen --no-sync python scripts/scale/local_day.py --source iex --date 2026-10-09 \
        --input 20261009_IEXTP1_DPLS1.0.pcap.gz --site                    # an IEX DEEP+ day, with the site

The same stages as `modal_app.py::days`, with a local process pool instead of Modal containers, so a day can be measured
for free: wall time per stage, the units' CPU core-hours (process CPU time, all threads), peak disk used under `--out`
and the lowest free space seen, failed units, and the day's cost at Modal's prices (units at 2 cores and 6 GiB per
container, as `modal_app.py` reserves them, from the measured unit wall times; plus the split and reduce stages at their
reserved resources). `--workers` is lowered to what the available memory holds at `--unit-mem-gib` (6 GiB, as
`modal_app.py` reserves) per unit: a worker killed out of memory stops the run, and a rerun keeps the finished units.
Bucket files are deleted after the units unless `--keep-buckets`. Stops (and deletes the buckets) when free space falls
below `--min-free-gib`. The single-day atlas uses `--flag-policy exclude` unless told otherwise (symbol-days whose
replay checks flagged or that were not trading in the session are left out; the combine stage of `local_day.json`
records the policy). Writes `<out>/local_day.json`.

`--source iex` splits an IEX DEEP+ capture with `iex_split.py` (`--input FILE` or `--url URL`; `--date` is the
session date YYYY-MM-DD, the day's directory is its MMDDYYYY form as for ITCH). `--site` builds the Fill-Realism
Atlas site of the day into `<out>/site` (`atlas_site.py`; IEX's attribution on every page for an IEX day).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as K  # noqa: E402

USD_CPU_CORE_H, USD_MEM_GIB_H = 0.0473, 0.008  # `modal billing rates`, 2026-10-23
UNIT_CPU, UNIT_MEM_GIB = 2.0, 6  # modal_app.run_unit
SPLIT_CPU, SPLIT_MEM_GIB = 2.0, 4  # modal_app.split_day
REDUCE_CPU, REDUCE_MEM_GIB = 4.0, 32  # modal_app.reduce_day
PINNED = ("12302019", "01302020")
CPU_UTIL = 0.7  # assumed busy share of a unit container's 2 reserved cores (single-threaded ingest and Python steps)


def _du(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


class DiskWatch(threading.Thread):
    def __init__(self, root: Path, min_free_gib: float):
        super().__init__(daemon=True)
        self.root, self.min_free = root, min_free_gib
        self.peak_bytes = 0
        self.min_free_seen = float("inf")
        self.low = threading.Event()
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            self.peak_bytes = max(self.peak_bytes, _du(self.root))
            fg = K.free_gib()
            self.min_free_seen = min(self.min_free_seen, fg)
            if fg < self.min_free:
                self.low.set()
            self.stop.wait(10)


def avail_mem_gib() -> float | None:
    """Memory this machine can give the units: MemAvailable (else physical memory), capped by a cgroup v2 limit."""
    try:
        with open("/proc/meminfo") as f:
            avail = next(int(ln.split()[1]) * 1024 for ln in f if ln.startswith("MemAvailable:"))
    except (OSError, StopIteration, ValueError):
        try:
            avail = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, OSError, ValueError):
            return None
    try:
        lim = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if lim != "max":
            avail = min(avail, int(lim))
    except (OSError, ValueError):
        pass
    return avail / 2**30


def cap_workers(workers: int, unit_mem_gib: float, avail_gib: float | None) -> int:
    """At most as many workers as units of `unit_mem_gib` fit in `avail_gib` (at least one); unchanged if unknown."""
    if avail_gib is None or unit_mem_gib <= 0:
        return workers
    return max(1, min(workers, int(avail_gib // unit_mem_gib)))


def _unit(args: tuple[dict, str, str, int]) -> dict[str, Any]:
    from unit import run_unit

    unit, split, out, threads = args
    t0 = time.perf_counter()
    try:
        m = run_unit(unit, Path(split), Path(out), threads)
        return {"unit_id": unit["unit_id"], "wall_s": time.perf_counter() - t0, "telemetry": m["telemetry"]}
    except Exception as e:
        return {"unit_id": unit["unit_id"], "wall_s": time.perf_counter() - t0, "error": f"{type(e).__name__}: {e}"}


def usd(core_h: float, mem_gib_h: float) -> float:
    return core_h * USD_CPU_CORE_H + mem_gib_h * USD_MEM_GIB_H


def main(argv: list[str] | None = None) -> int:
    import atlas
    import iex_split
    import itch_split
    import plan as P

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="MMDDYYYY (itch) or YYYY-MM-DD (iex)")
    ap.add_argument("--source", choices=["itch", "iex"], default="itch")
    ap.add_argument("--url", help="for a day not pinned in itch_subset.PUBLIC_DAYS, or the IEX capture to stream")
    ap.add_argument("--input", help="with --source iex: a local IEX DEEP+ capture (pcap or pcapng, gzip or raw)")
    ap.add_argument("--site", action="store_true", help="build the atlas site of the day into <out>/site")
    ap.add_argument("--out", type=Path, default=K.REPO / ".realdata" / "scale" / "local")
    ap.add_argument("--buckets", type=int, default=32)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument(
        "--unit-mem-gib",
        type=float,
        default=UNIT_MEM_GIB,
        help="memory one unit may need (modal_app reserves %(default)s GiB): --workers is lowered so that the workers "
        "fit in the available memory; 0 turns the cap off",
    )
    ap.add_argument("--unit-core-s", type=float, default=300.0)
    ap.add_argument("--min-free-gib", type=float, default=10.0)
    ap.add_argument("--keep-buckets", action="store_true")
    ap.add_argument(
        "--flag-policy",
        choices=["exclude", "label"],
        default="exclude",
        help="atlas.py combine: drop (default) or label the symbol-days that are not exact_ok (replay checks "
        "flagged, or not trading in the session: findings/README.md)",
    )
    a = ap.parse_args(argv)
    if a.source == "iex":
        import datetime as dt

        iso = a.date
        a.date = dt.date.fromisoformat(iso).strftime("%m%d%Y")
    out = a.out / a.date
    out.mkdir(parents=True, exist_ok=True)
    watch = DiskWatch(a.out, a.min_free_gib)
    watch.start()
    rep: dict[str, Any] = {"schema": "qt.scale.local_day", "date": a.date, "build": K.build_info(), "stages": {}}
    t_all = time.perf_counter()
    split_dir = out / "split" / a.date

    def stage(name: str, t0: float, **kw: Any) -> None:
        rep["stages"][name] = {"wall_s": round(time.perf_counter() - t0, 1), **kw}
        print(f"[{name}] {rep['stages'][name]}", file=sys.stderr, flush=True)

    try:
        t0 = time.perf_counter()
        mp = split_dir / "split_manifest.json"
        if a.source == "iex" and not (mp.exists() and json.loads(mp.read_text())["ok"]):
            if bool(a.input) == bool(a.url):
                raise SystemExit("--source iex needs exactly one of --input FILE and --url URL")
            args = ["--date", iso, "--buckets", str(a.buckets), "--out", str(out / "split")]
            args += ["--min-free-gib", str(a.min_free_gib)]
            args += ["--input", a.input] if a.input else ["--url", a.url]
            rc = iex_split.main(args)
            if rc != 0:
                raise SystemExit(f"split failed: exit {rc}")
        elif not (mp.exists() and json.loads(mp.read_text())["ok"]):
            args = [
                "--date",
                a.date,
                "--buckets",
                str(a.buckets),
                "--out",
                str(out / "split"),
                "--min-free-gib",
                str(a.min_free_gib),
            ]
            if a.url or a.date not in PINNED:
                if not a.url:
                    raise SystemExit("--url is needed for a day not pinned in itch_subset.PUBLIC_DAYS")
                args += ["--url", a.url]
            rc = itch_split.main(args)
            if rc != 0:
                raise SystemExit(f"split failed: exit {rc}")
        sm = json.loads(mp.read_text())
        stage(
            "split",
            t0,
            messages=sm["stream"]["messages"],
            scan_cpu_s=sm["timings"].get("scan_cpu_s"),
            bucket_bytes=sum(b["file_bytes"] for b in sm["buckets"]),
            checks_ok=sm["ok"],
        )

        t0 = time.perf_counter()
        plan_path = out / "plan.json"
        P.main(
            [
                "--split",
                str(split_dir),
                "--out",
                str(plan_path),
                "--quota-rule",
                "scale",
                "--unit-core-s",
                str(a.unit_core_s),
            ]
        )
        plan = json.loads(plan_path.read_text())
        stage("plan", t0, run_id=plan["run_id"], units=len(plan["units"]), estimate=plan["estimate"])

        t0 = time.perf_counter()
        shards = out / "shards"
        todo = []
        for u in sorted(plan["units"], key=lambda u: -u["predicted_core_s"]):
            mpath = shards / u["unit_id"] / "shard_manifest.json"
            if mpath.exists():
                m = json.loads(mpath.read_text())
                if m["leaf"]["unit_spec_sha256"] == K.sha256_hex(K.canonical_json(u)):
                    continue
            todo.append((u, str(split_dir), str(shards / u["unit_id"]), a.threads))
        results = []
        mem = avail_mem_gib()
        workers = cap_workers(a.workers, a.unit_mem_gib, mem)
        rep["workers"] = {"asked": a.workers, "used": workers, "avail_mem_gib": None if mem is None else round(mem, 1)}
        if workers < a.workers:
            print(
                f"  {workers} workers, not {a.workers}: {mem:.1f} GiB available, {a.unit_mem_gib} GiB per unit",
                file=sys.stderr,
                flush=True,
            )
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_unit, t) for t in todo]
            for i, f in enumerate(as_completed(futs), 1):
                try:
                    r = f.result()
                except BrokenProcessPool:
                    raise SystemExit(
                        "a unit worker was killed (out of memory?); finished units are kept, so rerun with fewer "
                        "--workers or a larger --unit-mem-gib to go on"
                    ) from None
                results.append(r)
                print(
                    f"  [{i}/{len(todo)}] {r['unit_id']} {r['wall_s']:.0f} s"
                    + (f" ERROR {r['error']}" if "error" in r else ""),
                    file=sys.stderr,
                    flush=True,
                )
                if watch.low.is_set():
                    for g in futs:
                        g.cancel()
                    raise SystemExit(f"free space below {a.min_free_gib} GiB; stopped")
        ok = [r for r in results if "error" not in r]
        failed = [r for r in results if "error" in r]
        cpu_s = sum(r["telemetry"]["cpu_s"] for r in ok)
        unit_wall_s = sum(r["wall_s"] for r in ok)
        stage(
            "units",
            t0,
            ran=len(todo),
            ok=len(ok),
            failed=[{k: r[k] for k in ("unit_id", "error")} for r in failed],
            cpu_core_h=round(cpu_s / 3600, 3),
            unit_wall_h=round(unit_wall_s / 3600, 3),
            max_unit_wall_s=round(max((r["wall_s"] for r in ok), default=0), 1),
            max_peak_rss_mb=max((r["telemetry"]["peak_rss_mb"] for r in ok), default=None),
        )

        t0 = time.perf_counter()
        r = atlas.reduce_day(plan, shards, out / "day")
        stage("reduce", t0, **r)
        t0 = time.perf_counter()
        r = atlas.combine([out / "day"], out / "atlas", flag_policy=a.flag_policy)
        stage("combine", t0, **r)
        if a.site:
            import atlas_site

            t0 = time.perf_counter()
            r = atlas_site.build(out / "atlas", out / "site", source="iex" if a.source == "iex" else "nasdaq")
            stage("site", t0, **r)
    finally:
        if not a.keep_buckets:
            for p in [*split_dir.glob("*.itch50.gz"), *split_dir.glob("*.pcap.gz")]:
                p.unlink()
        watch.stop.set()
        watch.join()
        rep["wall_s"] = round(time.perf_counter() - t_all, 1)
        rep["peak_disk_gib"] = round(watch.peak_bytes / 2**30, 2)
        rep["min_free_gib"] = round(watch.min_free_seen, 2)
        st = rep["stages"]
        if "units" in st:
            unit_h = st["units"]["unit_wall_h"]
            split_h = st.get("split", {}).get("wall_s", 0) / 3600
            reduce_h = (st.get("reduce", {}).get("wall_s", 0) + st.get("combine", {}).get("wall_s", 0)) / 3600
            cpu_h = st["units"]["cpu_core_h"]
            cont_h = cpu_h / (UNIT_CPU * CPU_UTIL)
            rep["modal_usd_from_cpu"] = {
                "units": round(usd(cont_h * UNIT_CPU, cont_h * UNIT_MEM_GIB), 2),
                "note": f"container hours = measured unit CPU core-h / ({UNIT_CPU} cores x {CPU_UTIL} utilisation)",
            }
            rep["modal_usd_at_local_speed"] = {
                "units": round(usd(unit_h * UNIT_CPU, unit_h * UNIT_MEM_GIB), 2),
                "split": round(usd(split_h * SPLIT_CPU, split_h * SPLIT_MEM_GIB), 2),
                "reduce_combine": round(usd(reduce_h * REDUCE_CPU, reduce_h * REDUCE_MEM_GIB), 2),
                "note": "unit wall time x the resources modal_app reserves (2 cores, 6 GiB) at `modal billing rates`; "
                "the laptop ran in Low Power Mode with several units per core pair, so this is an upper-side figure "
                "for cloud cores; image build and retries not included",
            }
            rep["modal_usd_at_local_speed"]["total"] = round(
                sum(v for v in rep["modal_usd_at_local_speed"].values() if isinstance(v, float)), 2
            )
        rep["machine"] = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
        ).stdout.strip()
        (out / "local_day.json").write_text(json.dumps(rep, indent=1, default=str) + "\n")
        shutil.rmtree(out / "split" / a.date / "__pycache__", ignore_errors=True)
    print(json.dumps({k: rep[k] for k in ("wall_s", "peak_disk_gib", "min_free_gib")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
