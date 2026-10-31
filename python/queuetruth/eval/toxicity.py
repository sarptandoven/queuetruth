"""Fill-type tags, markouts and the adverse-selection ranking of zoo models (docs/concepts/fill-toxicity.md; design
contract docs/design/extensions/fill-toxicity.md).

The leaderboard says how often a model's fills disagree with the exact arm; this module says what the disagreement is
worth. Every fill of the exact arm and of each model arm within the fill window gets a mechanism, every (model,
shadow) pair a status, and every fill a signed mid markout at each horizon.

Population (the leaderboard's rule applied at `W`, `queuetruth.eval.zoo`): the shadows of `common_population` (no
faithful hbt #312 abort on them) with an exact arm (`truth_filled` not null), no planned cancel before `ts_live + W`
and an administrative end at or after `ts_live + W`, where `W = window_ns` (default the zoo's `h_max_ns`) is the fill
window. Left out and counted in `n_excluded`: a shadow whose exact fill rows within `W` do not reproduce the zoo's
truth (`truth_filled` and `truth_ts_first_fill` of the `horizon_ns = W` rows; `truth_mismatch`, an error unless
`allow_truth_mismatch`: it means the replay is not the zoo's exact arm), and a shadow the exact replay rejected before
it went live (`end_state` `rejected_*`; `never_live`) that no arm fills (a model fill on one keeps it, as a phantom).

Mechanisms (`MECHANISMS`). Exact rows (`qt.shadow_fills.reason`): `queue_depleted` -> `queue` (queue consumed by
executions at the price), `trade_through` -> `swept` (the level traded through), `crossed_at_live` ->
`cross_on_arrival` (a taker fill on arrival), `crossed` -> `crossed_resting` (a resting order crossed by an opposite
arrival); a `queue_depleted` or `trade_through` row whose (instrument_id, ts_event, sequence) is a `hidden` print of
`qt.trades` and no `displayed` print is `hidden_print` (SPEC 5.2.4a A3d: a print record has its own sequence, an
execution's T, F and removal records share one; the timestamp keeps sequences of different days apart). Model rows
(`qt.zoo_fills.reason`): `queue` -> `queue`, `trade_through` -> `swept`, `crossed_at_live` -> `cross_on_arrival`,
`crossed` -> `crossed_resting`, `touch_draw` -> `touch`. Particle families (`databento_mc`, which write no fill rows)
get one row per filled shadow at the median particle first-fill time (`model_ts_first_fill`) for the whole quantity,
mechanism `particle`.

Status of (model, shadow): exact filled = `truth_filled` at `horizon_ns = W`; model filled = the model booked a fill
in the window: a `qt.zoo_fills` row with `ts_event <= ts_live + W` (the realized simulated fill, also for the
families whose fill rows are random draws, `nt_prob_touch[p<1]` and `nt_queue_l2_prob_touch`), and for the particle
families `p_fill >= 0.5` at `W` (the leaderboard's rule; their pseudo-row is the median particle). `confirmed` (both),
`phantom` (model only), `missed` (exact only), `neither`. `n_inconsistent` cross-checks the forecast against the
rows: a non-particle pair whose `p_fill` at `W` is exactly 0 or 1 and disagrees with the presence of a fill row
(the rows decide its status), or a filled particle pair without a median first-fill time in the window (no markout).

Fill prices: exact rows `fill_px`; model maker rows the shadow's price (`qt.shadow_results.price`, the price both
arms rest at); model taker rows (`liquidity = taker`) the prevailing opposite best at the fill, capped at the limit
(`min(ask, limit)` buying, `max(bid, limit)` selling; the limit when there is no quote; the zoo writes no model fill
price).

Markouts (the scorer's conventions, SPEC 5.7.5): `d = +1` buy, `-1` sell; `M(t)` = mid of the prevailing `qt.bbo`
row of the fill's instrument-day (last row with `ts_event <= t`, equal timestamps: the last in file order), null with
an empty side, before the first row or when `t` is past the instrument-day's last event; `M(t-)` = last row strictly
before `t` (no end check). `mo_ticks = d (M(t+h) - P) / tick`, `mo_bps = d (M(t+h) - P) / |P| 1e4`, `mm_ticks =
d (M(t+h) - M(t-)) / tick`, `mm_bps = d (M(t+h) - M(t-)) / M(t-) 1e4`. Prices are differenced as integers first.

Per-shadow PnL decomposition, model `m`, horizon `h`: `mo^m_s`, `mo^e_s` = `mo_ticks` of the first fill row (earliest
`ts_event`, then `fill_seq`) of shadow `s` in the model and the exact arm, unit quantity; the `n` shadows whose
first-fill markouts are defined in every arm that filled them. Per shadow of those `n`: `grant = sum_phantom mo^m / n`
(PnL of the fills the model wrongly grants), `deny = -sum_missed mo^e / n` (minus the PnL of the fills it wrongly
denies), `timing = sum_confirmed (mo^m - mo^e) / n`, `bias = grant + timing + deny = model_pnl - exact_pnl` with
`model_pnl = sum_{model filled} mo^m / n`, `exact_pnl = sum_{exact filled} mo^e / n`. Positive = the model's simulated
PnL is too high. The same in bps (`*_bps`). `evaluate_toxicity` also returns these per-shadow terms (`PnlTerms`), which
sharded runs sum (`queuetruth.eval.blocks.pnl_block_stats`, the W1 atlas). Status means: `phantom_mo_ticks` = mean
`mo^m` over the phantom shadows of the `n`, likewise `missed` (`mo^e`), `confirmed` (`mo^m`) and `confirmed_exact`
(`mo^e`), and `*_mm_ticks` the same with `mm_ticks` (shadows with a defined `mm`).

Fill-type table (`by_type`): per arm (`exact`, then each model), status (exact: null; model rows: `confirmed` /
`phantom`; `missed`: the exact rows of the model's missed shadows) and mechanism (each present one, then `all`), the
`fill_qty`-weighted mean `mo_ticks`, `mo_bps`, `mm_ticks`, `mm_bps` over the rows where each is defined.

CIs: 95% percentile block bootstrap, blocks = session dates (>= 5 dates) else 30-minute buckets of `ts_live` of the
population's shadows; one draw matrix `block_counts(n_blocks, bootstrap_b, stable_seed(seed, [b"bootstrap",
"zoo_toxicity"]))` shared by every statistic; replicate = `sum_k count_k num_k / sum_k count_k den_k` (a fill row
belongs to its shadow's block). Rankings at `rank_horizon_ns`: `grant`, `deny` and `bias` each by `|value|`
descending (rank 1 = the largest PnL distortion of that kind, the signed value kept), undefined values last, ties in
model order; from the shared replicates each place carries a rank interval (2.5% quantile of the replicate's
competition rank, 97.5% quantile of its tie-inclusive rank) and the paired gap `|value| - |next value|` to the next
place with its CI (`separated_from_next` = the gap's CI lies above 0). Output order: exact arm, then models in
first-appearance order of the predictions; shadows in first-appearance order; fill rows by shadow, then `fill_seq`.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import Field, field_validator

from queuetruth.errors import QtError
from queuetruth.eval import _cols as C
from queuetruth.eval.zoo import PARTICLE_FAMILIES, _planned_life, aborted_shadows, common_population, family
from queuetruth.schemas import arrow as qa
from queuetruth.schemas.specs import NonNegInt, PathRef, SpecModel
from queuetruth.stats import block_counts, block_labels, bootstrap_seed, percentile_ci

MECHANISMS = ("queue", "swept", "cross_on_arrival", "crossed_resting", "hidden_print", "touch", "particle")
STATUSES = ("confirmed", "phantom", "missed", "neither")
EXACT_ARM = "exact"
DEFAULT_HORIZONS_NS = (100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000)
DEFAULT_RANK_HORIZON_NS = 60_000_000_000
BOOTSTRAP_TEST_ID = "zoo_toxicity"
RANKED = (("grant", "grant_ticks"), ("deny", "deny_ticks"), ("bias", "bias_ticks"))
#: `qt.shadow_fills.reason` -> mechanism (before the hidden-print tag).
EXACT_MECHANISM = {
    "queue_depleted": "queue",
    "trade_through": "swept",
    "crossed": "crossed_resting",
    "crossed_at_live": "cross_on_arrival",
}
#: `qt.zoo_fills.reason` -> mechanism.
MODEL_MECHANISM = {
    "queue": "queue",
    "trade_through": "swept",
    "crossed": "crossed_resting",
    "crossed_at_live": "cross_on_arrival",
    "touch_draw": "touch",
}
_HIDDEN_ELIGIBLE = ("queue_depleted", "trade_through")
#: prediction columns `evaluate_toxicity` reads (with `status` for the population filter)
_PRED_READ = (
    "model_id",
    "shadow_id",
    "qty",
    "horizon_ns",
    "p_fill",
    "model_ts_first_fill",
    "truth_filled",
    "truth_ts_first_fill",
    "instrument_id",
    "session_date",
    "ts_live",
    "status",
)
_MEASURES = ("mo_ticks", "mo_bps", "mm_ticks", "mm_bps")
_NONE_TS = np.iinfo(np.int64).max

_TS = pa.timestamp("ns", tz="UTC")
_DICT = pa.dictionary(pa.int8(), pa.utf8())
#: Arrow tables written by `zoo-toxicity` (column, type, nullable, dictionary values).
TABLES: dict[str, list[tuple[str, pa.DataType, bool, tuple[str, ...] | None]]] = {
    "qt.zoo_toxicity_fills": [
        ("arm", pa.utf8(), False, None),
        ("shadow_id", pa.utf8(), False, None),
        ("fill_seq", pa.uint32(), False, None),
        ("instrument_id", pa.uint32(), False, None),
        ("side", pa.uint8(), False, None),
        ("ts_event", _TS, False, None),
        ("px", pa.int64(), True, None),
        ("fill_qty", pa.float64(), False, None),
        ("liquidity", _DICT, False, ("maker", "taker")),
        ("mechanism", _DICT, False, MECHANISMS),
        ("status", _DICT, True, ("confirmed", "phantom")),
        ("reason", pa.utf8(), False, None),
    ],
    "qt.zoo_toxicity_markouts": [
        ("arm", pa.utf8(), False, None),
        ("shadow_id", pa.utf8(), False, None),
        ("fill_seq", pa.uint32(), False, None),
        ("horizon_ns", pa.int64(), False, None),
        ("mo_ticks", pa.float64(), True, None),
        ("mo_bps", pa.float64(), True, None),
        ("mm_ticks", pa.float64(), True, None),
        ("mm_bps", pa.float64(), True, None),
    ],
    "qt.zoo_toxicity_shadows": [
        ("model_id", pa.utf8(), False, None),
        ("shadow_id", pa.utf8(), False, None),
        ("instrument_id", pa.uint32(), False, None),
        ("session_date", pa.date32(), False, None),
        ("ts_live", _TS, False, None),
        ("status", _DICT, False, STATUSES),
        ("model_ts_first_fill", _TS, True, None),
        ("exact_ts_first_fill", _TS, True, None),
        ("model_mechanism", _DICT, True, MECHANISMS),
        ("exact_mechanism", _DICT, True, MECHANISMS),
    ],
}


def table_schema(name: str) -> pa.Schema:
    """Arrow schema of a `zoo-toxicity` table (`qt.schema` metadata as the registered tables)."""
    fields = [pa.field(n, t, nullable=nl) for n, t, nl, _ in TABLES[name]]
    return pa.schema(
        fields,
        metadata={
            b"qt.schema": name.encode(),
            b"qt.schema_version": qa.ARROW_SCHEMA_VERSION.encode(),
            b"qt.engine_version": qa.ENGINE_VERSION.encode(),
        },
    )


def _column(values: Any, t: pa.DataType, dict_values: tuple[str, ...] | None) -> pa.Array:
    """One output column from a numpy array (object arrays: None = null; float arrays: NaN = null) or an Arrow
    array."""
    if isinstance(values, (pa.Array, pa.ChunkedArray)):
        return values.cast(t) if not values.type.equals(t) else values
    v = np.asarray(values)
    if dict_values is not None:
        codes = np.full(v.size, -1, dtype=np.int8)
        for i, x in enumerate(dict_values):
            codes[v == x] = i
        null = np.array([x is None for x in v], dtype=bool) if v.dtype == object else np.zeros(v.size, dtype=bool)
        if ((codes < 0) & ~null).any():
            bad = sorted({str(x) for x in v[(codes < 0) & ~null]})
            raise QtError("QT-INPUT-002", f"zoo-toxicity: value(s) {bad} outside {list(dict_values)}")
        idx = pa.array(codes, type=pa.int8(), mask=codes < 0)
        return pa.DictionaryArray.from_arrays(idx, pa.array(list(dict_values), type=pa.utf8()))
    if pa.types.is_timestamp(t) or pa.types.is_date32(t):
        base = pa.int64() if pa.types.is_timestamp(t) else pa.int32()
        if v.dtype == object:
            return pa.array(v.tolist(), type=base).cast(t)
        return pa.array(v.astype(np.int64 if base == pa.int64() else np.int32), type=base).cast(t)
    if pa.types.is_floating(t):
        f = v.astype(float)
        return pa.array(f, type=t, mask=~np.isfinite(f))
    if pa.types.is_string(t):
        return pa.array(v.tolist() if v.dtype == object else [str(x) for x in v], type=t)
    if v.dtype == object:
        return pa.array(v.tolist(), type=t)
    return pa.array(v, type=t)


def _build(name: str, cols: dict[str, Any]) -> pa.Table:
    arrays = [_column(cols[n], t, values) for n, t, _, values in TABLES[name]]
    return pa.Table.from_arrays(arrays, schema=table_schema(name))


class ToxicitySpec(SpecModel):
    """`zoo-toxicity` arguments: the zoo output directory, the dataset it ran on and the evaluation settings."""

    zoo: PathRef
    dataset: PathRef
    #: shadow spec file of the zoo run (default: the zoo summary's `spec.shadows`)
    shadows: PathRef | None = None
    #: shadow amend file of the zoo run (default: the zoo summary's `spec.amends`; only when the zoo ran with amends)
    amends: PathRef | None = None
    out: str | None = None
    #: markout horizons; stored sorted and without duplicates, so equal sets give one run_id
    horizons_ns: list[int] = Field(default_factory=lambda: list(DEFAULT_HORIZONS_NS), min_length=1)
    #: fill window from `ts_live` (default: the zoo's `h_max_ns`, filled in before the run_id); must be a prediction
    #: horizon of the zoo run
    window_ns: int | None = Field(default=None, gt=0)
    rank_horizon_ns: int = Field(default=DEFAULT_RANK_HORIZON_NS, gt=0)
    bootstrap_b: int = Field(default=1000, ge=1)
    #: evaluate the shadows whose replay reproduces the zoo's exact arm even when some do not (default: an error)
    allow_truth_mismatch: bool = False
    #: replay threads (execution-only)
    threads: int = Field(default=1, ge=1)
    seed: NonNegInt = 0

    @field_validator("horizons_ns")
    @classmethod
    def _canonical_horizons(cls, v: list[int]) -> list[int]:
        if any(int(h) <= 0 for h in v):
            raise ValueError(f"every markout horizon must be > 0, got {list(v)}")
        return sorted({int(h) for h in v})


# ---- mechanisms --------------------------------------------------------------------------------------------------


def exact_mechanism(
    reason: np.ndarray, instrument_id: np.ndarray, ts_event: np.ndarray, sequence: np.ndarray, trades: pa.Table
) -> np.ndarray:
    """Mechanism of each exact fill row (module docstring): the reason's mechanism, `hidden_print` for a
    `queue_depleted` / `trade_through` row at the (instrument_id, ts_event, sequence) of a hidden print with no
    displayed print there."""
    C.require(trades, ["instrument_id", "ts_event", "sequence", "kind"], "trades")
    reason = np.asarray(reason, dtype=object)
    unknown = sorted({str(r) for r in reason} - set(EXACT_MECHANISM))
    if unknown:
        raise QtError("QT-INPUT-002", f"shadow_fills: unknown fill reason(s) {unknown}")
    out = np.empty(reason.size, dtype=object)
    for r, mech in EXACT_MECHANISM.items():
        out[reason == r] = mech
    elig = np.isin(reason, np.array(_HIDDEN_ELIGIBLE, dtype=object))
    if not elig.any() or trades.num_rows == 0:
        return out
    iid = np.asarray(instrument_id, dtype=np.int64)
    ts = np.asarray(ts_event, dtype=np.int64)
    seq = np.asarray(sequence, dtype=np.int64)
    # only the prints at the fills' sequences matter: filter in Arrow before building keys
    kind = trades.column("kind")
    if pa.types.is_dictionary(kind.type):
        kind = kind.cast(pa.utf8())
    seq_col = trades.column("sequence").cast(pa.int64())
    mask = pc.and_(
        pc.is_in(kind, value_set=pa.array(["hidden", "displayed"])),
        pc.is_in(seq_col, value_set=pa.array(np.unique(seq[elig]), type=pa.int64())),
    )
    sub = trades.filter(mask)
    if sub.num_rows == 0:
        return out
    t_iid, _ = C.ints(sub, "instrument_id")
    t_ts, _ = C.ints(sub, "ts_event")
    t_seq, _ = C.ints(sub, "sequence")
    t_kind = C.strings(sub, "kind")
    hidden = {(int(a), int(b), int(c)) for a, b, c, k in zip(t_iid, t_ts, t_seq, t_kind, strict=True) if k == "hidden"}
    shown = {(int(a), int(b), int(c)) for a, b, c, k in zip(t_iid, t_ts, t_seq, t_kind, strict=True) if k != "hidden"}
    for j in np.flatnonzero(elig):
        key = (int(iid[j]), int(ts[j]), int(seq[j]))
        if key in hidden and key not in shown:
            out[j] = "hidden_print"
    return out


def model_mechanism(reason: np.ndarray) -> np.ndarray:
    """Mechanism of each `qt.zoo_fills` row."""
    reason = np.asarray(reason, dtype=object)
    unknown = sorted({str(r) for r in reason} - set(MODEL_MECHANISM))
    if unknown:
        raise QtError("QT-INPUT-002", f"zoo_fills: unknown fill reason(s) {unknown}")
    out = np.empty(reason.size, dtype=object)
    for r, mech in MODEL_MECHANISM.items():
        out[reason == r] = mech
    return out


# ---- quotes and markouts -----------------------------------------------------------------------------------------


@dataclass
class _Day:
    ts: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    ok: np.ndarray
    last: int


def _groups(*keys: np.ndarray) -> list[tuple[tuple[int, ...], np.ndarray]]:
    """Row indices per distinct key tuple (one sort, contiguous slices), keys ascending."""
    n = keys[0].size
    if n == 0:
        return []
    order = np.lexsort(tuple(np.asarray(k, dtype=np.int64) for k in reversed(keys)))
    sk = [np.asarray(k, dtype=np.int64)[order] for k in keys]
    change = np.zeros(n, dtype=bool)
    change[0] = True
    for k in sk:
        change[1:] |= k[1:] != k[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], n)
    return [(tuple(int(k[s]) for k in sk), order[s:e]) for s, e in zip(starts, ends, strict=True)]


class QuoteBook:
    """Prevailing-quote lookup per instrument-day from `qt.bbo` and `qt.instruments` (module docstring)."""

    def __init__(
        self, bbo: pa.Table, instruments: pa.Table, last_event_ts: dict[tuple[int, int], int] | None = None
    ) -> None:
        C.require(instruments, ["instrument_id", "session_date", "midnight_utc_ns", "tick_size"], "instruments")
        C.require(bbo, ["ts_event", "instrument_id", "bid_px", "ask_px"], "bbo")
        iid, _ = C.ints(instruments, "instrument_id")
        date, _ = C.ints(instruments, "session_date")
        mid, _ = C.ints(instruments, "midnight_utc_ns")
        tick, _ = C.ints(instruments, "tick_size")
        self._cal: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for (i,), g in _groups(iid):
            order = g[np.argsort(mid[g], kind="stable")]
            self._cal[i] = (mid[order], date[order], tick[order])
        b_iid, _ = C.ints(bbo, "instrument_id")
        b_ts, _ = C.ints(bbo, "ts_event")
        bid, bidv = C.ints(bbo, "bid_px")
        ask, askv = C.ints(bbo, "ask_px")
        b_date, _, known = self.day(b_iid, b_ts)
        self._days: dict[tuple[int, int], _Day] = {}
        last = dict(last_event_ts or {})
        keep = np.flatnonzero(known)
        order = keep[np.lexsort((keep, b_ts[keep], b_date[keep], b_iid[keep]))]
        if order.size:
            ki, kd = b_iid[order], b_date[order]
            cut = np.flatnonzero((np.diff(ki) != 0) | (np.diff(kd) != 0)) + 1
            for g in np.split(order, cut):
                key = (int(b_iid[g[0]]), int(b_date[g[0]]))
                self._days[key] = _Day(
                    ts=b_ts[g],
                    bid=bid[g],
                    ask=ask[g],
                    ok=bidv[g] & askv[g],
                    last=int(last.get(key, int(b_ts[g][-1]))),
                )

    def day(self, instrument_id: np.ndarray, ts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(session_date days, tick_size, known) of the instrument-day of each timestamp: the `qt.instruments` row
        with the latest `midnight_utc_ns <= ts` (the first row when `ts` precedes all)."""
        iid = np.asarray(instrument_id, dtype=np.int64)
        ts = np.asarray(ts, dtype=np.int64)
        n = ts.size
        date = np.zeros(n, dtype=np.int64)
        tick = np.zeros(n, dtype=np.int64)
        known = np.zeros(n, dtype=bool)
        for (i,), g in _groups(iid):
            cal = self._cal.get(i)
            if cal is None:
                continue
            j = np.clip(np.searchsorted(cal[0], ts[g], side="right") - 1, 0, None)
            date[g], tick[g], known[g] = cal[1][j], cal[2][j], True
        return date, tick, known

    def quote(
        self, instrument_id: np.ndarray, date: np.ndarray, at: np.ndarray, *, strict: bool, end_check: bool
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(bid, ask, defined) of the quote prevailing at `at` (`strict`: the last row strictly before) in the
        instrument-day (`instrument_id`, `date`); `end_check`: undefined past the instrument-day's last event."""
        at = np.asarray(at, dtype=np.int64)
        n = at.size
        bid = np.zeros(n, dtype=np.int64)
        ask = np.zeros(n, dtype=np.int64)
        ok = np.zeros(n, dtype=bool)
        for key, g in _groups(np.asarray(instrument_id), np.asarray(date)):
            d = self._days.get((key[0], key[1]))
            if d is None:
                continue
            j = np.searchsorted(d.ts, at[g], side="left" if strict else "right") - 1
            jj = np.clip(j, 0, None)
            good = (j >= 0) & d.ok[jj]
            if end_check:
                good &= at[g] <= d.last
            bid[g], ask[g], ok[g] = d.bid[jj], d.ask[jj], good
        return bid, ask, ok


def mid_markouts(
    book: QuoteBook,
    instrument_id: np.ndarray,
    ts: np.ndarray,
    side_sign: np.ndarray,
    px: np.ndarray,
    px_valid: np.ndarray,
    horizons_ns: list[int] | tuple[int, ...],
) -> dict[str, np.ndarray]:
    """`mo_ticks`, `mo_bps`, `mm_ticks`, `mm_bps` of each fill at each horizon, `(n, H)` float arrays with NaN where
    undefined (module docstring)."""
    iid = np.asarray(instrument_id, dtype=np.int64)
    ts = np.asarray(ts, dtype=np.int64)
    d = np.asarray(side_sign, dtype=float)
    p = np.asarray(px, dtype=np.int64)
    n, nh = ts.size, len(horizons_ns)
    out = {k: np.full((n, nh), np.nan) for k in _MEASURES}
    if n == 0:
        return out
    date, tick, known = book.day(iid, ts)
    if not known.all():
        missing = sorted({int(x) for x in iid[~known]})
        raise QtError("QT-INPUT-002", f"instruments: no instrument-day for instrument_id {missing}")
    b0, a0, ok0 = book.quote(iid, date, ts, strict=True, end_check=False)
    m0 = b0.astype(float) + (a0 - b0).astype(float) * 0.5
    tk = tick.astype(float)
    pv = np.asarray(px_valid, dtype=bool)
    absp = np.abs(p).astype(float)
    for k, h in enumerate(horizons_ns):
        b1, a1, ok1 = book.quote(iid, date, ts + int(h), strict=False, end_check=True)
        mo = (b1 - p).astype(float) + (a1 - b1).astype(float) * 0.5
        mm = (b1 - b0).astype(float) + ((a1 - b1) - (a0 - b0)).astype(float) * 0.5
        good = ok1 & pv
        out["mo_ticks"][good, k] = d[good] * mo[good] / tk[good]
        gb = good & (absp > 0)
        out["mo_bps"][gb, k] = d[gb] * mo[gb] / absp[gb] * 1e4
        gm = ok1 & ok0
        out["mm_ticks"][gm, k] = d[gm] * mm[gm] / tk[gm]
        gmb = gm & (m0 > 0)
        out["mm_bps"][gmb, k] = d[gmb] * mm[gmb] / m0[gmb] * 1e4
    return out


# ---- statistics --------------------------------------------------------------------------------------------------


def _none(x: float | None) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


class _Boot:
    """Ratio-of-sums statistics on one shared block draw matrix."""

    def __init__(self, counts: np.ndarray) -> None:
        self.counts = counts.astype(float)
        self.b = counts.shape[0]
        self.nb = counts.shape[1]

    def stat(self, num: np.ndarray, den: np.ndarray, blocks: np.ndarray) -> tuple[dict[str, Any], np.ndarray]:
        """The statistic and its replicates (NaN where a replicate's denominator is 0, all NaN when undefined)."""
        if self.nb == 0 or not (den > 0).any():
            return {"value": None, "ci_lo": None, "ci_hi": None, "n": 0}, np.full(self.b, np.nan)
        num = np.where(den > 0, num, 0.0)
        ns = np.bincount(blocks, weights=num, minlength=self.nb)
        ds = np.bincount(blocks, weights=den, minlength=self.nb)
        value = float(ns.sum()) / float(ds.sum())
        rn = self.counts @ ns
        rd = self.counts @ ds
        reps = np.where(rd > 0, rn / np.where(rd > 0, rd, 1.0), np.nan)
        lo, hi = percentile_ci(reps, 0.05)
        return {"value": _none(value), "ci_lo": _none(lo), "ci_hi": _none(hi), "n": int((den > 0).sum())}, reps

    def __call__(self, num: np.ndarray, den: np.ndarray, blocks: np.ndarray) -> dict[str, Any]:
        return self.stat(num, den, blocks)[0]


# ---- evaluation --------------------------------------------------------------------------------------------------


#: per-shadow PnL terms of `PnlTerms.values`, in ticks unless named `_bps` (module docstring, "Per-shadow PnL")
PNL_TERMS = (
    "grant_ticks",
    "deny_ticks",
    "timing_ticks",
    "model_pnl_ticks",
    "exact_pnl_ticks",
    "bias_ticks",
    "bias_bps",
)


@dataclass
class PnlTerms:
    """The per-shadow numerators and denominators behind every model's PnL statistics, for summing across runs
    (`queuetruth.eval.blocks.pnl_block_stats`): shadow arrays over the population (`pop`), and per term an array of
    shape (models, horizons, shadows) that is 0 where the shadow does not count. `ok` (ticks) and `ok_bps` (bps,
    `bias_bps` only) say which shadows count; the model's statistic at a horizon is `sum(term) / sum(ok)` over the
    population, the `value` of the same field in `doc["models"][i]["horizons"]`."""

    models: list[str]
    horizons_ns: list[int]
    instrument_id: np.ndarray
    session_date: np.ndarray
    ts_live: np.ndarray
    pop: np.ndarray
    ok: np.ndarray
    ok_bps: np.ndarray
    values: dict[str, np.ndarray]


@dataclass
class ToxicityResult:
    """Output of `evaluate_toxicity`: the `qt.zoo_toxicity` body (without run_id, spec, files), the tables, and the
    per-shadow PnL terms (not written by `zoo-toxicity`)."""

    doc: dict[str, Any]
    fills: pa.Table
    markouts: pa.Table
    shadows: pa.Table
    warnings: list[str] = field(default_factory=list)
    pnl: PnlTerms | None = None


_ROW_COLS = ("arm", "sidx", "fill_seq", "ts", "px", "px_ok", "qty", "liquidity", "mechanism", "status", "reason")
_ROW_DTYPES = {
    "arm": object,
    "sidx": np.int64,
    "fill_seq": np.int64,
    "ts": np.int64,
    "px": np.int64,
    "px_ok": bool,
    "qty": float,
    "liquidity": object,
    "mechanism": object,
    "status": object,
    "reason": object,
}


@dataclass
class _Rows:
    """Fill rows of all arms, appended as column chunks (one per arm) and concatenated once."""

    chunks: list[dict[str, np.ndarray]] = field(default_factory=list)

    def add(self, **cols: Any) -> None:
        n = np.asarray(cols["sidx"]).size
        out = {}
        for k in _ROW_COLS:
            v = cols[k]
            out[k] = np.asarray(v, dtype=_ROW_DTYPES[k]) if np.ndim(v) else np.full(n, v, dtype=_ROW_DTYPES[k])
        self.chunks.append(out)

    def finish(self) -> dict[str, np.ndarray]:
        if not self.chunks:
            return {k: np.zeros(0, dtype=_ROW_DTYPES[k]) for k in _ROW_COLS}
        return {k: np.concatenate([c[k] for c in self.chunks]) for k in _ROW_COLS}


def check_horizons(horizons_ns: list[int] | tuple[int, ...], rank_horizon_ns: int, window_ns: int) -> list[int]:
    """Sorted unique markout horizons; `QT-CONFIG-001` unless every horizon and the window are positive and the
    rank horizon is one of the horizons."""
    hs = sorted({int(h) for h in horizons_ns})
    if not hs or hs[0] <= 0:
        raise QtError("QT-CONFIG-001", f"horizons_ns: positive durations required, got {list(horizons_ns)}")
    if int(rank_horizon_ns) not in hs:
        raise QtError(
            "QT-CONFIG-001",
            f"rank_horizon_ns {rank_horizon_ns} is not one of horizons_ns {hs}",
            "pass --rank-horizon equal to one of --horizons",
        )
    if window_ns <= 0:
        raise QtError("QT-CONFIG-001", f"window_ns: a positive duration is required, got {window_ns}")
    return hs


def _window_error(window_ns: int, have: list[int]) -> QtError:
    return QtError(
        "QT-CONFIG-001",
        f"window_ns {window_ns} is not a prediction horizon of the zoo run (horizons {have})",
        "pass --window equal to the zoo's --h-max or one of its --horizons",
    )


def evaluate_toxicity(
    predictions: pa.Table,
    planned_life: pa.Table,
    model_fills: pa.Table,
    shadow_results: pa.Table,
    exact_fills: pa.Table,
    trades: pa.Table,
    bbo: pa.Table,
    instruments: pa.Table,
    *,
    window_ns: int,
    horizons_ns: list[int] | tuple[int, ...] = DEFAULT_HORIZONS_NS,
    rank_horizon_ns: int = DEFAULT_RANK_HORIZON_NS,
    bootstrap_b: int = 1000,
    seed: int = 0,
    last_event_ts: dict[tuple[int, int], int] | None = None,
    allow_truth_mismatch: bool = False,
) -> ToxicityResult:
    """Fill-type tags, markouts, per-model decomposition and rankings (module docstring). Inputs: `qt.zoo_predictions`,
    the planned life (`shadow_id`, `ts_cancel_planned`, `ts_admin_end`), `qt.zoo_fills`, the exact replay's
    `qt.shadow_results` and `qt.shadow_fills` of the same shadows, its `qt.trades` and `qt.bbo`, and
    `qt.instruments`; `last_event_ts` maps (instrument_id, session_date days) to the instrument-day's last event.
    `QT-INPUT-002` when the replay does not reproduce the zoo's exact arm on some shadow, unless
    `allow_truth_mismatch` (those shadows are then left out and counted)."""
    window_ns = int(window_ns)
    hs = check_horizons(horizons_ns, rank_horizon_ns, window_ns)
    warnings: list[str] = []
    C.require(
        predictions,
        ["model_id", "shadow_id", "qty", "horizon_ns", "p_fill", "model_ts_first_fill", "truth_filled",
         "truth_ts_first_fill", "instrument_id", "session_date", "ts_live"],
        "zoo_predictions",
    )  # fmt: skip
    model_fills = qa.upgrade(model_fills, "qt.zoo_fills")
    exact_fills = qa.upgrade(exact_fills, "qt.shadow_fills")
    C.require(shadow_results, ["shadow_id", "instrument_id", "side", "price"], "shadow_results")

    n_hbt = len(aborted_shadows(predictions))
    # only the columns read below: the population filter then copies part of the table
    t = common_population(predictions.select([c for c in _PRED_READ if c in predictions.column_names]))
    model = C.strings(t, "model_id")
    sid = C.strings(t, "shadow_id")
    h, _ = C.ints(t, "horizon_ns")
    if sid.size and not (h == window_ns).any():
        raise _window_error(window_ns, sorted({int(x) for x in np.unique(h)}))
    ids, first, inv = np.unique(sid, return_index=True, return_inverse=True)
    order = np.argsort(first, kind="stable")
    ids, first = ids[order], first[order]
    rank_of = np.empty(order.size, dtype=np.int64)
    rank_of[order] = np.arange(order.size)
    inv = rank_of[np.asarray(inv).reshape(-1)]
    n_sh = ids.size
    live_all, _ = C.ints(t, "ts_live")
    date_all, _ = C.ints(t, "session_date")
    iid_all, _ = C.ints(t, "instrument_id")
    qty_all = C.floats(t, "qty")
    live, date, iid, sh_qty = live_all[first], date_all[first], iid_all[first], qty_all[first]
    cancel_rel, admin_rel = _planned_life(planned_life, ids, live) if n_sh else (np.zeros(0), np.zeros(0))
    end_w = live + window_ns

    # exact truth at the window (the zoo's exact arm; the same rows for every model)
    at_w = h == window_ns
    y, yv = C.bools(t, "truth_filled")
    y_ts, y_tsv = C.ints(t, "truth_ts_first_fill")
    has_truth = np.zeros(n_sh, dtype=bool)
    exact_filled = np.zeros(n_sh, dtype=bool)
    exact_first = np.full(n_sh, -1, dtype=np.int64)
    rows_w = np.flatnonzero(at_w & yv)
    has_truth[inv[rows_w]] = True
    hit = rows_w[y[rows_w]]
    exact_filled[inv[hit]] = True
    exact_first[inv[hit]] = np.where(y_tsv[hit], y_ts[hit], -1)

    no_truth = ~has_truth
    cancel_bearing = has_truth & (cancel_rel < float(window_ns))
    no_label = has_truth & ~cancel_bearing & (admin_rel < float(window_ns))
    pop = has_truth & ~cancel_bearing & ~no_label

    # shadow prices, sides and liveness from the exact replay
    r_sid = C.strings(shadow_results, "shadow_id")
    r_pos = {str(s): i for i, s in enumerate(r_sid)}
    pos = np.array([r_pos.get(str(s), -1) for s in ids], dtype=np.int64)
    if (pop & (pos < 0)).any():
        missing = [str(s) for s in ids[pop & (pos < 0)]]
        raise QtError(
            "QT-INPUT-002",
            f"shadow_results: no exact replay row for {len(missing)} zoo shadow(s) (first {missing[:3]})",
            "replay the zoo's own shadow spec (--shadows) on the zoo's dataset",
        )
    r_side, _ = C.ints(shadow_results, "side")
    r_px, r_pxv = C.ints(shadow_results, "price")
    has_r = pos >= 0
    pj = np.clip(pos, 0, None)
    side = np.where(has_r, r_side[pj] if r_side.size else 0, 0)
    price = np.where(has_r, r_px[pj] if r_px.size else 0, 0)
    price_ok = has_r & (r_pxv[pj] if r_pxv.size else False)
    never_live = np.zeros(n_sh, dtype=bool)
    if "end_state" in shadow_results.column_names and r_sid.size:
        r_never = np.array([str(x).startswith("rejected_") for x in C.strings(shadow_results, "end_state")])
        never_live = has_r & r_never[pj]
    sign = np.where(side == ord("B"), 1, -1)
    pos_of = {str(s): i for i, s in enumerate(ids)}

    # exact fill rows within the window
    e_sid = C.strings(exact_fills, "shadow_id")
    e_idx = np.array([pos_of.get(str(s), -1) for s in e_sid], dtype=np.int64)
    e_ts, _ = C.ints(exact_fills, "ts_event")
    e_seq_no, _ = C.ints(exact_fills, "fill_seq")
    e_sequence, _ = C.ints(exact_fills, "sequence")
    e_px, e_pxv = C.ints(exact_fills, "fill_px")
    e_qty = C.floats(exact_fills, "fill_qty")
    e_reason = C.strings(exact_fills, "reason")
    e_liq = C.strings(exact_fills, "liquidity")
    keep = e_idx >= 0
    keep[keep] &= e_ts[keep] <= end_w[e_idx[keep]]

    # a replay that does not reproduce the zoo's exact first fill is not the zoo's exact arm
    rep_first = np.full(n_sh, _NONE_TS, dtype=np.int64)
    np.minimum.at(rep_first, e_idx[keep], e_ts[keep])
    rep_has = rep_first < _NONE_TS
    mismatch = pop & ((rep_has != exact_filled) | (exact_filled & (rep_first != exact_first)))
    n_checked = int(pop.sum())
    if mismatch.any():
        first_bad = str(ids[np.flatnonzero(mismatch)[0]])
        what = (
            f"{int(mismatch.sum())} of {n_checked} shadow(s) whose replayed exact fills within the window differ from "
            f"the zoo's exact arm (first {first_bad})"
        )
        if not allow_truth_mismatch:
            raise QtError(
                "QT-INPUT-002",
                f"truth_mismatch: {what}: the replay is not the zoo's exact arm",
                "pass the zoo's own dataset (--data), shadows (--shadows) and amends (--amends); the replay takes the "
                "zoo's latency, jitter seed and exclusions from its summary. --allow-truth-mismatch evaluates the "
                "other shadows (a selected subset)",
            )
        warnings.append(f"truth_mismatch: {what} are left out")
    pop &= ~mismatch

    # model status per (model, shadow), before the population's final cut
    models = [str(x) for x in _first_order(model)] if model.size else []
    p = C.floats(t, "p_fill")
    m_ts, m_tsv = C.ints(t, "model_ts_first_fill")
    f_model = C.strings(model_fills, "model_id")
    f_sid = C.strings(model_fills, "shadow_id")
    f_idx = np.array([pos_of.get(str(s), -1) for s in f_sid], dtype=np.int64)
    f_ts, _ = C.ints(model_fills, "ts_event")
    f_seq, _ = C.ints(model_fills, "fill_seq")
    f_qty = C.floats(model_fills, "fill_qty")
    f_reason = C.strings(model_fills, "reason")
    f_liq = C.strings(model_fills, "liquidity")
    f_in = f_idx >= 0
    f_in[f_in] &= f_ts[f_in] <= end_w[f_idx[f_in]]
    model_filled: dict[str, np.ndarray] = {}
    inconsistent: dict[str, np.ndarray] = {}
    no_markout: dict[str, np.ndarray] = {}
    ts_median: dict[str, np.ndarray] = {}
    for mname in models:
        idx_m = np.flatnonzero((model == mname) & at_w)
        p_w = np.full(n_sh, np.nan)
        p_w[inv[idx_m]] = p[idx_m]
        if family(mname) in PARTICLE_FAMILIES:
            ts_med = np.full(n_sh, -1, dtype=np.int64)
            ts_med[inv[idx_m]] = np.where(m_tsv[idx_m], m_ts[idx_m], -1)
            filled = np.nan_to_num(p_w, nan=0.0) >= 0.5
            pseudo = filled & (ts_med >= 0) & (ts_med <= end_w)
            bad = filled & ~pseudo
            model_filled[mname], inconsistent[mname], no_markout[mname] = filled, bad, bad
            ts_median[mname] = ts_med
        else:
            has_rows = np.zeros(n_sh, dtype=bool)
            has_rows[f_idx[f_in & (f_model == mname)]] = True
            deterministic = (p_w == 0.0) | (p_w == 1.0)
            model_filled[mname] = has_rows
            inconsistent[mname] = deterministic & ((p_w >= 0.5) != has_rows)
            no_markout[mname] = np.zeros(n_sh, dtype=bool)

    # shadows that never went live and that no arm fills carry no information: left out
    any_filled = exact_filled.copy()
    for mname in models:
        any_filled |= model_filled[mname]
    never_live_out = pop & never_live & ~any_filled
    pop &= ~never_live_out

    status: dict[str, np.ndarray] = {}
    for mname in models:
        filled = model_filled[mname]
        status[mname] = np.where(
            filled & exact_filled,
            "confirmed",
            np.where(filled, "phantom", np.where(exact_filled, "missed", "neither")),
        ).astype(object)
        bad = inconsistent[mname] & pop
        if bad.any():
            if family(mname) in PARTICLE_FAMILIES:
                warnings.append(
                    f"inconsistent: {mname}: {int(bad.sum())} shadow(s) with p_fill >= 0.5 at the window have no "
                    "median first fill in it and no markout"
                )
            else:
                warnings.append(
                    f"inconsistent: {mname}: {int(bad.sum())} shadow(s) whose p_fill of 0 or 1 at the window "
                    "disagrees with its fill rows (the fill rows decide the status)"
                )

    # fill rows of every arm (exact first, then models in order; by shadow, then fill_seq)
    rows = _Rows()
    e_keep = np.flatnonzero(keep & (e_idx >= 0))
    e_keep = e_keep[pop[e_idx[e_keep]]]
    e_keep = e_keep[np.lexsort((e_seq_no[e_keep], e_idx[e_keep]))]
    e_si = e_idx[e_keep]
    rows.add(
        arm=EXACT_ARM,
        sidx=e_si,
        fill_seq=e_seq_no[e_keep],
        ts=e_ts[e_keep],
        px=e_px[e_keep],
        px_ok=e_pxv[e_keep],
        qty=e_qty[e_keep],
        liquidity=e_liq[e_keep],
        mechanism=exact_mechanism(e_reason[e_keep], iid[e_si], e_ts[e_keep], e_sequence[e_keep], trades),
        status=None,
        reason=e_reason[e_keep],
    )
    for mname in models:
        st = status[mname]
        if family(mname) in PARTICLE_FAMILIES:
            ts_med = ts_median[mname]
            si = np.flatnonzero(pop & model_filled[mname] & ~no_markout[mname])
            rows.add(arm=mname, sidx=si, fill_seq=0, ts=ts_med[si], px=price[si], px_ok=price_ok[si],
                     qty=sh_qty[si], liquidity="maker", mechanism="particle", status=_row_status(st[si]),
                     reason="particle")  # fmt: skip
        else:
            sel = np.flatnonzero(f_in & (f_model == mname))
            sel = sel[pop[f_idx[sel]]]
            sel = sel[np.lexsort((f_seq[sel], f_idx[sel]))]
            si = f_idx[sel]
            rows.add(arm=mname, sidx=si, fill_seq=f_seq[sel], ts=f_ts[sel], px=price[si], px_ok=price_ok[si],
                     qty=f_qty[sel], liquidity=f_liq[sel], mechanism=model_mechanism(f_reason[sel]),
                     status=_row_status(st[si]), reason=f_reason[sel])  # fmt: skip
    R = rows.finish()
    arm, sidx, r_ts = R["arm"], R["sidx"], R["ts"]
    r_iid = iid[sidx] if sidx.size else np.zeros(0, dtype=np.int64)
    r_sign = sign[sidx] if sidx.size else np.zeros(0, dtype=np.int64)

    # prices of model taker rows: the prevailing opposite best, capped at the limit
    book = QuoteBook(bbo, instruments, last_event_ts)
    taker = (arm != EXACT_ARM) & (R["liquidity"] == "taker")
    if taker.any():
        dd, _, _ = book.day(r_iid[taker], r_ts[taker])
        bq, aq, okq = book.quote(r_iid[taker], dd, r_ts[taker], strict=False, end_check=False)
        lim = R["px"][taker]
        opp = np.where(r_sign[taker] > 0, np.minimum(aq, lim), np.maximum(bq, lim))
        R["px"][taker] = np.where(okq, opp, lim)
        for a in _first_order(arm[taker][~okq]) if (~okq).any() else []:
            k = int(np.sum(arm[taker][~okq] == a))
            warnings.append(f"taker_price: {a}: {k} taker fill(s) without an opposite quote are priced at the limit")
    mk = mid_markouts(book, r_iid, r_ts, r_sign, R["px"], R["px_ok"], hs)

    # blocks over the population's shadows (the same draws for every statistic)
    pop_idx = np.flatnonzero(pop)
    block = np.zeros(n_sh, dtype=np.int64)
    kind = "30min"
    n_blocks = 0
    if pop_idx.size:
        labels, kind = block_labels(date[pop_idx], live[pop_idx])
        _, bl = np.unique(labels, return_inverse=True)
        block[pop_idx] = np.asarray(bl).reshape(-1)
        n_blocks = int(block[pop_idx].max()) + 1
    boot = _Boot(block_counts(n_blocks, bootstrap_b, bootstrap_seed(seed, BOOTSTRAP_TEST_ID)))

    # first row of each (arm, shadow)
    first_row: dict[str, np.ndarray] = {}
    for a in [EXACT_ARM, *models]:
        fr = np.full(n_sh, -1, dtype=np.int64)
        sel = np.flatnonzero(arm == a)
        sel = sel[np.lexsort((R["fill_seq"][sel], r_ts[sel], sidx[sel]))]
        uniq, at = np.unique(sidx[sel], return_index=True)
        fr[uniq] = sel[at]
        first_row[a] = fr

    model_docs = []
    rank_reps: list[dict[str, np.ndarray]] = []
    shape = (len(models), len(hs), n_sh)
    pnl = PnlTerms(
        models=list(models), horizons_ns=list(hs), instrument_id=iid, session_date=date, ts_live=live, pop=pop.copy(),
        ok=np.zeros(shape, dtype=bool), ok_bps=np.zeros(shape, dtype=bool),
        values={k: np.zeros(shape) for k in PNL_TERMS},
    )  # fmt: skip
    for i, mname in enumerate(models):
        d, reps = _model_doc(
            mname, model_filled, status, inconsistent, no_markout, exact_filled, pop, first_row, mk, hs, block, boot,
            int(rank_horizon_ns), (pnl, i),
        )  # fmt: skip
        model_docs.append(d)
        rank_reps.append(reps)
    by_type = _by_type(R, models, status, pop, block, mk, hs, boot)
    rankings = _rankings(model_docs, rank_reps, int(rank_horizon_ns))
    ex_rows = arm == EXACT_ARM
    mech_counts = {mch: int(np.sum(R["mechanism"][ex_rows] == mch)) for mch in MECHANISMS}
    doc = {
        "n_shadows": int(pop.sum()),
        "n_excluded": {
            "hbt_312": n_hbt,
            "no_exact_arm": int(no_truth.sum()),
            "cancel_bearing": int(cancel_bearing.sum()),
            "no_label": int(no_label.sum()),
            "truth_mismatch": int(mismatch.sum()),
            "never_live": int(never_live_out.sum()),
        },
        "truth_mismatch_rate": float(mismatch.sum()) / n_checked if n_checked else None,
        "window_ns": window_ns,
        "horizons_ns": hs,
        "ci_block": kind,
        "n_blocks": n_blocks,
        "bootstrap_b": int(bootstrap_b),
        "exact": {
            "n_filled": int((pop & exact_filled).sum()),
            "n_fills": int(ex_rows.sum()),
            "fills_by_mechanism": {k: v for k, v in mech_counts.items() if v},
        },
        "models": model_docs,
        "by_type": by_type,
        "rankings": rankings,
    }
    side_u8 = side[sidx] if sidx.size else np.zeros(0, dtype=np.int64)
    fills_t = _build(
        "qt.zoo_toxicity_fills",
        {
            "arm": arm,
            "shadow_id": ids[sidx] if sidx.size else np.zeros(0, dtype=object),
            "fill_seq": R["fill_seq"],
            "instrument_id": r_iid,
            "side": side_u8,
            "ts_event": r_ts,
            "px": pa.array(R["px"], type=pa.int64(), mask=~R["px_ok"]),
            "fill_qty": R["qty"],
            "liquidity": R["liquidity"],
            "mechanism": R["mechanism"],
            "status": R["status"],
            "reason": R["reason"],
        },
    )
    markouts_t = _markouts_table(fills_t, mk, hs)
    shadows_t = _shadows_table(models, status, pop, ids, iid, date, live, first_row, R)
    seen: set[str] = set()
    uniq_warnings = [w for w in warnings if not (w in seen or seen.add(w))]
    return ToxicityResult(
        doc=doc, fills=fills_t, markouts=markouts_t, shadows=shadows_t, warnings=uniq_warnings, pnl=pnl
    )


def _row_status(st: np.ndarray) -> np.ndarray:
    """A model row's status column: the pair's status when it is `confirmed` or `phantom`, else null."""
    out = np.asarray(st, dtype=object).copy()
    out[~np.isin(out, np.array(["confirmed", "phantom"], dtype=object))] = None
    return out


def _first_order(keys: np.ndarray) -> np.ndarray:
    uniq, first = np.unique(keys, return_index=True)
    return uniq[np.argsort(first, kind="stable")]


def _shadow_values(first: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per shadow: the first row's value and whether it is defined (a row exists and the value is finite)."""
    out = np.full(first.size, np.nan)
    has = first >= 0
    out[has] = values[first[has]]
    return out, np.isfinite(out)


def _model_doc(
    mname: str,
    model_filled: dict[str, np.ndarray],
    status: dict[str, np.ndarray],
    inconsistent: dict[str, np.ndarray],
    no_markout: dict[str, np.ndarray],
    exact_filled: np.ndarray,
    pop: np.ndarray,
    first_row: dict[str, np.ndarray],
    mk: dict[str, np.ndarray],
    hs: list[int],
    block: np.ndarray,
    boot: _Boot,
    rank_hz: int,
    terms: tuple[PnlTerms, int] | None = None,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    st = status[mname]
    mf = model_filled[mname] & pop
    ef = exact_filled & pop
    conf, phan, miss = pop & (st == "confirmed"), pop & (st == "phantom"), pop & (st == "missed")
    drop = no_markout[mname]
    n = int(pop.sum())
    horizons: dict[str, Any] = {}
    reps_out: dict[str, np.ndarray] = {}

    def z(x: np.ndarray, m: np.ndarray) -> np.ndarray:
        return np.where(m, np.nan_to_num(x), 0.0)

    for k, hz in enumerate(hs):
        mo_m, dm = _shadow_values(first_row[mname], mk["mo_ticks"][:, k])
        mo_e, de = _shadow_values(first_row[EXACT_ARM], mk["mo_ticks"][:, k])
        bp_m, dbm = _shadow_values(first_row[mname], mk["mo_bps"][:, k])
        bp_e, dbe = _shadow_values(first_row[EXACT_ARM], mk["mo_bps"][:, k])
        mm_m, dmm = _shadow_values(first_row[mname], mk["mm_ticks"][:, k])
        mm_e, dme = _shadow_values(first_row[EXACT_ARM], mk["mm_ticks"][:, k])
        ok = pop & ~drop & (~mf | dm) & (~ef | de)
        okb = pop & ~drop & (~mf | dbm) & (~ef | dbe)
        one, oneb = ok.astype(float), okb.astype(float)
        grant = z(mo_m, phan)
        deny = -z(mo_e, miss)
        timing = z(mo_m, conf) - z(mo_e, conf)
        grant_b, deny_b = z(bp_m, phan), -z(bp_e, miss)
        timing_b = z(bp_m, conf) - z(bp_e, conf)
        okm = pop & ~drop & dmm
        oke = pop & ~drop & dme
        g_s, g_r = boot.stat(grant, one, block)
        d_s, d_r = boot.stat(deny, one, block)
        b_s, b_r = boot.stat(grant + timing + deny, one, block)
        if terms is not None:
            pt, i = terms
            pt.ok[i, k], pt.ok_bps[i, k] = ok, okb
            for name, v, m in (
                ("grant_ticks", grant, ok), ("deny_ticks", deny, ok), ("timing_ticks", timing, ok),
                ("model_pnl_ticks", z(mo_m, mf), ok), ("exact_pnl_ticks", z(mo_e, ef), ok),
                ("bias_ticks", grant + timing + deny, ok), ("bias_bps", grant_b + timing_b + deny_b, okb),
            ):  # fmt: skip
                pt.values[name][i, k] = np.where(m, v, 0.0)
        if hz == rank_hz:
            reps_out = {"grant_ticks": g_r, "deny_ticks": d_r, "bias_ticks": b_r}
        horizons[str(hz)] = {
            "n": int(ok.sum()),
            "model_pnl_ticks": boot(z(mo_m, mf), one, block),
            "exact_pnl_ticks": boot(z(mo_e, ef), one, block),
            "grant_ticks": g_s,
            "deny_ticks": d_s,
            "timing_ticks": boot(timing, one, block),
            "bias_ticks": b_s,
            "grant_bps": boot(grant_b, oneb, block),
            "deny_bps": boot(deny_b, oneb, block),
            "timing_bps": boot(timing_b, oneb, block),
            "bias_bps": boot(grant_b + timing_b + deny_b, oneb, block),
            "phantom_mo_ticks": boot(z(mo_m, phan), (ok & phan).astype(float), block),
            "missed_mo_ticks": boot(z(mo_e, miss), (ok & miss).astype(float), block),
            "confirmed_mo_ticks": boot(z(mo_m, conf), (ok & conf).astype(float), block),
            "confirmed_exact_mo_ticks": boot(z(mo_e, conf), (ok & conf).astype(float), block),
            "phantom_mm_ticks": boot(z(mm_m, phan), (okm & phan).astype(float), block),
            "missed_mm_ticks": boot(z(mm_e, miss), (oke & miss).astype(float), block),
            "confirmed_mm_ticks": boot(z(mm_m, conf), (okm & conf).astype(float), block),
            "confirmed_exact_mm_ticks": boot(z(mm_e, conf), (oke & conf).astype(float), block),
        }
    doc = {
        "model_id": mname,
        "family": family(mname),
        "n_shadows": n,
        "n_model_filled": int(mf.sum()),
        "n_exact_filled": int(ef.sum()),
        "n_confirmed": int(conf.sum()),
        "n_phantom": int(phan.sum()),
        "n_missed": int(miss.sum()),
        "n_inconsistent": int((inconsistent[mname] & pop).sum()),
        "phantom_rate": float(phan.sum()) / n if n else None,
        "missed_rate": float(miss.sum()) / n if n else None,
        "horizons": horizons,
    }
    return doc, reps_out


def _type_entry(
    arm: str,
    status: str | None,
    mech: str,
    sel: np.ndarray,
    sidx: np.ndarray,
    qty: np.ndarray,
    block: np.ndarray,
    mk: dict[str, np.ndarray],
    hs: list[int],
    boot: _Boot,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "arm": arm,
        "status": status,
        "mechanism": mech,
        "n_fills": int(sel.size),
        "n_shadows": int(np.unique(sidx[sel]).size),
        "qty": float(qty[sel].sum()),
        "markouts": {},
    }
    b = block[sidx[sel]]
    for k, hz in enumerate(hs):
        entry = {}
        for name in _MEASURES:
            v = mk[name][sel, k]
            good = np.isfinite(v)
            entry[name] = boot(np.where(good, qty[sel] * np.nan_to_num(v), 0.0), np.where(good, qty[sel], 0.0), b)
        out["markouts"][str(hz)] = entry
    return out


def _by_type(
    R: dict[str, np.ndarray],
    models: list[str],
    status: dict[str, np.ndarray],
    pop: np.ndarray,
    block: np.ndarray,
    mk: dict[str, np.ndarray],
    hs: list[int],
    boot: _Boot,
) -> list[dict[str, Any]]:
    arm, mech, st, qty, sidx = R["arm"], R["mechanism"], R["status"], R["qty"], R["sidx"]
    out: list[dict[str, Any]] = []

    def groups(a: str, s: str | None, base: np.ndarray) -> None:
        if not base.any():
            return
        for m in MECHANISMS:
            sel = np.flatnonzero(base & (mech == m))
            if sel.size:
                out.append(_type_entry(a, s, m, sel, sidx, qty, block, mk, hs, boot))
        out.append(_type_entry(a, s, "all", np.flatnonzero(base), sidx, qty, block, mk, hs, boot))

    exact = arm == EXACT_ARM
    groups(EXACT_ARM, None, exact)
    for mname in models:
        mine = arm == mname
        groups(mname, "confirmed", mine & (st == "confirmed"))
        groups(mname, "phantom", mine & (st == "phantom"))
        missed_sh = pop & (status[mname] == "missed")
        groups(mname, "missed", exact & missed_sh[sidx] if sidx.size else exact)
    return out


def _rankings(model_docs: list[dict[str, Any]], rank_reps: list[dict[str, np.ndarray]], hz: int) -> dict[str, Any]:
    """Each ranking by |value| descending (module docstring), with rank intervals and paired gaps from the shared
    bootstrap replicates."""

    def ranked(metric: str) -> list[dict[str, Any]]:
        stats = [d["horizons"][str(hz)][metric] for d in model_docs]
        n = len(stats)
        if n == 0:
            return []
        mag = np.array([abs(s["value"]) if s["value"] is not None else np.nan for s in stats])
        order = sorted(range(n), key=lambda i: (bool(np.isnan(mag[i])), -mag[i] if np.isfinite(mag[i]) else 0.0, i))
        reps = np.abs(np.stack([r[metric] for r in rank_reps], axis=1))  # (B, models)
        rf = np.where(np.isfinite(reps), reps, -np.inf)
        rank_min = 1 + (rf[:, None, :] > rf[:, :, None]).sum(axis=2)  # competition rank
        rank_max = (rf[:, None, :] >= rf[:, :, None]).sum(axis=2)  # last place of its tie group
        out = []
        for place, i in enumerate(order):
            s = stats[i]
            row: dict[str, Any] = {
                "rank": place + 1,
                "model_id": model_docs[i]["model_id"],
                "value": s["value"],
                "ci_lo": s["ci_lo"],
                "ci_hi": s["ci_hi"],
                "rank_lo": None,
                "rank_hi": None,
                "gap_to_next": None,
                "separated_from_next": None,
            }
            if s["value"] is not None and reps.shape[0]:
                row["rank_lo"] = int(np.quantile(rank_min[:, i], 0.025, method="lower"))
                row["rank_hi"] = int(np.quantile(rank_max[:, i], 0.975, method="higher"))
            if place + 1 < n and s["value"] is not None:
                j = order[place + 1]
                if stats[j]["value"] is not None:
                    gap_reps = reps[:, i] - reps[:, j]
                    lo, hi = percentile_ci(gap_reps, 0.05)
                    row["gap_to_next"] = {
                        "value": float(mag[i] - mag[j]),
                        "ci_lo": _none(lo),
                        "ci_hi": _none(hi),
                    }
                    row["separated_from_next"] = bool(np.isfinite(lo) and lo > 0)
            out.append(row)
        return out

    return {"horizon_ns": hz, **{name: ranked(metric) for name, metric in RANKED}}


def _markouts_table(fills: pa.Table, mk: dict[str, np.ndarray], hs: list[int]) -> pa.Table:
    n, nh = fills.num_rows, len(hs)
    rep = pa.array(np.repeat(np.arange(n, dtype=np.int64), nh))
    cols: dict[str, Any] = {
        "arm": pc.take(fills.column("arm"), rep).combine_chunks(),
        "shadow_id": pc.take(fills.column("shadow_id"), rep).combine_chunks(),
        "fill_seq": pc.take(fills.column("fill_seq"), rep).combine_chunks(),
        "horizon_ns": np.tile(np.array(hs, dtype=np.int64), n),
    }
    for name in _MEASURES:
        cols[name] = mk[name].reshape(-1)
    return _build("qt.zoo_toxicity_markouts", cols)


def _shadows_table(
    models: list[str],
    status: dict[str, np.ndarray],
    pop: np.ndarray,
    ids: np.ndarray,
    iid: np.ndarray,
    date: np.ndarray,
    live: np.ndarray,
    first_row: dict[str, np.ndarray],
    R: dict[str, np.ndarray],
) -> pa.Table:
    sh = np.flatnonzero(pop)
    k = len(models)
    ef = first_row[EXACT_ARM][sh]

    def pick(fr: np.ndarray, values: np.ndarray, null: Any) -> np.ndarray:
        out = np.full(fr.size, null, dtype=object)
        has = fr >= 0
        out[has] = values[fr[has]]
        return out

    model_fr = [first_row[m][sh] for m in models]
    return _build(
        "qt.zoo_toxicity_shadows",
        {
            "model_id": np.repeat(np.array(models, dtype=object), sh.size),
            "shadow_id": np.tile(ids[sh], k),
            "instrument_id": np.tile(iid[sh], k),
            "session_date": np.tile(date[sh], k),
            "ts_live": np.tile(live[sh], k),
            "status": np.concatenate([status[m][sh] for m in models]) if k else np.zeros(0, dtype=object),
            "model_ts_first_fill": np.concatenate([pick(fr, R["ts"], None) for fr in model_fr])
            if k
            else np.zeros(0, dtype=object),
            "exact_ts_first_fill": np.tile(pick(ef, R["ts"], None), k),
            "model_mechanism": np.concatenate([pick(fr, R["mechanism"], None) for fr in model_fr])
            if k
            else np.zeros(0, dtype=object),
            "exact_mechanism": np.tile(pick(ef, R["mechanism"], None), k),
        },
    )


# ---- human view --------------------------------------------------------------------------------------------------


def _fmt(v: float | None, digits: int = 3) -> str:
    return "-" if v is None else f"{v:+.{digits}f}"


def _star(s: dict[str, Any] | None) -> str:
    if not s or s.get("ci_lo") is None or s.get("ci_hi") is None:
        return " "
    return "*" if s["ci_lo"] > 0 or s["ci_hi"] < 0 else " "


def _cell(s: dict[str, Any] | None, digits: int = 3) -> str:
    return (_fmt(s.get("value"), digits) if s else "-") + _star(s)


def _ci(s: dict[str, Any] | None, digits: int = 3) -> str:
    if not s or s.get("value") is None:
        return "-"
    return f"{_fmt(s['value'], digits)} [{_fmt(s.get('ci_lo'), digits)}, {_fmt(s.get('ci_hi'), digits)}]"


def _text_table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths, strict=True))).rstrip()
        for r in [header, *rows]
    ]


def render_toxicity(doc: dict[str, Any]) -> str | None:
    """Answer-first terminal view of a `qt.zoo_toxicity` document: each model's PnL error at the rank horizon (ranked
    by |bias|), the exact arm's markout per fill mechanism, and the answer in plain words."""
    from queuetruth.api import fmt_dur

    models = doc.get("models")
    rk = doc.get("rankings") or {}
    if not isinstance(models, list) or "horizon_ns" not in rk:
        return None
    hz = int(rk["horizon_ns"])
    key = str(hz)
    by_id = {m["model_id"]: m for m in models}
    horizons = [int(h) for h in doc.get("horizons_ns") or []]
    lines = [
        f"zoo-toxicity: {doc.get('n_shadows')} orders, {len(models)} models (zoo run {doc.get('zoo_run_id')}, "
        f"{doc.get('zoo_mode')} mode; run {doc.get('run_id')})",
        f"fill window {fmt_dur(doc.get('window_ns'))} from ts_live; rankings at the {fmt_dur(hz)} markout; 95% CIs "
        f"from {doc.get('n_blocks')} {doc.get('ci_block')} blocks, {doc.get('bootstrap_b')} draws",
    ]
    excl = {k: v for k, v in (doc.get("n_excluded") or {}).items() if v}
    if excl:
        lines.append("left out: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in excl.items()))
    ex = doc.get("exact") or {}
    lines += ["", f"exact arm: {ex.get('n_filled')} of {doc.get('n_shadows')} orders filled, {ex.get('n_fills')} "
              "fills; quantity-weighted mean markout from the fill price, ticks (* = 95% CI excludes 0):"]  # fmt: skip
    rows = []
    for e in doc.get("by_type") or []:
        if e.get("arm") != EXACT_ARM:
            continue
        mo = e.get("markouts") or {}
        rows.append([str(e["mechanism"]), str(e["n_fills"]), *(_cell((mo.get(str(h)) or {}).get("mo_ticks"), 2)
                                                              for h in horizons)])  # fmt: skip
    lines += ["  " + x for x in _text_table(["mechanism", "fills", *(fmt_dur(h) for h in horizons)], rows)]
    lines += ["", f"each model's PnL error at the {fmt_dur(hz)} markout, ticks per order (positive = the model "
              "overstates PnL; * = 95% CI excludes 0), largest |bias| first:"]  # fmt: skip
    rows = []
    for r in rk.get("bias") or []:
        m = by_id.get(r["model_id"])
        if m is None:
            continue
        x = (m.get("horizons") or {}).get(key) or {}
        rows.append([
            str(m["model_id"]), str(m["n_phantom"]), str(m["n_missed"]), _cell(x.get("grant_ticks")),
            _cell(x.get("deny_ticks")), _cell(x.get("timing_ticks")), _cell(x.get("bias_ticks")),
            "-" if r.get("rank_lo") is None else f"{r['rank_lo']}-{r['rank_hi']}",
        ])  # fmt: skip
    lines += ["  " + x for x in _text_table(
        ["model", "phantom", "missed", "grant", "deny", "timing", "bias", "rank range"], rows)]  # fmt: skip
    lines += ["", "in plain words:"]
    exact_pnl = None
    for m in models:
        exact_pnl = ((m.get("horizons") or {}).get(key) or {}).get("exact_pnl_ticks", {}).get("value")
        if exact_pnl is not None:
            break
    if exact_pnl is not None:
        lines.append(f"  The exact arm earns {_fmt(exact_pnl)} ticks per order {fmt_dur(hz)} after its fills.")
    for name, what in (("bias", "misstates PnL the most"), ("grant", "gains the most from fills it wrongly grants"),
                       ("deny", "loses the most PnL to fills it wrongly denies")):  # fmt: skip
        top = (rk.get(name) or [None])[0]
        if not top or top.get("value") is None or top["value"] == 0:
            continue
        m = by_id.get(top["model_id"], {})
        x = (m.get("horizons") or {}).get(key) or {}
        term = {"bias": "bias", "grant": "grant", "deny": "deny"}[name]
        extra = ""
        if name == "grant" and (x.get("phantom_mo_ticks") or {}).get("value") is not None:
            extra = f"; its {m.get('n_phantom')} phantom fills average {_fmt(x['phantom_mo_ticks']['value'], 2)} ticks"
        if name == "deny" and (x.get("missed_mo_ticks") or {}).get("value") is not None:
            extra = f"; its {m.get('n_missed')} missed fills average {_fmt(x['missed_mo_ticks']['value'], 2)} ticks"
        sep = "" if top.get("separated_from_next") else (
            f" (not separated from {rk[name][1]['model_id']} at 95%)" if len(rk[name]) > 1 else "")  # fmt: skip
        ci = _ci({"value": top["value"], "ci_lo": top["ci_lo"], "ci_hi": top["ci_hi"]})
        lines.append(f"  {top['model_id']} {what}: {term} {ci} ticks per order{extra}{sep}.")
    fq = next((m for m in models if m.get("family") == "front_of_queue"), None)
    tt = next((m for m in models if m.get("family") == "trade_through_only"), None)
    if fq and tt:
        b_fq = ((fq.get("horizons") or {}).get(key) or {}).get("bias_ticks")
        b_tt = ((tt.get("horizons") or {}).get(key) or {}).get("bias_ticks")
        lines.append(f"  The bounds: {fq['model_id']} {_ci(b_fq)} and {tt['model_id']} {_ci(b_tt)} ticks per order.")
    bad = [f"{m['model_id']} ({m['n_inconsistent']})" for m in models if m.get("n_inconsistent")]
    if bad:
        lines.append("  Forecasts that disagree with their fill rows (n_inconsistent): " + ", ".join(bad) + ".")
    if (doc.get("n_excluded") or {}).get("truth_mismatch"):
        lines.append(f"  {doc['n_excluded']['truth_mismatch']} orders whose replay did not reproduce the zoo's exact "
                     "arm are left out (--allow-truth-mismatch): the rest are a selected subset.")  # fmt: skip
    files = doc.get("files") or []
    if files:
        lines += ["", "files: summary.json, " + ", ".join(str(f.get("name")) for f in files)]
    return "\n".join(lines)


# ---- runner ------------------------------------------------------------------------------------------------------


def _read(path: Path, what: str, hint: str | None = None) -> pa.Table:
    if not path.exists():
        raise QtError("QT-INPUT-001", f"{path}: missing ({what})", hint)
    return pq.read_table(path)


def _manifest(dataset: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _last_event_ts(manifest: dict[str, Any]) -> dict[tuple[int, int], int]:
    """(instrument_id, session_date days) -> `ts_max` of the dataset manifest's partitions."""
    out: dict[tuple[int, int], int] = {}
    for part in manifest.get("partitions") or []:
        if part.get("ts_max") is None:
            continue
        key = (int(part["instrument_id"]), int(part["session_date"]))
        out[key] = max(out.get(key, int(part["ts_max"])), int(part["ts_max"]))
    return out


def _resolve_input(path: str | None, what: str, flag: str) -> str:
    if path is None:
        raise QtError("QT-INPUT-001", f"{what}: not recorded in the zoo summary", f"pass {flag}")
    if not Path(path).exists():
        raise QtError(
            "QT-INPUT-001",
            f"{what} {path}: not found (the zoo summary records it relative to the zoo's working directory)",
            f"pass {flag}",
        )
    return path


def _zoo_dataset_id(zds: Any) -> str | None:
    """`dataset_id` of the zoo's dataset reference when it can be read (a dataset id, or a path with a manifest)."""
    if not isinstance(zds, dict):
        return None
    if zds.get("dataset_id"):
        return str(zds["dataset_id"])
    if zds.get("path"):
        man = _manifest(Path(str(zds["path"])))
        if man and man.get("dataset_id"):
            return str(man["dataset_id"])
    return None


_ZOO_INPUTS = ("summary.json", "zoo_predictions.parquet", "zoo_fills.parquet", "zoo_shadows.parquet")


def run_zoo_toxicity(spec: ToxicitySpec | dict[str, Any]) -> Any:
    """`zoo-toxicity`: replays the zoo's shadows exactly on the dataset (fill rows, trades, BBO), tags every fill,
    computes the markouts and ranks the models; writes `summary.json` (`qt.zoo_toxicity`),
    `zoo_toxicity_fills.parquet`, `zoo_toxicity_markouts.parquet` and `zoo_toxicity_shadows.parquet` to `out` (when
    set) and returns the `qt.zoo_toxicity` document. Every input and configuration check runs before the replay."""
    from queuetruth import api
    from queuetruth.schemas import models as m
    from queuetruth.schemas.io import sha256_file, write_table
    from queuetruth.schemas.runid import run_id, spec_echo
    from queuetruth.score._io import resolve_ref

    sp = api._validate(ToxicitySpec, spec)
    zoo = Path(sp.zoo.path)
    dataset = Path(sp.dataset.path)
    if not zoo.is_dir():
        raise QtError("QT-INPUT-001", f"{zoo}: not a zoo output directory")
    try:
        zsum = json.loads((zoo / "summary.json").read_text(encoding="utf-8"))
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{zoo}/summary.json: {e}") from None
    except ValueError as e:
        raise QtError("QT-INPUT-002", f"{zoo}/summary.json: invalid JSON: {e}") from None
    if not isinstance(zsum, dict) or zsum.get("schema") != "qt.zoo_summary":
        raise QtError("QT-INPUT-002", f"{zoo}/summary.json: not a qt.zoo_summary document")
    zspec = zsum.get("spec") or {}
    if not (dataset / "manifest.json").is_file():
        raise QtError("QT-INPUT-001", f"{dataset}: no dataset here (manifest.json not found)", "pass the zoo's --data")
    manifest = _manifest(dataset)
    if manifest is None:
        raise QtError("QT-INPUT-002", f"{dataset}/manifest.json: not a readable dataset manifest")
    if manifest.get("kind") != "mbo":
        raise QtError(
            "QT-INPUT-002",
            f"{dataset}: an MBO dataset is required (the exact arm's fill rows come from an order-level replay)",
        )
    want_id, have_id = _zoo_dataset_id(zspec.get("dataset")), manifest.get("dataset_id")
    if want_id and have_id and str(want_id) != str(have_id):
        raise QtError(
            "QT-INPUT-002",
            f"{dataset}: dataset_id {str(have_id)[:16]} is not the dataset the zoo ran on ({str(want_id)[:16]})",
            "pass the zoo's own dataset as --data",
        )

    # configuration checks before the replay
    h_max = zspec.get("h_max_ns")
    window = int(sp.window_ns if sp.window_ns is not None else (h_max if h_max is not None else 300_000_000_000))
    check_horizons(sp.horizons_ns, sp.rank_horizon_ns, window)
    if isinstance(zspec.get("horizons_ns"), list) and h_max is not None:
        zoo_h = sorted({int(x) for x in zspec["horizons_ns"] if int(x) <= int(h_max)} | {int(h_max)})
        if window not in zoo_h:
            raise _window_error(window, zoo_h)

    preds = _read(zoo / "zoo_predictions.parquet", "qt.zoo_predictions")
    fills = _read(
        zoo / "zoo_fills.parquet", "qt.zoo_fills", "rerun the zoo without --no-fills: the model fill rows are needed"
    )
    planned, notes = api._planned_life(zoo)
    shadows = sp.shadows.path if sp.shadows else _resolve_input(
        (zspec.get("shadows") or {}).get("path"), "zoo shadows", "--shadows"
    )  # fmt: skip
    if sp.amends is not None:
        amends: str | None = sp.amends.path
        if not Path(amends).exists():
            raise QtError("QT-INPUT-001", f"amends {amends}: not found")
    elif zspec.get("amends") is not None:
        amends = _resolve_input((zspec.get("amends") or {}).get("path"), "zoo amends", "--amends")
    else:
        amends = None
    with tempfile.TemporaryDirectory(prefix="qt-toxicity-") as tmp:
        replay_spec = {
            "dataset": {"path": str(dataset)},
            "shadows": {"path": shadows},
            "amends": None if amends is None else {"path": amends},
            "latency": zspec.get("latency") or {},
            "exclude_order_ids": zspec.get("exclude_order_ids") or [],
            "seed": int(zspec.get("seed", 0)),
            "bbo": "change",
            "trajectory": "none",
            "threads": sp.threads,
            "out": tmp,
        }
        api.replay(replay_spec)
        out_tmp = Path(tmp)
        res = evaluate_toxicity(
            preds,
            planned,
            fills,
            pq.read_table(out_tmp / "shadow_results.parquet"),
            pq.read_table(out_tmp / "shadow_fills.parquet"),
            pq.read_table(out_tmp / "trades.parquet"),
            pq.read_table(out_tmp / "bbo.parquet"),
            pq.read_table(dataset / "instruments.parquet"),
            window_ns=window,
            horizons_ns=sp.horizons_ns,
            rank_horizon_ns=sp.rank_horizon_ns,
            bootstrap_b=sp.bootstrap_b,
            seed=sp.seed,
            last_event_ts=_last_event_ts(manifest),
            allow_truth_mismatch=sp.allow_truth_mismatch,
        )
    dumped = sp.model_dump(mode="json")
    dumped["window_ns"] = window  # the default resolved: an explicit window equal to h_max is the same run
    zoo_key = json.dumps({"path": sp.zoo.path}, sort_keys=True)

    def resolve(ref: dict[str, Any]) -> str:
        if json.dumps(ref, sort_keys=True) == zoo_key:
            hsh = hashlib.sha256()
            for name in _ZOO_INPUTS:
                hsh.update(f"{name}\n{sha256_file(zoo / name)}\n".encode())
            return hsh.hexdigest()
        return resolve_ref(ref)

    files: list[dict[str, Any]] = []
    if sp.out:
        out = Path(sp.out)
        out.mkdir(parents=True, exist_ok=True)
        files.append(write_table(res.fills, out / "zoo_toxicity_fills.parquet"))
        files.append(write_table(res.markouts, out / "zoo_toxicity_markouts.parquet"))
        files.append(write_table(res.shadows, out / "zoo_toxicity_shadows.parquet"))
    doc = m.ZooToxicity.model_validate(
        {
            "schema": "qt.zoo_toxicity",
            "schema_version": 1,
            "run_id": run_id(dumped, qa.ENGINE_VERSION, resolve),
            "engine_version": qa.ENGINE_VERSION,
            "spec": spec_echo(dumped),
            "zoo_run_id": zsum.get("run_id"),
            "zoo_mode": zspec.get("mode"),
            **res.doc,
            "files": files,
            "warnings": [*notes, *res.warnings],
        }
    )
    if sp.out:
        text = json.dumps(doc.model_dump(mode="json", by_alias=True), indent=2, ensure_ascii=False, allow_nan=False)
        (Path(sp.out) / "summary.json").write_text(text + "\n", encoding="utf-8")
    return doc
