"""Self-contained HTML of a `qt.dq_report`, in the style of `queuetruth report` (same stylesheet, status badges with
icon and label, tables with right-aligned numbers, inline SVG with a data-table fallback). The bytes are a pure
function of the report: no generation time or host name is written."""

from __future__ import annotations

from typing import Any

from queuetruth._version import __version__
from queuetruth.dq.model import DqCheck, DqEvidence, DqReport
from queuetruth.report import fmt
from queuetruth.report.page import (
    CRIT,
    GOOD,
    INFO,
    NEUTRAL,
    WARN,
    Raw,
    Status,
    badge,
    callout,
    figure,
    kv,
    section,
    shell,
    table,
    ul,
)
from queuetruth.report.svg import bar_chart

SKIP = Status(NEUTRAL.cls, NEUTRAL.icon, "SKIP")
STATUS: dict[str, Status] = {"pass": GOOD, "warn": WARN, "fail": CRIT, "skip": SKIP}

#: Evidence columns holding UTC timestamps (ns).
TS_COLUMNS = {
    "ts_event",
    "ts_recv",
    "previous_ts_event",
    "ts_before",
    "ts_after",
    "ts_start",
    "ts_end",
    "first_ts",
    "last_ts",
    "open_ts",
    "close_ts",
    "quiet_from",
    "quiet_to",
    "start_ns",
    "end_ns",
}
#: Evidence columns holding durations (ns).
DUR_COLUMNS = {"back_ns", "latency_ns", "quiet_ns"}


def _cell(col: str, v: Any) -> str:
    if v is None:
        return fmt.DASH
    if col in TS_COLUMNS and fmt.is_num(v):
        return fmt.ts(v)
    if col in DUR_COLUMNS and fmt.is_num(v):
        return fmt.dur(v)
    if isinstance(v, float):
        return fmt.num(v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _metric(k: str, v: Any) -> str:
    if v is None:
        return fmt.DASH
    if isinstance(v, bool):
        return "yes" if v else "no"
    if fmt.is_num(v) and k.endswith("_ns"):
        return fmt.dur(v)
    if isinstance(v, float):
        return fmt.num(v)
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _evidence(ev: DqEvidence) -> Raw:
    numeric = {
        i
        for i, c in enumerate(ev.columns)
        if any(isinstance(r[i], (int, float)) and not isinstance(r[i], bool) for r in ev.rows if i < len(r))
        and c not in TS_COLUMNS
    }
    rows = [[_cell(c, r[i] if i < len(r) else None) for i, c in enumerate(ev.columns)] for r in ev.rows]
    shown = f"{len(ev.rows):,} of {ev.total:,}" if ev.truncated else f"{ev.total:,}"
    cap = f"{ev.name.replace('_', ' ')}: {shown} row(s)"
    return table(ev.columns, rows, numeric=numeric, caption=cap)


def _latency_chart(c: DqCheck) -> Raw:
    hist = next((e for e in c.evidence if e.name == "latency_histogram"), None)
    if hist is None or not any(r[1] for r in hist.rows):
        return Raw("")
    cats = [str(r[0]) for r in hist.rows]
    vals = [float(r[1]) for r in hist.rows]
    svg = bar_chart(
        "dq-lat",
        "Feed latency histogram",
        "Records per ts_recv minus ts_event bucket",
        cats,
        [("records", 1, vals)],
        x_label="ts_recv - ts_event",
        y_label="records",
        width=760,
    )
    return figure(svg, "Records per latency bucket (ts_recv - ts_event, snapshot and BAD_TS_RECV records excluded).")


def _check_section(c: DqCheck) -> Raw:
    st = STATUS[c.status]
    body: list[str] = [f"<p>{badge(st)} {fmt.esc(c.summary)}</p>"]
    if c.metrics:
        body.append(kv((k.replace("_", " "), _metric(k, v)) for k, v in c.metrics.items()))
    if c.id == "timestamps":
        body.append(_latency_chart(c))
    if c.notes:
        body.append(callout(INFO, "Notes", ul(c.notes)))
    for ev in c.evidence:
        if ev.name == "latency_histogram":
            body.append(f"<details><summary>Latency histogram data</summary>{_evidence(ev)}</details>")
            continue
        body.append(f"<h3>{fmt.esc(ev.name.replace('_', ' ').capitalize())}</h3>")
        body.append(_evidence(ev))
    return section(f"check-{c.id}", f"{c.title}", body)


def render_html(rep: DqReport, title: str | None = None) -> str:
    """The HTML page of a report."""
    d = rep.dataset
    title = title or f"Data quality: {d.path.rstrip('/').rsplit('/', 1)[-1]}"
    st = STATUS[rep.status]
    counts = rep.counts
    head = [
        f"<h1>{fmt.esc(title)}</h1>",
        f'<p class="sub">queuetruth dq over <code>{fmt.esc(d.path)}</code>: {fmt.esc(d.source_format or "?")} '
        f"{fmt.esc(d.kind or '?')} data, venue {fmt.esc(d.venue or '?')}, "
        f"{len(d.session_dates)} session date(s), {len(d.symbols)} symbol(s)</p>",
    ]
    headline = (
        f"{len(rep.checks)} checks: {counts.pass_} pass, {counts.warn} warn, {counts.fail} fail, {counts.skip} skipped"
    )
    tiles = "".join(
        f'<div class="tile"><div class="t">{fmt.esc(t)}</div><div class="v">{fmt.esc(v)}</div>'
        f'<div class="s">{fmt.esc(s)}</div></div>'
        for t, v, s in (
            ("records", f"{d.records:,}", f"{d.partitions:,} partition(s)"),
            (
                "session dates",
                f"{len(d.session_dates)}",
                ", ".join(d.session_dates[:3]) + (" ..." if len(d.session_dates) > 3 else ""),
            ),
            ("symbols", f"{len(d.symbols)}", ", ".join(d.symbols[:4]) + (" ..." if len(d.symbols) > 4 else "")),
            ("validation status", d.validation_status or "unknown", f"dataset {str(d.dataset_id or '')[:12]}"),
        )
    )
    gate = f"exit gate --fail-on {rep.spec.get('fail_on', 'warn')}: " + (
        "FAILED (exit 1)" if rep.gate_failed else "passed (exit 0)"
    )
    verdict = (
        f'<div class="verdict"><div class="kicker">{badge(st)} '
        f'{fmt.esc(gate)}</div><div class="headline">{fmt.esc(headline)}</div>'
        f'<div class="tiles">{tiles}</div></div>'
    )
    rows = [
        [
            badge(STATUS[c.status]),
            Raw(f'<a href="#check-{fmt.esc(c.id)}">{fmt.esc(c.title)}</a>'),
            Raw(f"<code>{fmt.esc(c.id)}</code>"),
            c.summary,
        ]
        for c in rep.checks
    ]
    overview = section(
        "overview",
        "Checks",
        [verdict, table(["Status", "Check", "Id", "Result"], rows)],
    )
    parts = [*head, overview]
    if rep.compare is not None:
        cmp = rep.compare
        parts.append(
            section(
                "compare",
                "Compared dataset",
                [
                    kv(
                        [
                            ("path", cmp.path),
                            ("source format", cmp.source_format or fmt.DASH),
                            ("venue", cmp.venue or fmt.DASH),
                            ("records", f"{cmp.records:,}"),
                            ("session dates", ", ".join(cmp.session_dates) or fmt.DASH),
                            ("dataset id", cmp.dataset_id or fmt.DASH),
                        ]
                    )
                ],
            )
        )
    if rep.warnings:
        parts.append(callout(WARN, "Warnings", ul(rep.warnings)))
    parts.extend(_check_section(c) for c in rep.checks)
    toc = "".join(f'<a href="#check-{fmt.esc(c.id)}">{fmt.esc(c.id)}</a>' for c in rep.checks)
    parts.insert(2, f'<nav class="toc" aria-label="Checks">{toc}</nav>')
    parts.append(
        f"<footer>Generated by queuetruth {fmt.esc(__version__)} (queuetruth dq). Deterministic output: the same "
        "dataset and spec give byte-identical HTML; no generation time is recorded. Evidence tables show the first "
        f"{rep.spec.get('max_evidence', 20)} rows per table; dq_report.json holds the same rows. Prices are "
        "integers in each instrument's price_exp; timestamps are UTC.</footer>"
    )
    return shell(title, "\n".join(str(p) for p in parts), f"queuetruth data-quality report: {headline}")


__all__ = ["render_html"]
