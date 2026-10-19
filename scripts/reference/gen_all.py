"""Regenerate every reference fixture in tests/fixtures/reference/, then PROVENANCE.json and MANIFEST.sha256.

    python scripts/reference/gen_all.py            # Python generators + R (gen_gray.R) if cmprsk is installed
    python scripts/reference/gen_all.py --no-r     # keep the committed gray_cmprsk.json

Run from the repo root with the project venv (`uv run --frozen --no-sync python scripts/reference/gen_all.py`).
Output is deterministic: a second run must leave MANIFEST.sha256 unchanged.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from _common import FIXTURES, write_fixture, write_manifest

HERE = Path(__file__).resolve().parent
PY_GENERATORS = [
    ("gen_mcnemar.py", "mcnemar.json", ["O-STAT-1"]),
    ("gen_wilcoxon.py", "wilcoxon.json", ["O-STAT-2"]),
    ("gen_survival.py", "survival.json", ["O-STAT-2"]),
    ("gen_multitest.py", "multitest.json", ["O-STAT-4"]),
    ("gen_tost.py", "tost.json", ["O-STAT-4"]),
    ("gen_bootstrap.py", "bootstrap.json", ["O-STAT-4"]),
    ("gen_et_calibration.py", "et_calibration.json", ["O-STAT-5"]),
    ("gen_scoring.py", "scoring_rules.json", ["O-STAT-5", "O-MOD-5"]),
    ("gen_isotonic.py", "isotonic.json", ["O-MOD-2"]),
    ("gen_markouts.py", "markouts.json", ["O-SCORE-3"]),
]
PY_PACKAGES = ["numpy", "scipy", "statsmodels", "lifelines", "pandas", "scikit-learn"]


def r_versions(rscript: str) -> dict:
    expr = (
        'cat(paste(R.version$major, R.version$minor, sep="."), '
        'as.character(packageVersion("cmprsk")), as.character(packageVersion("survival")), '
        'as.character(packageVersion("jsonlite")), sep="\\n")'
    )
    out = subprocess.run([rscript, "-e", expr], check=True, capture_output=True, text=True).stdout.split()
    return dict(zip(["R", "cmprsk", "survival", "jsonlite"], out, strict=True))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-r", action="store_true", help="do not run gen_gray.R (keep the existing fixture)")
    args = ap.parse_args()
    for gen, _, _ in PY_GENERATORS:
        subprocess.run(
            [sys.executable, str(HERE / gen)], check=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
        )
    rscript = shutil.which("Rscript") or "/opt/homebrew/bin/Rscript"
    r_info = None
    if not args.no_r and Path(rscript).exists():
        subprocess.run([rscript, str(HERE / "gen_gray.R")], check=True)
    if Path(rscript).exists():
        try:
            r_info = r_versions(rscript)
        except subprocess.CalledProcessError:
            r_info = None
    fixtures = {
        f: {"generator": f"scripts/reference/{g}", "oracle": o, "command": f"python scripts/reference/{g}"}
        for g, f, o in PY_GENERATORS
    }
    fixtures["gray_cmprsk.json"] = {
        "generator": "scripts/reference/gen_gray.R",
        "oracle": ["O-STAT-3", "O-STAT-2"],
        "command": "Rscript scripts/reference/gen_gray.R",
    }
    prov = {
        "description": "Software used to generate tests/fixtures/reference (no wall clock, host or user recorded).",
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
        },
        "python_packages": {p: md.version(p) for p in PY_PACKAGES},
        "r": r_info,
        "rng": "SPEC 3.1 splitmix64 / stable_seed / xoshiro256** (Python mirror in scripts/reference/_common.py, "
        "checked against schemas/rng_kat.json); R inputs from a Park-Miller LCG inside gen_gray.R",
        "fixtures": dict(sorted(fixtures.items())),
        "regenerate_all": "python scripts/reference/gen_all.py",
    }
    write_fixture("PROVENANCE.json", prov)
    print(write_manifest())
    print(sorted(p.name for p in FIXTURES.iterdir()))
    json.loads((FIXTURES / "PROVENANCE.json").read_text())


if __name__ == "__main__":
    main()
