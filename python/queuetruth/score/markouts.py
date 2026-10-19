"""Markouts and position series (SPEC 5.7.5, oracle O-SCORE-3).

`d = +1` buy, `-1` sell; `P` fill price; `M(t)` = reference price of the prevailing quote: the last `qt.bbo` row of
the fill's instrument-day with `ts_event <= t` (rows at equal timestamps: the last in file order), null if there is
none, if a side is empty, or if `t` is beyond the instrument-day's last event (`t > last_event_ts`; equality is
valid). With `past_end = "last_quote"` (`ScorecardSpec.markout_past_end`, schema 1.1) a horizon past the end of data
uses the last quote at or before the instrument-day's last event instead of null.
`mid = (bid + ask) / 2`; `microprice = (Qb Pa + Qa Pb) / (Qb + Qa)` (null when both sizes are 0).
`mo_ticks = d (M(t+h) - P) / tick`; `mo_ccy = d (M(t+h) - P) 10^price_exp qty 10^size_exp multiplier`;
`mm_ticks = d (M(t+h) - M(t-)) / tick` with `M(t-)` from the last row strictly before `t` (no end-of-data check).
Prices are differenced as integers before any float operation, so ticks are exact for half-tick mids.
Optional per-row extras (review F10; no `qt.score_markouts` column yet): `mo_bps = d (M(t+h) - P) / |P| 10^4` and
`mo_net_ccy = mo_ccy - fee`, with the fill's `fee` (positive = cost, negative = rebate) when its `fee_ccy` is null
or equals the instrument's `currency` (case-insensitive), else null.

The instrument-day of a timestamp is the `qt.instruments` row of that instrument with the latest
`midnight_utc_ns <= t`. The fills table is `qt.score_fills` plus the columns `side` (`buy`/`sell`, `B`/`A` or the
ASCII byte) and `instrument_id` (optional when `instruments` holds one instrument).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from queuetruth.score._io import config_error, input_format, make_table, rows

REFS = ("mid", "microprice")
PAST_END = (None, "last_quote")


def side_sign(v: object) -> int:
    """+1 for buy/bid, -1 for sell/ask."""
    if isinstance(v, (int, np.integer)):
        v = chr(int(v))
    s = str(v).strip().lower()
    if s in ("buy", "b", "bid", "+1", "1"):
        return 1
    if s in ("sell", "a", "ask", "s", "-1"):
        return -1
    raise input_format("fills.side", f"unknown side {v!r}")


@dataclass
class _Inst:
    instrument_id: int
    session_date: int
    midnight: int
    tick: int
    price_exp: int
    size_exp: int
    mult: float
    currency: str | None = None


class InstrumentDays:
    """Instrument-day lookup from `qt.instruments` rows."""

    def __init__(self, instruments: pa.Table) -> None:
        self._by: dict[int, list[_Inst]] = {}
        for r in rows(instruments):
            inst = _Inst(
                instrument_id=int(r["instrument_id"]),
                session_date=int(r.get("session_date") or 0),
                midnight=int(r.get("midnight_utc_ns") or 0),
                tick=int(r["tick_size"]),
                price_exp=int(r.get("price_exp", -9)),
                size_exp=int(r.get("size_exp", 0) or 0),
                mult=float(r.get("contract_multiplier", 1.0) or 1.0),
                currency=None if r.get("currency") is None else str(r["currency"]),
            )
            self._by.setdefault(inst.instrument_id, []).append(inst)
        for v in self._by.values():
            v.sort(key=lambda i: i.midnight)
        self._mid = {k: [i.midnight for i in v] for k, v in self._by.items()}

    def only_id(self) -> int | None:
        """The instrument id when exactly one instrument is listed."""
        return next(iter(self._by)) if len(self._by) == 1 else None

    def lookup(self, instrument_id: int, ts: int) -> _Inst | None:
        """Row of the instrument-day containing `ts` (latest midnight <= ts; the first row if ts precedes all)."""
        v = self._by.get(instrument_id)
        if not v:
            return None
        i = bisect.bisect_right(self._mid[instrument_id], ts) - 1
        return v[max(i, 0)]


@dataclass
class _Quotes:
    ts: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bsz: np.ndarray
    asz: np.ndarray
    ok: np.ndarray


def _quotes(bbo: pa.Table, days: InstrumentDays, default_id: int | None) -> dict[tuple[int, int], _Quotes]:
    groups: dict[tuple[int, int], list[tuple[int, int, int | None, int, int | None, int]]] = {}
    for i, r in enumerate(rows(bbo)):
        iid = r.get("instrument_id", default_id)
        if iid is None:
            raise input_format("bbo", "instrument_id column required with several instruments")
        ts = int(r["ts_event"])
        inst = days.lookup(int(iid), ts)
        if inst is None:
            continue
        key = (int(iid), inst.session_date)
        groups.setdefault(key, []).append(
            (ts, i, r.get("bid_px"), int(r.get("bid_sz") or 0), r.get("ask_px"), int(r.get("ask_sz") or 0))
        )
    out: dict[tuple[int, int], _Quotes] = {}
    for key, g in groups.items():
        g.sort(key=lambda x: (x[0], x[1]))
        ok = np.array([b is not None and a is not None for _, _, b, _, a, _ in g], dtype=bool)
        out[key] = _Quotes(
            ts=np.array([x[0] for x in g], dtype=np.int64),
            bid=np.array([0 if x[2] is None else int(x[2]) for x in g], dtype=np.int64),
            ask=np.array([0 if x[4] is None else int(x[4]) for x in g], dtype=np.int64),
            bsz=np.array([x[3] for x in g], dtype=np.float64),
            asz=np.array([x[5] for x in g], dtype=np.float64),
            ok=ok,
        )
    return out


def _ref_minus(q: _Quotes, i: int, ref: str, base: int) -> float | None:
    """`M - base` at quote row `i` (None if undefined); integer differences first."""
    if i < 0 or not q.ok[i]:
        return None
    b, a = int(q.bid[i]), int(q.ask[i])
    if ref == "mid":
        return float(b - base) + float(a - b) * 0.5
    tot = q.bsz[i] + q.asz[i]
    if tot <= 0:
        return None
    return float(b - base) + float(a - b) * (q.bsz[i] / tot)


def compute_markouts(
    fills: pa.Table,
    bbo: pa.Table,
    instruments: pa.Table,
    horizons_ns: Sequence[int],
    refs: Sequence[str],
    *,
    last_event_ts: Mapping[tuple[int, int], int] | None = None,
    past_end: str | None = None,
    extras: dict[str, list[float | None]] | None = None,
) -> pa.Table:
    """`qt.score_markouts` rows in the 1.1 layout (with `mo_bps`, `mo_net_ccy`): one per fill x horizon x
    reference (fill order, then horizons, then refs).

    `last_event_ts` maps (instrument_id, session_date days) to the instrument-day's last event time; the default is
    the last BBO row of that instrument-day. `past_end`: null = a horizon past that time has no markout,
    `"last_quote"` = it uses the last quote at or before it. Fills may carry `fee` and `fee_ccy` (net of fees,
    module docstring). When `extras` is a dict, its lists `mo_bps` and `mo_net_ccy` also receive the column values."""
    for r in refs:
        if r not in REFS:
            raise input_format("refs", f"unknown markout reference {r!r}")
    if past_end not in PAST_END:
        raise config_error(f"markout_past_end: unknown value {past_end!r} (null or 'last_quote')")
    if extras is not None:
        extras.setdefault("mo_bps", [])
        extras.setdefault("mo_net_ccy", [])
    days = InstrumentDays(instruments)
    default_id = days.only_id()
    quotes = _quotes(bbo, days, default_id)
    last = dict(last_event_ts or {})
    for key, q in quotes.items():
        last.setdefault(key, int(q.ts[-1]))
    out: dict[str, list] = {
        k: []
        for k in (
            "order_key",
            "arm",
            "fill_seq",
            "horizon_ns",
            "ref",
            "mo_ticks",
            "mo_ccy",
            "mm_ticks",
            "mo_bps",
            "mo_net_ccy",
        )
    }
    for f in rows(fills):
        iid = f.get("instrument_id", default_id)
        if iid is None:
            raise input_format("fills", "instrument_id column required with several instruments")
        t = int(f["ts"])
        d = side_sign(f["side"])
        p = int(f["px"])
        qty = int(f["qty"])
        fee = f.get("fee")
        fee_ccy = f.get("fee_ccy")
        inst = days.lookup(int(iid), t)
        q = None if inst is None else quotes.get((int(iid), inst.session_date))
        end = None if inst is None else last.get((int(iid), inst.session_date))
        pre_idx = -1 if q is None else int(np.searchsorted(q.ts, t, side="left")) - 1
        same_ccy = (
            inst is not None
            and fee is not None
            and (fee_ccy is None or inst.currency is None or str(fee_ccy).upper() == inst.currency.upper())
        )
        for h in horizons_ns:
            th = t + int(h)
            idx = -1
            if q is not None and end is not None:
                if th <= end:
                    idx = int(np.searchsorted(q.ts, th, side="right")) - 1
                elif past_end == "last_quote":
                    idx = int(np.searchsorted(q.ts, end, side="right")) - 1
            for ref in refs:
                mo = mo_ccy = mm = bps = net = None
                if q is not None and inst is not None:
                    m_rel = _ref_minus(q, idx, ref, p)
                    if m_rel is not None:
                        mo = d * m_rel / inst.tick
                        mo_ccy = d * m_rel * 10.0**inst.price_exp * qty * 10.0**inst.size_exp * inst.mult
                        bps = d * m_rel / abs(p) * 1e4 if p != 0 else None
                        net = mo_ccy - float(fee) if same_ccy else None  # type: ignore[arg-type]
                        pre_rel = _ref_minus(q, pre_idx, ref, p)
                        if pre_rel is not None:
                            mm = d * (m_rel - pre_rel) / inst.tick
                if extras is not None:
                    extras["mo_bps"].append(bps)
                    extras["mo_net_ccy"].append(net)
                out["order_key"].append(f["order_key"])
                out["arm"].append(f["arm"])
                out["fill_seq"].append(int(f["fill_seq"]))
                out["horizon_ns"].append(int(h))
                out["ref"].append(ref)
                out["mo_ticks"].append(mo)
                out["mo_ccy"].append(mo_ccy)
                out["mm_ticks"].append(mm)
                out["mo_bps"].append(bps)
                out["mo_net_ccy"].append(net)
    return make_table("qt.score_markouts", out, full=True)


def _notional(f: Mapping[str, object], inst: _Inst) -> float:
    return abs(int(f["px"])) * 10.0**inst.price_exp * int(f["qty"]) * 10.0**inst.size_exp * inst.mult  # type: ignore[call-overload]


def apply_fee_rates(fills: list[dict], instruments: pa.Table) -> list[str]:
    """Fees for net-of-fee markouts (review F10), in place on score-fill rows (`arm`, `instrument_id`, `ts`, `px`,
    `qty`, `liquidity`, `fee`, `fee_ccy`): the live fee rate per instrument and liquidity (taker vs maker/unknown) =
    sum of live fees / live notional over live fills whose fee is in the instrument's currency (or has no currency);
    each sim fill gets `fee = rate x notional` in that currency (null without a live rate). Live fees in another
    currency are not netted. Returns warning lines."""
    days = InstrumentDays(instruments)
    default_id = days.only_id()
    acc: dict[tuple[int, bool], list[float]] = {}
    foreign: dict[str, int] = {}
    for f in fills:
        if f.get("arm") != "live" or f.get("fee") is None:
            continue
        iid = f.get("instrument_id", default_id)
        inst = None if iid is None else days.lookup(int(iid), int(f["ts"]))
        if inst is None:
            continue
        ccy = f.get("fee_ccy")
        if ccy is not None and inst.currency is not None and str(ccy).upper() != inst.currency.upper():
            foreign[str(ccy)] = foreign.get(str(ccy), 0) + 1
            continue
        a = acc.setdefault((inst.instrument_id, f.get("liquidity") == "taker"), [0.0, 0.0])
        a[0] += float(f["fee"])
        a[1] += _notional(f, inst)
    for f in fills:
        if f.get("arm") != "sim":
            continue
        iid = f.get("instrument_id", default_id)
        inst = None if iid is None else days.lookup(int(iid), int(f["ts"]))
        a = None if inst is None else acc.get((inst.instrument_id, f.get("liquidity") == "taker"))
        if inst is None or a is None or a[1] <= 0:
            f["fee"], f["fee_ccy"] = None, None
            continue
        f["fee"] = a[0] / a[1] * _notional(f, inst)
        f["fee_ccy"] = inst.currency
    return [
        f"fees: {n} live fill(s) charged in {c}, not the instrument currency; not netted in net-of-fee markouts"
        for c, n in sorted(foreign.items())
    ]


def position_series(fills: pa.Table) -> pa.Table:
    """`qt.score_position`: per arm (live, then sim) and instrument, the cumulative signed filled qty after each
    distinct fill timestamp (fills at one timestamp collapse into one row)."""
    acc: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    order = {"live": 0, "sim": 1}
    for i, f in enumerate(rows(fills)):
        iid = f.get("instrument_id")
        if iid is None:
            raise input_format("fills", "instrument_id column required for the position series")
        arm = str(f["arm"])
        if arm not in order:
            raise input_format("fills.arm", f"unknown arm {arm!r}")
        acc.setdefault((order.get(arm, 2), int(iid)), []).append(
            (int(f["ts"]), i, side_sign(f["side"]) * int(f["qty"]))
        )
    out: dict[str, list] = {"arm": [], "instrument_id": [], "ts": [], "position": []}
    names = {0: "live", 1: "sim"}
    for a, iid in sorted(acc):
        pos = 0
        seq = sorted(acc[(a, iid)])
        for j, (ts, _, dq) in enumerate(seq):
            pos += dq
            if j + 1 < len(seq) and seq[j + 1][0] == ts:
                continue
            out["arm"].append(names[a])
            out["instrument_id"].append(iid)
            out["ts"].append(ts)
            out["position"].append(pos)
    return make_table("qt.score_position", out)
