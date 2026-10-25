"""Orders of a sandbox episode: the instructions as shadow orders (`qt.shadow_spec` + `qt.shadow_amend`, SPEC 3.4)
and the order state read back from replay outputs.

Mapping (SPEC 3.1 latency semantics, 5.2):

- every order is a shadow with `ts_submit` = the agent's clock at the decision and `latency_ns` = the session latency
  plus the order's jitter draw, so its arrival is `ts_live = ts_submit + latency_ns` and a cancel sent at `c0` takes
  effect at `c0 + latency_ns`;
- a limit order has `price_mode = abs`; a pegged limit order (`peg_offset_ticks`) has `price_mode = touch` and is
  priced against the same-side best at arrival (rejected when no touch exists, or when a positive offset would cross);
- a market order is an IOC limit order at the collar price; an IOC order gets `ts_cancel = ts_submit + 1 ns` and
  only its arrival fills (`crossed_at_live`, the taker walk of SPEC 5.2.4a) count: its remainder is cancelled on
  arrival. The same filter applies to every replay, so it never breaks equality with a full replay;
- a partial cancel is an amend (`new_qty` = remaining requested size; priority kept).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.sandbox._util import dec, money
from queuetruth.score._io import make_table

#: Engine end states that end an order (in any replay).
TERMINAL = {
    "filled",
    "amended_to_filled",
    "cancelled",
    "rejected_would_cross",
    "rejected_no_touch",
    "censored_clear",
    "censored_gap",
}
#: Engine end states that only mean "the replayed data ended".
OPEN_AT_END = {"open_at_end", "censored_session_end"}
#: Columns of a fill row compared for prefix stability and replay equality (every `qt.shadow_fills` column).
FILL_COLUMNS = (
    "shadow_id",
    "fill_seq",
    "ts_event",
    "sequence",
    "fill_qty",
    "cum_qty",
    "leaves_qty",
    "fill_px",
    "reason",
    "queue_ahead_before",
    "traded_at_px_event",
    "trade_through_event",
    "behind_volume_executed",
    "liquidity",
)


@dataclass
class Order:
    """One order: the instruction as committed and the state as last read from a replay."""

    order_id: str
    index: int
    client_order_id: str | None
    side: str
    type: str
    tif: str
    price: int | None
    peg_offset_ticks: int | None
    orig_qty: int
    ts_submit: int
    latency_ns: int
    est_price: int | None
    decision_mid_x2: int | None
    request: dict[str, Any]
    qty: int = 0
    cancel_send_ns: int | None = None
    cancel_by: str | None = None
    reduces: list[tuple[int, int]] = field(default_factory=list)
    status: str = "pending"
    reason: str | None = None
    final: bool = False
    resolved_price: int | None = None
    ts_activated: int | None = None
    ts_done: int | None = None
    queue_ahead: int | None = None
    rank: int | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)
    fill_steps: list[int | None] = field(default_factory=list)
    result: dict[str, Any] | None = None
    out_of_session: bool = False
    #: What the session showed (checked against the certification replay at finish): the step that first showed the
    #: order working, the step that showed it ended, and (status, reason, ts_done, filled_qty) as shown then.
    working_step: int | None = None
    final_step: int | None = None
    shown_final: tuple[str, str | None, int | None, int] | None = None

    def __post_init__(self) -> None:
        if self.qty == 0:
            self.qty = self.orig_qty

    @property
    def aggressive(self) -> bool:
        """IOC semantics: market orders and IOC limit orders."""
        return self.type == "market" or self.tif == "ioc"

    @property
    def sign(self) -> int:
        """+1 buy, -1 sell."""
        return 1 if self.side == "buy" else -1

    @property
    def filled_qty(self) -> int:
        """Filled size so far."""
        return sum(int(f["fill_qty"]) for f in self.fills)

    @property
    def open(self) -> bool:
        """Not ended (pending or working)."""
        return not self.final

    def known_ts(self, fill: dict[str, Any]) -> int:
        """When a fill could be known: its event, or the arrival event for an arrival (taker) fill."""
        if fill["reason"] == "crossed_at_live":
            if self.ts_activated is None:
                raise QtError("QT-ENGINE-001", f"order {self.order_id}: arrival fill without an activation event")
            return int(self.ts_activated)
        return int(fill["ts_event"])

    def shadow_row(self, instrument_id: int) -> dict[str, Any]:
        """The `qt.shadow_spec` row."""
        pegged = self.peg_offset_ticks is not None
        if self.aggressive:
            ts_cancel = self.ts_submit + 1
        else:
            ts_cancel = self.cancel_send_ns
        return {
            "shadow_id": self.order_id,
            "instrument_id": instrument_id,
            "side": ord("B") if self.side == "buy" else ord("A"),
            "price_mode": "touch" if pegged else "abs",
            "price": None if pegged else self.price,
            "offset_ticks": int(self.peg_offset_ticks or 0),
            "qty": self.orig_qty,
            "ts_submit": self.ts_submit,
            "latency_ns": self.latency_ns,
            "ts_cancel": ts_cancel,
            "group": None,
            "source": "user",
            "order_key": None,
        }


def schedule(orders: list[Order], instrument_id: int) -> tuple[pa.Table, pa.Table]:
    """`qt.shadow_spec` and `qt.shadow_amend` tables of `orders` (input order)."""
    rows = [o.shadow_row(instrument_id) for o in orders]
    amends = [{"shadow_id": o.order_id, "ts_send": t, "new_qty": q} for o in orders for t, q in o.reduces]
    return make_table("qt.shadow_spec", rows), make_table("qt.shadow_amend", amends)


def _status_of(end_state: str) -> tuple[str, str | None]:
    if end_state == "filled":
        return "filled", None
    if end_state == "amended_to_filled":
        return "filled", "reduced_to_filled"
    if end_state == "cancelled":
        return "cancelled", None
    if end_state in ("rejected_would_cross", "rejected_no_touch"):
        return "rejected", end_state
    if end_state == "censored_clear":
        return "censored", "book_clear"
    if end_state == "censored_gap":
        return "censored", "feed_gap"
    raise QtError("QT-ENGINE-001", f"unexpected shadow end_state {end_state!r}")


def absorb(
    o: Order,
    result: dict[str, Any],
    fill_rows: list[dict[str, Any]],
    traj_last: dict[str, Any] | None,
    *,
    final_replay: bool,
    step: int | None,
) -> list[dict[str, Any]]:
    """Updates `o` from one replay's rows and returns its fills that are new to the session. Raises `QT-ENGINE-001`
    when a fill already reported is missing or different in this replay (prefix stability)."""
    rows = sorted(fill_rows, key=lambda r: int(r["fill_seq"]))
    if o.aggressive:
        rows = [r for r in rows if r["reason"] == "crossed_at_live"]
    known = len(o.fills)
    if rows[:known] != o.fills:
        raise QtError(
            "QT-ENGINE-001",
            f"order {o.order_id}: a replay of a longer prefix changed fills already reported (prefix stability)",
            details={"order_id": o.order_id, "known": o.fills, "now": rows[:known]},
        )
    new = rows[known:]
    o.fills = rows
    o.fill_steps.extend([step] * len(new))
    o.result = result
    o.out_of_session = bool(result.get("out_of_session"))
    if result.get("price") is not None:
        o.resolved_price = int(result["price"])
    act = result.get("ts_activated_event")
    o.ts_activated = None if act is None else int(act)
    end = str(result["end_state"])
    o.queue_ahead = o.rank = None
    if o.aggressive:
        if o.ts_activated is not None and end not in ("rejected_would_cross", "rejected_no_touch"):
            o.final = True
            filled = o.filled_qty
            o.status = "filled" if filled >= o.qty else "cancelled"
            o.reason = None if o.status == "filled" else ("ioc_remainder" if filled else "no_liquidity")
            o.ts_done = o.ts_activated
            return new
    if end in TERMINAL:
        o.final = True
        o.status, o.reason = _status_of(end)
        if o.status == "cancelled":
            o.reason = "finish_cancel" if o.cancel_by == "finish" else "agent_cancel"
            o.ts_done = result.get("ts_cancel_effective")
        elif o.status == "filled":
            o.ts_done = result.get("ts_full_fill") or result.get("ts_last_fill")
        else:  # rejected at arrival, or censored: the trajectory's end row when there is one
            end_ts = (traj_last or {}).get("ts_event")
            if end_ts is None:
                end_ts = o.ts_activated if o.ts_activated is not None else result.get("ts_live")
            o.ts_done = end_ts
        return new
    if end not in OPEN_AT_END:
        raise QtError("QT-ENGINE-001", f"order {o.order_id}: unexpected shadow end_state {end!r}")
    if final_replay:
        o.final = True
        o.status, o.reason = "expired", "open_at_end_of_data"
        o.ts_done = (traj_last or {}).get("ts_event")
        return new
    o.final = False
    if o.ts_activated is None:
        o.status, o.reason = "pending", None
    else:
        o.status, o.reason = "working", None
        if traj_last is not None:
            o.queue_ahead = int(traj_last["queue_ahead_after"])
            o.rank = int(traj_last["rank_after"])
    return new


def fee_dec(qty: int, px: int, liquidity: str, fees: Any, *, price_exp: int, size_exp: int, mult: float) -> Decimal:
    """Fee of one fill (positive = cost), exact: `qty` (real units) x per-unit fee + notional x bps / 10^4
    (`FeeSchedule`; the notional includes the contract multiplier)."""
    q_real = Decimal(qty).scaleb(size_exp)
    notional = Decimal(abs(px)).scaleb(price_exp) * q_real * dec(mult)
    if liquidity == "maker":
        per_unit, bps = fees.maker_per_unit, fees.maker_bps
    else:
        per_unit, bps = fees.taker_per_unit, fees.taker_bps
    return q_real * dec(per_unit) + notional * dec(bps) / 10_000


def fee_of(qty: int, px: int, liquidity: str, fees: Any, *, price_exp: int, size_exp: int, mult: float) -> float:
    """`fee_dec` as a float."""
    return float(fee_dec(qty, px, liquidity, fees, price_exp=price_exp, size_exp=size_exp, mult=mult))


@dataclass
class Account:
    """Exact running account of fills: position and traded sizes (size units), cash and notional (raw price x size
    units, before the multiplier), fees by liquidity (currency)."""

    qty: int = 0
    bought: int = 0
    sold: int = 0
    cash_raw: int = 0
    notional_raw: int = 0
    fees_maker: Decimal = Decimal(0)
    fees_taker: Decimal = Decimal(0)

    @property
    def fees(self) -> Decimal:
        """All fees."""
        return self.fees_maker + self.fees_taker

    def add(
        self, sign: int, q: int, p: int, liq: str, fees: Any, *, price_exp: int, size_exp: int, mult: float
    ) -> None:
        """Books one fill (`sign` +1 buy, -1 sell)."""
        self.qty += sign * q
        if sign > 0:
            self.bought += q
        else:
            self.sold += q
        self.cash_raw -= sign * q * p
        self.notional_raw += abs(p) * q
        f = fee_dec(q, p, liq, fees, price_exp=price_exp, size_exp=size_exp, mult=mult)
        if liq == "maker":
            self.fees_maker += f
        else:
            self.fees_taker += f


def account(
    fills: list[tuple[int, int, int, str]], fees: Any, *, price_exp: int, size_exp: int, mult: float
) -> Account:
    """The account of `(sign, qty, price, liquidity)` fills."""
    a = Account()
    for sign, q, p, liq in fills:
        a.add(sign, q, p, liq, fees, price_exp=price_exp, size_exp=size_exp, mult=mult)
    return a


def pnl(a: Account, mark_x2: int | None, *, price_exp: int, size_exp: int, mult: float) -> Decimal | None:
    """Gross mark-to-market PnL: cash + position x mark (`mark_x2` = twice the mark price, raw); exact. A flat
    position needs no mark; an open one without a mark has no PnL (None)."""
    if a.qty == 0:
        return money(a.cash_raw, price_exp + size_exp, mult)
    if mark_x2 is None:
        return None
    return money(Decimal(2 * a.cash_raw + a.qty * mark_x2) / 2, price_exp + size_exp, mult)


__all__ = [
    "FILL_COLUMNS",
    "OPEN_AT_END",
    "TERMINAL",
    "Account",
    "Order",
    "absorb",
    "account",
    "fee_dec",
    "fee_of",
    "pnl",
    "schedule",
]
