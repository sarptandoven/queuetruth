"""Leaderboard presentation (trader review C1, top-10 item 4): rank the zoo models by the absolute fill-rate bias at a
horizon that matters and state each model's bias in one plain-English sentence.

- `sort_horizon(horizons, target)`: `target` (default 60 s) when it is one of the horizons, else the largest horizon
  below it, else the smallest horizon (None without horizons).
- `rank_leaderboard(entries, horizon_ns)`: entries ordered by `|fill_rate_bias(h)|` at the sort horizon (models
  without a value last), then by Brier at that horizon, then by model id. Brier alone ties when most models never
  fill at short horizons (Brier 0.0000 for 15 of 17 models at 1 s in the review), so it is only a tie-break.
- `bias_sentence(entry, horizon_ns, aborts=None)`: "At 60 s, hbt_power3[n=2] fills 10.3% of orders vs 11.0% in the
  exact replay, 0.7 pp less often (95% CI -1.3 to -0.3 pp)."; a CI containing zero reads "..., within noise (95% CI
  ...)". The two rates are the entry's `model_fill_rate` and `exact_fill_rate` (0.1.1); a summary without them reads
  "fills 0.7 pp less often than the exact replay (...)". With `aborts` (`abort_shares` of the zoo summary's warnings)
  a model with faithful hbt #312 aborts adds "; 12 of 3,000 shadow pairs (0.4%) aborted at the hbt #312 event
  (excluded)".
- `rates_at(entry, horizon_ns)`: (model fill rate, exact fill rate) at the horizon.
- `abort_shares(warnings)`: model id -> (aborted pairs, shadows) from the `hbt_312_abort: ` warning lines.

Entries are `qt.zoo_summary.leaderboard` items, as dicts (`evaluate_zoo`) or `LeaderboardEntry` models; the bias is
model minus exact truth on never-cancel shadows whose label exists at the horizon (SPEC 5.6, audit ruling A2).

Model confidence set (Hansen, Lunde, Nason 2011; `queuetruth.stats.selection.model_confidence_set`): per horizon,
which models are statistically tied with the best. `horizon_losses` builds, on exactly the leaderboard rows
(never-cancel shadows of `common_population` with an exact arm whose label exists at `h <= h_max`), a balanced
`shadows x models` loss matrix per horizon (shadows lacking a row for one of the horizon's models are dropped,
`n_dropped`), with
- first-fill error `|min(T_model, h) - min(T_exact, h)| / h`, `T = ts_first_fill - ts_live` (null = never; the
  exact term is `h` when the exact arm did not fill by `h`); the particle family reports the median particle
  first-fill time, the point forecast that minimises expected absolute error; the touch families with `p < 1`
  (`nt_prob_touch`, `nt_queue_l2_prob_touch`: analytic `p_fill`, one RNG-sampled fill path) are scored on their
  sampled path, the fill time a backtest that uses them would see, so their sampling noise counts as error; a share
  of the horizon in [0, 1];
- Brier `(p_fill - truth_filled)^2` (its mean equals the entry's `brier[h]` when `n_dropped = 0`; otherwise the
  dropped shadows are in `brier[h]` and not in the MCS, and a warning says so).
`leaderboard_mcs` runs the `T_R` MCS (`MCS_METHOD`) at `alpha = MCS_ALPHA` (the 90% MCS) on each loss over the
blocks of the leaderboard CIs (session dates with >= 5 dates, else 30-minute buckets of `ts_live`, labelled over the
whole population as `evaluate_zoo` does) with the wild cluster bootstrap-t (`MCS_STUDENTIZE = "wild"`: Webb weights
per block from `bootstrap_rng(seed, "zoo_mcs")`, `bootstrap_b` replicates, each studentized with its own
cluster-robust variance; the fixed bootstrap variance of Hansen, Lunde and Nason keeps the whole set of 16 equal
models 0.42 of the time at 13 blocks instead of 0.9, the wild one 0.88, docs/concepts/selection-bias.md), over
the L2-input models: the order-level families `ORDER_LEVEL_FAMILIES` (`hbt_l3_fifo` reads the MBO records and,
corrected, equals the exact engine by construction, O-ZOO-4) are left out by default, else the set would be that
reference alone at every horizon; their MCS fields are null. A horizon with fewer than `MCS_MIN_BLOCKS` bootstrap
blocks gets null MCS fields and an `mcs: ` warning (one block has no between-block variance; the wild bootstrap fails
at 3). `leaderboard_mcs` and `annotate_mcs` return `(result, warnings)`. `annotate_mcs` adds the additive entry
fields `first_fill_error`, `mcs_member`, `mcs_p` (first-fill error) and `mcs_brier_member`, `mcs_brier_p` (Brier), keyed
by horizon (null where the model has no row at the horizon or the horizon has too few blocks). A p-value has resolution
`1 / bootstrap_b` (0 = below it). `mcs_mark(entry, h)` is the CLI marker (`MCS_MARK` for a member), `mcs_table` the
CLI table of first-fill errors and `mcs_sentence` its plain-words line. The spec and its run_id are unchanged: the
MCS reuses `bootstrap_b` and `seed`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.eval import _cols as C

DEFAULT_SORT_HORIZON_NS = 60_000_000_000
#: Biases smaller than this (in absolute value) read "as often as".
NEGLIGIBLE_BIAS = 0.0005


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _finite(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def bias_at(entry: Any, horizon_ns: int | None) -> tuple[float | None, float | None, float | None]:
    """(value, ci_lo, ci_hi) of the entry's `fill_rate_bias` at the horizon (Nones when absent)."""
    if horizon_ns is None:
        return None, None, None
    fb = _field(entry, "fill_rate_bias") or {}
    cv = fb.get(str(int(horizon_ns))) if isinstance(fb, dict) else None
    if cv is None:
        return None, None, None
    return _finite(_field(cv, "value")), _finite(_field(cv, "ci_lo")), _finite(_field(cv, "ci_hi"))


def rates_at(entry: Any, horizon_ns: int | None) -> tuple[float | None, float | None]:
    """(model fill rate, exact fill rate) of the entry at the horizon (Nones when absent, e.g. before 0.1.1)."""
    if horizon_ns is None:
        return None, None
    key = str(int(horizon_ns))
    out = []
    for name in ("model_fill_rate", "exact_fill_rate"):
        m = _field(entry, name) or {}
        out.append(_finite(m.get(key)) if isinstance(m, dict) else None)
    return out[0], out[1]


def brier_at(entry: Any, horizon_ns: int | None) -> float | None:
    """The entry's Brier at the horizon (None when absent)."""
    if horizon_ns is None:
        return None
    b = _field(entry, "brier") or {}
    return _finite(b.get(str(int(horizon_ns)))) if isinstance(b, dict) else None


def horizons_of(entries: Iterable[Any]) -> list[int]:
    """Horizons present in the entries' `fill_rate_bias` or `brier` maps, ascending."""
    out: set[int] = set()
    for e in entries:
        for name in ("fill_rate_bias", "brier"):
            m = _field(e, name) or {}
            if isinstance(m, dict):
                out.update(int(k) for k in m)
    return sorted(out)


def sort_horizon(horizons: Sequence[int], target_ns: int = DEFAULT_SORT_HORIZON_NS) -> int | None:
    """The horizon the leaderboard is sorted at (module docstring)."""
    hs = sorted({int(h) for h in horizons})
    if not hs:
        return None
    if int(target_ns) in hs:
        return int(target_ns)
    below = [h for h in hs if h < int(target_ns)]
    return below[-1] if below else hs[0]


def rank_leaderboard(entries: Sequence[Any], horizon_ns: int | None) -> list[Any]:
    """Entries sorted by absolute fill-rate bias at `horizon_ns`, then Brier, then model id."""

    def key(e: Any) -> tuple[bool, float, float, str]:
        v = bias_at(e, horizon_ns)[0]
        b = brier_at(e, horizon_ns)
        bias = abs(v) if v is not None else math.inf
        return (v is None, bias, b if b is not None else math.inf, str(_field(e, "model_id")))

    return sorted(entries, key=key)


def duration_text(ns: int) -> str:
    """`60 s`, `500 ms`, `1.5 s`, `5 min` (plain ASCII; minutes from 5 min on)."""
    v = float(ns)
    for unit, scale in (("min", 6e10), ("s", 1e9), ("ms", 1e6), ("us", 1e3)):
        if v >= scale and (unit != "min" or (v % scale == 0 and v >= 3e11)):
            return f"{v / scale:g} {unit}"
    return f"{int(ns)} ns"


def pct(x: float) -> str:
    """A fraction as a percentage: one decimal (two below 0.1%), no sign (`11.0`, `0.04`)."""
    v = 100.0 * x
    return f"{v:.{1 if abs(v) >= 0.1 or v == 0 else 2}f}"


def _pp(x: float, signed: bool = True) -> str:
    """Percentage points of a fraction: one decimal (two below 0.1 pp), explicit sign unless zero or unsigned."""
    v = 100.0 * x
    digits = 1 if abs(v) >= 0.1 or v == 0 else 2
    if not signed:
        return f"{abs(v):.{digits}f}"
    s = f"{v:+.{digits}f}"
    return s[1:] if float(s) == 0 else s


ABORT_PREFIX = "hbt_312_abort: "


def abort_shares(warnings: Iterable[str] | None) -> dict[str, tuple[int, int]]:
    """Model id -> (aborted pairs, shadows) from a zoo summary's `hbt_312_abort: <model>: <n> of <N> shadow
    pair(s) ...` warning lines (faithful hbt #312 aborts, per (model, shadow) pair)."""
    out: dict[str, tuple[int, int]] = {}
    for w in warnings or ():
        if not isinstance(w, str) or not w.startswith(ABORT_PREFIX):
            continue
        rest = w[len(ABORT_PREFIX) :]
        model, _, tail = rest.partition(": ")
        parts = tail.split(" ", 3)
        if len(parts) >= 3 and parts[1] == "of" and parts[0].isdecimal() and parts[2].isdecimal():
            out[model] = (int(parts[0]), int(parts[2]))
    return out


def _abort_text(n: int, total: int) -> str:
    share = f" ({100.0 * n / total:.1f}%)" if total else ""
    return f"{n:,} of {total:,} shadow pairs{share} aborted at the hbt #312 event (excluded)"


def bias_sentence(entry: Any, horizon_ns: int | None, aborts: dict[str, tuple[int, int]] | None = None) -> str:
    """One plain-English sentence on the model's fill-rate bias at the horizon (module docstring)."""
    m = str(_field(entry, "model_id"))
    ab = (aborts or {}).get(m)
    if ab is not None and ab[0] > 0:
        return _bias_sentence(entry, horizon_ns, m)[:-1] + "; " + _abort_text(*ab) + "."
    return _bias_sentence(entry, horizon_ns, m)


def reality_gap_line(ranked: Sequence[Any], horizon_ns: int | None) -> str | None:
    """The zoo's answer in one line, first: the closest and the furthest model from the exact replay at the horizon
    (fill-rate bias, model minus exact, in pp; `ranked` as `rank_leaderboard` orders it). None without a bias."""
    rows = [(str(_field(e, "model_id")), bias_at(e, horizon_ns)[0]) for e in ranked]
    rows = [(m, v) for m, v in rows if v is not None]
    if horizon_ns is None or not rows:
        return None
    when = duration_text(int(horizon_ns))
    (m0, v0), (m1, v1) = rows[0], rows[-1]
    if len(rows) == 1:
        return f"reality gap at {when}: {m0} {_pp(v0)} pp (model minus exact fill rate)"
    return (
        f"reality gap at {when}: from {_pp(v0)} pp ({m0}, the closest) to {_pp(v1)} pp ({m1}, the furthest), "
        "model minus exact fill rate"
    )


def _bias_sentence(entry: Any, horizon_ns: int | None, m: str) -> str:
    if horizon_ns is None:
        return f"{m}: no horizon with an exact label."
    when = duration_text(int(horizon_ns))
    v, lo, hi = bias_at(entry, horizon_ns)
    if v is None:
        return f"At {when}, {m} has no fill-rate bias: no shadow has an exact label at that horizon."
    ci = f"95% CI {_pp(lo)} to {_pp(hi)} pp" if lo is not None and hi is not None else "no CI"
    noise = lo is not None and hi is not None and lo <= 0.0 <= hi
    rate, exact = rates_at(entry, horizon_ns)
    more = "more" if v > 0 else "less"
    if abs(v) < NEGLIGIBLE_BIAS:
        if rate is not None and exact is not None:
            what = f"fills {pct(rate)}% of orders, as often as the exact replay ({pct(exact)}%, bias {_pp(v)} pp)"
        else:
            what = f"fills as often as the exact replay (bias {_pp(v)} pp)"
    elif rate is not None and exact is not None:
        what = (
            f"fills {pct(rate)}% of orders vs {pct(exact)}% in the exact replay, {_pp(v, signed=False)} pp {more} often"
        )
    else:
        what = f"fills {_pp(v, signed=False)} pp {more} often than the exact replay"
    return f"At {when}, {m} {what}{', within noise' if noise and abs(v) >= NEGLIGIBLE_BIAS else ''} ({ci})."


# ---- model confidence set -----------------------------------------------------------------------------------------

#: Level of the leaderboard MCS: a member has MCS p-value > 0.10 (the 90% model confidence set).
MCS_ALPHA = 0.10
#: Test statistic of the leaderboard MCS (`T_R`, the range statistic of Hansen, Lunde and Nason).
MCS_METHOD = "R"
#: Studentization of the leaderboard MCS: the wild cluster bootstrap-t (`model_confidence_set(studentize="wild")`).
MCS_STUDENTIZE = "wild"
#: Fewest bootstrap blocks (session dates or 30-minute buckets) at a horizon for its MCS; as `MIN_DAY_BLOCKS`.
MCS_MIN_BLOCKS = 5
MCS_TEST_ID = "zoo_mcs"
MCS_WARNING_PREFIX = "mcs: "
#: CLI table marker of a model in the first-fill-error MCS at a horizon.
MCS_MARK = "#"
#: Families fed order-level (MBO) records rather than L2 rows; left out of the leaderboard MCS by default.
ORDER_LEVEL_FAMILIES = ("hbt_l3_fifo",)
_LOSS_COLS = [
    "model_id",
    "shadow_id",
    "horizon_ns",
    "p_fill",
    "model_ts_first_fill",
    "truth_filled",
    "truth_ts_first_fill",
    "session_date",
    "ts_live",
]


@dataclass(frozen=True)
class HorizonLosses:
    """Balanced per-shadow losses at one horizon: rows = `shadow_ids` (ascending), columns = `model_ids` (first
    appearance order); `blocks` = the bootstrap block label of each row, `block_kind` = `"day"` or `"30min"`;
    `n_dropped` = shadows left out because a model of the horizon has no row for them."""

    horizon_ns: int
    model_ids: tuple[str, ...]
    shadow_ids: np.ndarray
    blocks: np.ndarray
    first_fill: np.ndarray
    brier: np.ndarray
    n_dropped: int
    block_kind: str = "30min"

    @property
    def n_blocks(self) -> int:
        return int(np.unique(self.blocks).size)


def horizon_losses(
    predictions: pa.Table,
    planned_life: pa.Table | None,
    *,
    h_max_ns: int,
    exclude_families: Sequence[str] = (),
) -> list[HorizonLosses]:
    """Per horizon `<= h_max_ns` (ascending) the leaderboard's per-shadow loss matrices (module docstring), without
    the models of `exclude_families`. `QT-INPUT-001` without `planned_life`, `QT-INPUT-002` for missing columns or
    shadows, `QT-INPUT-003` for a duplicate (model, shadow, horizon) row."""
    from queuetruth.eval.zoo import _first_order, _planned_life, common_population, family
    from queuetruth.stats import block_labels

    C.require(predictions, _LOSS_COLS, "horizon_losses")
    if planned_life is None:
        raise QtError(
            "QT-INPUT-001",
            "horizon_losses: planned_life (qt.zoo_shadows: shadow_id, ts_cancel_planned, ts_admin_end) is required; "
            "label eligibility is never inferred from the censored flag (audit ruling A2, E38 OC-7)",
        )
    t = common_population(predictions)
    sid = C.strings(t, "shadow_id")
    if sid.size == 0:
        return []
    model = C.strings(t, "model_id")
    h, _ = C.ints(t, "horizon_ns")
    p = C.floats(t, "p_fill")
    y, yv = C.bools(t, "truth_filled")
    m_ts, m_tsv = C.ints(t, "model_ts_first_fill")
    x_ts, x_tsv = C.ints(t, "truth_ts_first_fill")
    live, _ = C.ints(t, "ts_live")
    date, _ = C.ints(t, "session_date")
    ids, first, inv = np.unique(sid, return_index=True, return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    labels, kind = block_labels(date[first], live[first])
    cancel_rel, admin_rel = _planned_life(planned_life, ids, live[first])
    never_cancel = cancel_rel >= float(h_max_ns)
    hf = h.astype(float)
    keep = yv & (h <= h_max_ns) & (h > 0) & never_cancel[inv] & (admin_rel[inv] >= hf)
    names, code = np.unique(model, return_inverse=True)
    code = np.asarray(code).reshape(-1)
    if exclude_families:
        allowed = np.array([family(str(m)) not in exclude_families for m in names], dtype=bool)
        keep &= allowed[code]
    t_model = np.where(m_tsv, np.maximum(m_ts - live, 0).astype(float), np.inf)
    t_exact = np.where(x_tsv & y, np.maximum(x_ts - live, 0).astype(float), np.inf)
    ff = np.abs(np.minimum(t_model, hf) - np.minimum(t_exact, hf)) / np.maximum(hf, 1.0)
    br = (p - y.astype(float)) ** 2
    rank = {str(m): i for i, m in enumerate(_first_order(model))}
    out: list[HorizonLosses] = []
    for hz in np.unique(h[keep]):
        rows = np.flatnonzero(keep & (h == hz))
        present = np.unique(code[rows])
        present = present[np.argsort([rank[str(names[c])] for c in present], kind="stable")]
        mids = tuple(str(names[c]) for c in present)
        col_of = np.full(names.size, -1, dtype=np.int64)
        col_of[present] = np.arange(present.size)
        cj = col_of[code[rows]]
        si = inv[rows]
        k = len(mids)
        uniq, cnt = np.unique(si.astype(np.int64) * k + cj, return_counts=True)
        if (cnt > 1).any():
            dup = int(uniq[np.argmax(cnt > 1)])
            raise QtError(
                "QT-INPUT-003",
                f"duplicate prediction row: model {mids[dup % k]}, shadow {ids[dup // k]}, horizon {int(hz)}",
            )
        filled = np.zeros((ids.size, k), dtype=bool)
        filled[si, cj] = True
        complete = filled.all(axis=1)
        sh = np.flatnonzero(complete)
        ff_m = np.full((ids.size, k), np.nan)
        br_m = np.full((ids.size, k), np.nan)
        ff_m[si, cj] = ff[rows]
        br_m[si, cj] = br[rows]
        out.append(
            HorizonLosses(
                horizon_ns=int(hz),
                model_ids=mids,
                shadow_ids=ids[sh],
                blocks=labels[sh],
                first_fill=ff_m[sh],
                brier=br_m[sh],
                n_dropped=int((filled.any(axis=1) & ~complete).sum()),
                block_kind=str(kind),
            )
        )
    return out


def leaderboard_mcs(
    predictions: pa.Table,
    planned_life: pa.Table | None,
    *,
    h_max_ns: int,
    bootstrap_b: int = 1000,
    seed: int = 0,
    alpha: float = MCS_ALPHA,
    method: str = MCS_METHOD,
    exclude_families: Sequence[str] = ORDER_LEVEL_FAMILIES,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str]]:
    """(model id -> {field -> {horizon key -> value}} for the MCS fields of `annotate_mcs`, `mcs: ` warnings)
    (module docstring)."""
    from queuetruth.stats.selection import model_confidence_set

    out: dict[str, dict[str, dict[str, Any]]] = {}
    notes: list[str] = []
    for hl in horizon_losses(predictions, planned_life, h_max_ns=h_max_ns, exclude_families=exclude_families):
        key = str(hl.horizon_ns)
        when = duration_text(hl.horizon_ns)
        if hl.n_dropped:
            notes.append(
                f"{MCS_WARNING_PREFIX}at {when}, {hl.n_dropped:,} shadow(s) lack a row for some model and are left "
                f"out of the model confidence set (its Brier means then differ from brier)"
            )
        if hl.n_blocks < MCS_MIN_BLOCKS:
            notes.append(
                f"{MCS_WARNING_PREFIX}at {when}, {hl.n_blocks} bootstrap block(s) ({hl.block_kind}) < "
                f"{MCS_MIN_BLOCKS}: no model confidence set (MCS fields null)"
            )
            continue
        for loss, member_f, p_f in (
            (hl.first_fill, "mcs_member", "mcs_p"),
            (hl.brier, "mcs_brier_member", "mcs_brier_p"),
        ):
            res = model_confidence_set(
                loss,
                alpha=alpha,
                method=method,  # type: ignore[arg-type]
                b=bootstrap_b,
                seed=seed,
                test_id=MCS_TEST_ID,
                blocks=hl.blocks,
                studentize=MCS_STUDENTIZE,
            )
            for j, m in enumerate(hl.model_ids):
                f = out.setdefault(m, {})
                f.setdefault(member_f, {})[key] = bool(res.p_values[j] > alpha)
                f.setdefault(p_f, {})[key] = float(res.p_values[j])
                if loss is hl.first_fill:
                    f.setdefault("first_fill_error", {})[key] = float(res.mean_loss[j])
    return out, notes


MCS_FIELDS = ("first_fill_error", "mcs_member", "mcs_p", "mcs_brier_member", "mcs_brier_p")


def annotate_mcs(
    entries: Sequence[Any],
    predictions: pa.Table,
    planned_life: pa.Table | None,
    *,
    h_max_ns: int,
    bootstrap_b: int = 1000,
    seed: int = 0,
    alpha: float = MCS_ALPHA,
    method: str = MCS_METHOD,
    exclude_families: Sequence[str] = ORDER_LEVEL_FAMILIES,
) -> tuple[list[Any], list[str]]:
    """(the entries with the MCS fields set for every horizon of the entry, null where undefined; the `mcs: `
    warnings). Dict entries come back as new dicts, `LeaderboardEntry` models as updated copies; the input is not
    modified."""
    by_model, notes = leaderboard_mcs(
        predictions,
        planned_life,
        h_max_ns=h_max_ns,
        bootstrap_b=bootstrap_b,
        seed=seed,
        alpha=alpha,
        method=method,
        exclude_families=exclude_families,
    )
    out = []
    for e in entries:
        keys = [str(k) for k in horizons_of([e])]
        got = by_model.get(str(_field(e, "model_id")), {})
        upd = {name: {k: (got.get(name) or {}).get(k) for k in keys} for name in MCS_FIELDS}
        out.append({**e, **upd} if isinstance(e, dict) else e.model_copy(update=upd))
    return out, notes


def mcs_mark(entry: Any, horizon_ns: int | None) -> str:
    """`MCS_MARK` when the entry is in the first-fill-error MCS at the horizon, else a space."""
    if horizon_ns is None:
        return " "
    m = _field(entry, "mcs_member") or {}
    return MCS_MARK if isinstance(m, dict) and m.get(str(int(horizon_ns))) is True else " "


def has_mcs(entries: Iterable[Any]) -> bool:
    """True when any entry carries an MCS membership value."""
    return any(v is not None for e in entries for v in ((_field(e, "mcs_member") or {}).values()))


def _ffe(entry: Any, horizon_ns: int) -> float | None:
    m = _field(entry, "first_fill_error") or {}
    return _finite(m.get(str(int(horizon_ns)))) if isinstance(m, dict) else None


def mcs_table(entries: Sequence[Any], horizons: Sequence[int]) -> tuple[list[str], list[list[str]]]:
    """(legend lines, CLI rows `[model, cell per horizon]`) of the first-fill-error table: the entries that have a
    first-fill error at some horizon, in the given order, each cell the error in % of the horizon plus `mcs_mark`
    (`-` where undefined); the legend names the entries left out (the order-level reference, or no MCS rows)."""
    from queuetruth.eval.zoo import family

    rows, unranked = [], []
    for e in entries:
        mid = str(_field(e, "model_id"))
        vals = [_ffe(e, h) for h in horizons]
        if all(v is None for v in vals):
            why = "order-level reference" if family(mid) in ORDER_LEVEL_FAMILIES else "no MCS rows"
            unranked.append(f"{mid} ({why})")
            continue
        cells = [pct(v) + mcs_mark(e, h) if v is not None else "-  " for v, h in zip(vals, horizons, strict=True)]
        rows.append([mid, *cells])
    legend = [f"first-fill error in % of the horizon (# = tied with the best, {100 * (1 - MCS_ALPHA):g}% MCS):"]
    if unranked:
        legend.insert(0, f"not ranked on first-fill error: {', '.join(unranked)}")
    return legend, rows


def mcs_sentence(entries: Sequence[Any], horizon_ns: int | None) -> str | None:
    """ "At 60 s, 3 of 16 models are tied for the lowest first-fill error (90% MCS): a (0.8%), b (1.0%), c (1.1%)."
    (members by ascending error), or a sentence saying there is no set at that horizon; None without MCS fields."""
    if horizon_ns is None or not has_mcs(entries):
        return None
    when = duration_text(int(horizon_ns))
    key = str(int(horizon_ns))
    scored = [e for e in entries if isinstance(m := _field(e, "mcs_member") or {}, dict) and m.get(key) is not None]
    if not scored:
        return f"At {when} there is no model confidence set (see the mcs warnings)."
    members = sorted(
        (e for e in scored if (_field(e, "mcs_member") or {}).get(key) is True),
        key=lambda e: (v if (v := _ffe(e, int(horizon_ns))) is not None else math.inf, str(_field(e, "model_id"))),
    )
    level = f"{100 * (1 - MCS_ALPHA):g}% MCS"

    def named(e: Any) -> str:
        v = _ffe(e, int(horizon_ns))
        return f"{_field(e, 'model_id')}" + (f" ({pct(v)}%)" if v is not None else "")

    names = ", ".join(named(e) for e in members)
    if len(members) == 1:
        return f"At {when}, {names} alone has the lowest first-fill error of {len(scored)} models ({level})."
    tied = f"{len(members)} of {len(scored)} models are tied for the lowest first-fill error"
    return f"At {when}, {tied} ({level}): {names}."


__all__ = [
    "ABORT_PREFIX",
    "DEFAULT_SORT_HORIZON_NS",
    "MCS_ALPHA",
    "MCS_FIELDS",
    "MCS_MARK",
    "MCS_METHOD",
    "MCS_MIN_BLOCKS",
    "MCS_STUDENTIZE",
    "MCS_WARNING_PREFIX",
    "ORDER_LEVEL_FAMILIES",
    "HorizonLosses",
    "abort_shares",
    "annotate_mcs",
    "bias_at",
    "bias_sentence",
    "brier_at",
    "duration_text",
    "has_mcs",
    "horizon_losses",
    "horizons_of",
    "leaderboard_mcs",
    "mcs_mark",
    "mcs_sentence",
    "mcs_table",
    "pct",
    "rank_leaderboard",
    "rates_at",
    "sort_horizon",
]
