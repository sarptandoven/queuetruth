"""Referee section: exact-FIFO regression oracle results (`qt.referee_report`, `referee_report.json` written by
`queuetruth.score.referee.run_referee`) and the bug-class matrix over the 16 referee fixture classes (SPEC 5.8 with
E27). A report belongs to a bug class when its spec's `dataset.path` (or its parent) is a fixture directory
named `NN_<class>`; that directory's `README.txt` is quoted when it resolves from the current directory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from queuetruth.report import fmt as F
from queuetruth.report.ctx import Ctx
from queuetruth.report.load import RunDir
from queuetruth.report.page import CRIT, GOOD, INFO, Raw, badge, callout, kv, section, table
from queuetruth.schemas.models import RefereeReport

#: The referee bug classes (tests/fixtures/referee/<dir>), in order.
BUG_CLASSES: tuple[str, ...] = (
    "01_unrelated_delete_keeps_position",
    "02_level_shrink_caps_queue",
    "03_distinct_trades_each_consume",
    "04_post_trade_book_decides",
    "05_partial_fills_sum_to_cum",
    "06_fill_equal_to_leaves_completes",
    "07_size_increase_loses_priority",
    "08_price_change_loses_priority",
    "09_hidden_print_no_queue",
    "10_trade_through_fills",
    "11_fill_before_cancel_effective_stands",
    "12_hidden_execution_at_price_after_queue",
    "13_execution_without_print",
    "14_off_price_print_not_trade_through",
    "15_cme_same_size_modify_loses_priority",
    "16_crossed_at_live_takes_opposite",
)
_CLASS = re.compile(r"^\d\d_[a-z0-9_]+$")
MAX_README = 1200


def _class_dir(r: RefereeReport) -> tuple[str | None, Path | None]:
    ds = r.spec.get("dataset") if isinstance(r.spec, dict) else None
    p = ds.get("path") if isinstance(ds, dict) else None
    if not isinstance(p, str):
        return None, None
    path = Path(p)
    for cand in (path, path.parent):
        if _CLASS.match(cand.name):
            return cand.name, cand
    return None, None


def _readme(d: Path | None) -> str | None:
    if d is None:
        return None
    f = d / "README.txt"
    try:
        text = f.read_text(encoding="utf-8").strip() if f.is_file() else None
    except (OSError, UnicodeDecodeError):
        return None
    if text and len(text) > MAX_README:
        text = text[: MAX_README - 1] + "…"
    return text


def _pretty(name: str) -> str:
    num, _, rest = name.partition("_")
    return f"{num}. {rest.replace('_', ' ')}"


def _one(run: RunDir, r: RefereeReport) -> list[str]:
    cls, d = _class_dir(r)
    sim = r.simulator.name + (f" {r.simulator.version}" if r.simulator.version else "")
    st = GOOD if r.pass_ else CRIT
    title = _pretty(cls) if cls else run.label
    out = [f"<h3>{F.esc(title)} · simulator {F.esc(sim)} {badge(st, 'pass' if r.pass_ else 'fail')}</h3>"]
    readme = _readme(d)
    if readme:
        out.append(f'<p class="lead">{F.esc(readme)}</p>')
    tol = r.tolerance
    out.append(
        kv(
            [
                ("orders", F.num(r.n_orders)),
                ("filled in both", F.num(r.n_both_filled)),
                ("sim-only fills", F.num(r.discordant.sim_only)),
                ("exact-only fills", F.num(r.discordant.exact_only)),
                ("quantity mismatches", F.num(r.qty_mismatch)),
                ("max |first-fill time error|", F.dur(r.max_abs_ttf_err_ns)),
                ("median |first-fill time error|", F.dur(r.ttf_err_p50_ns)),
                (
                    "tolerance (discordant / qty / ttf)",
                    f"{tol.max_discordant} / {tol.max_qty_mismatch} / {F.dur(tol.max_abs_ttf_err_ns)}",
                ),
            ]
        )
    )
    if r.by_reason:
        out.append(
            table(
                ["exact first-fill reason of disagreeing orders", "orders"],
                [(k, F.num(v)) for k, v in sorted(r.by_reason.items())],
                numeric=(1,),
            )
        )
    if r.examples:
        rows: list[tuple[Any, ...]] = []
        for e in r.examples:
            ex, sm = e.exact or {}, e.sim or {}
            rows.append(
                (
                    e.order_key,
                    F.num(ex.get("filled_qty")),
                    F.ts(ex.get("ts_first_fill")),
                    str(ex.get("first_fill_reason") or F.DASH),
                    str(ex.get("end_state") or F.DASH),
                    F.num(sm.get("filled_qty")),
                    F.ts(sm.get("ts_first_fill")),
                )
            )
        out.append(f"<details><summary>{len(r.examples)} disagreeing order(s)</summary>")
        out.append(
            table(
                [
                    "order",
                    "exact qty",
                    "exact first fill (UTC)",
                    "exact reason",
                    "exact end",
                    "sim qty",
                    "sim first fill (UTC)",
                ],
                rows,
                numeric=(1, 5),
            )
        )
        out.append("</details>")
    return out


def referee(ctx: Ctx) -> Raw | None:
    """Referee reports and the 16-class bug matrix."""
    reps = ctx.runs_with(RefereeReport)
    if not reps:
        return None
    body: list[str] = []
    by_class: dict[str, list[RefereeReport]] = {}
    for _, r in reps:
        cls, _ = _class_dir(r)
        if cls:
            by_class.setdefault(cls, []).append(r)
    if by_class:
        rows, rc = [], []
        for name in [*BUG_CLASSES, *sorted(set(by_class) - set(BUG_CLASSES))]:
            got = by_class.get(name, [])
            if not got:
                rows.append((_pretty(name), "not run", F.DASH, F.DASH))
                rc.append("")
                continue
            ok = all(x.pass_ for x in got)
            sims = ", ".join(sorted({x.simulator.name for x in got}))
            disc = sum(x.discordant.sim_only + x.discordant.exact_only for x in got)
            rows.append((_pretty(name), Raw(badge(GOOD if ok else CRIT, "pass" if ok else "fail")), sims, F.num(disc)))
            rc.append("" if ok else "hl")
        n_run = sum(1 for n in BUG_CLASSES if n in by_class)
        n_fail = sum(1 for n, v in by_class.items() if not all(x.pass_ for x in v))
        body.append(
            callout(
                CRIT if n_fail else (GOOD if n_run == len(BUG_CLASSES) else INFO),
                f"{n_run} of {len(BUG_CLASSES)} bug classes run, {n_fail} failed",
                "<p>Each class is a synthetic scenario with an exact-FIFO answer; a failing class names a specific "
                "simulator bug (see the class description).</p>",
            )
        )
        body.append(table(["bug class", "result", "simulator", "discordant orders"], rows, numeric=(3,), row_cls=rc))
    for run, r in reps:
        body += _one(run, r)
    return section(
        "referee",
        "Referee: exact-FIFO regression oracle",
        body,
        lead="Another simulator's fills compared order by order with the exact FIFO replay of the same schedule.",
    )
