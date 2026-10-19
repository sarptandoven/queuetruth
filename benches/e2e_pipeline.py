"""End-to-end CLI pipeline timer (SPEC 7.1 last-but-one row): every stage is a separate CLI process.

Synthetic (--data DATASET): replay -> shadows sample -> zoo all -> calibrate alloc power3 -> features -> fit-hazard
  -> live pair -> score -> report.
Real day (--et-day DIR --midnight-ns NS): ingest etcsv first (RD-1 flags), score uses --sim exact on real orders
  converted by scripts/realdata/real_orders_to_live.py (RD-11 recipe).
Prints one JSON document: {ok, failed_stage, total_s, peak_rss_bytes, scratch_bytes, stages: [{name, wall_s,
peak_rss_bytes, exit}]}. Budget (day one): total <= 900 s, peak RSS <= 2 GiB, scratch <= 3 GiB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qtbench.runner import run_process


def du(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qt", required=True)
    ap.add_argument("--core", required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--data", type=Path, default=None, help="existing dataset (synthetic)")
    ap.add_argument("--et-day", type=Path, default=None, help="et converted day dir (mbo/trades/instruments.csv)")
    ap.add_argument("--midnight-ns", type=int, default=None)
    ap.add_argument("--n-shadows", type=int, default=3000)
    ap.add_argument("--n-features", type=int, default=20000)
    ap.add_argument("--n-live", type=int, default=3000)
    ap.add_argument("--threads", type=int, default=1)
    a = ap.parse_args()
    w = a.work
    w.mkdir(parents=True, exist_ok=True)
    qt, core, py = a.qt, a.core, sys.executable
    repo = Path(__file__).resolve().parents[1]
    j = ["--format", "json", "--threads", str(a.threads)]
    ds = a.data or (w / "ds")
    stages: list[tuple[str, list[str]]] = []
    if a.et_day:
        # API[cli] ingest --format etcsv
        stages.append(
            (
                "ingest",
                [
                    core,
                    "ingest",
                    "--format",
                    "etcsv",
                    "--in",
                    str(a.et_day / "mbo.csv"),
                    "--in",
                    str(a.et_day / "trades.csv"),
                    "--instruments",
                    str(a.et_day / "instruments.csv"),
                    "--venue",
                    "nasdaq",
                    "--midnight-ns",
                    str(a.midnight_ns),
                    "--price-exp",
                    "-9",
                    "--tick-size",
                    "0.01",
                    "--lot-size",
                    "1",
                    "--out",
                    str(ds),
                    *j,
                ],
            )
        )
    stages += [
        # API[cli] replay (native), RD-2 flags minus parity (no source trades for synthetic)
        (
            "replay",
            [core, "replay", "--data", str(ds), "--out", str(w / "replay"), "--rank-oracle", "--bbo", "change", *j],
        ),
        # API[cli] shadows sample (native)
        (
            "shadows",
            [
                core,
                "shadows",
                "sample",
                "--data",
                str(ds),
                "--n",
                str(a.n_shadows),
                "--out",
                str(w / "shadows.parquet"),
                *j,
            ],
        ),
        (
            "shadows_feat",
            [
                core,
                "shadows",
                "sample",
                "--data",
                str(ds),
                "--n",
                str(a.n_features),
                "--seed",
                "1",
                "--out",
                str(w / "shadows_feat.parquet"),
                *j,
            ],
        ),
        # API[cli] zoo (hybrid), all 17 models
        (
            "zoo",
            [
                qt,
                "zoo",
                "--data",
                str(ds),
                "--shadows",
                str(w / "shadows.parquet"),
                "--models",
                "all",
                "--out",
                str(w / "zoo"),
                *j,
            ],
        ),
        # API[cli] calibrate alloc (hybrid)
        (
            "calibrate",
            [
                qt,
                "calibrate",
                "alloc",
                "--family",
                "power3",
                "--data",
                str(ds),
                "--shadows",
                str(w / "shadows.parquet"),
                "--train",
                "blocks:0-3/5",
                "--test",
                "blocks:4/5",
                "--out",
                str(w / "calib"),
                *j,
            ],
        ),
        # API[cli] features (native)
        (
            "features",
            [
                qt,
                "features",
                "--data",
                str(ds),
                "--shadows",
                str(w / "shadows_feat.parquet"),
                "--out",
                str(w / "features"),
                *j,
            ],
        ),
        # API[cli] fit-hazard (python)
        ("fit_hazard", [qt, "fit-hazard", "--features", str(w / "features"), "--out", str(w / "hazard"), *j]),
    ]
    if a.et_day:
        stages += [
            (
                "live",
                [
                    py,
                    str(repo / "scripts/realdata/real_orders_to_live.py"),
                    "--data",
                    str(ds),
                    "--n",
                    str(a.n_live),
                    "--seed",
                    "0",
                    "--out",
                    str(w / "live"),
                ],
            ),
            (
                "score",
                [
                    qt,
                    "score",
                    "--live",
                    str(w / "live"),
                    "--sim",
                    "exact",
                    "--data",
                    str(ds),
                    "--out",
                    str(w / "score"),
                    *j,
                ],
            ),
        ]
    else:
        stages += [
            (
                "live",
                [
                    py,
                    str(repo / "benches/make_live_pair.py"),
                    "--n",
                    str(a.n_live),
                    "--live",
                    str(w / "live"),
                    "--sim",
                    str(w / "sim"),
                ],
            ),
            (
                "score",
                [qt, "score", "--live", str(w / "live"), "--sim-log", str(w / "sim"), "--out", str(w / "score"), *j],
            ),
        ]
    # API[cli] report (python)
    stages.append(
        (
            "report",
            [
                qt,
                "report",
                "--in",
                str(w / "replay"),
                "--in",
                str(w / "zoo"),
                "--in",
                str(w / "calib"),
                "--in",
                str(w / "hazard"),
                "--in",
                str(w / "score"),
                "--out",
                str(w / "report.html"),
                *j,
            ],
        )
    )
    out = {"ok": True, "failed_stage": None, "stages": []}
    for name, argv in stages:
        pr = run_process(argv, w, {"OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "1")}, 3600)
        out["stages"].append(
            {"name": name, "wall_s": pr.wall_s, "peak_rss_bytes": pr.maxrss_bytes, "exit": pr.returncode}
        )
        if pr.returncode != 0:
            out.update(ok=False, failed_stage=name, stderr_tail=pr.stderr[-1500:])
            break
    out["total_s"] = sum(s["wall_s"] for s in out["stages"])
    out["peak_rss_bytes"] = max((s["peak_rss_bytes"] for s in out["stages"]), default=0)
    out["scratch_bytes"] = du(w)
    print(json.dumps(out))
    if not out["ok"]:
        print(f"stage {out['failed_stage']} failed:\n{out['stderr_tail']}", file=sys.stderr)
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
