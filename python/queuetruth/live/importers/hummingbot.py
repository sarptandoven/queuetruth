"""Hummingbot importers: the trade and order records of a live bot and of a paper-trade bot -> canonical live
logs, a pairing of the two runs, `score` and a per-order explanation of the differences
(`queuetruth.live.importers.freqtrade` holds the shared explanation code).

Formats (read from Hummingbot's public source, `master` at commit 9af100d6822d, version 2.17.0):

- The bot database (`data/<config>.sqlite`, `hummingbot/model/`): table `Order` (`id` = client order id,
  `market`, `symbol` `BASE-QUOTE`, `creation_timestamp` ms, `order_type` `LIMIT` / `LIMIT_MAKER` / `MARKET`,
  `amount`, `price`, `last_status`, `exchange_order_id`, `position`), table `OrderStatus` (`order_id`,
  `timestamp` ms, `status` = the `MarketEvent` name: `BuyOrderCreated`, `SellOrderCreated`, `OrderFilled`,
  `OrderCancelled`, `OrderFailure`, `OrderExpired`, `BuyOrderCompleted`, `SellOrderCompleted`) and table
  `TradeFill` (`market`, `symbol`, `timestamp` ms, `order_id`, `trade_type` `BUY` / `SELL`, `order_type`, `price`,
  `amount`, `trade_fee` JSON, `trade_fee_in_quote`, `exchange_trade_id`, `position`). `price`, `amount` and
  `trade_fee_in_quote` are `SqliteDecimal(6)` columns: SQLite integers holding the value times 10^6 (truncated
  to 6 decimals when written); REAL values (databases from before that type) are read as decimals.
- The trades CSV (`data/trades_<config>.csv`, `MarketsRecorder.append_to_csv`): one row per fill with the
  `TradeFill.attribute_names_for_file_export` columns plus `age`; values in real units (full precision),
  `trade_fee` as JSON or Python dict text. A CSV alone has no unfilled orders and no creation times
  (`coverage = fills_only`; pairing refuses it). A fill in both the database and the CSV keeps the CSV copy.

Times are the bot's clock (`creation_timestamp` and fills: the connector's clock tick, `ClientOrderTracker`;
status rows: the recorder's clock): `submit_sent` at `creation_timestamp` (local send), fills, cancels
(`cancel_ack`), failures (`reject`) and expiries at their row time as local receive times, all `ms`.
`LIMIT_MAKER` orders are post-only; `MARKET` fills are taker, `LIMIT_MAKER` fills maker, others unknown.
The side comes from the `*OrderCreated` status or the fills; an order with neither is excluded (counted).
`*OrderCompleted` rows carry no event (the fills do). Fees: `trade_fee_in_quote` in the quote asset, else a
single flat fee; `MarketsRecorder` stores `trade_fee_in_quote = 0` when its conversion fails, so a 0 next to a
non-zero `trade_fee` is recomputed (percent times price times amount when charged in the quote, else the single
flat fee; warned).

Paper trading: a paper-trade connector's market name is `<exchange>_PaperTrade` (`PaperTradeExchange.display_name`).
It fills a resting limit order in full when the opposite best price of its order book reaches the order's price,
or a public trade prints strictly through it (`c_process_crossed_limit_orders`, `c_match_trade_to_limit_orders`):
no queue position. A paper database imported as the live arm is marked `validation_status = simulated`.

Pairing (`pair_orders`): a live and a paper bot running the same strategy have no shared order ids. A paper order
pairs with the unpaired live order of the same symbol and side whose price is within `pair_price_tol` (default 0:
equal) and whose creation time is nearest, within `pair_window_ms` (default 1000) of it (live orders bucketed by
symbol and side, sorted by creation time: O(n log n)). Paired paper orders take the live `order_key`; others keep
`sim:hummingbot:<account>:<client order id>`. `reconcile_hummingbot(sim, live, out)` takes the arms in the order of
`reconcile_freqtrade`; option `market` names the live market there and in `import_sim("hummingbot", ...)` (the
paper arm is `<market>_PaperTrade`).
"""

from __future__ import annotations

import ast
import bisect
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import (
    Record,
    dec,
    epoch_ns,
    input_error,
    make_order_key,
    read_csv_rows,
    read_text,
)
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.imap import InstrumentMap, load_instrument_map
from queuetruth.live.importers._base import ImportOptions
from queuetruth.live.importers.freqtrade import (
    OrderView,
    ReconcileReport,
    auto_instrument_map,
    check_options,
    dtext,
    explain_pairs,
    finish_to_dir,
    num,
    opt_int,
    reconcile_run_id,
    reconcile_spec,
    run_score,
    summarize,
    write_reconcile,
)
from queuetruth.schemas.io import sha256_file

HB_SIM = "sim:hummingbot"
#: Hummingbot sources pinned for the fixtures and the format notes above.
HUMMINGBOT_COMMIT = "9af100d6822da7d2d0291a906c730ef172284ee2"
_NS = 1_000_000_000
_PAPER_SUFFIXES = ("_papertrade", "_paper_trade")
_CREATED = {"buyordercreated": "buy", "sellordercreated": "sell"}
HB_OPTIONS = frozenset(
    {"market", "pair_with", "pair_window_ms", "pair_price_tol", "price_exp", "size_exp", "venue", "map_venue"}
)


@dataclass
class HbFill:
    """One Hummingbot trade fill."""

    ts_ns: int
    qty: Decimal
    px: Decimal
    trade_id: str
    fee: Decimal | None
    fee_ccy: str | None
    rec: Record
    #: from a trades CSV (full precision) rather than the database (`SqliteDecimal(6)`, truncated).
    from_csv: bool = False


@dataclass
class HbOrder:
    """One Hummingbot order with its status rows and fills."""

    cid: str
    market: str
    symbol: str
    side: str | None
    order_type: str
    qty: Decimal | None
    px: Decimal | None
    created_ns: int | None
    exchange_order_id: str | None
    position: str | None
    rec: Record
    record: bool
    statuses: list[tuple[int, str, Record]] = field(default_factory=list)
    fills: list[HbFill] = field(default_factory=list)
    key: str | None = None

    @property
    def filled(self) -> Decimal:
        total = Decimal(0)
        for f in self.fills:
            total += f.qty
        return total


@dataclass
class HbRun:
    """Every order of one Hummingbot bot run (one market)."""

    market: str
    paper: bool
    orders: list[HbOrder]
    paths: list[str]
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def venue(self) -> str:
        return _strip_paper(self.market)

    @property
    def fills_only(self) -> list[HbOrder]:
        """Orders known only from fills (a trades CSV, or fills without an Order row): no creation time."""
        return [o for o in self.orders if o.created_ns is None]

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + n


def _strip_paper(market: str) -> str:
    m = market.lower()
    for s in _PAPER_SUFFIXES:
        if m.endswith(s):
            return m[: -len(s)]
    return m


def is_paper(market: str) -> bool:
    """True for a paper-trade connector's market name (`binance_PaperTrade`)."""
    return market.lower().endswith(_PAPER_SUFFIXES)


def _scaled(v: Any, what: str, loc: str, scale: int = 6) -> Decimal | None:
    """`SqliteDecimal(scale)` integer -> exact decimal; REAL (older databases) and text read as decimals."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise input_error(loc, f"{what}: boolean {v!r} is not a number")
    if isinstance(v, int):
        return Decimal(v).scaleb(-scale).normalize() if v else Decimal(0)
    return num(v, what, loc)


def _ms(v: Any, what: str, loc: str) -> int:
    if v is None or (isinstance(v, str) and not v.strip()):
        raise input_error(loc, f"{what}: missing timestamp")
    if isinstance(v, float):
        v = int(v)
    return epoch_ns(v, "ms", what, loc)[0]


def _fee_doc(v: Any, loc: str) -> dict[str, Any] | None:
    if v is None or v == "":
        return None
    if isinstance(v, dict):
        return v
    text = str(v)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        try:
            doc = ast.literal_eval(text)
        except (ValueError, SyntaxError) as e:
            raise input_error(loc, f"trade_fee is neither JSON nor a dict literal: {text[:80]!r}") from e
    if not isinstance(doc, dict):
        raise input_error(loc, "trade_fee is not an object")
    return doc


def _fee(
    row: dict[str, Any], quote: str | None, qty: Decimal, px: Decimal, loc: str, *, scaled: bool
) -> tuple[Decimal | None, str | None, bool]:
    """(fee, currency, recomputed): `trade_fee_in_quote`, else the single flat fee. `MarketsRecorder` stores
    `trade_fee_in_quote = 0` when its conversion fails, so a 0 next to a non-zero `trade_fee` is recomputed: the
    percent fee times price times amount when it is charged in the quote (or its token is not named), else the single
    flat fee (`recomputed = True`)."""
    doc = _fee_doc(row.get("trade_fee"), loc) or {}
    flats = doc.get("flat_fees") or []
    flat = flats[0] if isinstance(flats, list) and len(flats) == 1 and isinstance(flats[0], dict) else None
    q = row.get("trade_fee_in_quote")
    if q not in (None, ""):
        v = _scaled(q, "trade_fee_in_quote", loc) if scaled else dec(q, "trade_fee_in_quote", loc)
        if v is not None and v != 0:
            return v, quote, False
        if v is not None:
            pct = dec(doc.get("percent"), "trade_fee percent", loc) if doc.get("percent") not in (None, "") else None
            token = doc.get("percent_token")
            if pct and (token in (None, "", quote)):
                return pct * px * qty, quote, True
            if flat is not None and dec(flat.get("amount"), "flat fee", loc):
                return dec(flat.get("amount"), "flat fee", loc), flat.get("token"), True
            return v, quote, False
    if flat is not None:
        return dec(flat.get("amount"), "flat fee", loc), flat.get("token"), False
    return None, None, False


def _otype(v: Any) -> str:
    return str(v or "").strip().upper()


def _side(v: Any) -> str | None:
    s = str(v or "").strip().lower()
    return s if s in ("buy", "sell") else None


def read_hb_sqlite(
    path: str | Path,
    run: dict[str, HbOrder],
    warnings: list[str],
    skipped: dict[str, int],
    notes: dict[str, int] | None = None,
) -> None:
    """Orders, status rows and fills of a Hummingbot database (read-only) into `run` (client id -> order)."""
    p = Path(path)
    try:
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise input_error(str(p), f"cannot open SQLite database: {e}") from e
    try:
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError as e:
            raise input_error(str(p), f"not a readable SQLite database: {e}") from e
        if "Order" not in tables and "TradeFill" not in tables:
            raise input_error(
                str(p), f"no Order / TradeFill table (not a Hummingbot database; tables: {sorted(tables)})"
            )
        orders = (
            [dict(r) for r in con.execute('SELECT * FROM "Order" ORDER BY creation_timestamp, id')]
            if ("Order" in tables)
            else []
        )
        statuses = (
            [dict(r) for r in con.execute('SELECT * FROM "OrderStatus" ORDER BY timestamp, id')]
            if "OrderStatus" in tables
            else []
        )
        fills = (
            [dict(r) for r in con.execute('SELECT * FROM "TradeFill" ORDER BY timestamp, exchange_trade_id')]
            if "TradeFill" in tables
            else []
        )
    except sqlite3.OperationalError as e:
        raise input_error(str(p), f"unexpected Hummingbot schema: {e}") from e
    finally:
        con.close()
    if "Order" not in tables:
        warnings.append(f"{p}: no Order table: fills only (coverage=fills_only)")
    for i, o in enumerate(orders):
        loc = f"{p}:Order#{o.get('id')}"
        cid = o.get("id")
        if cid in (None, ""):
            raise input_error(loc, "order without id")
        if cid in run:
            skipped["duplicate Order row"] = skipped.get("duplicate Order row", 0) + 1
            continue
        px = _scaled(o.get("price"), "price", loc)
        otype = _otype(o.get("order_type"))
        run[str(cid)] = HbOrder(
            cid=str(cid),
            market=str(o.get("market") or ""),
            symbol=str(o.get("symbol") or ""),
            side=None,
            order_type=otype,
            qty=_scaled(o.get("amount"), "amount", loc),
            px=None if otype == "MARKET" or px is None or px == 0 else px,
            created_ns=_ms(o.get("creation_timestamp"), "creation_timestamp", loc),
            exchange_order_id=str(o["exchange_order_id"]) if o.get("exchange_order_id") not in (None, "") else None,
            position=o.get("position"),
            rec=Record(o, json.dumps(o, sort_keys=True, default=str), loc, 0, i),
            record=True,
        )
    for i, s in enumerate(statuses):
        loc = f"{p}:OrderStatus#{s.get('id')}"
        o = run.get(str(s.get("order_id")))
        if o is None:
            skipped["status row of an order without an Order row"] = (
                skipped.get("status row of an order without an Order row", 0) + 1
            )
            continue
        o.statuses.append(
            (
                _ms(s.get("timestamp"), "timestamp", loc),
                str(s.get("status") or ""),
                Record(s, json.dumps(s, sort_keys=True, default=str), loc, 0, 100_000 + i),
            )
        )
    for i, f in enumerate(fills):
        _add_fill(run, f, f"{p}:TradeFill#{f.get('exchange_trade_id')}", i, scaled=True, notes=notes or {})


def _add_fill(
    run: dict[str, HbOrder], f: dict[str, Any], loc: str, i: int, *, scaled: bool, notes: dict[str, int]
) -> None:
    cid = f.get("order_id")
    if cid in (None, ""):
        raise input_error(loc, "fill without order_id")
    tid = f.get("exchange_trade_id")
    if tid in (None, ""):
        raise input_error(loc, "fill without exchange_trade_id")
    rd = _scaled if scaled else (lambda v, w, lc: dec(v, w, lc))
    qty = rd(f.get("amount"), "amount", loc)
    px = rd(f.get("price"), "price", loc)
    if qty is None or qty <= 0 or px is None or px <= 0:
        raise input_error(loc, "fill price and amount must be positive")
    symbol = str(f.get("symbol") or "")
    quote = f.get("quote_asset") or (symbol.split("-", 1)[1] if "-" in symbol else None)
    fee, ccy, recomputed = _fee(f, quote, qty, px, loc, scaled=scaled)
    if recomputed:
        notes["fee recomputed"] = notes.get("fee recomputed", 0) + 1
    rec = Record(f, json.dumps(f, sort_keys=True, default=str), loc, 0, 200_000 + i)
    o = run.get(str(cid))
    if o is None:
        o = HbOrder(
            cid=str(cid),
            market=str(f.get("market") or ""),
            symbol=symbol,
            side=None,
            order_type=_otype(f.get("order_type")),
            qty=None,
            px=None,
            created_ns=None,
            exchange_order_id=None,
            position=f.get("position"),
            rec=rec,
            record=False,
        )
        run[str(cid)] = o
    side = _side(f.get("trade_type"))
    if side is None:
        raise input_error(loc, f"trade_type {f.get('trade_type')!r} is not BUY / SELL")
    if o.side is not None and o.side != side:
        raise input_error(loc, f"fill side {side} contradicts order {cid} side {o.side}")
    o.side = side
    fill = HbFill(_ms(f.get("timestamp"), "timestamp", loc), qty, px, str(tid), fee, ccy, rec, from_csv=not scaled)
    for j, x in enumerate(o.fills):
        if x.trade_id == str(tid):
            # one fill in both the database and the CSV: the CSV copy wins (the database truncates to 6 decimals)
            if fill.from_csv and not x.from_csv:
                o.fills[j] = fill
            return
    o.fills.append(fill)


def read_hb_csv(path: str | Path, run: dict[str, HbOrder], notes: dict[str, int] | None = None) -> None:
    """Fills of a Hummingbot trades CSV into `run`."""
    p = Path(path)
    header, rows = read_csv_rows(read_text(p))
    need = ("exchange_trade_id", "market", "symbol", "timestamp", "order_id", "trade_type", "price", "amount")
    miss = [c for c in need if c not in header]
    if miss:
        raise input_error(str(p), f"not a Hummingbot trades CSV: missing column(s) {miss}")
    for i, r in enumerate(rows):
        row = {header[j]: (r[j] if j < len(r) else "") for j in range(len(header))}
        _add_fill(run, row, f"{p}:{i + 2}", i, scaled=False, notes=notes if notes is not None else {})


def read_hb_run(paths: list[str], *, market: str | None = None, paper_of: str | None = None) -> HbRun:
    """A Hummingbot run (databases and / or trades CSVs of one bot) restricted to one market: `market` names it;
    `paper_of` names the live market whose paper-trade market (`<market>_PaperTrade`) to take, else that market
    itself."""
    orders: dict[str, HbOrder] = {}
    warnings: list[str] = []
    skipped: dict[str, int] = {}
    notes: dict[str, int] = {}
    for p in paths:
        pp = Path(p)
        try:
            head = pp.read_bytes()[:16]
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{p}: {e}") from e
        if head.startswith(b"SQLite format 3\x00"):
            read_hb_sqlite(pp, orders, warnings, skipped, notes)
        else:
            read_hb_csv(pp, orders, notes)
    markets = sorted({o.market for o in orders.values() if o.market})
    if market is None and paper_of is not None:
        papers = [m for m in markets if is_paper(m) and _strip_paper(m) == paper_of.lower()]
        same = [m for m in markets if m.lower() == paper_of.lower()]
        market = papers[0] if len(papers) == 1 else (same[0] if same else f"{paper_of}_PaperTrade")
    if market is None:
        if len(markets) > 1:
            raise QtError("QT-CONFIG-001", f"orders on markets {markets}; pass option market=<name> (one per log)")
        market = markets[0] if markets else "hummingbot"
    kept = [o for o in orders.values() if o.market.lower() == market.lower()]
    other = len(orders) - len(kept)
    run = HbRun(market=market, paper=is_paper(market), orders=kept, paths=[str(p) for p in paths])
    run.warnings.extend(warnings)
    if notes.get("fee recomputed"):
        run.warn(
            f"{notes['fee recomputed']} fill(s) with trade_fee_in_quote = 0 next to a non-zero trade_fee (Hummingbot "
            "stores 0 when its fee conversion fails): fee recomputed from trade_fee"
        )
    for k, v in skipped.items():
        run.skip(k, v)
    if other:
        run.warn(f"{other} order(s) on other markets than {market} left out")
    for o in kept:
        created = next((_CREATED[s.lower()] for _, s, _ in o.statuses if s.lower() in _CREATED), None)
        if created is not None and o.side is not None and created != o.side:
            raise input_error(o.rec.location, f"order {o.cid}: {created} order (created status) with {o.side} fills")
        o.side = o.side or created
        o.statuses.sort(key=lambda x: (x[0], x[2].line_idx))
        o.fills.sort(key=lambda f: (f.ts_ns, f.trade_id))
    run.orders.sort(key=lambda o: (o.created_ns if o.created_ns is not None else (o.fills[0].ts_ns), o.cid))
    return run


# ---- pairing -------------------------------------------------------------------------------------------------------


def pair_orders(
    live: HbRun, sim: HbRun, *, window_ms: int = 1000, price_tol: Decimal | str | int = 0
) -> list[tuple[HbOrder | None, HbOrder | None]]:
    """Pairs paper (simulator) orders with live orders (rules in the module docstring). Live orders are bucketed by
    (symbol, side) and sorted by creation time, so each paper order scans only the live orders created within the
    window: O((L + S) log L). Orders without a creation time (fills only: a trades CSV) cannot pair."""
    tol = Decimal(str(price_tol))
    if window_ms < 0 or tol < 0:
        raise QtError("QT-CONFIG-001", "pair_window_ms and pair_price_tol must be >= 0")
    win = window_ms * 1_000_000
    buckets: dict[tuple[str, str], list[tuple[int, str, int]]] = {}
    for idx, lo in enumerate(live.orders):
        if lo.created_ns is not None and lo.side is not None:
            buckets.setdefault((lo.symbol, lo.side), []).append((lo.created_ns, lo.cid, idx))
    times: dict[tuple[str, str], list[int]] = {}
    for k, b in buckets.items():
        b.sort()
        times[k] = [x[0] for x in b]
    used: set[int] = set()
    pairs: list[tuple[HbOrder | None, HbOrder | None]] = []
    paired_sim: set[int] = set()
    for so in sim.orders:
        if so.created_ns is None or so.side is None:
            continue
        key = (so.symbol, so.side)
        b = buckets.get(key)
        if not b:
            continue
        ts = times[key]
        best: tuple[tuple[int, Decimal, int, str], int] | None = None
        for j in range(bisect.bisect_left(ts, so.created_ns - win), bisect.bisect_right(ts, so.created_ns + win)):
            created, cid, idx = b[j]
            if idx in used:
                continue
            lo = live.orders[idx]
            if (lo.px is None) != (so.px is None):
                continue
            dp = abs(lo.px - so.px) if lo.px is not None and so.px is not None else Decimal(0)
            if dp > tol:
                continue
            k2 = (abs(created - so.created_ns), dp, created, cid)
            if best is None or k2 < best[0]:
                best = (k2, idx)
        if best is not None:
            used.add(best[1])
            paired_sim.add(id(so))
            pairs.append((live.orders[best[1]], so))
    pairs += [(lo, None) for idx, lo in enumerate(live.orders) if idx not in used]
    pairs += [(None, so) for so in sim.orders if id(so) not in paired_sim]
    return pairs


# ---- canonical rows ------------------------------------------------------------------------------------------------


def _check_opts(extra: dict[str, Any]) -> None:
    check_options(extra, HB_OPTIONS, "hummingbot")


def emit_hb(
    b: LogBuilder,
    run: HbRun,
    *,
    account: str,
    sim: bool,
    sim_live: dict[int, HbOrder] | None = None,
) -> None:
    """Adds the orders of a Hummingbot run to a builder (events as in the module docstring)."""
    src = "sim" if sim else "rest"
    no_side = 0
    for o in run.orders:
        if o.side is None:
            no_side += 1
            continue
        ident = o.exchange_order_id or o.cid
        if sim:
            live = (sim_live or {}).get(id(o))
            key = live.key if live is not None and live.key else make_order_key(HB_SIM, account, o.cid)
        else:
            key = make_order_key(b.venue, account, ident)
        o.key = key
        ot = {"LIMIT": "limit", "LIMIT_MAKER": "limit", "MARKET": "market"}.get(o.order_type, "other")
        maker = o.order_type == "LIMIT_MAKER"
        h = b.order(
            oid=o.exchange_order_id,
            cid=o.cid,
            rec=o.rec,
            record=o.record and o.qty is not None,
            qty=o.qty,
            qty_text=dtext(o.qty),
            px=o.px,
            px_text=dtext(o.px),
            instrument=o.symbol,
            side=o.side,
            order_type=ot,
            tif=("post_only" if maker else "gtc") if ot == "limit" else "other",
            post_only=maker,
            reduce_only=True if str(o.position or "").upper() == "CLOSE" else None,
        )
        b.set_order_field(h, "key_override", key)
        cs: dict[str, Any] = {"source": src, "stream": False, "precision": "ms", "kind": "unknown"}
        if o.record and o.created_ns is not None:
            b.event(h, "submit_sent", rec=o.rec, ts_local_send=o.created_ns, qty=o.qty, px=o.px, **cs)
        liq = "taker" if o.order_type == "MARKET" else ("maker" if maker else "unknown")
        for f in o.fills:
            b.event(
                h,
                "fill",
                rec=f.rec,
                ts_local_recv=f.ts_ns,
                qty=f.qty,
                px=f.px,
                fill_id=f.trade_id,
                liquidity=liq,
                fee=f.fee,
                fee_ccy=f.fee_ccy,
                **cs,
            )
        for ts, status, rec in o.statuses:
            s = status.lower()
            if s in _CREATED or s == "orderfilled":
                continue
            if s in ("buyordercompleted", "sellordercompleted"):
                b.skip("*OrderCompleted status (the fills carry it)")
                continue
            et = {"ordercancelled": "cancel_ack", "orderfailure": "reject", "orderexpired": "expire"}.get(s)
            if et is None:
                b.skip(f"status {status}")
                continue
            b.event(h, et, rec=rec, ts_local_recv=ts, reason=status, **cs)
    if no_side:
        b.warn(f"{b.venue}: {no_side} order(s) without a side (no *OrderCreated status and no fill) excluded")


def _builder(
    venue: str, account: str, imap: InstrumentMap | None, opts: dict[str, Any], *, sim: bool, mv: str
) -> LogBuilder:
    return LogBuilder(
        venue,
        account=account,
        imap=imap,
        price_exp=opt_int(opts, "price_exp", -9),
        size_exp=opt_int(opts, "size_exp", -8),
        map_venue=mv,
        validation_status="simulated" if sim else "fixture_only",
        source_override="sim" if sim else None,
    )


def _finish(b: LogBuilder, run: HbRun) -> ImportResult:
    for w in run.warnings:
        b.warn(f"{b.venue}: {w}")
    for reason, n in run.skipped.items():
        b.skip(reason, n)
    if run.paper and b.validation_status != "simulated":
        b.validation_status = "simulated"
        b.warn(
            f"{b.venue}: paper trade market {run.market}: fills are Hummingbot paper-trade fills (no queue), "
            "validation_status = simulated"
        )
    return b.finish()


def import_hummingbot(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Hummingbot database / trades CSV of a live bot -> canonical rows (venue = the exchange market name)."""
    check_options(opts.extra, HB_OPTIONS - {"pair_with", "pair_window_ms", "pair_price_tol"}, "hummingbot")
    run = read_hb_run([str(p) for p in paths], market=opts.extra.get("market"))
    venue = str(opts.extra.get("venue") or run.venue)
    account = opts.account or "main"
    b = _builder(venue, account, opts.imap, opts.extra, sim=False, mv=str(opts.extra.get("map_venue") or venue))
    emit_hb(b, run, account=account, sim=False)
    return _finish(b, run)


def import_hummingbot_sim(paths: list[str], opts: ImportOptions) -> ImportResult:
    """Hummingbot paper-trade database / CSV -> canonical rows of venue `sim:hummingbot`; option `pair_with`
    (the live bot's database / CSV) gives paired orders the live `order_key`."""
    _check_opts(opts.extra)
    market = opts.extra.get("market")
    run = read_hb_run([str(p) for p in paths], paper_of=None if market is None else str(market))
    account = opts.account or "main"
    sim_live: dict[int, HbOrder] = {}
    pw = opts.extra.get("pair_with")
    live_venue = None
    pw_paths: list[str] = []
    if pw:
        pw_paths = [str(x) for x in ([pw] if isinstance(pw, (str, Path)) else pw)]
        live = read_hb_run(pw_paths, market=None if market is None else str(market))
        _require_orders(live, "pair_with (live)")
        _require_orders(run, "simulator")
        live_venue = str(opts.extra.get("venue") or live.venue)
        for o in live.orders:
            o.key = make_order_key(live_venue, account, o.exchange_order_id or o.cid)
        pairs = pair_orders(
            live,
            run,
            window_ms=opt_int(opts.extra, "pair_window_ms", 1000),
            price_tol=opts.extra.get("pair_price_tol", 0),
        )
        sim_live = {id(s): lo for lo, s in pairs if lo is not None and s is not None}
    mv = str(opts.extra.get("map_venue") or opts.extra.get("venue") or live_venue or run.venue)
    b = _builder(HB_SIM, account, opts.imap, opts.extra, sim=True, mv=mv)
    emit_hb(b, run, account=account, sim=True, sim_live=sim_live)
    if not pw:
        b.warn(f"{HB_SIM}: no pair_with inputs: simulator orders keep sim:hummingbot keys and match no live order")
    else:
        b.warn(f"{HB_SIM}: {len(sim_live)} of {len(run.orders)} simulator order(s) paired with a live order")
        refs = ", ".join(f"{x} (sha256 {sha256_file(x)[:16]})" for x in pw_paths)
        b.warn(
            f"{HB_SIM}: pair_with: keys taken from {refs} as venue {live_venue}, account {account}; import that live "
            "log with the same venue and account, or no key matches"
        )
    return _finish(b, run)


def import_hummingbot_live(
    inputs: list[str] | str,
    out: str | Path,
    *,
    account: str | None = None,
    instrument_map: str | None = None,
    append: bool = False,
    **options: Any,
) -> Any:
    """Hummingbot database / trades CSV -> canonical live-log directory `out`; returns the `LiveImportSummary`."""
    paths = [inputs] if isinstance(inputs, str) else [str(p) for p in inputs]
    for p in paths:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    imap = load_instrument_map(instrument_map) if instrument_map else None
    res = import_hummingbot(paths, ImportOptions(account=account, imap=imap, extra=dict(options)))
    status = "simulated" if any(o["validation_status"] == "simulated" for o in res.orders) else "fixture_only"
    return finish_to_dir(res.venue, res, paths, Path(out), append=append, status=status)


# ---- reconcile ------------------------------------------------------------------------------------------------------


def _view(o: HbOrder) -> OrderView:
    filled = o.filled
    avg = None
    if filled > 0:
        notional = Decimal(0)
        for f in o.fills:
            notional += f.qty * f.px
        avg = notional / filled
        avg = avg.quantize(Decimal(1).scaleb(-12)).normalize() if avg != avg.to_integral_value() else avg
    end = None
    status = "open"
    for ts, s, _ in o.statuses:
        k = s.lower()
        if k in ("ordercancelled", "orderfailure", "orderexpired"):
            end = ts
            status = {"ordercancelled": "cancelled", "orderfailure": "rejected", "orderexpired": "expired"}[k]
    if not o.record:
        status = "unknown (fills only)"
    assert o.side is not None
    return OrderView(
        key=o.key or o.cid,
        instrument=o.symbol,
        side=o.side,
        role="order",
        order_type=o.order_type.lower(),
        qty=o.qty if o.qty is not None else filled,
        limit_px=o.px,
        placed_ns=o.created_ns,
        filled_qty=filled,
        avg_px=avg,
        first_fill_ns=o.fills[0].ts_ns if o.fills else None,
        end_ns=end,
        status=status,
        note=None,
        group=o.cid,
    )


def _require_orders(run: HbRun, arm: str) -> None:
    """`QT-CONFIG-001` when no order of an arm has a creation time: a trades CSV alone (fills only) cannot pair."""
    if run.orders and not any(o.created_ns is not None for o in run.orders):
        raise QtError(
            "QT-CONFIG-001",
            f"{arm} inputs {run.paths} hold fills only (a trades CSV): pairing needs each order's creation time "
            "and side, which only the bot database (data/<config>.sqlite, table Order) has; pass the database",
        )


def _paths(x: list[str | Path] | str | Path) -> list[str]:
    return [str(x)] if isinstance(x, (str, Path)) else [str(p) for p in x]


#: below this share of live orders paired, the report warns that the two bots did not run in lockstep.
LOW_PAIRING_COVERAGE = 0.5


def reconcile_hummingbot(
    sim: list[str | Path] | str | Path,
    live: list[str | Path] | str | Path,
    out: str | Path,
    *,
    account: str = "main",
    instrument_map: str | Path | None = None,
    window_ns: int = 60 * _NS,
    price_tol_bps: float = 0.5,
    time_tol_ns: int = _NS,
    score: bool = True,
    arms_as_given: bool = False,
    **options: Any,
) -> ReconcileReport:
    """Paper-trade bot (`sim`) vs live bot (`live`), each its database (`data/<config>.sqlite`, optionally with its
    trades CSV): writes `<out>/live`, `<out>/sim`, `<out>/score` and `<out>/reconcile.json` /
    `reconcile_orders.csv`; returns the `ReconcileReport`. The argument order is the one of `reconcile_freqtrade`:
    simulator, live, out. A paper live arm with a non-paper simulator arm fails `QT-CONFIG-001` (swapped arguments)
    unless `arms_as_given`; an arm of trades CSVs alone fails `QT-CONFIG-001` (no creation times to pair by).
    `window_ns` is the scorer's horizon (default 60 s; set it to your `order_refresh_time`). Options: `market` (the
    live market; the paper arm uses `<market>_PaperTrade`), `pair_window_ms`, `pair_price_tol`, `venue`,
    `price_exp`, `size_exp`."""
    check_options(options, HB_OPTIONS - {"pair_with", "map_venue"}, "hummingbot reconcile")
    outp = Path(out)
    sp, lp = _paths(sim), _paths(live)
    for p in [*lp, *sp]:
        if not Path(p).exists():
            raise QtError("QT-INPUT-001", f"{p}: input not found")
    market = options.get("market")
    lv = read_hb_run(lp, market=None if market is None else str(market))
    sv = read_hb_run(sp, paper_of=str(market) if market is not None else None)
    warnings: list[str] = []
    if lv.paper and not sv.paper:
        if not arms_as_given:
            raise QtError(
                "QT-CONFIG-001",
                f"the live arm {lp} is a paper-trade market ({lv.market}) and the simulator arm {sp} is not "
                f"({sv.market}): arms swapped? (reconcile_hummingbot(sim, live, out); pass arms_as_given=True to "
                "score them as given)",
            )
        warnings.append(f"arms_as_given: the live arm is a paper-trade market ({lv.market})")
    elif lv.paper:
        lv.warn(f"live inputs are paper trades ({lv.market}): this compares two simulators")
    elif not sv.paper:
        sv.warn(f"simulator inputs are not paper trades ({sv.market}): scored as given")
    _require_orders(lv, "live")
    _require_orders(sv, "simulator")
    for arm, run in (("live", lv), ("simulator", sv)):
        if run.fills_only:
            warnings.append(
                f"{arm}: {len(run.fills_only)} order(s) known from fills only (CSV rows without an Order row) "
                "cannot pair"
            )
    venue = str(options.get("venue") or lv.venue)
    if instrument_map is not None:
        imap = load_instrument_map(str(instrument_map))
        small = sorted(
            e.instrument
            for e in imap.entries
            if e.venue == venue and Decimal(e.tick_size).scaleb(e.price_exp) < Decimal("0.000001")
        )
        if small:
            warnings.append(
                f"instrument_map: tick below 1e-6 for {small}: Hummingbot's database truncates prices and amounts "
                "to 6 decimals (SqliteDecimal(6)), so differences below that are invisible in both arms"
            )
    else:
        insts = {(venue, o.symbol) for o in [*lv.orders, *sv.orders]}
        imap = auto_instrument_map(
            sorted(insts), price_exp=opt_int(options, "price_exp", -9), size_exp=opt_int(options, "size_exp", -8)
        )
        warnings.append(
            "instrument_map: none given; instruments numbered 1..n in sorted order with default scales "
            "(price_exp -9, size_exp -8) so both logs share ids"
        )
    pairs = pair_orders(
        lv, sv, window_ms=opt_int(options, "pair_window_ms", 1000), price_tol=options.get("pair_price_tol", 0)
    )
    lb = _builder(venue, account, imap, options, sim=False, mv=venue)
    emit_hb(lb, lv, account=account, sim=False)
    lres = _finish(lb, lv)
    sb = _builder(HB_SIM, account, imap, options, sim=True, mv=venue)
    emit_hb(sb, sv, account=account, sim=True, sim_live={id(s): lo for lo, s in pairs if lo and s})
    sres = _finish(sb, sv)
    lsum = finish_to_dir(lres.venue, lres, lv.paths, outp / "live", status="simulated" if lv.paper else "fixture_only")
    ssum = finish_to_dir(HB_SIM, sres, sv.paths, outp / "sim", status="simulated")
    warnings += [f"live: {w}" for w in lsum.warnings] + [f"sim: {w}" for w in ssum.warnings]
    sc, err, classes = (None, "not requested", {})
    if score:
        sc, err, classes = run_score(outp / "live", outp / "sim", outp / "score", window_ns=window_ns)
    views = [
        (None if lo is None or lo.side is None else _view(lo), None if so is None or so.side is None else _view(so))
        for lo, so in pairs
    ]
    views = [v for v in views if v[0] is not None or v[1] is not None]
    rows = explain_pairs(
        views,
        price_tol_bps=price_tol_bps,
        time_tol_ns=time_tol_ns,
        sim_name="paper",
        live_name="live",
        no_sim_hint="the paper bot placed no order of this side and price within the pairing window",
        no_live_hint="the live bot placed no order of this side and price within the pairing window",
        score_classes=classes,
    )
    counts, adv, missed = summarize(rows)
    n_paired = sum(1 for lo, so in pairs if lo is not None and so is not None)
    n_live = len(lv.orders)
    coverage = None if n_live == 0 else n_paired / n_live
    if coverage is not None and coverage < LOW_PAIRING_COVERAGE:
        warnings.append(
            f"pairing: only {n_paired} of {n_live} live orders paired ({coverage:.0%}): the bots did not run in "
            "lockstep (start time, inventory skew, order_refresh_time, filled_order_delay) or pair_price_tol / "
            "pair_window_ms are too tight; unpaired orders say nothing about fills"
        )
    spec = reconcile_spec(
        "hummingbot",
        sp,
        lp,
        instrument_map=None if instrument_map is None else str(instrument_map),
        options=options,
        account=account,
        window_ns=int(window_ns),
        price_tol_bps=price_tol_bps,
        time_tol_ns=int(time_tol_ns),
        score=bool(score),
        arms_as_given=bool(arms_as_given),
    )
    report = ReconcileReport(
        kind="hummingbot",
        run_id=reconcile_run_id(spec),
        live_log="live",
        sim_log="sim",
        n_live_orders=n_live,
        n_sim_orders=len(sv.orders),
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


__all__ = [
    "HB_SIM",
    "HUMMINGBOT_COMMIT",
    "HbFill",
    "HbOrder",
    "HbRun",
    "import_hummingbot",
    "import_hummingbot_live",
    "import_hummingbot_sim",
    "is_paper",
    "pair_orders",
    "read_hb_csv",
    "read_hb_run",
    "read_hb_sqlite",
    "reconcile_hummingbot",
]
