"""Model sections: zoo leaderboard with signed-bias forest plots, reliability diagrams per model x horizon (Wilson
CIs) and fill-rate bias by queue decile; calibration likelihood profile, estimates, held-out comparison and export
code; learned hazard model scores, reliability, BSS vs climatology and comparison with the zoo."""

from __future__ import annotations

import datetime
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.eval import leaderboard as lbm
from queuetruth.report import cols
from queuetruth.report import fmt as F
from queuetruth.report import zooinfo as zi
from queuetruth.report.compute import (
    ALLOC_FAMILIES,
    PROFILE_CHI2_95,
    alloc_profile,
    default_grid,
    reliability_bins,
    strata,
)
from queuetruth.report.ctx import FULL_RELIABILITY_MODELS, Ctx
from queuetruth.report.load import FileRec, RunDir
from queuetruth.report.page import (
    CRIT,
    GOOD,
    INFO,
    WARN,
    Raw,
    callout,
    code_block,
    figure,
    kv,
    section,
    table,
    ul,
)
from queuetruth.report.svg import Ref, Series, forest_chart, heatmap, line_chart, reliability_panel
from queuetruth.schemas.io import sha256_file
from queuetruth.schemas.models import CalibrationResult, HazardModelCard, ZooSummary

NOT_COMPUTABLE = "not computable"


def _hkeys(d: dict[str, Any]) -> list[int]:
    return sorted(int(k) for k in d)


def _ref_horizon(hs: list[int]) -> int | None:
    if not hs:
        return None
    return 1_000_000_000 if 1_000_000_000 in hs else hs[len(hs) // 2]


def _label(run: RunDir, doc: Any) -> str:
    rid = getattr(doc, "run_id", None)
    return f"{run.label} (run {rid})" if rid else run.label


# ---- zoo ---------------------------------------------------------------------------------------------------------


def _reliability_from_predictions(
    ctx: Ctx, run: RunDir, models: list[str], horizons: list[int], h_max: int | None
) -> list[str]:
    tbl = run.table("qt.zoo_predictions")
    if tbl is None:
        return []
    tab = tbl.read()
    mid = cols.strs(tab, "model_id")
    hz, _ = cols.i64(tab, "horizon_ns")
    p = cols.f64(tab, "p_fill")
    y, _ = cols.boolean(tab, "truth_filled")
    never, elig, rule = zi.row_masks(run, tab, h_max)
    usable = never & elig & np.isfinite(p)
    if not usable.any():
        return [
            callout(
                INFO,
                "No exact truth in the zoo predictions",
                "<p>The dataset is L2-only (or every row is censored): reliability needs exact fills.</p>",
            )
        ]
    present = sorted(set(mid[usable].tolist()))
    order = [m for m in models if m in present] + [m for m in present if m not in models]
    hs = [h for h in horizons if (usable & (hz == h)).any()] or sorted(set(hz[usable].tolist()))
    out: list[str] = []
    blocks: list[str] = []
    for m in order:
        panels, rows = [], []
        for h in hs:
            sel = usable & (mid == m) & (hz == h)
            if not sel.any():
                continue
            bins = reliability_bins(p[sel], y[sel].astype(np.float64))
            n = int(sel.sum())
            panels.append(
                reliability_panel(
                    ctx.uid(),
                    f"{m} · {F.dur(h)}",
                    f"Reliability of {m} at horizon {F.dur(h)}: observed exact fill frequency vs forecast, n = {n:,}.",
                    bins,
                    label=f"horizon {F.dur(h)} · n = {n:,}",
                )
            )
            rows += [
                (
                    F.dur(h),
                    f"[{b.lo:.1f}, {b.hi:.1f}]",
                    F.num(b.n),
                    F.fixed(b.p_mean, 3) if b.n else F.DASH,
                    F.fixed(b.obs, 3) if b.n else F.DASH,
                    F.ci(b.ci_lo, b.ci_hi, lambda v: F.fixed(v, 3)) if b.n else F.DASH,
                )
                for b in bins
            ]
        if not panels:
            continue
        data = (
            ""
            if ctx.compact
            else table(["horizon", "bin", "n", "mean forecast", "observed", "95% Wilson CI"], rows, numeric=(2, 3, 4))
        )
        det = f"<details><summary>Data table</summary>{data}</details>" if data else ""
        blocks.append(f'<h4>{F.esc(m)}</h4><div class="relgrid">{"".join(panels)}</div>{det}')
    if len(blocks) > FULL_RELIABILITY_MODELS:
        rest = len(blocks) - FULL_RELIABILITY_MODELS
        blocks = [
            *blocks[:FULL_RELIABILITY_MODELS],
            f"<details><summary>Reliability of the remaining {rest} models</summary>",
            *blocks[FULL_RELIABILITY_MODELS:],
            "</details>",
        ]
    out.append("<h3>Reliability diagrams</h3>")
    out.append(
        '<p class="lead">Never-cancel shadows only. Each point is a bin of forecasts (10 equal-width bins); '
        "whiskers are 95% Wilson intervals of the observed exact fill frequency; the strip shows how many forecasts "
        "fall in each bin (square-root scale). Deterministic models forecast 0 or 1 only. Label eligibility: "
        f"{F.esc(rule)}.</p>"
    )
    out += blocks
    return out


def _decile_bias(ctx: Ctx, run: RunDir, models: list[str], horizons: list[int], h_max: int | None) -> list[str]:
    """Fill-rate bias by queue decile: from qt.zoo_errors when present, else computed from qt.zoo_predictions."""
    vals: dict[tuple[str, int, int], tuple[float, float | None, float | None, int]] = {}
    source = ""
    et = run.table("qt.zoo_errors")
    if et is not None:
        for r in cols.pylist(et.read()):
            if r["metric"] == "fill_rate_bias" and r["stratum_kind"] == "queue_decile" and r["horizon_ns"] is not None:
                try:
                    d = int(str(r["stratum"]).lstrip("D"))
                except ValueError:
                    continue
                vals[(r["model_id"], int(r["horizon_ns"]), d)] = (r["value"], r["ci_lo"], r["ci_hi"], int(r["n"]))
        source = (
            "qt.zoo_errors (block-bootstrap CIs in the tooltips; blocks are session days, or 30-minute blocks with "
            "fewer than 5 days)"
        )
    if not vals:
        pt = run.table("qt.zoo_predictions")
        if pt is None:
            return []
        tab = pt.read()
        y, _ = cols.boolean(tab, "truth_filled")
        never, elig, _rule = zi.row_masks(run, tab, h_max)
        qa = cols.f64(tab, "queue_ahead_at_live")
        if not (never & elig & np.isfinite(qa)).any():
            return []
        sid = cols.strs(tab, "shadow_id")
        inst, _ = cols.i64(tab, "instrument_id")
        day, _ = cols.i64(tab, "session_date")
        # deciles over the run's shadows (one value per shadow), per (instrument, date)
        _, first = np.unique(sid.astype(str), return_index=True)
        first = np.sort(first)
        groups = np.array([f"{a}:{b}" for a, b in zip(inst.tolist(), day.tolist(), strict=True)], dtype=object)
        dec_shadow = strata(qa[first], groups[first], 10)
        dec_of = dict(zip(sid[first].tolist(), dec_shadow.tolist(), strict=True))
        dec = np.array([dec_of[s] for s in sid.tolist()], dtype=np.int64)
        mid = cols.strs(tab, "model_id")
        hz, _ = cols.i64(tab, "horizon_ns")
        p = cols.f64(tab, "p_fill")
        ok = never & elig & (dec >= 0) & np.isfinite(p)
        for m in sorted(set(mid[ok].tolist())):
            for h in sorted(set(hz[ok].tolist())):
                base = ok & (mid == m) & (hz == h)
                for d in range(10):
                    sel = base & (dec == d)
                    n = int(sel.sum())
                    if n:
                        vals[(m, int(h), d)] = (float(np.sort(p[sel]).mean() - y[sel].mean()), None, None, n)
        source = (
            "computed by the report from qt.zoo_predictions on never-cancel shadows (no CIs; no qt.zoo_errors table "
            "in this input)"
        )
    present = sorted({k[0] for k in vals})
    rows_m = [m for m in models if m in present] + [m for m in present if m not in models]
    hs = sorted({k[1] for k in vals})
    vmax = max((abs(v[0]) for v in vals.values() if v[0] is not None and math.isfinite(v[0])), default=0.0) or 1.0
    out = [
        "<h3>Fill-rate bias by queue decile</h3>",
        f'<p class="lead">Never-cancel shadows. Mean forecast minus exact fill rate per queue-ahead decile at entry '
        "(0 = shortest queue). "
        f"Red = the model fills too often, blue = too rarely; full color = ±{F.fixed(100 * vmax, 1)} pp, shared by "
        f"every horizon. Source: {F.esc(source)}.</p>",
    ]
    for h in hs:
        grid, tips, trows = [], [], []
        for m in rows_m:
            row, trow = [], []
            for d in range(10):
                v = vals.get((m, h, d))
                row.append(v[0] if v else None)
                trow.append(
                    f"{m}, decile {d}: " + (f"{F.pp(v[0])} {F.ci(v[1], v[2], F.pp)}, n = {v[3]:,}" if v else "no data")
                )
                if v:
                    trows.append((m, d, F.pp(v[0]), F.ci(v[1], v[2], F.pp), F.num(v[3])))
            grid.append(row)
            tips.append(trow)
        svg = heatmap(
            ctx.uid(),
            f"Fill-rate bias by queue decile at {F.dur(h)}",
            f"Signed fill-rate bias (model minus exact) per model and queue decile at horizon {F.dur(h)}.",
            rows_m,
            [f"D{d}" for d in range(10)],
            grid,
            fmt=lambda v: F.fixed(100 * v, 1),
            tips=tips,
            x_label="queue-ahead decile at entry (values in pp)",
            vmax=vmax,
        )
        data = "" if ctx.compact else table(["model", "decile", "bias", "95% CI", "n"], trows, numeric=(1, 2, 4))
        out.append(figure(svg, f"Horizon {F.dur(h)}: values in percentage points.", data=data))
    return out


def _bias_forest(
    ctx: Ctx, idx: dict[zi.ErrKey, zi.ErrVal], models: list[str], h: int, prefix: str, what: str
) -> str | None:
    """Forest of `<prefix>fill_rate_bias` (eligible rows, 95% CI) with `<prefix>fill_rate_bias_aj` diamonds."""
    frows, alt = [], []
    for m in models:
        fb = idx.get((m, prefix + "fill_rate_bias", h, "all", "all"))
        aj = idx.get((m, prefix + "fill_rate_bias_aj", h, "all", "all"))
        if fb is None and aj is None:
            continue
        v, lo, hi = (fb[0], fb[1], fb[2]) if fb else (None, None, None)
        frows.append(
            (
                m,
                v,
                lo,
                hi,
                f"{m} at {F.dur(h)}: {F.pp(v)} {F.ci(lo, hi, F.pp)} on eligible rows; vs Aalen-Johansen CIF "
                f"{F.pp(aj[0] if aj else None)}",
            )
        )
        alt.append(aj[0] if aj else None)
    if not frows:
        return None
    svg = forest_chart(
        ctx.uid(),
        f"Signed fill-rate bias by model at {F.dur(h)} ({what})",
        f"Model minus exact fill rate at horizon {F.dur(h)}: dots with 95% block-bootstrap CI on label-eligible "
        "shadows; diamonds against the Aalen-Johansen cumulative incidence of fill over all shadows.",
        frows,
        x_fmt=lambda v: F.fixed(100 * v, 1),
        x_label="fill-rate bias (pp, positive = model fills too often)",
        slot=2,
        alt=alt,
    )
    data = table(
        ["model", "bias (eligible rows)", "95% CI", "bias vs AJ CIF"],
        [(r[0], F.pp(r[1]), F.ci(r[2], r[3], F.pp), F.pp(a)) for r, a in zip(frows, alt, strict=True)],
        numeric=(1, 3),
    )
    return figure(
        svg,
        f"Horizon {F.dur(h)}: points right of zero are optimistic queue models. Truth fill rate = Aalen-Johansen "
        "CIF (never 1 − Kaplan-Meier).",
        items=[("bias on eligible rows, 95% CI", 2, False), ("bias vs Aalen-Johansen CIF", 3, False)],
        data=data,
    )


def _err(idx: dict[zi.ErrKey, zi.ErrVal], m: str, metric: str, h: int | None) -> zi.ErrVal | None:
    return idx.get((m, metric, h, "all", "all"))


def _cancel_bearing(
    ctx: Ctx,
    idx: dict[zi.ErrKey, zi.ErrVal],
    models: list[str],
    horizons: list[int],
    href: int | None,
    h_max: int | None,
) -> list[str]:
    P = zi.CANCEL_PREFIX
    present = [m for m in models if any(k[0] == m and k[1].startswith(P) for k in idx)]
    present += sorted({k[0] for k in idx if k[1].startswith(P)} - set(present))
    if not present:
        return []
    rows = []
    for m in present:
        fb = _err(idx, m, P + "fill_rate_bias", href)
        aj = _err(idx, m, P + "fill_rate_bias_aj", href)
        br = _err(idx, m, P + "brier", href)
        ob = _err(idx, m, P + "overfill_bias", h_max)
        dp = _err(idx, m, P + "discordance_p_primary", h_max)
        k = _err(idx, m, P + "n_clusters", h_max)
        mb, mc = _err(idx, m, P + "mcnemar_b", h_max), _err(idx, m, P + "mcnemar_c", h_max)
        mp = _err(idx, m, P + "mcnemar_p_exact", h_max)
        rows.append(
            (
                m,
                F.fixed(br[0] if br else None, 4),
                f"{F.pp(fb[0] if fb else None)} {F.ci(fb[1], fb[2], F.pp) if fb else ''}".strip(),
                F.pp(aj[0] if aj else None),
                f"{F.pp(ob[0] if ob else None)} {F.ci(ob[1], ob[2], F.pp) if ob else ''}".strip(),
                F.pval(dp[0] if dp else None),
                F.num(int(k[0]) if k else None),
                f"{F.num(int(mb[0]) if mb else None)} / {F.num(int(mc[0]) if mc else None)}"
                if mb or mc
                else NOT_COMPUTABLE,
                F.pval(mp[0] if mp else None) if mb or mc else NOT_COMPUTABLE,
                F.num(fb[3] if fb else (br[3] if br else None)),
            )
        )
    out = [
        "<h4>Cancel-bearing shadows (mirror orders with real cancel times), reported separately</h4>",
        '<p class="lead">These shadows carry their own planned cancel, so a fill competes with the cancel. Truth is '
        "the Aalen-Johansen cumulative incidence of fill with the own cancel as a competing risk; they are kept out "
        "of the leaderboard because their labels depend on the cancel policy, not only on the queue model (audit "
        "ruling A2).</p>",
        table(
            [
                "model",
                f"Brier {F.dur(href)}",
                f"fill-rate bias {F.dur(href)}",
                "bias vs AJ CIF",
                "overfill bias at h_max",
                "discordance p (cluster-robust)",
                "clusters",
                "McNemar b / c (secondary)",
                "McNemar p (secondary)",
                "n",
            ],
            rows,
            numeric=(1, 3, 5, 6, 8, 9),
        ),
    ]
    if href is not None:
        fig = _bias_forest(ctx, idx, present, href, P, "cancel-bearing shadows")
        if fig:
            out.append(fig)
    return out


def _taker_table(run: RunDir) -> list[str]:
    tk = zi.taker_by_model(run)
    if not tk or not any(r["taker_fills"] for r in tk):
        return []
    rows = [
        (
            r["model_id"],
            F.num(r["fills"]),
            F.num(r["taker_fills"]),
            F.pct(r["taker_qty_share"]),
            F.num(r["shadows_first_taker"]),
            F.num(r["shadows_filled"]),
        )
        for r in tk
    ]
    return [
        "<h4>Taker fills by model (book crossed the order)</h4>",
        '<p class="lead">Taker fills (liquidity taker; reason crossed_at_live) take liquidity at the opposite '
        "prices; they are not queue fills and are listed separately (audit ruling A5).</p>",
        table(
            ["model", "fills", "taker fills", "taker share of qty", "shadows first filled as taker", "shadows filled"],
            rows,
            numeric=(1, 2, 3, 4, 5),
        ),
    ]


def zoo(ctx: Ctx) -> Raw | None:
    """Zoo leaderboard on never-cancel shadows (A2), Aalen-Johansen truth, cluster-robust discordance (A10),
    cancel-bearing results separately, taker fills (A5), reliability and bias by queue decile."""
    runs = ctx.runs_with(ZooSummary)
    if not runs:
        return None
    body: list[str] = []
    for run, z in runs:
        spec = z.spec
        body.append(f"<h3>{F.esc(_label(run, z))}</h3>")
        l2 = spec.get("l2") if isinstance(spec.get("l2"), dict) else {}
        pl = zi.planned_life(run)
        h_max = spec.get("h_max_ns") if isinstance(spec.get("h_max_ns"), int) else None
        pop = F.DASH
        if pl is not None:
            nn = sum(1 for v in pl.values() if v[2] is True or (v[2] is None and v[0] is None))
            pop = f"{nn:,} never-cancel / {len(pl) - nn:,} cancel-bearing"
        body.append(
            kv(
                [
                    ("shadows", F.num(z.n_shadows)),
                    ("population (zoo_shadows)", pop),
                    ("models", F.num(len(z.models))),
                    ("mode / exchange", f"{spec.get('mode', F.DASH)} / {spec.get('exchange', F.DASH)}"),
                    (
                        "L2 view",
                        f"depth {l2.get('depth_levels', F.DASH)}, cadence {F.dur(l2.get('cadence_ns'))}, trades "
                        f"{l2.get('trade_position', F.DASH)}",
                    ),
                    ("h_max", F.dur(h_max)),
                    ("particles", F.num(spec.get("particles"))),
                ]
            )
        )
        if z.warnings:
            # the faithful hbt #312 abort lines (one per model) collapse into one item with the per-model counts
            aborts = [w for w in z.warnings if lbm.ABORT_PREFIX in w]
            items = [w for w in z.warnings if lbm.ABORT_PREFIX not in w]
            if aborts:
                per_model = ", ".join(f"{m} {n:,}" for m, (n, _) in lbm.abort_shares(aborts).items())
                items.insert(
                    0,
                    f"hftbacktest bug #312: {len(aborts)} faithful hbt model(s) aborted on some shadows ({per_model}); "
                    f"the {z.n_excluded_hbt_312:,} shadows involved are left out of every model, so all models are "
                    "scored on the same population",
                )
            body.append(callout(WARN, "Zoo warnings", ul(items)))
        idx = zi.errors_index(run)
        lb = z.leaderboard
        horizons = lbm.horizons_of(lb) if lb else []
        href = _ref_horizon([h for h in horizons if h_max is None or h < h_max] or horizons)
        hsort = lbm.sort_horizon(horizons, ctx.sort_horizon_ns)
        ordered = lbm.rank_leaderboard(lb, hsort)
        models = [e.model_id for e in ordered] or list(z.models)
        if lb:
            body.append(
                "<h4>Leaderboard: never-cancel shadows (planned life ≥ h_max), sorted by absolute fill-rate bias at "
                f"{F.esc(lbm.duration_text(hsort) if hsort else F.DASH)} (bias vs the Aalen-Johansen truth CIF); "
                "closer to zero is better</h4>"
            )
            if z.n_leaderboard is not None:
                body.append(
                    f'<p class="lead">Every model is scored on the same {z.n_leaderboard:,} never-cancel shadows'
                    + (
                        f" ({z.n_excluded_hbt_312:,} shadows on which a faithful hftbacktest model hit its bug #312 "
                        "are left out of every model)"
                        if z.n_excluded_hbt_312
                        else ""
                    )
                    + ".</p>"
                )
            body.append(ul([lbm.bias_sentence(e, hsort) for e in ordered], "sentences"))
            heads = (
                ["model"]
                + [f"fill-rate bias {F.dur(h)}" for h in horizons]
                + [
                    f"bias on eligible rows {F.dur(hsort)}" if hsort else "bias on eligible rows",
                    "overfill bias at h_max",
                    "discordance p (cluster-robust)",
                    "clusters",
                    "McNemar b / c (secondary)",
                    "McNemar p (secondary)",
                ]
            )
            rows = []
            exact = [next((r for e in ordered if (r := lbm.rates_at(e, h)[1]) is not None), None) for h in horizons]
            if any(x is not None for x in exact):
                # the base rate of every bias in its column: the truth AJ CIF, the same for every model (0.1.1)
                rows.append(["exact fill rate (truth)", *(F.pct(x) for x in exact), *([""] * 6)])
            for e in ordered:
                # the leaderboard bias is the AJ figure (A2); this column is the eligible-row diagnostic
                aj = _err(idx, e.model_id, "fill_rate_bias", hsort)
                dp = _err(idx, e.model_id, "discordance_p_primary", h_max)
                k = _err(idx, e.model_id, "n_clusters", h_max)
                mcn = e.mcnemar
                computable = mcn.p_exact is not None or mcn.b + mcn.c > 0
                cells = []
                for h in horizons:
                    v, lo, hi = lbm.bias_at(e, h)
                    cells.append(f"{F.pp(v)} {F.ci(lo, hi, F.pp) if v is not None else ''}".strip())
                rows.append(
                    [
                        e.model_id,
                        *cells,
                        F.pp(aj[0] if aj else None),
                        f"{F.pp(e.overfill_bias.value)} {F.ci(e.overfill_bias.ci_lo, e.overfill_bias.ci_hi, F.pp)}",
                        F.pval(dp[0] if dp else None),
                        F.num(int(k[0]) if k else None),
                        f"{mcn.b:,} / {mcn.c:,}" if computable else NOT_COMPUTABLE,
                        F.pval(mcn.p_exact) if computable else NOT_COMPUTABLE,
                    ]
                )
            nh = len(horizons)
            body.append(table(heads, rows, numeric=(*range(1, nh + 1), nh + 1, nh + 3, nh + 4, nh + 6)))
            srows = [
                [e.model_id]
                + [F.fixed(e.brier.get(str(h)), 4) for h in horizons]
                + [
                    F.pct(e.qty_bias, 1),
                    F.num(e.ttf_log_ratio_median, 3),
                    F.num(e.ttf_iad, 3),
                    F.num(e.qtrack_mae, 3),
                ]
                for e in ordered
            ]
            body.append(
                table(
                    ["model"]
                    + [f"Brier {F.dur(h)}" for h in horizons]
                    + ["qty bias", "ttf log-ratio median", "ttf IAD (vs AJ CIF)", "queue MAE"],
                    srows,
                    numeric=tuple(range(1, nh + 5)),
                    caption="Proper scores and timing on the same never-cancel shadows (same model order)",
                )
            )
            body.append(
                '<p class="lead">Bias = mean forecast minus exact fill rate (positive = optimistic), with its 95% '
                "block-bootstrap CI at every horizon; the first row gives the exact fill rate itself. The table is "
                "sorted by the absolute bias at "
                f"{F.esc(lbm.duration_text(hsort) if hsort else F.DASH)} (the horizon is a report setting, default "
                "60 s, or the "
                "longest horizon below it); Brier is only a tie-break, because models that never fill at short "
                "horizons all score 0 there. Truth labels at horizon h exist iff the shadow's planned (exogenous) life "
                "reaches entry + h, whatever its outcome; the truth fill rate is the Aalen-Johansen cumulative "
                'incidence ("bias vs AJ CIF", ttf IAD), compared with the mean forecast over the same label-eligible '
                "shadows. Overfill bias and the discordance tests compare model-filled (p ≥ 0.5) with exact at h_max; "
                "the cluster-robust test is primary (orders on one day are not independent), plain McNemar is "
                "secondary and is not computable when no shadow has an exact label at h_max.</p>"
            )
            for h in horizons:
                fig = _bias_forest(ctx, idx, models, h, "", "never-cancel shadows")
                if fig is None:
                    frows = [
                        (e.model_id, fb.value, fb.ci_lo, fb.ci_hi, f"{e.model_id}: {F.pp(fb.value)}")
                        for e in ordered
                        if (fb := e.fill_rate_bias.get(str(h))) is not None
                    ]
                    if not frows:
                        continue
                    svg = forest_chart(
                        ctx.uid(),
                        f"Signed fill-rate bias by model at {F.dur(h)}",
                        f"Model minus exact fill rate with 95% block-bootstrap CI at horizon {F.dur(h)}.",
                        frows,
                        x_fmt=lambda v: F.fixed(100 * v, 1),
                        x_label="fill-rate bias (pp, positive = model fills too often)",
                        slot=2,
                    )
                    fig = figure(
                        svg,
                        f"Horizon {F.dur(h)}: points right of zero are optimistic queue models.",
                        data=table(
                            ["model", "bias", "95% CI"],
                            [(r[0], F.pp(r[1]), F.ci(r[2], r[3], F.pp)) for r in frows],
                            numeric=(1,),
                        ),
                    )
                body.append(fig)
        else:
            body.append(
                callout(
                    INFO,
                    "Leaderboard not computed",
                    "<p>This is the Rust runner's summary; the Python evaluation (queuetruth.eval) has not "
                    "filled the leaderboard. Reliability below is computed directly from the predictions."
                    "</p>",
                )
            )
        body += _cancel_bearing(ctx, idx, models, horizons, href, h_max)
        body += _taker_table(run)
        if not horizons:
            pt = run.table("qt.zoo_predictions")
            if pt is not None:
                hz, _ = cols.i64(pt.read(), "horizon_ns")
                horizons = sorted(set(hz.tolist()))
        body += _reliability_from_predictions(ctx, run, models, horizons, h_max)
        body += _decile_bias(ctx, run, models, horizons, h_max)
        et = run.table("qt.zoo_errors")
        if et is not None and not ctx.compact:
            rs = [r for r in cols.pylist(et.read()) if r["stratum_kind"] == "all"]
            rs.sort(key=lambda r: (r["model_id"], r["metric"], r["horizon_ns"] if r["horizon_ns"] is not None else -1))
            if rs:
                body.append("<details><summary>All overall metrics (qt.zoo_errors, stratum all)</summary>")
                body.append(
                    table(
                        ["model", "metric", "horizon", "value", "95% CI", "n"],
                        [
                            (
                                r["model_id"],
                                r["metric"],
                                F.dur(r["horizon_ns"]),
                                F.num(r["value"]),
                                F.ci(r["ci_lo"], r["ci_hi"]),
                                F.num(r["n"]),
                            )
                            for r in rs
                        ],
                        numeric=(3, 5),
                    )
                )
                body.append("</details>")
    return section(
        "zoo",
        "Queue-model zoo vs exact truth",
        body,
        lead="Every model ran on the same orders and the same L2 view; truth is the exact FIFO replay.",
    )


# ---- calibration -------------------------------------------------------------------------------------------------


def _window(w: Any) -> str:
    if w.dates:
        return ", ".join(w.dates)
    if w.blocks:
        return f"blocks {w.blocks.index} of {w.blocks.n_blocks}"
    return "all data"


def _instrument_sessions(ctx: Ctx, c: CalibrationResult) -> dict[tuple[int, int], tuple[int, int, int]] | None:
    """(instrument_id, session_date days) -> (midnight_utc_ns, open_s, close_s) from the calibration's dataset
    (`instruments.parquet` of the spec echo `dataset.path`), recorded as a provenance file; None when unresolvable."""
    ds = c.spec.get("dataset")
    path = ds.get("path") if isinstance(ds, dict) else None
    if not isinstance(path, str):
        return None
    ip = Path(path) / "instruments.parquet"
    if not ip.is_file():
        return None
    try:
        tab = pq.read_table(ip, columns=["instrument_id", "session_date", "midnight_utc_ns", "open_s", "close_s"])
    except (OSError, pa.ArrowException, KeyError):
        return None
    shown = f"{path.rstrip('/')}/instruments.parquet"
    if not any(f.path == shown for f in ctx.inputs.files):
        ctx.inputs.files.append(
            FileRec(shown, "qt.instruments", ip.stat().st_size, sha256_file(ip), rows=tab.num_rows, role="provenance")
        )
    iid, _ = cols.i64(tab, "instrument_id")
    day, _ = cols.i64(tab, "session_date")
    mid, _ = cols.i64(tab, "midnight_utc_ns")
    op, _ = cols.i64(tab, "open_s")
    cl, _ = cols.i64(tab, "close_s")
    return {
        (int(a), int(b)): (int(m), int(o), int(e))
        for a, b, m, o, e in zip(iid.tolist(), day.tolist(), mid.tolist(), op.tolist(), cl.tolist(), strict=True)
    }


def _train_mask(ctx: Ctx, c: CalibrationResult, tab: pa.Table) -> tuple[np.ndarray, str]:
    """Rows of `qt.alloc_events` from train-window shadows (the rows the fit used, as `qt_calib`): session date in
    `train.dates`; block of the shadow's entry among `n_blocks` equal-duration blocks of the continuous session in
    `train.blocks` (entry approximated by the shadow's first allocation event). Returns the mask and its wording."""
    n = tab.num_rows
    w = c.train
    mask = np.ones(n, dtype=bool)
    if not w.dates and not w.blocks:
        return mask, "every allocation event (no train window: the fit used all data)"
    day, _ = cols.i64(tab, "session_date")
    words = []
    if w.dates:
        epoch = datetime.date(1970, 1, 1)
        want = {(datetime.date.fromisoformat(d) - epoch).days for d in w.dates}
        mask &= np.isin(day, list(want))
        words.append(f"session dates {', '.join(w.dates)}")
    if w.blocks:
        sessions = _instrument_sessions(ctx, c)
        if sessions is None:
            return np.ones(n, dtype=bool), (
                "every allocation event (train and test windows: the train blocks need the dataset's "
                "instruments.parquet, which did not resolve)"
            )
        sid = cols.strs(tab, "shadow_id")
        inst, _ = cols.i64(tab, "instrument_id")
        ts, _ = cols.i64(tab, "ts_event")
        first: dict[str, int] = {}
        for s_, t_ in zip(sid.tolist(), ts.tolist(), strict=True):
            first[s_] = min(first.get(s_, t_), t_)
        nb = w.blocks.n_blocks
        keep = set(w.blocks.index)
        blk = np.zeros(n, dtype=bool)
        for i in range(n):
            ss = sessions.get((int(inst[i]), int(day[i])))
            if ss is None:
                b = 0
            else:
                midnight, open_s, close_s = ss
                open_ns = midnight + open_s * 1_000_000_000
                length = max(close_s - open_s, 1) * 1_000_000_000
                b = min(max(int((first[str(sid[i])] - open_ns) / length * nb // 1), 0), nb - 1)
            blk[i] = b in keep
        mask &= blk
        words.append(f"blocks {w.blocks.index} of {nb} (entry = first allocation event)")
    return mask, "train-window events (" + "; ".join(words) + ")"


def _crossing(pts: list[tuple[float, float]], cut: float) -> tuple[float | None, float | None]:
    """Outermost parameter values where the relative log likelihood `pts` (sorted by parameter) crosses `cut`
    (linear interpolation); an end stays None when the region reaches the plotted range."""
    inside = [i for i, (_, v) in enumerate(pts) if v >= cut]
    if not inside:
        return None, None
    a, b = min(inside), max(inside)

    def interp(i: int, j: int) -> float:
        (x0, y0), (x1, y1) = pts[i], pts[j]
        return x0 if y1 == y0 else x0 + (cut - y0) * (x1 - x0) / (y1 - y0)

    lo = interp(a - 1, a) if a > 0 else None
    hi = interp(b, b + 1) if b + 1 < len(pts) else None
    return lo, hi


def _profile(ctx: Ctx, run: RunDir, c: CalibrationResult) -> list[str]:
    if c.kind != "alloc" or c.family not in ALLOC_FAMILIES:
        return []
    at = run.table("qt.alloc_events")
    if at is None:
        return []
    full = at.read()
    train, rows_text = _train_mask(ctx, c, full)
    tab = full.filter(pa.array(train))
    grid = default_grid(c.family)
    g = c.spec.get("grid") if isinstance(c.spec.get("grid"), dict) else None
    key = "databento" if c.family == "databento" else "alloc"
    if g and isinstance(g.get(key), dict):
        gg = g[key]
        try:
            grid = (float(gg["lo"]), float(gg["hi"]), int(gg["n"]))
        except (KeyError, TypeError, ValueError):
            pass
    args = (
        cols.f64(tab, "front"),
        cols.f64(tab, "back"),
        cols.f64(tab, "ahead_cancel"),
        cols.f64(tab, "behind_cancel"),
    )
    coarse = [(t, v) for t, v in alloc_profile(c.family, *args, grid) if math.isfinite(v)]
    if len(coarse) < 2:
        return []
    lmax = max(v for _, v in coarse)
    near = [i for i, (_, v) in enumerate(coarse) if v - lmax >= -12.0]
    lo_t = coarse[max(min(near) - 1, 0)][0]
    hi_t = coarse[min(max(near) + 1, len(coarse) - 1)][0]
    is_db = c.family == "databento"
    marks = [c.estimate, *(c.ci_bootstrap or ()), *(c.ci_profile or ())]
    for v in marks:
        if v is not None and (is_db or v > 0):
            th = v if is_db else math.log(v)
            lo_t, hi_t = min(lo_t, th), max(hi_t, th)
    pad = (hi_t - lo_t) * 0.05
    lo_t, hi_t = max(lo_t - pad, grid[0]), min(hi_t + pad, grid[1])
    fine = [(t, v) for t, v in alloc_profile(c.family, *args, (lo_t, hi_t, 161)) if math.isfinite(v)]
    lmax = max([lmax] + [v for _, v in fine])
    tx = (lambda t: t) if is_db else math.exp
    floor = -8.0
    pts = [(tx(t), max(v - lmax, floor)) for t, v in fine]
    x_lo, x_hi = tx(lo_t), tx(hi_t)
    use_log = (not is_db) and x_hi / x_lo > 10
    vrefs = []
    if c.estimate is not None and x_lo <= c.estimate <= x_hi:
        vrefs.append(Ref(c.estimate, f"{c.param_name or 'estimate'} = {F.num(c.estimate)}", "ref est"))
    if c.ci_bootstrap and c.ci_bootstrap[0] is not None and c.ci_bootstrap[1] is not None:
        vrefs += [
            Ref(v, lab)
            for v, lab in ((c.ci_bootstrap[0], "bootstrap 95%"), (c.ci_bootstrap[1], ""))
            if x_lo <= v <= x_hi
        ]
    cut = -PROFILE_CHI2_95 / 2
    c_lo, c_hi = _crossing([(t, v - lmax) for t, v in fine], cut)
    step = (hi_t - lo_t) / 160
    pub = c.ci_profile
    check = ""
    if pub and pub[0] is not None and pub[1] is not None and (is_db or (pub[0] > 0 and pub[1] > 0)):
        p_lo, p_hi = (pub[0], pub[1]) if is_db else (math.log(pub[0]), math.log(pub[1]))
        ok = all(a is not None and abs(a - b) <= 1.5 * step for a, b in ((c_lo, p_lo), (c_hi, p_hi)))
        shown = F.ci(tx(c_lo) if c_lo is not None else None, tx(c_hi) if c_hi is not None else None)
        check = (
            f" Its 95% cut-off crossing {shown} matches the published profile CI {F.ci(pub[0], pub[1])}."
            if ok
            else f" Its 95% cut-off crossing {shown} does NOT match the published profile CI {F.ci(pub[0], pub[1])}: "
            "the report could not select exactly the rows the fit used."
        )
    svg = line_chart(
        ctx.uid(),
        f"Allocation quasi-log-likelihood profile ({c.family})",
        f"Log-likelihood relative to its maximum around the estimate, evaluated on {rows_text}; the dashed "
        "horizontal line is the 95% profile cut-off (chi-square, 1 df).",
        [Series("profile", pts, slot=1)],
        x_fmt=lambda v: F.num(v, 3),
        y_fmt=lambda v: F.num(v, 3),
        x_log=use_log,
        x_range=(x_lo, x_hi),
        y_range=(floor, 0.5),
        hrefs=[Ref(cut, "95% profile (quasi)")],
        vrefs=vrefs,
        x_label="k" if is_db else ("n (log scale)" if use_log else "n"),
        y_label="ℓ(θ) − ℓ_max",
    )
    stride = max(len(coarse) // 40, 1)
    data = table(
        ["θ", "parameter", "ℓ(θ) − ℓ_max"],
        [(F.num(t), F.num(tx(t)), F.num(v - lmax)) for t, v in coarse[::stride]],
        numeric=(0, 1, 2),
    )
    return [
        figure(
            svg,
            f"Recomputed by the report from {at.rec.path}: {tab.num_rows:,} of {full.num_rows:,} events, {rows_text}; "
            "events with no queue in front or behind carry no information and are skipped." + check,
            data=data,
        )
    ]


def _live_profile(ctx: Ctx, c: CalibrationResult, doc: dict[str, Any] | None) -> list[str]:
    """Profile of the calibrate-live grid likelihood (`live_calibration_test.json` `grid`)."""
    grid = (doc or {}).get("grid") or []
    pts = []
    for g in grid:
        v, ll = g.get("value"), g.get("loglik")
        if F.is_num(v) and F.is_num(ll):
            pts.append((float(v), float(ll)))
    if len(pts) < 2:
        return []
    pts.sort()
    lmax = max(ll for _, ll in pts)
    floor = -8.0
    rel = [(v, max(ll - lmax, floor)) for v, ll in pts]
    lo, hi = pts[0][0], pts[-1][0]
    use_log = lo > 0 and hi / lo > 10
    vrefs = []
    if c.estimate is not None:
        vrefs.append(Ref(c.estimate, f"{c.param_name or 'estimate'} = {F.num(c.estimate)}", "ref est"))
    if c.ci_bootstrap and c.ci_bootstrap[0] is not None and c.ci_bootstrap[1] is not None:
        vrefs += [Ref(c.ci_bootstrap[0], "bootstrap 95%"), Ref(c.ci_bootstrap[1], "")]
    svg = line_chart(
        ctx.uid(),
        f"Live-fill log-likelihood profile ({c.family})",
        "Train log-likelihood of the live first-maker-fill times relative to its maximum over the parameter grid; the "
        "dashed horizontal line is the 95% profile cut-off.",
        [Series("profile", rel, slot=1, markers=len(rel) <= 40)],
        x_fmt=lambda v: F.num(v, 3),
        y_fmt=lambda v: F.num(v, 3),
        x_log=use_log,
        y_range=(floor, 0.5),
        hrefs=[Ref(-PROFILE_CHI2_95 / 2, "95% profile")],
        vrefs=vrefs,
        x_label=(c.param_name or "parameter") + (" (log scale)" if use_log else ""),
        y_label="ℓ(θ) − ℓ_max",
    )
    data = table(["parameter", "ℓ(θ) − ℓ_max"], [(F.num(v), F.num(ll - lmax)) for v, ll in pts], numeric=(0, 1))
    return [figure(svg, "Each grid value is one zoo model run on the same live orders (no refinement).", data=data)]


def _live_held_out(c: CalibrationResult, doc: dict[str, Any] | None) -> list[str]:
    """Held-out discordance of a calibrate-live fit: the 1.1 field `held_out_discordance`, else
    `live_calibration_test.json`, else the v0.1 stratified rows `held_out_test`."""
    rows = {x.stratum: x for x in c.stratified if x.stratum_kind == "held_out_test"}
    hd = c.held_out_discordance
    if hd is None and not rows and not doc:
        return []
    d: dict[str, Any] = dict(doc or {})
    if hd is not None:
        d.update(hd.model_dump())

    def est(name: str) -> float | None:
        r = rows.get(name)
        return r.estimate if r is not None else None

    fb = rows.get("fill_rate_bias")
    n = d.get("n", fb.n if fb else None)
    b = d.get("b", est("mcnemar_b"))
    cc = d.get("c", est("mcnemar_c"))
    p_cl = d.get("p_clustered", est("discordance_p_clustered"))
    method = d.get("clustered_method")
    k = d.get("n_clusters")
    bias = d["fill_rate_bias"] if "fill_rate_bias" in d else (fb.estimate if fb else None)
    lo = d["ci_lo"] if "ci_lo" in d else (fb.ci_lo if fb else None)
    hi = d["ci_hi"] if "ci_hi" in d else (fb.ci_hi if fb else None)
    return [
        "<h4>Held-out test orders: fitted model vs your live fills</h4>",
        kv(
            [
                ("test orders", F.num(n)),
                ("fill-rate bias (model − live) at H", f"{F.pp(bias)} {F.ci(lo, hi, F.pp)}"),
                ("discordance p, cluster-robust (primary)", F.pval(p_cl)),
                ("method / clusters", f"{si_method(method)} / {F.num(k)}"),
                ("b live-only / c model-only", f"{F.num(b)} / {F.num(cc)}"),
                ("McNemar exact p (secondary)", F.pval(d.get("p_exact", est("mcnemar_p_exact")))),
            ]
        ),
    ]


def _verdict(c: CalibrationResult) -> list[str]:
    """Exportability verdict (trader review D1, D2, E1): the 1.1 field `trust` (verdict, refusing reasons,
    flags, per-horizon `beats_default`) when present, else `queuetruth.export.trust.assess` (the `trust:` warning lines
    plus the held-out comparison with the library default). Reason texts and the held-out numbers come from
    `assess` when it is available."""
    t = None
    summary = None
    try:
        from queuetruth.export.trust import assess, held_out_summary

        t = assess(c)
        summary = held_out_summary(t)
    except Exception:  # the verdict decorates the section; a failing assessor must not break it
        t = None
    native = c.trust
    if native is None and t is None:
        return []
    texts: dict[str, str] = {}
    if t is not None:
        texts.update(t.flags)
        texts.update(dict(t.reasons))
    if native is not None:
        ok = native.verdict == "exportable"
        reasons = list(native.reasons)
        flags = [f for f in native.flags if f not in reasons]
        labels = dict(native.beats_default)
        source = "trust field of the calibration result"
    else:
        assert t is not None
        ok = t.ok
        reasons = [k for k, _ in t.reasons]
        flags = [k for k in t.flags if k not in reasons and k not in ("beats_default", "verdict")]
        labels = {str(h.horizon_ns): h.label for h in t.horizons}
        source = "queuetruth.export.trust (from the trust: warning lines and the held-out metrics)"
    by_h = {str(h.horizon_ns): h for h in t.horizons} if t is not None else {}
    rows = []
    for k in sorted(set(labels) | set(by_h), key=int):
        h = by_h.get(k)
        rows.append(
            (
                F.dur(int(k)),
                F.num(h.fitted_logloss, 5) if h else F.DASH,
                F.num(h.default_logloss, 5) if h else F.DASH,
                F.num(h.fitted_brier, 5) if h else F.DASH,
                F.num(h.default_brier, 5) if h else F.DASH,
                labels.get(k, h.label if h else "n/a"),
            )
        )

    def line(k: str) -> str:
        return f"{k}: {texts[k]}" if texts.get(k) else k

    parts = []
    if c.kind != "latency" and summary:
        parts.append(f"<p>{F.esc(summary)}.</p>")
    if reasons:
        parts.append(ul([line(k) for k in reasons]))
    if flags:
        parts.append(f"<p>Other diagnostics: {F.esc(', '.join(flags))}.</p>")
    if rows:
        parts.append(
            table(
                ["horizon", "log loss fitted", "log loss default", "Brier fitted", "Brier default", "verdict"],
                rows,
                numeric=(1, 2, 3, 4),
                caption="Held-out comparison with the library default (lower is better on both scores)",
            )
        )
    parts.append(f'<p class="note">Verdict source: {F.esc(source)}.</p>')
    if ok:
        return [callout(GOOD, "Exportable: the fit is identified and beats the default out of sample", "".join(parts))]
    return [
        callout(
            CRIT,
            "Not exportable: queuetruth export refuses this calibration (--force writes it with a warning header)",
            "".join(parts),
        )
    ]


def si_method(m: Any) -> str:
    """Readable name of a clustered discordance method."""
    return {"obuchowski": "Obuchowski", "block_bootstrap": "block bootstrap"}.get(
        str(m), F.DASH if m is None else str(m)
    )


def calibration(ctx: Ctx) -> Raw | None:
    """Estimates and CIs, likelihood profile, held-out comparison, stratified estimates, latency, exports."""
    runs = ctx.runs_with(CalibrationResult)
    if not runs:
        return None
    body: list[str] = []
    for run, c in runs:
        body.append(f"<h3>{F.esc(_label(run, c))}: {F.esc(c.kind)} · {F.esc(c.family)}</h3>")
        body.append(
            kv(
                [
                    ("parameter", c.param_name or F.DASH),
                    ("estimate", F.num(c.estimate, 5)),
                    ("95% bootstrap CI (primary)", F.ci(*(c.ci_bootstrap or (None, None)))),
                    ("95% profile CI (quasi)", F.ci(*(c.ci_profile or (None, None)))),
                    ("log-likelihood", F.num(c.loglik, 6)),
                    ("mode", c.mode),
                    ("events / shadows", f"{c.n_events:,} / {c.n_shadows:,}"),
                    ("train", _window(c.train)),
                    ("test (held out)", _window(c.test)),
                ]
            )
        )
        if c.kind == "nt_touch":
            body.append(
                callout(
                    INFO,
                    "p is per touch",
                    "<p>The fitted fill probability applies per at-price touch and is not portable across "
                    "feeds with different print rates.</p>",
                )
            )
        live = c.spec.get("calibration_source") == "live"
        live_doc = run.extra("qt.live_calibration_test") if live else None
        if live:
            body.append(
                callout(
                    INFO,
                    "Fitted against your own live fills (calibrate-live)",
                    "<p>No exact queue truth was used: the parameter maximizes a censored discrete-time likelihood of "
                    "your live orders' first maker fill inside the evaluation window, on a chronological train split; "
                    "the held-out rows below are the later orders.</p>",
                )
            )
        verdict = _verdict(c)
        body += verdict
        other = [w for w in c.warnings if not (verdict and w.startswith("trust: "))]
        if other:
            body.append(callout(WARN, "Calibration warnings", ul(other)))
        body += _live_profile(ctx, c, live_doc) if live else _profile(ctx, run, c)
        if live:
            body += _live_held_out(c, live_doc)
        if c.held_out or c.baseline_held_out:
            hs = sorted({int(k) for ho in [*c.held_out.values(), *c.baseline_held_out.values()] for k in ho.brier})
            series, rows = [], []
            slot = 1
            for label, group, dashed in (("fitted", c.held_out, False), ("default", c.baseline_held_out, True)):
                for mid in sorted(group):
                    ho = group[mid]
                    pts = [(float(h), ho.brier.get(str(h))) for h in hs]
                    series.append(Series(f"{label}: {mid}", pts, slot=min(slot, 4), markers=True, dashed=dashed))
                    slot += 1
                    rows += [
                        (
                            label,
                            mid,
                            F.dur(h),
                            F.fixed(ho.brier.get(str(h)), 4),
                            F.fixed(ho.logloss.get(str(h)), 4),
                            F.pp(ho.fill_rate_bias.get(str(h))),
                        )
                        for h in hs
                    ]
            if hs and series:
                svg = line_chart(
                    ctx.uid(),
                    "Held-out Brier score: fitted vs default parameters",
                    "Brier score on the held-out window per horizon for the fitted model and the defaults.",
                    series,
                    x_fmt=F.dur_axis,
                    y_fmt=lambda v: F.num(v, 3),
                    x_log=True,
                    x_label="horizon (log)",
                    y_label="Brier (lower is better)",
                )
                body.append(
                    figure(
                        svg,
                        "Out-of-sample check: the fit only earns its place if it beats the default "
                        "parameters on data it never saw.",
                        items=[(s.name, s.slot, s.dashed) for s in series],
                        data=table(
                            ["arm", "model", "horizon", "Brier", "log-loss", "fill-rate bias"], rows, numeric=(3, 4, 5)
                        ),
                    )
                )
        strat = [x for x in c.stratified if x.stratum_kind != "held_out_test"]
        if strat:
            body.append("<h4>Stratified estimates</h4>")
            body.append(
                table(
                    ["stratum kind", "stratum", "estimate", "95% CI", "n"],
                    [
                        (s.stratum_kind, s.stratum, F.num(s.estimate, 5), F.ci(s.ci_lo, s.ci_hi), F.num(s.n))
                        for s in strat
                    ],
                    numeric=(2, 4),
                )
            )
        if c.latency is not None:
            lt = c.latency
            body.append("<h4>Latency (one-way figures assume synchronized clocks)</h4>")
            body.append(
                table(
                    ["leg", "p10", "p50", "p90", "p99", "n"],
                    [
                        (name, F.dur(q.p10), F.dur(q.p50), F.dur(q.p90), F.dur(q.p99), F.num(q.n))
                        for name, q in (
                            ("entry (send → exchange)", lt.entry_ns),
                            ("response (exchange → local)", lt.response_ns),
                            ("cancel", lt.cancel_ns),
                            ("amend", lt.amend_ns),
                        )
                    ],
                    numeric=(1, 2, 3, 4, 5),
                )
            )
        ex = c.exports
        for name, snip in (("hftbacktest", ex.hftbacktest), ("NautilusTrader", ex.nautilus)):
            if snip is None:
                continue
            body.append(f"<h4>Export: {name}</h4>")
            body.append(code_block(snip.python, f"{name} config (paste-ready)"))
            if snip.notes:
                body.append(ul(snip.notes))
        if ex.latency_npz:
            body.append(kv([("IntpOrderLatency .npz", ex.latency_npz)]))
        if c.data_sources:
            body.append(
                table(
                    ["dataset", "venue", "source venue", "validation status"],
                    [(d.dataset_id, d.venue, d.source_venue, d.validation_status) for d in c.data_sources],
                )
            )
    return section(
        "calibration",
        "Calibration",
        body,
        lead="Parameters fitted on the train window (against exact queue truth, or against your own live fills for "
        "calibrate-live) and scored on the held-out window.",
    )


# ---- learned model -----------------------------------------------------------------------------------------------


def learned_model(ctx: Ctx) -> Raw | None:
    """Hazard model card scores, BSS vs climatology, reliability per horizon, comparison with zoo models."""
    runs = ctx.runs_with(HazardModelCard)
    if not runs:
        return None
    body: list[str] = []
    for run, card in runs:
        sc = card.scores_test
        body.append(f"<h3>{F.esc(run.label)}: {F.esc(card.model_id)}</h3>")
        if card.privileged:
            body.append(
                callout(
                    WARN,
                    "Privileged upper bound",
                    "<p>This model uses exact-queue features an L2 strategy cannot observe; it bounds what "
                    "better queue estimation could buy, it is not deployable.</p>",
                )
            )
        body.append(
            kv(
                [
                    ("split", f"{card.split.scheme}"),
                    (
                        "orders train / calib / test",
                        f"{card.n_orders.train:,} / {card.n_orders.calib:,} / {card.n_orders.test:,}",
                    ),
                    ("person-period rows (train)", F.num(card.n_rows_train)),
                    ("recalibration", card.recalibration),
                    ("RCLL (primary, lower is better)", F.num(sc.rcll, 5)),
                    ("integrated Brier", F.num(sc.ibs, 5)),
                    ("Uno c-index at 10 s (improper)", F.num(sc.c_index_uno, 4)),
                    ("features", F.num(len(card.feature_names))),
                ]
            )
        )
        if card.warnings:
            body.append(callout(WARN, "Model warnings", ul(card.warnings)))
        hs = sorted({int(k) for k in sc.brier} | {int(k) for k in sc.ipcw_logloss})
        et = sc.hl_p_et if sc.hl_p_et and any(v is not None for v in sc.hl_p_et.values()) else None
        rows = []
        for h in hs:
            k = str(h)
            b = sc.bss_vs_climatology.get(k)
            row = [
                F.dur(h),
                F.num(sc.ipcw_logloss.get(k), 4),
                F.num(sc.brier.get(k), 4),
                f"{F.num(b.value if b else None, 3)} {F.ci(b.ci_lo, b.ci_hi) if b else ''}".strip(),
                F.num(sc.calib_slope.get(k), 3),
                F.num(sc.calib_intercept.get(k), 3),
                F.num(sc.spiegelhalter_z.get(k), 3),
                F.pval(sc.hl_p.get(k)),
            ]
            if et is not None:
                row.append(F.pval(et.get(k)))
            rows.append((*row, F.num(sc.auc.get(k), 3)))
        heads = [
            "horizon",
            "IPCW log-loss",
            "IPCW Brier",
            "BSS vs climatology [95% CI]",
            "calib. slope",
            "calib. intercept",
            "Spiegelhalter z",
            "HL p",
        ]
        if et is not None:
            heads.append("HL p, 8 df (et)")
        heads.append("AUC (improper)")
        body.append(
            table(
                heads,
                rows,
                numeric=tuple(i for i in range(1, len(heads)) if i != 3),
                caption="Calibration diagnostics are IPCW-weighted (rows censored before the horizon weigh 0); the "
                "intercept is calibration-in-the-large (slope fixed at 1); HL p uses one df per bin on this held-out "
                "split",
            )
        )
        frows = [
            (
                F.dur(int(k)),
                v.value,
                v.ci_lo,
                v.ci_hi,
                f"{F.dur(int(k))}: BSS {F.num(v.value, 3)} {F.ci(v.ci_lo, v.ci_hi)}",
            )
            for k, v in sorted(sc.bss_vs_climatology.items(), key=lambda kv_: int(kv_[0]))
        ]
        if frows:
            svg = forest_chart(
                ctx.uid(),
                "Brier skill vs climatology by horizon",
                "Brier skill score of the learned model against the Aalen-Johansen climatology, 95% CI.",
                frows,
                x_fmt=lambda v: F.num(v, 2),
                x_label="BSS (positive = better than climatology)",
                slot=3,
            )
            data = table(
                ["horizon", "BSS", "95% CI"], [(r[0], F.num(r[1], 3), F.ci(r[2], r[3])) for r in frows], numeric=(1,)
            )
            body.append(
                figure(svg, "Skill above zero means the L2 features carry information beyond the base rate.", data=data)
            )
        pt = run.table("qt.hazard_predictions")
        if pt is not None:
            tab = pt.read()
            split = cols.strs(tab, "split")
            hz, _ = cols.i64(tab, "horizon_ns")
            p = cols.f64(tab, "cif_fill")
            y, yok = cols.boolean(tab, "y_fill")
            cens, _ = cols.boolean(tab, "censored")
            ok = (split == "test") & yok & ~cens & np.isfinite(p)
            panels, drows = [], []
            for h in sorted(set(hz[ok].tolist())):
                sel = ok & (hz == h)
                bins = reliability_bins(p[sel], y[sel].astype(np.float64))
                panels.append(
                    reliability_panel(
                        ctx.uid(),
                        f"learned · {F.dur(h)}",
                        f"Reliability of the recalibrated fill CIF at {F.dur(h)} on the test split, "
                        f"n = {int(sel.sum()):,}.",
                        bins,
                        slot=3,
                        label=f"horizon {F.dur(h)} · n = {int(sel.sum()):,}",
                    )
                )
                drows += [
                    (
                        F.dur(h),
                        f"[{b.lo:.1f}, {b.hi:.1f}]",
                        F.num(b.n),
                        F.fixed(b.p_mean, 3) if b.n else F.DASH,
                        F.fixed(b.obs, 3) if b.n else F.DASH,
                        F.ci(b.ci_lo, b.ci_hi, lambda v: F.fixed(v, 3)) if b.n else F.DASH,
                    )
                    for b in bins
                ]
            if panels:
                body.append("<h4>Reliability on the test split (recalibrated CIF of fill)</h4>")
                body.append(
                    '<p class="lead">Unweighted, on the rows whose status at the horizon is known (rows censored '
                    "before it are dropped). Where many rows are censored early this over-represents fills, so a "
                    "calibrated model can sit below the diagonal; the calibration intercept, Spiegelhalter z and HL "
                    "p in the table above are IPCW-weighted and correct for it.</p>"
                )
                body.append(f'<div class="relgrid">{"".join(panels)}</div>')
                body.append(
                    "<details><summary>Data table</summary>"
                    + table(
                        ["horizon", "bin", "n", "mean forecast", "observed", "95% Wilson CI"], drows, numeric=(2, 3, 4)
                    )
                    + "</details>"
                )
        if card.zoo_comparison:
            zc = card.zoo_comparison
            own = zc.get(card.model_id)
            zoo_ids = sorted(m for m in zc if m != card.model_id)

            def mean_b(d: dict[str, float | None]) -> float:
                v = [x for x in d.values() if x is not None]
                return sum(v) / len(v) if v else math.inf

            if zoo_ids:
                hz2 = sorted({int(k) for m in zoo_ids for k in zc[m].brier})
                best = min(zoo_ids, key=lambda m: mean_b(zc[m].brier))
                mine = own.brier if own is not None else sc.brier
                series = [
                    Series("learned model", [(float(h), mine.get(str(h))) for h in hz2], slot=3, markers=True),
                    Series(
                        f"best zoo: {best}",
                        [(float(h), zc[best].brier.get(str(h))) for h in hz2],
                        slot=2,
                        markers=True,
                        dashed=True,
                    ),
                ]
                svg = line_chart(
                    ctx.uid(),
                    "Learned model vs best zoo model (Brier on the same test shadows)",
                    "Brier score by horizon of the learned hazard model and the zoo model with the lowest mean "
                    "Brier, against the exact never-cancel fill on the same test shadows.",
                    series,
                    x_fmt=F.dur_axis,
                    y_fmt=lambda v: F.num(v, 3),
                    x_log=True,
                    x_label="horizon (log)",
                    y_label="Brier (lower is better)",
                )
                rows = [("learned model", *[F.fixed(mine.get(str(h)), 4) for h in hz2])]
                rows += [(m, *[F.fixed(zc[m].brier.get(str(h)), 4) for h in hz2]) for m in zoo_ids]
                if own is not None:
                    cap = (
                        "Same test shadows, landmark 0: never-cancel shadows whose planned life covers the horizon, "
                        "scored against their exact never-cancel fill (the zoo's truth), so the policy cancel of the "
                        "learned model's labels does not compete (see the model warnings for row counts)."
                    )
                else:
                    cap = (
                        "Older model card: the learned model's line is its IPCW Brier on all test rows against its own "
                        "labels (policy cancel competing), not on the zoo's rows; the two are not directly comparable."
                    )
                body.append(
                    figure(
                        svg,
                        cap,
                        items=[(x.name, x.slot, x.dashed) for x in series],
                        data=table(["model", *[F.dur(h) for h in hz2]], rows, numeric=tuple(range(1, len(hz2) + 1))),
                    )
                )
    return section(
        "learned_model",
        "Learned L2 fill model",
        body,
        lead="Discrete-time competing-risk hazard on L2 features, labelled by exact truth, scored with proper "
        "rules on a later test window.",
    )
