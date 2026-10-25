"""An independent Python reading of Nasdaq TotalView-ITCH 5.0 for tracing the replay checks' flags (W1 scale runs).

`scan(path, locates)` walks a gzip ITCH file (a day or an `itch_split.py` bucket) once and, for the selected
locates, rebuilds the displayed book the way the ITCH ingest and the replay engine see it:

- record-producing messages and their `sequence` follow the ingest's mapper (`crates/ingest/src/itch/mapper.rs`):
  one sequence number per `A`/`F`/`E`/`C`/`X`/`D`/`U`/`P`/`Q` of a selected locate that yields records, numbered
  across the selected locates in file order (an `E`/`C`/`X`/`D`/`U` of an unknown order, a zero-size execution or
  cancel, a bad side or a zero-share `Q` yields none);
- book effects: `A`/`F` add; `E`/`C` remove the executed shares at the resting price; `X` the cancelled shares,
  `D` the rest, `U` the rest of the old order and add the new one (same side, back of the queue); `P`/`Q` none;
- after every record-producing message (one event) the book of its locate is checked as `Book::note_event_end`
  does: crossed or locked when best bid >= best ask, negative spread when best bid > best ask;
- every `E`/`C` of a known order is checked as the rank oracle does: a price-priority violation when the order's
  side holds a live order at a better price than the order's resting price.

Next to the counts it keeps what explains them: the Stock Directory entry (authenticity `P`/`T`, IPO flag, issue
classification), the trading-state timeline (`H` Stock Trading Action, `K` IPO Quoting Period Update, `J` LULD
Auction Collar, `h` Operational Halt), every `Q` cross print, the system events, crossed episodes (maximal runs of
crossed events) with their opening and closing messages, and each price-priority violation with its message.
Messages are identified by `idx`, the 0-based position of the message in the file, and the mapper `seq`.

`classify_event` / `classify_ppv` give each flagged event a cause (`CAUSES`). A crossed episode takes the cause of
the message that crossed the book, for all its events: the book stays crossed until messages uncross it, and a halt
or IPO cross is disseminated as `C` executions that continue after the `H` resumption message (the `Q` print and
the `H` T first, then the cross's non-printable executions, as in the synthetic `NEWIP` of `tests/flag_itch.py` and
in the 2020-01-30 IPO and halt crosses), so those events belong to the halt. `events_in_state_T` counts them per
episode. A violation by a non-printable `C` is a cross execution and names the nearest `Q` print of its symbol
(`cross`).
"""

from __future__ import annotations

import gzip
import heapq
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NS = 1_000_000_000
#: Causes of a crossed event or a price-priority violation, most specific first.
CAUSES = {
    "test_symbol": "Stock Directory authenticity T (a Nasdaq test issue): never a real security",
    "ipo_quoting_period": "IPO before its first trade (H state H/P/Q with an IPO reason, or an IPO flag Y or K "
    "message before the first T): orders are displayed but not matched until the IPO cross, whose executions are "
    "reported after the resumption",
    "halt_or_pause_quoting": "trading halt or LULD pause (H state H/P/Q): orders are displayed but not matched "
    "until the halt cross, whose executions are reported after the resumption",
    "cross_execution": "execution in a Nasdaq cross (non-printable C, the print is the Q message): every order "
    "executes at the cross price at once, ITCH lists the executions one by one in no price order",
    "pre_open_continuous": "crossed display while trading (state T) before the 09:30 open",
    "post_close_continuous": "crossed display while trading (state T) after the 16:00 close",
    "regular_session": "crossed display or price-priority violation while trading (state T) in 09:30-16:00 with no "
    "cross: not explained by ITCH semantics",
}
_LAYOUT = {
    ord("S"): 12,
    ord("R"): 39,
    ord("H"): 25,
    ord("K"): 28,
    ord("J"): 35,
    ord("h"): 21,
    ord("A"): 36,
    ord("F"): 40,
    ord("E"): 31,
    ord("C"): 36,
    ord("X"): 23,
    ord("D"): 19,
    ord("U"): 35,
    ord("P"): 44,
    ord("Q"): 40,
}


def hms(ns: int | None) -> str | None:
    """Nanoseconds since midnight as `HH:MM:SS.nnnnnnnnn`."""
    if ns is None:
        return None
    s, n = divmod(ns, NS)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}.{n:09d}"


def px4(p: int) -> str:
    """An ITCH Price (4) as a decimal string."""
    return f"{p // 10_000}.{p % 10_000:04d}"


def _u(b: bytes, i: int, n: int) -> int:
    return int.from_bytes(b[i : i + n], "big")


def _stock(b: bytes, i: int) -> str:
    return b[i : i + 8].decode("ascii", "replace").strip()


@dataclass
class SymbolTrace:
    """Reference state and evidence of one locate."""

    locate: int
    symbol: str
    directory: dict[str, Any]
    bids: dict[int, int] = field(default_factory=dict)
    asks: dict[int, int] = field(default_factory=dict)
    _bh: list[int] = field(default_factory=list)  # max-heap of bid prices (negated), lazy deletion
    _ah: list[int] = field(default_factory=list)
    state: str | None = None
    reason: str = ""
    traded: bool = False  # an H with state T was seen
    ipo: bool = False  # a K message was seen
    events: int = 0
    records_msgs: int = 0
    crossed: int = 0
    negative: int = 0
    executions_checked: int = 0
    unknown_refs: int = 0
    ppv: int = 0
    crossed_by_cause: dict[str, int] = field(default_factory=dict)
    ppv_by_cause: dict[str, int] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    crosses: list[dict[str, Any]] = field(default_factory=list)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    ppv_events: list[dict[str, Any]] = field(default_factory=list)
    _ep: dict[str, Any] | None = None
    #: [start, end) in ns since midnight: crossed or locked episodes, and spans in a state other than T (end None:
    #: open at the end of the file)
    crossed_spans: list[list[int | None]] = field(default_factory=list)
    halt_spans: list[list[int | None]] = field(default_factory=list)

    # -- book
    def best_bid(self) -> int | None:
        h = self._bh
        while h and -h[0] not in self.bids:
            heapq.heappop(h)
        return -h[0] if h else None

    def best_ask(self) -> int | None:
        h = self._ah
        while h and h[0] not in self.asks:
            heapq.heappop(h)
        return h[0] if h else None

    def add(self, side: str, price: int, qty: int) -> None:
        lv = self.bids if side == "B" else self.asks
        if price not in lv:
            lv[price] = 0
            heapq.heappush(self._bh if side == "B" else self._ah, -price if side == "B" else price)
        lv[price] += qty

    def remove(self, side: str, price: int, qty: int) -> None:
        lv = self.bids if side == "B" else self.asks
        left = lv.get(price, 0) - qty
        if left > 0:
            lv[price] = left
        else:
            lv.pop(price, None)

    def depth(self, side: str, n: int = 3) -> list[list[Any]]:
        lv = self.bids if side == "B" else self.asks
        ps = sorted(lv, reverse=side == "B")[:n]
        return [[px4(p), lv[p]] for p in ps]


def in_spans(ns: int, spans: list[list[int | None]]) -> bool:
    """`ns` (since midnight) lies in one of the `[start, end)` spans (end None: to the end of the day)."""
    return any(a <= ns and (b is None or ns < b) for a, b in spans)


def classify_event(t: SymbolTrace, ts: int, session: dict[str, int]) -> str:
    """Cause of a crossed event of `t` at `ts` (ns since midnight); `session` holds the system-event times."""
    if t.directory.get("authenticity") == "T":
        return "test_symbol"
    if t.state in ("H", "P", "Q"):
        if t.reason.startswith("IPO") or ((t.ipo or t.directory.get("ipo_flag") == "Y") and not t.traded):
            return "ipo_quoting_period"
        return "halt_or_pause_quoting"
    if ts < session.get("Q", 34_200 * NS):
        return "pre_open_continuous"
    if ts >= session.get("M", 57_600 * NS):
        return "post_close_continuous"
    return "regular_session"


def classify_ppv(t: SymbolTrace, ts: int, printable: str | None, session: dict[str, int]) -> str:
    """Cause of a price-priority violation: a non-printable `C` is a cross execution; otherwise as a crossed event."""
    if t.directory.get("authenticity") == "T":
        return "test_symbol"
    if printable == "N":
        return "cross_execution"
    return classify_event(t, ts, session)


@dataclass
class Scan:
    """Result of `scan`."""

    symbols: dict[int, SymbolTrace]
    session: dict[str, int]
    messages: int
    sequence: int
    max_episodes: int
    max_ppv_events: int

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "messages": self.messages,
            "sequence": self.sequence,
            "system_events": {k: hms(v) for k, v in sorted(self.session.items())},
            "symbols": {},
        }
        for t in sorted(self.symbols.values(), key=lambda x: x.symbol):
            out["symbols"][t.symbol] = symbol_json(t, self.max_episodes, self.max_ppv_events)
        return out


def symbol_json(t: SymbolTrace, max_episodes: int, max_ppv: int) -> dict[str, Any]:
    return {
        "locate": t.locate,
        "directory": t.directory,
        "counts": {
            "events": t.events,
            "crossed_books_after_last": t.crossed,
            "negative_spreads": t.negative,
            "executions_checked": t.executions_checked,
            "price_priority_violations": t.ppv,
            "unknown_order_refs": t.unknown_refs,
        },
        "crossed_by_cause": dict(sorted(t.crossed_by_cause.items())),
        "ppv_by_cause": dict(sorted(t.ppv_by_cause.items())),
        "timeline": t.timeline,
        "crossed_spans": [[hms(a), hms(b)] for a, b in t.crossed_spans[:max_episodes]],
        "halt_spans": [[hms(a), hms(b)] for a, b in t.halt_spans],
        "crosses": t.crosses,
        "episodes_total": len(t.episodes),
        "episodes": t.episodes[:max_episodes],
        "ppv_events_total": len(t.ppv_events),
        "ppv_events": t.ppv_events[:max_ppv],
    }


def _frames(path: Path, chunk: int = 1 << 24):
    """Yields (idx, frame bytes without the length prefix) of a gzip (or raw) framed ITCH file."""
    op = gzip.open if path.suffix == ".gz" else open
    idx, carry = 0, b""
    with op(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            b = carry + data
            pos, n = 0, len(b)
            while pos + 2 <= n:
                ln = (b[pos] << 8) | b[pos + 1]
                end = pos + 2 + ln
                if end > n:
                    break
                yield idx, b[pos + 2 : end]
                idx += 1
                pos = end
            carry = b[pos:]
    if carry:
        raise ValueError(f"{path}: {len(carry)} bytes of a partial frame at the end")


def scan(
    path: Path,
    locates: dict[str, int] | None = None,
    symbols: list[str] | None = None,
    max_episodes: int = 200,
    max_ppv_events: int = 400,
) -> Scan:
    """Reference replay of `path` for the given locates (`{symbol: locate}`) or symbols (located by their `R`).

    The sequence numbering covers exactly the selected locates, as an ITCH ingest with that symbol list does."""
    want_sym = set(symbols or []) | set(locates or {})
    sel: dict[int, SymbolTrace] = {}
    orders: dict[int, list[Any]] = {}  # ref -> [locate, side 'B'/'S', price, remaining]
    session: dict[str, int] = {}
    seq = 0
    msgs = 0
    for idx, m in _frames(path):
        msgs += 1
        if len(m) < 11:
            continue
        typ = m[0]
        loc = (m[1] << 8) | m[2]
        if typ == 83 and loc == 0 and len(m) >= 12:  # 'S'
            session[chr(m[11])] = _u(m, 5, 6)
            continue
        if typ == 82:  # 'R'
            if len(m) < 39:
                continue
            sym = _stock(m, 11)
            if sym in want_sym and loc not in sel and (not locates or locates.get(sym) == loc):
                sel[loc] = SymbolTrace(
                    loc,
                    sym,
                    {
                        "idx": idx,
                        "ts": hms(_u(m, 5, 6)),
                        "market_category": chr(m[19]),
                        "financial_status": chr(m[20]),
                        "round_lot_size": _u(m, 21, 4),
                        "issue_classification": chr(m[26]),
                        "issue_subtype": m[27:29].decode("ascii", "replace").strip(),
                        "authenticity": chr(m[29]),
                        "ipo_flag": chr(m[31]),
                        "luld_tier": chr(m[32]),
                        "etp_flag": chr(m[33]),
                    },
                )
            continue
        t = sel.get(loc)
        if t is None:
            continue
        need = _LAYOUT.get(typ)
        if need is not None and len(m) < need:
            continue
        ts = _u(m, 5, 6)
        c = chr(typ)
        if c == "H":
            t.state, t.reason = chr(m[19]), m[21:25].decode("ascii", "replace").strip()
            halted = bool(t.halt_spans) and t.halt_spans[-1][1] is None
            if t.state != "T" and not halted:
                t.halt_spans.append([ts, None])
            elif t.state == "T" and halted:
                t.halt_spans[-1][1] = ts
            t.traded = t.traded or t.state == "T"
            t.timeline.append({"idx": idx, "ts": hms(ts), "type": "H", "state": t.state, "reason": t.reason})
            continue
        if c == "K":
            t.ipo = True
            t.timeline.append(
                {
                    "idx": idx,
                    "ts": hms(ts),
                    "type": "K",
                    "release_time": hms(_u(m, 19, 4) * NS),
                    "qualifier": chr(m[23]),
                    "ipo_price": px4(_u(m, 24, 4)),
                }
            )
            continue
        if c == "J":
            t.timeline.append(
                {
                    "idx": idx,
                    "ts": hms(ts),
                    "type": "J",
                    "reference": px4(_u(m, 19, 4)),
                    "upper": px4(_u(m, 23, 4)),
                    "lower": px4(_u(m, 27, 4)),
                    "extension": _u(m, 31, 4),
                }
            )
            continue
        if c == "h":
            t.timeline.append({"idx": idx, "ts": hms(ts), "type": "h", "market": chr(m[19]), "action": chr(m[20])})
            continue
        produced = False
        ppv_hit: dict[str, Any] | None = None
        if c in "AF":
            side = chr(m[19])
            if side in "BS":
                ref, shares, price = _u(m, 11, 8), _u(m, 20, 4), _u(m, 32, 4)
                orders[ref] = [loc, side, price, shares]
                t.add(side, price, shares)
                produced = True
                desc = {"type": c, "ref": ref, "side": side, "shares": shares, "price": px4(price)}
        elif c in "EC":
            ref, shares = _u(m, 11, 8), _u(m, 19, 4)
            printable = chr(m[31]) if c == "C" else None
            o = orders.get(ref)
            if o is None:
                t.unknown_refs += 1
            else:
                ot = sel[o[0]]
                ex = min(shares, o[3])
                if ex > 0:
                    produced = True
                    ot.executions_checked += 1
                    better = ot.best_bid() if o[1] == "B" else ot.best_ask()
                    viol = better is not None and (better > o[2] if o[1] == "B" else better < o[2])
                    desc = {
                        "type": c,
                        "ref": ref,
                        "side": o[1],
                        "shares": shares,
                        "resting_price": px4(o[2]),
                        **({"exec_price": px4(_u(m, 32, 4)), "printable": printable} if c == "C" else {}),
                    }
                    if viol:
                        ot.ppv += 1
                        cause = classify_ppv(ot, ts, printable, session)
                        ot.ppv_by_cause[cause] = ot.ppv_by_cause.get(cause, 0) + 1
                        ppv_hit = {
                            "idx": idx,
                            "seq": seq + 1,
                            "ts": hms(ts),
                            **desc,
                            "best_same_side": px4(better),
                            "cause": cause,
                            "state": ot.state,
                            "reason": ot.reason,
                        }
                        ot.ppv_events.append(ppv_hit)
                    ot.remove(o[1], o[2], ex)
                    o[3] -= ex
                    if o[3] == 0:
                        del orders[ref]
                    t = ot
        elif c in "XD":
            ref = _u(m, 11, 8)
            o = orders.get(ref)
            if o is None:
                t.unknown_refs += 1
            else:
                by = min(_u(m, 19, 4), o[3]) if c == "X" else o[3]
                if by > 0:
                    produced = True
                    ot = sel[o[0]]
                    ot.remove(o[1], o[2], by)
                    o[3] -= by
                    if o[3] == 0:
                        del orders[ref]
                    desc = {"type": c, "ref": ref, "side": o[1], "shares": by, "price": px4(o[2])}
                    t = ot
        elif c == "U":
            old, new, shares, price = _u(m, 11, 8), _u(m, 19, 8), _u(m, 27, 4), _u(m, 31, 4)
            o = orders.pop(old, None)
            if o is None:
                t.unknown_refs += 1
            else:
                produced = True
                ot = sel[o[0]]
                ot.remove(o[1], o[2], o[3])
                orders[new] = [o[0], o[1], price, shares]
                ot.add(o[1], price, shares)
                desc = {
                    "type": "U",
                    "old_ref": old,
                    "new_ref": new,
                    "side": o[1],
                    "old_price": px4(o[2]),
                    "shares": shares,
                    "price": px4(price),
                }
                t = ot
        elif c == "P":
            if chr(m[19]) in "BS":
                produced = True
                desc = {"type": "P", "shares": _u(m, 20, 4), "price": px4(_u(m, 32, 4))}
        elif c == "Q":
            shares = _u(m, 11, 8)
            cross = {"idx": idx, "ts": hms(ts), "shares": shares, "price": px4(_u(m, 27, 4)), "cross_type": chr(m[39])}
            t.crosses.append(cross)
            if shares > 0:
                produced = True
                desc = {"type": "Q", **{k: cross[k] for k in ("shares", "price", "cross_type")}}
        if not produced:
            continue
        seq += 1
        t.events += 1
        bb, ba = t.best_bid(), t.best_ask()
        crossed = bb is not None and ba is not None and bb >= ba
        if crossed:
            t.crossed += 1
            t.negative += bb > ba
            if t._ep is None:
                cause = classify_event(t, ts, session)
                t._ep = {
                    "start": {"idx": idx, "seq": seq, "ts": hms(ts), **desc},
                    "state": t.state,
                    "reason": t.reason,
                    "cause": cause,
                    "events": 0,
                    "events_in_state_T": 0,
                    "negative": 0,
                    "max_width": 0,
                    "book_at_start": {"bids": t.depth("B"), "asks": t.depth("S")},
                }
                t.episodes.append(t._ep)
                t.crossed_spans.append([ts, None])
            ep = t._ep
            # a crossed book stays crossed until messages uncross it: every event of an episode has the cause of
            # the state that let the book cross (a halt or IPO cross is reported after the resumption, see CAUSES)
            t.crossed_by_cause[ep["cause"]] = t.crossed_by_cause.get(ep["cause"], 0) + 1
            ep["events"] += 1
            ep["events_in_state_T"] += t.state == "T"
            ep["negative"] += bb > ba
            ep["max_width"] = max(ep["max_width"], bb - ba)
            ep["last"] = {"idx": idx, "seq": seq, "ts": hms(ts), "state": t.state}
        elif t._ep is not None:
            t.crossed_spans[-1][1] = ts
            t._ep["end"] = {"idx": idx, "seq": seq, "ts": hms(ts), **desc}
            t._ep["max_width"] = px4(t._ep["max_width"])
            t._ep = None
    for t in sel.values():
        for e in t.ppv_events:
            if e["cause"] == "cross_execution" and t.crosses:
                q = min(t.crosses, key=lambda c: abs(c["idx"] - e["idx"]))
                e["cross"] = {"idx": q["idx"], "ts": q["ts"], "cross_type": q["cross_type"], "price": q["price"]}
        if t._ep is not None:
            t._ep["end"] = None  # still crossed at the end of the file
            t._ep["max_width"] = px4(t._ep["max_width"])
            t._ep = None
    return Scan(sel, session, msgs, seq, max_episodes, max_ppv_events)
