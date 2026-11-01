"""Shared code of the backtester importers that have no live arm of their own (Backtrader, VectorBT): one simulated
order model, the canonical live log as the live arm, the pairing of the two, the simulator log and the
reconciliation (`queuetruth.live.importers.freqtrade` holds the explanation code shared with the bot importers).

A backtest written by `queuetruth.live.importers.backtrader` or `queuetruth.live.importers.vectorbt` becomes a
`SimRun` of `SimOrder`s (exact decimals, ns times); the importers turn it into a canonical log of venue
`sim:<kind>` (`source = sim`, `validation_status = simulated`):

- `submit_sent` at the order's submission time when the export has one (`ts_local_send`), `ack` at its placement
  time (`ts_exchange`, kind `record_time`), one `fill` per execution (`ts_exchange`, kind `record_time`, fee in
  the quote currency, liquidity `taker` for Backtrader market and stop orders, else `unknown`; the scorer counts
  maker fills only, so an `unknown` fill counts and a `taker` one does not), and the terminal event
  (`cancel_ack`, `expire`, `reject`; `ts_exchange`) with the library's status in `reason`. Precisions are those of
  the timestamp text (`s` for `2024-03-01 10:00:00`, `ms` for epoch milliseconds).
- Prices and sizes are doubles in both libraries: read as decimals rounded to 15 significant digits, then rounded
  half-even to the instrument's exponents (counted in a warning), as for a Freqtrade backtest.
- Option `fill_delay_bars` (needs `timeframe`) moves every fill time `k` bars later: a fill stamped with the label
  of the bar whose close price it used, on bars labelled by their open time, happened `1` bar later.

The live arm is a canonical live-log directory (`queuetruth live import` of the venue's or broker's records, any
venue). Pairing (`pair_sim_live`): a simulator order whose `key_hint` (a canonical order key the user stored on
the simulated order, Backtrader `order.info["order_key"]`) is a live order key pairs with that order. Every other
simulator order, in order of its reference time `r` (submission, else placement, else first fill), pairs with the
unpaired live order of the same instrument and side placed nearest to `r` within `pair_window_ms` (default two bars of
`timeframe`, else 60 s), ties by the smaller quantity difference, then the earlier placement and key (live orders
bucketed by (instrument, side) and sorted by placement: O((L + S) log L)). A paired simulator order takes the live
`order_key`, and a fill stamped before the live order's placement (the bar's time) is clamped to that placement
with the bar time kept in the fill's `reason` (counted), as for a Freqtrade backtest; others keep
`sim:<kind>:<account>:<ref>`.

`reconcile_backtest` writes `<out>/live` (the live log, instrument ids completed), `<out>/sim`, `<out>/score` and
`reconcile.json` / `reconcile_orders.csv` (`qt.bot_reconcile`) as `reconcile_freqtrade` does.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import Record, input_error, make_order_key, rfc3339_ns, utc_date_days, valid_id
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.imap import InstrumentEntry, InstrumentMap, load_instrument_map
from queuetruth.live.importers._base import ImportOptions
from queuetruth.live.importers.freqtrade import (
    OrderView,
    ReconcileReport,
    dtext,
    explain_pairs,
    finish_to_dir,
    iso_ns,
    opt_int,
    reconcile_run_id,
    reconcile_spec,
    run_score,
    summarize,
    timeframe_seconds,
    write_reconcile,
)
from queuetruth.schemas.io import sha256_file

_NS = 1_000_000_000
#: Options every backtest importer reads.
COMMON_OPTIONS = frozenset(
    {
        "venue",
        "map_venue",
        "pair_with",
        "pair_window_ms",
        "timeframe",
        "fill_delay_bars",
        "data_tz",
        "instrument",
        "instruments",
        "price_exp",
        "size_exp",
    }
)
_TERMINAL = {"cancel_ack": "cancelled", "expire": "expired", "reject": "rejected"}


@dataclass
class SimFill:
    """One execution of a simulated order."""

    ts_ns: int
    precision: str
    qty: Decimal
    px: Decimal
    fee: Decimal | None
    fill_id: str
    rec: Record


@dataclass
class SimOrder:
    """One simulated order of a backtest (exact decimals, ns timestamps)."""

    ref: str
    instrument: str
    side: str
    order_type: str
    tif: str
    qty: Decimal | None
    limit_px: Decimal | None
    placed_ns: int | None
    placed_precision: str
    rec: Record
    #: an order-level record (False: known from fills only, e.g. a Backtrader `Transactions` analyzer row).
    record: bool = True
    submit_ns: int | None = None
    fills: list[SimFill] = field(default_factory=list)
    #: (time, precision, event type, reason) of the terminal event.
    end: tuple[int, str, str, str] | None = None
    #: a canonical live order key the user stored on the simulated order.
    key_hint: str | None = None
    note: str | None = None
    key: str | None = None
    #: fill liquidity when the export knows it (default: `taker` for market and stop orders, else `unknown`).
    liquidity: str | None = None

    @property
    def filled(self) -> Decimal:
        total = Decimal(0)
        for f in self.fills:
            total += f.qty
        return total

    @property
    def ref_ns(self) -> int:
        """Reference time of the pairing: submission, else placement, else the first fill."""
        if self.submit_ns is not None:
            return self.submit_ns
        if self.placed_ns is not None:
            return self.placed_ns
        return self.fills[0].ts_ns if self.fills else 0


@dataclass
class SimRun:
    """Every order of one backtest export."""

    kind: str
    orders: list[SimOrder]
    paths: list[str]
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    #: bar length (ns) when option `timeframe` is given: the fill time is known to the bar only.
    timeframe_ns: int | None = None

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + n


# ---- options -------------------------------------------------------------------------------------------------------


def time_of(value: Any, what: str, loc: str, tz: str) -> tuple[int, str]:
    """A backtest timestamp: ISO 8601 text (`2024-03-01 10:00:00`, with or without an offset; naive times are in
    `tz`) -> (UTC ns, precision)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise input_error(loc, f"{what}: missing timestamp")
    return rfc3339_ns(str(value), what, loc, default_tz=tz)


def data_tz(opts: dict[str, Any]) -> str:
    """Option `data_tz`: the IANA zone of naive timestamps (default UTC)."""
    return str(opts.get("data_tz") or "UTC")


def instrument_names(opts: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
    """Options `instrument` (one name for every order) and `instruments` (`LABEL:NAME,LABEL:NAME`: per data feed or
    column label)."""
    one = opts.get("instrument")
    many: dict[str, str] = {}
    raw = opts.get("instruments")
    if isinstance(raw, dict):
        many = {str(k): str(v) for k, v in raw.items()}
    elif raw not in (None, ""):
        for part in str(raw).split(","):
            label, sep, name = part.partition(":")
            if not sep or not label.strip() or not name.strip():
                raise QtError("QT-CONFIG-001", f"instruments: expected LABEL:NAME[,LABEL:NAME...], got {raw!r}")
            many[label.strip()] = name.strip()
    if one not in (None, "") and many:
        raise QtError("QT-CONFIG-001", "instrument and instruments: give one of them")
    return (None if one in (None, "") else str(one)), many


def rename(label: str, one: str | None, many: dict[str, str]) -> str:
    """The live instrument name of a data feed / column label."""
    if one is not None:
        return one
    return many.get(label, label)


def apply_timeframe(run: SimRun, opts: dict[str, Any]) -> None:
    """Options `timeframe` (bar length) and `fill_delay_bars` (fills `k` bars after their timestamp)."""
    tf = opts.get("timeframe")
    run.timeframe_ns = None if tf in (None, "") else timeframe_seconds(tf) * _NS
    k = opt_int(opts, "fill_delay_bars", 0)
    if k < 0:
        raise QtError("QT-CONFIG-001", f"fill_delay_bars: {k} must be >= 0")
    if k and run.timeframe_ns is None:
        raise QtError("QT-CONFIG-001", "fill_delay_bars needs timeframe (the bar length)")
    if k:
        assert run.timeframe_ns is not None
        for o in run.orders:
            for f in o.fills:
                f.ts_ns += k * run.timeframe_ns
        run.warn(f"fill_delay_bars={k}: fill times moved {k} bar(s) ({k * run.timeframe_ns // _NS} s) later")


def pair_window_ns(run: SimRun, opts: dict[str, Any]) -> int:
    """Option `pair_window_ms` (default two bars of `timeframe`, else 60 s)."""
    default = 2 * run.timeframe_ns // 1_000_000 if run.timeframe_ns else 60_000
    w = opt_int(opts, "pair_window_ms", default)
    if w < 0:
        raise QtError("QT-CONFIG-001", "pair_window_ms must be >= 0")
    return w * 1_000_000


# ---- the live arm --------------------------------------------------------------------------------------------------


@dataclass
class LiveOrder:
    """One order of a canonical live log as the pairing and the explanation see it."""

    key: str
    venue: str
    instrument: str
    side: str
    order_type: str
    qty: Decimal
    limit_px: Decimal | None
    placed_ns: int | None
    fills: list[tuple[int, Decimal, Decimal | None]]
    end_ns: int | None
    status: str

    @property
    def filled(self) -> Decimal:
        total = Decimal(0)
        for _, q, _ in self.fills:
            total += q
        return total


@dataclass
class LiveArm:
    """A canonical live-log directory read as the live arm."""

    path: str
    orders: list[LiveOrder]
    orows: list[dict[str, Any]]
    erows: list[dict[str, Any]]

    @property
    def venues(self) -> list[str]:
        return sorted({o.venue for o in self.orders})

    @property
    def simulated(self) -> bool:
        """Every order is a simulator's (`sim:` venue) or marked simulated (a dry run, a paper bot)."""
        return bool(self.orows) and all(
            str(o["venue"]).startswith("sim:") or o.get("validation_status") == "simulated" for o in self.orows
        )


def _best_ts(e: dict[str, Any]) -> int | None:
    for k in ("ts_exchange", "ts_local_recv", "ts_local_send"):
        if e.get(k) is not None:
            return int(e[k])
    return None


def read_live_arm(path: str | Path) -> LiveArm:
    """A canonical live-log directory (`orders.parquet` + `events.parquet`) -> `LiveArm`. Placement = the first
    `ack` (else `submit_sent`); fills net of nothing (busts and corrections are counted by `live validate`, not
    here); end = the first terminal event."""
    from queuetruth.live.canonical import read_log, table_rows

    p = Path(path)
    if not p.is_dir():
        raise QtError(
            "QT-INPUT-001",
            f"{p}: the live arm must be a canonical live-log directory (queuetruth live import ... --out DIR)",
        )
    o_t, e_t = read_log(p)
    orows, erows = table_rows(o_t), table_rows(e_t)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for e in erows:
        by_key.setdefault(str(e["order_key"]), []).append(e)
    out: list[LiveOrder] = []
    for o in orows:
        key = str(o["order_key"])
        pe, se = int(o["price_exp"]), int(o["size_exp"])
        evs = sorted(by_key.get(key, []), key=lambda r: int(r["event_seq"]))
        placed = None
        for want in ("ack", "submit_sent"):
            placed = next((_best_ts(e) for e in evs if e["event_type"] == want), None)
            if placed is not None:
                break
        fills: list[tuple[int, Decimal, Decimal | None]] = []
        end = None
        status = "open"
        for e in evs:
            et = e["event_type"]
            if et == "fill" and e.get("qty") is not None:
                ts = _best_ts(e)
                px = None if e.get("px") is None else Decimal(int(e["px"])).scaleb(pe)
                fills.append((ts if ts is not None else 0, Decimal(int(e["qty"])).scaleb(se), px))
            elif et in _TERMINAL and end is None:
                end = _best_ts(e)
                status = _TERMINAL[et]
        out.append(
            LiveOrder(
                key=key,
                venue=str(o["venue"]),
                instrument=str(o["instrument"]),
                side=str(o["side"]),
                order_type=str(o["order_type"]),
                qty=Decimal(int(o["orig_qty"])).scaleb(se),
                limit_px=None if o.get("limit_px") is None else Decimal(int(o["limit_px"])).scaleb(pe),
                placed_ns=placed,
                fills=sorted(fills, key=lambda f: f[0]),
                end_ns=end,
                status=status,
            )
        )
    return LiveArm(str(p), out, orows, erows)


def live_venue(arm: LiveArm, opts: dict[str, Any]) -> str:
    """The live arm's venue (option `venue` when the log holds several)."""
    if opts.get("venue"):
        return str(opts["venue"]).lower()
    vs = arm.venues
    if len(vs) > 1:
        raise QtError("QT-CONFIG-001", f"the live log {arm.path} holds venues {vs}; pass option venue=<name>")
    return vs[0] if vs else ""


# ---- pairing -------------------------------------------------------------------------------------------------------


def pair_sim_live(
    live: list[LiveOrder], sim: list[SimOrder], *, window_ns: int
) -> list[tuple[LiveOrder | None, SimOrder | None]]:
    """Pairs simulator orders with live orders (rules in the module docstring)."""
    if window_ns < 0:
        raise QtError("QT-CONFIG-001", "pair_window_ms must be >= 0")
    idx_of = {lo.key: i for i, lo in enumerate(live)}
    used: set[int] = set()
    pairs: list[tuple[LiveOrder | None, SimOrder | None]] = []
    paired_sim: set[int] = set()
    for so in sim:
        i = idx_of.get(so.key_hint or "")
        if i is not None and i not in used and live[i].side == so.side:
            used.add(i)
            paired_sim.add(id(so))
            pairs.append((live[i], so))
    buckets: dict[tuple[str, str], list[tuple[int, str, int]]] = {}
    for i, lo in enumerate(live):
        if i in used or lo.placed_ns is None:
            continue
        buckets.setdefault((lo.instrument, lo.side), []).append((lo.placed_ns, lo.key, i))
    times: dict[tuple[str, str], list[int]] = {}
    for k, b in buckets.items():
        b.sort()
        times[k] = [x[0] for x in b]
    for so in sorted(sim, key=lambda o: (o.ref_ns, o.ref)):
        if id(so) in paired_sim:
            continue
        k = (so.instrument, so.side)
        b = buckets.get(k)
        if not b:
            continue
        ts, r = times[k], so.ref_ns
        best: tuple[tuple[int, Decimal, int, str], int] | None = None
        for j in range(bisect.bisect_left(ts, r - window_ns), bisect.bisect_right(ts, r + window_ns)):
            placed, key, i = b[j]
            if i in used:
                continue
            dq = abs(live[i].qty - (so.qty if so.qty is not None else so.filled))
            cand = (abs(placed - r), dq, placed, key)
            if best is None or cand < best[0]:
                best = (cand, i)
        if best is not None:
            used.add(best[1])
            paired_sim.add(id(so))
            pairs.append((live[best[1]], so))
    pairs += [(lo, None) for i, lo in enumerate(live) if i not in used]
    pairs += [(None, so) for so in sim if id(so) not in paired_sim]
    return pairs


# ---- the simulator log ---------------------------------------------------------------------------------------------


def _round(d: Decimal | None, exp: int, counter: list[int]) -> Decimal | None:
    if d is None:
        return None
    q = d.quantize(Decimal(1).scaleb(exp), rounding=ROUND_HALF_EVEN)
    if q != d:
        counter[0] += 1
    return q.normalize() if q else Decimal(0)


def _exps(imap: InstrumentMap | None, venue: str, inst: str, ts: int, opts: dict[str, Any]) -> tuple[int, int]:
    e = imap.lookup(venue, inst, utc_date_days(ts)) if imap is not None else None
    if e is not None:
        return e.price_exp, e.size_exp
    return opt_int(opts, "price_exp", -9), opt_int(opts, "size_exp", -8)


def emit_sim(
    b: LogBuilder,
    run: SimRun,
    *,
    sim_name: str,
    account: str,
    imap: InstrumentMap | None,
    opts: dict[str, Any],
    keys: dict[int, str] | None = None,
    live_of: dict[int, LiveOrder] | None = None,
) -> None:
    """Adds the orders of a backtest to a builder (events as in the module docstring). `keys` maps a simulator
    order (`id`) to the live order key it pairs with, `live_of` to the paired live order (fill times clamped to
    its placement). Prices and sizes are rounded to the instrument exponents in place, so a reconciliation explains
    the values the simulator log holds."""
    rounded = [0]
    clamped = 0
    cs: dict[str, Any] = {"source": "sim", "stream": False}
    for o in run.orders:
        lo = (live_of or {}).get(id(o))
        key = (
            (lo.key if lo is not None else None) or (keys or {}).get(id(o)) or make_order_key(sim_name, account, o.ref)
        )
        o.key = key
        pe, se = _exps(imap, b.map_venue, o.instrument, o.ref_ns, opts)
        qty, limit = _round(o.qty, se, rounded), _round(o.limit_px, pe, rounded)
        o.qty, o.limit_px = qty, limit  # the explanation quotes the log's values
        h = b.order(
            cid=o.ref,
            rec=o.rec,
            record=o.record and qty is not None,
            qty=qty,
            qty_text=dtext(qty),
            px=limit,
            px_text=dtext(limit),
            instrument=o.instrument,
            side=o.side,
            order_type=o.order_type if o.record else None,
            tif=o.tif if o.record else None,
            post_only=False,
        )
        b.set_order_field(h, "key_override", key)
        if o.submit_ns is not None:
            b.event(
                h, "submit_sent", rec=o.rec, kind="unknown", precision=o.placed_precision,
                ts_local_send=o.submit_ns, qty=qty, px=limit, **cs,
            )  # fmt: skip
        if o.record and o.placed_ns is not None:
            b.event(
                h, "ack", rec=o.rec, kind="record_time", precision=o.placed_precision,
                ts_exchange=o.placed_ns, qty=qty, px=limit, **cs,
            )  # fmt: skip
        liq = o.liquidity or ("taker" if o.order_type in ("market", "stop") else "unknown")
        for f in o.fills:
            fq, fp = _round(f.qty, se, rounded), _round(f.px, pe, rounded)
            assert fq is not None and fp is not None
            f.qty, f.px = fq, fp
            if not fq:
                b.skip("execution of a quantity below the size exponent")
                continue
            ts, note = f.ts_ns, o.note
            if lo is not None and lo.placed_ns is not None and lo.placed_ns > ts:
                ts = lo.placed_ns
                clamped += 1
                note = f"{note or 'backtest fill'}; bar time {iso_ns(f.ts_ns)} clamped to the paired live order's entry"
            b.event(
                h, "fill", rec=f.rec, kind="record_time", precision=f.precision, ts_exchange=ts,
                qty=fq, px=fp, fill_id=f.fill_id, liquidity=liq, fee=f.fee, reason=note, **cs,
            )  # fmt: skip
        if o.end is not None:
            ts, prec, et, reason = o.end
            b.event(h, et, rec=o.rec, kind="record_time", precision=prec, ts_exchange=ts, reason=reason, **cs)
    if rounded[0]:
        b.warn(f"{b.venue}: {rounded[0]} backtest price / size value(s) rounded half-even to the instrument exponents")
    if clamped:
        b.warn(
            f"{b.venue}: {clamped} backtest fill time(s) before the paired live order's entry clamped to it (the bar "
            "is all the backtest knows; the scorer's window starts at the live entry)"
        )


def sim_builder(
    sim_name: str, account: str, imap: InstrumentMap | None, opts: dict[str, Any], *, map_venue: str
) -> LogBuilder:
    return LogBuilder(
        sim_name,
        account=account,
        imap=imap,
        price_exp=opt_int(opts, "price_exp", -9),
        size_exp=opt_int(opts, "size_exp", -8),
        map_venue=map_venue,
        validation_status="simulated",
        source_override="sim",
    )


def finish_sim(b: LogBuilder, run: SimRun) -> ImportResult:
    for w in run.warnings:
        b.warn(f"{b.venue}: {w}")
    for reason, n in run.skipped.items():
        b.skip(reason, n)
    return b.finish()


def import_backtest_sim(run: SimRun, opts: ImportOptions) -> ImportResult:
    """A backtest -> canonical rows of venue `sim:<kind>`; option `pair_with` (a canonical live-log directory) gives
    paired orders the live `order_key`, `venue` names the live venue of the instrument-map rows."""
    sim_name = f"sim:{run.kind}"
    account = opts.account or "main"
    keys: dict[int, str] = {}
    live_of: dict[int, LiveOrder] = {}
    pw = opts.extra.get("pair_with")
    map_venue = str(opts.extra.get("map_venue") or opts.extra.get("venue") or run.kind)
    hinted = sum(1 for o in run.orders if o.key_hint is not None)
    if pw:
        if isinstance(pw, (list, tuple)):
            if len(pw) != 1:
                raise QtError("QT-CONFIG-001", "pair_with: one canonical live-log directory")
            pw = pw[0]
        arm = read_live_arm(str(pw))
        venue = live_venue(arm, opts.extra)
        map_venue = str(opts.extra.get("map_venue") or venue)
        pairs = pair_sim_live(
            [lo for lo in arm.orders if lo.venue == venue], run.orders, window_ns=pair_window_ns(run, opts.extra)
        )
        live_of = {id(s): lo for lo, s in pairs if lo is not None and s is not None}
        keys = {k: lo.key for k, lo in live_of.items()}
        run.warn(f"{len(keys)} of {len(run.orders)} simulator order(s) paired with an order of the live log {pw}")
        run.warn(f"pair_with: keys taken from {pw} (orders sha256 {_log_digest(Path(str(pw)))}), venue {venue}")
    else:
        keys = {id(o): o.key_hint for o in run.orders if o.key_hint is not None}
        if not keys:
            run.warn(f"no pair_with live log: simulator orders keep {sim_name} keys and match no live order")
    if hinted and not pw:
        run.warn(f"{hinted} simulator order(s) carry a live order_key (order.info) and keep it")
    b = sim_builder(sim_name, account, opts.imap, opts.extra, map_venue=map_venue)
    emit_sim(b, run, sim_name=sim_name, account=account, imap=opts.imap, opts=opts.extra, keys=keys, live_of=live_of)
    return finish_sim(b, run)


def _log_files(p: Path) -> list[str]:
    """The table files of a canonical live-log directory (hashed into the reconciliation's `run_id`)."""
    for names in (("orders.parquet", "events.parquet"), ("orders.csv", "events.csv")):
        if all((p / n).exists() for n in names):
            return [str(p / n) for n in names]
    return [str(p / "orders.parquet"), str(p / "events.parquet")]


def _log_digest(p: Path) -> str:
    f = p / "orders.parquet"
    return sha256_file(f)[:16] if f.exists() else "n/a"


# ---- reconcile -----------------------------------------------------------------------------------------------------


def _avg(fills: list[tuple[Decimal, Decimal | None]]) -> Decimal | None:
    qty, notional = Decimal(0), Decimal(0)
    for q, p in fills:
        if p is None:
            return None
        qty += q
        notional += q * p
    if qty == 0:
        return None
    avg = notional / qty
    return avg.quantize(Decimal(1).scaleb(-12)).normalize() if avg != avg.to_integral_value() else avg


def live_view(o: LiveOrder) -> OrderView:
    filled = o.filled
    return OrderView(
        key=o.key,
        instrument=o.instrument,
        side=o.side,
        role="order",
        order_type=o.order_type,
        qty=o.qty,
        limit_px=o.limit_px,
        placed_ns=o.placed_ns,
        filled_qty=filled,
        avg_px=_avg([(q, p) for _, q, p in o.fills]) if filled > 0 else None,
        first_fill_ns=o.fills[0][0] if o.fills else None,
        end_ns=o.end_ns,
        status=o.status,
    )


def sim_view(o: SimOrder, *, resolution_ns: int) -> OrderView:
    filled = o.filled
    status = "open"
    if o.end is not None:
        status = _TERMINAL[o.end[2]]
    if not o.record:
        status = "unknown (fills only)"
    return OrderView(
        key=o.key or o.ref,
        instrument=o.instrument,
        side=o.side,
        role="order",
        order_type=o.order_type,
        qty=o.qty if o.qty is not None else filled,
        limit_px=o.limit_px,
        placed_ns=o.placed_ns,
        filled_qty=filled,
        avg_px=_avg([(f.qty, f.px) for f in o.fills]) if filled > 0 else None,
        first_fill_ns=o.fills[0].ts_ns if o.fills else None,
        end_ns=None if o.end is None else o.end[0],
        status=status,
        note=o.note,
        resolution_ns=resolution_ns,
    )


def _complete_ids(
    arm: LiveArm, run: SimRun, venue: str, opts: dict[str, Any]
) -> tuple[list[dict[str, Any]], InstrumentMap, bool]:
    """(live order rows with every `instrument_id` set, the instrument map of both logs, ids were added): ids the
    live log has are kept; instruments without one are numbered after the largest id in sorted order, with the
    live rows' scales (simulator-only instruments: the default scales)."""
    rows = [dict(r) for r in arm.orows]
    ids: dict[str, int] = {}
    scales: dict[str, tuple[int, int]] = {}
    for r in rows:
        if r["venue"] != venue:
            continue
        scales.setdefault(str(r["instrument"]), (int(r["price_exp"]), int(r["size_exp"])))
        if r.get("instrument_id") is not None:
            ids.setdefault(str(r["instrument"]), int(r["instrument_id"]))
    names = sorted({str(r["instrument"]) for r in rows if r["venue"] == venue} | {o.instrument for o in run.orders})
    nxt = max(ids.values(), default=0) + 1
    added = False
    for n in names:
        if n not in ids:
            ids[n] = nxt
            nxt += 1
            added = True
    for r in rows:
        if r["venue"] == venue and r.get("instrument_id") is None:
            r["instrument_id"] = ids[str(r["instrument"])]
    dpe, dse = opt_int(opts, "price_exp", -9), opt_int(opts, "size_exp", -8)
    entries = [
        InstrumentEntry(venue, n, ids[n], None, *scales.get(n, (dpe, dse)), 1, 1) for n in names
    ]  # tick and lot of one unit at the exponent: the log's own scales
    return rows, InstrumentMap(entries), added


def reconcile_backtest(
    run: SimRun,
    live: str | Path | list[str | Path],
    out: str | Path,
    *,
    sim_paths: list[str],
    account: str,
    instrument_map: str | Path | None,
    window_ns: int,
    price_tol_bps: float,
    time_tol_ns: int | None,
    score: bool,
    arms_as_given: bool,
    options: dict[str, Any],
) -> ReconcileReport:
    """A backtest (`run`, read from `sim_paths`) against a canonical live log: writes `<out>/live`, `<out>/sim`,
    `<out>/score`, `reconcile.json` and `reconcile_orders.csv`; returns the `ReconcileReport`."""
    from queuetruth.live import write_canonical_log

    kind = run.kind
    sim_name = f"sim:{kind}"
    lp = [str(live)] if isinstance(live, (str, Path)) else [str(x) for x in live]
    if len(lp) != 1:
        raise QtError(
            "QT-CONFIG-001",
            f"live: one canonical live-log directory, got {len(lp)} (import every live record into one log with "
            "queuetruth live import --in ... --in ...)",
        )
    arm = read_live_arm(lp[0])
    warnings: list[str] = []
    if arm.simulated:
        if not arms_as_given:
            raise QtError(
                "QT-CONFIG-001",
                f"the live arm {lp[0]} is a simulator log or a simulated run (venue sim:*, validation_status "
                f"simulated): arms swapped? (reconcile_{kind}(sim, live, out); pass arms_as_given=True to score "
                "them as given)",
            )
        warnings.append("arms_as_given: the live arm is a simulator log or a simulated run")
    venue = live_venue(arm, options)
    other = [o for o in arm.orders if o.venue != venue]
    if other:
        warnings.append(f"live: {len(other)} order(s) on other venues than {venue} left out of the pairing")
    lorders = [o for o in arm.orders if o.venue == venue]
    outp = Path(out)
    if instrument_map is not None:
        imap = load_instrument_map(str(instrument_map))
        lrows = arm.orows
        if any(r.get("instrument_id") is None for r in lrows):
            warnings.append(
                "live: orders without instrument_id (the log was imported without this instrument map): reimport "
                "it with --instrument-map to score them"
            )
    else:
        lrows, imap, added = _complete_ids(arm, run, venue, options)
        warnings.append(
            "instrument_map: none given; instrument ids and scales taken from the live log"
            + (" (instruments without an id numbered after its largest id)" if added else "")
        )
    pairs = pair_sim_live(lorders, run.orders, window_ns=pair_window_ns(run, options))
    live_of = {id(s): lo for lo, s in pairs if lo is not None and s is not None}
    write_canonical_log(outp / "live", lrows, arm.erows, instrument_map_sha256=None)
    sb = sim_builder(sim_name, account, imap, options, map_venue=venue)
    emit_sim(sb, run, sim_name=sim_name, account=account, imap=imap, opts=options, live_of=live_of)
    sres = finish_sim(sb, run)
    ssum = finish_to_dir(sim_name, sres, run.paths, outp / "sim", status="simulated")
    warnings += [f"sim: {w}" for w in ssum.warnings]
    sc, err, classes = (None, "not requested", {})
    if score:
        sc, err, classes = run_score(outp / "live", outp / "sim", outp / "score", window_ns=window_ns)
    res = run.timeframe_ns or 0
    tol = time_tol_ns if time_tol_ns is not None else (0 if res else 60 * _NS)
    views = [
        (None if lo is None else live_view(lo), None if so is None else sim_view(so, resolution_ns=res))
        for lo, so in pairs
    ]
    rows = explain_pairs(
        views,
        price_tol_bps=price_tol_bps,
        time_tol_ns=tol,
        sim_name="backtest",
        live_name="live",
        no_sim_hint="the backtest placed no order of this instrument and side within the pairing window",
        no_live_hint="the live log has no order of this instrument and side within the pairing window",
        score_classes=classes,
    )
    counts, adv, missed = summarize(rows)
    n_paired = len(live_of)
    n_live = len(lorders)
    coverage = None if n_live == 0 else n_paired / n_live
    if coverage is not None and coverage < 0.5:
        warnings.append(
            f"pairing: only {n_paired} of {n_live} live orders paired ({coverage:.0%}): the backtest and the live run "
            "cover other periods or instruments, or pair_window_ms / timeframe are too tight; unpaired orders say "
            "nothing about fills"
        )
    unnamed = sorted({o.instrument for o in run.orders} - {o.instrument for o in lorders})
    if unnamed and lorders:
        warnings.append(
            f"pairing: simulator instruments {unnamed} are not in the live log (live: "
            f"{sorted({o.instrument for o in lorders})}); name them with option instrument=NAME or "
            "instruments=LABEL:NAME"
        )
    spec = reconcile_spec(
        kind,
        sim_paths,
        _log_files(Path(lp[0])),
        instrument_map=None if instrument_map is None else str(instrument_map),
        options=options,
        account=account,
        window_ns=int(window_ns),
        price_tol_bps=price_tol_bps,
        time_tol_ns=int(tol),
        score=bool(score),
        arms_as_given=bool(arms_as_given),
    )
    report = ReconcileReport(
        kind=kind,  # type: ignore[arg-type]
        run_id=reconcile_run_id(spec),
        live_log="live",
        sim_log="sim",
        n_live_orders=n_live,
        n_sim_orders=len(run.orders),
        n_paired=n_paired,
        pairing_coverage=coverage,
        counts=counts,
        sim_price_advantage_quote=adv,
        missed_fill_notional=missed,
        score=sc,
        score_error=err,
        options=spec,
        warnings=warnings,
        orders=rows,
    )
    write_reconcile(outp, report)
    return report


def key_hint(v: Any) -> str | None:
    """A user-given live order key (`<venue>:<account>:<id>`, valid id characters), else None."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    return s if valid_id(s) and s.count(":") >= 2 else None


__all__ = [
    "COMMON_OPTIONS",
    "LiveArm",
    "LiveOrder",
    "SimFill",
    "SimOrder",
    "SimRun",
    "emit_sim",
    "import_backtest_sim",
    "pair_sim_live",
    "read_live_arm",
    "reconcile_backtest",
]
