"""'Why did this fill' traces and the config echo section.

Selection (SPEC 5.9): scorecard orders that the arms disagree on, sim-only first, then live-only, then jointly filled
orders by |time-to-fill difference|; zoo shadows with the largest model-vs-exact error at h_max (the worst model per
shadow). Event tables come from, in order: a `qt.why_trace` document in the inputs; when a zoo run is among the
inputs, `queuetruth.api.why_explain` (exact trace merged with the zoo model's per-event queue estimate, the first
divergence and a one-sentence conclusion; zoo = the item's own zoo run, else the first zoo run of the inputs; model =
the item's model) when the run's dataset (and live log or shadow file) resolve from the current directory; else the
exact engine through `queuetruth.api.why`; the exact queue trajectory (`qt.queue_trajectory`) in the inputs; or else
a note with the command that produces one (`queuetruth.api.why_suggestion`, which never suggests `why` on L2-only
data, else a template). The model's queue estimate per event is read from the 1.1 trace fields
(`model_queue_ahead`, `first_divergence`, `conclusion`), else from the `why_explain` result, else from the events'
`model_ahead=<x>` / `first_divergence:` notes; without any, from `qt.zoo_trajectory` in the inputs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from queuetruth.errors import QtError
from queuetruth.report import cols
from queuetruth.report import fmt as F
from queuetruth.report.ctx import MAX_WHY_EVENTS, Ctx
from queuetruth.report.load import RunDir
from queuetruth.report.page import (
    INFO,
    WARN,
    Raw,
    badge,
    callout,
    code_block,
    details,
    figure,
    kv,
    section,
    table,
    ul,
)
from queuetruth.report.svg import Series, line_chart
from queuetruth.schemas.models import ScorecardSummary, WhyTrace, ZooSummary

WhyProvider = Callable[[dict[str, Any]], WhyTrace | None]


def _api_why(spec: dict[str, Any]) -> WhyTrace | None:
    """Runs the exact engine trace through `queuetruth.api.why`; None when unavailable or failing."""
    try:
        from queuetruth import api

        res = api.why(spec)
    except (QtError, NotImplementedError, ImportError, OSError, ValueError, RuntimeError, TypeError):
        return None
    if isinstance(res, WhyTrace):
        return res
    if isinstance(res, dict):
        try:
            return WhyTrace.model_validate(res)
        except ValueError:
            return None
    return None


#: Hook producing a `qt.why_trace` for a WhySpec dict (replaceable; None disables engine traces).
WHY_PROVIDER: WhyProvider | None = _api_why

WhyExplainProvider = Callable[[dict[str, Any], str | None, str | None], Any]


def _api_why_explain(spec: dict[str, Any], zoo: str | None, model: str | None) -> Any:
    """`queuetruth.api.why_explain(spec, zoo=..., model=...)`; None when unavailable or failing."""
    try:
        from queuetruth import api

        fn = getattr(api, "why_explain", None)
        return fn(spec, zoo=zoo, model=model) if fn is not None else None
    except (QtError, NotImplementedError, ImportError, OSError, ValueError, RuntimeError, TypeError):
        return None


#: Hook producing an explained trace (object with `trace` and optional model fields, or a `qt.why_trace`) for a
#: WhySpec dict, a zoo run directory and a model id; used when a zoo run is among the inputs (None disables it).
WHY_EXPLAIN_PROVIDER: WhyExplainProvider | None = _api_why_explain


@dataclass
class Explained:
    """An exact trace with the model comparison when one exists."""

    trace: WhyTrace
    model_id: str | None = None
    #: The model's queue ahead as of each event (aligned with `trace.events`; None entries before its first estimate).
    model_ahead: list[float | None] = field(default_factory=list)
    first_divergence: int | None = None
    conclusion: str | None = None
    notes: list[str] = field(default_factory=list)


_MODEL_NOTE = re.compile(r"(?:^|;\s*)model_ahead=(?P<v>[-+0-9.eEinfa]+)")
_DIV_NOTE = "first_divergence:"
#: Element id of the cross-summary warning list in the config section (the answer block links to it).
WARNINGS_ID = "config-warnings"


def _strip_model_notes(note: str | None) -> str:
    """An event note without the `model_ahead=` / `first_divergence:` parts (shown in their own column)."""
    if not note:
        return ""
    parts = [x.strip() for x in note.split(";")]
    return "; ".join(x for x in parts if x and not x.startswith("model_ahead=") and not x.startswith(_DIV_NOTE))


def explained(obj: Any) -> Explained | None:
    """`Explained` from a `qt.why_trace` (dict or model) or a `why_explain` result (module docstring precedence)."""
    if obj is None:
        return None
    raw = getattr(obj, "trace", None) if not isinstance(obj, WhyTrace | dict) else obj
    if raw is None:
        return None
    try:
        tr = raw if isinstance(raw, WhyTrace) else WhyTrace.model_validate(raw)
    except ValueError:
        return None
    n = len(tr.events)
    x = Explained(trace=tr, model_id=tr.model_id or getattr(obj, "model_id", None))
    mq = tr.model_queue_ahead
    if mq is None or len(mq) != n:
        mq = getattr(obj, "model_queue_ahead", None)
    if mq is None or len(mq) != n:
        per_event = [getattr(e, "model_queue_ahead", None) for e in tr.events]
        mq = per_event if any(v is not None for v in per_event) else None
    if mq is None or len(mq) != n:
        parsed: list[float | None] = []
        for e in tr.events:
            m = _MODEL_NOTE.search(e.note or "")
            try:
                parsed.append(float(m.group("v")) if m else None)
            except ValueError:
                parsed.append(None)
        mq = parsed if any(v is not None for v in parsed) else None
    x.model_ahead = [None if v is None else float(v) for v in mq] if mq is not None else []
    if tr.first_divergence is not None:
        x.first_divergence = int(tr.first_divergence.index)
    else:
        fd = getattr(obj, "first_divergence", None)
        if isinstance(fd, int) and not isinstance(fd, bool):
            x.first_divergence = fd
        else:
            x.first_divergence = next((i for i, e in enumerate(tr.events) if _DIV_NOTE in (e.note or "")), None)
    x.conclusion = tr.conclusion or getattr(obj, "conclusion", None) or None
    x.notes = list(tr.warnings or []) + [str(v) for v in (getattr(obj, "notes", None) or []) if v]
    if x.model_ahead and x.model_id is None:
        x.model_id = "model"
    return x


@dataclass
class WhyItem:
    """One order selected for a trace."""

    source: str
    run: RunDir
    subject: str
    title: str
    facts: list[tuple[str, str]]
    instrument_id: int | None = None
    model_id: str | None = None
    ts_live: int | None = None
    why_spec: dict[str, Any] | None = None
    reason: str = ""
    extra: list[str] = field(default_factory=list)


def _path_of(spec: dict[str, Any], key: str) -> str | None:
    v = spec.get(key)
    return v["path"] if isinstance(v, dict) and isinstance(v.get("path"), str) else None


def _score_items(ctx: Ctx, run: RunDir, s: ScorecardSummary, k: int) -> list[WhyItem]:
    so = run.table("qt.score_orders")
    if so is None or k <= 0:
        return []
    rows = cols.pylist(so.read())

    def ttf_diff(r: dict[str, Any]) -> int:
        a, b = r["ttf_live_ns"], r["ttf_sim_ns"]
        return abs(int(a) - int(b)) if a is not None and b is not None else -1

    def ent(r: dict[str, Any]) -> int:
        return _ns(r["ts_entry"])

    sel = sorted((r for r in rows if r["class"] == "sim_only"), key=lambda r: (ent(r), r["order_key"]))
    sel += sorted((r for r in rows if r["class"] == "live_only"), key=lambda r: (ent(r), r["order_key"]))
    sel += sorted(
        (r for r in rows if r["class"] == "both" and ttf_diff(r) > 0), key=lambda r: (-ttf_diff(r), r["order_key"])
    )
    ds, lv = _path_of(s.spec, "dataset"), _path_of(s.spec, "live")
    items = []
    for r in sel[:k]:
        iid = r["instrument_id"]
        why_spec = None
        if ds and lv and Path(ds).is_dir() and Path(lv).is_dir():
            why_spec = {"dataset": {"path": ds}, "live": {"path": lv}, "order_key": r["order_key"], "max_events": 5000}
        reason = {
            "sim_only": "filled in the simulator only",
            "live_only": "filled live only",
            "both": "filled in both arms, time to fill differs",
        }[r["class"]]
        facts = [
            ("instrument", F.num(iid) if iid is not None else F.DASH),
            ("side", r["side"]),
            ("limit price", F.price(r["limit_px"], ctx.price_exp(iid))),
            ("qty", F.num(r["qty"])),
            ("entry (UTC)", F.ts(_ns(r["ts_entry"]))),
            (
                "queue ahead at entry",
                f"{F.num(r['queue_ahead_at_entry'])} ({r['queue_ahead_source']}, decile {F.num(r['queue_decile'])})",
            ),
            (
                "live",
                f"{'filled' if r['live_filled'] else 'not filled'} {F.num(r['live_filled_qty'])}, ttf "
                f"{F.dur(r['ttf_live_ns'])}, end {r['live_end']}",
            ),
            (
                "sim",
                f"{'filled' if r['sim_filled'] else 'not filled'} {F.num(r['sim_filled_qty'])}, ttf "
                f"{F.dur(r['ttf_sim_ns'])} ({r['sim_model_id']})",
            ),
            ("legs", F.num(r["legs"])),
        ]
        items.append(
            WhyItem(
                "scorecard",
                run,
                r["order_key"],
                f"Order {r['order_key']}",
                facts,
                iid,
                s.sim_source.model_id if s.sim_source.kind == "zoo" else None,
                _ns(r["ts_entry"]),
                why_spec,
                reason,
            )
        )
    return items


def _ns(v: Any) -> int:
    """Timestamp from `cols.pylist` rows (int ns; None -> 0)."""
    return 0 if v is None else int(v)


def _zoo_items(ctx: Ctx, run: RunDir, z: ZooSummary, k: int) -> list[WhyItem]:
    pt = run.table("qt.zoo_predictions")
    if pt is None or k <= 0:
        return []
    tab = pt.read()
    hz, _ = cols.i64(tab, "horizon_ns")
    if hz.size == 0:
        return []
    hmax = int(hz.max())
    _, yok = cols.boolean(tab, "truth_filled")
    cens, _ = cols.boolean(tab, "censored")
    mask = (hz == hmax) & yok & ~cens
    if not mask.any():
        return []
    sub = tab.filter(pa.array(mask))
    p = cols.f64(sub, "p_fill")
    yy = cols.boolean(sub, "truth_filled")[0].astype(np.float64)
    mts, mok = cols.i64(sub, "model_ts_first_fill")
    tts, tok = cols.i64(sub, "truth_ts_first_fill")
    err = np.abs(p - yy)
    tte = np.where(mok & tok, np.abs(mts - tts), 0)
    sid = cols.strs(sub, "shadow_id").tolist()
    mid = cols.strs(sub, "model_id").tolist()
    best: dict[str, tuple[float, int, int, str, int]] = {}
    nwrong: dict[str, int] = {}
    nmod: dict[str, int] = {}
    for i, s_ in enumerate(sid):
        e = float(err[i]) if np.isfinite(err[i]) else 0.0
        nmod[s_] = nmod.get(s_, 0) + 1
        if e >= 0.5:
            nwrong[s_] = nwrong.get(s_, 0) + 1
        key = (e, int(tte[i]))
        cur = best.get(s_)
        if cur is None or key > (cur[0], cur[1]) or (key == (cur[0], cur[1]) and mid[i] < cur[3]):
            best[s_] = (e, int(tte[i]), 0, mid[i], i)
    ranked = sorted(best.items(), key=lambda kv_: (-kv_[1][0], -nwrong.get(kv_[0], 0), -kv_[1][1], kv_[0]))
    ranked = [r for r in ranked if r[1][0] > 0 or r[1][1] > 0][:k]
    rows = cols.pylist(sub)
    ds, sh = _path_of(z.spec, "dataset"), _path_of(z.spec, "shadows")
    items = []
    for s_, (e, _, _, m, i) in ranked:
        r = rows[i]
        tl = _ns(r["ts_live"])
        tff = _ns(r["truth_ts_first_fill"]) if r["truth_ts_first_fill"] is not None else None
        mff = _ns(r["model_ts_first_fill"]) if r["model_ts_first_fill"] is not None else None
        why_spec = None
        if ds and sh and Path(ds).is_dir() and Path(sh).exists():
            why_spec = {"dataset": {"path": ds}, "shadows": {"path": sh}, "shadow_id": s_, "max_events": 5000}
        iid = r["instrument_id"]
        facts = [
            ("instrument", F.num(iid)),
            ("qty", F.num(r["qty"])),
            ("live from (UTC)", F.ts(tl)),
            ("exact queue ahead at live", F.num(r["queue_ahead_at_live"])),
            (
                "visible level qty at live",
                F.num(r["visible_level_qty_at_live"]) + (" (out of view)" if r["out_of_view_at_live"] else ""),
            ),
            (
                "exact outcome",
                f"{'filled' if r['truth_filled'] else 'not filled'} by {F.dur(hmax)}"
                + (f", first fill after {F.dur(tff - tl)}" if tff is not None else ""),
            ),
            (
                f"worst model: {m}",
                f"p_fill {F.fixed(r['p_fill'], 3)}"
                + (f", model first fill after {F.dur(mff - tl)}" if mff is not None else ""),
            ),
            ("models wrong at h_max", f"{nwrong.get(s_, 0)} of {nmod.get(s_, 0)}"),
        ]
        items.append(
            WhyItem(
                "zoo",
                run,
                s_,
                f"Shadow {s_}",
                facts,
                iid,
                m,
                tl,
                why_spec,
                f"model-vs-exact error {F.fixed(e, 2)} at h_max",
            )
        )
    return items


def _shadow_ids(ctx: Ctx, item: WhyItem) -> pa.Array:
    """Shadow ids of the traced order: for a scorecard order every schedule leg whose `order_key` column equals
    the order key (legs are named `<order_key>.leg<k>` by the live schedule); always the subject id itself."""
    ids = {item.subject}
    if item.source == "scorecard":
        runs = [item.run] + [r for r in ctx.inputs.runs if r is not item.run]
        for run in runs:
            t = run.table("qt.shadow_spec")
            if t is None:
                continue
            tab = t.read()
            if "order_key" not in tab.column_names:
                continue
            sub = tab.filter(pc.equal(tab.column("order_key"), item.subject))
            ids.update(str(x) for x in sub.column("shadow_id").to_pylist())
            if sub.num_rows:
                break
    return pa.array(sorted(ids), pa.utf8())


def _match(sid: pa.ChunkedArray, ids: pa.Array) -> pa.ChunkedArray:
    return pc.is_in(sid, value_set=ids)


def _trajectory_rows(ctx: Ctx, item: WhyItem) -> list[dict[str, Any]]:
    runs = [item.run] + [r for r in ctx.inputs.runs if r is not item.run]
    for run in runs:
        t = run.table("qt.queue_trajectory")
        if t is None:
            continue
        tab = t.read()
        sub = tab.filter(_match(tab.column("shadow_id"), _shadow_ids(ctx, item)))
        if sub.num_rows:
            rows = cols.pylist(sub)
            for r in rows:
                r["ts_event"] = _ns(r["ts_event"])
            return rows
    return []


def _model_track(ctx: Ctx, item: WhyItem) -> list[tuple[int, float, int]]:
    if item.model_id is None:
        return []
    runs = [item.run] + [r for r in ctx.inputs.runs if r is not item.run]
    for run in runs:
        t = run.table("qt.zoo_trajectory")
        if t is None:
            continue
        tab = t.read()
        m = pc.and_(
            pc.equal(tab.column("model_id"), item.model_id), _match(tab.column("shadow_id"), _shadow_ids(ctx, item))
        )
        sub = cols.pylist(tab.filter(m))
        if sub:
            return [(_ns(r["ts_event"]), float(r["queue_ahead_est"]), int(r["queue_ahead_exact"])) for r in sub]
    return []


def _event_table(ctx: Ctx, item: WhyItem, tr: WhyTrace, x: Explained | None = None) -> Raw:
    ev = tr.events
    has_model = x is not None and bool(x.model_ahead)
    exp = ctx.price_exp(tr.instrument_id)
    idx = list(range(len(ev)))
    gap_at = None
    if len(ev) > MAX_WHY_EVENTS:
        head = MAX_WHY_EVENTS * 2 // 3
        idx = list(range(head)) + list(range(len(ev) - (MAX_WHY_EVENTS - head), len(ev)))
        gap_at = head
    rows: list[list[Any]] = []
    rc: list[str] = []
    for j, i in enumerate(idx):
        if gap_at is not None and j == gap_at:
            rows.append([f"… {len(ev) - MAX_WHY_EVENTS:,} events omitted …"] + [""] * (13 if has_model else 12))
            rc.append("gap")
        e = ev[i]
        rows.append(
            [
                F.ts(e.ts_event),
                str(e.sequence),
                e.action,
                e.side,
                F.price(e.price, exp),
                F.num(e.size),
                str(e.order_id),
                e.effect,
                "ahead" if e.is_ahead else "behind",
                F.num(e.queue_ahead_after),
                F.num(e.rank_after),
                F.num(e.level_qty_after),
                F.num(e.fill_qty) if e.fill_qty else "",
            ]
        )
        div = has_model and x is not None and x.first_divergence == i
        if has_model and x is not None:
            v = x.model_ahead[i]
            rows[-1].append((F.num(v) if v is not None else "") + (" ◀ first divergence" if div else ""))
            rows[-1].append(_strip_model_notes(e.note))
        else:
            rows[-1].append(e.note or "")
        rc.append("hl" if div else ("fill" if e.fill_qty else ""))
    width = 15 if has_model else 14
    for r in rows:
        if len(r) < width:
            r.extend([""] * (width - len(r)))
    heads = [
        "ts (UTC)",
        "seq",
        "action",
        "side",
        "price",
        "size",
        "order id",
        "effect",
        "vs shadow",
        "queue ahead after",
        "rank after",
        "level qty after",
        "fill qty",
    ]
    numeric: tuple[int, ...] = (1, 5, 6, 9, 10, 11, 12)
    if has_model and x is not None:
        heads.append(f"model queue ahead ({x.model_id})")
        numeric = (*numeric, 13)
    heads.append("note")
    return table(heads, rows, numeric=numeric, row_cls=rc, cls="why")


def _traj_table(ctx: Ctx, rows: list[dict[str, Any]]) -> Raw:
    out, rc = [], []
    shown = rows if len(rows) <= MAX_WHY_EVENTS else rows[: MAX_WHY_EVENTS * 2 // 3] + rows[-(MAX_WHY_EVENTS // 3) :]
    for r in shown:
        out.append(
            (
                F.ts(r["ts_event"]),
                r["kind"],
                f"{r['queue_ahead_before']:,} → {r['queue_ahead_after']:,}",
                f"{r['rank_before']:,} → {r['rank_after']:,}",
                F.num(r["level_qty_after"]),
                F.num(r["cancel_ahead_qty"]),
                F.num(r["cancel_behind_qty"]),
                F.num(r["add_behind_qty"]),
                F.num(r["traded_at_px_qty"]),
                F.num(r["trade_through_qty"]),
                F.num(r["filled_qty"]),
                r["shadow_id"],
            )
        )
        rc.append("fill" if r["kind"] == "end" and r["filled_qty"] else "")
    return table(
        [
            "ts (UTC)",
            "kind",
            "queue ahead",
            "rank",
            "level qty after",
            "cancels ahead",
            "cancels behind",
            "adds behind",
            "traded at price",
            "trade-through",
            "filled qty",
            "leg",
        ],
        out,
        numeric=(4, 5, 6, 7, 8, 9, 10),
        row_cls=rc,
        cls="why",
    )


def _step_chart(ctx: Ctx, item: WhyItem, exact: list[tuple[int, float]], model: list[tuple[int, float, int]]) -> str:
    t0 = item.ts_live or min([t for t, _ in exact] + [t for t, _, _ in model] or [0])
    series = []
    if len(exact) >= 1:
        pts = [((t - t0) / 1e9, v) for t, v in exact]
        series.append(Series("exact queue ahead", pts, slot=1, kind="step"))
    elif model:
        series.append(
            Series("exact queue ahead", [((t - t0) / 1e9, float(x)) for t, _, x in model], slot=1, kind="step")
        )
    if model:
        series.append(
            Series(
                f"model estimate ({item.model_id})",
                [((t - t0) / 1e9, v) for t, v, _ in model],
                slot=2,
                kind="step",
                dashed=True,
            )
        )
    if not series or max(len(s.points) for s in series) < 2:
        return ""
    svg = line_chart(
        ctx.uid(),
        f"Queue ahead over the life of {item.subject}",
        "Displayed quantity ahead of the order after each event: exact FIFO truth vs the model's estimate.",
        series,
        x_fmt=lambda v: f"{F.num(v, 3)}s",
        y_fmt=F.num,
        x_label="seconds since live",
        y_label="queue ahead (size units)",
        height=240,
        y_include=(0.0,),
    )
    rows = sorted((x, s.name, y) for s in series for x, y in s.points)
    if len(rows) > MAX_WHY_EVENTS:
        rows = rows[:: len(rows) // MAX_WHY_EVENTS + 1]
    data = table(
        ["seconds since live", "series", "queue ahead"],
        [(F.num(x, 6), n, F.num(y)) for x, n, y in rows],
        numeric=(0, 2),
    )
    return figure(
        svg,
        "Step chart of the queue ahead; the order can fill once the queue ahead reaches zero.",
        items=[(s.name, s.slot, s.dashed) for s in series],
        data=data,
    )


def why_section(ctx: Ctx) -> Raw | None:
    """Up to `max_why` traced orders."""
    k = ctx.spec.max_why
    scs = ctx.runs_with(ScorecardSummary)
    zs = ctx.runs_with(ZooSummary)
    if k <= 0 or not (scs or zs):
        return None
    k_score = k if not zs else (k + 1) // 2
    items: list[WhyItem] = []
    for run, s in scs:
        items += _score_items(ctx, run, s, k_score - len(items))
    for run, z in zs:
        items += _zoo_items(ctx, run, z, k - len(items))
    if not items:
        return None
    zoo_dirs = [str(run.root) for run, _ in zs]
    docs: dict[str, WhyTrace] = {}
    for run in ctx.inputs.runs:
        for d in run.docs_of(WhyTrace):
            key = d.subject.order_key or d.subject.shadow_id
            if key and key not in docs:
                docs[key] = d
    body: list[str] = []
    many_runs = len({id(it.run) for it in items}) > 1
    for n, it in enumerate(items, 1):
        aid = f"why-{n}"
        parts = [kv(it.facts)]
        x = explained(docs.get(it.subject))
        origin = "qt.why_trace document in the inputs" if x else ""
        if x is None and it.why_spec is not None and WHY_EXPLAIN_PROVIDER is not None and zoo_dirs:
            zdir = str(it.run.root) if it.source == "zoo" else zoo_dirs[0]
            x = explained(WHY_EXPLAIN_PROVIDER(it.why_spec, zdir, it.model_id))
            origin = "exact engine with the zoo model's queue estimate (queuetruth.api.why_explain)" if x else ""
        if x is None and it.why_spec is not None and WHY_PROVIDER is not None:
            x = explained(WHY_PROVIDER(it.why_spec))
            origin = "exact engine (queuetruth why)" if x else ""
        tr = x.trace if x is not None else None
        if x is not None and x.model_ahead:
            model = [
                (e.ts_event, float(v), int(e.queue_ahead_after))
                for e, v in zip(x.trace.events, x.model_ahead, strict=True)
                if v is not None
            ]
            it.model_id = x.model_id
        else:
            model = _model_track(ctx, it)
        exact: list[tuple[int, float]] = []
        if tr is not None and x is not None:
            exact = [(e.ts_event, float(e.queue_ahead_after)) for e in tr.events]
            if x.conclusion:
                parts.append(f'<p class="lead"><strong>Conclusion:</strong> {F.esc(x.conclusion)}</p>')
            if x.model_ahead and x.first_divergence is not None and x.first_divergence < len(tr.events):
                e0 = tr.events[x.first_divergence]
                v0 = x.model_ahead[x.first_divergence]
                parts.append(
                    kv(
                        [
                            (
                                "first divergence (model vs exact queue ahead)",
                                f"event {x.first_divergence + 1} at {F.ts(e0.ts_event)}: {x.model_id} "
                                f"{F.num(v0)} vs exact {F.num(e0.queue_ahead_after)}",
                            )
                        ]
                    )
                )
            if x.notes:
                parts.append(callout(INFO, "Trace notes", ul(x.notes)))
            parts.append(_step_chart(ctx, it, exact, model))
            parts.append(
                f"<h4>At-price events ({len(tr.events):,}{', truncated by the engine' if tr.truncated else ''}; "
                f"source: {F.esc(origin)})</h4>"
            )
            parts.append(_event_table(ctx, it, tr, x))
        else:
            traj = _trajectory_rows(ctx, it)
            if traj:
                exact = [(r["ts_event"], float(r["queue_ahead_after"])) for r in traj]
                parts.append(_step_chart(ctx, it, exact, model))
                parts.append(
                    f"<h4>Exact queue trajectory ({len(traj):,} events; per-event decomposition at the "
                    "order's price)</h4>"
                )
                parts.append(_traj_table(ctx, traj))
            else:
                if model:
                    parts.append(_step_chart(ctx, it, [], model))
                cmd = _cli_hint(it)
                parts.append(
                    callout(
                        INFO,
                        "No event-level trace in the inputs",
                        "<p>Produce one with the command below and pass its output file (or directory) as "
                        "another report input.</p>" + (code_block(cmd) if cmd else ""),
                    )
                )
        # one collapsed trace per order: the summary line is the index entry, the trace opens in place
        body.append(
            details(
                "",
                parts,
                cls="item why",
                sid=aid,
                summary_html=f"<h3>{n}. {F.esc(it.title)}</h3>",
                note=f"· {it.reason} · {it.source}" + (f" · {it.run.label}" if many_runs else ""),
            )
        )
    return section(
        "why",
        "Why did this fill?",
        body,
        lead="Event-by-event exact FIFO view of the orders where simulation and truth disagree most "
        f"({len(items)} traced); open an order to see its at-price events, its queue "
        "position and the first event where the model and the exact queue diverge.",
    )


def _cli_hint(it: WhyItem) -> str:
    spec = (
        next(
            (getattr(d.model, "spec", None) for d in it.run.docs if isinstance(d.model, ScorecardSummary | ZooSummary)),
            None,
        )
        or {}
    )
    ds_real, lv_real, sh_real = _path_of(spec, "dataset"), _path_of(spec, "live"), _path_of(spec, "shadows")
    if ds_real and Path(ds_real).is_dir():
        try:
            from queuetruth import api

            fn = getattr(api, "why_suggestion", None)
            got = None
            if fn is not None:
                if it.source == "scorecard":
                    got = fn(ds_real, live=lv_real, order_key=it.subject, model=it.model_id)
                else:
                    got = fn(ds_real, shadows=sh_real, shadow_id=it.subject, model=it.model_id)
        except (QtError, ImportError, OSError, ValueError, TypeError):
            got = None
        if got:
            return str(got)
    ds = ds_real or "<dataset>"
    if it.source == "scorecard":
        lv = lv_real or "<live log dir>"
        return f"queuetruth why --data {ds} --live {lv} --order-key {it.subject} --out why-{_safe(it.subject)}.json"
    sh = sh_real or "<shadows.parquet>"
    return f"queuetruth why --data {ds} --shadows {sh} --shadow-id {it.subject} --out why-{_safe(it.subject)}.json"


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)


# ---- config echo -------------------------------------------------------------------------------------------------


def summary_warnings(ctx: Ctx) -> tuple[list[str], int]:
    """Every summary's `warnings`, each prefixed with its file path, and the number of summaries that carry any (the
    config section lists them; the answer block shows the count)."""
    lines: list[str] = []
    n_docs = 0
    for run in ctx.inputs.runs:
        for d in run.docs:
            warns = getattr(d.model, "warnings", None) or []
            n_docs += bool(warns)
            lines += [f"{d.rec.path}: {w}" for w in warns]
    return lines, n_docs


def config(ctx: Ctx) -> Raw:
    """Spec echo of every summary and the file hashes it recorded, checked against the files read."""
    body: list[str] = []
    for run in ctx.inputs.runs:
        actual = {Path(x.rec.path).name: x.rec.sha256 for x in [*run.docs, *run.superseded, *run.extras, *run.tables]}
        for d in run.docs:
            m = d.model
            spec = getattr(m, "spec", None)
            files = getattr(m, "files", None)
            warns = getattr(m, "warnings", None) or []
            if spec is None and not files:
                continue
            rid = getattr(m, "run_id", None)
            block = [
                kv(
                    [
                        ("schema", m.SCHEMA),
                        ("run id", rid or F.DASH),
                        ("engine", getattr(m, "engine_version", None) or F.DASH),
                        ("warnings", F.num(len(warns))),
                    ]
                ),
            ]
            if spec is not None:
                block.append("<h4>Spec echo</h4>")
                block.append(code_block(json.dumps(spec, indent=2, ensure_ascii=False)))
            checks = ""
            if files:
                rows = []
                n_bad = 0
                for f in files:
                    name = Path(f.name).name
                    got = actual.get(name)
                    status = "not in input" if got is None else ("match" if got == f.sha256 else "CHANGED")
                    n_bad += status == "CHANGED"
                    rows.append(
                        (
                            f.name,
                            F.num(f.rows) if f.rows is not None else F.DASH,
                            F.bytes_h(f.bytes),
                            Raw(f'<span class="mono">{F.esc(f.sha256)}</span>'),
                            status,
                        )
                    )
                checks = f", {len(files)} file hash(es)" + (f", {n_bad} CHANGED" if n_bad else "")
                block.append("<h4>Files recorded by this summary</h4>")
                block.append(table(["file", "rows", "size", "sha256", "check"], rows, numeric=(1, 2)))
            # provenance is collapsed: the summary line carries what a reader scans for (schema, run id, changes)
            body.append(
                details(
                    d.rec.path,
                    block,
                    cls="item",
                    summary_html=f"<h3>{F.esc(d.rec.path)}</h3>",
                    note=f"· {m.SCHEMA} · run {rid or F.DASH}" + (" · spec echo" if spec is not None else "") + checks,
                )
            )
    warn_all, n_warned = summary_warnings(ctx)
    if warn_all:
        # collapsed, but its summary line is a warning badge with the count (the answer block links here)
        what = f"{len(warn_all)} warning(s) across {n_warned} summar{'y' if n_warned == 1 else 'ies'}"
        body.insert(
            0,
            details(
                what,
                [ul(warn_all)],
                sid=WARNINGS_ID,
                cls="more warns",
                summary_html=f"{badge(WARN, 'WARNINGS')} {F.esc(what)}",
            ),
        )
    return section(
        "config",
        "Configuration and file hashes",
        body,
        lead="Spec echoes exclude execution-only fields (out, threads); identical specs and inputs give "
        "identical run ids and identical outputs.",
    )
