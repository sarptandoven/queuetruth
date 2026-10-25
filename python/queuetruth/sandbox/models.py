"""Spec and documents of the agent sandbox (`qt.sandbox_*`), Pydantic v2 models in the conventions of
`queuetruth.schemas.models` (SPEC 0.1, 3.10): every document starts with `"schema"` and `"schema_version": 1`,
unknown fields are rejected, timestamps and durations are integer ns, prices are integers at the instrument's
`price_exp` and sizes integers at its `size_exp` (the "raw" units of every qt table). Money (`cash`, `fee`, `pnl_*`,
`mo_ccy`, ...) is a float in the instrument's currency; `avg_fill_price`, `mark_price` and `vwap_price` are floats in
currency per size unit (real prices), named so that they cannot be mistaken for raw integers.

The models are registered in `SANDBOX_DOC_MODELS` / `SANDBOX_SPEC_MODELS` (not in `queuetruth.schemas.DOC_MODELS`,
see docs/design/extensions/agent-sandbox.md); `json_schemas()` returns their draft 2020-12 JSON Schemas, which the
MCP tools declare as output schemas.
"""

from __future__ import annotations

import itertools
import math
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from queuetruth.schemas.models import FileEntry, QtDoc, QtModel, Sha256
from queuetruth.schemas.specs import DateStr, NonNegInt, PathRef, SpecModel

S_NS = 1_000_000_000
PosInt = Annotated[int, Field(ge=1)]
Hex16 = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
#: Client order ids follow the shadow-id grammar of SPEC 0.1 (they end up in no engine table, but stay printable).
ClientOrderId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:\-]+$")]
OrderId = Annotated[str, Field(pattern=r"^o[1-9][0-9]*$")]
#: One displayed level: (price, size, order count).
LevelTuple = tuple[int, int, int]
DEFAULT_HORIZONS_NS = (1 * S_NS, 10 * S_NS, 60 * S_NS)


def _tag(name: str) -> Any:
    return Field(default=name, alias="schema")


def _finite(v: float, what: str) -> float:
    if not math.isfinite(v):
        raise ValueError(f"{what} must be finite")
    return v


# ---- spec -------------------------------------------------------------------------------------------------------


class FeeSchedule(SpecModel):
    """Fees per fill by liquidity. `*_per_unit`: currency per size unit (real units, e.g. USD per share); `*_bps`:
    basis points of the fill's notional. Positive = cost, negative = rebate (SPEC 0.1). Both parts add up."""

    maker_per_unit: float = 0.0
    taker_per_unit: float = 0.0
    maker_bps: float = 0.0
    taker_bps: float = 0.0

    @field_validator("maker_per_unit", "taker_per_unit", "maker_bps", "taker_bps")
    @classmethod
    def _check(cls, v: float) -> float:
        return _finite(v, "fee")


def _default_fees() -> FeeSchedule:
    return FeeSchedule()


class SandboxSpec(SpecModel):
    """One sandbox episode (`qt.sandbox_spec`): one instrument-day of an MBO dataset, an episode window and the
    exchange simulation settings. Times are absolute ns since the epoch; `Sandbox.open` also accepts local
    times of day (`"10:30"`) and duration strings (`"1ms"`) and converts them before validation."""

    dataset: PathRef
    #: The instrument by id or by symbol (one of them); with several session dates, `session_date` picks the day.
    instrument_id: NonNegInt | None = None
    symbol: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    session_date: DateStr | None = None
    #: Episode window; null = the instrument's session open / close (`qt.instruments` open_s / close_s).
    start_ns: int | None = None
    end_ns: int | None = None
    #: One-way order-entry latency applied to every submit and cancel (the shadow `latency_ns`, SPEC 3.1).
    latency_ns: NonNegInt = 0
    #: Extra per-order latency drawn uniformly from [0, jitter_ns] (deterministic in `seed` and the order id).
    jitter_ns: NonNegInt = 0
    #: The agent sees market data and its own order states `feed_latency_ns` late (visible cut = clock - this).
    feed_latency_ns: NonNegInt = 0
    seed: NonNegInt = 0
    depth_levels: Annotated[int, Field(ge=1, le=50)] = 5
    #: Most recent prints listed per observation (the summary counts all of them).
    max_trades: Annotated[int, Field(ge=0, le=1000)] = 20
    fees: FeeSchedule = Field(default_factory=_default_fees)
    markout_horizons_ns: Annotated[list[PosInt], Field(min_length=1, max_length=16)] = Field(
        default_factory=lambda: list(DEFAULT_HORIZONS_NS)
    )
    #: Market orders become IOC limit orders this far through the opposite best visible at decision time.
    market_collar_bps: Annotated[float, Field(gt=0, le=10_000)] = 100.0
    max_order_qty: PosInt | None = None
    #: Bound on |position + open quantity on one side| in size units.
    max_position: NonNegInt | None = None
    max_open_orders: Annotated[int, Field(ge=1, le=10_000)] = 100
    #: `reject_new`: an order that would cross one of your own working orders is rejected at decision time.
    self_trade_prevention: Literal["reject_new", "off"] = "reject_new"
    #: Hidden-print policy of the exact engine (SPEC 5.2.4a); null = venue default.
    hidden_print_policy: Literal["ignore", "behind", "behind_and_through"] | None = None
    max_steps: Annotated[int, Field(ge=1, le=1_000_000)] = 100_000

    @field_validator("markout_horizons_ns")
    @classmethod
    def _increasing(cls, v: list[int]) -> list[int]:
        if any(b <= a for a, b in itertools.pairwise(v)):
            raise ValueError("markout_horizons_ns must be strictly increasing")
        return v

    @field_validator("market_collar_bps")
    @classmethod
    def _collar(cls, v: float) -> float:
        return _finite(v, "market_collar_bps")

    @model_validator(mode="after")
    def _window(self) -> SandboxSpec:
        if self.start_ns is not None and self.end_ns is not None and self.end_ns <= self.start_ns:
            raise ValueError("end_ns must be after start_ns")
        return self


# ---- document parts ---------------------------------------------------------------------------------------------


class InstrumentInfo(QtModel):
    """The traded instrument-day (`qt.instruments` row) with the session open and close as absolute ns."""

    instrument_id: int
    symbol: str
    session_date: DateStr
    venue: str
    rule_set: str
    tick_size: int
    lot_size: int
    price_exp: int
    size_exp: int
    contract_multiplier: float
    currency: str
    session_tz: str
    midnight_utc_ns: int
    open_ns: int
    close_ns: int


class BookState(QtModel):
    """The displayed book at the visible cut: best levels and the top `depth_levels` per side as
    `[price, size, count]`, best first. `ts_event` = time of the last event in view."""

    ts_event: int | None
    bid: LevelTuple | None
    ask: LevelTuple | None
    bids: list[LevelTuple]
    asks: list[LevelTuple]
    spread_ticks: int | None


class TradeView(QtModel):
    """One print (`T` record): aggressor side `B`/`A`, or `N` (none); kind as in `qt.trades`."""

    ts_event: int
    price: int
    size: int
    aggressor: Literal["B", "A", "N"]
    kind: Literal["displayed", "hidden", "cross"]


class TradeStats(QtModel):
    """Prints over an interval: count, volume (size units), `vwap_price` (a real price, float), high/low/last (raw
    fixed-point prices, like every other price field)."""

    n: int
    volume: int
    vwap_price: float | None
    high: int | None
    low: int | None
    last: int | None
    first_ts: int | None
    last_ts: int | None


OrderStatus = Literal["pending", "working", "filled", "cancelled", "rejected", "expired", "censored"]


class OrderView(QtModel):
    """An order as the agent knows it at the visible cut. `pending` = sent, not yet confirmed at the venue (the
    first market event after its arrival confirms it, SPEC 5.2.1); `working` = resting (queue fields set);
    `expired` = still open at the end of the data; `censored` = the venue cleared the book or the feed had a gap."""

    order_id: OrderId
    client_order_id: ClientOrderId | None
    side: Literal["buy", "sell"]
    type: Literal["limit", "market"]
    tif: Literal["day", "ioc"]
    price: int | None
    peg_offset_ticks: int | None
    qty: int
    filled_qty: int
    leaves_qty: int
    status: OrderStatus
    reason: str | None
    ts_submit: int
    ts_activated: int | None
    ts_done: int | None
    queue_ahead: int | None
    rank: int | None
    level_qty: int | None
    cancel_sent_ns: int | None
    n_fills: int
    avg_fill_price: float | None


class FillView(QtModel):
    """One fill of one of your orders (a `qt.shadow_fills` row). `ts_known` = when the sandbox could know it: the
    fill's event, or for an arrival (taker) fill the first event after arrival. `step` = the step that reported it
    (null: after the session's last visible cut, reported only by the scorecard)."""

    order_id: OrderId
    fill_seq: int
    ts_event: int
    ts_known: int
    side: Literal["buy", "sell"]
    price: int
    qty: int
    liquidity: Literal["maker", "taker"]
    reason: Literal["queue_depleted", "trade_through", "crossed", "crossed_at_live"]
    queue_ahead_before: int
    fee: float
    step: int | None


class MarkoutView(QtModel):
    """Markout of one fill at one horizon against the mid (SPEC 5.7.5): `d (mid(t+h) - P)` in ticks, basis points
    and currency (positive = the fill was good for you). Listed once `t + h` is visible."""

    order_id: OrderId
    fill_seq: int
    horizon_ns: int
    mo_ticks: float | None
    mo_bps: float | None
    mo_ccy: float | None


class PositionView(QtModel):
    """Position (size units, signed), cash and fees (currency) from the fills so far, marked at `mark_price` (the mid
    at the visible cut, else the last print). `pnl_gross = cash + position * mark`, `pnl_net = pnl_gross - fees`."""

    qty: int
    bought_qty: int
    sold_qty: int
    cash: float
    fees: float
    mark_price: float | None
    mark_source: Literal["mid", "last_trade"] | None
    pnl_gross: float | None
    pnl_net: float | None
    currency: str


# ---- documents --------------------------------------------------------------------------------------------------


class SandboxObservation(QtDoc):
    """What the agent sees after `open`, `observe` or `advance`: the market and its own orders at the visible cut,
    and what happened since the previous step (prints, fills, markouts that became computable)."""

    SCHEMA: ClassVar[str] = "qt.sandbox_observation"
    schema_: Literal["qt.sandbox_observation"] = _tag("qt.sandbox_observation")
    schema_version: Literal[1] = 1
    step: int
    clock_ns: int
    visible_ns: int
    time_local: str
    done: bool
    book: BookState
    trades: list[TradeView]
    trades_since_last: TradeStats
    session_trades: TradeStats
    orders: list[OrderView]
    n_orders: int
    n_open: int
    fills: list[FillView]
    markouts: list[MarkoutView]
    position: PositionView
    warnings: list[str]


class SandboxSession(QtDoc):
    """`open` result: the episode, the instrument, the rules of the simulation and the first observation."""

    SCHEMA: ClassVar[str] = "qt.sandbox_session"
    schema_: Literal["qt.sandbox_session"] = _tag("qt.sandbox_session")
    schema_version: Literal[1] = 1
    session_id: str | None
    engine_version: str
    spec_id: Hex16
    dataset_id: Sha256
    spec: dict[str, Any]
    instrument: InstrumentInfo
    start_ns: int
    end_ns: int
    rules: list[str]
    observation: SandboxObservation


class SandboxAck(QtDoc):
    """`submit` / `cancel` result: the order as now known and the receipt of the committed log entry."""

    SCHEMA: ClassVar[str] = "qt.sandbox_ack"
    schema_: Literal["qt.sandbox_ack"] = _tag("qt.sandbox_ack")
    schema_version: Literal[1] = 1
    action: Literal["submit", "cancel"]
    clock_ns: int
    order: OrderView
    duplicate: bool
    log_seq: int
    log_hash: Sha256
    warnings: list[str]


class FillRateRow(QtModel):
    """Fill rates of one order class over the orders that reached the venue (`n_live`)."""

    order_class: Literal["passive_limit", "marketable_limit", "ioc_limit", "market", "all"]
    n_orders: int
    n_live: int
    n_any_fill: int
    n_full_fill: int
    qty_live: int
    filled_qty: int
    rate_any: float | None
    rate_full: float | None
    rate_qty: float | None
    median_ttf_ns: int | None


class PnLView(QtModel):
    """Episode PnL: cash flows of every fill (including fills while the final cancels were in flight), the end
    position marked at the mid at `mark_ns` (else the last print), fees by liquidity, and the conservative mark at
    the touch (long at the bid, short at the ask)."""

    currency: str
    position_end: int
    bought_qty: int
    sold_qty: int
    cash: float
    mark_ns: int
    mark_price: float | None
    mark_source: Literal["mid", "last_trade"] | None
    pnl_gross: float | None
    fees: float
    fees_maker: float
    fees_taker: float
    pnl_net: float | None
    pnl_net_at_touch: float | None
    notional_traded: float


class MarkoutRow(QtModel):
    """Markouts at one horizon over fills of one liquidity (`all`, `maker`, `taker`): counts, means and the total in
    currency. `n_adverse` counts fills whose markout is negative (the price moved against you)."""

    horizon_ns: int
    liquidity: Literal["all", "maker", "taker"]
    n: int
    n_null: int
    mean_ticks: float | None
    mean_bps: float | None
    total_ccy: float | None
    n_adverse: int


class AdverseSelection(QtModel):
    """Adverse selection of passive (maker) fills at each horizon: `cost_ccy = -sum(maker mo_ccy)` (positive = the
    market moved against your resting orders after they filled) and the share of maker fills with a negative
    markout."""

    horizon_ns: int
    n_maker_fills: int
    cost_ccy: float | None
    mean_ticks: float | None
    adverse_fraction: float | None


class ExecutionCost(QtModel):
    """Execution versus the mid you saw when you decided: `edge_ccy = sum d (mid_decision - P) q` (positive = you
    bought below / sold above that mid), by liquidity, over fills whose decision-time mid existed."""

    n_fills: int
    edge_ccy: float | None
    edge_maker_ccy: float | None
    edge_taker_ccy: float | None
    mean_edge_ticks: float | None


class Consistency(QtModel):
    """Runtime proof that the step-by-step session equals one exact replay of the committed orders: every fill
    reported during the session is a row of the single full replay (all columns equal), every fill of that replay
    that was knowable by the last visible cut was reported, at the step whose cut first covered it, and every order
    was shown working and shown ended at the steps whose cuts first covered its activation and the event that decided
    its end, with the replay's end (`statuses_mismatched` counts the orders where any of this differs)."""

    method: str
    steps: int
    window_replays: int
    fills_reported: int
    fills_matched: int
    fills_unreported: int
    fills_misattributed: int
    fills_after_last_cut: int
    statuses_checked: int
    statuses_mismatched: int
    timestamps_monotone: bool
    equal: bool


class Diagnostics(QtModel):
    """Simulation limits made visible: your orders never see each other (SPEC 1.2), so two of them working at the
    same price both assume the full queue, taker fills in one event can take the same displayed size twice, and a
    buy and a sell at crossing prices would have traded with each other."""

    same_price_overlaps: int
    taker_same_event_overlaps: int
    self_cross_pairs: int
    out_of_session_orders: int
    rejected_instructions: int


class Certificate(QtModel):
    """What a third party needs to recompute this scorecard: the dataset, the engine, the spec identity and the root
    of the committed log; `scorecard_sha256` covers the scorecard without `certificate`, `timings` and `files`."""

    dataset_id: Sha256
    engine_version: str
    spec_id: Hex16
    log_root: Sha256
    run_id: Hex16
    scorecard_sha256: Sha256


class SandboxScorecard(QtDoc):
    """`finish` result: PnL after fees, fill rates, markouts and adverse selection, the final orders and fills, the
    runtime consistency proof and the certificate."""

    SCHEMA: ClassVar[str] = "qt.sandbox_scorecard"
    schema_: Literal["qt.sandbox_scorecard"] = _tag("qt.sandbox_scorecard")
    schema_version: Literal[1] = 1
    run_id: Hex16
    spec_id: Hex16
    engine_version: str
    dataset_id: Sha256
    instrument: InstrumentInfo
    start_ns: int
    end_ns: int
    finish_ns: int
    steps: int
    orders: list[OrderView]
    fills: list[FillView]
    fill_rates: list[FillRateRow]
    pnl: PnLView
    markouts: list[MarkoutRow]
    adverse_selection: list[AdverseSelection]
    execution_cost: ExecutionCost
    consistency: Consistency
    diagnostics: Diagnostics
    data_quality: str
    warnings: list[str]
    certificate: Certificate
    timings: dict[str, float]
    files: list[FileEntry]


class SandboxFinish(QtDoc):
    """`qt_sandbox_finish` result: the scorecard without its per-order and per-fill arrays (so a tool result stays
    small however long the episode), with the counts of orders and fills and the certificate; the full scorecard and
    order log are files the server wrote (`out_dir`, `files`) or, on request, inline (`scorecard`)."""

    SCHEMA: ClassVar[str] = "qt.sandbox_finish"
    schema_: Literal["qt.sandbox_finish"] = _tag("qt.sandbox_finish")
    schema_version: Literal[1] = 1
    session_id: str | None
    run_id: Hex16
    spec_id: Hex16
    engine_version: str
    dataset_id: Sha256
    instrument: InstrumentInfo
    start_ns: int
    end_ns: int
    finish_ns: int
    steps: int
    n_orders: int
    n_fills: int
    n_fills_maker: int
    pnl: PnLView
    fill_rates: list[FillRateRow]
    markouts: list[MarkoutRow]
    adverse_selection: list[AdverseSelection]
    execution_cost: ExecutionCost
    consistency: Consistency
    diagnostics: Diagnostics
    data_quality: str
    warnings: list[str]
    certificate: Certificate
    out_dir: str | None
    files: list[FileEntry]
    scorecard: SandboxScorecard | None


class SessionItem(QtModel):
    """One open sandbox session of an MCP server: its instrument-day, clock, step, orders and seconds since its last
    tool call."""

    session_id: str
    instrument_id: int
    symbol: str
    session_date: DateStr
    clock_ns: int
    time_local: str
    step: int
    n_orders: int
    n_open: int
    idle_s: float


class SandboxSessionList(QtDoc):
    """`qt_sandbox_list` / `qt_sandbox_close` result: the server's open sessions, its limits, and the session just
    closed (`closed`, `qt_sandbox_close` only)."""

    SCHEMA: ClassVar[str] = "qt.sandbox_session_list"
    schema_: Literal["qt.sandbox_session_list"] = _tag("qt.sandbox_session_list")
    schema_version: Literal[1] = 1
    sessions: list[SessionItem]
    max_sessions: int
    idle_timeout_s: float | None
    closed: SessionItem | None


LogKind = Literal["open", "submit", "cancel", "reject", "advance", "finish"]


class LogEntry(QtModel):
    """One committed log entry. `clock_ns` = the agent's clock when it acted, `visible_ns` / `rows_seen` = the data
    it had seen (the visible cut and the number of source records at or before it); `data` holds the instruction
    (submit, cancel), the error (reject) or the digest of what the step reported (advance, finish). `hash =
    sha256(canonical_json(entry without hash))`, chained through `prev`."""

    seq: NonNegInt
    kind: LogKind
    clock_ns: int
    visible_ns: int
    rows_seen: NonNegInt
    data: dict[str, Any]
    prev: Sha256
    hash: Sha256


class SandboxOrderLog(QtDoc):
    """The committed order log of an episode: every instruction in order with the information set it was made on,
    hash-chained; `root` = the last entry's hash."""

    SCHEMA: ClassVar[str] = "qt.sandbox_order_log"
    schema_: Literal["qt.sandbox_order_log"] = _tag("qt.sandbox_order_log")
    schema_version: Literal[1] = 1
    engine_version: str
    dataset_id: Sha256
    spec_id: Hex16
    spec: dict[str, Any]
    entries: list[LogEntry]
    root: Sha256


class SandboxVerification(QtDoc):
    """`verify` result: the hash chain, the header (engine version and spec id, committed by the `open` entry), the
    dataset identity (`dataset_id` and the sha256 of every partition file), a deterministic re-execution of the log
    on the dataset (every entry must reproduce with the same hash), plus the scorecard digest when one was given."""

    SCHEMA: ClassVar[str] = "qt.sandbox_verification"
    schema_: Literal["qt.sandbox_verification"] = _tag("qt.sandbox_verification")
    schema_version: Literal[1] = 1
    ok: bool
    run_id: Hex16 | None
    log_root: Sha256
    chain_ok: bool
    header_ok: bool
    dataset_ok: bool
    reexecution_ok: bool
    scorecard_ok: bool | None
    first_divergence: int | None
    problems: list[str]


#: Every sandbox document by schema name.
SANDBOX_DOC_MODELS: dict[str, type[QtDoc]] = {
    m.SCHEMA: m
    for m in (
        SandboxSession,
        SandboxObservation,
        SandboxAck,
        SandboxScorecard,
        SandboxOrderLog,
        SandboxVerification,
        SandboxFinish,
        SandboxSessionList,
    )
}
SANDBOX_SPEC_MODELS: dict[str, type[SpecModel]] = {"qt.sandbox_spec": SandboxSpec}


def json_schemas() -> dict[str, dict[str, Any]]:
    """Draft 2020-12 JSON Schema of every sandbox document and of the spec, keyed by `qt.*` name."""
    from queuetruth.schemas.export import json_schema

    return {
        name: json_schema(name, model) for name, model in [*SANDBOX_DOC_MODELS.items(), *SANDBOX_SPEC_MODELS.items()]
    }


def parse_sandbox_document(data: dict[str, Any]) -> QtDoc:
    """Validates a sandbox document by its `schema` field (`ValueError` for an unknown name)."""
    name = data.get("schema")
    model = SANDBOX_DOC_MODELS.get(name) if isinstance(name, str) else None
    if model is None:
        raise ValueError(f"unknown sandbox document schema {name!r}")
    return model.model_validate(data)


__all__ = [
    "DEFAULT_HORIZONS_NS",
    "SANDBOX_DOC_MODELS",
    "SANDBOX_SPEC_MODELS",
    "AdverseSelection",
    "BookState",
    "Certificate",
    "Consistency",
    "Diagnostics",
    "ExecutionCost",
    "FeeSchedule",
    "FillRateRow",
    "FillView",
    "InstrumentInfo",
    "LogEntry",
    "MarkoutRow",
    "MarkoutView",
    "OrderView",
    "PnLView",
    "PositionView",
    "SandboxAck",
    "SandboxFinish",
    "SandboxObservation",
    "SandboxOrderLog",
    "SandboxScorecard",
    "SandboxSession",
    "SandboxSessionList",
    "SandboxSpec",
    "SandboxVerification",
    "SessionItem",
    "TradeStats",
    "TradeView",
    "json_schemas",
    "parse_sandbox_document",
]
