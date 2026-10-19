"""Referee comparison (SPEC 5.8): exact-engine replay of a schedule vs another simulator's fills.

Per scheduled shadow: exact outcome from `qt.shadow_results` / `qt.shadow_fills`; simulator outcome from either a
`qt.shadow_fills`-like table (`shadow_id`, `fill_qty`, `ts_event`; `qt.zoo_fills` float quantities are rounded) or a
canonical simulator log (directory or `(orders, events)` tables; only `order_key`, `event_type`, `qty`, `px`,
`fill_id`, `ref_fill_id` and a timestamp are needed, engine-only columns are not; the order `venue = "sim:<name>"`
written by `queuetruth.live.import_sim` names the simulator, E38 OC-9, with an event `source` of that form as a
fallback). Log orders match a shadow by `shadow_id` or by the schedule's `order_key`.
Comparison unit (review F4): a canonical log is keyed by order (`import_sim` strips `.leg<k>`), so the exact legs of
one `order_key` are aggregated first (filled qty = sum, first fill = earliest leg fill, reason = that leg's
`first_fill_reason`, end state = the last leg's) and compared once with the order's simulator outcome; legs are
compared one by one when the simulator outcomes are keyed by `shadow_id` (a `qt.shadow_fills`-like table, or a log
that carries a later leg's id). `n_orders` counts the comparison units.
Counts: `discordant.sim_only` (sim filled, exact not; sim orders unknown to the schedule count here too),
`discordant.exact_only`; `qty_mismatch` = orders filled in both with different filled qty; first-fill time error
`sim - exact` over orders filled in both (`max_abs_ttf_err_ns`, `ttf_err_p50_ns` = median absolute error rounded
to ns). `by_reason` counts disagreeing orders by the exact `first_fill_reason` (`never` when exact did not fill).
`pass` iff `sim_only + exact_only <= max_discordant`, `qty_mismatch <= max_qty_mismatch` and the max absolute
first-fill error `<= max_abs_ttf_err_ns`.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.schemas.models import RefereeReport
from queuetruth.schemas.runid import spec_echo
from queuetruth.schemas.specs import RefereeSpec, RefereeTolerance, ReplaySpec, SimulatorInfo
from queuetruth.score._io import (
    SIM_PREFIX,
    Row,
    input_format,
    read_live_log,
    read_parquet,
    ref_path,
    rows,
    sim_label,
    write_json,
)
from queuetruth.score.outcomes import log_outcomes
from queuetruth.score.runner import DefaultRunner, ScoreRunner, find_table

MAX_EXAMPLES = 20


def _sim_from_fills(t: pa.Table) -> dict[str, tuple[int, int | None]]:
    if "shadow_id" not in t.column_names or "fill_qty" not in t.column_names:
        raise input_format("sim_fills", "need shadow_id and fill_qty columns (qt.shadow_fills) or a canonical log")
    out: dict[str, list[int]] = {}
    first: dict[str, int] = {}
    for r in rows(t):
        sid = str(r["shadow_id"])
        q = r["fill_qty"]
        qi = math.floor(float(q) + 0.5 + 1e-9) if isinstance(q, float) else int(q)
        out.setdefault(sid, []).append(qi)
        ts = r.get("ts_event", r.get("ts"))
        if qi > 0 and ts is not None:
            first[sid] = min(first.get(sid, int(ts)), int(ts))
    return {k: (sum(v), first.get(k)) for k, v in out.items()}


def load_sim(sim_log: Any) -> tuple[dict[str, tuple[int, int | None]], str | None]:
    """Simulator outcomes `{key: (filled_qty, ts_first_fill)}` and the simulator name found in the log."""
    sim, name, _ = _load_sim(sim_log)
    return sim, name


def _load_sim(sim_log: Any) -> tuple[dict[str, tuple[int, int | None]], str | None, bool]:
    """`load_sim` plus whether the outcomes are keyed by `shadow_id` (a fills table) rather than by order (a log)."""
    if isinstance(sim_log, tuple):
        orders, events = (rows(x) for x in sim_log)
        return (*_from_log(orders, events), False)
    if isinstance(sim_log, pa.Table):
        return _sim_from_fills(sim_log), None, True
    p = Path(str(sim_log))
    if p.is_dir():
        return (*_from_log(*read_live_log(p)), False)
    return _sim_from_fills(read_parquet(p)), None, True


def _from_log(orders: list[Row], events: list[Row]) -> tuple[dict[str, tuple[int, int | None]], str | None]:
    oc, _ = log_outcomes(orders, events)
    label = sim_label(orders, events)
    return {k: (o.filled_qty, o.ts_first_fill) for k, o in oc.items()}, (label[len(SIM_PREFIX) :] if label else None)


def _first(r: Row, ex_fill_first: dict[str, int]) -> int | None:
    v = r.get("ts_first_fill")
    return ex_fill_first.get(str(r["shadow_id"])) if v is None else int(v)


def _units(
    res: list[Row],
    alias: dict[str, str],
    sim: dict[str, tuple[int, int | None]],
    by_leg: bool,
    ex_fill_first: dict[str, int],
) -> list[tuple[str, str | None, Row, int, int | None]]:
    """Comparison units `(order_key, sim key or None, representative exact row, exact filled qty, exact first fill)`:
    one per leg when the simulator is keyed by `shadow_id`, else one per `order_key` with its legs aggregated."""
    groups: dict[str, list[Row]] = {}
    for r in res:
        sid = str(r["shadow_id"])
        groups.setdefault(alias.get(sid, sid), []).append(r)
    out: list[tuple[str, str | None, Row, int, int | None]] = []
    for key in sorted(groups):
        legs = groups[key]
        per_leg = by_leg or any(str(r["shadow_id"]) != key and str(r["shadow_id"]) in sim for r in legs)
        if per_leg or len(legs) == 1:
            for r in legs:
                sid = str(r["shadow_id"])
                sk = sid if sid in sim else (key if key in sim and len(legs) == 1 else None)
                out.append((key, sk, r, int(r["filled_qty"]), _first(r, ex_fill_first)))
            continue
        firsts = [(f, i) for i, r in enumerate(legs) if (f := _first(r, ex_fill_first)) is not None]
        e_first = min(firsts)[0] if firsts else None
        first_leg = legs[min(firsts)[1]] if firsts else None
        last = max(legs, key=lambda r: (int(r.get("ts_submit") or 0), str(r["shadow_id"])))
        rep = {
            "shadow_id": key,
            "end_state": last.get("end_state"),
            "first_fill_reason": first_leg.get("first_fill_reason") if first_leg is not None else "never",
        }
        out.append((key, key if key in sim else None, rep, sum(int(r["filled_qty"]) for r in legs), e_first))
    return out


def compare_referee(
    exact_results: pa.Table,
    exact_fills: pa.Table,
    sim_log: Any,
    tolerance: Any,
    *,
    schedule: pa.Table | None = None,
    spec: dict[str, Any] | None = None,
    simulator: SimulatorInfo | dict[str, Any] | None = None,
) -> RefereeReport:
    """Referee report for one simulator against exact-engine output (see module doc)."""
    tol = RefereeTolerance.model_validate(tolerance) if not isinstance(tolerance, RefereeTolerance) else tolerance
    sim, found_name, by_leg = _load_sim(sim_log)
    alias: dict[str, str] = {}
    if schedule is not None:
        for r in rows(schedule):
            if r.get("order_key"):
                alias[str(r["shadow_id"])] = str(r["order_key"])
    ex_fill_first: dict[str, int] = {}
    for f in rows(exact_fills):
        if int(f["fill_qty"]) > 0:
            sid = str(f["shadow_id"])
            ex_fill_first[sid] = min(ex_fill_first.get(sid, int(f["ts_event"])), int(f["ts_event"]))
    used: set[str] = set()
    sim_only = exact_only = qty_mismatch = both = 0
    errs: list[int] = []
    by_reason: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    res = sorted(rows(exact_results), key=lambda r: str(r["shadow_id"]))
    units = _units(res, alias, sim, by_leg, ex_fill_first)
    for key, sk, r, e_qty, e_first in units:
        if sk is not None:
            used.add(sk)
        s_qty, s_first = sim.get(sk, (0, None)) if sk is not None else (0, None)
        e_f, s_f = e_qty > 0, s_qty > 0
        bad = False
        if s_f and not e_f:
            sim_only += 1
            bad = True
        elif e_f and not s_f:
            exact_only += 1
            bad = True
        elif e_f and s_f:
            both += 1
            if s_qty != e_qty:
                qty_mismatch += 1
                bad = True
            if s_first is not None and e_first is not None:
                errs.append(int(s_first) - int(e_first))
                if s_first != e_first and abs(int(s_first) - int(e_first)) > tol.max_abs_ttf_err_ns:
                    bad = True
        if bad:
            reason = str(r.get("first_fill_reason") or "never")
            by_reason[reason] = by_reason.get(reason, 0) + 1
            if len(examples) < MAX_EXAMPLES:
                examples.append(
                    {
                        "order_key": key,
                        "exact": {
                            "filled_qty": e_qty,
                            "ts_first_fill": e_first,
                            "end_state": r.get("end_state"),
                            "first_fill_reason": r.get("first_fill_reason"),
                        },
                        "sim": {"filled_qty": s_qty, "ts_first_fill": s_first},
                    }
                )
    for k in sorted(set(sim) - used):
        if sim[k][0] > 0:
            sim_only += 1
            by_reason["unknown_order"] = by_reason.get("unknown_order", 0) + 1
            if len(examples) < MAX_EXAMPLES:
                examples.append(
                    {"order_key": k, "exact": {}, "sim": {"filled_qty": sim[k][0], "ts_first_fill": sim[k][1]}}
                )
    abs_err = [abs(e) for e in errs]
    max_err = max(abs_err) if abs_err else None
    p50 = round(float(np.median(abs_err))) if abs_err else None
    passed = (
        sim_only + exact_only <= tol.max_discordant
        and qty_mismatch <= tol.max_qty_mismatch
        and (max_err is None or max_err <= tol.max_abs_ttf_err_ns)
    )
    simu = SimulatorInfo.model_validate(simulator) if isinstance(simulator, dict) else simulator
    if simu is None or (simu.name == "unknown" and found_name):
        simu = SimulatorInfo(name=found_name or "unknown", version=simu.version if simu else None)
    return RefereeReport.model_validate(
        {
            "spec": spec or {},
            "simulator": simu.model_dump(),
            "n_orders": len(units),
            "n_both_filled": both,
            "discordant": {"sim_only": sim_only, "exact_only": exact_only},
            "qty_mismatch": qty_mismatch,
            "max_abs_ttf_err_ns": max_err,
            "ttf_err_p50_ns": p50,
            "by_reason": dict(sorted(by_reason.items())),
            "tolerance": tol.model_dump(),
            "pass": passed,
            "examples": examples,
        }
    )


def run_referee(spec: RefereeSpec | dict[str, Any], *, runner: ScoreRunner | None = None) -> RefereeReport:
    """`referee run`: exact replay of `spec.schedule`, then `compare_referee`; writes `referee_report.json` to
    `spec.out` when set."""
    s = RefereeSpec.model_validate(spec) if isinstance(spec, dict) else spec
    rn = runner or DefaultRunner()
    sched = ref_path(s.schedule, "schedule")
    sim = ref_path(s.sim_fills, "sim_fills")
    with tempfile.TemporaryDirectory(prefix="qt-referee-") as tmp:
        rs = ReplaySpec.model_validate(
            {
                "dataset": {"path": str(ref_path(s.dataset, "dataset"))},
                "out": str(Path(tmp) / "exact"),
                "shadows": {"path": str(sched)},
                "bbo": "none",
                "trajectory": "none",
            }
        )
        rn.replay(rs)
        res = find_table(Path(tmp) / "exact", "qt.shadow_results")
        fls = find_table(Path(tmp) / "exact", "qt.shadow_fills")
        if res is None:
            raise input_format(tmp, "exact replay wrote no qt.shadow_results")
        sched_t = read_parquet(sched) if sched.suffix != ".csv" else None
        report = compare_referee(
            res,
            fls
            if fls is not None
            else pa.table(
                {
                    "shadow_id": pa.array([], pa.utf8()),
                    "fill_qty": pa.array([], pa.uint64()),
                    "ts_event": pa.array([], pa.int64()),
                }
            ),
            str(sim),
            s.tolerance,
            schedule=sched_t if sched_t is not None and "order_key" in sched_t.column_names else None,
            spec=spec_echo(s.model_dump(mode="json")),
            simulator=s.simulator,
        )
    if s.out:
        write_json(report.model_dump(mode="json"), Path(s.out) / "referee_report.json")
    return report
