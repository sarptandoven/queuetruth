"""Modal entry points of the W1 scale run (written for the P1 pilot; nothing here runs without `--confirm-spend`).

    modal run scripts/scale/modal_app.py --date 01302020                      # dry run: plan + cost estimate only
    modal run scripts/scale/modal_app.py --date 01302020 --confirm-spend      # the one-day pilot
    modal run scripts/scale/modal_app.py::verify --run-id <id> --unit-id <unit>  # re-run one unit, check its leaf

Layout of the `queuetruth-w1` Volume (`/vol`):
    split/<date>/            itch_split.py buckets + split_manifest.json + <date>.directory.json
    plans/<run_id>.json      plan.py output
    shards/<run_id>/<unit>/  unit.py outputs + shard_manifest.json
    atlas/<run_id>/          aggregate.py outputs (atlas_manifest.json with the Merkle root)

Stages: `split_day` streams the public EMI file once (the raw day never touches disk; 4 to 16 MB/s from Nasdaq,
so 6 to 25 min for a 2019-2020 day) and writes the K buckets; `make_plan` bin-packs the day's symbols into units;
`run_unit` runs one unit (idempotent: a shard whose manifest already holds the same unit spec hash is skipped, so a
preempted or retried call only redoes missing units); `aggregate` verifies every shard and writes the atlas.
Functions are preemptible (Modal's default) with retries; nothing uses GitHub Actions.

Image: the Rust toolchain image pinned by digest in deploy/docker/Dockerfile, Python 3.13, the hash-pinned
dependencies of `uv.lock` (`uv export --frozen`), and a queuetruth wheel built from this checkout (the released
0.1.0 wheel lacks `allocation: per_partition`). The local tree is copied without target/, .venv/, .realdata/,
.scratch/ and .git/; `QT_GIT_HEAD` records the commit. Gate before trusting cloud numbers (SPEC 7.2): run
`scripts/repro/reproduce.py` in the image and compare with docs/RESULTS.md 4 (floats may differ by up to 4 ulp off
macOS arm64).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import modal

REPO = Path(__file__).resolve().parents[2]
VOL = "/vol"
SRC = "/src"
RUST_IMAGE = "rust:1.97-slim-bookworm@sha256:2775a09d208ff0d7c1f50490c45b62db929e87ba1dcbc3f2132ac71a704bcdd3"
UV_VERSION = "0.11.31"
IGNORE = ["target", ".venv", ".realdata", ".scratch", ".git", "**/__pycache__", "**/*.so", "dist", ".hypothesis"]


def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


app = modal.App("queuetruth-w1")
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
)
SCALE = f"{SRC}/scripts/scale"


def _scale_path() -> None:
    if SCALE not in sys.path:
        sys.path.insert(0, SCALE)


@app.function(image=image, volumes={VOL: volume}, cpu=2.0, memory=4096, timeout=3 * 3600, retries=1)
def split_day(date: str, buckets: int = 32, level: int = 1) -> dict:
    """One stream of the public day into `buckets` symbol buckets on the Volume."""
    _scale_path()
    import itch_split

    out = Path(VOL) / "split"
    rc = itch_split.main(
        ["--date", date, "--buckets", str(buckets), "--level", str(level), "--out", str(out), "--min-free-gib", "0"]
    )
    volume.commit()
    m = json.loads((out / date / "split_manifest.json").read_text())
    return {"rc": rc, "ok": m["ok"], "timings": m["timings"], "messages": m["stream"]["messages"]}


@app.function(image=image, volumes={VOL: volume}, cpu=1.0, memory=2048, timeout=1800)
def make_plan(dates: list[str], quota_rule: str = "scale", unit_core_s: float = 180.0) -> dict:
    _scale_path()
    import plan as P

    volume.reload()
    tmp = Path("/tmp/plan.json")
    args = [x for d in dates for x in ("--split", f"{VOL}/split/{d}")]
    P.main([*args, "--out", str(tmp), "--quota-rule", quota_rule, "--unit-core-s", str(unit_core_s)])
    plan = json.loads(tmp.read_text())
    dest = Path(VOL) / "plans" / f"{plan['run_id']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(tmp.read_text())
    volume.commit()
    return {"run_id": plan["run_id"], "estimate": plan["estimate"], "n_units": len(plan["units"])}


@app.function(image=image, volumes={VOL: volume}, cpu=2.0, memory=4096, timeout=3600, retries=2)
def run_unit(unit: dict) -> dict:
    """One work unit -> /vol/shards/<run_id>/<unit_id>; skipped when an identical shard is already there."""
    _scale_path()
    import common as K
    from unit import run_unit as run

    volume.reload()
    out = Path(VOL) / "shards" / unit["run_id"] / unit["unit_id"]
    mp = out / "shard_manifest.json"
    if mp.exists():
        m = json.loads(mp.read_text())
        if m["leaf"]["unit_spec_sha256"] == K.sha256_hex(K.canonical_json(unit)):
            return {"unit_id": unit["unit_id"], "skipped": True, "telemetry": m["telemetry"]}
    m = run(unit, Path(VOL) / "split" / unit["date"], out, threads=2, work=Path("/tmp"))
    volume.commit()
    return {"unit_id": unit["unit_id"], "skipped": False, "telemetry": m["telemetry"]}


@app.function(image=image, volumes={VOL: volume}, cpu=8.0, memory=32768, timeout=3 * 3600)
def aggregate(run_id: str) -> dict:
    _scale_path()
    import aggregate as A

    volume.reload()
    out = Path(VOL) / "atlas" / run_id
    rc = A.main(["--plan", f"{VOL}/plans/{run_id}.json", "--shards", f"{VOL}/shards/{run_id}", "--out", str(out)])
    volume.commit()
    m = json.loads((out / "atlas_manifest.json").read_text())
    return {"rc": rc, "merkle_root": m["merkle_root"], "units_valid": m["units_valid"], "problems": m["problems"]}


@app.function(image=image, volumes={VOL: volume}, cpu=2.0, memory=4096, timeout=3600)
def verify(run_id: str, unit_id: str) -> int:
    """Re-run one unit in a fresh container and check its leaf against the run's Merkle root."""
    _scale_path()
    import verify_unit

    volume.reload()
    plan = f"{VOL}/plans/{run_id}.json"
    unit = next(u for u in json.loads(Path(plan).read_text())["units"] if u["unit_id"] == unit_id)
    return verify_unit.main(
        [
            "--plan",
            plan,
            "--atlas",
            f"{VOL}/atlas/{run_id}/atlas_manifest.json",
            "--unit-id",
            unit_id,
            "--split",
            f"{VOL}/split/{unit['date']}",
            "--published-shard",
            f"{VOL}/shards/{run_id}/{unit_id}",
        ]
    )


@app.local_entrypoint()
def main(
    date: str = "01302020",
    buckets: int = 32,
    quota_rule: str = "scale",
    unit_core_s: float = 180.0,
    max_units: int = 0,
    confirm_spend: bool = False,
) -> None:
    """Split one day, plan, run every unit (or the first `max_units`), aggregate. Without `--confirm-spend` it only
    prints what it would do (the split is the first thing that costs anything, so nothing runs)."""
    if not confirm_spend:
        print(
            f"dry run: would split {date} into {buckets} buckets, plan with quota rule {quota_rule!r}, run the units "
            "and aggregate on Modal. Re-run with --confirm-spend after approving the estimate "
            "(docs/scale.md, 'Pilot cost')."
        )
        return
    s = split_day.remote(date, buckets)
    print("split:", json.dumps(s))
    if not s["ok"]:
        raise SystemExit("split checks failed; see split_manifest.json on the Volume")
    p = make_plan.remote([date], quota_rule, unit_core_s)
    print("plan:", json.dumps(p))
    volume.reload()
    plan = json.loads(b"".join(volume.read_file(f"plans/{p['run_id']}.json")))
    units = plan["units"][: max_units or None]
    done = 0
    cpu = 0.0
    for r in run_unit.map(units, return_exceptions=True):
        if isinstance(r, Exception):
            print("unit failed:", r)
            continue
        done += 1
        cpu += r["telemetry"]["cpu_s"]
    print(f"units: {done}/{len(units)} ok, {cpu / 3600:.2f} core-h measured")
    print("aggregate:", json.dumps(aggregate.remote(p["run_id"])))
