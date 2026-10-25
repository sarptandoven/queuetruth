"""Header, scorecard and data-quality sections (SPEC 5.9 order: header, verdict, data quality, 2x2 + McNemar +
signed bias, discordance by queue decile and liquidity, markouts, time to fill, position over time)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from queuetruth._version import __version__
from queuetruth.report import cols
from queuetruth.report import fmt as F
from queuetruth.report import scoreinfo as si
from queuetruth.report.compute import last_per_bucket
from queuetruth.report.ctx import (
    MAX_POSITION_INSTRUMENTS,
    MAX_POSITION_POINTS,
    MAX_POSITION_TABLE_ROWS,
    MAX_TEST_ROWS,
    Ctx,
)
from queuetruth.report.load import RunDir
from queuetruth.report.page import (
    CRIT,
    GOOD,
    INFO,
    NEUTRAL,
    WARN,
    Raw,
    badge,
    callout,
    details,
    figure,
    kv,
    section,
    table,
    theme_switch,
    ul,
)
from queuetruth.report.svg import Ref, Series, bar_chart, forest_chart, line_chart, time_ticks
from queuetruth.schemas.arrow import TAKER_REASONS as ARROW_TAKER_REASONS
from queuetruth.schemas.models import (
    CalibrationResult,
    DataQuality,
    HazardModelCard,
    ReplaySummary,
    ScorecardSummary,
    ZooSummary,
)

LOW_DISCORDANCE = 20


def _run_label(run: RunDir, doc: Any) -> str:
    rid = getattr(doc, "run_id", None)
    return f"{run.label} (run {rid})" if rid else run.label


def sim_label(s: ScorecardSummary) -> str:
    """The scorecard's simulated arm in words (zoo model, external log or the exact FIFO replay)."""
    k = s.sim_source.kind
    if k == "zoo":
        return f"zoo model {s.sim_source.model_id or '?'}"
    if k == "external":
        return f"external simulator log {s.sim_source.path or ''}".strip()
    return "exact FIFO replay"


# ---- header ------------------------------------------------------------------------------------------------------

#: Element ids of the header's collapsed inputs table and scope-and-limits list (the answer block links to the limits).
INPUTS_ID = "inputs"
LIMITS_ID = "limits"
#: Classes of those two blocks: `prov` makes the print stylesheet print them even when closed.
PROV_CLS = "more prov"


def _limits(ctx: Ctx) -> list[str]:
    items = [
        "Shadow orders never consume liquidity, never move the real book and never see each other; queue interaction "
        "between your own orders is not modelled in v0.1.",
        "Hidden liquidity (price-slid orders, icebergs, implied) is flagged and counted, not modelled; FIFO venues "
        "only; orders resting through an auction or halt are censored.",
        "Exact truth exists only for order-level (MBO) data; L2-only datasets support simulation and markouts only.",
    ]
    if ctx.runs_with(ScorecardSummary):
        items.append(
            "Live and simulated arms share the exchange-time entry of each order; one-way latency figures assume "
            "synchronized clocks. A non-significant McNemar test with few discordant orders is not evidence of "
            "realism: equivalence (TOST) is the claim that supports it."
        )
    if ctx.runs_with(ZooSummary):
        items.append(
            "Zoo models see an L2 view synthesized from the same MBO day; their errors are measured against the exact "
            "FIFO replay of that day, so they isolate queue-model error from data error."
        )
    if ctx.runs_with(CalibrationResult):
        items.append(
            "Profile-likelihood intervals are quasi-likelihood (allocation events are not independent); the bootstrap "
            "interval is the primary CI. Fitted parameters are specific to the venue, feed and depth they were fitted "
            "on."
        )
    if ctx.runs_with(HazardModelCard):
        items.append(
            "C-index and AUC are discrimination only (improper scores); model selection uses RCLL and IPCW "
            "log-loss only."
        )
    return items


def header(ctx: Ctx) -> Raw:
    """Title, provenance banner, integrity check, inputs with sha256, scope and limits."""
    inp = ctx.inputs
    prov = inp.provenance
    level = prov.level()
    srcs = ul([f"{w}: {s}" for w, s in prov.entries]) if prov.entries else Raw("")
    if level == "synthetic":
        banner = callout(
            CRIT,
            "Synthetic or fixture data",
            "<p>At least one data source is <code>synthetic_only</code> or <code>fixture_only</code>. The numbers "
            "below exercise the pipeline; they say nothing about the behaviour of a real venue.</p>" + srcs,
        )
    elif level == "rules":
        banner = callout(
            WARN,
            "Vendor data; venue rules validated on synthetic data only",
            "<p>The data comes from a vendor file, but the venue rule set it was replayed with (for example "
            "<code>cme</code>) has been validated on synthetic data only, not on a real session. The data is real; "
            "what is not yet proven is how faithfully the rules (modify priority, snapshots, implied trades) "
            "reproduce the venue's queue.</p>" + srcs,
        )
    elif level == "assumed":
        banner = callout(
            WARN,
            "Venue semantics assumed",
            "<p>At least one data source uses venue rules marked <code>assumed</code> (not validated against a "
            "reference implementation on real data).</p>" + srcs,
        )
    elif level == "real":
        banner = callout(
            GOOD,
            "Real data",
            "<p>Every data source with a recorded status is <code>real_data</code> (venue semantics validated on real "
            "data).</p>" + srcs,
        )
    else:
        banner = callout(
            WARN,
            "Data provenance unknown",
            "<p>No dataset manifest, calibration <code>data_sources</code> or live-log status could be read from the "
            "inputs. Dataset paths in the spec echoes are resolved from the current directory; run the report where "
            "the analyses ran, or include the dataset's <code>manifest.json</code> in an input directory.</p>",
        )
    n_files = len(inp.files)
    parts: list[str] = [
        '<header id="header" class="top">',
        f'<div class="ttl"><h1>{F.esc(ctx.spec.title)}</h1>',
        f'<p class="sub">queuetruth {F.esc(__version__)} · {len(inp.given)} input(s) · {len(inp.runs)} output '
        f"director{'y' if len(inp.runs) == 1 else 'ies'} · {n_files} files read</p></div>",
        theme_switch(),
        f'<div class="wide">{banner}',
    ]
    if inp.integrity:
        parts.append(
            callout(
                CRIT,
                "Files changed after their summary was written",
                "<p>The sha256 of these files differs from the value recorded in the summary that produced them; the "
                "report shows the current contents.</p>" + ul(inp.integrity),
            )
        )
    rows = [
        (
            f.path,
            f.role,
            f.schema,
            F.num(f.rows) if f.rows is not None else F.DASH,
            F.bytes_h(f.bytes),
            Raw(f'<span class="mono">{F.esc(f.sha256)}</span>'),
        )
        for f in inp.files
    ]
    # provenance and limits are one click away on screen, not the first screen (answer-first layout); print always
    # carries them (class `prov`: the print stylesheet opens these blocks; `print-prov` is the fallback copy for
    # browsers without `::details-content`, hidden everywhere else)
    parts.append(
        details(
            "Inputs",
            [table(["file", "role", "schema", "rows", "size", "sha256"], rows, numeric=(3, 4), cls="files")],
            note=f"{n_files} file(s) with sha256",
            summary_html="<h2>Inputs</h2>",
            cls=PROV_CLS,
            sid=INPUTS_ID,
        )
    )
    limits = _limits(ctx)
    parts.append(
        details(
            "Scope and limits",
            [ul(limits)],
            note=f"{len(limits)} statements",
            summary_html="<h2>Scope and limits</h2>",
            cls=PROV_CLS,
            sid=LIMITS_ID,
        )
    )
    fallback = [f"{f.path} · sha256 {f.sha256}" for f in inp.files]
    parts.append(
        '<div class="print-prov" hidden><p><strong>Inputs (sha256)</strong></p>'
        f"{ul(fallback, cls='mono')}<p><strong>Scope and limits</strong></p>{ul(limits)}</div>"
    )
    parts.append("</div></header>")
    return Raw("".join(parts))


# ---- verdict -----------------------------------------------------------------------------------------------------


def _tile(title: str, value: str, sub: str = "") -> str:
    s = f'<div class="s">{F.esc(sub)}</div>' if sub else ""
    return f'<div class="tile"><div class="t">{F.esc(title)}</div><div class="v">{F.esc(value)}</div>{s}</div>'


def _clustered_sub(cl: si.Clustered | None) -> str:
    if cl is None:
        return "not run by the scorer"
    method = si.METHOD_LABEL.get(cl.method or "", cl.method or "?")
    return f"{method}; {F.num(cl.n_clusters)} clusters, design effect {F.num(cl.design_effect, 3)}"


def verdict(ctx: Ctx) -> Raw | None:
    """Scorecard headline cards: signed bias, the cluster-robust primary discordance test (A10), plain McNemar as
    secondary, coverage, equivalence; fills-only sources show the blocked statistics as not computable."""
    runs = ctx.runs_with(ScorecardSummary)
    if not runs:
        return None
    body: list[str] = []
    for run, s in runs:
        b = s.bias
        unscored = si.not_scored(s)
        fo = si.fills_only(s) or unscored
        cl = si.clustered(run, s)
        gated = si.gates_configured(s)
        st = (GOOD if s.verdict.gates_passed else CRIT) if gated else NEUTRAL
        gate_label = ("Gates passed" if s.verdict.gates_passed else "Gates failed") if gated else "No gates configured"
        direction = {
            "sim_overfills": "sim overfills",
            "sim_underfills": "sim underfills",
            "no_significant_bias": "no significant bias",
        }[b.direction]
        dc = s.mcnemar.b + s.mcnemar.c
        nc = "not computable: no order scored" if unscored else si.NOT_COMPUTABLE
        tiles = [
            _tile(
                "Signed fill-rate bias (sim − live)",
                nc if fo else F.pp(b.fill_rate_bias),
                "" if fo else f"95% block-bootstrap CI {F.ci(b.ci_lo, b.ci_hi, F.pp)}",
            ),
            _tile(
                "Discordance test, cluster-robust (primary)",
                nc if fo else F.pval(cl.p if cl else None),
                "" if fo else _clustered_sub(cl),
            ),
            _tile(
                "McNemar exact p (secondary)",
                nc if fo else F.pval(s.mcnemar.p_exact),
                "" if fo else f"assumes independent orders; {s.mcnemar.c:,} sim-only vs {s.mcnemar.b:,} live-only",
            ),
            _tile(
                "Orders scored",
                F.num(s.n_matched),
                f"{s.n_unmatched_live:,} live / {s.n_unmatched_sim:,} sim unmatched; fills within "
                f"{si.window_text(s)} of entry",
            ),
            _tile(
                "Fill-rate equivalence",
                nc if fo else ("yes" if s.equivalence.fill_rate_equivalent else "no"),
                ""
                if fo
                else f"margin ±{F.fixed(100 * s.equivalence.fill_rate_margin, 1)} pp, TOST p "
                f"{F.pval(s.equivalence.tost_p)}",
            ),
        ]
        parts = [
            '<div class="verdict">',
            f'<div class="kicker">Scorecard · {F.esc(_run_label(run, s))} · sim arm: {F.esc(sim_label(s))}</div>',
            f'<p class="headline">{F.esc(si.headline_text(s))}</p>',
            f"<p>{badge(st, gate_label)} " + ("" if fo else f"{badge(INFO, direction)}") + "</p>",
            f'<div class="tiles">{"".join(tiles)}</div>',
        ]
        if unscored:
            parts.append(
                callout(
                    CRIT,
                    f"NOT SCORED: 0 of {s.n_orders:,} live orders scored",
                    "<p>No live order reached the simulator comparison (see the not_scheduled / schedule warnings "
                    "below for the count per reason); every statistic of this scorecard is empty.</p>",
                )
            )
        if si.blocked_by_own_orders(s):
            parts.append(
                callout(
                    CRIT,
                    "BLOCKED: your own resting orders are still in the L2 depth",
                    "<p>The queue models ran on visible depth that still holds your own orders, so each shadow queues "
                    "behind its own live twin and the simulator arm underfills by construction. The bias and tests "
                    "below are not a property of the simulator. Re-run with self_exclude = true (the default) so "
                    "the scorer removes your orders from the depth.</p>",
                )
            )
        if fo and not unscored:
            parts.append(
                callout(
                    WARN,
                    "Fills-only live source",
                    f"<p>The live log carries fills but not the orders that never filled, so fill-rate bias, McNemar "
                    f"and the cluster-robust discordance test, the strata tests, equivalence and the fill CIFs are "
                    f"{nc}. Markouts and time to fill conditional on a fill are still shown.</p>",
                )
            )
        if s.verdict.failed_gates:
            parts.append(callout(CRIT, "Failed gates", ul(s.verdict.failed_gates)))
        tm = si.twin_matching(s)
        if tm is not None and tm.rate is not None and tm.rate < 0.95:
            parts.append(
                callout(
                    WARN,
                    f"Self-exclusion matched only {F.pct(tm.rate)} of live orders to their MBO twins",
                    f"<p>{F.num(tm.n_unmatched)} live order(s) have no twin in the tape, so their shadows may sit "
                    "behind the real order itself and underfill.</p>",
                )
            )
        if not fo and dc < LOW_DISCORDANCE:
            parts.append(
                callout(
                    WARN,
                    f"Low power: only {dc} discordant order(s)",
                    "<p>With so few orders where the arms disagree, a non-significant test does not show the simulator "
                    "is realistic; rely on the equivalence result and the CI width.</p>",
                )
            )
        if not fo and cl is not None and cl.n_clusters is not None and cl.n_clusters < 5:
            parts.append(
                callout(
                    WARN,
                    f"Only {cl.n_clusters} cluster(s)",
                    "<p>Orders on the same day (or in the same 30-minute block) are not independent; with fewer "
                    "than 5 clusters the primary p-value comes from the block bootstrap and is coarse.</p>",
                )
            )
        if s.n_overlap_same_px:
            parts.append(
                callout(
                    INFO,
                    f"{s.n_overlap_same_px:,} order(s) overlapped another own order at the same price and side",
                    "<p>Own-order queue interaction is not modelled; these orders may be scored optimistically.</p>",
                )
            )
        if s.warnings:
            parts.append(callout(WARN, "Scorecard warnings", ul(s.warnings)))
        parts.append("</div>")
        body.append("".join(parts))
    return section("verdict", "Verdict", body)


# ---- data quality ------------------------------------------------------------------------------------------------

_DQ_FIELDS = [
    "records",
    "maybe_bad_book_records",
    "bad_ts_recv_records",
    "snapshot_records",
    "pre_snapshot_rows_skipped",
    "unknown_order_refs",
    "duplicate_adds",
    "unexpected_modifies",
    "sequence_gaps",
    "sequence_regressions",
    "out_of_order_ts_event",
    "crossed_books_after_last",
    "hidden_prints",
    "off_price_executions",
]


def _dq_block(ctx: Ctx, label: str, dq: DataQuality) -> list[str]:
    st = {"clean": GOOD, "warnings": WARN, "unusable": CRIT}[dq.verdict]
    out = [f"<h3>{F.esc(label)}</h3>"]
    body = ul(dq.reasons) if dq.reasons else Raw("")
    out.append(callout(st, f"Data-quality verdict: {dq.verdict}", body))
    rows, rc = [], []
    for name in _DQ_FIELDS:
        v = getattr(dq, name)
        rows.append((name, F.num(v)))
        rc.append("hl" if name != "records" and name != "snapshot_records" and v else "")
    for k in sorted(dq.source_specific):
        rows.append((f"source: {k}", F.num(dq.source_specific[k])))
        rc.append("hl" if dq.source_specific[k] else "")
    out.append(table(["counter", "value"], rows, numeric=(1,), row_cls=rc))
    if dq.feed_gaps:
        gaps = [
            (g.instrument_id, F.ts(g.ts_start), F.ts(g.ts_end), F.dur(g.ts_end - g.ts_start), g.reason)
            for g in dq.feed_gaps[:50]
        ]
        out.append(f"<details><summary>{len(dq.feed_gaps):,} feed gap(s)</summary>")
        out.append(table(["instrument", "start (UTC)", "end (UTC)", "length", "reason"], gaps, numeric=(0, 3)))
        out.append("</details>")
    return out


def _replay_block(ctx: Ctx, r: ReplaySummary) -> list[str]:
    out = [
        kv(
            [
                ("records", F.num(r.stats.records)),
                ("events", F.num(r.stats.events)),
                ("trades", F.num(r.stats.trades)),
                ("instruments", F.num(r.stats.instruments)),
                ("partitions", F.num(r.stats.partitions)),
                ("engine", r.engine_version),
            ]
        )
    ]
    inv = r.invariants.model_dump()
    rows = [(k, F.num(v)) for k, v in inv.items()]
    out.append(
        table(["book invariant", "violations"], rows, numeric=(1,), row_cls=["hl" if v else "" for v in inv.values()])
    )
    if r.parity is not None:
        p = r.parity
        st = GOOD if p.pass_ else CRIT
        out.append(
            callout(
                st,
                f"Parity against source data: {'pass' if p.pass_ else 'FAIL'}",
                kv(
                    [
                        ("trades compared", F.num(p.trades_compared)),
                        ("trade mismatches", F.num(p.trades_mismatches)),
                        ("missing derived", F.num(p.trades_missing_derived)),
                        ("extra derived", F.num(p.trades_extra_derived)),
                        ("BBO rows compared", F.num(p.bbo_compared)),
                        ("BBO mismatches", F.num(p.bbo_mismatches)),
                    ]
                ),
            )
        )
    if r.rank_oracle is not None:
        ro = r.rank_oracle
        out.append("<h4>FIFO rank oracle</h4>")
        out.append(
            kv(
                [
                    ("executions checked", F.num(ro.executions_checked)),
                    ("rank-0 share", F.pct(ro.rank0_share, 2)),
                    ("rank-0 share (sweep adjusted)", F.pct(ro.rank0_share_sweep_adjusted, 2)),
                    ("price-priority violations", F.num(ro.price_priority_violations)),
                    ("unknown-order executions", F.num(ro.executions_unknown_order)),
                    ("sweeps (multi-fill)", f"{ro.sweeps:,} ({ro.sweeps_multi_fill:,})"),
                ]
            )
        )
        cats = ["0", "1", "2", "3", "4", "5+"]
        svg = bar_chart(
            ctx.uid(),
            "FIFO rank of each execution",
            "Histogram of the number of resting orders ahead of each filled order at its price; rank 0 = the front of "
            "the queue (FIFO honoured).",
            cats,
            [
                ("raw", 1, [float(v) for v in ro.fifo_rank_histogram]),
                ("sweep adjusted", 3, [float(v) for v in ro.fifo_rank_histogram_sweep_adjusted]),
            ],
            y_fmt=F.num,
            x_label="orders ahead at execution",
            y_label="executions",
            height=220,
        )
        data = table(
            ["rank", "raw", "sweep adjusted"],
            [
                (c, F.num(a), F.num(b))
                for c, a, b in zip(cats, ro.fifo_rank_histogram, ro.fifo_rank_histogram_sweep_adjusted, strict=True)
            ],
            numeric=(1, 2),
        )
        out.append(
            figure(
                svg,
                "Executions by FIFO rank. Mass outside rank 0 means fills that skipped queue priority "
                "(or sweeps; the adjusted series ignores orders executed in the same sweep).",
                items=[("raw", 1, False), ("sweep adjusted", 3, False)],
                data=data,
            )
        )
    if r.shadows is not None:
        sh = r.shadows
        out.append("<h4>Shadow orders</h4>")
        out.append(kv([(k, F.num(v)) for k, v in sh.model_dump().items()]))
    if r.warnings:
        out.append(callout(WARN, "Replay warnings", ul(r.warnings)))
    return out


#: Reasons that are taker fills when a table has no 1.1 `liquidity` column (binding A5 refinement: crossed_at_live
#: only; a later `crossed` fill is maker).
TAKER_REASONS = ARROW_TAKER_REASONS


def _shadow_liquidity(run: RunDir) -> list[str]:
    """Shadow fills split into maker (queue) and taker (book crossed the order, audit ruling A5). SPEC 3.4 writes a
    taker walk as one `qt.shadow_fills` row per price level, so fills are counted as distinct (shadow, triggering
    event) pairs and the rows are shown next to them. Liquidity: the 1.1 `liquidity` column, else the reason."""
    t = run.table("qt.shadow_fills")
    if t is None:
        return []
    tab = t.read()
    if tab.num_rows == 0:
        return []
    reason = cols.strs(tab, "reason")
    qty = cols.f64(tab, "fill_qty")
    tk = np.isin(reason, TAKER_REASONS)
    if "liquidity" in tab.column_names:
        liq = cols.strs(tab, "liquidity")
        known = np.array([v is not None for v in liq.tolist()], dtype=bool)
        tk = np.where(known, liq == "taker", tk)
    sid = cols.strs(tab, "shadow_id")
    ts, _ = cols.i64(tab, "ts_event")
    seq, _ = cols.i64(tab, "sequence")
    event = np.array([f"{a}\x00{b}\x00{c}" for a, b, c in zip(sid.tolist(), ts.tolist(), seq.tolist(), strict=True)])

    def n_events(m: np.ndarray) -> int:
        return len(set(event[m].tolist()))

    rows = []
    for r in sorted(set(reason.tolist())):
        for is_taker in (False, True):
            m = (reason == r) & (tk == is_taker)
            if m.any():
                rows.append(
                    (
                        r,
                        "taker" if is_taker else "maker",
                        F.num(n_events(m)),
                        F.num(int(m.sum())),
                        F.num(int(qty[m].sum())),
                    )
                )
    return [
        "<h4>Shadow fills by liquidity</h4>",
        kv(
            [
                ("maker fills (queue, trade-through, crossed)", F.num(n_events(~tk))),
                (
                    "taker fills (crossed at live)",
                    f"{F.num(n_events(tk))} ({F.num(int(tk.sum()))} rows, one per price level walked)",
                ),
            ]
        ),
        table(["reason", "liquidity", "fills", "rows", "quantity"], rows, numeric=(2, 3, 4)),
    ]


def data_quality(ctx: Ctx) -> Raw | None:
    """Data-quality verdicts and counters, replay invariants, parity and rank oracle."""
    body: list[str] = []
    for run in ctx.inputs.runs:
        for d in run.docs:
            m = d.model
            if isinstance(m, ScorecardSummary) and m.data_quality is not None:
                body += _dq_block(ctx, f"Scorecard dataset · {_run_label(run, m)}", m.data_quality)
            elif isinstance(m, ReplaySummary):
                body += _dq_block(ctx, f"Replay · {_run_label(run, m)}", m.data_quality)
                body += _replay_block(ctx, m)
                body += _shadow_liquidity(run)
            elif isinstance(m, DataQuality):
                body += _dq_block(ctx, d.rec.path, m)
    if not body:
        return None
    return section(
        "data_quality",
        "Data quality",
        body,
        lead="Unusable partitions are still replayed, so every number downstream carries this verdict.",
    )


# ---- 2x2, McNemar, signed bias -----------------------------------------------------------------------------------


def _tests_table(run: RunDir) -> list[str]:
    tt = run.table("qt.score_tests")
    if tt is None:
        return []
    rows = cols.pylist(tt.read())
    prim = [r for r in rows if r["family"] == "primary"]
    sec = sorted(
        (r for r in rows if r["family"] != "primary"),
        key=lambda r: (r["p_adj"] if r["p_adj"] is not None else 2.0, r["test_id"], r["stratum"]),
    )
    shown = prim + sec[: max(MAX_TEST_ROWS - len(prim), 0)]
    trs = [
        (
            r["test_id"],
            r["family"],
            f"{r['stratum_kind']}={r['stratum']}" if r["stratum_kind"] else r["stratum"],
            r["statistic"],
            F.num(r["value"]),
            F.pval(r["p_value"]),
            F.pval(r["p_adj"]),
            r["adj_method"],
            F.ci(r["ci_lo"], r["ci_hi"]),
            F.num(r["n"]),
            r["note"] or "",
        )
        for r in shown
    ]
    more = f" (first {len(shown)} of {len(rows)}, secondary sorted by adjusted p)" if len(shown) < len(rows) else ""
    return [
        f"<details><summary>All hypothesis tests{F.esc(more)}</summary>",
        table(
            ["test", "family", "stratum", "statistic", "value", "p", "p adj", "method", "CI", "n", "note"],
            trs,
            numeric=(4, 5, 6, 9),
        ),
        "</details>",
    ]


def two_by_two(ctx: Ctx) -> Raw | None:
    """Per-order outcome table, McNemar, signed bias with equivalence band, tests."""
    runs = ctx.runs_with(ScorecardSummary)
    if not runs:
        return None
    body: list[str] = []
    for run, s in runs:
        t = s.two_by_two
        n = t.both + t.neither + t.live_only + t.sim_only

        def sh(v: int, n: int = n) -> str:
            return f"{v:,} ({F.pct(v / n) if n else F.DASH})"

        body.append(f"<h3>{F.esc(_run_label(run, s))}</h3>")
        tk = si.taker_counts(run, s)
        if tk and (tk["live"][0] or tk["sim"][0]):
            body.append(
                callout(
                    INFO,
                    f"Taker fills: live {si.taker_text(tk['live'])}, sim {si.taker_text(tk['sim'])}",
                    "<p>Fills where the order crossed the book on arrival (liquidity taker, reason crossed_at_live) "
                    "are not maker queue fills; the scorer keeps them out of the maker markout aggregates and reports "
                    "them separately (Markouts section).</p>",
                )
            )
        if si.fills_only(s):
            body.append(
                callout(
                    WARN,
                    "2×2 table, McNemar, cluster-robust discordance, fill-rate bias and equivalence: "
                    f"{si.NOT_COMPUTABLE}",
                    "<p>Never-filled live orders are absent from the source, so the live fill rate and every "
                    "discordance count are undefined.</p>",
                )
            )
            body += _tests_table(run)
            continue
        tm = si.twin_matching(s)
        if tm is not None:
            body.append(
                callout(
                    INFO if tm.rate is None or tm.rate >= 0.95 else WARN,
                    f"Self-exclusion: {F.pct(tm.rate) if tm.rate is not None else F.DASH} of live orders matched to "
                    f"their MBO twins ({F.num(tm.n_unmatched)} unmatched)",
                    "<p>Each live order's own record is removed from the replayed tape so its shadow does not queue "
                    "behind itself (audit ruling A1).</p>",
                )
            )
        win = si.window_text(s)
        grid = table(
            ["", f"sim filled within {win}", "sim not filled", "total"],
            [
                (
                    f"live filled within {win}",
                    sh(t.both),
                    Raw(f"<strong>{F.esc(sh(t.live_only))}</strong> live-only (b)"),
                    F.num(t.both + t.live_only),
                ),
                (
                    "live not filled",
                    Raw(f"<strong>{F.esc(sh(t.sim_only))}</strong> sim-only (c)"),
                    sh(t.neither),
                    F.num(t.sim_only + t.neither),
                ),
                ("total", F.num(t.both + t.sim_only), F.num(t.live_only + t.neither), F.num(n)),
            ],
            numeric=(3,),
            caption=f"Per-order fill outcome within the evaluation window H = {win} of entry, live vs sim (same "
            "orders, same entry time; a fill after H counts as not filled)",
        )
        m = s.mcnemar
        b = s.bias
        cl = si.clustered(run, s)
        prim = kv(
            [
                ("d = (c − b)/N", f"{F.pp(cl.d) if cl else F.DASH} {F.ci(cl.ci_lo, cl.ci_hi, F.pp) if cl else ''}"),
                ("p (primary)", F.pval(cl.p) if cl else "not run by the scorer"),
                ("method", si.METHOD_LABEL.get(cl.method or "", cl.method or F.DASH) if cl else F.DASH),
                (
                    "clusters (with discordance)",
                    f"{F.num(cl.n_clusters)} ({F.num(cl.n_clusters_discordant)})" if cl else F.DASH,
                ),
                ("design effect", F.num(cl.design_effect, 3) if cl else F.DASH),
                ("effective n", F.num(cl.n_effective, 4) if cl else F.DASH),
            ]
        )
        mc = kv(
            [
                ("discordant b (live-only)", F.num(m.b)),
                ("discordant c (sim-only)", F.num(m.c)),
                ("McNemar exact p (secondary)", F.pval(m.p_exact)),
                ("χ² (continuity corrected)", F.num(m.chi2_cc)),
                ("χ² p", F.pval(m.p_chi2)),
                ("odds ratio c/b", f"{F.num(m.odds_ratio)} {F.ci(m.or_ci_lo, m.or_ci_hi)}"),
            ]
        )
        bias = kv(
            [
                ("fill-rate bias (c − b)/N", f"{F.pp(b.fill_rate_bias)} {F.ci(b.ci_lo, b.ci_hi, F.pp)}"),
                ("relative overfill", F.pct(b.relative_overfill)),
                (
                    "quantity bias",
                    f"{F.pct(b.qty_bias, 2)} {F.ci(b.qty_bias_ci_lo, b.qty_bias_ci_hi, lambda v: F.pct(v, 2))}",
                ),
                ("direction", b.direction.replace("_", " ")),
            ]
        )
        body.append(
            f'<div class="grid2"><div>{grid}</div><div><h4>Primary: cluster-robust discordance test</h4>{prim}'
            f"<h4>Secondary: plain McNemar (assumes independent orders)</h4>{mc}<h4>Signed bias</h4>{bias}</div></div>"
        )
        e = s.equivalence
        margin = e.fill_rate_margin
        svg = forest_chart(
            ctx.uid(),
            "Signed fill-rate bias with 95% CI against the equivalence margin",
            f"Point estimate {F.pp(b.fill_rate_bias)} with 95% block-bootstrap CI ({si.ci_blocks_text(s)}) "
            f"{F.ci(b.ci_lo, b.ci_hi, F.pp)}; "
            f"shaded band is the equivalence margin ±{100 * margin:.1f} pp.",
            [
                (
                    "sim − live fill rate",
                    b.fill_rate_bias,
                    b.ci_lo,
                    b.ci_hi,
                    f"{F.pp(b.fill_rate_bias)}, 95% CI {F.ci(b.ci_lo, b.ci_hi, F.pp)}",
                )
            ],
            x_fmt=lambda v: F.pp(v, 1).replace(" pp", ""),
            x_label="percentage points (positive = simulator fills more often than live)",
            band=(-margin, margin),
            band_label=f"equivalence margin ±{100 * margin:.1f} pp",
            slot=2,
        )
        body.append(
            figure(
                svg,
                "Bias is the signed difference in fill rate; equivalence needs the 90% CI inside the "
                "shaded band (TOST).",
                note=f"Equivalent: {'yes' if e.fill_rate_equivalent else 'no'} (TOST p {F.pval(e.tost_p)}). "
                f"Markout equivalence at {F.dur(e.markout_horizon_ns)} within ±{F.num(e.markout_margin_ticks)} "
                f"ticks: {'yes' if e.markout_equivalent else 'no'} (TOST p {F.pval(e.markout_tost_p)}).",
                data=table(
                    ["estimate", "95% CI", "equivalence margin", "TOST p", "equivalent"],
                    [
                        (
                            F.pp(b.fill_rate_bias),
                            F.ci(b.ci_lo, b.ci_hi, F.pp),
                            f"±{100 * margin:.1f} pp",
                            F.pval(e.tost_p),
                            "yes" if e.fill_rate_equivalent else "no",
                        )
                    ],
                ),
            )
        )
        body.append(
            kv(
                [
                    (
                        "multiple testing",
                        f"{s.fdr.method.upper()} at q = {F.num(s.fdr.q)} (primary family Holm-adjusted)",
                    ),
                    ("tests / rejected", f"{s.fdr.n_tests:,} / {s.fdr.n_rejected:,}"),
                ]
            )
        )
        body += _tests_table(run)
    return section(
        "two_by_two",
        "Fill outcomes, discordance tests and signed bias",
        body,
        lead="b = filled live only, c = filled in the simulator only (maker fills inside the evaluation window). "
        "Positive bias means the simulator is optimistic. Orders on the same day are not independent, so the primary "
        "test is cluster-robust; plain McNemar is secondary.",
    )


# ---- discordance -------------------------------------------------------------------------------------------------


def discordance(ctx: Ctx) -> Raw | None:
    """Discordant counts by queue decile and liquidity bucket."""
    runs = [
        (r, s) for r, s in ctx.runs_with(ScorecardSummary) if s.by_queue_decile or s.by_liquidity or si.fills_only(s)
    ]
    if not runs:
        return None
    body: list[str] = []
    for run, s in runs:
        body.append(f"<h3>{F.esc(_run_label(run, s))}</h3>")
        if si.fills_only(s):
            body.append(callout(WARN, f"Discordance by queue decile and liquidity: {si.NOT_COMPUTABLE}"))
            continue
        src = None
        so = run.table("qt.score_orders")
        if so is not None:
            vals = sorted({str(v) for v in so.read().column("queue_ahead_source").to_pylist() if v is not None})
            src = ", ".join(vals)
        if src and src != "exact":
            body.append(
                callout(
                    INFO,
                    f"Queue position source: {src}",
                    "<p>Deciles use the visible L2 queue (or none) rather than the exact queue ahead.</p>",
                )
            )
        for title, rows, key, names in (
            ("queue decile at entry (0 = least queue ahead)", s.by_queue_decile, "decile", None),
            (
                "liquidity bucket (terciles of traded volume in the prior 60 s)",
                s.by_liquidity,
                "bucket",
                {0: "low", 1: "mid", 2: "high"},
            ),
        ):
            if not rows:
                continue
            cats = [names.get(getattr(r, key), str(getattr(r, key))) if names else f"D{getattr(r, key)}" for r in rows]
            lo = [float(r.live_only) for r in rows]
            so_ = [float(r.sim_only) for r in rows]
            tips = [
                [
                    f"{c}: live-only {r.live_only:,} of {r.both + r.neither + r.live_only + r.sim_only:,}, p_exact "
                    f"{F.pval(r.p_exact)}, adj {F.pval(r.p_adj)}"
                    for c, r in zip(cats, rows, strict=True)
                ],
                [
                    f"{c}: sim-only {r.sim_only:,} of {r.both + r.neither + r.live_only + r.sim_only:,}, p_exact "
                    f"{F.pval(r.p_exact)}, adj {F.pval(r.p_adj)}"
                    for c, r in zip(cats, rows, strict=True)
                ],
            ]
            svg = bar_chart(
                ctx.uid(),
                f"Discordant orders by {title}",
                "Live-only and sim-only order counts per stratum; a sim-only excess means the simulator fills "
                "orders the market did not.",
                cats,
                [("live-only (b)", 1, lo), ("sim-only (c)", 2, so_)],
                tips=tips,
                x_label=title,
                y_label="orders",
            )
            data = table(
                [key, "both", "neither", "live-only", "sim-only", "p exact", "p adj"],
                [
                    (
                        c,
                        F.num(r.both),
                        F.num(r.neither),
                        F.num(r.live_only),
                        F.num(r.sim_only),
                        F.pval(r.p_exact),
                        F.pval(r.p_adj),
                    )
                    for c, r in zip(cats, rows, strict=True)
                ],
                numeric=(1, 2, 3, 4, 5, 6),
            )
            body.append(
                figure(
                    svg,
                    f"Discordance by {title}. p-values are exact McNemar per stratum, FDR-adjusted.",
                    items=[("live-only (b)", 1, False), ("sim-only (c)", 2, False)],
                    data=data,
                )
            )
    return section(
        "discordance",
        "Where the simulator disagrees",
        body,
        lead="Queue-position errors concentrate where the queue ahead is long; liquidity errors where trading is thin.",
    )


# ---- markouts ----------------------------------------------------------------------------------------------------


def _markout_column_means(run: RunDir) -> tuple[dict, dict]:
    """Maker markout means from the 1.1 `qt.score_markouts` columns `mo_bps` / `mo_net_ccy` (fallback when the scorer
    wrote no `markout_mean_bps` / `markout_mean_net_ccy` test rows): {(arm, ref, horizon): row} without CIs."""
    t = run.table("qt.score_markouts")
    if t is None:
        return {}, {}
    tab = t.read()
    if "mo_bps" not in tab.column_names and "mo_net_ccy" not in tab.column_names:
        return {}, {}
    arm, ref = cols.strs(tab, "arm"), cols.strs(tab, "ref")
    hz, _ = cols.i64(tab, "horizon_ns")
    maker = np.ones(tab.num_rows, dtype=bool)
    ft = run.table("qt.score_fills")
    if ft is not None:  # taker fills stay out of the maker means (A5), as in the scorer's own aggregates
        f = ft.read()
        taker = {
            (k, a, int(q))
            for k, a, q, liq in zip(
                cols.strs(f, "order_key"),
                cols.strs(f, "arm"),
                cols.i64(f, "fill_seq")[0],
                cols.strs(f, "liquidity"),
                strict=True,
            )
            if liq == "taker"
        }
        seq, _ = cols.i64(tab, "fill_seq")
        keys = zip(cols.strs(tab, "order_key"), arm, seq.tolist(), strict=True)
        maker = np.array([(k, a, int(q)) not in taker for k, a, q in keys], dtype=bool)
    out: list[dict] = [{}, {}]
    for slot, name in enumerate(("mo_bps", "mo_net_ccy")):
        if name not in tab.column_names:
            continue
        v = cols.f64(tab, name)
        ok = np.isfinite(v) & maker
        for key in sorted({(str(a), str(r), int(h)) for a, r, h in zip(arm[ok], ref[ok], hz[ok], strict=True)}):
            m = ok & (arm == key[0]) & (ref == key[1]) & (hz == key[2])
            out[slot][key] = {"mean_ticks": float(v[m].mean()), "ci_lo": None, "ci_hi": None, "n": int(m.sum())}
    return out[0], out[1]


def _bps_net_table(run: RunDir) -> list[str]:
    """Maker markout means in bps of the fill price and in currency net of fees (review F10; `qt.score_tests` rows
    `markout_mean_bps` / `markout_mean_net_ccy`), when the scorer wrote them."""
    bps = {(r["arm"], r["ref"], r["horizon_ns"]): r for r in si.markout_rows(run, "markout_mean_bps")}
    net = {(r["arm"], r["ref"], r["horizon_ns"]): r for r in si.markout_rows(run, "markout_mean_net_ccy")}
    source = "qt.score_tests (bootstrap CIs)"
    if not bps and not net:
        bps, net = _markout_column_means(run)
        source = "per-fill qt.score_markouts columns mo_bps / mo_net_ccy (plain means, no CI)"
    keys = sorted(set(bps) | set(net), key=lambda k: (k[1], k[0], k[2]))
    if not keys:
        return []
    rows = []
    for k in keys:
        b, n = bps.get(k), net.get(k)
        rows.append(
            (
                k[0],
                k[1],
                F.dur(k[2]),
                F.num(b["mean_ticks"], 3) if b else F.DASH,
                F.ci(b["ci_lo"], b["ci_hi"]) if b else "",
                F.num(n["mean_ticks"], 4) if n else F.DASH,
                F.ci(n["ci_lo"], n["ci_hi"]) if n else "",
                F.num(n["n"]) if n else F.DASH,
            )
        )
    return [
        "<h4>Maker markouts in bps and net of fees</h4>",
        table(
            ["arm", "ref", "horizon", "mean bps", "95% CI", "mean net of fees (ccy)", "95% CI", "n net"],
            rows,
            numeric=(3, 5, 7),
            caption="bps = d·(M(t+h) − P)/P·10⁴; net = currency markout minus the fill's fee (positive fee = cost, "
            "rebates add); sim fills are charged the live log's fee rate per liquidity. Fees in another currency are "
            f"not netted. Source: {source}.",
        ),
    ]


def markouts(ctx: Ctx) -> Raw | None:
    """Markout curves live vs sim, paired differences and sim-only vs live-only."""
    runs = [
        (r, s)
        for r, s in ctx.runs_with(ScorecardSummary)
        if s.markouts or s.markout_diff or s.sim_only_vs_live_only_markout
    ]
    if not runs:
        return None
    body: list[str] = []
    yfmt = lambda v: F.num(v, 3)  # noqa: E731
    for run, s in runs:
        body.append(f"<h3>{F.esc(_run_label(run, s))}</h3>")
        for ref in sorted({m.ref for m in s.markouts}):
            series = []
            rows = []
            for arm, slot in (("live", 1), ("sim", 2)):
                ms = sorted((m for m in s.markouts if m.ref == ref and m.arm == arm), key=lambda m: m.horizon_ns)
                if not ms:
                    continue
                series.append(
                    Series(
                        arm,
                        [(float(m.horizon_ns), m.mean_ticks) for m in ms],
                        slot=slot,
                        markers=True,
                        ci=[(float(m.horizon_ns), m.ci_lo, m.ci_hi) for m in ms],
                        tips=[
                            f"{arm} {F.dur(m.horizon_ns)}: {F.num(m.mean_ticks)} ticks "
                            f"{F.ci(m.ci_lo, m.ci_hi)}, n = {m.n:,}"
                            for m in ms
                        ],
                    )
                )
                rows += [
                    (arm, F.dur(m.horizon_ns), F.num(m.n), F.num(m.mean_ticks), F.ci(m.ci_lo, m.ci_hi)) for m in ms
                ]
            svg = line_chart(
                ctx.uid(),
                f"Mean maker markout by horizon ({ref}), live vs sim",
                "Mean maker markout in ticks after each fill with 95% bootstrap CI, log-scaled horizon.",
                series,
                x_fmt=F.dur_axis,
                y_fmt=yfmt,
                x_log=True,
                x_label="horizon after fill (log)",
                y_label="markout (ticks)",
                hrefs=[Ref(0.0, "", "zero")],
            )
            body.append(
                figure(
                    svg,
                    f"Markouts vs {ref} (605-style, not NBBO): d·(M(t+h) − P)/tick per fill; positive = price "
                    "moved in the order's favour. The SEC realized spread of the taker is RS_h = 2 × the maker "
                    "markout.",
                    items=[(x.name, x.slot, False) for x in series],
                    data=table(["arm", "horizon", "n", "mean ticks", "95% CI"], rows, numeric=(2, 3)),
                )
            )
        tm = si.taker_markouts(run)
        tk = si.taker_counts(run, s)
        if tm or (tk and (tk["live"][0] or tk["sim"][0])):
            body.append("<h4>Taker fills (book crossed the order), reported separately</h4>")
            if tk:
                body.append(kv([(f"{a} taker fills", si.taker_text(tk[a])) for a in ("live", "sim")]))
            for ref in sorted({r["ref"] for r in tm}):
                ser = []
                for arm, slot in (("live", 1), ("sim", 2)):
                    ms = [r for r in tm if r["ref"] == ref and r["arm"] == arm]
                    if ms:
                        ser.append(
                            Series(
                                f"{arm} taker",
                                [(float(r["horizon_ns"]), r["mean_ticks"]) for r in ms],
                                slot=slot,
                                markers=True,
                                dashed=True,
                                ci=[(float(r["horizon_ns"]), r["ci_lo"], r["ci_hi"]) for r in ms],
                            )
                        )
                if not ser:
                    continue
                svg = line_chart(
                    ctx.uid(),
                    f"Mean taker markout by horizon ({ref})",
                    "Mean markout of fills where the book crossed the order (taker), 95% bootstrap CI.",
                    ser,
                    x_fmt=F.dur_axis,
                    y_fmt=yfmt,
                    x_log=True,
                    x_label="horizon after fill (log)",
                    y_label="markout (ticks)",
                    hrefs=[Ref(0.0, "", "zero")],
                )
                data = table(
                    ["arm", "horizon", "n", "mean ticks", "95% CI"],
                    [
                        (
                            r["arm"],
                            F.dur(r["horizon_ns"]),
                            F.num(r["n"]),
                            F.num(r["mean_ticks"]),
                            F.ci(r["ci_lo"], r["ci_hi"]),
                        )
                        for r in tm
                        if r["ref"] == ref
                    ],
                    numeric=(2, 3),
                )
                body.append(
                    figure(
                        svg,
                        "Taker fills pay the spread instead of earning it; mixing them into maker markouts would "
                        "flatter or punish the queue model for something it does not model (audit ruling A5).",
                        items=[(x.name, x.slot, True) for x in ser],
                        data=data,
                    )
                )
        body += _bps_net_table(run)
        if s.markout_diff:
            for ref in sorted({m.ref for m in s.markout_diff}):
                ms = sorted((m for m in s.markout_diff if m.ref == ref), key=lambda m: m.horizon_ns)
                ser = [
                    Series(
                        "sim − live",
                        [(float(m.horizon_ns), m.mean_diff_ticks) for m in ms],
                        slot=3,
                        markers=True,
                        ci=[(float(m.horizon_ns), m.ci_lo, m.ci_hi) for m in ms],
                    )
                ]
                svg = line_chart(
                    ctx.uid(),
                    f"Paired first-fill markout difference ({ref})",
                    "Mean of sim minus live first-fill markout over orders filled in both arms, 95% CI.",
                    ser,
                    x_fmt=F.dur_axis,
                    y_fmt=yfmt,
                    x_log=True,
                    x_label="horizon after fill (log)",
                    y_label="sim − live (ticks)",
                    hrefs=[Ref(0.0, "", "zero")],
                )
                data = table(
                    ["horizon", "n", "mean diff ticks", "95% CI", "p adj"],
                    [
                        (
                            F.dur(m.horizon_ns),
                            F.num(m.n),
                            F.num(m.mean_diff_ticks),
                            F.ci(m.ci_lo, m.ci_hi),
                            F.pval(m.p_adj),
                        )
                        for m in ms
                    ],
                    numeric=(1, 2, 4),
                )
                body.append(
                    figure(
                        svg,
                        "Orders filled in both arms: a non-zero difference means the simulator gets the "
                        "fill timing wrong in a way that changes adverse selection.",
                        data=data,
                    )
                )
        if s.sim_only_vs_live_only_markout:
            ms = sorted(s.sim_only_vs_live_only_markout, key=lambda m: m.horizon_ns)
            ser = [
                Series("sim-only fills", [(float(m.horizon_ns), m.sim_only_mean) for m in ms], slot=2, markers=True),
                Series("live-only fills", [(float(m.horizon_ns), m.live_only_mean) for m in ms], slot=1, markers=True),
            ]
            svg = line_chart(
                ctx.uid(),
                "Markouts of discordant fills: sim-only vs live-only",
                "Mean markout of fills that happened only in the simulator vs only live.",
                ser,
                x_fmt=F.dur_axis,
                y_fmt=yfmt,
                x_log=True,
                x_label="horizon after fill (log)",
                y_label="markout (ticks)",
                hrefs=[Ref(0.0, "", "zero")],
            )
            data = table(
                ["horizon", "sim-only mean", "live-only mean", "difference", "95% CI"],
                [
                    (
                        F.dur(m.horizon_ns),
                        F.num(m.sim_only_mean),
                        F.num(m.live_only_mean),
                        F.num(m.diff),
                        F.ci(m.ci_lo, m.ci_hi),
                    )
                    for m in ms
                ],
                numeric=(1, 2, 3),
            )
            body.append(
                figure(
                    svg,
                    "Phantom fills (sim-only) that mark out badly are the classic source of backtest "
                    "P&L that never shows up live.",
                    items=[(x.name, x.slot, False) for x in ser],
                    data=data,
                )
            )
    return section(
        "markouts",
        "Markouts",
        body,
        lead="605-style, not NBBO: markouts are realized spreads against this dataset's own venue mid (single feed), "
        "not the consolidated NBBO of SEC Rule 605 reports, and n is shown per horizon because fills whose horizon "
        "runs past the end of the data are dropped. Maker fills only in the main curves; taker fills (the book "
        "crossed the order) are shown separately.",
    )


# ---- time to fill ------------------------------------------------------------------------------------------------


def time_to_fill(ctx: Ctx) -> Raw | None:
    """CIF overlay, time-to-fill tests and QQ of log time to fill."""
    runs = ctx.runs_with(ScorecardSummary)
    if not runs:
        return None
    body: list[str] = []
    for run, s in runs:
        t = s.ttf
        fo = si.fills_only(s)
        nc = si.NOT_COMPUTABLE
        body.append(f"<h3>{F.esc(_run_label(run, s))}</h3>")
        ratio = math.exp(t.median_log_ratio) if F.is_num(t.median_log_ratio) else None
        body.append(
            kv(
                [
                    ("jointly filled orders", F.num(t.n_joint)),
                    ("median log(ttf sim / ttf live)", f"{F.num(t.median_log_ratio)} {F.ci(t.ci_lo, t.ci_hi)}"),
                    ("median ratio", f"{F.num(ratio, 3)}×" if ratio is not None else F.DASH),
                    ("paired permutation p (CIF, primary)", nc if fo else F.pval(t.paired_perm_p)),
                    ("Wilcoxon signed-rank p", F.pval(t.wilcoxon_p)),
                    ("log-rank p (descriptive, assumes independence)", nc if fo else F.pval(t.logrank_p)),
                    ("Gray p (descriptive, assumes independence)", nc if fo else F.pval(t.gray_p)),
                ]
            )
        )
        if fo:
            body.append(
                callout(
                    WARN,
                    f"Cumulative incidence of fill: {nc}",
                    "<p>Time to fill conditional on a fill (median ratio, Wilcoxon, QQ) is still valid.</p>",
                )
            )
        series, rows = [], []
        for arm, slot in (("live", 1), ("sim", 2)):
            cif = None if fo else t.cif_fill.get(arm)  # type: ignore[call-overload]
            if not cif:
                continue
            pts = sorted((int(h), v) for h, v in cif.items())
            series.append(
                Series(
                    arm,
                    [(float(h), v) for h, v in pts],
                    slot=slot,
                    kind="step",
                    markers=True,
                    tips=[f"{arm}: P(filled by {F.dur(h)}) = {F.num(v)}" for h, v in pts],
                )
            )
            rows += [(arm, F.dur(h), F.num(v)) for h, v in pts]
        if series:
            svg = line_chart(
                ctx.uid(),
                "Cumulative incidence of fill, live vs sim",
                "Aalen-Johansen cumulative incidence of the fill cause (cancels compete) at each horizon.",
                series,
                x_fmt=F.dur_axis,
                y_fmt=lambda v: F.num(v, 2),
                x_log=True,
                y_range=(0.0, 1.0),
                x_label="time since entry (log)",
                y_label="P(filled by t)",
            )
            body.append(
                figure(
                    svg,
                    "Cumulative incidence of the first fill with cancels and expiries as a competing "
                    "risk; a sim curve above live is optimistic about fill speed.",
                    items=[(x.name, x.slot, False) for x in series],
                    data=table(["arm", "horizon", "CIF fill"], rows, numeric=(2,)),
                )
            )
        so = run.table("qt.score_orders")
        if so is not None:
            tab = so.read()
            lv, lok = cols.i64(tab, "ttf_live_ns")
            sv, sok = cols.i64(tab, "ttf_sim_ns")
            both = lok & sok
            n = int(both.sum())
            if n >= 5:
                a = np.log10(np.maximum(lv[both], 1000).astype(np.float64))
                b = np.log10(np.maximum(sv[both], 1000).astype(np.float64))
                k = min(99, n)
                probs = (np.arange(1, k + 1) - 0.5) / k
                qa = np.quantile(a, probs, method="linear")
                qb = np.quantile(b, probs, method="linear")
                tfmt = lambda v: F.dur_axis(10.0**v)  # noqa: E731
                ser = [
                    Series(
                        "quantiles",
                        [(float(x), float(y)) for x, y in zip(qa, qb, strict=True)],
                        slot=2,
                        kind="points",
                        tips=[
                            f"q{100 * p:.1f}: live {F.dur(10**x)}, sim {F.dur(10**y)}"
                            for p, x, y in zip(probs, qa, qb, strict=True)
                        ],
                    )
                ]
                lo = math.floor(min(qa.min(), qb.min()))
                hi = math.ceil(max(qa.max(), qb.max()))
                svg = line_chart(
                    ctx.uid(),
                    "QQ plot of log time to first fill (jointly filled orders)",
                    "Quantiles of the simulated time to fill against the live quantiles, log10 scale; "
                    "points above the diagonal mean the simulator fills later.",
                    ser,
                    x_fmt=tfmt,
                    y_fmt=tfmt,
                    x_range=(lo, hi if hi > lo else lo + 1),
                    y_range=(lo, hi if hi > lo else lo + 1),
                    diag=True,
                    width=420,
                    height=380,
                    x_label="live time to fill",
                    y_label="sim time to fill",
                    end_labels=False,
                )
                data = table(
                    ["quantile", "live", "sim"],
                    [
                        (f"{100 * p:.1f}%", F.dur(10**x), F.dur(10**y))
                        for p, x, y in zip(
                            probs[:: max(k // 20, 1)], qa[:: max(k // 20, 1)], qb[:: max(k // 20, 1)], strict=True
                        )
                    ],
                    numeric=(1, 2),
                )
                body.append(figure(svg, f"{n:,} orders filled in both arms (times floored at 1 µs).", data=data))
    return section("time_to_fill", "Time to fill", body)


# ---- position ----------------------------------------------------------------------------------------------------


def position(ctx: Ctx) -> Raw | None:
    """Position over time per instrument, live vs sim (last value per uniform time bucket)."""
    found = [(r, t) for r in ctx.inputs.runs if (t := r.table("qt.score_position")) is not None]
    if not found:
        return None
    body: list[str] = []
    for run, tbl in found:
        tab = tbl.read()
        if tab.num_rows == 0:
            continue
        arm = cols.strs(tab, "arm")
        inst, _ = cols.i64(tab, "instrument_id")
        tsv, _ = cols.i64(tab, "ts")
        posv, _ = cols.i64(tab, "position")
        insts = sorted(set(inst.tolist()))
        body.append(f"<h3>{F.esc(run.label)}</h3>")
        if len(insts) > MAX_POSITION_INSTRUMENTS:
            body.append(
                callout(
                    INFO,
                    f"Showing {MAX_POSITION_INSTRUMENTS} of {len(insts)} instruments",
                    "<p>Instruments with the most position changes are shown.</p>",
                )
            )
            counts = {i: int((inst == i).sum()) for i in insts}
            insts = sorted(sorted(insts, key=lambda i: (-counts[i], i))[:MAX_POSITION_INSTRUMENTS])
        figs: list[str] = []
        for iid in insts:
            m = inst == iid
            t_all = tsv[m]
            t0, t1 = int(t_all.min()), int(t_all.max())
            series, table_pts = [], []
            for name, slot in (("live", 1), ("sim", 2)):
                mm = m & (arm == name)
                if not mm.any():
                    continue
                order = np.argsort(tsv[mm], kind="stable")
                tt, pv = tsv[mm][order], posv[mm][order]
                dt, dv = last_per_bucket(tt, pv, t0, t1, MAX_POSITION_POINTS)
                pts = [(float(x - t0) / 1e9, float(y)) for x, y in zip(dt.tolist(), dv.tolist(), strict=True)]
                if pts and len(pts) < MAX_POSITION_POINTS and pts[-1][0] < (t1 - t0) / 1e9:
                    pts.append(((t1 - t0) / 1e9, pts[-1][1]))
                series.append(Series(name, pts, slot=slot, kind="step"))
                tb, vb = last_per_bucket(tt, pv, t0, t1, MAX_POSITION_TABLE_ROWS)
                table_pts += [(int(x), name, int(y)) for x, y in zip(tb.tolist(), vb.tolist(), strict=True)]
            table_pts.sort()
            ticks, tfmt = time_ticks(t0, (t1 - t0) / 1e9)
            svg = line_chart(
                ctx.uid(),
                f"Position over time, instrument {iid}: live vs sim",
                "Cumulative signed filled quantity per arm (step), downsampled to the last value per "
                "uniform time bucket.",
                series,
                x_fmt=tfmt,
                y_fmt=F.num,
                x_ticks=ticks,
                x_label="time (UTC)",
                y_label="position (size units)",
                hrefs=[Ref(0.0, "", "zero")],
            )
            data = table(
                ["time (UTC)", "arm", "position"], [(F.ts(a), b, F.num(c)) for a, b, c in table_pts], numeric=(2,)
            )
            figs.append(
                figure(
                    svg,
                    f"Instrument {iid}: inventory path implied by each arm's fills (at most "
                    f"{MAX_POSITION_POINTS:,} points per series).",
                    items=[(x.name, x.slot, False) for x in series],
                    data=data,
                    note=f"Table: last value per {MAX_POSITION_TABLE_ROWS} buckets.",
                )
            )
        # the first instrument is shown; the others are one collapsed block (answer-first layout)
        if figs:
            body.append(figs[0])
        if len(figs) > 1:
            body.append(
                details("Position over time for the other instruments", figs[1:], note=f"{len(figs) - 1} instrument(s)")
            )
    if not body:
        return None
    return section(
        "position",
        "Position over time",
        body,
        lead="Divergent inventory paths compound fill-rate bias into P&L and risk differences.",
    )
