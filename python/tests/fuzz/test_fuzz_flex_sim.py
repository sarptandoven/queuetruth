"""Fuzz properties of the IBKR Flex importer (XML and CSV, both CSV layouts, slash-date inference, time zones) and
of the simulator importers (hftbacktest snapshots and NautilusTrader reports / events as CSV, JSON lines and
Parquet), through `import_venue` / `import_sim`, plus mutated fixtures and raw bytes."""

from __future__ import annotations

import csv
import io
import json
import warnings
from typing import Any
from xml.sax.saxutils import quoteattr

import pyarrow as pa
import pyarrow.parquet as pq
from fuzz._harness import FUZZ, guarded, scaled, workdir, write_files
from fuzz._mutate import mutated, seeds
from fuzz._values import EDGE_DECIMALS, EDGE_EPOCHS, amounts, ids, nasty, one_bad_value
from hypothesis import given
from hypothesis import strategies as st

from queuetruth.live import import_sim, import_venue
from queuetruth.live.importers import ImportOptions
from queuetruth.live.importers.ibkr_flex import import_ibkr_flex

# ---- IBKR Flex ------------------------------------------------------------------------------------------------------

FLEX_TIMES = [
    "20261015;093001",
    "2026-10-15, 09:30:01",
    "2026-10-15 09:30:01",
    "20261015 093001 -0500",
    "20261015;093001+0100",
    "10/15/2026 09:30:01",
    "15/10/2026;09:30:01",
    "03/04/2026 09:30:01",
    "02/30/2026 09:30:01",
    "13/13/2026 09:30:01",
    "20261301;093001",
    "00000101;000000",
    "20261015;250000",
    "20261015;093001 -9999",
    "20261101;013000",
    "20260308;023000",
    "",
    "garbage",
]


def _flex_row(hostile: bool) -> st.SearchStrategy[dict[str, str]]:
    def v(values: list[Any] | st.SearchStrategy[Any]) -> st.SearchStrategy[str]:
        base = st.sampled_from(values) if isinstance(values, list) else values
        alts = [base] * 4 + ([st.sampled_from(EDGE_DECIMALS), st.text(max_size=8), one_bad_value] if hostile else [])
        return st.one_of(*alts).map(str)

    if not hostile:  # one unambiguous date format, whole share quantities (Flex default size_exp = 0)
        return st.fixed_dictionaries(
            {
                "buySell": v(["BUY", "SELL", "BUY (Ca.)", "SELL (Ca.)"]),
                "dateTime": v(
                    st.integers(0, 6 * 3600).map(
                        lambda s: f"20261015;{9 + s // 3600:02d}{s // 60 % 60:02d}{s % 60:02d}"
                    )
                ),
                "quantity": v(st.integers(-500, 500).map(str)),
            },
            optional={
                "ibExecID": v(ids("e").map(str)),
                "tradeID": v(ids("t").map(str)),
                "origTradeID": v(ids("t").map(str)),
                "ibOrderID": v(ids("o").map(str)),
                "orderReference": v(ids("c").map(str)),
                "symbol": v(["ACME", "XYZ"]),
                "tradePrice": v(amounts),
                "ibCommission": v(["-0.3", "-1", "0"]),
                "orderType": v(["LMT", "MKT"]),
                "levelOfDetail": v(["EXECUTION", "EXECUTION", "ORDER"]),
                "transactionType": v(["ExchTrade", "TradeCancel"]),
            },
        )
    return st.fixed_dictionaries(
        {"buySell": v(["BUY", "SELL", "BUY (Ca.)", "SELL (Ca.)", "buy", "HOLD", ""])},
        optional={
            "ibExecID": v(ids("e").map(str)),
            "tradeID": v(ids("t").map(str)),
            "origTradeID": v(ids("t").map(str)),
            "ibOrderID": v(ids("o").map(str)),
            "orderReference": v(ids("c").map(str)),
            "accountId": v(["U0000001", "U2", ""]),
            "symbol": v(["ACME", "XYZ", ""]),
            "quantity": v(amounts),
            "tradePrice": v(amounts),
            "ibCommission": v(["-0.3", "-1", "0", "0.1"]),
            "ibCommissionCurrency": v(["USD", "EUR"]),
            "orderType": v(["LMT", "MKT", "STP", "STP LMT", "MOC"]),
            "levelOfDetail": v(["EXECUTION", "EXECUTION", "ORDER", "CLOSED_LOT"]),
            "dateTime": v(FLEX_TIMES),
            "tradeDate": v(["20261015", "2026-10-15", "10/15/2026", "15/10/2026", "x"]),
            "tradeTime": v(["093001", "09:30:01", "250000", ""]),
            "reportDate": v(["20261015", "10/16/2026", "16/10/2026"]),
            "transactionType": v(["ExchTrade", "TradeCancel", "BookTrade"]),
        },
    )


@st.composite
def flex_file(draw: st.DrawFn, *, hostile: bool) -> tuple[str, bytes]:
    rows = draw(st.lists(_flex_row(hostile), max_size=10))
    layout = draw(st.sampled_from(["xml", "csv", "csv_bof"]))
    if layout == "xml":
        trades = []
        for r in rows:
            tag = draw(st.sampled_from(["Trade", "Trade", "Order"]))
            attrs = " ".join(f"{k}={quoteattr(val)}" for k, val in r.items())
            trades.append(f"<{tag} {attrs}/>")
        doc = (
            '<?xml version="1.0" encoding="UTF-8"?><FlexQueryResponse><FlexStatements count="1"><FlexStatement>'
            f"<Trades>{''.join(trades)}</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"
        )
        if hostile and draw(st.integers(0, 8)) == 0:
            doc = draw(
                st.sampled_from(
                    [
                        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>',
                        "<Trade " + "a='1' " * 3 + ">" + "<b>" * 20_000 + "</b>" * 20_000 + "</Trade>",
                        "<FlexQueryResponse><Trade quantity='1'",
                        "<",
                    ]
                )
            )
            if doc.startswith("<Trade a="):
                doc = "<r>" + doc + "</r>"
        return "flex.xml", doc.encode("utf-8", "surrogatepass")
    cols = sorted({k for r in rows for k in r}) or ["buySell"]
    out = io.StringIO()
    w = csv.writer(out)
    if layout == "csv_bof":
        w.writerow(["BOF", "U0000001", "qt", "1", "20261015", "20261015"])
        w.writerow(["HEADER", *cols])
        for r in rows:
            w.writerow(["DATA", *[r.get(c, "") for c in cols]])
        w.writerow(["EOF", str(len(rows))])
    elif hostile and draw(st.booleans()):  # unquoted: a CR, NUL, quote or comma inside a field reaches the reader
        lines = [",".join(cols)] + [",".join(r.get(c, "") for c in cols) for r in rows]
        return "flex.csv", "\n".join(lines).encode("utf-8", "surrogatepass")
    else:
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols][: draw(st.integers(0, len(cols))) if hostile else None])
    return "flex.csv", out.getvalue().encode("utf-8", "surrogatepass")


def run_flex(files: dict[str, bytes], tz: str | None, order: str | None, what: str) -> Any:
    with workdir() as d:
        paths, total = write_files(d, files)

        def run() -> Any:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if order is not None:  # flex_date_order is a Python-only option (ImportOptions.extra)
                    return import_ibkr_flex(paths, ImportOptions(extra={"tz": tz, "flex_date_order": order}))
                spec: dict[str, Any] = {
                    "venue": "ibkr_flex",
                    "inputs": [{"path": p} for p in paths],
                    "out": str(d / "o"),
                }
                if tz is not None:
                    spec["tz"] = tz
                return import_venue(spec)

        return guarded(what, run, input_bytes=total)


TZS = [None, "UTC", "America/New_York", "Europe/London", "Asia/Tokyo", "Not/AZone", "", "../etc", "America"]


@FUZZ
@given(flex_file(hostile=True), st.sampled_from(TZS), st.sampled_from([None, None, "mdy", "dmy", "ymd", "MDY"]))
def test_flex_hostile(f: tuple[str, bytes], tz: str | None, order: str | None) -> None:
    run_flex({f[0]: f[1]}, tz, order, "ibkr_flex:hostile")


@FUZZ
@given(st.lists(flex_file(hostile=False), min_size=1, max_size=2), st.sampled_from(["UTC", "America/New_York"]))
def test_flex_plausible(fs: list[tuple[str, bytes]], tz: str) -> None:
    run_flex({f"{i}{name}": data for i, (name, data) in enumerate(fs)}, tz, None, "ibkr_flex:plausible")


@st.composite
def flex_one_field(draw: st.DrawFn) -> tuple[str, bytes]:
    """A plausible statement in which exactly one attribute or CSV cell is hostile (written unquoted in CSV half
    the time, so CR, NUL and quotes reach the CSV reader)."""
    rows = draw(st.lists(_flex_row(hostile=False), min_size=1, max_size=6))
    i = draw(st.integers(0, len(rows) - 1))
    key = draw(st.sampled_from(sorted({*rows[i], "tradePrice", "ibCommission", "dateTime", "orderReference"})))
    rows[i] = {**rows[i], key: str(draw(one_bad_value))}
    layout = draw(st.sampled_from(["xml", "csv", "csv_raw"]))
    if layout == "xml":
        body = "".join("<Trade " + " ".join(f"{k}={quoteattr(v)}" for k, v in r.items()) + "/>" for r in rows)
        return "flex.xml", f"<FlexQueryResponse>{body}</FlexQueryResponse>".encode("utf-8", "surrogatepass")
    cols = sorted({k for r in rows for k in r})
    if layout == "csv_raw":
        text = "\n".join([",".join(cols)] + [",".join(r.get(c, "") for c in cols) for r in rows])
    else:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        w.writerows([r.get(c, "") for c in cols] for r in rows)
        text = out.getvalue()
    return "flex.csv", text.encode("utf-8", "surrogatepass")


@FUZZ
@given(flex_one_field(), st.sampled_from(["UTC", "America/New_York"]))
def test_flex_one_bad_field(f: tuple[str, bytes], tz: str) -> None:
    run_flex({f[0]: f[1]}, tz, None, "ibkr_flex:one_field")


@FUZZ
@given(mutated(seeds("ibkr_flex/*/*.xml", "ibkr_flex/*/*.csv")), st.sampled_from(["flex.xml", "flex.csv"]))
def test_flex_mutated(data: bytes, name: str) -> None:
    run_flex({name: data}, "America/New_York", None, "ibkr_flex:mutated")


# ---- simulators -----------------------------------------------------------------------------------------------------

_HBT_STATUS = st.one_of(st.integers(-1, 8), st.sampled_from(["new", "filled", "canceled", "partially filled", "?"]))


def _hbt_rows(hostile: bool) -> st.SearchStrategy[list[dict[str, Any]]]:
    def v(base: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
        return st.one_of(base, base, base, nasty) if hostile else base

    snap = st.fixed_dictionaries(
        {
            "order_id": v(st.integers(1, 4)),
            "side": v(st.sampled_from([1, -1, "buy", "sell", 0])),
            "price_tick": v(st.integers(0, 200_000)),
            "tick_size": v(st.sampled_from(["0.01", "0.5", "1", "0.0001"])),
            "qty": v(amounts),
            "leaves_qty": v(amounts),
            "exec_price_tick": v(st.integers(0, 200_000)),
            "status": v(_HBT_STATUS),
            "exch_timestamp": v(st.one_of(st.integers(1_792_071_000 * 10**9, 1_792_072_000 * 10**9), st.just(0))),
            "local_timestamp": v(st.integers(1_792_071_000 * 10**9, 1_792_072_000 * 10**9)),
        },
        optional={
            "maker": v(st.booleans()),
            "order_type": v(st.sampled_from([0, 1, "limit", 7])),
            "time_in_force": v(st.sampled_from([0, 1, 2, 3, 9, "gtx"])),
            "order_key": v(st.sampled_from(["binance:acct1:o1", "o3.leg2", "binance:acct1:o1.leg2", ""])),
            "instrument": v(st.sampled_from(["BTCUSDT", ""])),
            "asset_no": v(st.integers(0, 3)),
        },
    )
    return st.lists(snap, max_size=10)


_NT_EVENTS = ["OrderInitialized", "OrderSubmitted", "OrderAccepted", "OrderRejected", "OrderCanceled", "OrderFilled"]


def _nt_rows(hostile: bool) -> st.SearchStrategy[list[dict[str, Any]]]:
    def v(base: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
        return st.one_of(base, base, base, nasty) if hostile else base

    ts = st.one_of(
        st.integers(1_792_071_000 * 10**9, 1_792_072_000 * 10**9),
        st.sampled_from(["2026-10-15T13:30:00.000000001+00:00", "2026-10-15T13:30:00Z"]),
    )
    row = st.fixed_dictionaries(
        {
            "client_order_id": v(st.sampled_from(["O-1", "O-2", "binance:acct1:o1", "o3.leg2"])),
            **({} if hostile else {"ts_event": ts}),  # plausible rows always carry their event time
        },
        optional={
            "type": v(st.sampled_from([*_NT_EVENTS, "LIMIT", "MARKET", "x"])),
            "venue_order_id": v(st.sampled_from(["V1", "V2"])),
            "instrument_id": v(st.sampled_from(["BTCUSDT.BINANCE", "ETH", ""])),
            "side": v(st.sampled_from(["BUY", "SELL"])),
            "order_side": v(st.sampled_from(["BUY", "SELL"])),
            "quantity": v(amounts),
            "price": v(amounts),
            "time_in_force": v(st.sampled_from(["GTC", "IOC", "DAY", "x"])),
            "post_only": v(st.booleans()),
            "reduce_only": v(st.booleans()),
            "status": v(st.sampled_from(["FILLED", "CANCELED", "EXPIRED", "REJECTED", "DENIED", "ACCEPTED"])),
            "filled_qty": v(amounts),
            "avg_px": v(amounts),
            "ts_init": v(ts),
            "ts_last": v(ts),
            **({"ts_event": v(ts)} if hostile else {}),
            "trade_id": v(st.sampled_from(["T1", "T2"])),
            "last_qty": v(amounts),
            "last_px": v(amounts),
            "liquidity_side": v(st.sampled_from(["MAKER", "TAKER", "NO_LIQUIDITY_SIDE"])),
            "commission": v(st.sampled_from(["0.01 USDT", "0.5", "", *(["x y z"] if hostile else [])])),
            "reason": v(st.sampled_from(["POST_ONLY", ""])),
            "due_post_only": v(st.booleans()),
        },
    )
    return st.lists(row, max_size=8)


def _utf8(text: str) -> str:
    """Text Arrow can store (lone surrogates replaced; JSON lines and CSV keep them, as escapes or raw bytes)."""
    return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def _render(rows: list[dict[str, Any]], fmt: str) -> bytes:
    if fmt == "jsonl":
        return "\n".join(json.dumps(r, default=str) for r in rows).encode("utf-8", "surrogatepass")
    if fmt == "parquet":
        cols = sorted({k for r in rows for k in r})
        table = pa.table(
            {c: pa.array([None if r.get(c) is None else _utf8(str(r.get(c))) for r in rows], pa.utf8()) for c in cols}
        )
        sink = io.BytesIO()
        pq.write_table(table, sink)
        return sink.getvalue()
    cols = sorted({k for r in rows for k in r})
    if fmt == "csv_raw":  # unquoted, as a hand-rolled writer would produce it
        lines = [",".join(cols)] + [",".join("" if r.get(c) is None else str(r.get(c)) for c in cols) for r in rows]
        return "\n".join(lines).encode("utf-8", "surrogatepass")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else str(r.get(c)) for c in cols])
    return out.getvalue().encode("utf-8", "surrogatepass")


SIM_OPTIONS = {
    "size_exp": st.sampled_from([0, -8, "-8", "x", 99, -(10**30), "\u00b2", "+-5", 1.5]),
    "key_from_client_id": st.booleans(),
    "order_key_map": st.sampled_from([{"1": "binance:acct1:o1"}, {"2": "bad key"}, {}]),
    "venue": st.sampled_from(["binance", "", None]),
}


@st.composite
def sim_case(draw: st.DrawFn, *, hostile: bool) -> tuple[str, dict[str, bytes], dict[str, Any]]:
    kind = draw(st.sampled_from(["hftbacktest", "nautilus"]))
    files: dict[str, bytes] = {}
    for i in range(draw(st.integers(1, 2))):
        rows = draw(_hbt_rows(hostile) if kind == "hftbacktest" else _nt_rows(hostile))
        fmt = draw(st.sampled_from(["csv", "jsonl", "parquet", *(["csv_raw"] if hostile else [])]))
        stem = draw(st.sampled_from(["orders", "fills", "order_filled", "OrderAccepted", "x"]))
        files[f"{stem}{i}.{fmt.removesuffix('_raw')}"] = _render(rows, fmt)
    opts: dict[str, Any] = {}
    if hostile:
        for k in draw(st.lists(st.sampled_from(sorted(SIM_OPTIONS)), max_size=2, unique=True)):
            opts[k] = draw(SIM_OPTIONS[k])
    return kind, files, opts


def run_sim(kind: str, files: dict[str, bytes], opts: dict[str, Any], what: str) -> Any:
    with workdir() as d:
        paths, total = write_files(d, files)

        def run() -> Any:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return import_sim(kind, paths, d / "out", account="acct1", **opts)

        return guarded(what, run, input_bytes=total)


@FUZZ
@given(sim_case(hostile=True))
def test_sim_hostile(case: tuple[str, dict[str, bytes], dict[str, Any]]) -> None:
    run_sim(*case, what=f"sim:hostile:{case[0]}")


@FUZZ
@given(sim_case(hostile=False))
def test_sim_plausible(case: tuple[str, dict[str, bytes], dict[str, Any]]) -> None:
    run_sim(*case, what=f"sim:plausible:{case[0]}")


#: Keys a one-field example may set (the documented columns of each simulator; absent ones are added).
_SIM_KEYS = {
    "hftbacktest": ["order_id", "side", "price_tick", "tick_size", "qty", "leaves_qty", "exec_qty", "exec_price_tick",
                    "status", "exch_timestamp", "local_timestamp", "maker", "order_type", "time_in_force",
                    "order_key", "instrument", "asset_no"],
    "nautilus": ["client_order_id", "venue_order_id", "instrument_id", "side", "order_side", "type", "quantity",
                 "price", "time_in_force", "post_only", "reduce_only", "status", "filled_qty", "avg_px", "ts_init",
                 "ts_last", "ts_event", "trade_id", "last_qty", "last_px", "liquidity_side", "commission"],
}  # fmt: skip


@st.composite
def sim_one_field(draw: st.DrawFn) -> tuple[str, dict[str, bytes], dict[str, Any]]:
    """Plausible simulator rows with exactly one hostile cell, or plausible rows with exactly one hostile option:
    every column and option is reached with all the others valid."""
    kind = draw(st.sampled_from(["hftbacktest", "nautilus"]))
    rows = draw((_hbt_rows if kind == "hftbacktest" else _nt_rows)(False).filter(bool))
    opts: dict[str, Any] = {}
    if draw(st.integers(0, 3)) == 0:
        key = draw(st.sampled_from(sorted(SIM_OPTIONS)))
        opts[key] = draw(st.one_of(SIM_OPTIONS[key], one_bad_value))
    else:
        i = draw(st.integers(0, len(rows) - 1))
        rows[i] = {**rows[i], draw(st.sampled_from(_SIM_KEYS[kind])): draw(one_bad_value)}
    fmt = draw(st.sampled_from(["csv", "csv_raw", "jsonl", "parquet"]))
    return kind, {f"orders.{fmt.removesuffix('_raw')}": _render(rows, fmt)}, opts


@FUZZ
@given(sim_one_field())
def test_sim_one_bad_field(case: tuple[str, dict[str, bytes], dict[str, Any]]) -> None:
    run_sim(*case, what=f"sim:one_field:{case[0]}")


#: Temporal Arrow types and raw values at and past the edges of Python's datetime range (year 1..9999).
_TEMPORAL = [
    (pa.date32(), [0, 2**31 - 1, -(2**31), 2_932_896, 2_932_897, -719_162, -719_163]),
    (pa.date64(), [0, 2**62, -(2**62), 2**63 - 1]),
    *[(pa.timestamp(u), [0, 2**63 - 1, -(2**63), 2**62, 1_792_071_000 * m]) for u, m in
      (("ms", 10**3), ("us", 10**6), ("ns", 10**9))],
    (pa.timestamp("us", tz="UTC"), [2**63 - 1, 1_792_071_000 * 10**6]),
    (pa.time64("ns"), [0, 86_400 * 10**9 - 1]),
    (pa.duration("s"), [2**63 - 1]),
    (pa.decimal128(38, 0), [2**63 - 1]),
]  # fmt: skip


@FUZZ
@given(
    sim_case(hostile=False),
    st.sampled_from(["ts_event", "ts_init", "ts_last", "exch_timestamp", "local_timestamp", "quantity", "qty"]),
    st.sampled_from(_TEMPORAL).flatmap(lambda tv: st.tuples(st.just(tv[0]), st.sampled_from(tv[1]))),
)
def test_sim_parquet_typed_columns(
    case: tuple[str, dict[str, bytes], dict[str, Any]], col: str, typed: tuple[pa.DataType, int]
) -> None:
    """Parquet columns of temporal and decimal Arrow types with values at the int32 / int64 limits (Python's
    date and datetime cannot hold them): the conversion to records must fail with a QT code."""
    kind = case[0]
    rows = [{"client_order_id": "O-1", "type": "OrderAccepted", "order_id": 1, "tick_size": "0.01"}]
    t, v = typed
    try:
        arr = pa.array([v], pa.int64()).cast(t, safe=False)
    except (pa.ArrowException, ValueError, OverflowError):
        return  # a value Arrow itself cannot hold in this type
    table = pa.table({**{k: pa.array([str(x)]) for k, x in rows[0].items()}, col: arr})
    sink = io.BytesIO()
    try:
        pq.write_table(table, sink)
    except (pa.ArrowException, ValueError):
        return
    run_sim(kind, {"x.parquet": sink.getvalue()}, {}, what=f"sim:typed_parquet:{kind}")


@scaled(0.5)
@given(
    st.sampled_from(["hftbacktest", "nautilus"]),
    st.sampled_from(["x.parquet", "x.csv", "x.jsonl", "OrderFilled.parquet"]),
    st.one_of(st.binary(max_size=400), mutated([_render([{"client_order_id": "O-1", "quantity": "1"}], "parquet")])),
)
def test_sim_raw_bytes(kind: str, name: str, data: bytes) -> None:
    """Arbitrary and mutated bytes, including mutated Parquet files (Arrow's own errors must become QtErrors)."""
    run_sim(kind, {name: data}, {}, what=f"sim:bytes:{kind}")


#: Valid Parquet seeds of both simulators (mutated by `test_sim_mutated_parquet`).
_PARQUET_SEEDS = [
    _render(
        [{"client_order_id": "O-1", "type": "OrderAccepted", "quantity": "1", "ts_event": "1792071000000000000"}],
        "parquet",
    ),
    _render(
        [
            {
                "order_id": "1",
                "side": "1",
                "price_tick": "100",
                "tick_size": "0.01",
                "qty": "1",
                "leaves_qty": "1",
                "status": "1",
                "exch_timestamp": "1792071000000000000",
                "local_timestamp": "1792071000000000000",
            }
        ],
        "parquet",
    ),
]


@FUZZ
@given(st.sampled_from(["hftbacktest", "nautilus"]), mutated(_PARQUET_SEEDS))
def test_sim_mutated_parquet(kind: str, data: bytes) -> None:
    """Mutated Parquet files: corrupt footers, page headers and column names (invalid UTF-8 in a column name is
    only decoded when the rows are read) must become QtErrors."""
    run_sim(kind, {"x.parquet": data}, {}, what=f"sim:parquet:{kind}")


@scaled(0.5)
@given(st.sampled_from(EDGE_EPOCHS), st.sampled_from(["hftbacktest", "nautilus"]))
def test_sim_edge_times(t: int, kind: str) -> None:
    rows: list[dict[str, Any]] = (
        [
            {
                "order_id": 1,
                "side": 1,
                "price_tick": 1,
                "tick_size": "1",
                "qty": "1",
                "leaves_qty": "1",
                "status": 1,
                "exch_timestamp": t,
                "local_timestamp": t,
            }
        ]
        if kind == "hftbacktest"
        else [{"client_order_id": "O-1", "type": "OrderAccepted", "ts_event": t}]
    )
    run_sim(kind, {"x.csv": _render(rows, "csv")}, {}, what=f"sim:edge_times:{kind}")
