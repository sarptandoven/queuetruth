"""Runs a simulator on queuetruth's referee bug-class fixtures and referees every result (the `referee` GitHub Action).

    python actions/referee/run.py --sim-command 'mysim --data "$QT_DATASET" --orders "$QT_SCHEDULE" --fills "$QT_FILLS"'
        [--classes all | 01,05,...] [--skip 09,12,...] [--simulator NAME@VERSION] [--max-discordant N]
        [--max-qty-mismatch N] [--max-ttf-err-ns DURATION] [--out DIR] [--queuetruth CMD]

For each fixture class (`queuetruth referee fixtures`; docs/how-to/referee-your-simulator.md) the simulator command
runs in a shell with these environment variables, and must write its fills to `$QT_FILLS`:

| variable | value |
|---|---|
| `QT_CLASS` | the class directory name, e.g. `05_partial_fills_sum_to_cum` |
| `QT_DATASET` | the class's order-level dataset directory (`qt.mbo`) |
| `QT_SCHEDULE` | the orders to place (`qt.shadow_spec` Parquet) |
| `QT_FILLS` | where to write the fills (`.parquet`; a CSV or a canonical log directory at that path also works) |
| `QT_WORK` | a private scratch directory for the class |

Then `queuetruth referee run` compares the fills with the exact FIFO engine. Writes `<out>/summary.json` (one entry
per class: `pass`, `discordant`, `qty_mismatch`, `max_abs_ttf_err_ns`, or the error), `<out>/summary.md` (also
appended to `$GITHUB_STEP_SUMMARY` when set), `<out>/badge.json` (a shields.io endpoint document) and every class's
`referee_report.json` under `<out>/reports/`. Exit 0 when every selected class passes, 1 when one fails its
tolerances or the simulator command fails, 2 on a setup error.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def _classes(fixtures: Path, select: str, skip: str) -> list[str]:
    names = sorted(p.name for p in fixtures.iterdir() if p.is_dir())

    def match(spec: str) -> set[str]:
        wanted = {s.strip() for s in spec.replace(" ", ",").split(",") if s.strip()}
        out = {n for n in names if n in wanted or n.split("_", 1)[0] in wanted}
        unknown = {w for w in wanted if not any(n == w or n.split("_", 1)[0] == w for n in names)}
        if unknown:
            raise SystemExit(f"referee action: unknown fixture classes {sorted(unknown)} (have {names})")
        return out

    chosen = names if select.strip() in ("", "all") else sorted(match(select))
    dropped = match(skip) if skip.strip() else set()
    return [n for n in chosen if n not in dropped]


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim-command", required=True, help="shell command that runs the simulator on one class")
    ap.add_argument("--classes", default="all", help="'all' or class names or numbers, comma separated")
    ap.add_argument("--skip", default="", help="class names or numbers to leave out, comma separated")
    ap.add_argument("--simulator", default="", help="NAME[@VERSION] recorded in the reports")
    ap.add_argument("--max-discordant", default="0")
    ap.add_argument("--max-qty-mismatch", default="0")
    ap.add_argument("--max-ttf-err-ns", default="0", help="duration, e.g. 0, 250us, 5ms")
    ap.add_argument("--out", default="queuetruth-referee", help="output directory")
    ap.add_argument("--queuetruth", default="queuetruth", help="the queuetruth command (default: on PATH)")
    a = ap.parse_args(argv)

    qt = shlex.split(a.queuetruth)
    out = Path(a.out).resolve()
    shutil.rmtree(out, ignore_errors=True)
    fixtures = out / "fixtures"
    (out / "reports").mkdir(parents=True)
    r = _run([*qt, "referee", "fixtures", "--out", str(fixtures)])
    if r.returncode != 0:
        print(r.stdout, r.stderr, sep="\n", file=sys.stderr)
        print("referee action: `queuetruth referee fixtures` failed", file=sys.stderr)
        return 2
    try:
        classes = _classes(fixtures, a.classes, a.skip)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    if not classes:
        print("referee action: no fixture class selected", file=sys.stderr)
        return 2

    results: dict[str, dict] = {}
    for name in classes:
        cdir = fixtures / name
        work = out / "work" / name
        work.mkdir(parents=True)
        fills = work / "fills.parquet"
        env = {
            **os.environ,
            "QT_CLASS": name,
            "QT_DATASET": str(cdir / "dataset"),
            "QT_SCHEDULE": str(cdir / "schedule.parquet"),
            "QT_FILLS": str(fills),
            "QT_WORK": str(work),
        }
        sim = subprocess.run(a.sim_command, shell=True, env=env, text=True, capture_output=True, check=False)
        if sim.returncode != 0 or not fills.exists():
            why = f"exit code {sim.returncode}" if sim.returncode != 0 else "no fills written to $QT_FILLS"
            results[name] = {"pass": False, "error": f"simulator command failed: {why}", "stderr": sim.stderr[-2000:]}
            continue
        rep_dir = out / "reports" / name
        cmd = [
            *qt,
            "referee",
            "run",
            "--data",
            env["QT_DATASET"],
            "--schedule",
            env["QT_SCHEDULE"],
            "--sim-fills",
            str(fills),
            "--out",
            str(rep_dir),
            "--max-discordant",
            a.max_discordant,
            "--max-qty-mismatch",
            a.max_qty_mismatch,
            "--max-ttf-err-ns",
            a.max_ttf_err_ns,
        ]
        if a.simulator:
            cmd += ["--simulator", a.simulator]
        ref = _run(cmd)
        report = rep_dir / "referee_report.json"
        if ref.returncode not in (0, 1) or not report.exists():
            results[name] = {"pass": False, "error": f"queuetruth referee run failed: {ref.stderr.strip()[-2000:]}"}
            continue
        doc = json.loads(report.read_text(encoding="utf-8"))
        results[name] = {
            "pass": bool(doc["pass"]),
            "discordant": doc["discordant"],
            "qty_mismatch": doc["qty_mismatch"],
            "max_abs_ttf_err_ns": doc["max_abs_ttf_err_ns"],
            "report": str(report.relative_to(out)),
        }

    n_pass = sum(1 for v in results.values() if v["pass"])
    n = len(results)
    summary = {
        "schema": "qt.referee_action_summary",
        "schema_version": 1,
        "simulator": a.simulator or None,
        "classes": results,
        "passed": n_pass,
        "total": n,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    color = "brightgreen" if n_pass == n else ("yellow" if n_pass >= n * 0.75 else "red")
    badge = {"schemaVersion": 1, "label": "queuetruth referee", "message": f"{n_pass}/{n} passed", "color": color}
    (out / "badge.json").write_text(json.dumps(badge) + "\n", encoding="utf-8")
    lines = [
        f"## queuetruth referee: {n_pass} of {n} fixture classes pass",
        "",
        "| class | result | sim only | exact only | qty mismatch | max first-fill error (ns) |",
        "|---|---|---|---|---|---|",
    ]
    for name, v in results.items():
        if "error" in v:
            lines.append(f"| `{name}` | error: {v['error'].splitlines()[0]} | | | | |")
            continue
        d = v["discordant"]
        lines.append(
            f"| `{name}` | {'pass' if v['pass'] else '**fail**'} | {d['sim_only']} | {d['exact_only']} | "
            f"{v['qty_mismatch']} | {v['max_abs_ttf_err_ns'] if v['max_abs_ttf_err_ns'] is not None else ''} |"
        )
    lines += [
        "",
        "Each class is one scenario that simulators have got wrong; its `README.txt` under the fixtures directory says "
        "what the exact FIFO engine does. Explain a disagreement with `queuetruth why`.",
    ]
    md = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(md, encoding="utf-8")
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as f:
            f.write(md)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"passed={n_pass}\ntotal={n}\nsummary={out / 'summary.json'}\nbadge={out / 'badge.json'}\n")
    print(md)
    return 0 if n_pass == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
