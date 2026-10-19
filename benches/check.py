"""Regression gate: compare a `qtbench.results` file with the committed `qtbench.baseline` (make bench-check).

Per case present in both files, on costs (time per unit of work; throughput is inverted):
  slowdown = median(cost_current) / median(cost_baseline) - 1
  regression   slowdown > threshold (default 0.20, SPEC 7.1) AND one-sided Mann-Whitney p(current slower) <= alpha
  suspect      slowdown > threshold but p > alpha (noise or too few repetitions)
  improved     slowdown < -threshold AND p(current faster) <= alpha (re-baseline suggested)
  insufficient fewer than 2 samples in a group, or 1 / C(m+n, m) > alpha (no attainable significance)
  ok           otherwise
A 95% percentile-bootstrap interval of the slowdown (B = 2000, seed 0) is reported next to every comparison.

Exit codes (precedence 2 > 3 > 1 > 0):
  0  no regression
  1  at least one regression (with --strict: also any suspect)
  2  error: unreadable or invalid file, wrong schema, or a current case errored (unless --allow-errors)
  3  inconclusive: environments not comparable (CPU/cores/memory/power source/Low Power Mode differ; override with
     --allow-env-mismatch), or --require-all and a baseline case is missing from the current results

Promote a results file to a baseline (prints nothing, writes --out):
  python benches/check.py --promote .realdata/bench.json --out benches/baseline.json [--threshold 0.2]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qtbench.env import comparable
from qtbench.stats import compare

RESULTS = "qtbench.results"
BASELINE = "qtbench.baseline"
CHECK = "qtbench.check"
EXIT_OK, EXIT_REGRESSION, EXIT_ERROR, EXIT_INCONCLUSIVE = 0, 1, 2, 3
_REQ_TOP = ("schema", "schema_version", "suite", "env", "cases")
_REQ_CASE = ("id", "unit", "direction", "status", "samples")


class CheckError(Exception):
    pass


def load(path: Path, expect: tuple[str, ...]) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError(f"{path}: {exc}") from exc
    for k in _REQ_TOP:
        if k not in doc:
            raise CheckError(f"{path}: missing top-level field {k!r}")
    if doc["schema"] not in expect:
        raise CheckError(f"{path}: schema {doc['schema']!r}, expected one of {expect}")
    if doc["schema_version"] != 1:
        raise CheckError(f"{path}: schema_version {doc['schema_version']} unsupported")
    seen = set()
    for c in doc["cases"]:
        for k in _REQ_CASE:
            if k not in c:
                raise CheckError(f"{path}: case {c.get('id')!r} missing {k!r}")
        if c["id"] in seen:
            raise CheckError(f"{path}: duplicate case id {c['id']!r}")
        if c["direction"] not in ("higher_is_better", "lower_is_better"):
            raise CheckError(f"{path}: case {c['id']!r} bad direction {c['direction']!r}")
        seen.add(c["id"])
    return doc


def promote(results_path: Path, out: Path, threshold: float, alpha: float, note: str | None) -> None:
    doc = load(results_path, (RESULTS,))
    bad = [c["id"] for c in doc["cases"] if c["status"] in ("error",)]
    if bad:
        raise CheckError(f"refusing to promote results with errored cases: {bad}")
    base = dict(doc)
    base["schema"] = BASELINE
    base["promoted_from"] = {
        "path": str(results_path),
        "sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
        "created_utc": doc.get("created_utc"),
        "note": note,
    }
    base["gate"] = {"threshold": threshold, "alpha": alpha, "per_case": {}}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(base, indent=1, allow_nan=False) + "\n")


def run_check(base: dict, cur: dict, args: argparse.Namespace) -> tuple[int, dict]:
    gate = base.get("gate", {})
    threshold = args.threshold if args.threshold is not None else gate.get("threshold", 0.20)
    alpha = args.alpha if args.alpha is not None else gate.get("alpha", 0.05)
    per_case = gate.get("per_case", {})
    env_reasons = comparable(base["env"], cur["env"])
    bcases = {c["id"]: c for c in base["cases"]}
    ccases = {c["id"]: c for c in cur["cases"]}
    rows = []
    for cid in sorted(set(bcases) | set(ccases)):
        b, c = bcases.get(cid), ccases.get(cid)
        row: dict = {"id": cid}
        if b is None:
            row["status"] = "new"
        elif c is None:
            row["status"] = "missing"
        elif c["status"] == "skipped" or b["status"] == "skipped":
            row["status"] = "skipped"
        elif c["status"] == "error":
            row["status"] = "error"
            row["errors"] = c.get("errors", [])[:3]
        elif b["unit"] != c["unit"] or b["direction"] != c["direction"]:
            row["status"] = "error"
            row["errors"] = [f"unit/direction changed: {b['unit']}/{b['direction']} -> {c['unit']}/{c['direction']}"]
        else:
            th = per_case.get(cid, {}).get("threshold", threshold)
            row.update(compare(b["samples"]["metric"], c["samples"]["metric"], c["direction"], th, alpha))
            row["base_median"] = b["summary"]["median"]
            row["cur_median"] = c["summary"]["median"]
            row["unit"] = c["unit"]
        rows.append(row)
    statuses = [r["status"] for r in rows]
    if "error" in statuses and not args.allow_errors:
        code = EXIT_ERROR
    elif env_reasons and not args.allow_env_mismatch:
        code = EXIT_INCONCLUSIVE
    elif args.require_all and "missing" in statuses:
        code = EXIT_INCONCLUSIVE
    elif "regression" in statuses or (args.strict and "suspect" in statuses):
        code = EXIT_REGRESSION
    else:
        code = EXIT_OK
    report = {
        "schema": CHECK,
        "schema_version": 1,
        "threshold": threshold,
        "alpha": alpha,
        "env_comparable": not env_reasons,
        "env_reasons": env_reasons,
        "baseline_env_class": base["env"].get("env_class"),
        "current_env_class": cur["env"].get("env_class"),
        "rows": rows,
        "exit_code": code,
    }
    return code, report


def _fmt(v: float | None, unit: str | None) -> str:
    if v is None:
        return "-"
    if unit and unit.endswith("/s") and v >= 1e5:
        return f"{v / 1e6:.3f}M"
    return f"{v:.4g}"


def print_report(rep: dict, out=sys.stdout) -> None:
    print(f"threshold {rep['threshold']:.0%}  alpha {rep['alpha']}  env comparable: {rep['env_comparable']}", file=out)
    for r in rep["env_reasons"]:
        print(f"  env: {r}", file=out)
    print(
        f"{'case':44s} {'status':12s} {'baseline':>10s} {'current':>10s} {'slowdown':>9s} {'95% CI':>17s} {'p':>7s}",
        file=out,
    )
    for r in rep["rows"]:
        sd = r.get("slowdown")
        ci = r.get("ci")
        ps = r.get("p_slower")
        p_txt = f"{ps:.3f}" if ps is not None else "-"
        print(
            f"{r['id']:44s} {r['status']:12s} {_fmt(r.get('base_median'), r.get('unit')):>10s} "
            f"{_fmt(r.get('cur_median'), r.get('unit')):>10s} "
            f"{(f'{sd:+.1%}' if sd is not None else '-'):>9s} "
            f"{(f'[{ci[0]:+.1%}, {ci[1]:+.1%}]' if ci else '-'):>17s} "
            f"{p_txt:>7s}",
            file=out,
        )
        for e in r.get("errors", []):
            print(f"    {e}", file=out)
    print(f"exit {rep['exit_code']}", file=out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--current", type=Path)
    ap.add_argument("--threshold", type=float, default=None, help="relative slowdown (default from baseline, 0.20)")
    ap.add_argument("--alpha", type=float, default=None, help="one-sided significance (default 0.05)")
    ap.add_argument("--strict", action="store_true", help="suspect counts as regression")
    ap.add_argument("--require-all", action="store_true", help="a baseline case missing from current -> exit 3")
    ap.add_argument("--allow-env-mismatch", action="store_true")
    ap.add_argument("--allow-errors", action="store_true")
    ap.add_argument("--json", type=Path, default=None, help="also write the qtbench.check report here")
    ap.add_argument("--promote", type=Path, default=None, help="results file to promote to a baseline")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--note", default=None)
    a = ap.parse_args(argv)
    try:
        if a.promote:
            if not a.out:
                raise CheckError("--promote needs --out")
            promote(a.promote, a.out, a.threshold or 0.20, a.alpha or 0.05, a.note)
            return EXIT_OK
        if not (a.baseline and a.current):
            raise CheckError("--baseline and --current are required")
        base = load(a.baseline, (BASELINE, RESULTS))
        cur = load(a.current, (RESULTS,))
        code, rep = run_check(base, cur, a)
    except CheckError as exc:
        print(f"bench-check error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print_report(rep)
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(rep, indent=1, allow_nan=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
