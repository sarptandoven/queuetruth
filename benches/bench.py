"""queuetruth benchmark driver (make bench): runs a suite of SPEC 7.1 cases, writes a qtbench.results document.

  python benches/bench.py --suite quick|full|realdata|gate [--out .realdata/bench.json] [--only REGEX]
         [--core target/release/queuetruth-core] [--et-root PATH] [--max-load 3.0] [--list]

Suites
  quick     Rust kernels on 2M generated records, binding overhead on the synth_small fixture (~3 min)
  full      quick + kernels on 10M records, zoo per model and all-17 at 1/4 threads, calibration, features 20k,
            fit-hazard 60k, score 10k, report, end-to-end CLI pipeline on synth_small (~30-45 min)
  realdata  et day one (RD-13): etcsv ingest, kernels from the day-one dataset, binding overhead, zoo, analysis,
            day-one end-to-end pipeline (<= 15 min budget); outputs under .realdata/bench (never committed)
  gate      full, plus realdata when --et-root exists (what `make gate-3` runs)

Noise control: one warmup process per group (page cache, dyld, allocator), interleaved repetitions within a group,
median + p10/p90 reported, every run records load1 before it; `--max-load` waits up to 30 s for a quiet machine and
flags the round as noisy otherwise. Power source / Low Power Mode / CPU are recorded (env_class) so `check.py`
refuses cross-environment comparisons. The whole run holds a `caffeinate -i` assertion on macOS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import (
    DAY1,
    Ctx,
    Group,
    analysis_group,
    binding_overhead,
    core_kernels,
    e2e_group,
    ingest_etcsv_group,
    ingest_itch_group,
    zoo_group,
)
from qtbench import env as envmod
from qtbench.doc import case_result, derived_ratio, results_doc, write_json
from qtbench.runner import run_group, run_process

EXIT_OK, EXIT_ERRORS, EXIT_USAGE = 0, 1, 2


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def prep(argv: list[str], cwd: Path) -> dict:
    """Untimed preparation step; fails loudly. Returns the parsed JSON summary when stdout is JSON."""
    log("prep: " + " ".join(argv))
    pr = run_process(argv, cwd, None, 7200)
    if pr.returncode != 0:
        raise RuntimeError(f"prep failed ({pr.returncode}): {' '.join(argv)}\n{pr.stderr[-2000:]}")
    try:
        return json.loads(pr.stdout)
    except json.JSONDecodeError:
        return {}


def synth_prep(ctx: Ctx, full: bool) -> dict:
    s, fx, core, qt = ctx.scratch, ctx.fixture, str(ctx.core), str(ctx.qt)
    s.mkdir(parents=True, exist_ok=True)
    info: dict = {}
    # API[cli] shadows sample (native) -> qt.shadow_sample_summary
    for n, name in ((3000, "shadows_3k"), (20000, "shadows_20k")):
        prep(
            [
                core,
                "shadows",
                "sample",
                "--data",
                str(fx),
                "--n",
                str(n),
                "--seed",
                "0",
                "--out",
                str(s / f"{name}.parquet"),
                "--format",
                "json",
            ],
            ctx.repo,
        )
    if not full:
        return info
    # API[cli] features (native) -> input of fit-hazard 60k
    prep(
        [
            qt,
            "features",
            "--data",
            str(fx),
            "--shadows",
            str(s / "shadows_20k.parquet"),
            "--landmarks",
            "0,1s,5s",
            "--out",
            str(s / "features_20k"),
            "--format",
            "json",
        ],
        ctx.repo,
    )
    # API[py] queuetruth.live.synthetic_log via benches/make_live_pair.py -> canonical live dirs (SPEC 3.8)
    prep(
        [
            str(ctx.py),
            str(ctx.repo / "benches/make_live_pair.py"),
            "--n",
            "10000",
            "--seed",
            "0",
            "--live",
            str(s / "live_10k"),
            "--sim",
            str(s / "sim_10k"),
        ],
        ctx.repo,
    )
    # inputs for the report case
    prep(
        [
            qt,
            "zoo",
            "--data",
            str(fx),
            "--shadows",
            str(s / "shadows_3k.parquet"),
            "--models",
            "front_of_queue,hbt_power3[n=2]",
            "--out",
            str(s / "zoo_prep"),
            "--format",
            "json",
        ],
        ctx.repo,
    )
    prep(
        [
            qt,
            "score",
            "--live",
            str(s / "live_10k"),
            "--sim-log",
            str(s / "sim_10k"),
            "--bootstrap-b",
            "200",
            "--out",
            str(s / "score_prep"),
            "--format",
            "json",
        ],
        ctx.repo,
    )
    # PROPOSED: bench itch --write FILE: qt_ingest::itch::encode_file of a generated stream; prints {messages}
    d = prep(
        [
            core,
            "bench",
            "itch",
            "--synth-messages",
            "20000000",
            "--write",
            str(s / "synth.itch.gz"),
            "--format",
            "json",
        ],
        ctx.repo,
    )
    info["itch_messages"] = int(d.get("records", 20_000_000))
    return info


def real_prep(ctx: Ctx) -> dict:
    """Day-one dataset + shadows + features + live log under .realdata/bench (RD-1 ingest flags)."""
    s, core, qt = ctx.scratch, str(ctx.core), str(ctx.qt)
    s.mkdir(parents=True, exist_ok=True)
    d1 = ctx.et_root / DAY1["dir"]
    ds = s / "ds_day1"
    if not (ds / "manifest.json").exists():
        prep(
            [
                core,
                "ingest",
                "--format",
                "etcsv",
                "--in",
                str(d1 / "mbo.csv"),
                "--in",
                str(d1 / "trades.csv"),
                "--instruments",
                str(d1 / "instruments.csv"),
                "--venue",
                "nasdaq",
                "--midnight-ns",
                str(DAY1["midnight"]),
                "--price-exp",
                "-9",
                "--tick-size",
                "0.01",
                "--lot-size",
                "1",
                "--out",
                str(ds),
                "--format",
                "json",
            ],
            ctx.repo,
        )
    for n, name in ((3000, "shadows_3k"), (20000, "shadows_20k"), (100000, "shadows_100k")):
        if not (s / f"{name}.parquet").exists():
            prep(
                [
                    core,
                    "shadows",
                    "sample",
                    "--data",
                    str(ds),
                    "--n",
                    str(n),
                    "--seed",
                    "0",
                    "--out",
                    str(s / f"{name}.parquet"),
                    "--format",
                    "json",
                ],
                ctx.repo,
            )
    if not (s / "features_20k").exists():
        prep(
            [
                qt,
                "features",
                "--data",
                str(ds),
                "--shadows",
                str(s / "shadows_20k.parquet"),
                "--landmarks",
                "0,1s,5s",
                "--out",
                str(s / "features_20k"),
                "--format",
                "json",
            ],
            ctx.repo,
        )
    if not (s / "live_10k").exists():
        # helper script: real orders of day one -> canonical live log (RD-11 recipe)
        prep(
            [
                str(ctx.py),
                str(ctx.repo / "scripts/realdata/real_orders_to_live.py"),
                "--data",
                str(ds),
                "--n",
                "10000",
                "--seed",
                "0",
                "--out",
                str(s / "live_10k"),
            ],
            ctx.repo,
        )
    if not (s / "zoo_prep").exists():
        prep(
            [
                qt,
                "zoo",
                "--data",
                str(ds),
                "--shadows",
                str(s / "shadows_3k.parquet"),
                "--models",
                "front_of_queue,hbt_power3[n=2]",
                "--out",
                str(s / "zoo_prep"),
                "--format",
                "json",
            ],
            ctx.repo,
        )
    if not (s / "score_prep").exists():
        prep(
            [
                qt,
                "score",
                "--live",
                str(s / "live_10k"),
                "--sim",
                "exact",
                "--data",
                str(ds),
                "--bootstrap-b",
                "200",
                "--out",
                str(s / "score_prep"),
                "--format",
                "json",
            ],
            ctx.repo,
        )
    return {"dataset": ds}


def build_groups(suite: str, ctx: Ctx, info: dict) -> list[Group]:
    fx = ctx.fixture
    groups: list[Group] = []
    if suite in ("quick", "full", "gate"):
        groups.append(core_kernels(ctx, None, 2_000_000, "synth2m"))
        groups.append(binding_overhead(ctx, fx, "fixture"))
    if suite in ("full", "gate"):
        groups.append(core_kernels(ctx, None, ctx.synth_records, "synth10m"))
        groups.append(ingest_itch_group(ctx, ctx.scratch / "synth.itch.gz", info.get("itch_messages", 20_000_000)))
        groups += zoo_group(ctx, fx, ctx.scratch / "shadows_3k.parquet", "fixture", per_model=True)
        groups.append(
            analysis_group(
                ctx,
                fx,
                ctx.scratch / "shadows_3k.parquet",
                "fixture",
                "blocks:0-3/5",
                "blocks:4/5",
                ["--sim-log", str(ctx.scratch / "sim_10k")],
            )
        )
        groups.append(e2e_group(ctx, "synth", ["--data", str(fx), "--n-shadows", "3000"], None, repeat=3))
    if suite == "realdata":
        rs = ctx.scratch
        ds = info["dataset"]
        groups.append(ingest_etcsv_group(ctx))
        groups.append(core_kernels(ctx, ds, None, "real_day1"))
        groups.append(binding_overhead(ctx, ds, "real_day1"))
        groups += zoo_group(ctx, ds, rs / "shadows_3k.parquet", "real_day1", per_model=True)
        groups.append(
            analysis_group(
                ctx,
                ds,
                rs / "shadows_3k.parquet",
                "real_day1",
                "blocks:0-3/5",
                "blocks:4/5",
                ["--sim", "exact", "--data", str(ds)],
            )
        )
        groups.append(
            e2e_group(
                ctx,
                "real_day1",
                [
                    "--et-day",
                    str(ctx.et_root / DAY1["dir"]),
                    "--midnight-ns",
                    str(DAY1["midnight"]),
                    "--n-shadows",
                    "3000",
                ],
                {"op": "<=", "value": 900.0, "spec": T71_E2E},
                repeat=1,
            )
        )
    return groups


T71_E2E = "SPEC 7.1 day-one pipeline <= 15 min, peak RSS <= 2 GB, scratch <= 3 GB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", default="quick", choices=["quick", "full", "realdata", "gate"])
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--core", type=Path, default=None)
    ap.add_argument(
        "--et-root",
        type=Path,
        default=os.environ.get("QT_ET_ROOT"),
        help="local checkout of the et prototype for the realdata suite (default: $QT_ET_ROOT; unset = no realdata)",
    )
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--only", default=None, help="regex on case ids")
    ap.add_argument("--max-load", type=float, default=None)
    ap.add_argument("--keep-scratch", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    repo = a.repo.resolve()
    tdir = Path(os.environ.get("CARGO_TARGET_DIR", repo / "target"))
    core = (a.core or tdir / "release/queuetruth-core").resolve()
    ctx = Ctx(
        repo=repo,
        scratch=repo / ".scratch/bench",
        core=core,
        py=repo / ".venv/bin/python",
        qt=repo / ".venv/bin/queuetruth",
        et_root=a.et_root if a.et_root and a.et_root.exists() else None,
    )
    out = a.out or repo / ".realdata/bench.json"

    suites = [a.suite] if a.suite != "gate" else (["full", "realdata"] if ctx.et_root else ["full"])
    if a.list:
        for s in suites:
            c2 = Ctx(**{**ctx.__dict__, "scratch": repo / (".realdata/bench" if s == "realdata" else ".scratch/bench")})
            for g in build_groups(s, c2, {"dataset": c2.scratch / "ds_day1"}):
                for c in g.cases:
                    print(f"{s:9s} {g.name:24s} {c.id}")
        return EXIT_OK
    for p in (core, ctx.py, ctx.qt):
        if not p.exists():
            log(f"missing {p} (make develop; cargo build --release -p queuetruth-cli --bin queuetruth-core)")
            return EXIT_USAGE

    caff = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())]) if shutil.which("caffeinate") else None
    env0 = envmod.capture()
    ver = subprocess.run([str(core), "--version"], capture_output=True, text=True).stdout.strip()
    env0.update(engine_version=ver, binary_sha256={"queuetruth-core": hashlib.sha256(core.read_bytes()).hexdigest()})

    case_docs: list[dict] = []
    derived_docs: list[dict] = []
    only = re.compile(a.only) if a.only else None
    try:
        for s in suites:
            sctx = Ctx(
                **{**ctx.__dict__, "scratch": repo / (".realdata/bench" if s == "realdata" else ".scratch/bench")}
            )
            info = real_prep(sctx) if s == "realdata" else synth_prep(sctx, full=s == "full")
            for g in build_groups(s, sctx, info):
                cases = [c for c in g.cases if not only or only.search(c.id)]
                if not cases:
                    continue
                raw = run_group(cases, sctx.scratch / "runs", warmup=g.warmup, repeat=g.repeat, max_load=a.max_load)
                res = {c.id: case_result(c, raw[c.id]) for c in cases}
                case_docs += list(res.values())
                for did, num, den, kind, target in g.derived:
                    if num in res and den in res and res[num]["samples"]["metric"] and res[den]["samples"]["metric"]:
                        derived_docs.append(derived_ratio(did, res[num], res[den], kind, target))
            if s != "realdata" and not a.keep_scratch:
                shutil.rmtree(sctx.scratch, ignore_errors=True)  # scratch is deleted after use (disk)
    finally:
        if caff:
            caff.terminate()
    doc = results_doc(
        a.suite, env0, {"suites": suites, "only": a.only, "max_load": a.max_load}, case_docs, derived_docs
    )
    write_json(doc, out)
    n_err = sum(1 for c in case_docs if c["status"] in ("error", "partial"))
    for c in case_docs:
        t = c["target"]
        tm = "" if not t else (" target " + ("met" if t["met"] else "MISSED") + f" ({t['op']} {t['value']:g})")
        log(f"{c['id']:52s} {c['status']:8s} median {c['summary']['median']} {c['unit']}{tm}")
    log(f"wrote {out}; {len(case_docs)} cases, {n_err} with errors")
    return EXIT_ERRORS if n_err else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
