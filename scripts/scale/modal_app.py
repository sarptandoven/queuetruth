"""Modal entry points of the W1 atlas runs (the pilot and the multi-day run). Nothing spends without `--confirm-spend`.

    modal run scripts/scale/modal_app.py::check_image                                  # build the image, smoke test
    modal run scripts/scale/modal_app.py::days --dates 01302020                       # dry run: what would run
    modal run scripts/scale/modal_app.py::days --dates 01302020 --gate --confirm-spend    # the one-day pilot
    modal run scripts/scale/modal_app.py::days --dates all --confirm-spend --budget-usd 45
    modal run scripts/scale/modal_app.py::combine_days --name atlas-v1 --run-ids id1,id2,... [--flag-policy label]

Layout of the `queuetruth-w1` Volume (`/vol`):
    split/<date>/              itch_split.py buckets (deleted after the day's units ran) + split_manifest.json
    plans/<run_id>.json        plan.py output (one plan per day; the breadth gate plan separately)
    shards/<run_id>/<unit>/    unit.py outputs + shard_manifest.json
    days/<run_id>/             atlas.py reduce-day outputs (day_manifest.json with the day's Merkle root)
    atlas/<name>/              atlas.py combine outputs (atlas_manifest.json with the root over the day roots)

Per day: `split_day` streams the public EMI file once (raw day never on disk) into K buckets; `make_plan` plans the
day (`scale` quotas, day-granularity statistics) and, with `--gate`, also the breadth equivalence plan (36 symbols,
one unit each, 30-minute blocks) whose shards are compared locally with breadth.json (`prove_breadth.py
--compare-only`); `run_unit` runs every unit (idempotent: a shard holding the same unit spec hash is skipped, so a
preempted or retried call redoes only missing units); `reduce_day` verifies the day's shards and writes its tables;
the buckets are then deleted. Spend is checked between days with `modal billing report` (this app's rows only; the
report lags up to an hour) and the CPU and memory reserved by the finished units, and the run stops before a day
that would start past `--budget-usd`.

Image: the Rust toolchain image pinned by digest in deploy/docker/Dockerfile, Python 3.13, the hash-pinned
dependencies of `uv.lock`, and a queuetruth wheel built from this checkout (one cached layer; `scripts/` is mounted
on top, so a script change does not rebuild the wheel). Containers are preemptible (Modal's default).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import modal

REPO = Path(__file__).resolve().parents[2]
VOL = "/vol"
SRC = "/src"
APP = "queuetruth-w1"
RUST_IMAGE = "rust:1.97-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3"
UV_VERSION = "0.11.31"
IGNORE = [
    "target",
    ".venv",
    ".realdata",
    ".scratch",
    ".git",
    "**/__pycache__",
    "**/*.so",
    "dist",
    ".hypothesis",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "scripts",
    "docs",
    "examples",
    ".github",
]
#: Public Nasdaq EMI TotalView-ITCH 5.0 sample days: MMDDYYYY -> file name (listing fetched 2026-10-23). The two
#: `itch50_05_*` files carry no date in their name; 2026-05-15 and 2026-05-18 are inferred from the upload date.
EMI = "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/"
PUBLIC_DAYS = {
    "01302019": "01302019.NASDAQ_ITCH50.gz",
    "03272019": "03272019.NASDAQ_ITCH50.gz",
    "07302019": "07302019.NASDAQ_ITCH50.gz",
    "08302019": "08302019.NASDAQ_ITCH50.gz",
    "10182019": "S101819-v50.txt.gz",
    "10302019": "10302019.NASDAQ_ITCH50.gz",
    "12302019": "12302019.NASDAQ_ITCH50.gz",
    "01302020": "01302020.NASDAQ_ITCH50.gz",
    "07132021": "S071321-v50.txt.gz",
    "08132021": "S081321-v50.txt.gz",
    "11282025": "S112825-v50.txt.gz",
    "12082025": "S120825-v50.txt.gz",
    "12092025": "S120925-v50.txt.gz",
    "12102025": "S121025-v50.txt.gz",
    "12112025": "S121125-v50.txt.gz",
    "12122025": "S121225-v50.txt.gz",
    "05152026": "itch50_05_15.gz",
    "05182026": "itch50_05_18.gz",
    "06122026": "S061226-v50.txt.gz",
}
PINNED = ("12302019", "01302020")  # checked against itch_subset.PUBLIC_DAYS
UNIT_CPU, UNIT_MEM_GIB = 2.0, 6
USD_CPU_CORE_H, USD_MEM_GIB_H = 0.0473, 0.008  # `modal billing rates`, 2026-10-23


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


app = modal.App(APP)
volume = modal.Volume.from_name("queuetruth-w1", create_if_missing=True)
image = (
    modal.Image.from_registry(RUST_IMAGE, add_python="3.13")
    .apt_install("curl", "ca-certificates")
    .pip_install(f"uv=={UV_VERSION}")
    .add_local_dir(str(REPO), SRC, copy=True, ignore=IGNORE)
    .run_commands(
        f"cd {SRC} && uv export --frozen --no-dev --no-emit-workspace --package queuetruth "
        "--format requirements-txt --output-file /tmp/requirements.txt",
        "pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt",
        f"cd {SRC} && CARGO_TARGET_DIR=/tmp/target uv build --wheel --out-dir /wheels . "
        "&& pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /tmp/target",
    )
    .env({"PYTHONUNBUFFERED": "1", "QT_GIT_HEAD": _git_head()})
    .add_local_dir(str(REPO / "scripts"), f"{SRC}/scripts", ignore=["**/__pycache__"])
)
SCALE = f"{SRC}/scripts/scale"


def _scale_path() -> None:
    if SCALE not in sys.path:
        sys.path.insert(0, SCALE)


@app.function(image=image, cpu=1.0, memory=2048, timeout=900)
def smoke() -> dict:
    """The image works: version, extension sha256, the splitter self-test, a per_partition sample on a fixture."""
    _scale_path()
    import common as K
    import itch_split

    import queuetruth as qt

    rc = itch_split.self_test()
    ds = {"path": f"{SRC}/tests/fixtures/synth_small"}
    s = qt.sample_shadows(
        {"dataset": ds, "out": "/tmp/s.parquet", "n": 0, "allocation": "per_partition", "partition_quotas": {"1": 5}}
    )
    return {"version": qt.__version__, "self_test": rc, "sample_n": s.n, "build": K.build_info()}


@app.function(image=image, volumes={VOL: volume}, cpu=2.0, memory=4096, timeout=3 * 3600, retries=1)
def split_day(date: str, buckets: int = 32) -> dict:
    """One stream of the public day into `buckets` symbol buckets on the Volume (skipped when already split)."""
    _scale_path()
    import itch_split

    out = Path(VOL) / "split"
    mp = out / date / "split_manifest.json"
    volume.reload()
    if mp.exists():
        m = json.loads(mp.read_text())
        if m["ok"] and all((out / date / b["file"]).exists() for b in m["buckets"]):
            return {"date": date, "skipped": True, "ok": True, "timings": m["timings"]}
    t0 = time.time()
    args = ["--buckets", str(buckets), "--out", str(out), "--min-free-gib", "0", "--date", date]
    if date not in PINNED:
        args += ["--url", EMI + PUBLIC_DAYS[date]]
    rc = itch_split.main(args)
    volume.commit()
    m = json.loads(mp.read_text())
    bad = [k for k, c in m["checks"].items() if c["ok"] is False]
    return {
        "date": date,
        "rc": rc,
        "ok": m["ok"],
        "bad": bad,
        "timings": m["timings"],
        "messages": m["stream"]["messages"],
        "wall_s": round(time.time() - t0, 1),
    }


@app.function(image=image, volumes={VOL: volume}, cpu=1.0, memory=4096, timeout=1800)
def make_plan(date: str, gate: bool = False, unit_core_s: float = 300.0) -> list[dict]:
    """The day's scale plan (and with `gate` the breadth equivalence plan); returns run ids and estimates."""
    _scale_path()
    import plan as P

    volume.reload()
    out = []
    variants = [("scale", ["--quota-rule", "scale", "--unit-core-s", str(unit_core_s)])]
    if gate:
        variants.append(("gate", ["--quota-rule", "breadth", "--one-symbol-per-unit"]))
    for kind, extra in variants:
        tmp = Path(f"/tmp/plan-{kind}.json")
        P.main(["--split", f"{VOL}/split/{date}", "--out", str(tmp), *extra])
        plan = json.loads(tmp.read_text())
        dest = Path(VOL) / "plans" / f"{plan['run_id']}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(tmp.read_text())
        out.append({"kind": kind, "date": date, "run_id": plan["run_id"], "estimate": plan["estimate"]})
    volume.commit()
    return out


@app.function(image=image, volumes={VOL: volume}, cpu=UNIT_CPU, memory=UNIT_MEM_GIB * 1024, timeout=3600, retries=2)
def run_unit(unit: dict) -> dict:
    """One work unit -> /vol/shards/<run_id>/<unit_id>; skipped when an identical shard is already there."""
    _scale_path()
    import common as K
    from unit import run_unit as run

    t0 = time.time()
    volume.reload()
    out = Path(VOL) / "shards" / unit["run_id"] / unit["unit_id"]
    mp = out / "shard_manifest.json"
    if mp.exists():
        m = json.loads(mp.read_text())
        if m["leaf"]["unit_spec_sha256"] == K.sha256_hex(K.canonical_json(unit)):
            return {"unit_id": unit["unit_id"], "skipped": True, "telemetry": m["telemetry"], "container_s": 0.0}
    m = run(unit, Path(VOL) / "split" / unit["date"], out, threads=int(UNIT_CPU), work=Path("/tmp"))
    volume.commit()
    return {
        "unit_id": unit["unit_id"],
        "skipped": False,
        "telemetry": m["telemetry"],
        "container_s": round(time.time() - t0, 1),
    }


@app.function(image=image, volumes={VOL: volume}, cpu=4.0, memory=32768, timeout=3 * 3600)
def reduce_day(run_id: str) -> dict:
    _scale_path()
    import atlas

    volume.reload()
    plan = json.loads((Path(VOL) / "plans" / f"{run_id}.json").read_text())
    r = atlas.reduce_day(plan, Path(VOL) / "shards" / run_id, Path(VOL) / "days" / run_id)
    volume.commit()
    return r


@app.function(image=image, volumes={VOL: volume}, cpu=1.0, memory=1024, timeout=900)
def drop_buckets(date: str) -> int:
    """Deletes a day's bucket files (keeps split_manifest.json and the directory file)."""
    volume.reload()
    n = 0
    for p in (Path(VOL) / "split" / date).glob("*.itch50.gz"):
        p.unlink()
        n += 1
    volume.commit()
    return n


@app.function(image=image, volumes={VOL: volume}, cpu=8.0, memory=65536, timeout=3 * 3600)
def combine(name: str, run_ids: list[str], bootstrap_b: int = 1000, flag_policy: str = "exclude") -> dict:
    """`flag_policy` (`atlas.combine`): `exclude` (default here, for published atlases) leaves out the symbol-days
    that are not exact_ok (replay checks flagged, or not trading in the session); `label` keeps and marks them."""
    _scale_path()
    import atlas

    volume.reload()
    days = [Path(VOL) / "days" / rid for rid in run_ids]
    r = atlas.combine(days, Path(VOL) / "atlas" / name, bootstrap_b, flag_policy=flag_policy)
    volume.commit()
    return r


# ---- local orchestration -----------------------------------------------------------------------------------------


def app_spend(since: str) -> float:
    """This app's metered cost since `since` (UTC date) from `modal billing report` (lags up to an hour)."""
    r = subprocess.run(
        ["modal", "billing", "report", "--start", since, "--resolution", "h", "--json"], capture_output=True, text=True
    )
    try:
        rows = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return float("nan")
    return round(sum(float(x["cost"]) for x in rows if x.get("description") == APP), 4)


def _units_usd(results: list[dict]) -> float:
    s = sum(r.get("container_s", 0.0) for r in results if isinstance(r, dict))
    return s / 3600 * (UNIT_CPU * USD_CPU_CORE_H + UNIT_MEM_GIB * USD_MEM_GIB_H)


def _log(path: Path, doc: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **doc}, default=str) + "\n")


@app.local_entrypoint()
def days(
    dates: str = "01302020",
    gate: bool = False,
    buckets: int = 32,
    unit_core_s: float = 300.0,
    budget_usd: float = 45.0,
    spent_before_usd: float = 0.0,
    since: str = "2026-10-23",
    confirm_spend: bool = False,
) -> None:
    """Split, plan, run and reduce each day (`all` = every public day). Logs every stage to
    .realdata/scale/cloud/run_log.jsonl and stops before a day that would start past the budget."""
    todo = list(PUBLIC_DAYS) if dates == "all" else [d for d in dates.split(",") if d]
    unknown = [d for d in todo if d not in PUBLIC_DAYS]
    if unknown:
        raise SystemExit(f"unknown dates {unknown}; known: {sorted(PUBLIC_DAYS)}")
    if not confirm_spend:
        print(
            f"dry run: would split, plan, run and reduce {todo} (gate={gate}) on Modal within ${budget_usd}; "
            "re-run with --confirm-spend"
        )
        return
    logp = REPO / ".realdata" / "scale" / "cloud" / "run_log.jsonl"
    logp.parent.mkdir(parents=True, exist_ok=True)
    est = {"units_usd": 0.0}

    def spent() -> float:
        billed = app_spend(since)
        return max(billed if billed == billed else 0.0, spent_before_usd + est["units_usd"])

    t0 = time.time()
    splits = list(split_day.starmap([(d, buckets) for d in todo], return_exceptions=True))
    for d, s in zip(todo, splits, strict=True):
        _log(logp, {"stage": "split", "date": d, "result": str(s) if isinstance(s, Exception) else s})
        print("split", d, s if isinstance(s, Exception) else {k: s.get(k) for k in ("ok", "bad", "wall_s", "skipped")})
    ready = [d for d, s in zip(todo, splits, strict=True) if isinstance(s, dict) and s.get("ok")]
    for d in ready:
        if spent() >= budget_usd:
            print(f"budget: ${spent():.2f} spent of ${budget_usd}; stopping before {d}")
            _log(logp, {"stage": "stop", "date": d, "spent": spent()})
            break
        plans = make_plan.remote(d, gate, unit_core_s)
        _log(logp, {"stage": "plan", "date": d, "plans": plans})
        for p in plans:
            volume.reload()
            plan = json.loads(b"".join(volume.read_file(f"plans/{p['run_id']}.json")))
            units = sorted(plan["units"], key=lambda u: -u["predicted_core_s"])  # long units start first
            t1 = time.time()
            res = list(run_unit.map(units, return_exceptions=True))
            failed = [str(r)[:300] for r in res if isinstance(r, Exception)]
            ok = [r for r in res if isinstance(r, dict)]
            est["units_usd"] += _units_usd(ok)
            cpu = sum(r["telemetry"]["cpu_s"] for r in ok)
            doc = {
                "stage": "units",
                "date": d,
                "kind": p["kind"],
                "run_id": p["run_id"],
                "units": len(units),
                "ok": len(ok),
                "failed": failed[:20],
                "n_failed": len(failed),
                "cpu_core_h": round(cpu / 3600, 3),
                "container_h": round(sum(r.get("container_s", 0) for r in ok) / 3600, 3),
                "wall_s": round(time.time() - t1, 1),
                "units_usd_est": round(_units_usd(ok), 3),
            }
            _log(logp, doc)
            print(json.dumps(doc))
            if p["kind"] == "scale":
                r = reduce_day.remote(p["run_id"])
                _log(logp, {"stage": "reduce", "date": d, "run_id": p["run_id"], "result": r})
                print("reduce", d, r)
        n = drop_buckets.remote(d)
        _log(
            logp,
            {
                "stage": "drop_buckets",
                "date": d,
                "files": n,
                "spent_billed": app_spend(since),
                "spent_est": round(spent(), 2),
                "elapsed_s": round(time.time() - t0, 1),
            },
        )
    print(f"done in {time.time() - t0:.0f} s; billed so far ${app_spend(since):.2f} (lags up to an hour)")


@app.local_entrypoint()
def combine_days(name: str, run_ids: str, bootstrap_b: int = 1000, flag_policy: str = "exclude") -> None:
    r = combine.remote(name, [r for r in run_ids.split(",") if r], bootstrap_b, flag_policy)
    print(json.dumps({**r, "flag_policy": flag_policy}))


@app.local_entrypoint()
def check_image() -> None:
    print(json.dumps(smoke.remote(), indent=1, default=str))
