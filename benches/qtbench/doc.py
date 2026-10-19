"""Assemble `qtbench.results` documents from runner output; derived (paired-ratio) metrics; target checks.

Document layout (JSON Schema: benches/schema/qtbench.results.schema.json):
  {schema: "qtbench.results", schema_version: 1, suite, created_utc, env, config, cases: [...], derived: [...]}
Wall-clock fields (`created_utc`, `env.load_avg_*`) are allowed here: this is a harness report, not a data file
covered by SPEC 7.2.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import statistics
from pathlib import Path

from . import env as envmod
from .runner import Case
from .stats import summarize

__all__ = ["case_result", "check_target", "derived_ratio", "results_doc", "write_json"]

SCHEMA = "qtbench.results"
SCHEMA_VERSION = 1

_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}


def check_target(value: float | None, target: dict | None) -> dict | None:
    if target is None:
        return None
    met = None if value is None else bool(_OPS[target["op"]](value, target["value"]))
    return {**target, "measured": value, "met": met}


def case_result(c: Case, raw: dict) -> dict:
    rounds = raw["rounds"]
    metric = [v for rd in rounds for v in rd["metric"]]
    rss = [rd["peak_rss_bytes"] for rd in rounds]
    status = "skipped" if raw["skip_reason"] else ("ok" if metric and not raw["errors"] else "error")
    if metric and raw["errors"]:
        status = "partial"
    summ = summarize(metric)
    extras: dict[str, list[float]] = {}
    for rd in rounds:
        for k, v in rd["extra"].items():
            extras.setdefault(k, []).extend(v if isinstance(v, list) else [v])
    return {
        "id": c.id,
        "group": c.group,
        "unit": c.unit,
        "direction": c.direction,
        "work": c.work,
        "status": status,
        "skip_reason": raw["skip_reason"],
        "errors": raw["errors"],
        "samples": {
            "metric": metric,
            "process_wall_s": [rd["wall_s"] for rd in rounds],
            "user_s": [rd["user_s"] for rd in rounds],
            "sys_s": [rd["sys_s"] for rd in rounds],
            "peak_rss_bytes": rss,
            "load1_before": [rd["load1_before"] for rd in rounds],
            "round": [rd["round"] for rd in rounds],
            "metric_round": [rd["round"] for rd in rounds for _ in rd["metric"]],
        },
        "extra_samples": {k: v for k, v in extras.items()},
        "extra_medians": {k: statistics.median(v) for k, v in extras.items() if v},
        "summary": summ,
        "peak_rss_bytes_max": max(rss) if rss else None,
        "noisy_rounds": raw["noisy_rounds"],
        "target": check_target(summ["median"], c.target),
        "reference": c.reference,
        "notes": c.notes,
    }


def _per_round(c: dict) -> dict[int, float]:
    """Median metric per round (a process may report several in-process samples)."""
    by: dict[int, list[float]] = {}
    for r, m in zip(c["samples"]["metric_round"], c["samples"]["metric"], strict=True):
        by.setdefault(r, []).append(m)
    return {r: statistics.median(v) for r, v in by.items()}


def derived_ratio(
    did: str, num: dict, den: dict, kind: str, target: dict | None = None, notes: list[str] | None = None
) -> dict:
    """Paired per-round ratio between two interleaved cases of the same group.

    kind "cost_ratio": process wall(num) / wall(den) per round (e.g. 2.1 = 2.1x slower);
    kind "throughput_loss": 1 - metric(num)/metric(den) per round (e.g. 0.12 = 12% fewer records/s).
    """
    a, b = _per_round(num), _per_round(den)
    paired = []
    for r in sorted(set(a) & set(b)):
        if kind == "cost_ratio":
            # metric is throughput (higher better): cost ratio = den_metric / num_metric
            paired.append(b[r] / a[r] if num["direction"] == "higher_is_better" else a[r] / b[r])
        elif kind == "throughput_loss":
            paired.append(1.0 - a[r] / b[r])
        else:
            raise ValueError(kind)
    summ = summarize(paired)
    return {
        "id": did,
        "kind": kind,
        "of": [num["id"], den["id"]],
        "paired": paired,
        "summary": summ,
        "target": check_target(summ["median"], target),
        "notes": notes or [],
    }


def results_doc(suite: str, env: dict, config: dict, cases: list[dict], derived: list[dict]) -> dict:
    env = dict(env)
    env["load_avg_end"] = list(os.getloadavg())
    env.update({k: v for k, v in envmod.power_state().items() if k not in env})
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "suite": suite,
        "created_utc": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": env,
        "config": config,
        "cases": cases,
        "derived": derived,
    }


def write_json(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, allow_nan=False) + "\n")
