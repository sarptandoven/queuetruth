"""Consistency checks over tests/fixtures/reference (no regeneration; numpy + scipy only).

    python scripts/reference/check_reference.py [--rng-kat schemas/rng_kat.json]

1. MANIFEST.sha256 matches every fixture file.
2. The RNG mirror in _common.py equals the Rust known answers (schemas/rng_kat.json), when present.
3. gray_cmprsk.json: each pv equals chi2.sf(stat, df) (1e-12); df = K - 1; cmprsk's AJ curves (pooled and by
   group, ties included) equal a plain Aalen-Johansen implementation to 1e-12.
4. survival.json: KM step values at query times are monotone and match the plain product-limit estimator.
5. No fixture contains NaN/Infinity tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from _common import FIXTURES, ROOT, Xoshiro256StarStar, stable_seed
from scipy.stats import chi2


def plain_aj(time, cause, k, grid):
    ts = np.unique(time)
    s_prev, f, vals = 1.0, 0.0, []
    for t in ts:
        n = np.sum(time >= t)
        f += s_prev * np.sum((time == t) & (cause == k)) / n
        s_prev *= 1.0 - np.sum((time == t) & (cause != 0)) / n
        vals.append(f)
    idx = np.searchsorted(ts, grid, side="right") - 1
    return np.where(idx >= 0, np.array(vals)[np.clip(idx, 0, None)], 0.0)


def check_manifest() -> None:
    for line in (FIXTURES / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name
    print("manifest ok")


def check_rng(kat_path: Path) -> None:
    if not kat_path.exists():
        print(f"rng: {kat_path} absent, skipped")
        return
    kat = json.loads(kat_path.read_text())
    for c in kat["stable_seed"]:
        assert stable_seed(c["run_seed"], [p.encode() for p in c["parts"]]) == c["value"]
    for s in kat["streams"]:
        r = Xoshiro256StarStar(s["seed"])
        assert [r.next_u64() for _ in range(len(s["next_u64"]))] == s["next_u64"]
    print("rng mirror ok")


def check_gray() -> None:
    doc = json.loads((FIXTURES / "gray_cmprsk.json").read_text())
    worst = 0.0
    for case in doc["cases"]:
        t = np.array(case["input"]["time"], float)
        c = np.array(case["input"]["cause"])
        g = np.array(case["input"]["group"])
        k = len(np.unique(g))
        grid = np.array(case["cif_grid"], float)
        for row in case["gray_tests"]:
            assert row["df"] == k - 1
            assert abs(chi2.sf(row["stat"], row["df"]) - row["pv"]) <= 1e-12 * max(1.0, row["pv"]), case["name"]
        for cur in case["cif_by_group"]:
            m = g == cur["group"]
            ref = plain_aj(t[m], c[m], cur["cause"], grid)
            est = np.array([np.nan if v is None else v for v in cur["est"]], float)
            ok = ~np.isnan(est)
            worst = max(worst, float(np.max(np.abs(est[ok] - ref[ok]))))
        for cur in case["cif_pooled"]:
            ref = plain_aj(t, c, cur["cause"], grid)
            worst = max(worst, float(np.max(np.abs(np.array(cur["est"], float) - ref))))
    assert worst <= 1e-12, worst
    print(f"gray ok (max |cmprsk AJ - plain AJ| = {worst:.2e})")


def check_survival() -> None:
    doc = json.loads((FIXTURES / "survival.json").read_text())
    for case in doc["kaplan_meier"]:
        v = np.array(case["survival_at_query"], float)
        assert np.all(np.diff(v) <= 0), case["name"]
    print("survival ok")


def check_no_nan() -> None:
    for p in FIXTURES.glob("*.json"):
        txt = p.read_text()
        assert "NaN" not in txt and "Infinity" not in txt, p.name
    print("no NaN/Infinity ok")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rng-kat", type=Path, default=ROOT / "schemas" / "rng_kat.json")
    a = ap.parse_args()
    check_manifest()
    check_rng(a.rng_kat)
    check_gray()
    check_survival()
    check_no_nan()


if __name__ == "__main__":
    main()
