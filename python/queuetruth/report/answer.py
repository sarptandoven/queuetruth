"""The answer block: the first section after the header. One plain sentence per question the inputs can answer,
each with its base rate and 95% interval where the inputs carry them, a status badge (icon and label, never color
alone) and a link to the drill-down that backs it.

Questions, in order, each asked once per input run (the referee: once per simulator) that answers it:

- zoo (`qt.zoo_summary` with a leaderboard): which L2 queue model fills most like the exact replay, and would a
  backtest with them overstate passive fills (fill significantly more often than the exact replay) at the
  leaderboard's sort horizon;
- scorecard (`qt.scorecard_summary`): does the simulator fill like live trading;
- calibration (`qt.calibration_result`): can the fitted parameters be exported;
- learned model (`qt.hazard_model_card`): does it beat the climatology base rate;
- referee (`qt.referee_report`): does the simulator reproduce the exact FIFO fills;
- data quality (scorecard, replay or `qt.data_quality` verdicts): is the data clean enough to trust the numbers,
  naming the inputs that carry no verdict.

Absence of evidence is never shown as evidence: a missing 95% CI gives a neutral NO CI badge, never "within noise"
or "no skill"; a CI that contains 0 but is wider than `ZOO_MARGIN` is INCONCLUSIVE, not a pass. Every figure in a
sentence is read from the same fields the drill-down sections show.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from queuetruth.eval import leaderboard as lbm
from queuetruth.report import fmt as F
from queuetruth.report import scoreinfo as si
from queuetruth.report.ctx import Ctx
from queuetruth.report.page import CRIT, GOOD, INFO, NEUTRAL, WARN, Raw, Status, badge, section
from queuetruth.report.sections_models import ZooView, exportable, zoo_view
from queuetruth.report.sections_referee import BUG_CLASSES, class_dir, pretty_class
from queuetruth.report.sections_score import LIMITS_ID, sim_label
from queuetruth.report.why import WARNINGS_ID, summary_warnings
from queuetruth.schemas.models import (
    CalibrationResult,
    DataQuality,
    HazardModelCard,
    RefereeReport,
    ReplaySummary,
    ScorecardSummary,
    ZooSummary,
)
from queuetruth.schemas.specs import EquivalenceSpec

#: Models (or inputs) named in a sentence before "and N more".
MAX_NAMED = 3
#: The learned model's headline horizon when present (else the median horizon of its scores).
LEARNED_HORIZON_NS = 10_000_000_000
#: The closest zoo model is called WITHIN NOISE only when its whole 95% CI lies within this fill-rate margin (a
#: fraction): the scorecard's default TOST equivalence margin, so a CI too wide to show anything is not a pass.
ZOO_MARGIN: float = EquivalenceSpec.model_fields["fill_rate_margin"].default
#: Outputs whose numbers a data-quality verdict speaks for (an input without one is named in that answer).
_ANALYSES = (ZooSummary, ScorecardSummary, CalibrationResult, HazardModelCard, RefereeReport, ReplaySummary)


@dataclass
class Answer:
    """One question with its one-sentence answer."""

    status: Status
    label: str
    question: str
    sentence: str
    href: str
    link: str


def _has_ci(lo: float | None, hi: float | None) -> bool:
    return lo is not None and hi is not None


def _ci_pp(lo: float | None, hi: float | None) -> str:
    return f"95% CI {F.pp(lo)} to {F.pp(hi)}" if _has_ci(lo, hi) else "no CI"


def _ci_num(lo: float | None, hi: float | None, sig: int) -> str:
    return f"95% CI {F.num(lo, sig)} to {F.num(hi, sig)}" if _has_ci(lo, hi) else "no CI"


def _p(p: float | None) -> str:
    """`p = 0.012` or `p <1e-300` (the formatter already carries the comparison for tiny values)."""
    v = F.pval(p)
    return f"p {v}" if v.startswith("<") else f"p = {v}"


def _excludes_zero(lo: float | None, hi: float | None) -> bool:
    return lo is not None and hi is not None and (lo > 0 or hi < 0)


def _suffix(label: str, many: bool) -> str:
    return f" ({label})" if many else ""


def _names(xs: Sequence[str]) -> str:
    head = ", ".join(xs[:MAX_NAMED])
    return head + (f" and {len(xs) - MAX_NAMED} more" if len(xs) > MAX_NAMED else "")


def _margin_pp(m: float) -> str:
    return f"±{F.fixed(100 * m, 1)} pp"


# ---- zoo ---------------------------------------------------------------------------------------------------------


def warnings_naming(warnings: Sequence[str] | None, model_id: str) -> int:
    """How many zoo warning lines name `model_id` as a whole id (`hbt_power[n=1]` does not match
    `hbt_power3[n=1]`, nor `hbt_power` match `hbt_power[n=1]`)."""
    pat = re.compile(rf"(?<![\w\[]){re.escape(model_id)}(?![\w\[])")
    return sum(1 for w in warnings or () if isinstance(w, str) and pat.search(w))


def _zoo(v: ZooView, many: bool) -> list[Answer]:
    h = v.hsort
    if h is None or not v.ordered:
        return []
    when = lbm.duration_text(h)
    tag = _suffix(v.run.label, many)
    q1 = f"Which L2 queue model fills most like the exact replay?{tag}"
    exact = next((r for e in v.ordered if (r := lbm.rates_at(e, h)[1]) is not None), None)
    n = v.z.n_leaderboard
    pop = f"the {n:,} never-cancel orders" if n is not None else "the never-cancel orders"
    best = v.ordered[0]
    b, lo, hi = lbm.bias_at(best, h)
    rate = lbm.rates_at(best, h)[0]
    if b is None:
        return [
            Answer(
                NEUTRAL,
                "NO LABELS",
                q1,
                f"No shadow has an exact label at {when}, so no model's fill-rate bias can be measured there.",
                "#leaderboard",
                "Leaderboard",
            )
        ]
    # (model id, bias, ci_lo, ci_hi) in rank order, for every model with a bias at the sort horizon
    measured = [(e.model_id, *lbm.bias_at(e, h)) for e in v.ordered]
    measured = [r for r in measured if r[1] is not None]
    with_ci = [r for r in measured if _has_ci(r[2], r[3])]
    noise = [r[0] for r in with_ci if r[2] <= 0.0 <= r[3]]
    m, k = len(measured), len(with_ci)
    margin = _margin_pp(ZOO_MARGIN)

    if rate is not None:
        what = f"fills {F.pct(rate)} ({F.pp(b)}, {_ci_pp(lo, hi)})"
    else:
        what = f"has the smallest fill-rate bias, {F.pp(b)} ({_ci_pp(lo, hi)})"
    s = f"At {when}, the exact replay fills {F.pct(exact)} of {pop}. The" if exact is not None else f"At {when}, the"
    s += f" closest model by point estimate is {best.model_id}, which {what}"
    if not _has_ci(lo, hi):
        st, lab = INFO, "NO CI"
        s += "; no 95% interval could be computed (for example a single cluster), so it cannot be called within noise."
    elif _excludes_zero(lo, hi):
        st, lab = WARN, "BIASED"
        s += "; even the closest model is biased."
    elif -ZOO_MARGIN <= lo and hi <= ZOO_MARGIN:
        st, lab = GOOD, "WITHIN NOISE"
        s += f"; its 95% CI contains 0 and lies within {margin}."
    else:
        st, lab = INFO, "INCONCLUSIVE (WIDE CI)"
        s += f"; its 95% CI contains 0 but reaches beyond {margin}, too wide to show the model is accurate."
    if noise == [best.model_id]:
        s += f" It is the only one of the {m} models whose 95% CI contains 0."
    elif noise:
        # statistical ties: every model whose CI contains 0, in rank order
        s += f" {len(noise)} of {m} models have a 95% CI that contains 0: {_names(noise)}."
    elif k:
        s += f" None of the {m} models has a 95% CI that contains 0."
    if m > k:
        s += f" {m - k} of {m} models have no CI."
    nw = warnings_naming(v.z.warnings, best.model_id)
    if nw:
        s += f" {nw} zoo warning(s) name {best.model_id}; see the zoo drill-down."
    out = [Answer(st, lab, q1, s, "#leaderboard", "Leaderboard with every model and horizon")]

    over = sorted(((mid, x) for mid, x, xlo, _ in with_ci if xlo > 0), key=lambda t: (-t[1], t[0]))
    under = sorted(((mid, x) for mid, x, _, xhi in with_ci if xhi < 0), key=lambda t: (t[1], t[0]))

    def named(xs: list[tuple[str, float]]) -> str:
        return _names([f"{mid} {F.pp(x)}" for mid, x in xs])

    if not k:
        s2 = f"Cannot tell: no model has a 95% CI at {when}."
        st2, lab2 = INFO, "NO CI"
    elif over:
        s2 = (
            f"Yes: {len(over)} of {k} models with a 95% CI fill significantly more often than the exact FIFO replay "
            f"of the same orders at {when} (CI above 0): {named(over)}. A backtest with them overstates passive "
            "fill rates."
        )
        st2, lab2 = CRIT, "OPTIMISTIC MODELS"
    elif k < m:
        s2 = (
            f"None of the {k} models with a 95% CI fills significantly more often than the exact FIFO replay of the "
            f"same orders at {when}."
        )
        st2, lab2 = INFO, "PARTLY JUDGED"
    else:
        s2 = f"No: no model fills significantly more often than the exact FIFO replay of the same orders at {when}."
        st2, lab2 = GOOD, "NONE OPTIMISTIC"
    if under:
        s2 += f" {len(under)} of {k} fill significantly less often: {named(under)}."
    if k and k < m:
        s2 += f" {m - k} model(s) have no CI and are not judged."
    out.append(
        Answer(
            st2,
            lab2,
            f"Would a backtest with these models overstate passive fills?{tag}",
            s2,
            "#zoo",
            "Signed-bias charts and bias by queue decile",
        )
    )
    return out


# ---- scorecard ---------------------------------------------------------------------------------------------------


def _score(run: Any, s: ScorecardSummary, many: bool) -> Answer:
    q = f"Does the simulator fill like live trading?{_suffix(run.label, many)}"
    if si.not_scored(s):
        return Answer(
            CRIT,
            "NOT SCORED",
            q,
            f"No: none of the {s.n_orders:,} live orders reached the simulator comparison, so nothing was scored.",
            "#verdict",
            "Scorecard verdict and warnings",
        )
    if si.blocked_by_own_orders(s):
        return Answer(
            CRIT,
            "BLOCKED",
            q,
            "Not answerable: the queue models ran on L2 depth that still holds your own resting orders, so the "
            "simulator underfills by construction; re-run with self-exclusion.",
            "#verdict",
            "Scorecard verdict",
        )
    if si.fills_only(s):
        return Answer(
            WARN,
            "FILLS ONLY",
            q,
            "Not answerable for fill rates: the live log holds fills but not the orders that never filled; markouts "
            "and time to fill of filled orders are in the drill-down.",
            "#verdict",
            "Scorecard verdict",
        )
    t = s.two_by_two
    n = t.both + t.neither + t.live_only + t.sim_only
    live = (t.both + t.live_only) / n if n else None
    sim = (t.both + t.sim_only) / n if n else None
    b = s.bias
    cl = si.clustered(run, s)
    eq = s.equivalence
    sentence = (
        f"Within {si.window_text(s)} of entry, the simulator ({sim_label(s)}) fills {F.pct(sim)} of {n:,} scored "
        f"orders vs {F.pct(live)} live: {F.pp(b.fill_rate_bias)} ({_ci_pp(b.ci_lo, b.ci_hi)})"
    )
    sentence += f"; cluster-robust discordance {_p(cl.p)}" if cl is not None and cl.p is not None else ""
    sentence += (
        f"; equivalent within ±{F.fixed(100 * eq.fill_rate_margin, 1)} pp (TOST {_p(eq.tost_p)})."
        if eq.fill_rate_equivalent
        else f"; not shown equivalent within ±{F.fixed(100 * eq.fill_rate_margin, 1)} pp."
    )
    gated = si.gates_configured(s)
    if gated and not s.verdict.gates_passed:
        st, lab = CRIT, "GATES FAILED"
    elif b.direction != "no_significant_bias" and eq.fill_rate_equivalent:
        # a significant but small bias that TOST shows inside the margin: realism holds within the margin
        st, lab = WARN, "BIASED, WITHIN MARGIN"
    elif b.direction == "sim_overfills":
        st, lab = CRIT, "SIM OVERFILLS"
    elif b.direction == "sim_underfills":
        st, lab = WARN, "SIM UNDERFILLS"
    elif eq.fill_rate_equivalent:
        st, lab = GOOD, "EQUIVALENT"
    else:
        st, lab = INFO, "INCONCLUSIVE"
    return Answer(st, lab, q, sentence, "#verdict", "Scorecard verdict, 2×2 and tests")


# ---- calibration, learned model, referee, data quality ------------------------------------------------------------


def _calibration(run: Any, c: CalibrationResult, many: bool) -> Answer:
    q = f"Can I export the fitted {c.family} parameters?{_suffix(run.label, many)}"
    ok, summary = exportable(c)
    lo, hi = c.ci_bootstrap or (None, None)
    est = (
        f"{c.param_name or 'the parameter'} = {F.num(c.estimate, 4)} (95% bootstrap CI {F.num(lo, 4)} to "
        f"{F.num(hi, 4)}) from {c.n_events:,} events on {c.n_shadows:,} shadows"
        if c.estimate is not None
        else f"no point estimate from {c.n_events:,} events on {c.n_shadows:,} shadows"
    )
    tail = f"; {summary}." if summary else "."
    if ok is None:
        return Answer(INFO, "NOT ASSESSED", q, f"Not assessed: {est}{tail}", "#calibration", "Calibration")
    if ok:
        return Answer(GOOD, "EXPORTABLE", q, f"Yes: {est}{tail}", "#calibration", "Calibration and export code")
    return Answer(CRIT, "NOT EXPORTABLE", q, f"No: {est}{tail}", "#calibration", "Calibration and refusal reasons")


def _learned(run: Any, card: HazardModelCard, many: bool) -> Answer | None:
    bss = card.scores_test.bss_vs_climatology
    if not bss:
        return None
    keys = sorted(bss, key=int)
    with_ci = [k for k in keys if _has_ci(bss[k].ci_lo, bss[k].ci_hi)]
    k_pos = sum(1 for k in with_ci if bss[k].ci_lo > 0)
    hk = str(LEARNED_HORIZON_NS) if str(LEARNED_HORIZON_NS) in bss else keys[len(keys) // 2]
    v = bss[hk]
    at = f"at {F.dur(int(hk))} its Brier skill score is {F.num(v.value, 3)}"
    head = f"On {card.n_orders.test:,} held-out orders, "
    if not with_ci:
        s = (
            f"{head}{card.model_id}'s Brier skill score against climatology has no 95% CI at any of its "
            f"{len(keys)} horizons; {at} (above 0 = better than the base rate), a point estimate that alone does "
            "not show skill."
        )
        st, lab = INFO, "NO CI"
    else:
        n_none = len(keys) - len(with_ci)
        of = (
            f"{k_pos} of {len(keys)} horizons"
            if not n_none
            else f"{k_pos} of the {len(with_ci)} horizons with a CI ({n_none} {'has' if n_none == 1 else 'have'} none)"
        )
        s = (
            f"{head}{card.model_id} beats climatology with 95% confidence at {of}; {at} "
            f"({_ci_num(v.ci_lo, v.ci_hi, 3)}; above 0 = better than the base rate)."
        )
        if k_pos == 0:
            st, lab = CRIT, "NO SKILL"
        elif card.privileged:
            st, lab = INFO, "UPPER BOUND (PRIVILEGED)"
        elif k_pos < len(with_ci):
            st, lab = WARN, "MIXED"
        elif len(with_ci) < len(keys):
            st, lab = INFO, "PARTLY JUDGED"
        else:
            st, lab = GOOD, "BEATS BASE RATE"
    if card.privileged:
        s += " It uses exact-queue features: an upper bound, not deployable."
    return Answer(
        st,
        lab,
        f"Does the learned fill-time model beat the base rate?{_suffix(run.label, many)}",
        s,
        "#learned_model",
        "Learned model scores and reliability",
    )


def _sim_name(r: RefereeReport) -> str:
    return r.simulator.name + (f" {r.simulator.version}" if r.simulator.version else "")


def _referee(sim: str, reps: list[RefereeReport]) -> Answer:
    """One simulator's referee runs: the bug-class tally for runs on a fixture class directory, the pass count and
    discordant orders for every other run; FAIL when any run failed."""
    classes: dict[str, bool] = {}
    others: list[RefereeReport] = []
    for r in reps:
        cls, _ = class_dir(r)
        if cls:
            classes[cls] = classes.get(cls, True) and r.pass_
        else:
            others.append(r)
    parts: list[str] = []
    bad = False
    if classes:
        n_run = sum(1 for c in BUG_CLASSES if c in classes)
        failed = [pretty_class(c) for c, ok in sorted(classes.items()) if not ok]
        parts.append(
            f"{n_run} of {len(BUG_CLASSES)} referee bug classes run, {len(failed)} failed"
            + (f": {', '.join(failed)}" if failed else "")
        )
        bad = bool(failed)
    if others:
        n_fail = sum(1 for r in others if not r.pass_)
        n_ord = sum(r.n_orders for r in others)
        disc = sum(r.discordant.sim_only + r.discordant.exact_only for r in others)
        parts.append(
            f"{len(others) - n_fail} of {len(others)} {'other ' if classes else ''}referee run(s) pass, {disc:,} of "
            f"{n_ord:,} orders disagree with the exact replay on whether they filled"
        )
        bad = bad or n_fail > 0
    return Answer(
        CRIT if bad else GOOD,
        "FAIL" if bad else "PASS",
        f"Does {sim} reproduce the exact FIFO fills?",
        ("No: " if bad else "Yes: ") + "; ".join(parts) + ".",
        "#referee",
        "Referee results",
    )


def _data_quality(ctx: Ctx) -> Answer | None:
    """The verdicts per input, the worst one's first reason, and the analysed inputs that carry no verdict (the
    answer speaks only for the inputs it covers: CLEAN WHERE CHECKED when some are uncovered)."""
    found: list[tuple[str, DataQuality]] = []
    covered: set[str] = set()
    for run in ctx.inputs.runs:
        for d in run.docs:
            m = d.model
            if isinstance(m, ScorecardSummary) and m.data_quality is not None:
                found.append((run.label, m.data_quality))
            elif isinstance(m, ReplaySummary):
                found.append((run.label, m.data_quality))
            elif isinstance(m, DataQuality):
                found.append((d.rec.path, m))
            else:
                continue
            covered.add(run.label)
    if not found:
        return None
    uncovered = [
        run.label
        for run in ctx.inputs.runs
        if run.label not in covered and any(isinstance(d.model, _ANALYSES) for d in run.docs)
    ]
    order = ("unusable", "warnings", "clean")
    by: dict[str, list[str]] = {v: [] for v in order}
    for label, dq in found:
        if label not in by[dq.verdict]:
            by[dq.verdict].append(label)
    worst = next(v for v in order if by[v])
    s = "Data-quality verdict " + "; ".join(f"{v} for {_names(by[v])}" for v in order if by[v]) + "."
    first = next((dq.reasons[0] for _, dq in found if dq.verdict == worst and dq.reasons), None)
    if first and worst != "clean":
        s += f" First reason: {first}."
    if uncovered:
        s += f" {_names(uncovered)} {'carries' if len(uncovered) == 1 else 'carry'} no data-quality verdict."
    st, lab = {"unusable": (CRIT, "UNUSABLE"), "warnings": (WARN, "WARNINGS"), "clean": (GOOD, "CLEAN")}[worst]
    if worst == "clean" and uncovered:
        st, lab = INFO, "CLEAN WHERE CHECKED"
    return Answer(
        st,
        lab,
        "Is the data clean enough to trust these numbers?",
        s,
        "#data_quality",
        "Data-quality counters and checks",
    )


def answers(ctx: Ctx, sections: set[str]) -> list[Answer]:
    """Every answer the inputs support, in the module docstring's order; links only to rendered sections."""
    out: list[Answer] = []
    zs = [(r, z) for r, z in ctx.runs_with(ZooSummary) if z.leaderboard]
    for run, z in zs:
        out += _zoo(zoo_view(ctx, run, z), len(zs) > 1)
    scs = ctx.runs_with(ScorecardSummary)
    out += [_score(run, s, len(scs) > 1) for run, s in scs]
    cals = ctx.runs_with(CalibrationResult)
    out += [_calibration(run, c, len(cals) > 1) for run, c in cals]
    cards = ctx.runs_with(HazardModelCard)
    out += [a for run, c in cards if (a := _learned(run, c, len(cards) > 1)) is not None]
    by_sim: dict[str, list[RefereeReport]] = {}
    for _, r in ctx.runs_with(RefereeReport):
        by_sim.setdefault(_sim_name(r), []).append(r)
    out += [_referee(sim, by_sim[sim]) for sim in sorted(by_sim)]
    dq = _data_quality(ctx)
    if dq is not None:
        out.append(dq)
    return [a for a in out if a.href.lstrip("#") in sections]


def _lead(ctx: Ctx, items: list[Answer], sections: set[str]) -> str:
    lead = (
        "One sentence per question these inputs can answer, with its base rate and 95% interval where the inputs "
        "carry them, and a link to the drill-down that backs it."
    )
    if any(a.href in ("#leaderboard", "#referee") for a in items):
        lead += (
            " The exact replay is the FIFO replay of zero-impact shadow orders, which never move the book or take "
            "liquidity"
        )
    else:
        lead += " What these numbers do not cover"
    lead += f' (see <a href="#{LIMITS_ID}">Scope and limits</a>); inputs and file hashes are collapsed in the header.'
    warns, n_docs = summary_warnings(ctx)
    if warns and "config" in sections:
        lead += (
            f' <a href="#{WARNINGS_ID}">{len(warns)} warning(s) across {n_docs} summar{"y" if n_docs == 1 else "ies"}'
            "</a> are listed in the configuration section."
        )
    if ctx.inputs.provenance.level() == "synthetic":
        lead += " <strong>The data is synthetic or fixture data: these answers exercise the pipeline only.</strong>"
    return lead


def answer(ctx: Ctx, sections: set[str]) -> Raw | None:
    """The answer section, or None when no input answers any question (e.g. a position table alone)."""
    items = answers(ctx, sections)
    if not items:
        return None
    lis = []
    for a in items:
        lis.append(
            f"<li>{badge(a.status, a.label)}<div><h3>{F.esc(a.question)}</h3><p>{F.esc(a.sentence)}</p>"
            f'<a class="go" href="{F.esc(a.href)}">{F.esc(a.link)} ↓</a></div></li>'
        )
    return section(
        "answer",
        "The answer",
        [f'<ol class="answers">{"".join(lis)}</ol>'],
        lead=_lead(ctx, items, sections),
        cls="answer",
    )


__all__ = ["ZOO_MARGIN", "Answer", "answer", "answers", "warnings_naming"]
