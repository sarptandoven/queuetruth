"""`why` (`explain`): the exact trace of one order, the model next to it, and a conclusion.

With a live order: the order's first schedule leg from `queuetruth.live.to_schedule` (moved into its MBO twin's queue
place as the scorer does), then `_core.why` with the order's own records removed (ruling A1). `why_explain` adds the
zoo model's queue estimate per event, the first divergence and a conclusion.
"""

from __future__ import annotations

import datetime
import re
import tempfile
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

from queuetruth import units
from queuetruth.dataset import open_dataset, read_manifest_doc
from queuetruth.errors import QtError
from queuetruth.ops._call import _native, _path_of, _validate, _write_json
from queuetruth.ops.replay import _expand_order_ids
from queuetruth.ops.table import OperationDef
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table


def _live_leg(live_dir: str, order_key: str) -> pa.Table:
    from queuetruth import live as qlive

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sched, _ = qlive.to_schedule(live_dir, entry="exchange_ack", latency_ns=None)
    keys = sched.column("order_key").to_pylist() if "order_key" in sched.column_names else []
    idx = [i for i, k in enumerate(keys) if k == order_key]
    if not idx:
        raise QtError(
            "QT-CONFIG-001",
            f"order_key: {order_key!r} has no schedulable leg in {live_dir}",
            "market/IOC orders and orders without an entry time are not scheduled",
        )
    ts = pc.cast(sched.column("ts_submit"), pa.int64()).to_pylist()
    first = min(idx, key=lambda i: (ts[i], i))
    return sched.slice(first, 1)


_NS = 1_000_000_000


def dataset_kind(dataset: str | Path) -> str | None:
    """`kind` of a dataset directory's manifest (`mbo` or `l2`); None when it cannot be read."""
    try:
        doc = read_manifest_doc(dataset)
    except QtError:
        return None
    k = doc.get("kind") if isinstance(doc, dict) else None
    return str(k) if k is not None else None


def why_suggestion(
    dataset: str,
    *,
    shadows: str | None = None,
    shadow_id: str | None = None,
    live: str | None = None,
    order_key: str | None = None,
    model: str | None = None,
) -> str | None:
    """A `queuetruth` command line that explains one order on this dataset, or None when the dataset cannot be read.

    On MBO data it is `queuetruth why` (with `--model` when given). An exact queue trace needs MBO, so on an L2-only
    dataset `why` cannot run; the suggestion is then the model's queue trajectory for that order
    (`zoo --write-trajectory`; a live order is turned into a shadow first with `shadows from-live`)."""
    import shlex

    kind = dataset_kind(dataset)
    if kind is None:
        return None
    q = shlex.quote
    if kind != "l2":
        if live is not None and order_key is not None:
            cmd = f"queuetruth why --data {q(dataset)} --live {q(live)} --order-key {q(order_key)}"
        elif shadows is not None and shadow_id is not None:
            cmd = f"queuetruth why --data {q(dataset)} --shadows {q(shadows)} --shadow-id {q(shadow_id)}"
        else:
            return None
        return cmd + (f" --model {q(model)}" if model else "")
    m_id = model or "hbt_power3[n=2]"
    if live is not None and order_key is not None:
        return (
            f"queuetruth shadows from-live --live {q(live)} --out own_orders.parquet && queuetruth zoo --data "
            f"{q(dataset)} --shadows own_orders.parquet --models {q(m_id)} --write-trajectory --out why_zoo "
            f"(L2-only data: no exact queue; read rows with shadow_id {q(order_key)} in why_zoo/zoo_trajectory.parquet)"
        )
    if shadows is not None:
        sid = f" (shadow_id {q(shadow_id)})" if shadow_id else ""
        return (
            f"queuetruth zoo --data {q(dataset)} --shadows {q(shadows)} --models {q(m_id)} --write-trajectory "
            f"--out why_zoo (L2-only data: no exact queue; read why_zoo/zoo_trajectory.parquet{sid})"
        )
    return None


class WhyExplanation(BaseModel):
    """Result of `why_explain` (a Python object, not a `qt.*` document).

    - `trace`: the exact engine's `qt.why_trace`; with a model, each event's `note` also carries `model_ahead=<x>`
      and the first divergent event `first_divergence: ...`.
    - `symbol`, `price_exp`, `size_exp`, `tick_size`, `session_tz`: the instrument's units and its venue's time
      zone, for display.
    - `model_id`, `model_queue_ahead`: the zoo model's queue-ahead estimate as of each trace event (aligned with
      `trace.events`; None before the model's first estimate).
    - `first_divergence`: index of the first event after which model and exact queue ahead differ by at least
      `divergence_tol` (compared at the end of each event batch); None when they never do (or no model).
    - `excluded_order_ids`: the MBO order ids of the user's own order removed from the tape (live orders, A1).
    - `conclusion`: one plain sentence; `notes`: caveats (also emitted as warnings by `why`).
    """

    model_config = ConfigDict(extra="forbid")

    trace: m.WhyTrace
    symbol: str | None = None
    price_exp: int | None = None
    size_exp: int | None = None
    tick_size: int | None = None
    session_tz: str | None = None
    model_id: str | None = None
    model_queue_ahead: list[float | None] = Field(default_factory=list)
    first_divergence: int | None = None
    divergence_tol: float | None = None
    excluded_order_ids: list[int] = Field(default_factory=list)
    conclusion: str = ""
    notes: list[str] = Field(default_factory=list)


def _instrument_units(root: Path, iid: int, ts: int | None) -> dict[str, Any]:
    """`queuetruth.units.instrument_units` over the dataset at `root` (kept for older callers)."""
    return units.instrument_units(_units_source(root), iid, ts)


def _units_source(root: Path) -> Any:
    """The opened dataset for `units.instrument_units` (one without instruments when it cannot be read)."""
    try:
        return open_dataset(root, missing_ok=True)
    except QtError:
        return _NoInstruments()


class _NoInstruments:
    """A dataset that cannot be read: no instrument (units as `{}`, as for a missing table)."""

    def instrument(self, instrument_id: int, *, ts: int | None = None) -> None:
        return None


def fmt_utc(ns: int | None, digits: int = 6, date: bool = False) -> str:
    """Deprecated: use `queuetruth.units.local_time(ns, tz)`, which names the zone. `HH:MM:SS.ffffff` (UTC) of an
    epoch-ns timestamp; with `date`, `YYYY-MM-DD HH:MM:SS.ffffff`."""
    warnings.warn(
        "queuetruth.api.fmt_utc is deprecated; use queuetruth.units.local_time(ns, tz)",
        DeprecationWarning,
        stacklevel=2,
    )
    if ns is None:
        return "-"
    sec, frac = divmod(int(ns), _NS)
    t = datetime.datetime.fromtimestamp(sec, tz=datetime.UTC)
    base = t.strftime("%Y-%m-%d %H:%M:%S" if date else "%H:%M:%S")
    return base + (f".{frac:09d}"[: digits + 1] if digits > 0 else "")


def fmt_scaled(v: float | int | None, exp: int | None) -> str:
    """Deprecated: use `queuetruth.units.format_scaled` (or `format_price` for a price)."""
    warnings.warn(
        "queuetruth.api.fmt_scaled is deprecated; use queuetruth.units.format_scaled", DeprecationWarning, stacklevel=2
    )
    return units.format_scaled(v, exp)


def fmt_dur(ns: int | None) -> str:
    """Deprecated: use `queuetruth.units.format_duration(ns, sig=3)` (`250ms`, `52.2s`, `4.85min`)."""
    warnings.warn(
        "queuetruth.api.fmt_dur is deprecated; use queuetruth.units.format_duration(ns, sig=3)",
        DeprecationWarning,
        stacklevel=2,
    )
    return units.format_duration(ns, sig=3)


_FILL_REASONS = {
    "queue_depleted": "nothing was left ahead of you when a trade reached your price",
    "trade_through": "a trade printed through your price",
    "crossed": "the opposite side crossed your price (a taker fill)",
    "crossed_at_live": "your order was marketable on arrival (a taker fill)",
    "hidden_print": "a hidden execution printed at your price with nothing displayed ahead",
}


def _batches(events: list[m.WhyEvent]) -> list[list[int]]:
    """Indices of the events grouped by record batch (same `ts_event` and `sequence`), in order."""
    out: list[list[int]] = []
    key = None
    for i, e in enumerate(events):
        k = (e.ts_event, e.sequence)
        if k != key:
            out.append([])
            key = k
        out[-1].append(i)
    return out


def _queue_story(trace: m.WhyTrace, upto: int | None, size: Callable[[int], str]) -> tuple[int, int, str]:
    """(queue ahead when the order went live, after event `upto` (default: the last), and "x cancelled, y traded"
    for what left the queue ahead of it). Rows of one record batch all carry the queue after the batch; the lots
    that left are read from the batch's records ahead: a `C` of an order executed (`F`) in the same batch is
    traded, any other `C` cancelled, the rest of the drop modified away (priority-losing `M`) or cleared."""
    ev = trace.events
    if not ev:
        return 0, 0, ""
    last = len(ev) - 1 if upto is None else upto
    left = {"cancelled": 0, "traded": 0, "modified away": 0, "cleared": 0}
    q0: int | None = None
    prev = 0
    qa = 0
    for b in _batches(ev):
        if b[0] > last:
            break
        rows = [ev[i] for i in b]
        qa = rows[-1].queue_ahead_after
        executed = {e.order_id for e in rows if e.action == "F"}
        reduced = {e.order_id for e in rows if e.action == "C"}
        traded = cancelled = 0
        for e in rows:
            if not e.is_ahead:
                continue
            if e.action == "C" and e.effect in ("remove", "reduce"):
                if e.order_id in executed:
                    traded += e.size
                else:
                    cancelled += e.size
            elif e.action == "F" and e.order_id not in reduced:
                traded += e.size
        if q0 is None:
            q0 = prev = qa + traded + cancelled
        drop = max(0, prev - qa)
        prev = qa
        t = min(traded, drop)
        c = min(cancelled, drop - t)
        rest = drop - t - c
        left["traded"] += t
        left["cancelled"] += c
        if rest:
            left["cleared" if any(e.action == "R" for e in rows) else "modified away"] += rest
    parts = [f"{size(v)} {k}" for k, v in left.items() if v]
    return q0 or 0, qa, ", ".join(parts)


def _conclusion(x: WhyExplanation) -> str:
    """One plain sentence: why the order filled (or not), then where the model first went wrong."""
    t = x.trace
    ev = t.events

    def size(v: float | int) -> str:
        return units.format_scaled(v, x.size_exp)

    start = t.ts_live
    fill_i = next((i for i, e in enumerate(ev) if e.fill_qty > 0), None)
    if t.outcome.filled_qty > 0 and fill_i is not None:
        fe = ev[fill_i]
        reasons = list(dict.fromkeys(re.findall(r"fill \S+ ([a-z_]+)", fe.note or ""))) or ["fill"]
        q0, q1, story = _queue_story(t, fill_i, size)
        batch = [e for e in ev if (e.ts_event, e.sequence) == (fe.ts_event, fe.sequence)]
        prints = [e for e in batch if e.action == "T"]
        execs = [e for e in batch if e.action == "F"]
        what = []
        if prints:
            sizes = ", ".join(size(e.size) for e in prints[:4])
            what.append(f"{'a trade' if len(prints) == 1 else 'trades'} of {sizes} printed")
        if execs:
            who = ", ".join(
                f"order {e.order_id} ({size(e.size)}, {'ahead of' if e.is_ahead else 'behind'} you)" for e in execs[:3]
            )
            what.append(f"{'executing ' if prints else ''}{who}{'' if prints else ' traded'}")
        trig = f" at {units.local_time(fe.ts_event, x.session_tz, digits=3)}" + (
            f" when {', '.join(what)}" if what else ""
        )
        why_txt = "; ".join(_FILL_REASONS.get(r, r.replace("_", " ")) for r in reasons)
        span = units.format_duration(fe.ts_event - start, sig=3) if start is not None else "-"
        text = (
            f"Filled {size(t.outcome.filled_qty)} of {size(t.qty)}{trig}: {why_txt}. Queue ahead went "
            f"{size(q0)} -> {size(q1)} over {span}" + (f" ({story})." if story else ".")
        )
    elif not ev:
        text = f"Not filled (end state {t.outcome.end_state}); nothing happened at the order's price while it was live."
    else:
        q0, q1, story = _queue_story(t, None, size)
        span = units.format_duration(ev[-1].ts_event - start, sig=3) if start is not None else "-"
        text = (
            f"Not filled (end state {t.outcome.end_state}): queue ahead went {size(q0)} -> {size(q1)} over {span}"
            + (f" ({story})" if story else "")
            + (f"; {size(q1)} still ahead at the end." if q1 > 0 else "; no trade reached the price after that.")
        )
    if x.model_id is not None:
        first_batch = _batches(ev)[0] if ev else []
        q_start = _queue_story(t, first_batch[-1], size)[0] if ev else 0
        est0 = x.model_queue_ahead[x.first_divergence] if x.first_divergence is not None else None
        if x.first_divergence in first_batch and est0 is not None and abs(est0 - q_start) >= (x.divergence_tol or 1.0):
            text += (
                f" Model {x.model_id} was off from the start: when the order went live it assumed "
                f"{size(round(est0, 6))} ahead where the exact queue had {size(q_start)}."
            )
        elif x.first_divergence is not None and x.first_divergence < len(ev):
            d = ev[x.first_divergence]
            est = x.model_queue_ahead[x.first_divergence]
            when = units.local_time(d.ts_event, x.session_tz, digits=3, date=False)
            text += (
                f" Model {x.model_id} first diverged at {when}: "
                f"it had "
                f"{size(round(est, 6)) if est is not None else '-'} ahead where the exact queue had "
                f"{size(d.queue_ahead_after)} (after {d.action} of order {d.order_id}, "
                f"{'ahead of' if d.is_ahead else 'behind'} you)."
            )
        elif any(v is not None for v in x.model_queue_ahead):
            text += f" Model {x.model_id} stayed within {size(x.divergence_tol or 0)} of the exact queue."
        else:
            text += f" Model {x.model_id}: no queue estimate for this order."
    return text


def _model_matches(want: str, got: str) -> bool:
    """`hbt_power3[n=2]` matches `hbt_power3[exch=partial;mode=faithful;n=2]` (name equal, given params present)."""
    if want == got:
        return True

    def split(mid: str) -> tuple[str, set[str]]:
        name, _, rest = mid.partition("[")
        params = {p.strip() for p in rest.rstrip("]").replace(",", ";").split(";") if p.strip()}
        return name.strip(), params

    wn, wp = split(want)
    gn, gp = split(got)
    if wn != gn:
        return False
    canon = {p.replace(" ", "") for p in gp}
    for p in wp:
        k, _, v = p.partition("=")
        if p.replace(" ", "") in canon:
            continue
        try:
            fv = float(v)
        except ValueError:
            return False
        if not any(q.partition("=")[0] == k.strip() and _float_or_none(q.partition("=")[2]) == fv for q in canon):
            return False
    return True


def _float_or_none(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def _traj_rows(t: pa.Table, sid: str) -> pa.Table:
    t = t.filter(pc.equal(t.column("shadow_id"), sid))
    ts = pc.cast(t.column("ts_event"), pa.int64())
    return t.set_column(t.schema.get_field_index("ts_event"), "ts_event", ts)


def _pick_model(rows: pa.Table, model: str | None) -> tuple[str | None, pa.Table]:
    """The trajectory rows of one model: `model` when given, else the model that strays furthest from the exact
    queue on this shadow (the one worth explaining)."""
    ids = sorted({str(v) for v in rows.column("model_id").to_pylist()})
    if model is not None:
        ids = [i for i in ids if _model_matches(model, i)]
        if not ids:
            return None, rows.slice(0, 0)
        mid = ids[0]
    elif not ids:
        return None, rows
    else:

        def err(i: str) -> float:
            r = rows.filter(pc.equal(rows.column("model_id"), i))
            est = r.column("queue_ahead_est").to_pylist()
            ex = r.column("queue_ahead_exact").to_pylist()
            return max((abs(float(a) - float(b)) for a, b in zip(est, ex, strict=True) if a is not None), default=0.0)

        mid = max(ids, key=lambda i: (err(i), i))
    return mid, rows.filter(pc.equal(rows.column("model_id"), mid))


def _exact_mismatches(trace: m.WhyTrace, rows: pa.Table) -> int:
    """Trajectory rows whose `queue_ahead_exact` differs from this trace's exact queue at that time (rows of another
    tape or schedule must not be shown next to it)."""
    import bisect

    ev = trace.events
    if not ev or "queue_ahead_exact" not in rows.column_names:
        return 0
    q0 = _queue_story(trace, _batches(ev)[0][-1], str)[0]
    ts = [e.ts_event for e in ev]
    bad = 0
    for t_, ex in zip(rows.column("ts_event").to_pylist(), rows.column("queue_ahead_exact").to_pylist(), strict=True):
        if ex is None or (trace.ts_live is not None and int(t_) < trace.ts_live):
            continue
        j = bisect.bisect_right(ts, int(t_)) - 1
        want = ev[j].queue_ahead_after if j >= 0 else q0
        bad += int(int(ex) != int(want))
    return bad


def _zoo_ts_live(zoo: Path, sid: str) -> int | None:
    """`ts_live` of a shadow in a zoo output's `zoo_shadows.parquet` (None when unknown)."""
    try:
        t = pq.read_table(zoo / "zoo_shadows.parquet", columns=["shadow_id", "ts_live"])
    except (OSError, pa.ArrowInvalid, KeyError):
        return None
    t = t.filter(pc.equal(t.column("shadow_id"), sid))
    return int(pc.cast(t.column("ts_live"), pa.int64())[0].as_py()) if t.num_rows else None


def _model_run(
    root: Path, leg: pa.Table, model: str, excluded: list[tuple[int, int]], h_max_ns: int, notes: list[str]
) -> pa.Table | None:
    """The model's trajectory for one schedule leg from a one-shadow zoo run (own order removed from the L2 view
    when `excluded` is given and the core supports it)."""
    with tempfile.TemporaryDirectory(prefix="qt-why-zoo-") as tmp:
        sched = Path(tmp) / "leg.parquet"
        write_table(leg, sched, SCHEMAS["qt.shadow_spec"])
        base: dict[str, Any] = {
            "dataset": {"path": str(root)},
            "shadows": {"path": str(sched)},
            "out": str(Path(tmp) / "zoo"),
            "models": [model],
            "h_max_ns": int(h_max_ns),
            "write_fills": False,
            "write_trajectory": True,
            "bootstrap_b": 1,
        }
        excl = [{"instrument_id": i, "order_id": o} for i, o in excluded]
        try:
            _native("run_zoo", _validate(s.ZooSpec, {**base, "exclude_order_ids": excl}), m.ZooSummary)
        except QtError as e:
            if not excl or "exclude_order_ids" not in e.message:
                raise
            notes.append("model: this core cannot remove your own order from the L2 view; the model saw it")
            _native("run_zoo", _validate(s.ZooSpec, base), m.ZooSummary)
        p = Path(tmp) / "zoo" / "zoo_trajectory.parquet"
        return pq.read_table(p) if p.exists() else None


def _merge_model(x: WhyExplanation, rows: pa.Table, tol: float) -> None:
    """Fills `model_queue_ahead` (estimate as of each event's time) and `first_divergence` in place."""
    ts = rows.column("ts_event").to_pylist()
    est = rows.column("queue_ahead_est").to_pylist()
    order = sorted(range(len(ts)), key=lambda i: (ts[i], i))
    ts = [int(ts[i]) for i in order]
    est = [est[i] for i in order]
    import bisect

    vals: list[float | None] = []
    for e in x.trace.events:
        j = bisect.bisect_right(ts, int(e.ts_event)) - 1
        v = est[j] if j >= 0 else None
        vals.append(float(v) if v is not None and v == v else None)
    x.model_queue_ahead = vals
    x.divergence_tol = tol
    x.first_divergence = None
    for b in _batches(x.trace.events):
        i = b[-1]
        v = vals[i]
        if v is not None and abs(v - float(x.trace.events[i].queue_ahead_after)) >= tol:
            x.first_divergence = i
            break


def _annotate(x: WhyExplanation) -> m.WhyTrace:
    """The trace with the model column in each event's note (`model_ahead=<x>`) and the divergence marker."""
    if x.model_id is None or not x.model_queue_ahead:
        return x.trace
    events = []
    for i, e in enumerate(x.trace.events):
        parts = [e.note] if e.note else []
        v = x.model_queue_ahead[i]
        if v is not None:
            parts.append(f"model_ahead={v:.6g}")
        if i == x.first_divergence:
            parts.append(f"first_divergence: {x.model_id} {v:.6g} vs exact {e.queue_ahead_after}")
        events.append(e.model_copy(update={"note": "; ".join(parts) if parts else None}))
    return x.trace.model_copy(update={"events": events})


def _own_twin(root: Path, live_dir: str, leg: pa.Table, order_key: str, notes: list[str]) -> tuple[pa.Table, list]:
    """Self-exclusion for a live order on MBO data (ruling A1), as the scorer's exact arm does it: the order's MBO
    twin (by venue order id, else side/price/size/time), and the leg moved 1 ns earlier when the twin's add opens
    its timestamp, so the shadow takes the twin's queue place. Returns (leg, [(instrument_id, order_id)])."""
    from queuetruth.score.selfexclude import align_to_twins, match_twins, twin_entry_points

    try:
        from queuetruth import live as qlive

        orders, _ = qlive.read_log(live_dir)
        vid = None
        for r in orders.select(["order_key", "venue_order_id"]).to_pylist():
            if r["order_key"] == order_key:
                vid = r["venue_order_id"]
                break
        r0 = leg.slice(0, 1)
        ts_submit = int(pc.cast(r0.column("ts_submit"), pa.int64())[0].as_py())
        row = {
            "order_key": order_key,
            "shadow_id": str(r0.column("shadow_id")[0].as_py()),
            "instrument_id": int(r0.column("instrument_id")[0].as_py()),
            "side": int(r0.column("side")[0].as_py()),
            "price": int(r0.column("price")[0].as_py()),
            "qty": int(r0.column("qty")[0].as_py()),
            "ts_entry": ts_submit,
            "ts_submit": ts_submit,
            "latency_ns": int(r0.column("latency_ns")[0].as_py() or 0),
            "venue_order_id": vid,
        }
        tw = match_twins(root, [row])
    except (QtError, OSError, KeyError, ValueError, TypeError, pa.ArrowException) as e:
        notes.append(f"self-exclusion: twin lookup failed ({e}); the trace runs on the full tape")
        return leg, []
    if not tw.ids:
        notes.append(f"self-exclusion: no MBO record of {order_key} found; the trace runs on the full tape")
        return leg, []
    if tw.by_heuristic:
        notes.append(f"self-exclusion: {order_key} matched to its MBO twin by side/price/size/time, not by id")
    pts = twin_entry_points(root, tw.ids)
    moved, n = align_to_twins([row], tw, pts)
    if n:
        f = leg.schema.field("ts_submit")
        arr = pa.array([int(moved[0]["ts_submit"])], pa.int64()).cast(f.type)
        leg = leg.set_column(leg.schema.get_field_index("ts_submit"), f, arr)
    return leg, [(i, o) for i, ids in sorted(tw.ids.items()) for o in sorted(ids)]


def _shadow_leg(path: str, shadow_id: str) -> pa.Table:
    t = pq.read_table(path)
    t = t.filter(pc.equal(t.column("shadow_id"), shadow_id))
    if t.num_rows == 0:
        raise QtError("QT-CONFIG-001", f"shadow_id: {shadow_id!r} not found in {path}")
    return t.slice(0, 1)


def why_explain(
    spec: s.WhySpec | dict[str, Any],
    *,
    model: str | None = None,
    zoo: str | None = None,
    divergence_tol: float | None = None,
) -> WhyExplanation:
    """`why` with its explanation: the exact trace of one shadow (`shadows` + `shadow_id`) or one live order (`live`
    + `order_key`: its first schedule leg, entry = exchange ack, with the order's own MBO records removed from the
    tape, ruling A1), the zoo model's queue estimate per event, the first event where model and exact queue
    diverge, and a one-sentence conclusion.

    The model comes from `zoo` (an output directory with `zoo_trajectory.parquet` holding the shadow; `model`
    selects one of its models, default the one furthest from the exact queue) or, with `model` alone or when the
    directory lacks the shadow, from a one-shadow zoo run. `divergence_tol` (default: 10% of the order's size, at
    least one size unit) is the gap that counts as divergence. An L2-only dataset has no exact queue: QT-CONFIG-001
    with a command that works there in the hint. Bare order ids in a spec dict's `exclude_order_ids` apply to every
    instrument of the dataset (`resolve_order_ids`, as the CLI)."""
    sp = _validate(s.WhySpec, _expand_order_ids(spec))
    root = Path(_path_of(sp.dataset, "dataset"))
    shadows_path = _path_of(sp.shadows, "shadows") if sp.shadows is not None else None
    live_dir = _path_of(sp.live, "live") if sp.live is not None else None
    if dataset_kind(root) == "l2":
        hint = why_suggestion(
            str(root), shadows=shadows_path, shadow_id=sp.shadow_id, live=live_dir, order_key=sp.order_key, model=model
        )
        raise QtError(
            "QT-CONFIG-001",
            f"why: {root} is an L2-only dataset; an exact queue trace needs MBO data",
            f"see the model's queue estimate instead: {hint}" if hint else None,
        )
    notes: list[str] = []
    excluded: list[tuple[int, int]] = []
    with tempfile.TemporaryDirectory(prefix="qt-why-") as tmp:
        if live_dir is None:
            if shadows_path is None or sp.shadow_id is None:
                raise QtError("QT-CONFIG-001", "why: shadows and shadow_id (or live and order_key) are required")
            leg = None
            run = sp.model_copy(update={"out": None})
        else:
            if sp.order_key is None:
                raise QtError("QT-CONFIG-001", "order_key: required with live", "--order-key KEY")
            leg = _live_leg(live_dir, sp.order_key)
            if dataset_kind(root) == "mbo":
                leg, excluded = _own_twin(root, live_dir, leg, sp.order_key, notes)
            sched = Path(tmp) / "schedule.parquet"
            write_table(leg, sched, SCHEMAS["qt.shadow_spec"])
            sid = leg.column("shadow_id")[0].as_py()
            # `live` + `order_key` stay in the spec: the core removes the order's own MBO records (A1).
            run = sp.model_copy(update={"shadows": s.PathRef(path=str(sched)), "shadow_id": sid, "out": None})
        trace = _native("why", run, m.WhyTrace)
        inst = units.instrument_units(_units_source(root), trace.instrument_id, trace.ts_live)
        x = WhyExplanation(trace=trace, excluded_order_ids=[o for _, o in excluded], notes=notes, **inst)
        if model is not None or zoo is not None:
            sid = trace.subject.shadow_id or sp.shadow_id or ""
            tol = divergence_tol if divergence_tol is not None else max(1.0, 0.1 * float(trace.qty))
            rows = None
            if zoo is not None:
                tp = Path(zoo) / "zoo_trajectory.parquet"
                stale = _zoo_ts_live(Path(zoo), sid)
                if tp.exists() and stale is not None and trace.ts_live is not None and stale != trace.ts_live:
                    notes.append(
                        f"model: {zoo} ran {sid} with another schedule (went live at "
                        f"{units.local_time(stale, x.session_tz)}, here at "
                        f"{units.local_time(trace.ts_live, x.session_tz)}); its trajectory is not used"
                        + ("; running the model on this order" if model else "")
                    )
                elif tp.exists():
                    mid, rows = _pick_model(_traj_rows(pq.read_table(tp), sid), model)
                    bad = _exact_mismatches(trace, rows) if rows.num_rows else 0
                    if bad:
                        notes.append(
                            f"model: the exact queue in {tp} disagrees with this trace at {bad} point(s) (another "
                            "dataset, schedule or self-exclusion); its trajectory is not used"
                            + ("; running the model on this order" if model else "")
                        )
                        rows = None
                    elif rows.num_rows == 0:
                        notes.append(
                            f"model: {tp} has no trajectory rows for {sid}"
                            + (f" and model {model}" if model else "")
                            + ("; running the model on this order" if model else "")
                        )
                        rows = None
                    else:
                        x.model_id = mid
                else:
                    notes.append(
                        f"model: {tp} not found (run zoo with --write-trajectory)"
                        + ("; running the model on this order" if model else "")
                    )
            if rows is None and model is not None:
                if leg is None:
                    leg = _shadow_leg(shadows_path or "", sp.shadow_id or "")
                last = max((e.ts_event for e in trace.events), default=trace.ts_live or 0)
                span = max(0, last - (trace.ts_live or last))
                traj = _model_run(root, leg, model, excluded, max(300 * _NS, span + _NS), notes)
                if traj is not None and traj.num_rows:
                    mid, rows = _pick_model(_traj_rows(traj, str(leg.column("shadow_id")[0].as_py())), None)
                    x.model_id = mid
                else:
                    notes.append(f"model: {model} produced no queue estimate for this order")
            if rows is not None and x.model_id is not None:
                _merge_model(x, rows, tol)
            elif model is not None and x.model_id is None:
                x.model_id = model
    x.notes = list(notes)  # validation copied the list; later notes are added here
    x.conclusion = _conclusion(x)
    x.trace = _annotate(x)
    if sp.out:
        _write_json(x.trace.model_dump(mode="json"), Path(sp.out))
    return x


def why(
    spec: s.WhySpec | dict[str, Any],
    *,
    model: str | None = None,
    zoo: str | None = None,
    divergence_tol: float | None = None,
) -> m.WhyTrace:
    """`why`: event trace of one shadow (`shadows` + `shadow_id`) or of one live order (`live` + `order_key`: its
    first schedule leg, entry = exchange ack, own MBO records removed). With `model` / `zoo` the events' notes carry
    the model's queue estimate and the first divergence (see `why_explain`, which also returns them as data and a
    conclusion). Caveats are emitted as warnings."""
    x = why_explain(spec, model=model, zoo=zoo, divergence_tol=divergence_tol)
    for n in x.notes:
        warnings.warn(n, UserWarning, stacklevel=2)
    return x.trace


OPERATIONS = (
    OperationDef(
        "why",
        s.WhySpec,
        why,
        m.WhyTrace,
        "Event by event trace of one shadow or one live order in the exact queue.",
        out_name="why.json",
    ),
)
