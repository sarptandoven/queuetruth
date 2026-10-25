"""Console script `queuetruth` (SPEC 4.4): clap parse via `_core.cli_parse`, then native commands in Rust
(`_core.cli_run_native`) and Python / hybrid commands through `queuetruth.api`.

Exit codes: 0 success, 1 the command completed but a requested gate failed (`--strict`, `--fail-*`, referee
tolerance, scorecard gates, doctor problems), 2 any error. With `--format json` exactly one JSON document goes to
stdout (the summary on 0/1, a `qt.error` document on 2); warnings, logs and progress go to stderr only.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from queuetruth.errors import EXIT_GATE, EXIT_OK, QtError

log = logging.getLogger("queuetruth")

Doc = dict[str, Any]


def _plain(v: Any) -> Any:
    """JSON-ready value: models dumped by alias, NaN/Infinity -> null."""
    if isinstance(v, BaseModel):
        v = v.model_dump(mode="json", by_alias=True)
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def _scalar(v: Any) -> str | None:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v == 0.0 or abs(v) >= 1e-4:
            return f"{v:.6f}".rstrip("0").rstrip(".")
        return f"{v:e}"
    if isinstance(v, (int, str)):
        return str(v)
    return None


def _short(a: list[Any]) -> str:
    if not a:
        return "none"
    items = [x for x in (_scalar(v) for v in a[:8]) if x is not None]
    return ", ".join(items) if len(items) == len(a) else f"{len(a)} items"


def render_human(path: list[str], doc: Any) -> str:
    """Terminal rendering of a summary (same layout as the Rust binary)."""
    if path == ["schema", "show"] or not isinstance(doc, dict):
        return json.dumps(doc, indent=2, ensure_ascii=False)
    if path == ["schema", "list"]:
        return "\n".join(f"{k}:\n" + "\n".join(f"  {n}" for n in v) for k, v in doc.items())
    lines = []
    if isinstance(doc.get("schema"), str):
        lines.append(f"{' '.join(path)} ({doc['schema']})")
    for k, v in doc.items():
        if k in ("schema", "schema_version", "spec", "warnings", "files"):
            continue
        sv = _scalar(v)
        if sv is not None:
            lines.append(f"{k}: {sv}")
        elif isinstance(v, list):
            lines.append(f"{k}: {_short(v)}")
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if k2 in ("schema", "schema_version"):
                    continue
                s2 = _scalar(v2)
                if s2 is None:
                    s2 = _short(v2) if isinstance(v2, list) else f"{len(v2)} fields" if isinstance(v2, dict) else ""
                lines.append(f"{k}.{k2}: {s2}")
    files = doc.get("files")
    if isinstance(files, list):
        lines.append(f"files ({len(files)}):")
        for f in files:
            rows = f"{f['rows']} rows, " if f.get("rows") is not None else ""
            lines.append(f"  {f.get('name')} ({rows}{f.get('bytes', 0)} bytes)")
    return "\n".join(lines)


# ---- answers first: human views of zoo, why, score and calibrate -------------------------------------------------
#
# Each view leads with the answer to the command's question and falls back to `render_human` when the summary lacks
# what it needs (a view never fails a command that succeeded). `--verbose` appends the full `render_human` dump.

H_DEFAULT_NS = 60_000_000_000
HBT_312_URL = "https://github.com/nkaz001/hftbacktest/issues/312"


def _pp(x: float | None, signed: bool = True) -> str:
    """A fraction as percentage points (`+1.3`)."""
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{x * 100:+.1f}" if signed else f"{abs(x) * 100:.1f}"


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3g}"


def _hz(ns: int | str | None) -> str:
    """Horizon label: `100ms`, `1s`, `60s`, `5min`."""
    if ns is None:
        return "-"
    n = int(ns)
    for unit, size in (("min", 60_000_000_000), ("s", 1_000_000_000), ("ms", 1_000_000), ("us", 1_000)):
        if n >= size and n % size == 0 and (unit != "min" or n >= 120_000_000_000):
            return f"{n // size}{unit}"
    return f"{n}ns"


def _table(header: list[str], rows: list[list[str]], right: set[int] | None = None) -> list[str]:
    """Plain aligned text table (numbers right-aligned in the `right` columns)."""
    right = right or set()
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]

    def line(r: list[str]) -> str:
        cells = [
            str(c).rjust(w) if i in right else str(c).ljust(w) for i, (c, w) in enumerate(zip(r, widths, strict=True))
        ]
        return "  ".join(cells).rstrip()

    return [line(header), *(line(r) for r in rows)]


def _files_lines(doc: Doc) -> list[str]:
    files = doc.get("files")
    if not isinstance(files, list) or not files:
        return []
    names = ", ".join(str(f.get("name")) for f in files if isinstance(f, dict))
    return [f"files: {names}"]


def _zoo_extras(out: str | None) -> tuple[int | None, int | None, dict[tuple[str, str, str], Any]]:
    """(never-cancel shadows, all shadows, {(model, metric, horizon): zoo_errors row}) from a zoo output directory."""
    if not out:
        return None, None, {}
    import pyarrow.parquet as pq

    nc = total = None
    rows: dict[tuple[str, str, str], Any] = {}
    try:
        t = pq.read_table(Path(out) / "zoo_shadows.parquet", columns=["never_cancel"])
        flags = t.column("never_cancel").to_pylist()
        nc, total = sum(1 for f in flags if f), len(flags)
    except (OSError, KeyError, ValueError):
        pass
    try:
        e = pq.read_table(Path(out) / "zoo_errors.parquet")
        for r in e.to_pylist():
            if r.get("stratum_kind") == "all":
                rows[(str(r["model_id"]), str(r["metric"]), str(r.get("horizon_ns")))] = r
    except (OSError, KeyError, ValueError):
        pass
    return nc, total, rows


def _render_zoo(doc: Doc, args: Doc, extra: Doc) -> str | None:
    """Zoo leaderboard: ranked and worded by `queuetruth.eval.leaderboard` (the report's single source): sorted by
    |fill-rate bias| at the sort horizon (`--sort-horizon`, default 60 s; `sort_horizon` picks the nearest horizon
    below when it is not evaluated), smallest first, then Brier, then model id."""
    from queuetruth.eval.leaderboard import (
        DEFAULT_SORT_HORIZON_NS,
        bias_sentence,
        has_mcs,
        mcs_sentence,
        mcs_table,
        pct,
        rank_leaderboard,
        rates_at,
        sort_horizon,
    )

    board = doc.get("leaderboard")
    if not isinstance(board, list) or not board:
        return None
    horizons = sorted({int(h) for e in board for h in (e.get("fill_rate_bias") or {})})
    if not horizons:
        return None
    sort_h = sort_horizon(horizons, int(extra.get("sort_horizon_ns") or DEFAULT_SORT_HORIZON_NS))
    if sort_h is None:
        return None
    key = str(sort_h)
    nc, total, err = _zoo_extras(args.get("out"))
    for k in ("n_never_cancel", "never_cancel_n", "n_leaderboard"):  # summary fields, when the zoo reports them
        if isinstance(doc.get(k), int):
            nc = int(doc[k])
    h_max = (doc.get("spec") or {}).get("h_max_ns") or args.get("h_max_ns") or max(horizons)
    ranked = rank_leaderboard(board, sort_h)
    lines = [f"zoo: {doc.get('n_shadows')} shadows, {len(doc.get('models') or [])} models (run {doc.get('run_id')})"]
    pop = (
        f"{nc} never-cancel shadows (planned life >= h_max {_hz(h_max)})" + (f" of {total}" if total else "")
        if nc is not None
        else "never-cancel shadows (planned life >= h_max)"
    )
    lines.append(f"leaderboard population: {pop}, the same for every model")
    excluded = doc.get("n_excluded_hbt_312")
    if isinstance(excluded, int) and excluded > 0:
        lines.append(
            f"  {excluded} shadow(s) on which an hftbacktest model hit its bug #312 are left out of every model "
            f"({HBT_312_URL})"
        )
    lines.append(f"sorted by |fill-rate bias| at {_hz(sort_h)}, smallest first")
    lines.append("")
    lines.append("exact fill rate in %, then each model's fill-rate bias (model minus exact) in pp")
    lines.append("(* = 95% CI excludes 0):")
    exact_cells = []
    for h in horizons:
        x = next((r for e in ranked if (r := rates_at(e, h)[1]) is not None), None)
        exact_cells.append(f"{pct(x)}% " if x is not None else "-  ")
    rows = [["exact fill rate", *exact_cells]] if any(c != "-  " for c in exact_cells) else []
    for e in ranked:
        cells = [str(e.get("model_id"))]
        for h in horizons:
            b = (e.get("fill_rate_bias") or {}).get(str(h)) or {}
            v = b.get("value") if isinstance(b, dict) else None
            lo, hi = (b.get("ci_lo"), b.get("ci_hi")) if isinstance(b, dict) else (None, None)
            star = "*" if lo is not None and hi is not None and (lo > 0 or hi < 0) else " "
            cells.append(_pp(v) + star if v is not None else "-  ")
        rows.append(cells)
    lines += _table(["model", *(_hz(h) for h in horizons)], rows, right=set(range(1, len(horizons) + 1)))
    lines.append("")
    if has_mcs(board):  # the model confidence set on first-fill error (`queuetruth.eval.leaderboard`)
        legend, mrows = mcs_table(ranked, horizons)
        lines += legend
        lines += _table(["model", *(_hz(h) for h in horizons)], mrows, right=set(range(1, len(horizons) + 1)))
        lines.append("")
    lines.append("in plain words:")
    for e in ranked:
        mid = str(e.get("model_id"))
        sentence = bias_sentence(e, sort_h)
        more = []
        n_row = err.get((mid, "fill_rate_bias", key))
        if n_row and n_row.get("n") is not None:
            more.append(f"n = {int(n_row['n'])}")
        p_row = err.get((mid, "discordance_p_primary", str(h_max)))
        if p_row and p_row.get("value") is not None:
            more.append(f"at {_hz(h_max)} cluster-robust discordance p = {_p(p_row['value'])}")
        if more:
            sentence = f"{sentence[:-1]}; {'; '.join(more)}."
        lines.append(f"  {sentence}")
    if (tied := mcs_sentence(ranked, sort_h)) is not None:
        lines.append(f"  {tied}")
    lines += _files_lines(doc)
    return "\n".join(lines)


_SIDE_WORD = {"B": "bid", "A": "ask", "N": "-"}
_ORDER_SIDE = {"B": "buy", "A": "sell"}


def _why_rows(x: Any, verbose: bool) -> list[int | None]:
    """Event indices to show (None = an elided gap): everything with --verbose or up to 80 events; else the first
    and last 25 plus 3 events around each fill and the first divergence."""
    n = len(x.trace.events)
    if verbose or n <= 80:
        return list(range(n))
    keep = set(range(25)) | set(range(n - 25, n))
    marks = [i for i, e in enumerate(x.trace.events) if e.fill_qty > 0]
    if x.first_divergence is not None:
        marks.append(x.first_divergence)
    for i in marks:
        keep |= set(range(max(0, i - 3), min(n, i + 4)))
    out: list[int | None] = []
    prev = -1
    for i in sorted(keep):
        if i != prev + 1:
            out.append(None)
        out.append(i)
        prev = i
    return out


def _render_why(x: Any, verbose: bool) -> str:
    from queuetruth.api import fmt_scaled, fmt_utc

    t = x.trace

    def px(v: int | None) -> str:
        return fmt_scaled(v, x.price_exp)

    def sz(v: float | int | None) -> str:
        return fmt_scaled(v, x.size_exp)

    subj = f"live order {t.subject.order_key}" if t.subject.order_key else f"shadow {t.subject.shadow_id}"
    inst = f"{x.symbol} (instrument {t.instrument_id})" if x.symbol else f"instrument {t.instrument_id}"
    side = _ORDER_SIDE.get(t.side, t.side)
    lines = [
        f"why: {subj} | {inst} | {side} {sz(t.qty)} @ {px(t.price)} | live {fmt_utc(t.ts_live, 6, date=True)} UTC",
        f"outcome: {t.outcome.end_state}, filled {sz(t.outcome.filled_qty)} of {sz(t.qty)}"
        + (f", first fill {fmt_utc(t.outcome.ts_first_fill, 6)}" if t.outcome.ts_first_fill is not None else ""),
    ]
    if x.excluded_order_ids:
        ids = ", ".join(str(i) for i in x.excluded_order_ids)
        lines.append(f"own order removed from the tape (self-exclusion): MBO order id {ids}")
    has_model = x.model_id is not None and any(v is not None for v in x.model_queue_ahead)
    if has_model:
        lines.append(f"model: {x.model_id} (its queue-ahead estimate next to the exact queue)")
    lines.append("")
    header = ["time (UTC)", "act", "order", "side", "price", "size", "effect", "", "q.ahead", "rank", "level"]
    header += ["fill", *(["model"] if has_model else []), "note"]
    rows: list[list[str]] = []
    for i in _why_rows(x, verbose):
        if i is None:
            rows.append(["...", *([""] * (len(header) - 1))])
            continue
        e = t.events[i]
        note = "; ".join(
            p for p in (e.note or "").split("; ") if p and not p.startswith(("model_ahead=", "first_divergence:"))
        )
        marks = []
        if e.fill_qty > 0:
            marks.append(f"<< FILL {sz(e.fill_qty)}")
        if i == x.first_divergence:
            marks.append("<< model diverges")
        cells = [
            fmt_utc(e.ts_event, 6),
            e.action,
            str(e.order_id) if e.order_id else "-",
            (_ORDER_SIDE if e.action == "T" else _SIDE_WORD).get(e.side, e.side),
            px(e.price),
            sz(e.size),
            e.effect,
            "ahead" if e.is_ahead else "",
            sz(e.queue_ahead_after),
            str(e.rank_after),
            sz(e.level_qty_after),
            sz(e.fill_qty) if e.fill_qty else "",
        ]
        if has_model:
            v = x.model_queue_ahead[i] if i < len(x.model_queue_ahead) else None
            cells.append("-" if v is None else f"{v:.4g}")
        cells.append(" ".join([note, *marks]).strip())
        rows.append(cells)
    num = {4, 5, 8, 9, 10, 11} | ({12} if has_model else set())
    if rows:
        lines += _table(header, rows, right=num)
    else:
        lines.append("(no event at the order's price while it was live)")
    if rows:
        lines.append("(side: bid/ask of the resting order; on T rows the aggressor, buy or sell)")
    if t.truncated:
        lines.append(f"(trace truncated at {len(t.events)} events; raise --max-events)")
    lines.append("")
    lines.append(f"conclusion: {x.conclusion}")
    return "\n".join(lines)


def _gates_set(spec: Doc) -> bool:
    g = spec.get("gates") or {}
    return isinstance(g, dict) and (g.get("max_abs_fill_rate_bias") is not None or bool(g.get("require_equivalence")))


def _score_clusters(doc: Doc, out: str | None) -> tuple[float | None, int | None, str | None]:
    """(cluster-robust p, clusters, method): `discordance_clustered` (1.1), else the `discordance_clustered` row of
    `score_tests.parquet` in the output directory."""
    dc = doc.get("discordance_clustered")
    if isinstance(dc, dict):
        return dc.get("p_value"), dc.get("n_clusters"), dc.get("method")
    if not out:
        return None, None, None
    try:
        import pyarrow.parquet as pq

        t = pq.read_table(Path(out) / "score_tests.parquet")
    except (OSError, ValueError):
        return None, None, None
    for r in t.to_pylist():
        if r.get("test_id") == "discordance_clustered" and r.get("stratum_kind") == "all":
            note = str(r.get("note") or "")
            k = re.search(r"clusters=(\d+)", note)
            return r.get("p_value"), int(k.group(1)) if k else None, note.split(";")[0] or None
    return None, None, None


def _render_score(doc: Doc, args: Doc, extra: Doc) -> str | None:
    bias = doc.get("bias")
    tbt = doc.get("two_by_two")
    verdict = doc.get("verdict")
    if not isinstance(bias, dict) or not isinstance(tbt, dict) or not isinstance(verdict, dict):
        return None
    spec = doc.get("spec") or {}
    window = int(extra.get("window_ns") or spec.get("window_ns") or H_DEFAULT_NS)
    sim = doc.get("sim_source") or {}
    arm = {
        "zoo": f"zoo model {sim.get('model_id')}",
        "exact": "exact replay",
        "external": f"simulator log {sim.get('path') or ''}".strip(),
    }.get(str(sim.get("kind")), str(sim.get("kind")))
    p, k, _method = _score_clusters(doc, args.get("out"))
    lines = [f"score: {arm} vs the live log {doc.get('live_source')} (run {doc.get('run_id')})"]
    v, lo, hi = bias.get("fill_rate_bias"), bias.get("ci_lo"), bias.get("ci_hi")
    if doc.get("fills_only"):
        lines.append(f"RESULT: {verdict.get('headline')}")
    elif v is None:
        lines.append(f"RESULT: not computable ({verdict.get('headline')})")
    else:
        if abs(v) < 5e-4:
            what = "the simulator fills as often as live"
        else:
            what = f"the simulator fills {_pp(v, signed=False)} pp {'more' if v > 0 else 'less'} often than live"
        ci = f"95% CI {_pp(lo)} to {_pp(hi)} pp" if lo is not None and hi is not None else "no CI"
        if p is not None:
            sig = f"{'significant' if p < 0.05 else 'not significant'} (cluster-robust p = {_p(p)}"
            sig += f", {k} clusters)" if k is not None else ")"
        else:
            excl = lo is not None and hi is not None and (lo > 0 or hi < 0)
            sig = "significant (CI excludes 0)" if excl else "not significant (CI includes 0)"
        mc = (doc.get("mcnemar") or {}).get("p_exact")
        sig += f"; plain McNemar p = {_p(mc)} assumes independent orders" if mc is not None else ""
        lines.append(f"RESULT: {what} ({ci}) within {_hz(window)} of entry; {sig}")
    eq = doc.get("equivalence") or {}
    if eq.get("fill_rate_margin") is not None and v is not None and not doc.get("fills_only"):
        m = _pp(eq.get("fill_rate_margin"), signed=False)
        if eq.get("fill_rate_equivalent"):
            lines.append(f"equivalence: equivalent to live within +/-{m} pp (TOST p = {_p(eq.get('tost_p'))})")
        else:
            lines.append(f"equivalence: NOT shown within +/-{m} pp (TOST p = {_p(eq.get('tost_p'))})")
    n_orders, n_matched = doc.get("n_orders"), doc.get("n_matched")
    lines.append(
        f"orders: {n_matched} of {n_orders} live orders scored ({doc.get('n_unmatched_live')} without a sim outcome, "
        f"{doc.get('n_unmatched_sim')} sim orders without a live order)"
    )
    taker = doc.get("n_taker_fills")
    if isinstance(taker, dict) and (taker.get("live") or taker.get("sim")):
        lines.append(
            f"taker fills (left out of the maker statistics): live {taker.get('live')}, sim {taker.get('sim')}"
        )
    if _gates_set(spec):
        failed = verdict.get("failed_gates") or []
        lines.append("gates: passed" if verdict.get("gates_passed") else f"gates: FAILED ({', '.join(failed)})")
    else:
        lines.append("gates: none set (pass --fail-abs-bias X or --fail-not-equivalent to gate on this)")
    lines.append("")
    lines.append(f"orders by fill outcome within {_hz(window)}:")
    lines += _table(
        ["", "sim filled", "sim not filled"],
        [
            ["live filled", str(tbt.get("both")), str(tbt.get("live_only"))],
            ["live not filled", str(tbt.get("sim_only")), str(tbt.get("neither"))],
        ],
        right={1, 2},
    )
    lines += _files_lines(doc)
    lines.append("(every statistic: --verbose, summary.json or queuetruth report)")
    return "\n".join(lines)


def _grid_of(doc: Doc) -> tuple[float, float, int] | None:
    """(lo, hi, n) of the parameter grid in theta units (ln n for the power families)."""
    spec = doc.get("spec") or {}
    fam = str(doc.get("family") or "")
    g = spec.get("grid")
    if isinstance(g, dict) and {"lo", "hi", "n"} <= set(g):
        return float(g["lo"]), float(g["hi"]), int(g["n"])
    if isinstance(g, dict):
        sub = g.get("databento" if fam == "databento" and doc.get("kind") == "alloc" else str(doc.get("kind")))
        if fam.startswith("power"):
            sub = g.get("alloc")
        if isinstance(sub, dict) and {"lo", "hi", "n"} <= set(sub):
            return float(sub["lo"]), float(sub["hi"]), int(sub["n"])
    if doc.get("kind") == "live" or "window_ns" in spec:  # calibrate-live defaults (queuetruth.score.calibrate_live)
        if fam.startswith("power"):
            return -3.0, 4.0, 29
        if fam == "databento":
            return -0.98, 0.9, 25
        if fam.startswith("nt_"):
            return 0.02, 1.0, 50
    return None


def _calibration_flags(doc: Doc) -> list[str]:
    """Reasons not to trust a fitted value: fields the calibration reports (`flags`, `at_grid_edge`,
    `identified`, `plateau`, ...) plus what the numbers show (no events, grid edge, zero-width CI)."""
    flags: list[str] = []
    for f in doc.get("flags") or []:
        flags.append(str(f))
    est = doc.get("estimate")
    pn = doc.get("param_name") or "estimate"
    fam = str(doc.get("family") or "")
    if doc.get("kind") != "latency":
        if not doc.get("n_events"):
            flags.append("fitted on 0 events")
        if est is None:
            flags.append("no estimate")
    if doc.get("identified") is False:
        flags.append("not identified (flat likelihood)")
    plateau = doc.get("plateau")
    if isinstance(plateau, list | tuple) and len(plateau) == 2:
        flags.append(f"likelihood plateau over {pn} in [{_num(plateau[0])}, {_num(plateau[1])}]")
    edge = doc.get("at_grid_edge")
    numeric = isinstance(est, int | float) and not isinstance(est, bool)
    if edge is None and numeric and (est > 0 or not fam.startswith("power")):
        g = _grid_of(doc)
        if g is not None:
            lo, hi, n = g
            theta = math.log(est) if fam.startswith("power") and est > 0 else float(est)
            step = (hi - lo) / max(n - 1, 1)
            if theta <= lo + step / 2:
                edge = "lower"
            elif theta >= hi - step / 2:
                edge = "upper"
    if edge:
        side = f"{edge} " if isinstance(edge, str) else ""
        flags.append(f"at the {side}grid edge ({pn} = {_num(est)}): the optimum may lie outside the grid")
    cb = doc.get("ci_bootstrap")
    if isinstance(cb, list | tuple) and len(cb) == 2 and cb[0] is not None and cb[0] == cb[1]:
        flags.append("zero-width bootstrap CI (degenerate, not precision)")
    cp = doc.get("ci_profile")
    if isinstance(cp, list | tuple) and len(cp) == 2 and (cp[0] is None) != (cp[1] is None):
        flags.append("profile CI open on one side (grid edge)")
    if doc.get("degenerate_ci") and not any("CI" in f for f in flags):
        flags.append("degenerate CI")
    return list(dict.fromkeys(flags))


def _num(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _beats(fitted: float | None, default: float | None) -> str:
    if fitted is None or default is None:
        return "n/a"
    tol = 1e-9 + 1e-3 * max(abs(fitted), abs(default))
    if fitted < default - tol:
        return "yes"
    if fitted > default + tol:
        return "NO"
    return "tie"


def _window_txt(w: Any) -> str:
    if not isinstance(w, dict):
        return "all data"
    if w.get("dates"):
        return ", ".join(w["dates"])
    b = w.get("blocks")
    if isinstance(b, dict):
        return f"blocks {','.join(str(i) for i in b.get('index', []))} of {b.get('n_blocks')}"
    return "all data"


def _render_calibration(doc: Doc, args: Doc, path: list[str]) -> str | None:
    if doc.get("schema") != "qt.calibration_result":
        return None
    title = " ".join(path)
    lines = [f"{title}: {doc.get('family')} (run {doc.get('run_id')})"]
    if doc.get("kind") == "latency":
        lat = doc.get("latency") or {}
        rows = []
        for k in ("entry_ns", "response_ns", "cancel_ns", "amend_ns"):
            q = lat.get(k) or {}
            rows.append(
                [k.removesuffix("_ns"), *(_dur(q.get(p)) for p in ("p10", "p50", "p90", "p99")), str(q.get("n"))]
            )
        lines += _table(["latency", "p10", "p50", "p90", "p99", "n"], rows, right={1, 2, 3, 4, 5})
        lines.append("(one-way latencies assume synchronized clocks)")
        lines += _files_lines(doc)
        return "\n".join(lines)
    pn = doc.get("param_name") or "estimate"
    est = doc.get("estimate")
    cis = []
    if isinstance(doc.get("ci_profile"), list | tuple):
        lo, hi = doc["ci_profile"]
        cis.append(f"profile 95% CI {_num(lo)} to {_num(hi)}")
    if isinstance(doc.get("ci_bootstrap"), list | tuple):
        lo, hi = doc["ci_bootstrap"]
        cis.append(f"bootstrap {_num(lo)} to {_num(hi)}")
    lines.append(
        f"fitted: {pn} = {_num(est)}"
        + (f" ({'; '.join(cis)})" if cis else "")
        + f" on {_window_txt(doc.get('train'))}: {doc.get('n_events')} events, {doc.get('n_shadows')} "
        + ("live orders" if doc.get("kind") == "live" or (doc.get("spec") or {}).get("live") else "shadows")
    )
    held = doc.get("held_out") or {}
    base = doc.get("baseline_held_out") or {}
    fit_id = next(iter(held), None)
    base_id = next(iter(base), None)
    verdict_bits: list[str] = []
    loses: list[str] = []
    wins: list[str] = []
    ties: list[str] = []
    explicit = doc.get("beats_default")
    trust = _trust(doc)
    lines_kv = _trust_lines(doc)
    labels = {str(h.horizon_ns): _TRUST_LABEL.get(h.label, h.label) for h in trust.horizons} if trust else {}
    for code in lines_kv.get("beats_default", "").split(" (", 1)[0].split(","):  # the calibration's own record
        h, _, label = code.strip().partition("=")
        if h.isdecimal() and label:
            labels[h] = _TRUST_LABEL.get(label, label)
    if fit_id and base_id:
        f_ll = held[fit_id].get("logloss") or {}
        b_ll = base[base_id].get("logloss") or {}
        f_b = held[fit_id].get("fill_rate_bias") or {}
        b_b = base[base_id].get("fill_rate_bias") or {}
        keys = sorted({*f_ll, *b_ll}, key=int)
        lines.append(
            f"held-out {_window_txt(doc.get('test'))}: fitted {fit_id} vs default {base_id} (log loss: lower is better)"
        )
        rows = []
        for h in keys:
            b = labels.get(h) or _beats(f_ll.get(h), b_ll.get(h))
            if isinstance(explicit, dict) and h in explicit and explicit[h] is not None:
                b = "yes" if explicit[h] else "NO"
            (wins if b == "yes" else loses if b == "NO" else ties if b == "tie" else []).append(_hz(h))
            rows.append([_hz(h), _num(f_ll.get(h)), _num(b_ll.get(h)), _pp(f_b.get(h)), _pp(b_b.get(h)), b])
        lines += _table(
            ["horizon", "log loss fitted", "default", "bias fitted (pp)", "default (pp)", "beats default"],
            rows,
            right={1, 2, 3, 4},
        )
    else:
        lines.append("held-out: no comparison (no test data or no estimate)")
    hd = doc.get("held_out_discordance")
    if isinstance(hd, dict):
        lines.append(
            f"held-out live orders: fitted model fill-rate bias {_pp(hd.get('fill_rate_bias'))} pp (95% CI "
            f"{_pp(hd.get('ci_lo'))} to {_pp(hd.get('ci_hi'))}), cluster-robust p = {_p(hd.get('p_clustered'))}, "
            f"{hd.get('n')} orders, {hd.get('n_clusters')} clusters"
        )
    flags = _calibration_flags(doc)
    if trust is not None or lines_kv:
        own = {k: v for k, v in (trust.flags if trust is not None else lines_kv).items()}
        own = {k: v for k, v in {**own, **lines_kv}.items() if k not in ("beats_default", "verdict")}
        if "at_grid_edge" in own:
            flags = [f for f in flags if "grid edge" not in f]
        if "degenerate_ci" in own:
            flags = [f for f in flags if "zero-width" not in f]
        flags = [f"{k.replace('_', ' ')}: {v}" if v else k.replace("_", " ") for k, v in own.items()] + flags
    lines.append("flags: " + ("; ".join(flags) if flags else "none"))
    if isinstance(explicit, bool):
        (wins if explicit else loses).append("held-out")
    recorded = lines_kv.get("verdict")
    if recorded is not None and recorded.startswith("refused"):
        texts = "; ".join(text for _, text in trust.reasons) if trust is not None and trust.reasons else recorded
        verdict_bits.append(f"DO NOT USE (export refuses it: {recorded.removeprefix('refused').strip(' ()')}): {texts}")
    elif recorded is not None and recorded.startswith("exportable"):
        won = f": beats the default at {', '.join(wins)}" if wins else ""
        verdict_bits.append(f"exportable{won} (export hftbacktest|nautilus will write it)")
    elif trust is not None and not trust.ok:
        verdict_bits.append("DO NOT USE (export refuses it): " + "; ".join(text for _, text in trust.reasons))
    elif trust is not None and wins:
        verdict_bits.append(f"exportable: beats the default at {', '.join(wins)} and never loses")
    elif flags:
        verdict_bits.append("DO NOT USE: " + flags[0])
    elif loses:
        verdict_bits.append(f"DO NOT USE: the fitted value is worse than the default at {', '.join(loses)}")
    elif wins:
        verdict_bits.append(f"beats the default at {', '.join(wins)} and never loses")
    elif ties:
        verdict_bits.append("no gain: same held-out score as the default; keep the default")
    else:
        verdict_bits.append("not checked out of sample (no held-out data)")
    lines.append("verdict: " + verdict_bits[0])
    ex = doc.get("exports") or {}
    if ex.get("hftbacktest") or ex.get("nautilus"):
        lines.append(
            "exports: hftbacktest and nautilus snippets (queuetruth export hftbacktest|nautilus --calibration)"
        )
    lines += _files_lines(doc)
    return "\n".join(lines)


_TRUST_LABEL = {"better": "yes", "worse": "NO", "tie": "tie", "mixed": "mixed", "n/a": "n/a"}
_TRUST_LINE = re.compile(r"^trust: ([a-z_]+): (.*)$", re.S)


def _trust_lines(doc: Doc) -> dict[str, str]:
    """The calibration's own trust record as `{token: text}`: the `trust: <token>: <text>` lines of its warnings
    (the last line of a token wins), overridden by the structured `trust` field (schema 1.1) when present
    (`verdict` -> `exportable` | `refused (<reasons>)`, `beats_default` -> `<h>=<label>,...`, flag tokens kept with
    their line's text)."""
    out: dict[str, str] = {}
    for w in doc.get("warnings") or []:
        hit = _TRUST_LINE.match(str(w))
        if hit:
            out[hit.group(1)] = hit.group(2)
    st = doc.get("trust")
    if isinstance(st, dict) and st.get("verdict") in ("exportable", "refused"):
        reasons = [str(r) for r in st.get("reasons") or []]
        out["verdict"] = "exportable" if st["verdict"] == "exportable" else f"refused ({', '.join(reasons)})"
        bd = st.get("beats_default") or {}
        if isinstance(bd, dict) and bd:
            out["beats_default"] = ",".join(f"{h}={bd[h]}" for h in sorted(bd, key=int))
        for tok in st.get("flags") or []:
            out.setdefault(str(tok), "")
    return out


def _trust(doc: Doc) -> Any:
    """`queuetruth.export.trust.assess` of the calibration (the export's own verdict), or None when unavailable."""
    try:
        from queuetruth.export.trust import assess
        from queuetruth.schemas.models import CalibrationResult

        return assess(CalibrationResult.model_validate(doc))
    except Exception:  # an older build or a partial document: the view computes its own verdict
        log.debug("trust assessment unavailable", exc_info=True)
        return None


_DQ_MARK = {"pass": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


def _render_dq(doc: Doc, args: Doc) -> str | None:
    """`dq`: the overall status first, then one line per check (status, id, summary) and the report files."""
    checks = doc.get("checks")
    if not isinstance(checks, list):
        return None
    c = doc.get("counts") or {}
    ds = doc.get("dataset") or {}
    lines = [
        f"data quality: {str(doc.get('status', '?')).upper()} ({c.get('pass', 0)} pass, {c.get('warn', 0)} warn, "
        f"{c.get('fail', 0)} fail, {c.get('skip', 0)} skipped) over {ds.get('path')} "
        f"({ds.get('records', 0):,} records, {len(ds.get('session_dates') or [])} date(s), "
        f"{len(ds.get('symbols') or [])} symbol(s))",
        "",
    ]
    width = max((len(str(ch.get("id"))) for ch in checks), default=0)
    for ch in checks:
        mark = _DQ_MARK.get(str(ch.get("status")), "?")
        lines.append(f"  {mark}  {str(ch.get('id')).ljust(width)}  {ch.get('summary')}")
    out = args.get("out")
    if out:
        lines += ["", f"report: {Path(out) / 'dq_report.html'} (JSON: {Path(out) / 'dq_report.json'})"]
    if doc.get("gate_failed"):
        lines.append(f"gate --fail-on {(doc.get('spec') or {}).get('fail_on', 'warn')}: failed (exit 1)")
    return "\n".join(lines)


def _dur(ns: Any) -> str:
    from queuetruth.api import fmt_dur

    return "-" if ns is None else fmt_dur(int(ns))


def render(
    path: list[str],
    doc: Any,
    *,
    args: Doc | None = None,
    extra: Doc | None = None,
    view: Any = None,
    verbose: bool = False,
) -> str:
    """Human output of a command: the answer-first view of zoo / why / score / calibrate (falls back to
    `render_human`), plus the full `render_human` dump with `verbose`."""
    args, extra = args or {}, extra or {}
    key = tuple(path)
    text: str | None = None
    try:
        if key == ("why",) and view is not None:
            text = _render_why(view, verbose)
        elif isinstance(doc, dict) and key == ("zoo",):
            text = _render_zoo(doc, args, extra)
        elif isinstance(doc, dict) and key == ("score",):
            text = _render_score(doc, args, extra)
        elif isinstance(doc, dict) and (key[0] == "calibrate" or key == ("calibrate-live",)):
            text = _render_calibration(doc, args, path)
        elif isinstance(doc, dict) and key == ("dq",):
            text = _render_dq(doc, args)
        elif isinstance(doc, dict) and key == ("zoo-toxicity",):
            from queuetruth.eval.toxicity import render_toxicity

            text = render_toxicity(doc)
    except Exception:  # a view never fails a command that succeeded
        log.debug("human view failed; plain rendering", exc_info=True)
        text = None
    if text is None:
        return render_human(path, doc)
    if verbose and key != ("why",):
        text += "\n\nall fields:\n" + render_human(path, doc)
    return text


#: Summary warnings that inform rather than warn: stderr only with --verbose (or --log-level info|debug).
_NOTE_PREFIXES = ("info: ", "ci_block=", "self_exclusion: matched ")
_ZERO_COUNTERS = re.compile(r"\b\w+=(\d+)\b")


def _is_note(w: str) -> bool:
    if w.startswith(_NOTE_PREFIXES):
        return True
    # replay's shadow diagnostics when every counter is 0
    counts = _ZERO_COUNTERS.findall(w)
    return w.startswith("shadow diagnostics") and bool(counts) and all(int(c) == 0 for c in counts)


def _hbt_312_line(lines: list[str], doc: Doc) -> str:
    """One stderr line for the `hbt_312_abort: <model>: <n> of <N> ...` warnings of a zoo or calibration run."""
    from queuetruth.eval.leaderboard import ABORT_PREFIX, abort_shares

    shares = abort_shares(w[w.index(ABORT_PREFIX) :] for w in lines)
    counts = sorted(n for n, _ in shares.values())
    total = max((t for _, t in shares.values()), default=0)
    span = f"{counts[0]}" if counts and counts[0] == counts[-1] else f"{counts[0]} to {counts[-1]}" if counts else "?"
    excluded = doc.get("n_excluded_hbt_312")
    left_out = (
        f"the {excluded} shadows involved are left out of every model"
        if isinstance(excluded, int) and excluded > 0
        else "the shadows involved are left out"
    )
    return (
        f"warning: hftbacktest bug #312 ({HBT_312_URL}): {len(shares) or len(lines)} faithful hbt model(s) aborted on "
        f"{span} of {total} shadows; {left_out} (--verbose lists each model)"
    )


def stderr_lines(doc: Any, *, verbose: bool) -> list[str]:
    """The stderr lines of a summary's `warnings`: `warning: <w>` each, except that the faithful hbt #312 abort lines
    collapse into one line and informational lines (`info: `, `ci_block=`, `self_exclusion: matched`, all-zero shadow
    diagnostics) are left out; `verbose` prints every line (`info: ` lines as they are). The summary itself always
    keeps every warning."""
    from queuetruth.eval.leaderboard import ABORT_PREFIX

    raw = doc.get("warnings") if isinstance(doc, dict) else None
    ws = [str(w) for w in raw] if isinstance(raw, list) else []
    if verbose:
        return [w if w.startswith("info: ") else f"warning: {w}" for w in ws]
    aborts = [w for w in ws if ABORT_PREFIX in w]
    out: list[str] = []
    for w in ws:
        if ABORT_PREFIX in w:
            if w is aborts[0]:
                out.append(_hbt_312_line(aborts, doc))
        elif not _is_note(w):
            out.append(f"warning: {w}")
    return out


def _wants_json(argv: list[str]) -> bool:
    pairs = itertools.pairwise(argv)
    return any(a in ("--format", "--output-format") and b == "json" for a, b in pairs) or any(
        a in ("--format=json", "--output-format=json") for a in argv
    )


def _core() -> Any:
    from queuetruth import _core as core

    return core


# ---- command handlers -------------------------------------------------------------------------------------------


def _native(cmd: Doc) -> tuple[Doc, bool]:
    from queuetruth.api import _call

    text, gate = _call("cli_run_native", json.dumps(cmd))
    return json.loads(text), bool(gate)


def _split_cli(args: Doc) -> tuple[Doc, Doc]:
    a = dict(args)
    extra = a.pop("_cli", {}) or {}
    return a, extra


def _doctor(cmd: Doc) -> tuple[Doc, bool]:
    native, _ = _native(cmd)
    problems = list(native.get("problems", []))
    core = _core()
    from queuetruth._version import __version__
    from queuetruth.schemas import arrow as qa

    core_version = core.version()
    if core_version != __version__:
        problems.append(f"queuetruth._core {core_version} does not match the Python package {__version__}")
    schemas_ok = bool(native.get("schemas_ok", False))
    for name, schema in qa.SCHEMAS.items():
        try:
            if not core.arrow_schema(name).equals(schema, check_metadata=True):
                schemas_ok = False
                problems.append(f"Arrow schema {name}: Rust and Python definitions differ")
        except KeyError:
            schemas_ok = False
            problems.append(f"Arrow schema {name}: unknown to the Rust core")
    from queuetruth.schemas.export import artifacts

    for rel, text in sorted(artifacts().items()):
        if not (rel.startswith("schemas/json/") and rel.endswith(".schema.json")):
            continue
        name = Path(rel).name.removesuffix(".schema.json")
        show = {**cmd, "path": ["schema", "show"], "args": {"name": name}, "implementation": "native"}
        try:
            embedded, _ = _native(show)
        except QtError:
            embedded = None
        if embedded != json.loads(text):
            schemas_ok = False
            problems.append(f"JSON Schema {name}: the Rust core's embedded copy differs from the Pydantic model")
    postgres = None
    if os.environ.get("QT_PG_DSN"):
        postgres = "QT_PG_DSN set"
    else:
        pg_ctl = Path(os.environ.get("QT_PG_BIN", "/opt/homebrew/opt/postgresql@16/bin")) / "pg_ctl"
        if pg_ctl.exists():
            postgres = _run_version([str(pg_ctl), "--version"]) or "pg_ctl found"
    docker = None
    if shutil.which("docker"):
        v = _run_version(["docker", "info", "--format", "{{.ServerVersion}}"])
        docker = f"daemon {v}" if v else "installed, daemon not running"
    doc = {
        "schema": "qt.doctor",
        "schema_version": 1,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "core_version": core_version,
        "schemas_ok": schemas_ok,
        "postgres": postgres,
        "docker": docker,
        "problems": problems,
    }
    return doc, bool(problems)


def _run_version(argv: list[str]) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return out.splitlines()[0] if r.returncode == 0 and out else None


def _live_validate(args: Doc) -> tuple[Doc, bool]:
    from queuetruth import live as qlive

    doc = qlive.check(args["live"]).model_dump(mode="json", by_alias=True)
    if doc["errors"]:
        try:
            qlive.validate(args["live"])
        except QtError as e:
            e.details.setdefault("validation", doc)
            raise
        raise QtError(
            "QT-INPUT-002", f"{args['live']}: {len(doc['errors'])} validation error(s)", details={"validation": doc}
        )
    return doc, False


def _jobs(path: list[str], args: Doc) -> tuple[Doc, bool]:
    from queuetruth.client import TERMINAL_JOB_STATES, Client, job_error

    sub = path[1]
    timeout = args.get("timeout")
    client = Client()
    try:
        if sub == "submit":
            spec_path = Path(args["spec"])
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
            except OSError as e:
                raise QtError("QT-INPUT-001", f"{spec_path}: {e}") from None
            except json.JSONDecodeError as e:
                raise QtError("QT-INPUT-002", f"{spec_path}: invalid JSON: {e}") from None
            job = client.jobs.submit(args["kind"], spec, idempotency_key=args.get("idempotency_key"))
            if args.get("wait"):
                job = client.jobs.wait(job.id, timeout=timeout)
            if job.state in TERMINAL_JOB_STATES and job.state != "succeeded":
                raise job_error(job)
            return job.model_dump(mode="json", by_alias=True), False
        job_id = args["id"]
        if sub in ("status", "wait"):
            job = client.jobs.get(job_id) if sub == "status" else client.jobs.wait(job_id, timeout=timeout)
            if job.state in TERMINAL_JOB_STATES and job.state != "succeeded":
                raise job_error(job)
            return job.model_dump(mode="json", by_alias=True), False
        manifest = client.jobs.files(job_id)
        if sub == "download":
            client.jobs.download_all(job_id, args.get("out") or ".")
        return manifest.model_dump(mode="json", by_alias=True), False
    finally:
        client.close()


def _not_scored(doc: Doc) -> QtError | None:
    """`QT-INPUT-002` for a scorecard that scored nothing (no live order matched a simulator outcome): exit 2 with
    the reasons, never a success (the scorer's own error, when it raises one, comes first)."""
    n_orders, n_matched = doc.get("n_orders"), doc.get("n_matched")
    if not isinstance(n_matched, int) or n_matched > 0:
        return None
    reasons = [str(w) for w in doc.get("warnings") or [] if isinstance(w, str)]
    return QtError(
        "QT-INPUT-002",
        f"NOT SCORED: 0 of {n_orders} live orders could be scored"
        + (f" ({'; '.join(reasons[:3])})" if reasons else ""),
        "check the live log's instrument_id (live import --instrument-map) and order types: market, IOC and FOK "
        "orders and orders without an entry time are not scheduled",
        details={"summary": doc},
    )


def _calibration_refused(doc: Doc) -> QtError | None:
    """`QT-INPUT-002` for a calibration that fitted nothing (no events, no estimate); exit 2, never a success. A
    fitted calibration whose `trust.verdict` is `refused` is a result, not an error: `export` refuses it
    (`QT-CALIB-001`, 5.10)."""
    if doc.get("kind") == "latency":
        lat = (doc.get("latency") or {}).get("entry_ns") or {}
        if not lat.get("n"):
            return QtError("QT-INPUT-002", "calibrate latency: no order with an entry latency in the live log")
        return None
    if not doc.get("n_events") or doc.get("estimate") is None:
        train = _window_txt(doc.get("train"))
        return QtError(
            "QT-INPUT-002",
            f"calibration fitted on {doc.get('n_events') or 0} events (train window {train}): nothing to export",
            "check --train / --test against the dataset's session dates and that the shadows fall in them",
            details={"calibration": doc},
        )
    return None


def _refusal_details(e: QtError, args: Doc) -> None:
    """Adds the refusal reasons of an export refusal (`QT-CALIB-001`) to its `qt.error` details:
    `reasons` (tokens), `reason_texts` ({token: text}) and `beats_default` ({horizon_ns: true|false|null})."""
    try:
        from queuetruth.export import load_calibration
        from queuetruth.export.trust import assess

        t = assess(load_calibration((args.get("calibration") or {}).get("path", "")))
    except Exception:  # the error itself is what matters; details are a bonus
        log.debug("refusal details unavailable", exc_info=True)
        return
    e.details.setdefault("reasons", [k for k, _ in t.reasons])
    e.details.setdefault("reason_texts", dict(t.reasons))
    e.details.setdefault("beats_default", t.beats_default)


def _missing_extra(e: ImportError) -> bool:
    """The import failed on a third-party module of an optional extra (not on queuetruth's own code)."""
    return not (e.name or "").startswith("queuetruth")


def _dispatch(cmd: Doc, ctx: Doc | None = None) -> tuple[Doc | None, bool]:
    """Runs a parsed command: (summary document or None, gate failed). `ctx["view"]` receives an object for the
    human view (why), `ctx["notes"]` caveats for stderr."""
    from queuetruth import api

    ctx = ctx if ctx is not None else {}
    path: list[str] = cmd["path"]
    imp = cmd["implementation"]
    args, extra = _split_cli(cmd["args"])
    key = tuple(path)
    if key == ("why",):
        x = api.why_explain(args, model=extra.get("model"), zoo=extra.get("zoo"))
        ctx["view"] = x
        ctx.setdefault("notes", []).extend(x.notes)
        return _plain(x.trace), False
    if imp == "native":
        from queuetruth.ledger import run_native  # trial ledger: records `shadows sample`

        return run_native(cmd, _native)
    if key == ("doctor",):
        return _doctor(cmd)
    if key == ("shadows", "from-live"):
        return _plain(
            api.shadows_from_live(
                args["live"], args["out"], entry=args.get("entry", "exchange_ack"), latency_ns=args.get("latency_ns")
            )
        ), False
    if key == ("zoo",):
        if extra.get("exclude_order_ids") is not None:  # --exclude-order-ids FILE -> ZooSpec.exclude_order_ids
            dataset = (args.get("dataset") or {}).get("path", "")
            args = {**args, "exclude_order_ids": api.resolve_order_ids(extra["exclude_order_ids"], dataset)}
        return _plain(api.run_zoo(args)), False
    if key == ("zoo-toxicity",):
        from queuetruth.eval.toxicity import run_zoo_toxicity

        return _plain(run_zoo_toxicity(args)), False
    if key[0] == "calibrate" or key == ("calibrate-live",):
        cal = _plain(api.calibrate(args) if key[0] == "calibrate" else api.calibrate_live(args))
        err = _calibration_refused(cal)
        if err is not None:
            raise err
        return cal, False
    if key == ("fit-hazard",):
        after = None
        if extra.get("conformal_alpha") is not None:  # --conformal-alpha A: conformal.json next to the model
            from queuetruth.model.conformal import conformal_step

            after = conformal_step(args, extra["conformal_alpha"])  # checks alpha, --out, features before the fit
        card = _plain(api.fit_hazard(args))
        if after is not None:
            files, lines = after(card)
            card["files"] = [*card.get("files", []), *files]  # the printed card lists the conformal files too
            ctx.setdefault("notes", []).extend(lines)
        return card, False
    if key == ("live", "import"):
        return _plain(api.import_live(args, tz=extra.get("tz"), **(extra.get("options") or {}))), False
    if key == ("live", "validate"):
        return _live_validate(args)
    if key == ("score",):
        kw = {k: v for k, v in extra.items() if k in ("window_ns", "match_window_ns", "self_exclude")}
        summary = api.score(args, **kw)
        doc = _plain(summary)
        err = _not_scored(doc)
        if err is not None:
            raise err
        return doc, not summary.verdict.gates_passed
    if key == ("report",):
        return _plain(api.report(args)), False
    if key == ("dq",):
        rep = api.dq(args)
        return _plain(rep), bool(rep.gate_failed)
    if key == ("referee", "run"):
        rep = api.referee(args)
        return _plain(rep), not bool(rep.pass_)
    if key == ("export", "replay-schedule"):
        refs = {k: (args.get(k) or {}).get("path") for k in ("schedule", "amends", "live", "dataset")}
        res = api.export_replay_schedule(args["target"], out=args["out"], entry=args.get("entry"), **refs)
        return _plain(res), False
    if key[0] == "export":
        try:
            return _plain(api.export(args, force=bool(extra.get("force")))), False
        except QtError as e:
            if e.code == "QT-CALIB-001":
                _refusal_details(e, args)
            raise
    if key[0] == "jobs":
        try:
            import queuetruth.client  # noqa: F401
        except ImportError as e:
            if not _missing_extra(e):
                raise
            raise QtError("QT-USAGE-001", 'jobs needs the client extra: pip install "queuetruth[client]"') from None
        return _jobs(path, args)
    if key == ("mcp", "serve"):
        try:
            from queuetruth.mcp.server import serve
        except ImportError as e:
            if not _missing_extra(e):
                raise
            raise QtError("QT-USAGE-001", 'mcp serve needs the MCP extra: pip install "queuetruth[mcp]"') from None
        # `--allow-run` turns run mode on; without it serve() reads QT_MCP_ALLOW_RUN
        serve(
            allow_submit=bool(args.get("allow_submit")),
            local=bool(args.get("local")),
            allow_run=True if args.get("allow_run") else None,
        )
        return None, False
    raise QtError("QT-USAGE-001", f"unknown command {' '.join(path)!r}")


# ---- entry point ------------------------------------------------------------------------------------------------


def _emit_error(e: QtError, json_out: bool) -> int:
    if json_out:
        print(json.dumps(_plain(e.to_document()), ensure_ascii=False))
    else:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(f"hint: {e.hint}", file=sys.stderr)
    return e.exit_code


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the exit code (0 ok, 1 gate failed, 2 error)."""
    args = list(sys.argv[1:] if argv is None else argv)
    # The Python-only groups run with their own argparse parsers before the clap grammar, which lists them in
    # `--help` (rows generated by crates/cli/gen_python_commands.py). `help G [SUB]` is `G [SUB] --help` for them,
    # so both spellings print the group's own help.
    python_groups = ("demo", "forecast", "ledger", "pm", "certify", "verify-cert", "commit", "reveal", "cert")
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        i += 2 if args[i] in ("--format", "--output-format", "--log-level", "--threads", "--seed") else 1
    if args[i : i + 1] == ["help"] and args[i + 1 : i + 2] and args[i + 1] in python_groups:
        args = [*args[:i], *args[i + 1 :], "--help"]
    from queuetruth.demo._dispatch import maybe_run as _demo  # `queuetruth demo` (Python only, before the grammar)

    if (rc := _demo(args)) is not None:
        return rc
    from queuetruth.forecast.cli import forecast_main  # `forecast ...`: Python-only argparse group

    if (rc := forecast_main(args)) is not None:
        return rc
    from queuetruth.ledger.cli import main_if_ledger  # `queuetruth ledger ...` (not in the Rust grammar)

    if (rc := main_if_ledger(args)) is not None:
        return rc
    from queuetruth.cert.cli import maybe_main  # certify, verify-cert, commit, reveal, cert (Python-only grammar)

    if (rc := maybe_main(args)) is not None:
        return rc
    from queuetruth.pm.cli import run as pm_run  # `queuetruth pm ...`: Python-only group (queuetruth.pm.cli)

    if (rc := pm_run(args)) is not None:
        return rc
    json_out = _wants_json(args)
    try:
        core = _core()
    except ImportError as e:
        return _emit_error(
            QtError("QT-ENGINE-001", f"queuetruth._core is not available: {e}", "reinstall queuetruth"), json_out
        )
    try:
        cmd = json.loads(core.cli_parse(["queuetruth", *args]))
    except core.CliExit as x:
        print(str(x.text).rstrip("\n"), file=sys.stderr if x.to_stderr else sys.stdout)
        return int(x.code)
    g = cmd["global"]
    json_out = g["format"] == "json"
    quiet = bool(g.get("quiet"))
    level = getattr(logging, str(g.get("log_level", "warn")).upper().replace("WARN", "WARNING"), logging.WARNING)
    logging.basicConfig(stream=sys.stderr, level=logging.ERROR if quiet else level, format="%(levelname)s %(message)s")
    ctx: Doc = {}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            doc, gate = _dispatch(cmd, ctx)
        except QtError as e:
            return _emit_error(e, json_out)
        except core.QtCoreError as e:
            from queuetruth.api import _qt_error

            return _emit_error(_qt_error(e), json_out)
        except KeyboardInterrupt:
            return _emit_error(QtError("QT-USAGE-001", "interrupted"), json_out)
        except Exception as e:  # every unexpected failure is QT-ENGINE-001 with exit code 2
            log.debug("unexpected error", exc_info=True)
            return _emit_error(QtError("QT-ENGINE-001", f"{type(e).__name__}: {e}"), json_out)
    if doc is None:
        return EXIT_OK
    doc = _plain(doc)
    if json_out:
        print(json.dumps(doc, ensure_ascii=False, allow_nan=False))
    else:
        args, extra = _split_cli(cmd["args"])
        print(render(cmd["path"], doc, args=args, extra=extra, view=ctx.get("view"), verbose=bool(g.get("verbose"))))
    if not quiet:
        sys.stdout.flush()  # the answer first, then the warnings, also when both go to one pipe
        verbose = bool(g.get("verbose"))
        for w in stderr_lines(doc, verbose=verbose or level <= logging.INFO):
            print(w, file=sys.stderr)
        for w in ctx.get("notes", []):
            print(f"warning: {w}", file=sys.stderr)
        for w in caught:
            print(f"warning: {w.message}", file=sys.stderr)
        if gate:
            print("QT-GATE-001: a requested gate failed", file=sys.stderr)
    return EXIT_GATE if gate else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
