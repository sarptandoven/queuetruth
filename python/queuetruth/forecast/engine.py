"""`forecast score`: eligibility (the leakage guard), proper scores, calibration, skill against baselines, paired
comparisons, power and executable PnL, written as a `qt.forecast_scorecard`.

Eligibility of a forecast entry for question q, in this order (the first failing rule names the status):
`unresolved` (no resolution entry), `void` (the effective resolution is void), `after_resolution_entry` (the
forecast's ledger position is after q's first resolution entry), `after_cutoff` (`recorded_at_ns >= cutoff`,
`cutoff = min(info_cutoff_ns, resolved_at_ns)` over every resolution entry of q), `short_lead`
(`cutoff - recorded_at_ns < min_lead_ns`), `declared_clock` (a writer-supplied time, unless
`allow_declared_clock`), `unanchored` and `anchored_after_cutoff` (with `require_anchor`: no anchored commitment
covers the entry, or the earliest one that does was attested at or after the cutoff), `as_of_after_commit`,
`kind_mismatch`, `outcome_mismatch` (categorical labels differ from the question's outcomes, or the probabilities do
not sum to 1). A corrected resolution changes the outcome that is scored (the last resolution entry wins); because
the cutoff is the minimum over every resolution entry, a correction can move the cutoff earlier and never later.
`policy` then picks the scored forecasts per (forecaster, question): `last` or `first` eligible entry (the others
are `superseded`), or `mean` (all of them, averaged per question).

Every statistic is per question: one loss per (forecaster, question), clusters = questions (or `group`s with
`cluster: group`), Diebold-Mariano order = resolution time. When a cluster holds several questions the DM test runs
on cluster sums (`inference.dm_test`), the bootstrap resamples clusters and the calibration test draws one outcome
per cluster, so correlated questions are never counted as independent evidence. Baselines (references
`baseline:<name>`), all computed from what was known when each forecast was recorded:
- `market`: the mid of the last quote at or before `recorded_at_ns`, no older than `market_max_age_ns` (binary
  questions with quotes);
- `climatology`: the Laplace-smoothed outcome frequency of questions of the same group (all ungrouped questions of
  the same kind and outcome set otherwise) that resolved before `recorded_at_ns`, once `climatology_min_prior` of
  them exist; for continuous questions (grouped only) the empirical distribution of those outcomes;
- `uniform`: 0.5 (binary) or 1 / K (categorical).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.forecast import pnl as pnl_mod
from queuetruth.forecast.calibration import calibration
from queuetruth.forecast.inference import (
    cluster_index,
    design_effect,
    dm_test,
    mean_cluster_ci,
    paired_cluster_bootstrap,
)
from queuetruth.forecast.io import Quotes, read_quotes
from queuetruth.forecast.ledger import (
    check_commitment,
    commitment_digest,
    read_ledger_bytes,
    verify_bytes,
)
from queuetruth.forecast.power import power_at, required_n
from queuetruth.forecast.schemas import (
    REJECT_STATUSES,
    SUM_TOL,
    AnchorEcho,
    Comparison,
    DmTest,
    ForecastCommitment,
    ForecastEntry,
    ForecasterScore,
    ForecastPayload,
    ForecastScorecard,
    ForecastScoreSpec,
    LedgerEcho,
    MetricScore,
    PowerAnalysis,
    Rejection,
    ResolutionPayload,
    ScoreCounts,
    Selection,
)
from queuetruth.forecast.scores import (
    binary_scores,
    categorical_scores,
    crps_normal,
    crps_quantiles,
    crps_samples,
)
from queuetruth.forecast.tables import BASELINES, BETS, ELIGIBILITY, METRICS, SCORES, write_table
from queuetruth.schemas.models import CiValue, FileEntry
from queuetruth.schemas.runid import run_id, spec_echo
from queuetruth.stats import holm

MAX_REJECTIONS = 1000
#: Fewer independent units (questions, or clusters) than this in a comparison draws a warning.
MIN_UNITS = 30
#: A median age of the market baseline's quotes above this draws a warning.
WARN_QUOTE_AGE_NS = 6 * 3_600_000_000_000
#: Metrics reported per kind (the first is the primary metric of the power analysis).
KIND_METRICS: dict[str, tuple[str, ...]] = {
    "binary": ("brier", "log", "spherical"),
    "categorical": ("brier", "log", "spherical", "rps"),
    "continuous": ("crps",),
}


def _f(x: float | None) -> float | None:
    return float(x) if x is not None and math.isfinite(x) else None


def _ci(est: float, lo: float, hi: float) -> CiValue:
    return CiValue(value=_f(est), ci_lo=_f(lo), ci_hi=_f(hi))


@dataclass
class _Question:
    first_seq: int
    cutoff_ns: int
    first: ResolutionPayload  # the first resolution entry
    first_cutoff_ns: int
    res: ResolutionPayload  # effective (last) resolution

    @property
    def voided_after_outcome(self) -> bool:
        return self.res.void and not self.first.void

    @property
    def outcome_corrected(self) -> bool:
        return not self.res.void and not self.first.void and self.res.outcome != self.first.outcome

    @property
    def cutoff_tightened(self) -> bool:
        return self.cutoff_ns < self.first_cutoff_ns


@dataclass
class _Row:
    """One scored forecast."""

    entry: ForecastEntry
    payload: ForecastPayload
    q: _Question
    cluster: str
    p: float | None = None
    outcome_text: str = ""
    losses: dict[str, float] = field(default_factory=dict)
    clipped: bool = False
    base: dict[str, dict[str, float]] = field(default_factory=dict)
    market_p: float | None = None
    market_ts: int | None = None
    clim_p: float | None = None


def _questions(entries: list[ForecastEntry]) -> dict[str, _Question]:
    out: dict[str, _Question] = {}
    for e in entries:
        pl = e.payload
        if not isinstance(pl, ResolutionPayload):
            continue
        cut = pl.resolved_at_ns if pl.info_cutoff_ns is None else min(pl.info_cutoff_ns, pl.resolved_at_ns)
        if pl.question_id in out:
            q = out[pl.question_id]
            q.cutoff_ns = min(q.cutoff_ns, cut)  # a correction can tighten the cutoff, never loosen it
            q.res = pl
        else:
            out[pl.question_id] = _Question(first_seq=e.seq, cutoff_ns=cut, first=pl, first_cutoff_ns=cut, res=pl)
    return out


def eligibility(
    e: ForecastEntry, q: _Question | None, spec: ForecastScoreSpec, anchored_at: int | None = None
) -> tuple[str, str | None]:
    """`(status, detail)` of one forecast entry before the policy (`eligible` when it may be scored);
    `anchored_at` is the earliest attested time of an anchored commitment that covers the entry."""
    pl = e.payload
    assert isinstance(pl, ForecastPayload)
    if q is None:
        return "unresolved", None
    if q.res.void:
        return "void", "the question was voided"
    if e.seq > q.first_seq:
        return "after_resolution_entry", f"ledger seq {e.seq} is after the resolution entry seq {q.first_seq}"
    if e.recorded_at_ns >= q.cutoff_ns:
        return "after_cutoff", f"recorded {e.recorded_at_ns - q.cutoff_ns} ns at or after the cutoff"
    if q.cutoff_ns - e.recorded_at_ns < spec.min_lead_ns:
        return "short_lead", f"lead {q.cutoff_ns - e.recorded_at_ns} ns < min_lead_ns {spec.min_lead_ns}"
    if e.clock == "declared" and not spec.allow_declared_clock:
        return "declared_clock", "the commit time was supplied by the writer"
    if spec.require_anchor:
        if anchored_at is None:
            return "unanchored", "no anchored commitment covers this entry"
        if anchored_at >= q.cutoff_ns:
            return "anchored_after_cutoff", f"first anchored {anchored_at - q.cutoff_ns} ns at or after the cutoff"
    if pl.as_of_ns is not None and pl.as_of_ns > e.recorded_at_ns:
        return "as_of_after_commit", "as_of is later than the commit"
    if pl.forecast.kind != q.res.kind:
        return "kind_mismatch", f"a {pl.forecast.kind} forecast of a {q.res.kind} question"
    if pl.forecast.kind == "categorical":
        labels = set(pl.forecast.probs)  # type: ignore[union-attr]
        want = set(q.res.outcomes or [])
        if labels != want:
            return "outcome_mismatch", f"labels {sorted(labels)} differ from outcomes {sorted(want)}"
        total = sum(pl.forecast.probs.values())  # type: ignore[union-attr]
        if abs(total - 1.0) > SUM_TOL:
            return "outcome_mismatch", f"probabilities sum to {total!r}"
    return "eligible", None


def _cluster_label(q: _Question, qid: str, spec: ForecastScoreSpec) -> str:
    if spec.cluster == "group" and q.res.group is not None:
        return "g:" + q.res.group
    return "q:" + qid


# ---- anchors ------------------------------------------------------------------------------------------------------


def _anchors(spec: ForecastScoreSpec, entries: list[ForecastEntry]) -> tuple[list[AnchorEcho], list[int | None]]:
    """Checks every anchored commitment against the ledger; returns the echoes and, per entry seq, the earliest
    attested time of a commitment that covers it (None when none does)."""
    echoes: list[AnchorEcho] = []
    for a in spec.anchors:
        path = Path(a.commitment.path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{path}: {e}") from None
        try:
            c = ForecastCommitment.model_validate_json(text)
        except ValueError as e:
            raise QtError("QT-INPUT-002", f"{path}: not a qt.forecast_commitment: {e}") from None
        problems = check_commitment(entries, c)
        if problems:
            raise QtError(
                "QT-INPUT-002",
                f"{path}: the ledger does not contain this anchored commitment: {problems[0]}",
                "the ledger was rewritten after the commitment was anchored, or the commitment belongs to another "
                "ledger; it is never scored against a failed anchor",
                {"problems": problems},
            )
        echoes.append(
            AnchorEcho(
                path=a.commitment.path,
                commitment_sha256=commitment_digest(c),
                head_seq=c.head_seq,
                head_sha256=c.head_sha256,
                anchored_at_ns=a.anchored_at_ns,
            )
        )
    covered: list[int | None] = [None] * len(entries)
    best: int | None = None
    by_head = sorted(echoes, key=lambda x: -x.head_seq)
    k = 0
    for seq in range(len(entries) - 1, -1, -1):  # suffix minimum over anchors with head_seq >= seq
        while k < len(by_head) and by_head[k].head_seq >= seq:
            t = by_head[k].anchored_at_ns
            best = t if best is None else min(best, t)
            k += 1
        covered[seq] = best
    return echoes, covered


# ---- baselines ----------------------------------------------------------------------------------------------------


class _Climatology:
    """Outcomes resolved before a time, per pool (group, or kind and outcome set)."""

    def __init__(self, questions: dict[str, _Question]) -> None:
        pools: dict[tuple[Any, ...], list[tuple[int, Any]]] = defaultdict(list)
        for q in questions.values():
            r = q.res
            if r.void or r.outcome is None:
                continue
            pools[self.pool(r)].append((r.resolved_at_ns, r.outcome))
        self.times: dict[tuple[Any, ...], list[int]] = {}
        self.values: dict[tuple[Any, ...], list[Any]] = {}
        for key, items in pools.items():
            items.sort(key=lambda x: x[0])
            self.times[key] = [t for t, _ in items]
            self.values[key] = [v for _, v in items]

    @staticmethod
    def pool(r: ResolutionPayload) -> tuple[Any, ...]:
        if r.group is not None:
            return ("group", r.kind, r.group)
        if r.kind == "continuous":
            return ("none",)
        return ("kind", r.kind, tuple(sorted(r.outcomes or [])))

    def prior(self, r: ResolutionPayload, before_ns: int) -> list[Any]:
        key = self.pool(r)
        if key == ("none",) or key not in self.times:
            return []
        k = bisect.bisect_left(self.times[key], before_ns)
        return self.values[key][:k]


def _market(
    quotes: dict[str, Quotes] | None, qid: str, t: int, max_age_ns: int | None
) -> tuple[float | None, int | None]:
    """`(mid, quote ts)` of the last two-sided quote at or before `t` no older than `max_age_ns`."""
    if quotes is None or qid not in quotes:
        return None, None
    qq = quotes[qid]
    i = qq.asof(t)
    if i is None:
        return None, None
    ts = int(qq.ts[i])
    if max_age_ns is not None and t - ts > max_age_ns:
        return None, ts
    return qq.mid(i), ts


# ---- per-forecast losses ------------------------------------------------------------------------------------------


def _score_row(
    row: _Row, spec: ForecastScoreSpec, clim: _Climatology, quotes: dict[str, Quotes] | None, market_age: int | None
) -> None:
    pl, r, t = row.payload, row.q.res, row.entry.recorded_at_ns
    fc = pl.forecast
    eps = spec.log_eps
    if fc.kind == "binary":
        y = float(r.outcome)  # type: ignore[arg-type]
        row.p = fc.p
        row.outcome_text = str(int(y))
        losses, clipped = binary_scores(np.array([fc.p]), np.array([y]), eps)
        row.losses = {k: float(v[0]) for k, v in losses.items()}
        row.clipped = bool(clipped[0])
        refs: dict[str, float] = {}
        if "market" in spec.baselines:
            m, mts = _market(quotes, pl.question_id, t, market_age)
            row.market_p, row.market_ts = m, (mts if m is not None else None)
            if m is not None:
                refs["market"] = m
        if "climatology" in spec.baselines:
            prior = clim.prior(r, t)
            if len(prior) >= spec.climatology_min_prior:
                row.clim_p = (sum(float(v) for v in prior) + 1.0) / (len(prior) + 2.0)
                refs["climatology"] = row.clim_p
        if "uniform" in spec.baselines:
            refs["uniform"] = 0.5
        for name, bp in refs.items():
            bl, _ = binary_scores(np.array([bp]), np.array([y]), eps)
            row.base[name] = {k: float(v[0]) for k, v in bl.items()}
    elif fc.kind == "categorical":
        labels = list(r.outcomes or [])
        yi = labels.index(str(r.outcome))
        row.outcome_text = str(r.outcome)
        probs = np.array([[fc.probs[k] for k in labels]])
        losses, clipped = categorical_scores(probs, np.array([yi]), r.ordered, eps)
        row.losses = {k: float(v[0]) for k, v in losses.items()}
        row.clipped = bool(clipped[0])
        refs_c: dict[str, np.ndarray] = {}
        if "climatology" in spec.baselines:
            prior = clim.prior(r, t)
            if len(prior) >= spec.climatology_min_prior:
                counts = np.array([sum(1 for v in prior if v == k) for k in labels], dtype=float)
                refs_c["climatology"] = (counts + 1.0) / (len(prior) + len(labels))
        if "uniform" in spec.baselines:
            refs_c["uniform"] = np.full(len(labels), 1.0 / len(labels))
        for name, bp in refs_c.items():
            bl, _ = categorical_scores(bp[None, :], np.array([yi]), r.ordered, eps)
            row.base[name] = {k: float(v[0]) for k, v in bl.items()}
    else:
        y = float(r.outcome)  # type: ignore[arg-type]
        row.outcome_text = repr(y)
        dist = fc.dist
        if dist.family == "normal":
            crps = float(crps_normal(dist.mean, dist.sd, y))
        elif dist.family == "quantiles":
            crps = crps_quantiles(np.array(dist.levels), np.array(dist.values), y)
        else:
            crps = crps_samples(np.array(dist.samples), y)
        row.losses = {"crps": crps}
        if "climatology" in spec.baselines:
            prior = clim.prior(r, t)
            if len(prior) >= spec.climatology_min_prior:
                row.base["climatology"] = {"crps": crps_samples(np.array(prior, dtype=float), y)}


# ---- aggregation --------------------------------------------------------------------------------------------------


@dataclass
class _QRow:
    """One (forecaster, question) after the policy: mean losses over its scored forecasts."""

    question_id: str
    kind: str
    cluster: str
    resolved_at_ns: int
    lead_ns: int
    losses: dict[str, float]
    base: dict[str, dict[str, float]]


def _aggregate(rows: list[_Row]) -> _QRow:
    first = rows[0]
    metrics = sorted(set().union(*(r.losses for r in rows)))
    losses = {m: float(np.mean([r.losses[m] for r in rows])) for m in metrics}
    base: dict[str, dict[str, float]] = {}
    for b in BASELINES:
        if all(b in r.base for r in rows):
            base[b] = {m: float(np.mean([r.base[b][m] for r in rows])) for m in rows[0].base[b]}
    leads = [r.q.cutoff_ns - r.entry.recorded_at_ns for r in rows]
    return _QRow(
        question_id=first.payload.question_id,
        kind=first.payload.forecast.kind,
        cluster=first.cluster,
        resolved_at_ns=first.q.res.resolved_at_ns,
        lead_ns=int(statistics.median(leads)),
        losses=losses,
        base=base,
    )


def _compare(
    subject: str,
    reference: str,
    kind: str,
    metric: str,
    pairs: list[tuple[_QRow, float, float]],
    spec: ForecastScoreSpec,
    lead_diff_ns: int | None = None,
) -> Comparison | None:
    """Paired comparison on `(question row, subject loss, reference loss)` triples."""
    if not pairs:
        return None
    pairs = sorted(pairs, key=lambda x: (x[0].resolved_at_ns, x[0].question_id))
    ls = np.array([x[1] for x in pairs])
    lr = np.array([x[2] for x in pairs])
    inv, _ = cluster_index([x[0].cluster for x in pairs])
    tid = f"forecast/{kind}/{metric}/{subject}/{reference}"
    bs = paired_cluster_bootstrap(ls, lr, inv, spec.bootstrap_b, spec.seed, tid, spec.alpha)
    dm = dm_test(lr - ls, spec.dm_lag, inv)
    return Comparison(
        subject=subject,
        reference=reference,
        kind=kind,  # type: ignore[arg-type]
        metric=metric,  # type: ignore[arg-type]
        n=bs.n,
        n_clusters=bs.n_clusters,
        mean_diff=_ci(bs.mean_diff, *bs.mean_diff_ci),
        skill=_ci(bs.skill, *bs.skill_ci),
        p_bootstrap=_f(bs.p_value),
        dm=DmTest(statistic=_f(dm.statistic), p_value=_f(dm.p_value), lag=dm.lag, units=dm.units),
        lead_diff_ns=lead_diff_ns,
    )


def _power(
    reference: str, kind: str, metric: str, pairs: list[tuple[_QRow, float, float]], spec: ForecastScoreSpec
) -> PowerAnalysis:
    d = np.array([x[2] - x[1] for x in pairs], dtype=float)
    n = int(d.size)
    inv, _ = cluster_index([x[0].cluster for x in pairs]) if pairs else (np.zeros(0, dtype=np.int64), 0)
    delta = float(d.mean()) if n else float("nan")
    sd = float(d.std(ddof=1)) if n > 1 else float("nan")
    deff = design_effect(d, inv) if n > 1 else 1.0
    return PowerAnalysis(
        reference=reference,
        kind=kind,  # type: ignore[arg-type]
        metric=metric,  # type: ignore[arg-type]
        n=n,
        observed_edge=_f(delta),
        sd_diff=_f(sd),
        design_effect=deff,
        achieved_power=_f(power_at(n, delta, sd, spec.alpha, deff)) if n > 1 else None,
        n_required_observed=required_n(delta, sd, spec.alpha, spec.power, deff) if n > 1 and delta > 0 else None,
        min_edge=spec.min_edge,
        n_required_min_edge=(
            required_n(spec.min_edge, sd, spec.alpha, spec.power, deff) if spec.min_edge and n > 1 else None
        ),
    )


def _pairwise_names(qrows: dict[str, dict[str, _QRow]], spec: ForecastScoreSpec) -> list[str]:
    if spec.pairwise == "none":
        return []
    names = sorted(qrows)
    if spec.pairwise == "all":
        return names
    top = sorted(names, key=lambda w: (-len(qrows[w]), w))[: int(spec.pairwise)]
    return sorted(top)


# ---- the scorecard ------------------------------------------------------------------------------------------------


def _file_sha(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise QtError("QT-INPUT-001", f"{p}: no such file")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _file_entry(path: Path, rows: int | None) -> FileEntry:
    data = path.read_bytes()
    return FileEntry(name=path.name, rows=rows, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


def parse_spec(spec: ForecastScoreSpec | dict[str, Any]) -> ForecastScoreSpec:
    """Validates a spec (`QT-CONFIG-001` naming the field)."""
    if isinstance(spec, ForecastScoreSpec):
        out = spec
    else:
        try:
            out = ForecastScoreSpec.model_validate(spec)
        except ValidationError as e:
            err = e.errors()[0]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            raise QtError("QT-CONFIG-001", f"forecast score spec: {loc}: {err.get('msg')}") from None
    if out.require_anchor and not out.anchors:
        raise QtError("QT-CONFIG-001", "forecast score spec: require_anchor: needs at least one anchor")
    return out


def _hours(ns: float) -> str:
    return f"{ns / 3_600_000_000_000:.1f} h"


def score(spec_in: ForecastScoreSpec | dict[str, Any]) -> ForecastScorecard:
    """Scores the ledger named by the spec; writes the files when `spec.out` is set."""
    spec = parse_spec(spec_in)
    full = spec.model_dump(mode="json")
    data = read_ledger_bytes(spec.ledger.path)  # read once: the run id and the scores describe the same bytes
    shas = {spec.ledger.path: hashlib.sha256(data).hexdigest()}
    rid = run_id(full, __version__, lambda ref: shas.get(ref["path"]) or _file_sha(ref["path"]))
    v = verify_bytes(data)
    if not v.ok:
        first = v.problems[0]
        raise QtError(
            "QT-INPUT-002",
            f"{spec.ledger.path}: ledger integrity check failed at line {first.line}: {first.problem}",
            "a ledger whose chain does not verify is never scored; run `queuetruth forecast verify`",
            {"problems": [p.model_dump() for p in v.problems[:20]]},
        )
    entries = v.entries
    if not entries:
        raise QtError("QT-INPUT-002", f"{spec.ledger.path}: the ledger is empty")
    anchor_echo, anchored = _anchors(spec, entries)
    quotes = read_quotes(spec.market.path) if spec.market is not None else None
    market_age = spec.market_max_age_ns
    if market_age is None and spec.pnl is not None:
        market_age = spec.pnl.max_quote_age_ns
    questions = _questions(entries)
    clim = _Climatology(questions)
    warnings: list[str] = []

    forecasts = [e for e in entries if isinstance(e.payload, ForecastPayload)]
    status: dict[int, tuple[str, str | None]] = {}
    eligible: dict[tuple[str, str], list[ForecastEntry]] = defaultdict(list)
    for e in forecasts:
        pl = e.payload
        assert isinstance(pl, ForecastPayload)
        st = eligibility(e, questions.get(pl.question_id), spec, anchored[e.seq])
        status[e.seq] = st
        if st[0] == "eligible":
            eligible[(pl.forecaster, pl.question_id)].append(e)
    by_status: dict[str, int] = defaultdict(int)
    for s in status.values():
        by_status[s[0]] += 1
    if spec.unresolved == "fail" and by_status.get("unresolved"):
        open_q = sorted({e.payload.question_id for e in forecasts if status[e.seq][0] == "unresolved"})  # type: ignore[union-attr]
        raise QtError(
            "QT-INPUT-002",
            f"{by_status['unresolved']} forecast(s) on {len(open_q)} unresolved question(s), e.g. {open_q[0]!r}",
            "resolve every question first, or score with --unresolved warn",
            {"unresolved_questions": open_q[:100]},
        )
    chosen: dict[tuple[str, str], list[ForecastEntry]] = {}
    for key, es in eligible.items():
        es.sort(key=lambda x: x.seq)
        keep = es if spec.policy == "mean" else [es[-1] if spec.policy == "last" else es[0]]
        chosen[key] = keep
        kept = {x.seq for x in keep}
        for x in es:
            status[x.seq] = ("scored", None) if x.seq in kept else ("superseded", f"policy {spec.policy}")
    by_status = defaultdict(int)
    for s in status.values():
        by_status[s[0]] += 1
    n_scored = by_status.get("scored", 0)
    if n_scored == 0:
        detail = ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())) or "no forecast entries"
        raise QtError(
            "QT-INPUT-002",
            f"NOT SCORED: 0 of {len(forecasts)} forecasts scored ({detail})",
            "resolve the questions (`queuetruth forecast resolve`) and commit forecasts before their cutoff",
            {"by_status": dict(by_status)},
        )

    # what the resolutions did to each forecaster's record (computed for every forecaster, scored or not)
    selection: dict[str, Selection] = {}
    for who in sorted({e.payload.forecaster for e in forecasts}):  # type: ignore[union-attr]
        mine = [e for e in forecasts if e.payload.forecaster == who]  # type: ignore[union-attr]
        qs = [questions[qid] for qid in sorted({e.payload.question_id for e in mine} & questions.keys())]  # type: ignore[union-attr]
        selection[who] = Selection(
            unresolved=sum(1 for e in mine if status[e.seq][0] == "unresolved"),
            void=sum(1 for e in mine if status[e.seq][0] == "void"),
            voided_after_outcome=sum(1 for q in qs if q.voided_after_outcome),
            outcome_corrected=sum(1 for q in qs if q.outcome_corrected),
            cutoff_tightened=sum(1 for q in qs if q.cutoff_tightened),
        )

    rows_by_f: dict[str, list[_Row]] = defaultdict(list)
    for (who, qid), es in sorted(chosen.items()):
        q = questions[qid]
        for e in es:
            pl = e.payload
            assert isinstance(pl, ForecastPayload)
            row = _Row(entry=e, payload=pl, q=q, cluster=_cluster_label(q, qid, spec))
            _score_row(row, spec, clim, quotes, market_age)
            rows_by_f[who].append(row)

    qrows: dict[str, dict[str, _QRow]] = {}
    for who, rows in rows_by_f.items():
        per_q: dict[str, list[_Row]] = defaultdict(list)
        for r in rows:
            per_q[r.payload.question_id].append(r)
        qrows[who] = {qid: _aggregate(rs) for qid, rs in sorted(per_q.items())}

    forecasters_out: list[ForecasterScore] = []
    all_comparisons: list[Comparison] = []
    bet_rows: list[dict[str, Any]] = []
    for who in sorted(rows_by_f):
        rows = rows_by_f[who]
        qr = qrows[who]
        kinds: dict[str, int] = defaultdict(int)
        for x in qr.values():
            kinds[x.kind] += 1
        scores_out: list[MetricScore] = []
        skill_out: list[Comparison] = []
        power_out: list[PowerAnalysis] = []
        for kind in ("binary", "categorical", "continuous"):
            ks = [x for x in qr.values() if x.kind == kind]
            if not ks:
                continue
            for metric in KIND_METRICS[kind]:
                vals = [(x, x.losses[metric]) for x in ks if metric in x.losses]
                if not vals:
                    continue
                inv, _ = cluster_index([x.cluster for x, _ in vals])
                est, lo, hi = mean_cluster_ci(
                    np.array([v for _, v in vals]),
                    inv,
                    spec.bootstrap_b,
                    spec.seed,
                    f"forecast/{kind}/{metric}/{who}/mean",
                    spec.alpha,
                )
                n_clip = sum(1 for r in rows if r.payload.forecast.kind == kind and r.clipped) if metric == "log" else 0
                scores_out.append(
                    MetricScore(kind=kind, metric=metric, n=len(vals), mean=_ci(est, lo, hi), n_clipped=n_clip)  # type: ignore[arg-type]
                )
                for b in spec.baselines:
                    pairs = [
                        (x, x.losses[metric], x.base[b][metric]) for x in ks if b in x.base and metric in x.base[b]
                    ]
                    c = _compare(who, f"baseline:{b}", kind, metric, pairs, spec)
                    if c is not None:
                        skill_out.append(c)
                    if metric == KIND_METRICS[kind][0] and pairs:
                        power_out.append(_power(f"baseline:{b}", kind, metric, pairs, spec))
        calib = []
        for kind in ("binary", "categorical"):
            krows = [r for r in rows if r.payload.forecast.kind == kind]
            if not krows:
                continue
            if kind == "binary":
                p = np.array([r.p for r in krows], dtype=float)
                y = np.array([float(r.q.res.outcome) for r in krows])  # type: ignore[arg-type]
                target = "binary"
            else:
                conf, hit = [], []
                for r in krows:
                    probs = r.payload.forecast.probs  # type: ignore[union-attr]
                    labels = list(r.q.res.outcomes or [])
                    top = max(labels, key=lambda k, pr=probs: (pr[k], -labels.index(k)))
                    conf.append(probs[top])
                    hit.append(1.0 if top == str(r.q.res.outcome) else 0.0)
                p, y, target = np.array(conf), np.array(hit), "top_label"
            inv, _ = cluster_index([r.cluster for r in krows])
            calib.append(
                calibration(
                    p,
                    y,
                    inv,
                    target=target,
                    n_bins=spec.n_bins,
                    b=spec.bootstrap_b,
                    seed=spec.seed,
                    test_id=f"forecast/{kind}/calibration/{who}",
                    alpha=spec.alpha,
                )
            )
        pnl_res = None
        if spec.pnl is not None and quotes is not None:
            cands = []
            last_rows: dict[str, _Row] = {}
            for r in rows:
                if r.payload.forecast.kind != "binary":
                    continue
                prev = last_rows.get(r.payload.question_id)
                if prev is None or r.entry.seq > prev.entry.seq:
                    last_rows[r.payload.question_id] = r
            inv_map = {lab: i for i, lab in enumerate(sorted({r.cluster for r in last_rows.values()}))}
            for qid, r in sorted(last_rows.items()):
                cands.append(
                    pnl_mod.Candidate(
                        forecaster=who,
                        question_id=qid,
                        p=float(r.p),  # type: ignore[arg-type]
                        decided_at_ns=r.entry.recorded_at_ns,
                        cutoff_ns=r.q.cutoff_ns,
                        resolved_at_ns=r.q.res.resolved_at_ns,
                        outcome=float(r.q.res.outcome),  # type: ignore[arg-type]
                        cluster=inv_map[r.cluster],
                    )
                )
            if cands:
                run = pnl_mod.run_pnl(
                    cands,
                    quotes,
                    spec.pnl,
                    b=spec.bootstrap_b,
                    seed=spec.seed,
                    test_id=f"forecast/pnl/{who}",
                    alpha=spec.alpha,
                    log_eps=spec.log_eps,
                )
                pnl_res = run.result
                bet_rows.extend(run.rows)
                if pnl_res.path_cash_limited:
                    warnings.append(
                        f"{who}: the PnL bankroll path is cash-limited (cash skipped or trimmed "
                        f"{pnl_res.n_skipped.get('insufficient_cash', 0) + pnl_res.cash_limited} of "
                        f"{pnl_res.n_candidates} candidates), so its return and drawdown describe an arbitrary "
                        "subset of bets; read isolated_growth"
                    )
        leads = [r.q.cutoff_ns - r.entry.recorded_at_ns for r in rows]
        mine = [e for e in forecasts if e.payload.forecaster == who]  # type: ignore[union-attr]
        n_rej = sum(1 for e in mine if status[e.seq][0] in REJECT_STATUSES)
        units = len({x.cluster for x in qr.values()})
        if units < MIN_UNITS:
            warnings.append(
                f"{who}: {units} independent question(s) or cluster(s) scored; below {MIN_UNITS} the tests and CIs "
                "are unreliable"
            )
        forecasters_out.append(
            ForecasterScore(
                forecaster=who,
                n_forecasts=len(mine),
                n_scored=len(rows),
                n_rejected=n_rej,
                selection=selection[who],
                n_anchored=sum(1 for r in rows if anchored[r.entry.seq] is not None),
                kinds=dict(kinds),
                scores=scores_out,
                calibration=calib,
                skill=skill_out,
                power=power_out,
                pnl=pnl_res,
                median_lead_ns=int(statistics.median(leads)) if leads else None,
            )
        )
        all_comparisons.extend(skill_out)

    pairwise: list[Comparison] = []
    names = _pairwise_names(qrows, spec)
    for i, a in enumerate(names):
        for bname in names[i + 1 :]:
            common = sorted(set(qrows[a]) & set(qrows[bname]))
            for kind in ("binary", "categorical", "continuous"):
                for metric in KIND_METRICS[kind]:
                    pairs = []
                    for qid in common:
                        xa, xb = qrows[a][qid], qrows[bname][qid]
                        if xa.kind == kind and metric in xa.losses and metric in xb.losses:
                            pairs.append((xa, xa.losses[metric], xb.losses[metric]))
                    if not pairs:
                        continue
                    lead = int(statistics.median(x.lead_ns - qrows[bname][x.question_id].lead_ns for x, _, _ in pairs))
                    c = _compare(a, bname, kind, metric, pairs, spec, lead)
                    if c is not None:
                        pairwise.append(c)
    all_comparisons.extend(pairwise)
    families: dict[tuple[str, str], list[Comparison]] = defaultdict(list)
    for c in all_comparisons:
        families[(c.kind, c.metric)].append(c)
    for fam in families.values():
        ps = np.array([c.dm.p_value if c.dm.p_value is not None else np.nan for c in fam])
        adj = holm(ps)
        for c, a in zip(fam, adj, strict=True):
            c.p_holm = _f(float(a))

    rejections: list[Rejection] = []
    for e in forecasts:
        st, detail = status[e.seq]
        if st not in REJECT_STATUSES:
            continue
        pl = e.payload
        assert isinstance(pl, ForecastPayload)
        rejections.append(
            Rejection(seq=e.seq, question_id=pl.question_id, forecaster=pl.forecaster, status=st, detail=detail or "")  # type: ignore[arg-type]
        )
    n_rej_total = len(rejections)
    if n_rej_total:
        warnings.append(
            f"{n_rej_total} forecast(s) rejected by the leakage and validity guard (see rejections and "
            "forecast_eligibility.parquet)"
        )
    for who, sel in selection.items():
        parts = []
        if sel.unresolved:
            parts.append(f"{sel.unresolved} forecast(s) on unresolved questions not scored")
        if sel.void:
            parts.append(f"{sel.void} forecast(s) on voided questions not scored")
        if sel.voided_after_outcome:
            parts.append(f"{sel.voided_after_outcome} question(s) voided by a correction after an outcome")
        if sel.outcome_corrected:
            parts.append(f"{sel.outcome_corrected} question(s) whose outcome a correction changed")
        if sel.cutoff_tightened:
            parts.append(f"{sel.cutoff_tightened} question(s) whose cutoff a correction moved earlier")
        if parts:
            warnings.append(f"{who}: " + "; ".join(parts) + " (resolutions are trusted input: check who wrote them)")
    if spec.pnl is not None and quotes is None:
        warnings.append("no --market quotes: market baseline and executable PnL not computed")
    ages = [r.entry.recorded_at_ns - r.market_ts for rs in rows_by_f.values() for r in rs if r.market_ts is not None]
    if ages and statistics.median(ages) > WARN_QUOTE_AGE_NS:
        warnings.append(
            f"the market baseline's quotes are old (median age {_hours(statistics.median(ages))} at the commit); "
            "a sparse quote file flatters forecasters who commit after news: set --market-max-age"
        )
    no_dm = sum(1 for c in all_comparisons if c.dm.p_value is None and c.n >= 2)
    if no_dm:
        warnings.append(
            f"{no_dm} comparison(s) have no DM p-value (constant non-zero loss differences, or too few clusters)"
        )
    head = entries[-1]
    n_void = sum(1 for q in questions.values() if q.res.void)
    q_ids = {e.payload.question_id for e in entries}
    files: list[FileEntry] = []
    if spec.out is not None:
        out = Path(spec.out)
        out.mkdir(parents=True, exist_ok=True)
        elig_rows = []
        for e in forecasts:
            pl = e.payload
            assert isinstance(pl, ForecastPayload)
            q = questions.get(pl.question_id)
            st, detail = status[e.seq]
            elig_rows.append(
                {
                    "seq": e.seq,
                    "recorded_at": e.recorded_at_ns,
                    "clock": e.clock,
                    "anchored_at": anchored[e.seq],
                    "forecaster": pl.forecaster,
                    "question_id": pl.question_id,
                    "kind": pl.forecast.kind,
                    "status": st,
                    "detail": detail,
                    "cutoff": q.cutoff_ns if q is not None else None,
                    "lead_ns": q.cutoff_ns - e.recorded_at_ns if q is not None else None,
                }
            )
        p_elig = out / "forecast_eligibility.parquet"
        n_e = write_table(elig_rows, ELIGIBILITY, p_elig)
        score_rows = []
        for who in sorted(rows_by_f):
            for r in sorted(rows_by_f[who], key=lambda x: x.entry.seq):
                d: dict[str, Any] = {
                    "seq": r.entry.seq,
                    "forecaster": who,
                    "question_id": r.payload.question_id,
                    "kind": r.payload.forecast.kind,
                    "cluster": r.cluster,
                    "recorded_at": r.entry.recorded_at_ns,
                    "resolved_at": r.q.res.resolved_at_ns,
                    "lead_ns": r.q.cutoff_ns - r.entry.recorded_at_ns,
                    "p": r.p,
                    "outcome": r.outcome_text,
                    "log_clipped": r.clipped,
                    "market_p": r.market_p,
                    "market_quote_ts": r.market_ts,
                    "market_quote_age_ns": r.entry.recorded_at_ns - r.market_ts if r.market_ts is not None else None,
                    "climatology_p": r.clim_p,
                }
                for m in METRICS:
                    d[m] = r.losses.get(m)
                    for b in BASELINES:
                        d[f"{b}_{m}"] = r.base.get(b, {}).get(m)
                score_rows.append(d)
        p_scores = out / "forecast_scores.parquet"
        n_s = write_table(score_rows, SCORES, p_scores)
        files = [_file_entry(p_elig, n_e), _file_entry(p_scores, n_s)]
        if bet_rows:
            p_bets = out / "forecast_bets.parquet"
            conv = [
                {
                    **r,
                    "decided_at": r["decided_at_ns"],
                    "exec_at": r["exec_at_ns"],
                    "quote_ts": r["quote_ts_ns"],
                    "decision_quote_ts": r["decision_quote_ts_ns"],
                }
                for r in bet_rows
            ]
            files.append(_file_entry(p_bets, write_table(conv, BETS, p_bets)))
    card = ForecastScorecard(
        run_id=rid,
        engine_version=__version__,
        spec=spec_echo(full),
        ledger=LedgerEcho(
            path=spec.ledger.path,
            ledger_id=entries[0].entry_sha256,
            head_seq=head.seq,
            head_sha256=head.entry_sha256,
            n_entries=len(entries),
            verified=True,
            anchors=anchor_echo,
        ),
        counts=ScoreCounts(
            forecasts=len(forecasts),
            by_status=dict(sorted(by_status.items())),
            questions=len(q_ids),
            questions_resolved=len(questions),
            questions_void=n_void,
            forecasters=len({e.payload.forecaster for e in forecasts}),  # type: ignore[union-attr]
        ),
        forecasters=forecasters_out,
        comparisons=pairwise,
        rejections=rejections[:MAX_REJECTIONS],
        rejections_truncated=n_rej_total > MAX_REJECTIONS,
        files=files,
        warnings=warnings,
    )
    if spec.out is not None:
        p_card = Path(spec.out) / "forecast_scorecard.json"
        p_card.write_text(scorecard_json(card), encoding="utf-8")
    return card


def scorecard_json(card: ForecastScorecard) -> str:
    """The scorecard as indented JSON (undefined statistics are null, never NaN)."""
    return json.dumps(card.model_dump(mode="json"), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


__all__ = ["KIND_METRICS", "MAX_REJECTIONS", "MIN_UNITS", "eligibility", "parse_spec", "score", "scorecard_json"]
