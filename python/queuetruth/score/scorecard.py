"""`build_scorecard` (SPEC 5.7): live log -> fixed schedule -> simulator arm -> per-order table -> statistics ->
`qt.score_*` tables and `qt.scorecard_summary`.

Spec (schema 1.1, SPEC 3.11 #8/#21): `window_ns` (evaluation horizon H, ruling A9), `match_window_ns` (twin
matching window), `self_exclude` (ruling A1) and `markout_past_end` are read from the `ScorecardSpec`; the keyword
arguments of the same names override them and are folded into the effective spec (re-validated) before `run_id`
and the summary echo are computed, so both reflect what ran (OC-34).

Arms: `exact` replays the schedule with the exact engine; `zoo` runs the named model through the zoo runner on the
dataset's L2 view, with the schedule's priority-keeping amends (`ZooSpec.amends`); `external` reads a canonical log
written by the user's simulator, matched by `order_key`. With an MBO dataset the exact replay always runs (queue
position at entry, BBO and trades for markouts); with an L2-only dataset BBO/trades come from `bbo_from_l2` and the
visible level qty at entry from a `front_of_queue` zoo pass when the arm is not a zoo model.

Units (review F4, `queuetruth.score.units`): live (and external-simulator) prices and quantities are converted to
the dataset's `price_exp`/`size_exp` before scheduling, so queues, quantities, `queue_ahead_at_entry` and currency
markouts share one unit system; a value that is not exactly representable is `QT-INPUT-004`. The live exponents come
from `queuetruth.live.instrument_scales`; an instrument whose scale changes within the log is refused
(`QT-INPUT-002`).

Schedule (SPEC 5.7.2): a runner with `build_schedule` (the default) returns the live package's schedule with its
accounting (`queuetruth.live.schedule_report`): the exclusion counts per reason feed the `not_scheduled:` warning and
the "NOT SCORED" error, the entry-fallback share and the other schedule notes become `schedule:` warnings. Same-price
size decreases keep queue priority on the venues of `keep_priority_venues` (the live package's documented venues
plus CME, audit A6; every live venue label when the dataset is a CME tape), so they become
priority-keeping amends, not new legs. A runner with only `to_schedule` (test fakes) gets no venue set and the
scorer infers the exclusion reasons from the log (`not_scheduled_reasons`).

Self-exclusion (ruling A1, MBO datasets, `self_exclude`): live orders are matched to their MBO twins
(`selfexclude.match_twins`, window `match_window_ns`); the exact replay gets `exclude_order_ids` natively (the
engine links each twin to the shadow entering at its record: the twin stays on the tape for every other order, its
shadow takes its queue place and is filled by each of its executions; identical legs are put in their twins' tape
order, `selfexclude.order_twin_ties`) and so does the zoo arm (`ZooSpec.exclude_order_ids`
on the original dataset); legs entered exactly at their twin's `A`/`M` timestamp activate 1 ns earlier when that
record opens its timestamp (`selfexclude.align_to_twins`), so a same-nanosecond sweep is not missed. Only the exact
arm of a runner without native replay options falls back to a filtered dataset copy (lossy, warned). L2-only
datasets have no order ids: the queue models (zoo arm, visible-queue pass) run on a copy of the dataset without the
user's own resting orders in the visible depth (trader review E2, `queuetruth.score.l2_own`, aligned to the feed
within `match_window_ns`; markouts keep the source quotes). When that removal is not applied (`self_exclude` false,
or the removal failed) the scorecard is blocked: failed gate `own_orders_in_l2_depth` (CLI exit 1), a `BLOCKED:`
headline prefix and a warning. Simulator logs of the external arm are recognised by the `sim:` venue
prefix (E38, OC-9).

`n_orders` counts the orders of the live log; scored orders are the scheduled ones with a simulator outcome
(`n_matched`); `n_unmatched_live` = scheduled orders without a sim outcome (external log missing the key, or every
leg ended `censored_gap` without activating); `n_unmatched_sim` = sim-log orders that are not scheduled. A scorecard
with no scored order is an error (`QT-INPUT-002`, "NOT SCORED: ...", with the count per reason; review F1). Each
order's `ts_entry` is its first leg's `ts_submit`; `session_date` is the instrument-day of `ts_entry`; CIF events use
cause 1 fill, cause 2 cancel/expire (live `cancel_ack`/`expire` time, exact `ts_cancel_effective`), else censoring
at the instrument-day's last event (or the latest live-log time without a dataset).

Summary 1.1 fields (SPEC 3.11 #9): `discordance_clustered` (statistics), `n_twin_unmatched` / `twin_match_rate`
(when twin matching ran), `n_taker_fills` per arm and `fills_only`; `qt.score_fills` is written in the 1.1 layout
(`side`, `instrument_id`). Markouts in bps and net of fees (review F10) are `qt.score_tests` rows
(`markout_mean_bps`, `markout_mean_net_ccy`); sim fills are charged the live log's fee rate per liquidity.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
from pydantic import ValidationError

from queuetruth.errors import QtError
from queuetruth.schemas.arrow import ENGINE_VERSION
from queuetruth.schemas.io import write_table
from queuetruth.schemas.models import DataQuality, ScorecardSummary
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.schemas.specs import PathRef, ReplaySpec, ScorecardSpec, ZooSpec
from queuetruth.score._dataset import DatasetView
from queuetruth.score._io import (
    Row,
    config_error,
    make_table,
    read_live_log,
    ref_path,
    resolve_ref,
    rows,
    sim_label,
    write,
    write_json,
)
from queuetruth.score.coverage import is_fills_only
from queuetruth.score.l2_own import own_free_l2_dataset, own_orders_from_log
from queuetruth.score.markouts import apply_fee_rates, compute_markouts, position_series
from queuetruth.score.outcomes import Outcome, exact_outcomes, log_outcomes, zoo_outcomes
from queuetruth.score.runner import DefaultRunner, ScoreRunner, find_table
from queuetruth.score.schedule import (
    cancel_instruction,
    in_window,
    instruction_legs,
    legs_by_order,
    window_end,
)
from queuetruth.score.selfexclude import (
    align_to_twins,
    filtered_dataset,
    match_twins,
    order_twin_ties,
    twin_display_prices,
    twin_entry_points,
)
from queuetruth.score.shortfall import score_shortfall
from queuetruth.score.statistics import assemble, assign_strata, classify
from queuetruth.score.units import UnitMap, dataset_exps, live_scales

SIXTY_S = 60_000_000_000
DAY_NS = 86_400_000_000_000
DEFAULT_H_MAX = 300_000_000_000
BUY = ord("B")


def _schedule_rows(t: pa.Table | None) -> list[Row]:
    return rows(t) if t is not None else []


class _TradeVol:
    """Displayed traded volume in `[t - 60 s, t)` per instrument from a `qt.trades` table."""

    def __init__(self, trades: pa.Table | None) -> None:
        self._ts: dict[int, list[int]] = {}
        self._cum: dict[int, list[int]] = {}
        acc: dict[int, list[tuple[int, int]]] = {}
        for r in rows(trades) if trades is not None else []:
            if r.get("kind", "displayed") != "displayed":
                continue
            acc.setdefault(int(r["instrument_id"]), []).append((int(r["ts_event"]), int(r["size"])))
        for iid, v in acc.items():
            v.sort()
            self._ts[iid] = [x[0] for x in v]
            c, run = [0], 0
            for _, s in v:
                run += s
                c.append(run)
            self._cum[iid] = c

    def at(self, iid: int | None, t: int) -> int | None:
        if iid is None or iid not in self._ts:
            return None if not self._ts else 0
        ts, cum = self._ts[iid], self._cum[iid]
        return cum[bisect.bisect_left(ts, t)] - cum[bisect.bisect_left(ts, t - SIXTY_S)]


def _overlaps(orders: list[Row], live: dict[str, Outcome], entry: dict[str, int]) -> dict[str, bool]:
    groups: dict[tuple[Any, ...], list[str]] = {}
    for o in orders:
        k = str(o["order_key"])
        if k in entry:
            key = (o.get("account"), o.get("instrument"), o.get("side"), o.get("limit_px"))
            groups.setdefault(key, []).append(k)
    out = {}
    for ks in groups.values():
        for k in ks:
            t = entry[k]
            out[k] = any(
                j != k and entry[j] <= t and (live[j].ts_end is None or live[j].ts_end > t) for j in ks if j in live
            )
    return out


def _latency_nonzero(lat: Any) -> bool:
    return lat is not None and any(int(v) for v in lat.model_dump().values())


def _replay_spec(
    ds_root: Path,
    out: Path,
    sched: Path,
    amends: Path | None,
    latency: dict[str, int] | None,
    seed: int,
    race_window_ns: int = 0,
) -> ReplaySpec:
    """ReplaySpec for the exact arm (options without a spec field go to `ScoreRunner.replay` as keywords).
    `race_window_ns` sets the shadows' `ambiguous_race` flag (audit minor 15; the scorer passes `match_window_ns`)."""
    doc: dict[str, Any] = {
        "dataset": {"path": str(ds_root)},
        "out": str(out),
        "shadows": {"path": str(sched)},
        "amends": None if amends is None else {"path": str(amends)},
        "latency": latency or {},
        "bbo": "change",
        "trajectory": "none",
        "seed": seed,
        "race_window_ns": int(race_window_ns),
    }
    return ReplaySpec.model_validate(doc)


def exclusion_items(
    ids: dict[int, set[int]], days: dict[tuple[int, int], set[int]] | None = None
) -> list[dict[str, Any]]:
    """`exclude_order_ids` items of matched twins, sorted: `{instrument_id, order_id, session_date}` per
    instrument-day (`TwinMatch.days`; review F7: an id is excluded on its own session date only), else
    `{instrument_id, order_id}` (no known date)."""
    if days is None:
        return [{"instrument_id": i, "order_id": o} for i in sorted(ids) for o in sorted(ids[i])]
    out: list[dict[str, Any]] = []
    for (i, d), os_ in sorted(days.items()):
        for o in sorted(os_):
            item: dict[str, Any] = {"instrument_id": i, "order_id": o}
            if d:
                item["session_date"] = (_dt.date(1970, 1, 1) + _dt.timedelta(days=int(d))).isoformat()
            out.append(item)
    return out


def run_exact(
    rn: ScoreRunner,
    rs: ReplaySpec,
    exclude: list[dict[str, int]],
    fallback_root: Any,
) -> tuple[Any, str]:
    """Runs the exact arm with self-exclusion (ruling A1): natively through the `exclude_order_ids` replay option of a
    runner with `native_replay_options` (the engine credits the twin's executions to the shadow in its place),
    otherwise on a filtered copy of the dataset (`fallback_root()`; lossy: the twin's executed volume is gone).
    Returns (replay result, mode: none | native | copy)."""
    if not exclude:
        return rn.replay(rs), "none"
    if getattr(rn, "native_replay_options", False):
        return rn.replay(rs, exclude_order_ids=exclude), "native"  # type: ignore[call-arg]
    copy = rs.model_copy(update={"dataset": rs.dataset.model_copy(update={"path": str(fallback_root())})})
    return rn.replay(copy), "copy"


#: Resting time-in-force values that `queuetruth.live.to_schedule` schedules (mirrors `live.canonical.RESTING_TIF`).
RESTING_TIF = ("gtc", "day", "post_only", "gtx", "gtd")
#: Readable text of the `to_schedule` exclusion reasons (review F1).
REASON_TEXT = {
    "no_instrument_id": "missing instrument_id: pass --instrument-map",
    "no_limit_px": "no limit price",
    "rejected": "rejected before an ack",
    "no_entry_time": "no usable entry time (no match/gateway-time ack, no submit_sent)",
    "no_sim_outcome": "no simulator outcome (no leg activated in the simulator: feed gap or outside the data)",
    "sim_hbt_312_abort": "simulator pair aborted at the faithful hbt #312 InvalidOrderStatus event within the window",
    "missing_in_sim_log": "missing from the simulator log",
}


def reason_text(reason: str) -> str:
    """Readable text of a not-scored reason."""
    if reason in REASON_TEXT:
        return REASON_TEXT[reason]
    if reason.startswith("order_type_"):
        return f"{reason[len('order_type_') :]} orders (only resting limit orders are scheduled)"
    if reason.startswith("tif_"):
        return f"{reason[len('tif_') :].upper()} time in force (not resting)"
    return reason


def not_scheduled_reasons(orders: list[Row], events: list[Row], scheduled: set[str]) -> dict[str, int]:
    """Count per reason of live orders absent from the schedule, with the reasons of `queuetruth.live.to_schedule`
    (`order_type_<t>`, `tif_<t>`, `no_limit_px`, `no_instrument_id`, `rejected`, else `no_entry_time`)."""
    types: dict[str, set[str]] = {}
    for e in events:
        types.setdefault(str(e["order_key"]), set()).add(str(e["event_type"]))
    out: dict[str, int] = {}
    for o in orders:
        key = str(o["order_key"])
        if key in scheduled:
            continue
        ot, tif = str(o.get("order_type") or "limit"), str(o.get("tif") or "gtc")
        ts = types.get(key, set())
        if ot != "limit":
            why = f"order_type_{ot}"
        elif tif not in RESTING_TIF:
            why = f"tif_{tif}"
        elif o.get("limit_px") is None:
            why = "no_limit_px"
        elif o.get("instrument_id") is None:
            why = "no_instrument_id"
        elif "ack" not in ts and ts & {"reject", "post_only_reject"}:
            why = "rejected"
        else:
            why = "no_entry_time"
        out[why] = out.get(why, 0) + 1
    return out


#: Venue labels of CME (a same-price size decrease keeps priority on CME, audit A6).
CME_LABELS = frozenset({"cme", "xcme", "glbx", "globex", "cme_globex"})


def keep_priority_venues(live_orders: list[Row], dataset_venue: str | None) -> frozenset[str]:
    """Venues whose same-price size decrease keeps queue priority in the scorer's schedule: the live package's
    `KEEP_PRIORITY_VENUES`, the CME labels among the live venues, and every live venue label when the dataset is a
    CME tape (the replay applies CME rules to all of them)."""
    from queuetruth.live import KEEP_PRIORITY_VENUES

    venues = {str(o.get("venue")) for o in live_orders if o.get("venue")}
    keep = set(KEEP_PRIORITY_VENUES) | {v for v in venues if v.lower() in CME_LABELS}
    if dataset_venue is not None and dataset_venue.lower() in CME_LABELS:
        keep |= venues
    return frozenset(keep)


def schedule_with_report(
    rn: ScoreRunner, live_dir: Path, entry: str, latency_ns: int | None, keep: frozenset[str]
) -> tuple[pa.Table, pa.Table, dict[str, Any] | None]:
    """(`qt.shadow_spec`, `qt.shadow_amend`, `queuetruth.live.schedule_report` or None): through the runner's
    `build_schedule` when it has one, else `to_schedule` (no report, no keep-priority venues)."""
    build = getattr(rn, "build_schedule", None)
    if callable(build):
        from queuetruth.live import schedule_report

        sch = build(str(live_dir), entry=entry, latency_ns=latency_ns, keep_priority_venues=keep)
        return sch.spec, sch.amends, schedule_report(sch)
    sched_t, amend_t = rn.to_schedule(str(live_dir), entry=entry, latency_ns=latency_ns)
    return sched_t, amend_t, None


def report_reasons(report: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
    """(order exclusion counts per reason, `schedule:` warnings) of a `schedule_report`: `amend_without_time` counts
    ignored amends of scheduled orders, so it becomes a warning, not an exclusion; the per-reason `excluded ...`
    lines are left to the `not_scheduled:` warning."""
    excluded = {str(k): int(v) for k, v in (report.get("excluded") or {}).items()}
    notes = [f"schedule: {w}" for w in report.get("warnings") or [] if not str(w).startswith("excluded ")]
    n_amend = excluded.pop("amend_without_time", 0)
    if n_amend:
        notes.append(f"schedule: {n_amend} amend(s) without a usable time ignored")
    return excluded, notes


def _breakdown(counts: dict[str, int]) -> str:
    return "; ".join(f"{n} {reason_text(r)}" for r, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def not_scored_error(n_orders: int, reasons: dict[str, int], live_dir: Path, sim: str | None = None) -> QtError:
    """`QT-INPUT-002` "NOT SCORED: 0 of N live orders scored (<count> <reason>; ...) [simulator <model>]" (review F1;
    the simulator named so a failing model is visible)."""
    if n_orders == 0:
        msg = f"NOT SCORED: the live log {live_dir} has no orders"
    else:
        msg = f"NOT SCORED: 0 of {n_orders} live orders scored ({_breakdown(reasons) or 'no reason recorded'})"
        if sim:
            msg += f" [simulator {sim}]"
    return QtError(
        "QT-INPUT-002",
        msg,
        "check the live log (queuetruth live validate): instrument mapping, order types and entry times",
    )


def effective_spec(
    spec: ScorecardSpec | dict[str, Any],
    *,
    window_ns: int | None = None,
    self_exclude: bool | None = None,
    match_window_ns: int | None = None,
) -> ScorecardSpec:
    """The spec `build_scorecard` runs: `spec` with the keyword overrides that are not None folded in and
    re-validated (ranges checked; an invalid override is `QT-CONFIG-001` naming the field)."""
    s = ScorecardSpec.model_validate(spec) if isinstance(spec, dict) else spec
    over = {
        k: v
        for k, v in (("window_ns", window_ns), ("self_exclude", self_exclude), ("match_window_ns", match_window_ns))
        if v is not None
    }
    if not over:
        return s
    try:
        return ScorecardSpec.model_validate({**s.model_dump(), **over})
    except ValidationError as e:
        bad = ", ".join(sorted({str(err["loc"][0]) for err in e.errors() if err.get("loc")})) or "spec"
        raise config_error(f"score: invalid {bad}: {e.errors()[0].get('msg', e)}") from e


OWN_ORDERS_GATE = "own_orders_in_l2_depth"


def own_free_l2(
    ds_root: Path, live_orders: list[Row], live_events: list[Row], spec: ScorecardSpec, work: Path
) -> tuple[Path, str | None, list[str]]:
    """L2-only datasets (trader review E2): the dataset copy without the user's own resting orders in the visible
    depth (`l2_own.own_free_l2_dataset`, alignment window `match_window_ns`), for the queue models of the zoo arm
    and the visible-queue pass; markouts keep the source dataset. The live rows are already in dataset units.
    Returns (root for the queue models, why removal was not applied or None, warnings)."""
    if not spec.self_exclude:
        return ds_root, "self_exclude is false", []
    own, notes = own_orders_from_log(live_orders, live_events)
    if not own:
        return ds_root, None, notes
    try:
        res = own_free_l2_dataset(ds_root, work / "dataset_own_free", own, match_window_ns=spec.match_window_ns)
    except QtError as e:
        return ds_root, f"removal failed: {e.code}: {e}", notes
    st = res.stats
    msg = (
        f"self_exclusion: own resting orders removed from the L2 depth ({st.orders} orders; "
        f"{st.changes_aligned}/{st.changes} changes aligned to feed rows within {spec.match_window_ns} ns, "
        f"{st.changes_unaligned} unaligned; {st.rows_adjusted} rows adjusted)"
    )
    return res.root, None, [*notes, msg, *res.warnings]


def block_own_orders(parts: dict[str, Any], why: str) -> str:
    """Blocks a scorecard whose zoo arm ran on L2 depth that still holds the user's own orders: a failed gate
    `own_orders_in_l2_depth` (CLI exit 1) and a headline prefix. Returns the warning text."""
    v = parts["verdict"]
    v["gates_passed"] = False
    v["failed_gates"] = [*v["failed_gates"], OWN_ORDERS_GATE]
    v["headline"] = "BLOCKED: own resting orders are in the L2 depth; " + v["headline"]
    return (
        f"{OWN_ORDERS_GATE}: BLOCKING: the user's own resting orders were not removed from the visible L2 depth "
        f"({why}); the twin shadows queue behind their own orders and the arm underfills. Set self_exclude = true "
        "(the default) to remove them"
    )


#: Orders whose zoo-model trajectory a scorecard writes (`ReportSpec.max_why` default).
TRACED_ORDERS = 20


def traced_order_keys(order_rows: list[Row], k: int = TRACED_ORDERS) -> list[str]:
    """The orders the report's scorecard why traces pick (SPEC 5.9, `queuetruth.report.why`): sim-only, then
    live-only (by entry time), then jointly filled by the largest |time-to-fill difference|; the first `k`."""

    def gap(r: Row) -> int:
        a, b = r.get("ttf_live_ns"), r.get("ttf_sim_ns")
        return abs(int(a) - int(b)) if a is not None and b is not None else -1

    def ent(r: Row) -> tuple[int, str]:
        return int(r["ts_entry"]), str(r["order_key"])

    sel = sorted((r for r in order_rows if r["class"] == "sim_only"), key=ent)
    sel += sorted((r for r in order_rows if r["class"] == "live_only"), key=ent)
    sel += sorted(
        (r for r in order_rows if r["class"] == "both" and gap(r) > 0), key=lambda r: (-gap(r), str(r["order_key"]))
    )
    return [str(r["order_key"]) for r in sel[:k]]


def traced_trajectory(
    rn: ScoreRunner, zs: ZooSpec, order_rows: list[Row], schedule: list[Row], amends: list[Row], out: Path
) -> dict[str, Any] | None:
    """The zoo model's queue estimate next to the exact queue (`qt.zoo_trajectory`) for the orders
    the report traces (`traced_order_keys`), so model-aware why traces need no rerun: a second zoo pass on their
    schedule legs (and amends) with `write_trajectory`, written to `<out>/zoo_trajectory.parquet` (MBO datasets;
    shadows are independent, so the rows equal those of the scored run). Returns its `Files` entry or None."""
    keys = set(traced_order_keys(order_rows))
    legs = [r for r in schedule if str(r.get("order_key") or r["shadow_id"]) in keys]
    if not legs:
        return None
    ids = {str(r["shadow_id"]) for r in legs}
    with tempfile.TemporaryDirectory(prefix="qt-score-traced-") as tmp:
        work = Path(tmp)
        write_table(make_table("qt.shadow_spec", legs), work / "schedule.parquet")
        am = [a for a in amends if str(a["shadow_id"]) in ids]
        if am:
            write_table(make_table("qt.shadow_amend", am), work / "amends.parquet")
        sub = zs.model_copy(
            update={
                "shadows": PathRef(path=str(work / "schedule.parquet")),
                "amends": PathRef(path=str(work / "amends.parquet")) if am else None,
                "out": str(work / "zoo"),
                "write_trajectory": True,
                "write_fills": False,
            }
        )
        rn.run_zoo(sub)
        t = find_table(work / "zoo", "qt.zoo_trajectory")
        if t is None:
            return None
        return write(t, out / "zoo_trajectory.parquet")


def build_scorecard(
    spec: ScorecardSpec | dict[str, Any],
    *,
    runner: ScoreRunner | None = None,
    window_ns: int | None = None,
    self_exclude: bool | None = None,
    match_window_ns: int | None = None,
) -> ScorecardSummary:
    """`score`: runs the arm, writes `score_*.parquet` + `summary.json` to `spec.out` (when set), returns the
    summary. Gate failures are reported in `verdict` (the CLI maps them to exit code 1); a scorecard that scores no
    order raises `QT-INPUT-002` ("NOT SCORED: ..."). `window_ns`, `self_exclude` and `match_window_ns` override the
    spec fields of the same names (None = the spec's value) and enter the summary echo and `run_id`."""
    s = effective_spec(spec, window_ns=window_ns, self_exclude=self_exclude, match_window_ns=match_window_ns)
    rn: ScoreRunner = runner or DefaultRunner()
    if s.entry == "exchange_ack" and _latency_nonzero(s.latency):
        raise config_error(
            "score: latency must be zero (or null) with entry = exchange_ack (exchange times already include it)",
            "use --entry send-plus-latency",
        )
    live_dir = ref_path(s.live, "live")
    warnings: list[str] = [f"live: {w}" for w in rn.validate(str(live_dir))]
    live_orders, live_events = read_live_log(live_dir)
    fills_only = is_fills_only(live_dir, live_orders, live_events)
    sim_kind = s.sim.kind
    ds = DatasetView(ref_path(s.dataset, "dataset")) if s.dataset is not None else None
    if sim_kind in ("exact", "zoo") and ds is None:
        raise config_error(f"score: sim {sim_kind} needs a dataset", "--data DIR")
    if sim_kind == "exact" and ds is not None and ds.kind == "l2":
        raise config_error("score: sim exact needs an MBO dataset (exact replay needs MBO)")

    # ---- units: live prices / quantities in the dataset's fixed-point units (review F4) -----------------------
    raw_live_exps = live_scales(live_dir, live_orders)
    units = UnitMap(dataset_exps(rows(ds.instruments)) if ds is not None and ds.instruments is not None else {})
    units.add_orders(live_orders, source=raw_live_exps)
    keep = keep_priority_venues(live_orders, None if ds is None else str(ds.manifest.get("venue") or "") or None)
    live_orders, live_events = units.orders(live_orders), units.events(live_events)
    warnings += units.notes("live log")

    use_lat = s.entry == "send_plus_latency" and s.latency is not None
    sched_t, amend_t, sched_report = schedule_with_report(rn, live_dir, s.entry, 0 if use_lat else None, keep)
    raw_schedule = _schedule_rows(sched_t)
    raw_amends = units.amends(_schedule_rows(amend_t), raw_schedule)
    raw_schedule = units.schedule(raw_schedule)
    latency = s.latency.model_dump() if use_lat and s.latency is not None else None

    live_oc, w = log_outcomes(live_orders, live_events)
    warnings += [f"live: {x}" for x in w]
    schedule, amends, leg_totals = instruction_legs(raw_schedule, raw_amends, live_events, live_oc, s.entry)
    order_info = {str(o["order_key"]): o for o in live_orders}
    ev_by: dict[str, list[Row]] = {}
    for e in live_events:
        ev_by.setdefault(str(e["order_key"]), []).append(e)
    legs = legs_by_order(schedule)
    order_qty = {k: (live_oc[k].order_qty if k in live_oc else max(int(x["qty"]) for x in v)) for k, v in legs.items()}
    if sched_report is not None:
        reasons, sched_notes = report_reasons(sched_report)
        warnings += sched_notes
    else:
        reasons = not_scheduled_reasons(live_orders, live_events, set(legs))
    n_excluded = sum(reasons.values())
    if n_excluded:
        warnings.append(f"not_scheduled: {n_excluded} live order(s) not schedulable ({_breakdown(reasons)})")

    sim_oc: dict[str, Outcome] = {}
    exact_first: dict[str, Row] = {}
    zoo_first: dict[str, Row] = {}
    sim_model_id = {"exact": "exact", "zoo": getattr(s.sim, "model", ""), "external": "external"}[sim_kind]
    zoo_aborted: set[str] = set()
    bbo = trades = None
    zoo_arm_spec: ZooSpec | None = None
    own_block: str | None = None
    n_unmatched_sim = 0
    with tempfile.TemporaryDirectory(prefix="qt-score-") as tmp:
        work = Path(tmp)
        from queuetruth.eval import impact as qt_impact  # lazy: `python -m queuetruth.eval.impact` imports score

        sched_path = amend_path = None
        if schedule:
            sched_path = work / "schedule.parquet"
            write_table(make_table("qt.shadow_spec", schedule), sched_path)
            if amends:
                amend_path = work / "amends.parquet"
                write_table(make_table("qt.shadow_amend", amends), amend_path)
        ex_oc: dict[str, Outcome] = {}
        raced: set[str] = set()
        twins = None
        filtered_root: Path | None = None
        if ds is not None and ds.kind != "l2" and sched_path is not None and s.self_exclude:
            twins = match_twins(
                ds.root,
                [
                    {
                        "order_key": k,
                        "instrument_id": int(v[0]["instrument_id"]),
                        "side": int(v[0]["side"]),
                        "price": int(v[0].get("price") or 0),
                        "qty": int(v[0]["qty"]),
                        "ts_entry": int(v[0]["ts_submit"]),
                        "venue_order_id": order_info.get(k, {}).get("venue_order_id"),
                        "session_date": ds.day(int(v[0]["instrument_id"]), int(v[0]["ts_submit"]))[0],
                    }
                    for k, v in legs.items()
                ],
                s.match_window_ns,
            )
            warnings.append(twins.summary())
            collide = twins.collision_warning()
            if collide is not None:
                warnings.append(collide)
            if twins.rate is not None and twins.rate < 0.95:
                warnings.append("self_exclusion: match rate below 95%; the arm may sit behind own orders")
            eng_sched, n_slid = twin_display_prices(schedule, twins)
            n_moved = n_ties = 0
            if s.entry == "exchange_ack" and twins.ids:
                eng_sched, n_moved = align_to_twins(eng_sched, twins, twin_entry_points(ds.root, twins.ids))
                eng_sched, n_ties = order_twin_ties(ds.root, eng_sched, twins)
            if n_slid or n_moved or n_ties:
                write_table(make_table("qt.shadow_spec", eng_sched), sched_path)
            if n_slid:
                warnings.append(
                    f"self_exclusion: {n_slid} order(s) matched by venue order id to a twin displayed at another price "
                    "(display-slid); their legs rest at the displayed price"
                )
            if n_moved:
                warnings.append(f"self_exclusion: {n_moved} leg(s) activated in their twin's queue place")
            if n_ties:
                warnings.append(
                    f"self_exclusion: {n_ties} identical leg(s) entered in the same nanosecond ordered by their "
                    "twins' tape order"
                )
        excl = exclusion_items(twins.ids, twins.days) if twins is not None else []

        def own_free_root() -> Path:
            """Filtered dataset copy for the exact arm of a runner without native replay options (fallback)."""
            nonlocal filtered_root
            if twins is None or not twins.all_ids() or ds is None:
                return ds.root if ds is not None else Path(".")
            if filtered_root is None:
                filtered_root = filtered_dataset(ds.root, work / "dataset_self_excluded", twins.ids, by_day=twins.days)
            return filtered_root

        if ds is not None and ds.kind != "l2" and sched_path is not None:
            rs = _replay_spec(
                ds.root, work / "exact", sched_path, amend_path, latency, s.bootstrap.seed, s.match_window_ns
            )
            rep, mode = run_exact(rn, rs, excl, own_free_root)
            if mode != "none":
                warnings.append(
                    "self_exclusion: applied via the replay option exclude_order_ids"
                    if mode == "native"
                    else "self_exclusion: applied via a filtered dataset copy (fallback; the twins' executions are "
                    "lost under the exact fill rule, so the arm may underfill)"
                )
            warnings += [f"replay: {w}" for w in getattr(rep, "warnings", None) or [] if "exclude_order_ids" in w]
            res = find_table(work / "exact", "qt.shadow_results")
            fls = find_table(work / "exact", "qt.shadow_fills")
            leg_key = {str(r["shadow_id"]): str(r.get("order_key") or r["shadow_id"]) for r in schedule}
            for r in rows(res) if res is not None else []:
                if r.get("ambiguous_race"):
                    raced.add(leg_key.get(str(r["shadow_id"]), str(r["shadow_id"])))
            ex_oc, exact_first = exact_outcomes(
                schedule,
                rows(res) if res is not None else [],
                rows(fls) if fls is not None else [],
                order_qty,
                leg_totals,
            )
            bbo = find_table(work / "exact", "qt.bbo")
            trades = find_table(work / "exact", "qt.trades")
            tw = set(twins.by_key) if twins is not None and mode == "native" else set()
            warnings += qt_impact.score_warnings(res, trades, ds.instruments, schedule=schedule, twin_keys=tw)
            if sim_kind == "exact":
                sim_oc = ex_oc
        if ds is not None and ds.kind == "l2":
            rn.bbo_from_l2(str(ds.root), str(work / "l2bbo"))
            bbo = find_table(work / "l2bbo", "qt.bbo")
            trades = find_table(work / "l2bbo", "qt.trades")
        zoo_root = ds.root if ds is not None else None
        if ds is not None and ds.kind == "l2" and sched_path is not None:
            zoo_root, own_block, w = own_free_l2(ds.root, live_orders, live_events, s, work)
            warnings += w
        if ds is not None and sched_path is not None and (sim_kind == "zoo" or ds.kind == "l2"):
            model = s.sim.model if sim_kind == "zoo" else "front_of_queue"  # type: ignore[union-attr]
            l2 = s.sim.l2.model_dump() if sim_kind == "zoo" else {}  # type: ignore[union-attr]
            zoo_excl = excl if ds.kind != "l2" else []
            zs = ZooSpec.model_validate(
                {
                    "dataset": {"path": str(zoo_root)},
                    "shadows": {"path": str(sched_path)},
                    "out": str(work / "zoo"),
                    "models": [model],
                    "horizons_ns": sorted({int(h) for h in s.ttf_horizons_ns}),
                    "h_max_ns": max(DEFAULT_H_MAX, int(s.window_ns)),
                    "l2": l2,
                    "latency": latency or {},
                    "write_fills": True,
                    "seed": s.bootstrap.seed,
                    "bootstrap_b": 1,
                    "amends": None if amend_path is None else {"path": str(amend_path)},
                    "exclude_order_ids": zoo_excl,
                }
            )
            with qt_impact.suppressed():
                rn.run_zoo(zs)
            if sim_kind == "zoo":
                zoo_arm_spec = zs
            if zoo_excl and sim_kind == "zoo":
                warnings.append("self_exclusion: zoo arm applies exclude_order_ids natively")
            pr = find_table(work / "zoo", "qt.zoo_predictions")
            zf = find_table(work / "zoo", "qt.zoo_fills")
            if ds.kind == "l2":
                zsh = find_table(work / "zoo", "qt.zoo_shadows")
                warnings += qt_impact.score_zoo_warnings(
                    pr, zsh, trades, ds.instruments, h_max_ns=zs.h_max_ns, schedule=schedule
                )
            pr_rows = rows(pr) if pr is not None else []
            mids = sorted({str(r["model_id"]) for r in pr_rows})
            mid = mids[0] if mids else model
            z_oc, zoo_first = zoo_outcomes(
                mid, schedule, pr_rows, rows(zf) if zf is not None else [], order_qty, ex_oc, leg_totals
            )
            if sim_kind == "zoo":
                sim_oc, sim_model_id = z_oc, mid
                # faithful hbt #312: a pair aborted within the window has no simulator outcome there
                aborted_sids = {
                    str(r["shadow_id"])
                    for r in pr_rows
                    if r.get("status") is not None and int(r["horizon_ns"]) <= int(s.window_ns)
                }
                zoo_aborted = {k for k, ls in legs.items() if any(str(x["shadow_id"]) in aborted_sids for x in ls)}
        if sim_kind == "external":
            sim_dir = ref_path(s.sim.log, "sim.log")  # type: ignore[union-attr]
            so, se = read_live_log(sim_dir)
            sim_units = UnitMap(units.target or raw_live_exps)
            sim_units.add_orders(
                so,
                {
                    str(o["order_key"]): int(o["instrument_id"])
                    for o in live_orders
                    if o.get("instrument_id") is not None
                },
            )
            so, se = sim_units.orders(so), sim_units.events(se)
            warnings += sim_units.notes("sim log")
            sim_oc, w = log_outcomes(so, se)
            warnings += [f"sim: {x}" for x in w]
            n_unmatched_sim = len([k for k in sim_oc if k not in legs])
            label = sim_label(so, se)
            if label is not None:
                sim_model_id = label
            else:
                warnings.append("sim: log has no venue prefix sim: (not written by import_sim?); scored as given")

    # ---- per-order table (evaluation window W, maker fills only; ruling A5 / A9) ------------------------------
    def exps_of(iid: int | None) -> dict[str, int | None]:
        """1.1 `price_exp` / `size_exp` of the unit system of prices and quantities (the dataset's, else the live
        log's) for an instrument."""
        e = None if iid is None else (units.target.get(int(iid)) or raw_live_exps.get(int(iid)))
        return {"price_exp": None if e is None else int(e[0]), "size_exp": None if e is None else int(e[1])}

    tv = _TradeVol(trades)
    entry = {k: int(v[0]["ts_submit"]) for k, v in legs.items()}
    overlap = _overlaps(live_orders, live_oc, entry)
    live_end_ts = max([int(e[k]) for e in live_events for k in ("ts_exchange", "ts_local_recv") if e.get(k)] + [0])
    order_rows: list[Row] = []
    fills_ext: list[Row] = []
    unpriced: dict[tuple[str, str], int] = {}  # (order_key, arm) -> unpriced fill qty (qt.score_shortfall)
    taker: set[tuple[str, str, int]] = set()
    n_unmatched_live = 0
    for key in sorted(legs):
        so_ = sim_oc.get(key)
        if key in zoo_aborted:
            n_unmatched_live += 1
            reasons["sim_hbt_312_abort"] = reasons.get("sim_hbt_312_abort", 0) + 1
            continue
        if so_ is None or not so_.matched:
            n_unmatched_live += 1
            why = "missing_in_sim_log" if sim_kind == "external" else "no_sim_outcome"
            reasons[why] = reasons.get(why, 0) + 1
            continue
        lo_ = live_oc.get(key) or Outcome(order_key=key, order_qty=order_qty[key])
        first = legs[key][0]
        iid = int(first["instrument_id"])
        t0 = entry[key]
        if ds is not None:
            sdate, midnight = ds.day(iid, t0)
        else:
            sdate = t0 // DAY_NS
            midnight = sdate * DAY_NS
        eod = (ds.end_of_data(iid, sdate) if ds is not None else None) or (live_end_ts or None)
        w_end, cut = window_end(t0, cancel_instruction(ev_by.get(key, [])), eod, s.window_ns)
        side = "buy" if int(first["side"]) == BUY else "sell"
        qa = src = None
        if key in exact_first:
            qa, src = exact_first[key].get("queue_ahead_at_live"), "exact"
        elif key in zoo_first:
            zr = zoo_first[key]
            if ds is not None and ds.kind == "l2":
                qa, src = zr.get("visible_level_qty_at_live"), "l2_visible"
            else:
                qa, src = zr.get("queue_ahead_at_live"), "exact"
        tv60 = zoo_first[key].get("trade_vol_60s") if key in zoo_first else tv.at(iid, t0)
        arm_vals: dict[str, tuple[int, int | None, int, int]] = {}
        for arm, oc in (("live", lo_), ("sim", so_)):
            maker, tk = in_window(oc, t0, w_end)
            qty = sum(f.qty for f in maker)
            ff = min((f.ts for f in maker), default=None)
            if ff is not None:
                ev, cause = ff - t0, 1
            elif tk:
                ev, cause = min(f.ts for f in tk) - t0, 2
            elif cut == "cancel":
                ev, cause = w_end - t0, 2
            else:
                ev, cause = w_end - t0, 0
            arm_vals[arm] = (qty, ff, ev, cause)
            seq = 0
            for f in sorted(maker + tk, key=lambda f: f.ts):
                if f.px is None:  # quantity counted above; no price for score_fills / markouts (review F13)
                    unpriced[(key, arm)] = unpriced.get((key, arm), 0) + f.qty
                    continue
                seq += 1
                liq = "taker" if f.liquidity == "taker" else (f.liquidity if f.liquidity == "maker" else "unknown")
                if liq == "taker":
                    taker.add((key, arm, seq))
                fills_ext.append(
                    {
                        "order_key": key,
                        "arm": arm,
                        "fill_seq": seq,
                        "ts": f.ts,
                        "qty": f.qty,
                        "px": f.px,
                        "liquidity": liq,
                        "side": side,
                        "instrument_id": iid,
                        "fee": f.fee,
                        "fee_ccy": f.fee_ccy,
                        **exps_of(iid),
                    }
                )
        lq, lff, lev, lc = arm_vals["live"]
        sq, sff, sev, sc = arm_vals["sim"]
        lf, sf = lq > 0, sq > 0
        order_rows.append(
            {
                "order_key": key,
                "instrument_id": iid,
                "session_date": sdate,
                "side": side,
                "limit_px": int(first.get("price") or order_info.get(key, {}).get("limit_px") or 0),
                "qty": order_qty[key],
                "ts_entry": t0,
                "live_filled": lf,
                "live_filled_qty": lq,
                "live_ts_first_fill": lff,
                "sim_filled": sf,
                "sim_filled_qty": sq,
                "sim_ts_first_fill": sff,
                "class": classify(lf, sf),
                "live_end": lo_.end,
                "queue_ahead_at_entry": None if qa is None else int(qa),
                "queue_ahead_source": "none" if qa is None else src,
                "queue_decile": None,
                "liquidity_bucket": None,
                "ttf_live_ns": None if lff is None else lff - t0,
                "ttf_sim_ns": None if sff is None else sff - t0,
                "overlap_same_px": bool(overlap.get(key, False)),
                "sim_model_id": sim_model_id,
                "trade_vol_60s": None if tv60 is None else int(tv60),
                "legs": len(legs[key]),
                **exps_of(iid),
                "ambiguous_race": key in raced,
                "local_ns": t0 - midnight,
                "live_event_ns": lev,
                "live_cause": lc,
                "sim_event_ns": sev,
                "sim_cause": sc,
            }
        )
    if not order_rows:
        raise not_scored_error(len(live_orders), reasons, live_dir, sim_model_id)
    assign_strata(order_rows)
    extra: list[Row] = []
    if fills_only:
        for key in sorted(live_oc):
            if key in legs or not live_oc[key].fills:
                continue
            o = order_info.get(key, {})
            iid = o.get("instrument_id")
            if iid is None:
                continue
            t0 = live_oc[key].fills[0].ts
            if ds is not None:
                sdate, midnight = ds.day(int(iid), t0)
            else:
                sdate = t0 // DAY_NS
                midnight = sdate * DAY_NS
            extra.append({"order_key": key, "session_date": sdate, "ts_entry": t0, "local_ns": t0 - midnight})
            for i, f in enumerate((x for x in live_oc[key].fills if x.px is not None), start=1):
                liq = f.liquidity if f.liquidity in ("maker", "taker") else "unknown"
                if liq == "taker":
                    taker.add((key, "live", i))
                fills_ext.append(
                    {
                        "order_key": key,
                        "arm": "live",
                        "fill_seq": i,
                        "ts": f.ts,
                        "qty": f.qty,
                        "px": f.px,
                        "liquidity": liq,
                        "side": o.get("side", "buy"),
                        "instrument_id": int(iid),
                        "fee": f.fee,
                        "fee_ccy": f.fee_ccy,
                        **exps_of(int(iid)),
                    }
                )
    n_taker = {arm: sum(1 for t in taker if t[1] == arm) for arm in ("live", "sim")}
    if n_taker["live"] or n_taker["sim"]:
        warnings.append(
            f"taker_fills: live {n_taker['live']}, sim {n_taker['sim']} (book crossed the order); excluded from maker "
            "fill rate and maker markouts"
        )

    mk_rows: list[Row] = []
    if fills_ext and bbo is not None and ds is not None and ds.instruments is not None:
        warnings += apply_fee_rates(fills_ext, ds.instruments)
        markouts = compute_markouts(
            pa.Table.from_pylist(fills_ext),
            bbo,
            ds.instruments,
            list(s.markout_horizons_ns),
            list(s.markout_refs),
            last_event_ts=ds.ts_max,
            past_end=s.markout_past_end,
        )
        mk_rows = rows(markouts)
    else:
        markouts = make_table("qt.score_markouts", [], full=True)
        if fills_ext:
            warnings.append("markouts skipped: no dataset BBO")
    # 1.1 layout (E52): sim fills carry the fee charged above; prices may be null (review F13).
    score_fills = make_table("qt.score_fills", fills_ext, full=True)
    position = position_series(pa.Table.from_pylist(fills_ext)) if fills_ext else make_table("qt.score_position", [])
    asm = assemble(order_rows, mk_rows, s, fills_only=fills_only, extra=extra, taker=taker)
    warnings += asm.warnings
    if own_block is not None:
        warnings.append(block_own_orders(asm.parts, own_block))
    score_orders = make_table("qt.score_orders", order_rows, full=True)
    tests = make_table("qt.score_tests", asm.tests)

    files: list[dict[str, Any]] = []
    if s.out:
        out = Path(s.out)
        out.mkdir(parents=True, exist_ok=True)
        files.append(write(score_orders, out / "score_orders.parquet"))
        files.append(write(score_fills, out / "score_fills.parquet"))
        files.append(write(markouts, out / "score_markouts.parquet"))
        files.append(write(position, out / "score_position.parquet"))
        files.append(write(tests, out / "score_tests.parquet"))
        shortfall = score_shortfall(
            order_rows, fills_ext, bbo, ds, s, unpriced_qty=unpriced, n_unmatched_live=n_unmatched_live
        )
        files.append(write(shortfall, out / "score_shortfall.parquet"))
        if schedule:
            files.append(write(make_table("qt.shadow_spec", schedule), out / "schedule.parquet"))
        if zoo_arm_spec is not None and ds is not None and ds.kind != "l2":
            entry = traced_trajectory(rn, zoo_arm_spec, order_rows, schedule, amends, out)
            if entry is not None:
                files.append(entry)
    dq = None
    if ds is not None:
        raw = ds.data_quality()
        if raw is not None:
            try:
                dq = DataQuality.model_validate(raw)
            except ValueError:
                warnings.append("data_quality.json does not validate; omitted")
    dumped = s.model_dump(mode="json")
    summary = ScorecardSummary.model_validate(
        {
            "run_id": run_id(dumped, ENGINE_VERSION, resolve_ref),
            "spec": spec_echo(dumped),
            "live_source": str(live_dir),
            "sim_source": {
                "kind": sim_kind,
                "model_id": sim_model_id if sim_kind == "zoo" else None,
                "path": str(ref_path(s.sim.log, "sim.log")) if sim_kind == "external" else None,  # type: ignore[union-attr]
            },
            "n_orders": len(live_orders),
            "n_unmatched_live": n_unmatched_live,
            "n_unmatched_sim": n_unmatched_sim,
            "n_overlap_same_px": sum(1 for r in order_rows if r["overlap_same_px"]),
            **asm.parts,
            "data_quality": dq,
            "files": files,
            "warnings": warnings,
            "n_twin_unmatched": None if twins is None else twins.unmatched,
            "twin_match_rate": None if twins is None else twins.rate,
            "n_taker_fills": n_taker,
            "fills_only": fills_only,
        }
    )
    if s.out:
        write_json(summary.model_dump(mode="json"), Path(s.out) / "summary.json")
    return summary


def gate_error(summary: ScorecardSummary) -> QtError | None:
    """`QT-GATE-001` when a requested gate failed (for callers that raise instead of returning)."""
    if summary.verdict.gates_passed:
        return None
    return QtError("QT-GATE-001", "scorecard gates failed: " + ", ".join(summary.verdict.failed_gates))
