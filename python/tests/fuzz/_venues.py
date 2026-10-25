"""Structure-aware record strategies of the JSON venue importers: each venue's message shapes (from the importer
module docstrings and the fixtures) with its field names, enum values from the venue documentation plus junk,
ids from small pools (so records join into orders) and hostile numbers and times (`fuzz._values`)."""

from __future__ import annotations

from typing import Any

from fuzz._values import (
    amounts,
    epoch_ms,
    epochs,
    field,
    ids,
    nasty,
    numbers_any,
    objects,
    rfc3339_texts,
    rfc3339_valid,
)
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BTC-USD", "BTC/USD", "BTC-PERPETUAL", "BTC", "ETH", ""]
SIDES = ["BUY", "SELL", "Buy", "Sell", "buy", "sell", "b", "s", "A", "B", "bid", "ask", "1", "2", "long", "x"]


#: Time fields of the venue records (required in plausible records).
TIME_KEYS = frozenset(
    {
        "E",
        "T",
        "time",
        "updatedTime",
        "createdTime",
        "execTime",
        "fillTime",
        "uTime",
        "cTime",
        "ts",
        "creation_timestamp",
        "last_update_timestamp",
        "timestamp",
        "statusTimestamp",
        "trade_time",
        "created_time",
        "creation_time",
    }
)


def venue_records(hostile: bool) -> dict[str, SearchStrategy[Any]]:
    """Record strategies per venue; `hostile=False` keeps every value plausible (imports then mostly succeed and
    exercise the order-building logic), `hostile=True` mixes in hostile values and junk records."""

    def F(values: SearchStrategy[Any] | list[Any], nasty_weight: int = 1) -> SearchStrategy[Any]:
        return field(values, nasty_weight if hostile else 0)

    def obj(fields: dict[str, SearchStrategy[Any]], required: dict[str, SearchStrategy[Any]] | None = None) -> Any:
        if not hostile:  # plausible records always carry their times (a missing time only ever fails the import)
            required = {**(required or {}), **{k: v for k, v in fields.items() if k in TIME_KEYS}}
            fields = {k: v for k, v in fields.items() if k not in TIME_KEYS}
        return objects(fields, required, hostile=hostile)

    N: SearchStrategy[Any] = nasty if hostile else st.nothing()
    E: SearchStrategy[Any] = epochs if hostile else epoch_ms
    local: SearchStrategy[Any] = epochs if hostile else epoch_ms.map(lambda t: t * 10**6 + 250_000)

    rfc = F(rfc3339_texts if hostile else rfc3339_valid)
    num = F(numbers_any if hostile else amounts)
    amt = F(amounts, nasty_weight=1)
    ep = F(E)
    boolish = F([True, False, "true", "false", "1", "0", "yes", "maybe", None])

    def _wrap_out(msg: SearchStrategy[Any]) -> SearchStrategy[Any]:
        return st.builds(lambda m, t: {"dir": "out", "local_ns": t, "msg": m}, msg, local)

    def _wrap_in(msg: SearchStrategy[Any]) -> SearchStrategy[Any]:
        return st.builds(lambda m, t: {"dir": "in", "local_ns": t, "msg": m}, msg, local)

    # ---- Binance --------------------------------------------------------------------------------------------------

    _BN_X = ["NEW", "TRADE", "CANCELED", "REPLACED", "REJECTED", "EXPIRED", "TRADE_PREVENTION", "AMENDMENT", "x"]
    _bn_exec = obj(
        {
            "E": ep,
            "s": F(SYMBOLS),
            "c": ids("c"),
            "C": ids("c"),
            "S": F(SIDES),
            "o": F(["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "ODD"]),
            "f": F(["GTC", "IOC", "FOK", "GTX", "GTD", "???"]),
            "q": amt,
            "p": amt,
            "x": F(_BN_X),
            "X": F(["NEW", "FILLED", "CANCELED", "PARTIALLY_FILLED"]),
            "r": F(["NONE", "Order would immediately match and take.", "x"]),
            "i": ids("o"),
            "l": amt,
            "z": amt,
            "L": amt,
            "n": num,
            "N": F(["BNB", "USDT", None]),
            "T": ep,
            "t": ids("t"),
            "m": boolish,
            "eR": F(["NONE", "STP", None]),
            "V": F(["NONE", "EXPIRE_MAKER"]),
            "A": num,
        },
        required={"e": st.just("executionReport")},
    )
    _bn_params = obj(
        {
            "symbol": F(SYMBOLS),
            "side": F(SIDES),
            "type": F(["LIMIT", "LIMIT_MAKER", "MARKET"]),
            "timeInForce": F(["GTC", "IOC"]),
            "price": amt,
            "quantity": amt,
            "newQty": amt,
            "newClientOrderId": ids("c"),
            "origClientOrderId": ids("c"),
            "orderId": ids("o"),
        }
    )
    _bn_req = obj(
        {
            "id": ids("r"),
            "method": F(["order.place", "order.cancel", "order.amend.keepPriority", "ping"]),
            "params": st.one_of(_bn_params, _bn_params, N),
        }
    )
    _bn_resp = obj(
        {
            "result": st.one_of(obj({"orderId": ids("o"), "clientOrderId": ids("c")}), N),
            "error": st.one_of(obj({"code": F([-2010, -1013, "x"]), "msg": F(["would match and take", ""])}), N),
        },
        required={"id": ids("r"), "status": F([200, 400, 403, 503, "200", "1e999999999"])},
    )
    _bn_trade = obj(
        {
            "symbol": F(SYMBOLS),
            "id": ids("t"),
            "price": amt,
            "qty": amt,
            "commission": num,
            "commissionAsset": F(["BNB", None]),
            "time": ep,
            "isMaker": boolish,
        },
        required={"isBuyer": boolish, "orderId": ids("o")},
    )
    _bn_order = obj(
        {
            "symbol": F(SYMBOLS),
            "clientOrderId": ids("c"),
            "price": amt,
            "status": F(["NEW", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED", "FILLED"]),
            "type": F(["LIMIT", "MARKET", "LIMIT_MAKER"]),
            "timeInForce": F(["GTC", "IOC"]),
            "side": F(SIDES),
            "workingTime": ep,
            "updateTime": ep,
        },
        required={"origQty": amt, "orderId": ids("o")},
    )
    binance = st.one_of(
        _bn_exec,
        _bn_exec.map(lambda e: {"subscriptionId": 0, "event": e}),
        _wrap_out(_bn_req),
        _wrap_in(_bn_resp),
        _bn_resp,
        _bn_trade,
        _bn_order,
        N,
    )

    # ---- Bybit ----------------------------------------------------------------------------------------------------

    _bb_order = obj(
        {
            "orderId": ids("o"),
            "orderLinkId": ids("c"),
            "symbol": F(SYMBOLS),
            "side": F(SIDES),
            "orderType": F(["Limit", "Market", "x"]),
            "timeInForce": F(["GTC", "IOC", "FOK", "PostOnly", "RPI"]),
            "price": amt,
            "qty": amt,
            "cumExecQty": amt,
            "leavesQty": amt,
            "orderStatus": F(
                [
                    "New",
                    "PartiallyFilled",
                    "Filled",
                    "Cancelled",
                    "PartiallyFilledCanceled",
                    "Rejected",
                    "Deactivated",
                    "x",
                ]
            ),
            "rejectReason": F(["EC_NoError", "EC_PostOnlyWillTakeLiquidity", "x"]),
            "cancelType": F(["UNKNOWN", "CancelByUser", "CancelBySmp", None]),
            "updatedTime": ep,
            "createdTime": ep,
            "reduceOnly": boolish,
        }
    )
    _bb_exec = obj(
        {
            "orderId": ids("o"),
            "orderLinkId": ids("c"),
            "symbol": F(SYMBOLS),
            "side": F(SIDES),
            "orderType": F(["Limit", "Market"]),
            "execType": F(["Trade", "Funding", "AdlTrade", None]),
            "execQty": amt,
            "execPrice": amt,
            "leavesQty": amt,
            "execTime": ep,
            "isMaker": boolish,
            "execFee": num,
            "feeCurrency": F(["USDT", None]),
        },
        required={"execId": ids("e")},
    )
    bybit = st.one_of(
        st.builds(lambda d: {"topic": "order", "data": d}, st.one_of(st.lists(_bb_order, max_size=3), _bb_order, N)),
        st.builds(lambda d: {"topic": "execution.linear", "data": d}, st.one_of(st.lists(_bb_exec, max_size=3), N)),
        st.builds(
            lambda d: {"retCode": 0, "result": {"list": d}}, st.lists(st.one_of(_bb_exec, _bb_order), max_size=3)
        ),
        st.builds(lambda t: {"topic": t, "data": []}, F(["wallet", "position", 5])),
        st.just({"op": "auth", "success": True}),
        _bb_order,
        N,
    )

    # ---- OKX ------------------------------------------------------------------------------------------------------

    _okx_row = obj(
        {
            "ordId": ids("o"),
            "clOrdId": ids("c"),
            "instId": F(SYMBOLS),
            "side": F(SIDES),
            "ordType": F(["limit", "post_only", "ioc", "fok", "market", "optimal_limit_ioc", "mmp", "x"]),
            "px": amt,
            "sz": amt,
            "accFillSz": amt,
            "fillSz": amt,
            "fillPx": amt,
            "fillTime": ep,
            "tradeId": ids("t"),
            "execType": F(["M", "T", ""]),
            "fillFee": num,
            "fee": num,
            "fillFeeCcy": F(["USDT", None]),
            "feeCcy": F(["USDT"]),
            "state": F(["live", "partially_filled", "filled", "canceled", "mmp_canceled", "x"]),
            "cancelSource": F(["1", "31", "32", "14", "", None]),
            "amendResult": F(["0", "-1", "", None]),
            "amendSource": F(["1", None]),
            "reqId": ids("r"),
            "code": F(["0", "51000"]),
            "uTime": ep,
            "cTime": ep,
            "ts": ep,
            "billId": ids("b"),
            "reduceOnly": boolish,
            "tag": F(["strat1", "", None]),
        }
    )
    okx = st.one_of(
        st.builds(
            lambda ch, d: {"arg": {"channel": ch, "instType": "SPOT"}, "data": d},
            F(["orders", "fills", "tickers", None]),
            st.one_of(st.lists(_okx_row, max_size=3), N),
        ),
        st.builds(lambda ch: {"event": "subscribe", "arg": {"channel": ch}}, F(["orders"])),
        st.builds(lambda d: {"code": "0", "data": d}, st.lists(_okx_row, max_size=3)),
        st.just({"event": "login"}),
        _okx_row,
        N,
    )

    # ---- Coinbase -------------------------------------------------------------------------------------------------

    _cb_ws = obj(
        {
            "order_id": ids("o"),
            "client_order_id": ids("c"),
            "product_id": F(SYMBOLS),
            "order_side": F(SIDES),
            "side": F(SIDES),
            "order_type": F(["LIMIT", "MARKET", "STOP_LIMIT", "x"]),
            "time_in_force": F(["GOOD_UNTIL_CANCELLED", "IMMEDIATE_OR_CANCEL", "x"]),
            "post_only": boolish,
            "status": F(["PENDING", "OPEN", "FILLED", "CANCELLED", "EXPIRED", "FAILED", "x"]),
            "cumulative_quantity": amt,
            "leaves_quantity": amt,
            "filled_value": amt,
            "total_fees": num,
            "number_of_fills": F([0, 1, 2, "3", "1e999999999", "-1"]),
            "limit_price": amt,
            "creation_time": rfc,
            "cancel_reason": F(["User requested", None]),
            "reject_reason": F(["POST_ONLY", "x", None]),
        }
    )
    _cb_fill = obj(
        {
            "entry_id": ids("f"),
            "trade_id": ids("t"),
            "order_id": ids("o"),
            "product_id": F(SYMBOLS),
            "side": F(SIDES),
            "trade_type": F(["FILL", "REVERSAL", "CORRECTION", "SYNTHETIC", None]),
            "price": amt,
            "size": amt,
            "size_in_quote": boolish,
            "commission": num,
            "liquidity_indicator": F(["MAKER", "TAKER", "UNKNOWN"]),
            "trade_time": rfc,
        }
    )
    _cb_order = obj(
        {
            "order_id": ids("o"),
            "client_order_id": ids("c"),
            "product_id": F(SYMBOLS),
            "side": F(SIDES),
            "status": F(["OPEN", "CANCELLED", "FILLED"]),
            "created_time": rfc,
            "order_configuration": st.one_of(
                st.builds(
                    lambda k, b: {k: b},
                    st.sampled_from(["limit_limit_gtc", "market_market_ioc", "sor_limit_ioc", "x", ""]),
                    obj({"base_size": amt, "limit_price": amt, "post_only": boolish}),
                ),
                N,
            ),
        }
    )
    coinbase = st.one_of(
        st.builds(
            lambda ts, evs: {"channel": "user", "timestamp": ts, "events": evs},
            rfc,
            st.one_of(
                st.lists(
                    st.builds(
                        lambda t, os_: {"type": t, "orders": os_},
                        F(["snapshot", "update"]),
                        st.one_of(st.lists(_cb_ws, max_size=3), N),
                    ),
                    max_size=2,
                ),
                N,
            ),
        ),
        st.builds(lambda f: {"fills": f}, st.one_of(st.lists(_cb_fill, max_size=4), N)),
        st.builds(lambda o: {"orders": o}, st.one_of(st.lists(_cb_order, max_size=3), N)),
        st.just({"channel": "heartbeats"}),
        _cb_fill,
        _cb_order,
        N,
    )

    # ---- Kraken ---------------------------------------------------------------------------------------------------

    _kr_item = obj(
        {
            "exec_type": F(
                ["pending_new", "new", "trade", "filled", "amended", "restated", "canceled", "expired", "status", "x"]
            ),
            "order_id": ids("o"),
            "cl_ord_id": ids("c"),
            "symbol": F(SYMBOLS),
            "side": F(SIDES),
            "order_type": F(["limit", "market", "stop-loss", "x"]),
            "time_in_force": F(["GTC", "IOC", "GTD"]),
            "post_only": boolish,
            "order_qty": amt,
            "limit_price": amt,
            "cum_qty": amt,
            "last_qty": amt,
            "last_price": amt,
            "exec_id": ids("e"),
            "liquidity_ind": F(["m", "t", "x"]),
            "fees": st.one_of(st.lists(obj({"asset": F(["USD", "EUR"]), "qty": num}), max_size=3), N),
            "timestamp": rfc,
            "reason": F(["User requested", "Post only order", None]),
        }
    )
    kraken = st.one_of(
        st.builds(
            lambda t, d: {"channel": "executions", "type": t, "data": d},
            F(["snapshot", "update"]),
            st.one_of(st.lists(_kr_item, max_size=4), _kr_item, N),
        ),
        st.just({"method": "subscribe", "success": True}),
        st.just({"channel": "heartbeat"}),
        N,
    )

    # ---- Deribit --------------------------------------------------------------------------------------------------

    _db_order = obj(
        {
            "order_id": ids("o"),
            "label": ids("c"),
            "instrument_name": F(SYMBOLS),
            "direction": F(["buy", "sell", "x"]),
            "order_type": F(["limit", "market", "stop_limit", "x"]),
            "time_in_force": F(["good_til_cancelled", "immediate_or_cancel", "x"]),
            "post_only": boolish,
            "reduce_only": boolish,
            "price": F(["market_price", *["100", "101.5"]]),
            "amount": amt,
            "filled_amount": amt,
            "order_state": F(["open", "filled", "cancelled", "rejected", "untriggered", "triggered", "x"]),
            "cancel_reason": F(["user_request", "edit_post_only_reject", "x", None]),
            "reject_reason": F(["post_only_reject", None]),
            "replaced": boolish,
            "creation_timestamp": ep,
            "last_update_timestamp": ep,
        }
    )
    _db_trade = obj(
        {
            "order_id": ids("o"),
            "instrument_name": F(SYMBOLS),
            "order_type": F(["limit", "market"]),
            "direction": F(["buy", "sell"]),
            "amount": amt,
            "price": amt,
            "timestamp": ep,
            "liquidity": F(["M", "T"]),
            "fee": num,
            "fee_currency": F(["BTC"]),
            "reduce_only": boolish,
        },
        required={"trade_id": ids("t")},
    )
    deribit = st.one_of(
        st.builds(
            lambda ch, d: {"jsonrpc": "2.0", "method": "subscription", "params": {"channel": ch, "data": d}},
            F(["user.orders.BTC-PERPETUAL.raw", "user.trades.any.raw", "user.portfolio", "x"]),
            st.one_of(st.lists(st.one_of(_db_order, _db_trade), max_size=3), _db_order, N),
        ),
        st.builds(lambda p: {"method": "subscription", "params": p}, N),
        st.builds(lambda t: {"result": {"trades": t}}, st.lists(_db_trade, max_size=3)),
        st.builds(lambda o: {"result": o}, st.one_of(st.lists(_db_order, max_size=3), _db_order, N)),
        _db_trade,
        N,
    )

    # ---- Hyperliquid ----------------------------------------------------------------------------------------------

    _hl_fill = obj(
        {
            "coin": F(["BTC", "ETH", ""]),
            "oid": ids("o"),
            "cloid": ids("c"),
            "side": F(["B", "A", "x"]),
            "sz": amt,
            "px": amt,
            "time": ep,
            "crossed": boolish,
            "fee": num,
            "feeToken": F(["USDC"]),
        },
        required={"tid": ids("t")},
    )
    _hl_order = obj(
        {
            "coin": F(["BTC", "ETH"]),
            "oid": ids("o"),
            "cloid": ids("c"),
            "side": F(["B", "A"]),
            "sz": amt,
            "origSz": amt,
            "limitPx": amt,
            "tif": F(["Gtc", "Alo", "Ioc", "FrontendMarket", "x"]),
            "orderType": F(["Limit", "Market"]),
            "reduceOnly": boolish,
            "timestamp": ep,
        }
    )
    _hl_update = obj(
        {
            "order": st.one_of(_hl_order, _hl_order, N),
            "status": F(
                [
                    "open",
                    "filled",
                    "canceled",
                    "badAloPxRejected",
                    "iocCancelRejected",
                    "marginCanceled",
                    "scheduledCancel",
                ]
            ),
            "statusTimestamp": ep,
        }
    )
    hyperliquid = st.one_of(
        st.builds(
            lambda d: {"channel": "userFills", "data": d},
            st.one_of(st.builds(lambda f: {"isSnapshot": True, "fills": f}, st.lists(_hl_fill, max_size=3)), N),
        ),
        st.builds(lambda d: {"channel": "orderUpdates", "data": d}, st.one_of(st.lists(_hl_update, max_size=4), N)),
        st.just({"channel": "subscriptionResponse", "data": {}}),
        _hl_fill,
        st.builds(lambda u: {**u, "order": u.get("order") or {}}, _hl_update),
        N,
    )

    return {
        "binance": binance,
        "bybit": bybit,
        "okx": okx,
        "coinbase": coinbase,
        "kraken": kraken,
        "deribit": deribit,
        "hyperliquid": hyperliquid,
    }


VENUE_RECORDS = venue_records(hostile=True)
VENUE_RECORDS_CLEAN = venue_records(hostile=False)
