"""Facts about a scorecard the report needs beyond `qt.scorecard_summary`'s v0.1 fields.

Schema 1.1 summaries (SPEC 3.11 #9) carry them natively and are read first: `discordance_clustered` (the
cluster-robust primary discordance test, audit ruling A10), `fills_only`, `n_taker_fills` (ruling A5),
`n_twin_unmatched` / `twin_match_rate` (self-exclusion, ruling A1). For v0.1 outputs (those fields null) the report
falls back to what the scorer recorded elsewhere: the `qt.score_tests` row `discordance_clustered` or the
`clusters: ...` summary warning; the `fills_only: ...` warning / `not computable` headline; taker fills from
`qt.score_fills.liquidity = taker`; the `self_exclusion: matched ...` warning. Taker markouts are `qt.score_tests`
rows `markout_mean_taker` (stratum kind = arm, stratum = `<ref>:<horizon_ns>`); markouts in bps and net of fees
(review F10) are rows `markout_mean_bps` / `markout_mean_net_ccy` of the same shape."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from queuetruth.report import cols
from queuetruth.report.load import RunDir
from queuetruth.schemas.models import ScorecardSummary

NOT_COMPUTABLE = "not computable: fills-only source"


def fills_only(s: ScorecardSummary) -> bool:
    """True when the live source lacks never-filled orders (statistics needing them are not computable): the 1.1
    field `fills_only`, else the `fills_only:` warning / not-computable headline of v0.1 outputs."""
    if s.fills_only is not None:
        return bool(s.fills_only)
    return any(w.startswith("fills_only") or "coverage=fills_only" in w for w in s.warnings) or (
        NOT_COMPUTABLE in s.verdict.headline
    )


DEFAULT_WINDOW_NS = 60_000_000_000


def window_ns(s: ScorecardSummary) -> int:
    """Evaluation window H of the scorecard (spec echo `window_ns`; 60 s for v0.1 outputs)."""
    v = s.spec.get("window_ns")
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else DEFAULT_WINDOW_NS


def window_text(s: ScorecardSummary) -> str:
    """H as `60 s`, `1.5 s`, `500 ms` (the scorer's headline wording)."""
    ns = window_ns(s)
    for unit, scale in (("s", 10**9), ("ms", 10**6), ("us", 10**3)):
        if ns >= scale:
            return f"{ns / scale:g} {unit}"
    return f"{ns} ns"


def gates_configured(s: ScorecardSummary) -> bool:
    """True when the scorecard requested a gate (spec echo `gates`) or a gate failed (the scorer's own blocking gate
    `own_orders_in_l2_depth` fails without any user gate); `gates_passed` is vacuously true otherwise."""
    if s.verdict.failed_gates:
        return True
    g = s.spec.get("gates")
    if not isinstance(g, dict):
        return False
    return g.get("max_abs_fill_rate_bias") is not None or bool(g.get("require_equivalence"))


#: Failed gate of an L2 scorecard whose queue models ran on depth that still holds the user's own resting orders
#: (trader review E2, `queuetruth.score.scorecard.OWN_ORDERS_GATE`).
OWN_ORDERS_GATE = "own_orders_in_l2_depth"


def blocked_by_own_orders(s: ScorecardSummary) -> bool:
    """True when the scorer blocked the scorecard because the user's own orders stayed in the L2 depth."""
    return OWN_ORDERS_GATE in s.verdict.failed_gates or s.verdict.headline.startswith("BLOCKED:")


def not_scored(s: ScorecardSummary) -> bool:
    """True when no live order was scored (the 1.1 scorer raises "NOT SCORED" instead of writing such a summary;
    older outputs can still hold one)."""
    return s.n_matched == 0


def headline_text(s: ScorecardSummary) -> str:
    """The scorer's headline; v0.1 headlines that do not name the evaluation window get it appended."""
    h = s.verdict.headline
    if "within " in h or fills_only(s):
        return h
    return f"{h} (fills within {window_text(s)} of entry)"


_BLOCKS = re.compile(r"^clusters: \d+ \((?P<kind>[^)]*?) blocks\)")
_KIND_TEXT = {"day": "session-day blocks", "30min": "30-minute blocks"}


def ci_blocks_text(s: ScorecardSummary) -> str:
    """The bootstrap blocks of the scorecard's CIs (SPEC 5.7.5: session days, 30-minute blocks with fewer than 5
    dates): from the `ci_block=30min` / `clusters: ... (<kind> blocks)` warnings or the spec's `bootstrap.block`."""
    if any(w.startswith("ci_block=30min") for w in s.warnings):
        return _KIND_TEXT["30min"]
    for w in s.warnings:
        m = _BLOCKS.match(w)
        if m and m.group("kind") in _KIND_TEXT:
            return _KIND_TEXT[m.group("kind")]
    boot = s.spec.get("bootstrap")
    block = boot.get("block") if isinstance(boot, dict) else None
    if block in _KIND_TEXT:
        return _KIND_TEXT[block]
    return "session-day blocks, or 30-minute blocks with fewer than 5 days"


@dataclass
class TwinMatching:
    """Self-exclusion twin matching (ruling A1): unmatched live orders and the match rate."""

    n_unmatched: int | None
    rate: float | None
    source: str


_TWIN = re.compile(r"^self_exclusion: matched (?P<m>\d+)/(?P<n>\d+) live orders")


def twin_matching(s: ScorecardSummary) -> TwinMatching | None:
    """Twin matching of an MBO scorecard with self-exclusion (1.1 fields, else the `self_exclusion: matched` warning);
    None when it did not run."""
    if s.n_twin_unmatched is not None or s.twin_match_rate is not None:
        return TwinMatching(s.n_twin_unmatched, s.twin_match_rate, "summary")
    for w in s.warnings:
        m = _TWIN.match(w)
        if m:
            k, n = int(m.group("m")), int(m.group("n"))
            return TwinMatching(n - k, k / n if n else None, "summary warning")
    return None


@dataclass
class Clustered:
    """Cluster-robust discordance test: d = (c - b) / N, primary p, method and cluster structure."""

    d: float | None
    p: float | None
    ci_lo: float | None
    ci_hi: float | None
    n: int | None
    method: str | None
    n_clusters: int | None
    n_clusters_discordant: int | None
    design_effect: float | None
    n_effective: float | None
    source: str


def _f(x: str | None) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _i(x: str | None) -> int | None:
    v = _f(x)
    return None if v is None else int(v)


_NOTE = re.compile(
    r"^(?P<method>[a-z_]+); clusters=(?P<k>\d+); discordant clusters=(?P<kd>\d+); "
    r"design_effect=(?P<deff>[^;]+); n_effective=(?P<neff>[^;]+)"
)
_WARN = re.compile(
    r"^clusters: (?P<k>\d+) \((?P<kind>[^)]*)\), with discordance (?P<kd>\d+), design effect (?P<deff>\S+), "
    r"effective n (?P<neff>[^;]+); primary discordance test (?P<method>[a-z_]+)"
)


def clustered(run: RunDir, s: ScorecardSummary) -> Clustered | None:
    """The primary discordance test of a scorecard, or None when the scorer did not run it: the 1.1 summary field
    `discordance_clustered`, else the `qt.score_tests` row, else the `clusters:` warning."""
    dc = s.discordance_clustered
    if dc is not None:
        return Clustered(
            d=dc.estimate,
            p=dc.p_value,
            ci_lo=dc.ci_lo,
            ci_hi=dc.ci_hi,
            n=s.n_matched,
            method=dc.method,
            n_clusters=dc.n_clusters,
            n_clusters_discordant=dc.n_clusters_discordant,
            design_effect=dc.design_effect,
            n_effective=dc.n_effective,
            source="summary",
        )
    tt = run.table("qt.score_tests")
    if tt is not None:
        for r in cols.pylist(tt.read()):
            if r["test_id"] != "discordance_clustered":
                continue
            m = _NOTE.match(str(r.get("note") or ""))
            return Clustered(
                d=r["value"],
                p=r["p_value"],
                ci_lo=r["ci_lo"],
                ci_hi=r["ci_hi"],
                n=r["n"],
                method=m.group("method") if m else None,
                n_clusters=_i(m.group("k")) if m else None,
                n_clusters_discordant=_i(m.group("kd")) if m else None,
                design_effect=_f(m.group("deff")) if m else None,
                n_effective=_f(m.group("neff")) if m else None,
                source=tt.rec.path,
            )
    for w in s.warnings:
        m = _WARN.match(w)
        if m:
            b = s.bias
            return Clustered(
                d=b.fill_rate_bias,
                p=None,
                ci_lo=b.ci_lo,
                ci_hi=b.ci_hi,
                n=s.n_matched,
                method=m.group("method"),
                n_clusters=_i(m.group("k")),
                n_clusters_discordant=_i(m.group("kd")),
                design_effect=_f(m.group("deff")),
                n_effective=_f(m.group("neff")),
                source="summary warning",
            )
    return None


METHOD_LABEL = {
    "obuchowski": "Obuchowski cluster-adjusted McNemar (t, K − 1 df)",
    "block_bootstrap": "block bootstrap of (c − b)/N",
}


def taker_counts(run: RunDir, s: ScorecardSummary | None = None) -> dict[str, tuple[int, int | None]] | None:
    """Per arm: (taker fills, all fills). Taker counts from the 1.1 summary field `n_taker_fills` when present, else
    from `qt.score_fills.liquidity`; all fills from `qt.score_fills` (None when the table is absent)."""
    t = run.table("qt.score_fills")
    native = s.n_taker_fills if s is not None else None
    if t is None:
        if native is None:
            return None
        return {"live": (native.live, None), "sim": (native.sim, None)}
    tab = t.read()
    arm = cols.strs(tab, "arm")
    liq = cols.strs(tab, "liquidity")
    out: dict[str, tuple[int, int | None]] = {}
    for a in ("live", "sim"):
        m = arm == a
        n_taker = int((m & (liq == "taker")).sum()) if native is None else int(getattr(native, a))
        out[a] = (n_taker, int(m.sum()))
    return out


def taker_text(tk: tuple[int, int | None]) -> str:
    """`3 of 120 fills` (or `3` without the fill table)."""
    return f"{tk[0]:,}" if tk[1] is None else f"{tk[0]:,} of {tk[1]:,} fills"


def taker_markouts(run: RunDir) -> list[dict[str, Any]]:
    """Taker-fill markout means: {arm, ref, horizon_ns, n, mean_ticks, ci_lo, ci_hi}."""
    return markout_rows(run, "markout_mean_taker")


def markout_rows(run: RunDir, test_id: str) -> list[dict[str, Any]]:
    """Per-arm markout means of `qt.score_tests` rows `test_id` (`markout_mean_taker`, `markout_mean_bps`,
    `markout_mean_net_ccy`): {arm, ref, horizon_ns, n, mean_ticks (the row's value), ci_lo, ci_hi}."""
    t = run.table("qt.score_tests")
    if t is None:
        return []
    out = []
    for r in cols.pylist(t.read()):
        if r["test_id"] != test_id:
            continue
        ref, _, h = str(r["stratum"]).rpartition(":")
        try:
            hz = int(h)
        except ValueError:
            continue
        out.append(
            {
                "arm": str(r["stratum_kind"]),
                "ref": ref,
                "horizon_ns": hz,
                "n": int(r["n"]),
                "mean_ticks": r["value"],
                "ci_lo": r["ci_lo"],
                "ci_hi": r["ci_hi"],
            }
        )
    return sorted(out, key=lambda x: (x["ref"], x["arm"], x["horizon_ns"]))
