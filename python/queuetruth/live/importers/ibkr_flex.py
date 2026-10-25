"""Interactive Brokers Flex Query "Trades" importer (SPEC 5.7.1; fixture-validated only).

Inputs: the Flex XML (`<Trade .../>` / `<Order .../>` elements) or CSV output (plain header, or the "header and
trailer records" layout with `BOF/HEADER/DATA/EOF` markers) of a Trades query with Level of Detail = Executions.
XML attribute names and CSV display labels without spaces are both accepted (`ibExecID` / `IBExecID`, `buySell` /
`Buy/Sell`, ...).

- One fill per EXECUTION row: `ibExecID` fill id (fallback `tradeID`), `ibOrderID` order id, `|quantity|`,
  `orderReference` client id (single-day queries), `tradePrice`, `fee = -ibCommission` (IBKR reports
  commissions as negative amounts), `liquidity = unknown` (Flex has no maker/taker field), `dateTime` (or
  `tradeDate` + `tradeTime`) in the account's statement time zone (option `tz`, IANA name, default UTC with a
  warning; an explicit UTC offset wins), precision `s`, match_time.
- Dates: `yyyyMMdd` / `yyyy-MM-dd`, or slash dates `MM/dd/yyyy` / `dd/MM/yyyy` (a Flex query setting). The slash
  order is inferred per input file from its `dateTime` / `tradeDate` / `reportDate` values: a first component > 12
  means dd/MM, a second component > 12 means MM/dd, both in one file fail `QT-INPUT-002` naming the file. A file
  whose slash dates are all ambiguous (both components <= 12) is read as MM/dd (US) with a warning naming the
  assumption (export with `yyyyMMdd` / `yyyy-MM-dd` to make dates unambiguous). Option `flex_date_order` (`mdy` or
  `dmy`; Python `ImportOptions.extra` only) forces the order, other values fail `QT-CONFIG-001`. Impossible dates
  or times fail `QT-INPUT-002`.
- Rows with `levelOfDetail = ORDER` (aggregates) and other non-execution rows are counted and skipped.
- Trade cancellations (`buySell` `BUY (Ca.)` / `SELL (Ca.)` or `transactionType = TradeCancel`) -> fill_bust
  referencing the execution whose `tradeID` equals the row's `origTradeID` (else the same `ibExecID`); a
  cancellation that references nothing is counted and warned about (UNVERIFIED layout). The bust row has no fill
  id of its own, keeps the cancelled trade's time and has kind `unknown`.
- Flex has no order records: every order is reconstructed from its executions (`orig_qty` = summed fill qty,
  `limit_px` null); `orderType` gives `order_type`. Default `size_exp = 0` (equities).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from queuetruth.errors import QtError
from queuetruth.live._common import (
    Record,
    dec,
    input_error,
    local_to_utc_s,
    read_csv_rows,
    read_text,
    text_or_none,
)
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions, new_builder
from queuetruth.live.importers._xml import parse_xml, to_text

VENUE = "ibkr_flex"
DEFAULT_TZ = "UTC"
#: `flex_date_order` values: slash dates as MM/dd/yyyy (`mdy`, US) or dd/MM/yyyy (`dmy`).
DATE_ORDERS = ("mdy", "dmy")
_ORDER_TYPE = {"LMT": "limit", "MKT": "market", "STP": "stop", "STPLMT": "stop_limit", "STP LMT": "stop_limit"}
_ALT: dict[str, tuple[str, ...]] = {
    "exec_id": ("ibExecID", "IBExecID", "IBExecutionID"),
    "trade_id": ("tradeID", "TradeID"),
    "orig_trade_id": ("origTradeID", "OrigTradeID"),
    "order_id": ("ibOrderID", "IBOrderID"),
    "client_id": ("orderReference", "OrderReference"),
    "report_date": ("reportDate", "ReportDate"),
    "account": ("accountId", "ClientAccountID", "AccountId", "AccountID"),
    "symbol": ("symbol", "Symbol"),
    "qty": ("quantity", "Quantity"),
    "price": ("tradePrice", "TradePrice", "Price"),
    "commission": ("ibCommission", "IBCommission"),
    "commission_ccy": ("ibCommissionCurrency", "IBCommissionCurrency", "currency", "CurrencyPrimary"),
    "order_type": ("orderType", "OrderType"),
    "level": ("levelOfDetail", "LevelOfDetail"),
    "side": ("buySell", "Buy/Sell"),
    "datetime": ("dateTime", "DateTime", "Date/Time"),
    "date": ("tradeDate", "TradeDate"),
    "time": ("tradeTime", "TradeTime"),
    "txn": ("transactionType", "TransactionType"),
}
_DT = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})[;, T]+(\d{2}):?(\d{2}):?(\d{2})\s*(?:([+-]\d{2}):?(\d{2}))?$", re.ASCII)
_SLASH = re.compile(r"^(\d{2})/(\d{2})/(\d{4})[;, T]+(\d{2}):?(\d{2}):?(\d{2})$", re.ASCII)
_SLASH_DATE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})(?![\d/])", re.ASCII)


def _get(row: dict[str, str], key: str) -> str | None:
    for k in _ALT[key]:
        v = row.get(k)
        if v is not None and v.strip() != "":
            return v.strip()
    return None


def _check_wall(y: int, mo: int, d: int, h: int, mi: int, s: int, text: str, form: str, loc: str) -> None:
    try:
        dt.datetime(y, mo, d, h, mi, s)
    except ValueError as e:
        raise input_error(loc, f"invalid Flex date-time {text!r} (read as {form}): {e}") from e


def _flex_ts(text: str, tz: str, loc: str, date_order: str = "mdy") -> int:
    """Flex date-time -> UTC ns; slash dates are read in `date_order` (`mdy` or `dmy`, see the module docstring)."""
    t = text.strip()
    m = _DT.match(t)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        _check_wall(y, mo, d, h, mi, s, text, "yyyyMMdd", loc)
        if m.group(7) is not None:
            sign = -1 if m.group(7).startswith("-") else 1
            oh, om = abs(int(m.group(7))), int(m.group(8))
            if oh > 23 or om > 59:
                raise input_error(loc, f"invalid Flex date-time {text!r}: UTC offset out of range")
            off = sign * (oh * 3600 + om * 60)
            return (local_to_utc_s(y, mo, d, h, mi, s, "UTC", "dateTime", loc) - off) * 10**9
        return local_to_utc_s(y, mo, d, h, mi, s, tz, "dateTime", loc) * 10**9
    m = _SLASH.match(t)
    if m:
        a, c, y, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        mo, d = (a, c) if date_order == "mdy" else (c, a)
        _check_wall(y, mo, d, h, mi, s, text, "MM/dd/yyyy" if date_order == "mdy" else "dd/MM/yyyy", loc)
        return local_to_utc_s(y, mo, d, h, mi, s, tz, "dateTime", loc) * 10**9
    raise input_error(loc, f"unrecognised Flex date-time {text!r}")


def _forced_date_order(opts: ImportOptions) -> str | None:
    v = opts.extra.get("flex_date_order")
    if v is None:
        return None
    order = str(v).strip().lower()
    if order not in DATE_ORDERS:
        raise QtError("QT-CONFIG-001", f"flex_date_order must be one of {list(DATE_ORDERS)}, got {v!r}")
    return order


def _date_order(b: LogBuilder, rows: list[tuple[dict[str, str], str, int]], path: str, forced: str | None) -> str:
    """Slash-date order of one input file (see the module docstring)."""
    if forced is not None:
        return forced
    n = 0
    dmy: str | None = None  # first component > 12
    mdy: str | None = None  # second component > 12
    for row, _, _ in rows:
        for key in ("datetime", "date", "report_date"):
            v = _get(row, key)
            m = _SLASH_DATE.match(v) if v is not None else None
            if m is None:
                continue
            n += 1
            if dmy is None and int(m.group(1)) > 12:
                dmy = v
            if mdy is None and int(m.group(2)) > 12:
                mdy = v
    if dmy is not None and mdy is not None:
        raise input_error(
            path,
            f"contradictory slash dates: {dmy!r} reads only as dd/MM/yyyy and {mdy!r} only as MM/dd/yyyy; export the "
            "Flex query with the date format yyyyMMdd or yyyy-MM-dd",
        )
    if dmy is not None:
        return "dmy"
    if mdy is None and n:
        b.warn(
            f"{VENUE}: {path}: all {n} slash date(s) are ambiguous (day and month both <= 12): read as MM/dd/yyyy "
            "(US order, ASSUMED); for dd/MM/yyyy pass ImportOptions.extra flex_date_order='dmy', or export the Flex "
            "query with the date format yyyyMMdd or yyyy-MM-dd to make the dates unambiguous"
        )
    return "mdy"


def _rows(path: Path, file_idx: int) -> list[tuple[dict[str, str], str, int]]:
    text = read_text(path)
    out: list[tuple[dict[str, str], str, int]] = []
    if text.lstrip().startswith("<"):
        # `text` is already decoded (UTF-8): the declaration's encoding must not be applied a second time
        root = parse_xml(text.encode("utf-8"), str(path), "a Flex XML statement", encoding="utf-8")
        n = 0
        for el in root.iter():
            if el.tag in ("Trade", "Order"):
                attrs = dict(el.attrib)
                if el.tag == "Order" and "levelOfDetail" not in attrs:
                    attrs["levelOfDetail"] = "ORDER"
                out.append((attrs, to_text(el, f"{path}#{n + 1}"), n))
                n += 1
        return out
    header, rows = read_csv_rows(text, str(path))
    if header and header[0] == "BOF":
        hdr = next((r[1:] for r in rows if r and r[0].strip() == "HEADER"), None)
        if hdr is None:
            raise input_error(str(path), "Flex CSV with BOF marker but no HEADER record")
        header = [c.strip() for c in hdr]
        rows = [r[1:] for r in rows if r and r[0].strip() == "DATA"]
    for n, r in enumerate(rows):
        out.append(({header[i]: r[i] for i in range(min(len(header), len(r)))}, ",".join(r), n))
    return out


def import_ibkr_flex(paths: list[str], opts: ImportOptions) -> ImportResult:
    """IBKR Flex Trades (XML or CSV) -> canonical rows."""
    b: LogBuilder = new_builder(VENUE, opts, size_exp=0)
    tz = str(opts.extra.get("tz") or "")
    if not tz:
        tz = DEFAULT_TZ
        b.warn(f"{VENUE}: Flex times interpreted in {DEFAULT_TZ} (no tz option given; Flex times carry no offset)")
    forced = _forced_date_order(opts)
    cancels: list[tuple[dict[str, str], Record, str]] = []
    by_trade_id: dict[str, tuple[int, str]] = {}
    for file_idx, p in enumerate(paths):
        rows = _rows(Path(p), file_idx)
        order = _date_order(b, rows, p, forced)
        for row, raw, n in rows:
            rec = Record(row, raw, f"{p}#{n + 1}", file_idx, n)
            level = (_get(row, "level") or "EXECUTION").upper()
            if level != "EXECUTION":
                b.skip(f"levelOfDetail={level} row (aggregate, not an execution)")
                continue
            side_raw = (_get(row, "side") or "").upper()
            txn = (_get(row, "txn") or "").lower()
            if "(CA.)" in side_raw or txn == "tradecancel":
                cancels.append((row, rec, order))
                continue
            side = {"BUY": "buy", "SELL": "sell"}.get(side_raw)
            if side is None:
                b.skip(f"row with buySell={side_raw or 'missing'}")
                continue
            h, fid = _execution(b, row, rec, side, tz, order, "fill", None)
            tid = _get(row, "trade_id")
            if tid is not None and fid is not None:
                by_trade_id[tid] = (h, fid)
    for row, rec, order in cancels:
        ref_tid = _get(row, "orig_trade_id") or _get(row, "trade_id")
        target = by_trade_id.get(ref_tid or "")
        if target is None:
            b.skip("trade-cancel row referencing no imported execution")
            b.warn(f"{VENUE}: trade cancellation {_get(row, 'exec_id') or ref_tid} references no imported execution")
            continue
        side_raw = (_get(row, "side") or "").upper()
        side = "buy" if side_raw.startswith("BUY") else "sell"
        _execution(b, row, rec, side, tz, order, "fill_bust", target[1])
    return b.finish()


def _execution(
    b: LogBuilder,
    row: dict[str, str],
    rec: Record,
    side: str,
    tz: str,
    date_order: str,
    etype: str,
    ref: str | None,
) -> tuple[int, str | None]:
    loc = rec.location
    ot = _get(row, "order_type")
    h = b.order(
        oid=_get(row, "order_id"),
        cid=_get(row, "client_id") if etype == "fill" else None,
        rec=rec,
        account=_get(row, "account"),
        instrument=_get(row, "symbol"),
        side=side,
        order_type=_ORDER_TYPE.get(ot.upper(), "other") if ot else None,
    )
    dt_text = _get(row, "datetime")
    if dt_text is None:
        d, t = _get(row, "date"), _get(row, "time")
        if d is None:
            raise input_error(loc, "execution without dateTime / tradeDate")
        dt_text = f"{d};{t or '000000'}"
    ts = _flex_ts(dt_text, tz, loc, date_order)
    qty = dec(_get(row, "qty"), "quantity", loc)
    comm = dec(_get(row, "commission"), "ibCommission", loc)
    fid = _get(row, "exec_id") or _get(row, "trade_id")
    b.event(
        h,
        etype,
        rec=rec,
        source="flex",
        kind="match_time" if etype == "fill" else "unknown",
        precision="s",
        ts_exchange=ts,
        qty=abs(qty) if qty is not None else None,
        px=dec(_get(row, "price"), "tradePrice", loc),
        fill_id=fid if etype == "fill" else None,
        ref_fill_id=ref,
        liquidity="unknown",
        fee=-comm if comm is not None else None,
        fee_ccy=_get(row, "commission_ccy"),
        reason=f"TradeCancel origTradeID={_get(row, 'orig_trade_id')} reportDate={_get(row, 'report_date')}"
        if etype == "fill_bust"
        else None,
    )
    return h, fid if text_or_none(fid) else None
