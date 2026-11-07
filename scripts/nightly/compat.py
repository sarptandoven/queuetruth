#!/usr/bin/env python3
"""The upstream compatibility checks (ROADMAP "Upstream compatibility dashboard"): the nightly runner's engine checks
against the newest hftbacktest and NautilusTrader releases on PyPI, and against the pinned versions for reference.

`.github/workflows/compat.yml` runs it every day; it works the same on any machine with the runner's requirements
(docs/how-to/nightly-checks.md).

  python3 scripts/nightly/compat.py plan --out targets.json [--py-tag cp313 --system Linux --machine x86_64]
      ask PyPI for every release; write the versions to check (pinned, and the newest of each major line from the
      pinned one up, pre-releases included, that has a wheel for the interpreter and platform, by default this
      host's) and print them as `key=value` lines for $GITHUB_OUTPUT
  python3 scripts/nightly/compat.py run --targets targets.json --out DIR --python PY --qt-python QTPY [...]
      one `nightly.py run --mode reduced --only <check>` per target, each with its own state directory and a
      time limit (--target-timeout); writes DIR/upload/compat-<date>.json (schema qt.compat.result) after every
      target, so a later hang keeps the earlier results, and one log per target next to it
  python3 scripts/nightly/compat.py select DIR --date YYYY-MM-DD
      the files of DIR to publish: compat-<date>.json when it is a valid result of that date, and the logs it names
      (the workflow's publish job uploads only these from the check job's artifact)
  python3 scripts/nightly/compat.py verdict RESULT.json
      GitHub annotations; exit 1 when a pinned version did not pass (a newer release that disagrees is a warning:
      it is what the dashboard is for, not a regression of queuetruth)
  python3 scripts/nightly/compat.py prune --today YYYY-MM-DD [--keep-days 90] < asset-names
      the release assets older than --keep-days, one per line (the workflow deletes them)

The checks replay the synthetic Nasdaq day of the nightly runner (seed 7), so no market data is needed or published.
Standard library only, like the runner.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import nightly as N  # noqa: E402
from qtnightly import envs as E  # noqa: E402

SCHEMA_RESULT = "qt.compat.result"
SCHEMA_VERSION = 1

#: (library, PyPI project, check of a pinned version, check of any other version, the runner option naming it)
LIBRARIES = (
    ("hftbacktest", "hftbacktest", "hbt", "hbt", "--hbt"),
    ("nautilus_trader", "nautilus-trader", "nt-pinned", "nt-rc", "--nt-rc"),
)
RESULT_NAME = re.compile(r"^compat-(\d{4}-\d{2}-\d{2})(?:\.json|-.+\.log\.txt)$")


# ---------------------------------------------------------------------------------------------------------------
# plan: which versions to check
# ---------------------------------------------------------------------------------------------------------------


def host_installable(
    py_tag: str, system: str, machine: str, os_major: int | None = None
) -> Callable[[list[dict[str, Any]]], bool]:
    """Whether a release's files hold a wheel of `py_tag` for this host (`system` as `platform.system()` names it;
    the GitHub runner is Linux x86_64)."""
    return lambda files: E.wheel_for_host(files, py_tag, system, machine, os_major) is not None


def newest_per_major(
    releases: dict[str, list[dict[str, Any]]], min_major: int, installable: Callable[[list[dict[str, Any]]], bool]
) -> list[str]:
    """The newest installable release (pre-releases included) of every major line from `min_major` up, newest first.

    One version per line, so a 1.x maintenance release is checked even while 2.x release candidates come out."""
    majors = sorted({k[0] for v in releases if (k := E.version_key(v)) is not None and k[0] >= min_major})
    found = [v for m in majors if (v := E.newest_installable(releases, m, installable)) is not None]
    return sorted(found, key=lambda v: E.version_key(v) or (), reverse=True)


def plan_targets(
    pinned: dict[str, str],
    fetch: Callable[[str], dict[str, Any]],
    py_tag: str = "cp313",
    system: str = "Linux",
    machine: str = "x86_64",
    os_major: int | None = None,
) -> list[dict[str, Any]]:
    """The versions to check: `pinned[library]`, then the newest release of each major line from the pinned one up.

    A newest release equal to the pinned version is one target with both roles; one older than the pinned version
    (the pin is not on PyPI, or not for this platform) is left out. `fetch(url) -> JSON` is the PyPI client
    (`qtnightly.envs.fetch_json`; tests pass a fake)."""
    targets: list[dict[str, Any]] = []
    ok = host_installable(py_tag, system, machine, os_major)
    for library, project, pinned_check, other_check, option in LIBRARIES:
        pin = pinned[library]
        pin_key = E.version_key(pin)
        if pin_key is None:
            raise ValueError(f"{library}: pinned version {pin!r} is not a release version")
        pinned_target = {"library": library, "version": pin, "roles": ["pinned"], "check": pinned_check}
        pinned_target["args"] = [option, pin] if pinned_check == other_check else []
        mine = [pinned_target]
        releases = E.pypi_project(project, fetch).get("releases") or {}
        for v in newest_per_major(releases, pin_key[0], ok):
            role = f"newest {E.version_key(v)[0]}.x"  # type: ignore[index]
            if v == pin:
                pinned_target["roles"].append(role)
            elif (E.version_key(v) or ()) > pin_key:
                mine.append({"library": library, "version": v, "roles": [role], "check": other_check,
                             "args": [option, v]})  # fmt: skip
        targets += mine
    return targets


def github_output(targets: list[dict[str, Any]]) -> str:
    """`key=value` lines: one per library (its versions, comma separated) and the count."""
    lines = [f"count={len(targets)}"]
    for library, *_ in LIBRARIES:
        lines.append(f"{library}={','.join(t['version'] for t in targets if t['library'] == library)}")
    return "\n".join(lines) + "\n"


def cmd_plan(a: argparse.Namespace, fetch: Callable[[str], dict[str, Any]] = E.fetch_json) -> int:
    repo = Path(a.repo).resolve()
    pinned = {"hftbacktest": N.pinned_hbt_version(repo), "nautilus_trader": N.pinned_nt_version(repo)[0]}
    os_major = E.mac_major() if a.system == "Darwin" else None
    targets = plan_targets(pinned, fetch, a.py_tag, a.system, a.machine, os_major)
    Path(a.out).write_text(json.dumps(targets, indent=1) + "\n")
    sys.stdout.write(github_output(targets))
    return 0


# ---------------------------------------------------------------------------------------------------------------
# run: one nightly run per target
# ---------------------------------------------------------------------------------------------------------------


TIMEOUT_RC = 124


def run_with_timeout(argv: list[str], log: Path, timeout_s: float, grace_s: float = 120.0) -> int:
    """Runs `argv` in its own process group with output to `log`; at `timeout_s` sends SIGTERM (the nightly runner
    then stops its check and records a partial run), SIGKILL after `grace_s`, and returns `TIMEOUT_RC`."""
    with log.open("w") as f:
        p = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            f.write(f"\n[compat] stopped at the {timeout_s:g} s time limit\n")
            f.flush()
            for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 30.0)):
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
            return TIMEOUT_RC


def slug(target: dict[str, Any]) -> str:
    """`hftbacktest-2.4.4`: the target's state directory and the middle of its log asset name."""
    return f"{target['library']}-{target['version']}"


def runner_argv(target: dict[str, Any], state: Path, a: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(HERE / "nightly.py"),
        "run",
        "--repo",
        str(Path(a.repo).resolve()),
        "--mode",
        a.mode,
        "--state",
        str(state),
        "--only",
        target["check"],
        "--python",
        a.python,
        "--qt-python",
        a.qt_python,
        "--min-free-gib",
        str(a.min_free_gib),
        "--threads",
        str(a.threads),
        *target.get("args", []),
    ]


def collect_log(state: Path, run: dict[str, Any] | None, check: str, dest: Path) -> None:
    """One text file per target: the runner's log, the setup logs and the check's log, each under a header."""
    parts: list[Path] = []
    if run is not None:
        rdir = state / "runs" / run["nightly_id"]
        parts = [rdir / "runner.log", *sorted((rdir / "setup").glob("*.log")), rdir / "checks" / check / "log.txt"]
    parts.append(state / "compat-runner.log")
    with dest.open("w") as out:
        for p in parts:
            if p.is_file():
                out.write(f"===== {p.relative_to(state)} =====\n")
                out.write(p.read_text(errors="replace"))
                out.write("\n")


def target_result(target: dict[str, Any], run: dict[str, Any] | None, rc: int) -> dict[str, Any]:
    """The result row of one target from its run's `latest.json` (None when the runner wrote none)."""
    row: dict[str, Any] = {k: target[k] for k in ("library", "version", "roles", "check")}
    row["exit_code"] = rc
    res = (run or {}).get("checks", {}).get(target["check"])
    if res is None:
        reason = (run or {}).get("runner_error") or f"the nightly runner wrote no result (exit {rc})"
        row.update(status="error", reason=reason, gates=[], wall_s=None)
        return row
    row["status"] = res.get("status", "error")
    if res.get("reason"):
        row["reason"] = res["reason"]
    row["gates"] = [{"name": g["name"], "ok": bool(g["ok"]), "detail": g.get("detail", "")} for g in res["gates"]]
    row["wall_s"] = res.get("wall_s")
    if run and (qt := (run.get("setup") or {}).get("qt", {}).get("queuetruth")):
        row["queuetruth"] = qt
    return row


def run_targets(
    targets: list[dict[str, Any]],
    a: argparse.Namespace,
    runner: Callable[[list[str], Path], int] | None = None,
) -> dict[str, Any]:
    """Runs every target and writes `<out>/upload/compat-<date>.json` and the logs; returns the result document.

    `runner(argv, log, timeout_s) -> exit code` runs one nightly run, `TIMEOUT_RC` when it was stopped at the time
    limit (tests pass a fake that writes a `latest.json`). The document is rewritten after every target."""
    out = Path(a.out)
    upload = out / "upload"
    upload.mkdir(parents=True, exist_ok=True)
    run_one = runner or run_with_timeout
    rows: list[dict[str, Any]] = []
    doc: dict[str, Any] = {
        "schema": SCHEMA_RESULT,
        "schema_version": SCHEMA_VERSION,
        "date": a.date,
        "mode": a.mode,
        "commit": a.commit or None,
        "run_url": a.run_url or None,
        "targets": rows,
    }
    for t in targets:
        state = out / "state" / slug(t)
        if state.exists():
            shutil.rmtree(state)
        state.mkdir(parents=True)
        rc = run_one(runner_argv(t, state, a), state / "compat-runner.log", a.target_timeout)
        try:
            run = json.loads((state / "latest.json").read_text())
        except (OSError, ValueError):
            run = None
        row = target_result(t, run, rc)
        if rc == TIMEOUT_RC and row["status"] != "pass":
            why = f"stopped at the {a.target_timeout:g} s time limit"
            row["status"] = "error"
            row["reason"] = f"{why}: {row['reason']}" if row.get("reason") else why
        log_name = f"compat-{a.date}-{slug(t)}.log.txt"
        collect_log(state, run, t["check"], upload / log_name)
        row["log"] = log_name
        if a.log_base:
            row["log_url"] = a.log_base.rstrip("/") + "/" + log_name
        rows.append(row)
        print(f"{slug(t)} ({t['check']}, {', '.join(t['roles'])}): {row['status']}", file=sys.stderr, flush=True)
        (upload / f"compat-{a.date}.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return doc


def summary_markdown(doc: dict[str, Any]) -> str:
    """The job summary: one row per target."""
    lines = [
        f"### Upstream compatibility {doc['date']}",
        "",
        "| library | version | role | check | result | gates passed |",
        "|---|---|---|---|---|---|",
    ]
    for r in doc["targets"]:
        passed = sum(g["ok"] for g in r["gates"])
        lines.append(
            f"| {r['library']} | {r['version']} | {', '.join(r['roles'])} | `{r['check']}` | {r['status']} "
            f"| {passed} / {len(r['gates'])} |"
        )
    return "\n".join(lines) + "\n"


def cmd_run(a: argparse.Namespace) -> int:
    targets = json.loads(Path(a.targets).read_text())
    doc = run_targets(targets, a)
    if a.summary:
        with Path(a.summary).open("a") as f:
            f.write(summary_markdown(doc))
    return 0


# ---------------------------------------------------------------------------------------------------------------
# verdict and prune
# ---------------------------------------------------------------------------------------------------------------


def escape_command_data(text: str) -> str:
    """A workflow command's message as GitHub requires: `%`, CR and LF encoded, so a reason that holds a newline
    cannot start a command of its own."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def verdict(doc: dict[str, Any]) -> tuple[int, list[str]]:
    """(exit code, GitHub annotation lines): 1 when a pinned version did not pass."""
    lines, rc = [], 0
    for r in doc["targets"]:
        if r["status"] == "pass":
            continue
        failed = [g["name"] for g in r["gates"] if not g["ok"]]
        why = r.get("reason") or ("failed gates: " + "; ".join(failed) if failed else "no gate")
        msg = escape_command_data(f"{r['library']} {r['version']} ({', '.join(r['roles'])}): {r['status']}: {why}")
        if "pinned" in r["roles"]:
            rc = 1
            lines.append(f"::error title=compat::{msg}")
        else:
            lines.append(f"::warning title=compat::{msg}")
    return rc, lines


def cmd_verdict(a: argparse.Namespace) -> int:
    rc, lines = verdict(json.loads(Path(a.result).read_text()))
    for line in lines:
        print(line)
    return rc


def valid_result(doc: Any) -> bool:
    """A `qt.compat.result` document whose targets are objects with string library, version and status."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_RESULT or not isinstance(doc.get("targets"), list):
        return False
    return all(
        isinstance(t, dict) and all(isinstance(t.get(k), str) for k in ("library", "version", "status"))
        for t in doc["targets"]
    )


def upload_files(directory: Path, date: str) -> list[Path]:
    """The files of `directory` to publish for `date`: `compat-<date>.json` (a valid result whose own date is
    `date`) and the logs it names, which must be `compat-<date>-*.log.txt` files there. Anything else in the
    directory is ignored; a missing or invalid result raises ValueError. The directory comes from the job that ran
    the new packages, so nothing in it is trusted to choose a name or a date."""
    dt.date.fromisoformat(date)
    result = directory / f"compat-{date}.json"
    try:
        doc = json.loads(result.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"{result.name}: {type(e).__name__}: {e}") from e
    if not valid_result(doc):
        raise ValueError(f"{result.name}: not a valid {SCHEMA_RESULT} document")
    if doc.get("date") != date:
        raise ValueError(f"{result.name}: its date is {doc.get('date')!r}, not {date}")
    log_name = re.compile(rf"^compat-{re.escape(date)}-[A-Za-z0-9._-]+\.log\.txt$")
    logs = []
    for t in doc["targets"]:
        name = t.get("log")
        if name is None:
            continue
        if not isinstance(name, str) or not log_name.match(name) or not (directory / name).is_file():
            raise ValueError(f"{result.name}: log {name!r} is not a compat-{date}-*.log.txt file of the directory")
        logs.append(directory / name)
    return [result, *sorted(set(logs))]


def cmd_select(a: argparse.Namespace) -> int:
    for p in upload_files(Path(a.dir), a.date):
        print(p)
    return 0


def stale_assets(names: Sequence[str], today: dt.date, keep_days: int) -> list[str]:
    """Result and log asset names dated more than `keep_days` before `today` (other names are never returned)."""
    cutoff = today - dt.timedelta(days=keep_days)
    out = []
    for n in names:
        m = RESULT_NAME.match(n.strip())
        if m and dt.date.fromisoformat(m.group(1)) < cutoff:
            out.append(n.strip())
    return sorted(out)


def cmd_prune(a: argparse.Namespace) -> int:
    for n in stale_assets(sys.stdin.read().split(), dt.date.fromisoformat(a.today), a.keep_days):
        print(n)
    return 0


# ---------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    today = dt.datetime.now(dt.UTC).date().isoformat()

    p = sub.add_parser("plan", help="the versions to check, from PyPI")
    p.add_argument("--repo", default=str(N.REPO_DEFAULT))
    p.add_argument("--out", required=True, help="targets JSON to write")
    p.add_argument(
        "--py-tag",
        default=f"cp{sys.version_info[0]}{sys.version_info[1]}",
        help="interpreter tag a release must have a wheel for (default: this interpreter's)",
    )
    p.add_argument("--system", default=platform.system(), help="Linux or Darwin (default: this host)")
    p.add_argument("--machine", default=platform.machine(), help="x86_64, arm64, aarch64 (default: this host)")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("run", help="one nightly run per target; the result JSON and the logs")
    p.add_argument("--repo", default=str(N.REPO_DEFAULT))
    p.add_argument("--targets", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--python", required=True, help="interpreter the engine venvs are created from")
    p.add_argument("--qt-python", required=True, help="interpreter that imports this checkout's queuetruth")
    p.add_argument("--mode", choices=sorted(N.MODES), default="reduced")
    p.add_argument("--date", default=today, help="date of the result (default: today, UTC)")
    p.add_argument("--min-free-gib", type=float, default=2.0)
    p.add_argument("--target-timeout", type=float, default=2400.0, help="seconds one version may take (setup included)")
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--commit", default="", help="commit checked (recorded)")
    p.add_argument("--run-url", default="", help="URL of the CI run (recorded, linked from the page)")
    p.add_argument("--log-base", default="", help="URL the log assets are served under (recorded per target)")
    p.add_argument("--summary", default="", help="append a Markdown table here (e.g. $GITHUB_STEP_SUMMARY)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("verdict", help="annotations; exit 1 when a pinned version did not pass")
    p.add_argument("result")
    p.set_defaults(fn=cmd_verdict)

    p = sub.add_parser("select", help="the files of DIR to publish for --date")
    p.add_argument("dir")
    p.add_argument("--date", required=True)
    p.set_defaults(fn=cmd_select)

    p = sub.add_parser("prune", help="asset names (stdin) older than --keep-days")
    p.add_argument("--today", default=today)
    p.add_argument("--keep-days", type=int, default=90)
    p.set_defaults(fn=cmd_prune)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except ValueError as e:
        print(f"compat: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
