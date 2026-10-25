"""Run history (`<state>/history.json`), the diff of a run against the earlier runs of its mode (per check), the exit
code and its flags. Pure functions over plain dicts (the run summary documents written by `nightly.py`)."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from . import (
    EXIT_ADVISORY,
    EXIT_CHANGED,
    EXIT_ERROR,
    EXIT_FAIL,
    EXIT_MEANING,
    EXIT_OK,
    SCHEMA_HISTORY,
    SCHEMA_VERSION,
)

REL_TOL = 1e-12  # two floats are the same number when math.isclose(a, b, rel_tol=REL_TOL, abs_tol=0.0)


def empty_history() -> dict[str, Any]:
    return {"schema": SCHEMA_HISTORY, "schema_version": SCHEMA_VERSION, "runs": []}


def load_history(path: Path) -> dict[str, Any]:
    """The history document, or an empty one when the file does not exist. A file of another schema is an error."""
    if not path.exists():
        return empty_history()
    doc = json.loads(path.read_text())
    if doc.get("schema") != SCHEMA_HISTORY or not isinstance(doc.get("runs"), list):
        raise ValueError(f"{path}: not a {SCHEMA_HISTORY} document")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version {doc.get('schema_version')} (this runner reads {SCHEMA_VERSION})")
    return doc


def save_history(path: Path, doc: dict[str, Any]) -> None:
    """Atomic write (temporary file + rename), so an interrupted run never leaves a truncated history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def append_run(doc: dict[str, Any], run: dict[str, Any], keep: int) -> dict[str, Any]:
    """History with `run` appended (oldest first), trimmed to the newest `keep` runs; `run` replaces an entry with
    the same `nightly_id`."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    runs = [r for r in doc["runs"] if r.get("nightly_id") != run["nightly_id"]]
    runs.append(run)
    return {**doc, "runs": runs[-keep:]}


def previous_run(doc: dict[str, Any], mode: str, before_id: str | None = None) -> dict[str, Any] | None:
    """The newest run of `mode` in the history (strictly before `before_id` when given)."""
    for r in reversed(doc["runs"]):
        if r.get("mode") != mode:
            continue
        if before_id is not None and r.get("nightly_id", "") >= before_id:
            continue
        return r
    return None


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        if isinstance(a, int) and isinstance(b, int):
            return a == b
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=0.0)
    return a == b


def _is_noisy(name: str, noisy: list[str]) -> bool:
    return any(name == p or name.startswith(p) for p in noisy)


def _earlier(runs: list[dict[str, Any]], cur: dict[str, Any]) -> list[dict[str, Any]]:
    """Runs of `cur`'s mode strictly before it, newest first."""
    nid, mode = cur.get("nightly_id", ""), cur.get("mode")
    return [r for r in reversed(runs) if r.get("mode") == mode and r.get("nightly_id", "") < nid]


def diff_runs(prev: dict[str, Any] | list[dict[str, Any]] | None, cur: dict[str, Any]) -> dict[str, Any]:
    """What changed in `cur` against the earlier runs of its mode (`prev`: the history's runs, oldest first, or a
    single earlier run, or None).

    Each check `cur` ran is compared with its own baseline, so a partial run (`--only`, `--skip`) or a night on which
    a check errored never hides a change from the next full night:

    - `baselines[check]`: `status`, the newest earlier run that ran the check; `metrics`, the newest earlier run in
      which it produced results (status `pass` or `fail`).
    - `status_changes`: checks whose status differs from their status baseline. A check `cur` did not run is not
      compared, and a check with no earlier run has no baseline (it is listed in `new_checks`, not counted).
    - `changed`: tracked metrics whose value differs from the metrics baseline (integers and strings exactly, floats
      by `math.isclose` with a relative tolerance of 1e-12 and no absolute tolerance), plus metrics that appeared or
      disappeared. Not computed for a check that errored in `cur`.
    - `noisy`: metrics the check declares noisy (timings, benchmark medians), with their relative change; never
      counted.
    - `context`: provenance that changed (source tree hash, engine version, input paths); shown, never counted.
    - `vs`: the newest earlier run of the mode, or None when there is none.
    """
    runs = [] if prev is None else [prev] if isinstance(prev, dict) else list(prev)
    earlier = _earlier(runs, cur)
    out: dict[str, Any] = {
        "vs": earlier[0].get("nightly_id") if earlier else None,
        "baselines": {},
        "status_changes": [],
        "changed": [],
        "noisy": [],
        "context": [],
        "new_checks": [],
    }
    if not earlier:
        return out
    for name in sorted(cur.get("checks", {})):
        b = cur["checks"][name]
        st_base = next((r for r in earlier if name in r.get("checks", {})), None)
        if st_base is None:
            out["new_checks"].append(name)
            continue
        m_base = next((r for r in earlier if r.get("checks", {}).get(name, {}).get("status") in ("pass", "fail")), None)
        out["baselines"][name] = {
            "status": st_base.get("nightly_id"),
            "metrics": None if m_base is None else m_base.get("nightly_id"),
        }
        sa, sb = st_base["checks"][name].get("status"), b.get("status")
        if sa != sb:
            out["status_changes"].append({"check": name, "before": sa, "after": sb, "vs": st_base.get("nightly_id")})
        if m_base is None or sb not in ("pass", "fail"):
            continue
        a = m_base["checks"][name]
        pa, pb = a.get("provenance", {}), b.get("provenance", {})
        for key in sorted(set(pa) | set(pb)):
            va, vb = pa.get(key), pb.get(key)
            if va != vb:
                out["context"].append({"check": name, "what": key, "before": va, "after": vb})
        ma, mb = a.get("metrics", {}), b.get("metrics", {})
        noisy = list(b.get("noisy", [])) + list(a.get("noisy", []))
        for key in sorted(set(ma) | set(mb)):
            va, vb = ma.get(key, _MISSING), mb.get(key, _MISSING)
            if va is not _MISSING and vb is not _MISSING and _same(va, vb):
                continue
            entry: dict[str, Any] = {
                "check": name,
                "metric": key,
                "before": None if va is _MISSING else va,
                "after": None if vb is _MISSING else vb,
            }
            if m_base is not earlier[0]:
                entry["vs"] = m_base.get("nightly_id")
            if va is _MISSING or vb is _MISSING:
                entry["kind"] = "added" if va is _MISSING else "removed"
            if _is_noisy(key, noisy):
                if isinstance(va, int | float) and isinstance(vb, int | float) and va:
                    entry["rel_change"] = vb / va - 1.0
                out["noisy"].append(entry)
            else:
                out["changed"].append(entry)
    return out


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def run_flags(checks: dict[str, dict[str, Any]], diff: dict[str, Any], runner_error: bool = False) -> list[str]:
    """Every condition that holds for a run, so a script need not decode one exit code: `error` (a required check
    could not run, or a runner or setup error), `fail` (a required check failed), `advisory` (the advisory check
    failed or errored), `changed` (a tracked number or a status changed)."""
    req = [c for c in checks.values() if c.get("required", True)]
    adv = [c for c in checks.values() if not c.get("required", True)]
    flags = []
    if runner_error or any(c["status"] == "error" for c in req):
        flags.append("error")
    if any(c["status"] == "fail" for c in req):
        flags.append("fail")
    if any(c["status"] in ("fail", "error") for c in adv):
        flags.append("advisory")
    if diff.get("changed") or diff.get("status_changes"):
        flags.append("changed")
    return flags


def exit_code(checks: dict[str, dict[str, Any]], diff: dict[str, Any], runner_error: bool = False) -> int:
    """Exit code of a run (precedence 2 > 1 > 4 > 3 > 0; see `qtnightly.EXIT_MEANING`)."""
    flags = run_flags(checks, diff, runner_error)
    for flag, code in (("error", EXIT_ERROR), ("fail", EXIT_FAIL), ("advisory", EXIT_ADVISORY)):
        if flag in flags:
            return code
    return EXIT_CHANGED if "changed" in flags else EXIT_OK


def verdict(code: int, diff: dict[str, Any]) -> str:
    """The exit code's meaning, plus the tracked changes a higher-precedence code would otherwise hide."""
    text = EXIT_MEANING.get(code, f"exit {code}")
    n = len(diff.get("changed", [])) + len(diff.get("status_changes", []))
    if code in (EXIT_ERROR, EXIT_FAIL, EXIT_ADVISORY) and n:
        text += f" (also {n} tracked change(s) against earlier runs)"
    return text


def flatten(prefix: str, obj: Any, out: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scalar leaves of nested dicts / lists as `prefix.a.b[0]` -> value (bool, int, float, str, None)."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k in sorted(obj, key=str):
            flatten(f"{prefix}.{k}" if prefix else str(k), obj[k], out)
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            flatten(f"{prefix}[{i}]", v, out)
    elif obj is None or isinstance(obj, bool | int | float | str):
        out[prefix] = obj
    return out
