"""FIX order-entry and drop-copy log importer (SPEC 5.7.1 `fix44` row; fixture-validated only), with first-class
support for the files QuickFIX engines write (QuickFIX C++ and its Python binding, QuickFIX/J, QuickFIX/n,
QuickFIX/Go; formats and sources in `queuetruth.live.importers.quickfix`).

Inputs: FileLog message logs (`*.messages.current.log`, `*.messages.log`, `*.messages.backup.<n>.log`, rotated or
gzip-compressed copies), FileStore bodies (`*.body` + `*.header`), event logs (`*.event*.log`, summarised),
FileStore sequence-number files (`*.seqnums`, `*.senderseqnums`, `*.targetseqnums`: completeness check), whole
`FileLogPath` / `FileStorePath` directories, or any one-message-per-line FIX log (SOH, `|` or `^A` delimited;
`fix.delimiter`). Log-line times: the QuickFIX prefixes are recognised per line (UTC); other loggers need
`fix.local_ts_regex` (first capture group or group `ts`: FIX UTCTimestamp, `YYYY/MM/DD HH:MM:SS[.f]`, RFC 3339 or
integer epoch; times without an offset are UTC, or in `tz` when the spec sets it).

Message validation (`fix.checksum`, default `exclude`): every message is framed by BodyLength(9) and checked
against CheckSum(10) (SOH form; `|` logs also in the literal form). Messages failing either check, truncated
messages (a crashed writer's last line) and malformed ones are excluded and counted per reason: a QuickFIX engine
discards garbled messages itself and the counterparty resends them. `keep` imports CheckSum / BodyLength failures
(counted) for logs re-encoded after capture.

Sessions: messages are grouped by BeginString, CompID pair and the file name's session qualifier. The local side
is the file name's SenderCompID (`FIX.4.4-<SENDER>-<TARGET>`), else the sender of NewOrderSingle / Cancel /
Replace requests, else the target of ExecutionReports. Per direction and per sequence epoch the importer tracks
MsgSeqNum(34): gaps (too-high numbers; counted with the number of missing messages), PossDupFlag(43) resends that
fill a gap (accepted) or repeat a message already seen (dropped, counted), SequenceReset(35=4) gap fills and
resets, session resets (Logon with ResetSeqNumFlag(141)=Y, or a new Logon at MsgSeqNum 1), out-of-order lines and
regressions. Messages are placed in MsgSeqNum order within each direction (out-of-order lines and late resends go
back to their place). Unrecovered inbound gaps are reported; a CumQty(14) jump they cause becomes a `derived` fill
(`fill_id` null, `liquidity` unknown, price from AvgPx(6) when exact, reason names the jump) so cumulative
quantities stay true. Fills before the log started (CumQty > 0 on the first report of an order the log never
submitted) are reported and not invented. Byte-identical lines (overlapping rotated files, a FileStore copy of a
logged message) are dropped and counted. Files are read in time order (first line time, else first SendingTime),
FileStores last (they fill outbound messages missing from the logs, placed by SendingTime).

Order-entry semantics:

- Outbound 35=D / F / G -> submit_sent / cancel_sent / amend_sent, `ts_local_send` = the log-line time (the
  engine writes the line just before the socket write), else SendingTime(52) (the local clock); a PossDup resend
  of a request missing from the log uses OrigSendingTime(122). Inbound 35=8 / 9 / j / 3 get `ts_local_recv` only
  from the log-line time: inbound 52 is the venue's clock. `ts_precision` is the coarsest precision among the
  event's time sources.
- ExecutionReport kinds by ExecType(150), version-aware: FIX 4.2 partial / full fills 150=1 / 2 and FIX 4.3+ 150=F
  (also 1 / 2 when a venue keeps them) -> fill; FIX 4.2 ExecTransType(20)=1 bust / 2 correct (ExecRefID 19) ->
  fill_bust / fill_correct, 20=3 status -> no event; FIX 4.4+ 150=H / G -> bust / correct; 150=I (order status) ->
  no event; without ExecType (FIX 4.0-style) the kind comes from OrdStatus(39). 0 ack, 5 amend_ack (OrderQty 38 =
  new total), 8 reject (post_only_reject when the order carries ExecInst 18=6 and Text 58 mentions post-only),
  C expire, D restate, 3 done; pending A / E / 6 and 7 / 9 / B / J-N are counted without an event. A
  dictionary's custom ExecType values map by their description.
- 150=4 is `cancel_ack` when the user asked for it (an OrderCancelRequest for the order, OrigClOrdID(41) echoed, or
  an OrderMassCancelRequest 35=q / OrderMassActionRequest 35=CA sent before it), else `expire` (an unsolicited
  venue cancel: self-trade prevention, cancel-on-disconnect) when the log shows the order's submission; drop-copy
  logs without the request side keep `cancel_ack`.
- TimeInForce(59): 0 day, 1 gtc, 3 ioc, 4 fok, 5 gtx, 6 gtd, others `other`; ExecInst(18)=6 -> `post_only`. An order
  whose messages never carry 59 is Day (the FIX default; counted as `tif_default_day`).
- Fills: ExecID(17), LastQty(32), LastPx(31), CumQty(14), LeavesQty(151); liquidity LastLiquidityInd(851) 1 maker
  / 2 taker, else AggressorIndicator(1057) Y taker / N maker (FIX 5.0, CME), else a dictionary's custom liquidity
  field; Commission 12 (+ CommCurrency 479). TransactTime(60): match_time on fills, gateway_time otherwise;
  without 60 the venue's SendingTime(52) is used with kind unknown (counted). FIX 5.0SP2 picosecond times are
  truncated to ns (counted).
- Rejects of requests: 35=9 OrderCancelReject -> cancel_reject (434=1) / amend_reject (434=2); 35=j
  BusinessMessageReject and 35=3 session Reject referencing a 35=D / F / G (RefMsgType 372, BusinessRejectRefID
  379 or RefSeqNum 45) -> reject / cancel_reject / amend_reject. An outbound 35=3 (the local engine rejected a
  venue message) is counted; the venue's report is imported.
- Identity: venue order ids (37; `NONE` / `0` / empty are no id) across the venue; ClOrdID (11) / OrigClOrdID (41)
  chains within a session (a ClOrdID reused by a later NewOrderSingle starts a new order). A replace that keeps
  the order (150=5 with a new 37) aliases the new id (reason names it). A replace the venue answers with a new
  order (150=0 with a new OrderID and the request's OrigClOrdID) is a separate order whose `replaces_order_key`
  names the replaced one. Duplicates across sessions (an order-entry plus a drop-copy session of the same venue)
  are dropped by ExecID, or by the reported state when the drop copy uses its own ExecIDs.
- `fix.venue_name` names the real exchange (canonical `venue`, `order_key` prefix, instrument-map lookups;
  default `fix44`); `session_venues` maps sessions (file stem or counterparty CompID) to venues. Coinbase Exchange
  documents that a size decrease at the same price keeps priority: amend_acks of such replaces on venue
  `coinbase` carry `amend_keeps_priority = true` (SPEC 5.7.2); elsewhere it stays null.
- Optional QuickFIX data dictionaries (`data_dictionary`) remap tag numbers by name and enum values by description
  (`queuetruth.live.importers.fixdict`); tags they do not define are counted and kept in `raw`.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from queuetruth.errors import QtError
from queuetruth.live._common import (
    ID_RE,
    Record,
    ddiv_exact,
    dec,
    dec_text,
    dmul,
    dsub,
    dsum,
    epoch_ns,
    input_error,
    local_to_utc_s,
    rfc3339_ns,
)
from queuetruth.live.builder import ImportResult, LogBuilder
from queuetruth.live.importers._base import ImportOptions
from queuetruth.live.importers.fixdict import STANDARD_TAGS, FixDictionary, load_dictionary
from queuetruth.live.importers.fixwire import (
    coarsest,
    display,
    first_values,
    parse_fields,
    utc_ts,
)
from queuetruth.live.importers.quickfix import (
    LogStats,
    SessionHint,
    classify,
    expand_inputs,
    first_time,
    iter_log,
    read_event_log,
    read_seqnums,
    read_store,
)

VENUE = "fix44"
_NO_ID = {"", "NONE", "0", "UNKNOWN"}
_SESSION_TYPES = {"0", "1", "2", "4", "5", "A"}
_TIF = {"0": "day", "1": "gtc", "3": "ioc", "4": "fok", "5": "gtx", "6": "gtd"}
_ORD = {"1": "market", "2": "limit", "3": "stop", "4": "stop_limit"}
_SIDE = {"1": "buy", "3": "buy", "2": "sell", "4": "sell", "5": "sell", "6": "sell"}
_UTC = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$")
_GO = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$")
_FIX_OPTION_KEYS = (
    "delimiter",
    "local_ts_regex",
    "venue_name",
    "checksum",
    "data_dictionary",
    "session_venues",
    "instrument_tag",
)
#: ExecType -> kind (all FIX versions; `fill` covers 4.2's 1 / 2 and 4.3+'s F).
_EXEC_KIND = {
    "0": "new",
    "1": "fill",
    "2": "fill",
    "F": "fill",
    "3": "done",
    "4": "cancel",
    "5": "replace",
    "6": "pending",
    "A": "pending",
    "E": "pending",
    "8": "reject",
    "C": "expire",
    "D": "restate",
    "G": "correct",
    "H": "bust",
    "I": "status",
}
_ORDSTATUS_KIND = {
    "0": "new",
    "1": "fill",
    "2": "fill",
    "3": "done",
    "4": "cancel",
    "5": "replace",
    "6": "pending",
    "A": "pending",
    "E": "pending",
    "8": "reject",
    "C": "expire",
}
_APPL_VER = {"2": "FIX.4.0", "3": "FIX.4.1", "4": "FIX.4.2", "5": "FIX.4.3", "6": "FIX.4.4", "7": "FIX.5.0"}
_APPL_VER |= {"8": "FIX.5.0SP1", "9": "FIX.5.0SP2"}
#: Venues whose FIX documentation keeps queue priority on a same-price size decrease and that are not already in
#: `KEEP_PRIORITY_VENUES` (SPEC 5.7.2, E17: "Coinbase FIX").
FIX_KEEP_PRIORITY_VENUES = frozenset({"coinbase"})


# ---- options ------------------------------------------------------------------------------------------------------


@dataclass
class FixConfig:
    """Validated `fix` options (`LiveImportSpec.fix` plus the Python-only keys of `import_quickfix`)."""

    delimiter: str = "auto"
    local_ts_regex: re.Pattern[str] | None = None
    venue_name: str | None = None
    checksum: str = "exclude"
    data_dictionary: list[str] = field(default_factory=list)
    session_venues: dict[str, str] = field(default_factory=dict)
    instrument_tag: str | None = None
    tz: str | None = None

    @classmethod
    def from_extra(cls, extra: dict[str, Any]) -> FixConfig:
        """From `ImportOptions.extra` (`fix` dict and `tz`); invalid values fail `QT-CONFIG-001`."""
        raw = dict(extra.get("fix") or {})
        unknown = sorted(set(raw) - set(_FIX_OPTION_KEYS))
        if unknown:
            raise QtError("QT-CONFIG-001", f"fix: unknown option(s) {unknown}; known: {list(_FIX_OPTION_KEYS)}")
        cfg = cls()
        delim = str(raw.get("delimiter") or "auto")
        if delim.lower() not in ("auto", "soh") and delim != "^A" and len(delim) != 1:
            raise QtError("QT-CONFIG-001", f"fix.delimiter: 'auto', 'soh', '^A' or one character, got {delim!r}")
        if delim in ("=", "\n") or delim.isdigit():
            raise QtError("QT-CONFIG-001", f"fix.delimiter: {delim!r} cannot delimit FIX fields")
        cfg.delimiter = delim
        rx = raw.get("local_ts_regex")
        if rx:
            try:
                cfg.local_ts_regex = re.compile(str(rx))
            except re.error as e:
                raise QtError("QT-CONFIG-001", f"fix.local_ts_regex: {e}") from e
            if cfg.local_ts_regex.groups < 1:
                raise QtError("QT-CONFIG-001", "fix.local_ts_regex needs a capture group (or a group named ts)")
        vn = raw.get("venue_name")
        if vn is not None:
            cfg.venue_name = _venue(str(vn), "fix.venue_name")
        ck = str(raw.get("checksum") or "exclude")
        if ck not in ("exclude", "keep"):
            raise QtError("QT-CONFIG-001", f"fix.checksum: 'exclude' or 'keep', got {ck!r}")
        cfg.checksum = ck
        dd = raw.get("data_dictionary")
        if dd:
            cfg.data_dictionary = [str(dd)] if isinstance(dd, str) else [str(x) for x in dd]
        sv = raw.get("session_venues") or {}
        if not isinstance(sv, dict):
            raise QtError("QT-CONFIG-001", "fix.session_venues: an object {session stem or CompID: venue}")
        cfg.session_venues = {str(k): _venue(str(v), f"fix.session_venues[{k}]") for k, v in sv.items()}
        it = raw.get("instrument_tag")
        if it is not None:
            if not str(it).isdigit():
                raise QtError("QT-CONFIG-001", f"fix.instrument_tag: a tag number, got {it!r}")
            cfg.instrument_tag = str(int(str(it)))
        tz = extra.get("tz")
        if tz:
            if cfg.local_ts_regex is None:
                raise QtError(
                    "QT-CONFIG-001",
                    "tz: FIX UTCTimestamps and QuickFIX log prefixes are UTC; tz applies only to times captured by "
                    "fix.local_ts_regex",
                )
            local_to_utc_s(2026, 1, 1, 0, 0, 0, str(tz), "tz")  # validates the zone name
            cfg.tz = str(tz)
        return cfg


def _venue(v: str, what: str) -> str:
    if not v or not ID_RE.match(v) or v.startswith("sim:") or len(v) > 32:
        raise QtError(
            "QT-CONFIG-001",
            f"{what}: 1-32 characters of [A-Za-z0-9_.:-], not starting with 'sim:' (simulator logs), got {v!r}",
        )
    return v.lower()  # venue names are case-insensitive; canonical logs carry them in lower case (E53)


def fix_ts(text: str, what: str, loc: str) -> tuple[int, str]:
    """FIX UTCTimestamp `YYYYMMDD-HH:MM:SS[.f{1,12}]` -> (UTC ns, precision); `QT-INPUT-002` when malformed."""
    r = utc_ts(text)
    if r is None:
        raise input_error(loc, f"{what}: {text!r} is not a FIX UTCTimestamp")
    return r[0], r[1]


def _regex_ts(text: str, loc: str, tz: str | None) -> tuple[int, str]:
    """A time captured by `fix.local_ts_regex` -> (UTC ns, precision)."""
    t = text.strip()
    for rx in (_UTC, _GO):
        m = rx.match(t)
        if m:
            y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
            frac = m.group(7) or ""
            secs = local_to_utc_s(y, mo, d, h, mi, s, tz or "UTC", "log time", loc)
            prec = "s" if not frac else "ms" if len(frac) <= 3 else "us" if len(frac) <= 6 else "ns"
            return secs * 10**9 + int((frac + "000000000")[:9]), prec
    if t.isdigit():
        return epoch_ns(t, "auto", "log time", loc)
    return rfc3339_ns(t, "log time", loc, default_tz=tz)


class _Tags:
    """Tag numbers of the concepts read (standard numbers, or a data dictionary's numbers for the same names)."""

    def __init__(self, d: FixDictionary | None) -> None:
        for name, num in STANDARD_TAGS.items():
            setattr(self, name, d.tag(name) if d is not None else num)

    def __getattr__(self, name: str) -> str:  # pragma: no cover - typing aid
        raise AttributeError(name)


# ---- reading ------------------------------------------------------------------------------------------------------


@dataclass
class _Msg:
    f: dict[str, str]
    text: str
    local_ns: int | None
    local_prec: str | None
    location: str
    src: int
    line: int
    origin: str  # log | store
    hint: SessionHint | None
    fields: list[tuple[str, str]]
    mt: str
    sender: str
    target: str
    begin: str
    seq: int | None
    possdup: bool
    epoch: int = 0
    gap_fill: bool = False
    out: bool | None = None


@dataclass
class _Stream:
    """Sequence state of the messages one CompID sends in one session."""

    expected: int | None = None
    epoch: int = 0
    seen: set[int] = field(default_factory=set)
    missing: set[int] = field(default_factory=set)
    count: int = 0
    max_seq: int | None = None


@dataclass
class _Session:
    key: tuple[str, frozenset[str], str | None]
    begin: str
    comps: list[str]
    hint: SessionHint | None
    streams: dict[str, _Stream] = field(default_factory=dict)
    msgs: list[_Msg] = field(default_factory=list)
    store_msgs: list[_Msg] = field(default_factory=list)
    by_seq: dict[tuple[str, int, int], _Msg] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    local: str | None = None
    appl_ver: str | None = None
    epoch: int = 0
    venue: str = VENUE
    sid: str = ""
    order: int = 0

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def stream(self, sender: str) -> _Stream:
        st = self.streams.get(sender)
        if st is None:
            st = _Stream(epoch=self.epoch)
            self.streams[sender] = st
        return st

    def reset(self) -> None:
        """A new sequence epoch for both directions (unrecovered gaps of the old epoch are final)."""
        self.epoch += 1
        self.count("resets")
        for sender, st in self.streams.items():
            self._close(sender, st)
            st.expected, st.epoch, st.count, st.max_seq = None, self.epoch, 0, None
            st.seen, st.missing = set(), set()

    def _close(self, sender: str, st: _Stream) -> None:
        if st.missing:
            self.count(f"unrecovered_from:{sender}", len(st.missing))

    def close(self) -> None:
        for sender, st in self.streams.items():
            self._close(sender, st)
            st.missing = set()


class _Tracker:
    """MsgSeqNum bookkeeping (see the module docstring); `observe` returns False for a message to drop."""

    def observe(self, s: _Session, m: _Msg) -> bool:
        st = s.stream(m.sender)
        f = m.f
        if m.mt == "A" and m.origin == "log":
            if "1137" in f:
                s.appl_ver = _APPL_VER.get(f["1137"], f["1137"])
            restart = f.get("141") == "Y" or (m.seq == 1 and st.expected is not None and st.expected > 1)
            if restart and st.count > 0:
                s.reset()
                st = s.stream(m.sender)
        m.epoch = st.epoch
        if m.seq is None:
            s.count("no_msgseqnum")
            st.count += 1
            return True
        seq = m.seq
        if m.origin == "store":
            if seq in st.seen:
                s.count("store_dropped_seen")
                return False
            if seq in st.missing:
                st.missing.discard(seq)
                s.count("recovered_from_store")
            st.seen.add(seq)
            return True
        st.count += 1
        if st.expected is None:
            if seq != 1 and s.epoch == 0 and not st.seen:
                s.count(f"starts_at:{m.sender}={seq}")
            st.expected = seq + 1
        elif seq == st.expected:
            st.expected = seq + 1
        elif seq > st.expected:
            n = seq - st.expected
            s.count(f"gaps_from:{m.sender}")
            s.count(f"gap_msgs_from:{m.sender}", n)
            st.missing.update(range(st.expected, seq))
            st.expected = seq + 1
        elif seq in st.seen:
            if m.possdup:
                s.count("possdup_dropped")
                return False
            s.count("seq_conflicts")
        elif seq in st.missing:
            st.missing.discard(seq)
            if m.possdup:
                m.gap_fill = True
                s.count("recovered_by_resend")
            else:
                s.count("out_of_order")
        elif m.possdup:
            s.count("possdup_unknown")
        else:
            s.count("seq_regressions")
        st.seen.add(seq)
        st.max_seq = seq if st.max_seq is None else max(st.max_seq, seq)
        if m.mt == "4":
            new = f.get("36", "")
            if new.isdigit():
                nv = int(new)
                if f.get("123") == "Y":
                    filled = {x for x in st.missing if seq <= x < nv}
                    st.missing -= filled
                    s.count("gap_filled_admin", len(filled))
                    if st.expected < nv:
                        st.expected = nv
                else:
                    lost = {x for x in st.missing if x < nv}
                    if lost:
                        s.count(f"unrecovered_from:{m.sender}", len(lost))
                        st.missing -= lost
                    if nv < (st.expected or 0):
                        s.count("seq_reset_backward")
                        st.seen = {x for x in st.seen if x < nv}
                    st.expected = nv
                    s.count("seq_reset_hard")
        return True


# ---- semantics ----------------------------------------------------------------------------------------------------


@dataclass
class _OState:
    """Per-order reconciliation state (CumQty vs imported fills)."""

    net: Decimal = Decimal(0)
    base: Decimal = Decimal(0)
    seen_cum: bool = False
    fills: dict[str, Decimal] = field(default_factory=dict)
    last_cum: Decimal | None = None
    last_avg: Decimal | None = None
    qty: Decimal | None = None
    px: Decimal | None = None


class _Fix:
    """Canonical events of one venue's sessions (see the module docstring)."""

    def __init__(
        self,
        b: LogBuilder,
        cfg: FixConfig,
        tags: _Tags,
        d: FixDictionary | None,
        cancel_cids: set[str],
        cancel_oids: set[str],
        mass_cancels: list[int],
    ) -> None:
        self.b = b
        self.cfg = cfg
        self.T = tags
        self.d = d
        self.liq_fields = d.liquidity_fields() if d is not None else {}
        self.cancel_cids = cancel_cids
        self.cancel_oids = cancel_oids
        self.mass_cancels = sorted(mass_cancels)
        self.state: dict[int, _OState] = {}
        self.seen_er: dict[tuple[str, ...], tuple[Any, ...]] = {}
        self.seen_state: dict[tuple[str, ...], tuple[Any, ...]] = {}
        self.seen_req: set[tuple[str, ...]] = set()
        self.counters: dict[str, int] = {}
        self.keep_priority = b.venue.lower() in FIX_KEEP_PRIORITY_VENUES

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def st(self, h: int) -> _OState:
        r = self.b.root(h)
        s = self.state.get(r)
        if s is None:
            s = _OState()
            self.state[r] = s
        return s

    # -- identity --

    def _oid(self, f: dict[str, str]) -> str | None:
        v = f.get(self.T.OrderID, "").strip()
        return None if v.upper() in _NO_ID else v

    def _static(self, f: dict[str, str]) -> dict[str, Any]:
        T = self.T
        ei = f.get(T.ExecInst, "")
        po = "6" in ei.split() if ei else None
        tif = _TIF.get(f.get(T.TimeInForce, ""), "other") if T.TimeInForce in f else None
        if po:
            tif = "post_only"
        inst = None
        for tag in (self.cfg.instrument_tag,) if self.cfg.instrument_tag else (T.Symbol, T.SecurityID):
            if f.get(tag):
                inst = f[tag]
                break
        return {
            "account": f.get(T.Account) or None,
            "instrument": inst,
            "side": _SIDE.get(f.get(T.Side, "")),
            "order_type": _ORD.get(f.get(T.OrdType, ""), "other") if T.OrdType in f else None,
            "tif": tif,
            "post_only": po,
        }

    def _lookup(self, f: dict[str, str], scope: str) -> int | None:
        b, T = self.b, self.T
        oid = self._oid(f)
        h_oid = b.find_order(oid=oid) if oid else None
        if h_oid is not None:
            return h_oid
        orig, cid = f.get(T.OrigClOrdID) or None, f.get(T.ClOrdID) or None
        h = b.find_order(cid=orig, cid_scope=scope) if orig else None
        if h is None and cid:
            h = b.find_order(cid=cid, cid_scope=scope)
        return h

    def _order(
        self,
        f: dict[str, str],
        rec: Record,
        scope: str,
        *,
        record: bool,
        fresh: bool = False,
        replaces: int | None = None,
    ) -> int:
        b, T = self.b, self.T
        cid, orig = f.get(T.ClOrdID) or None, f.get(T.OrigClOrdID) or None
        oid = self._oid(f)
        known = None if fresh else self._lookup(f, scope)
        known_oid = b.order_field(known, "venue_order_id") if known is not None else None
        pass_oid = oid if fresh or known_oid is None or known_oid == oid else None
        loc = rec.location
        has_qty = record and T.OrderQty in f
        static = self._static(f)
        h = b.order(
            oid=pass_oid,
            cid=cid,
            orig_cid=orig,
            rec=rec,
            record=has_qty,
            qty=dec(f.get(T.OrderQty), "OrderQty(38)", loc) if has_qty else None,
            qty_text=dec_text(f.get(T.OrderQty)) if has_qty else None,
            px=dec(f.get(T.Price), "Price(44)", loc) if record and T.Price in f else None,
            px_text=dec_text(f.get(T.Price)) if record and T.Price in f else None,
            cid_scope=scope,
            fresh=fresh,
            replaces=replaces,
            **static,
        )
        if oid is not None and pass_oid is None:
            b.alias_oid(h, oid)
        if static["post_only"]:
            b.flag(h, "post_only")
        return h

    # -- outbound --

    def outbound(self, m: _Msg, rec: Record, scope: str) -> None:
        b, T, f = self.b, self.T, m.f
        loc = rec.location
        t52 = utc_ts(f[T.SendingTime]) if T.SendingTime in f else None
        local, prec = m.local_ns, m.local_prec
        if m.possdup and T.OrigSendingTime in f and utc_ts(f[T.OrigSendingTime]) is not None:
            t122 = utc_ts(f[T.OrigSendingTime])
            assert t122 is not None
            local, prec = t122[0], t122[1]
            self.count("resent_request_original_time")
        elif local is None and t52 is not None:
            local, prec = t52[0], t52[1]
        elif local is not None and t52 is not None:
            prec = coarsest(prec, t52[1])
        if local is None:
            self.count("outbound_without_time")
            return
        cid = f.get(T.ClOrdID) or ""
        key = (scope, str(m.epoch), m.mt, cid)
        if cid and m.possdup and key in self.seen_req:
            self.count("possdup_requests_dropped")
            b.dropped_duplicate()
            return
        if cid:
            self.seen_req.add(key)
        common: dict[str, Any] = {
            "rec": rec,
            "source": "fix_dropcopy",
            "kind": "unknown",
            "precision": prec or "ns",
            "ts_local_send": local,
        }
        if m.gap_fill:
            common["reason"] = "PossDup resend (43=Y) of a request missing from the log"
        if m.mt == "D":
            fresh = False
            known = b.find_order(cid=cid, cid_scope=scope) if cid else None
            if known is not None and b.has_flag(known, "submitted"):
                fresh = True
                self.count("clordid_reused")
            h = self._order(f, rec, scope, record=True, fresh=fresh)
            b.flag(h, "submitted")
            qty, px = dec(f.get(T.OrderQty), "38", loc), dec(f.get(T.Price), "44", loc)
            s = self.st(h)
            s.qty, s.px = (qty if s.qty is None else s.qty), (px if s.px is None else s.px)
            b.event(h, "submit_sent", qty=qty, px=px, **common)
        elif m.mt == "G":
            h = self._order(f, rec, scope, record=False)
            b.event(h, "amend_sent", qty=dec(f.get(T.OrderQty), "38", loc), px=dec(f.get(T.Price), "44", loc), **common)
        else:
            h = self._order(f, rec, scope, record=False)
            b.event(h, "cancel_sent", **common)

    # -- inbound --

    def _times(self, m: _Msg, kind_if_60: str) -> dict[str, Any] | None:
        """Event time fields of an inbound message: TransactTime(60) with `kind_if_60`; without it the venue's
        SendingTime(52) (kind unknown) when there is no log-line time; None when the message has no time."""
        T, f = self.T, m.f
        ts = utc_ts(f[T.TransactTime]) if T.TransactTime in f else None
        kind = kind_if_60
        if ts is not None and ts[2]:
            self.count("sub_ns_truncated")
        if ts is None:
            kind = "unknown"
            if T.TransactTime in f:
                self.count("bad_transacttime")
            elif m.mt == "8":
                self.count("no_transacttime")
            t52 = utc_ts(f[T.SendingTime]) if T.SendingTime in f else None
            if t52 is not None and m.local_ns is None:
                ts = t52
                self.count("exchange_time_from_sendingtime")
        if ts is None and m.local_ns is None:
            return None
        return {
            "source": "fix_dropcopy",
            "kind": kind,
            "precision": coarsest(ts[1] if ts else None, m.local_prec if m.local_ns is not None else None),
            "ts_exchange": ts[0] if ts else None,
            "ts_local_recv": m.local_ns,
        }

    def _exec_kind(self, f: dict[str, str]) -> tuple[str, str]:
        T = self.T
        tt = f.get(T.ExecTransType)
        if tt == "1":
            return "bust", "20=1"
        if tt == "2":
            return "correct", "20=2"
        if tt == "3":
            return "status", "ExecTransType 20=3 (status)"
        et = f.get(T.ExecType, "")
        if et == "":
            os_ = f.get(T.OrdStatus, "")
            k = _ORDSTATUS_KIND.get(os_, "unknown")
            if k == "fill" and not (dec(f.get(T.LastQty), "32") or 0) > 0:
                k = "status"
            self.count("exec_type_from_ordstatus")
            return k, f"OrdStatus 39={os_ or 'missing'} (no ExecType)"
        k = _EXEC_KIND.get(et)
        if k is not None:
            return k, f"ExecType 150={et}"
        if self.d is not None:
            code = self.d.exec_type(et)
            if code is not None and code in _EXEC_KIND:
                self.count("exec_type_from_dictionary")
                return _EXEC_KIND[code], f"ExecType 150={et} (dictionary)"
        return "other", f"ExecType 150={et}"

    def _liquidity(self, f: dict[str, str]) -> str:
        T = self.T
        v = f.get(T.LastLiquidityInd)
        if v:
            return {"1": "maker", "2": "taker"}.get(v, "unknown")
        v = f.get(T.AggressorIndicator)
        if v:
            return {"Y": "taker", "N": "maker"}.get(v, "unknown")
        for tag, vals in self.liq_fields.items():
            if tag in f and f[tag] in vals:
                return vals[f[tag]]
        return "unknown"

    def _duplicate_er(self, m: _Msg, skey: Any, kind_text: str) -> bool:
        T, f = self.T, m.f
        ident = self._oid(f) or f.get(T.ClOrdID) or ""
        exec_id = f.get(T.ExecID)
        if exec_id:
            k1 = (ident, f.get(T.ClOrdID, ""), exec_id, f.get(T.ExecType, ""), f.get(T.ExecTransType, ""))
            first = self.seen_er.get(k1)
            if first is not None:
                if first != skey:
                    self.count("cross_session_duplicates")
                else:
                    self.count("possdup_reports_dropped" if m.possdup else "repeated_reports_dropped")
                self.b.dropped_duplicate()
                return True
            self.seen_er[k1] = skey
        k2 = tuple(f.get(t, "") for t in (T.ExecType, T.OrdStatus, T.CumQty, T.LastQty, T.LastPx, T.TransactTime))
        k2 = (ident, *k2)
        first2 = self.seen_state.get(k2)
        if first2 is not None and first2 != skey:
            self.count("cross_session_duplicates")
            self.b.dropped_duplicate()
            return True
        self.seen_state.setdefault(k2, skey)
        return False

    def _reconcile(self, h: int, f: dict[str, str], rec: Record, common: dict[str, Any], delta: Decimal) -> Any:
        """CumQty(14) check; emits a derived fill for quantity the log lost. Returns the cum to record."""
        T = self.T
        s = self.st(h)
        cum = dec(f.get(T.CumQty), "CumQty(14)", rec.location)
        avg = dec(f.get(T.AvgPx), "AvgPx(6)", rec.location)
        after = dsum([s.net, delta])
        if cum is None:
            s.net = after
            return None
        if not s.seen_cum and not self.b.has_flag(h, "submitted") and cum > after:
            s.base = dsub(cum, after)
            self.count("pre_capture_fill_qty_orders")
        target = dsub(cum, s.base)
        if target > after:
            missing = dsub(target, after)
            px = None
            if avg is not None and s.last_avg is not None and s.last_cum is not None:
                px = ddiv_exact(dsub(dmul(avg, cum), dmul(s.last_avg, s.last_cum)), missing)
            elif avg is not None and s.last_cum in (None, Decimal(0)) and s.net == 0:
                px = avg
            prev = s.last_cum if s.last_cum is not None else Decimal(0)
            kw = {
                **common,
                "kind": "unknown",
                "reason": f"not in the log: CumQty(14) {format(prev, 'f')} -> {format(cum, 'f')} without a fill "
                "report (unrecovered sequence gap?)",
            }
            self.b.event(
                h, "fill", qty=missing, px=px, liquidity="unknown", derived=True, soft_px=True, keep_derived=True, **kw
            )
            self.count("derived_gap_fills")
            after = target
        elif target < after:
            s.base = dsub(cum, after)
            self.count("cumqty_below_imported_fills")
        s.net = after
        s.seen_cum = True
        s.last_cum = cum
        s.last_avg = avg if avg is not None else s.last_avg
        return cum if s.base == 0 else None

    def _solicited_cancel(self, h: int, f: dict[str, str], m: _Msg) -> bool:
        b, T = self.b, self.T
        if f.get(T.OrigClOrdID):
            return True
        if any(c in self.cancel_cids for c in b.order_field(h, "client_ids")):
            return True
        oid = b.order_field(h, "venue_order_id")
        if oid is not None and oid in self.cancel_oids:
            return True
        t = m.local_ns
        if t is None and T.TransactTime in f and utc_ts(f[T.TransactTime]) is not None:
            t = utc_ts(f[T.TransactTime])[0]  # type: ignore[index]
        return any(q <= t for q in self.mass_cancels) if t is not None else bool(self.mass_cancels)

    def exec_report(self, m: _Msg, rec: Record, scope: str, skey: Any) -> None:
        b, T, f = self.b, self.T, m.f
        loc = rec.location
        kind, why = self._exec_kind(f)
        if kind in ("pending", "status", "other", "unknown"):
            b.skip(f"{why} (no canonical event)")
            return
        if self._duplicate_er(m, skey, why):
            return
        oid = self._oid(f)
        known = self._lookup(f, scope)
        known_oid = b.order_field(known, "venue_order_id") if known is not None else None
        prior_oid = known_oid
        if kind == "new" and known is not None and oid and known_oid and known_oid != oid:
            orig = f.get(T.OrigClOrdID)
            h = self._order(f, rec, scope, record=True, fresh=True, replaces=known if orig else None)
            self.count("replaced_by_new_order" if orig else "new_order_id_without_link")
            prior_oid = None
        else:
            h = self._order(f, rec, scope, record=True)
        common = self._times(m, "match_time" if kind == "fill" else "gateway_time")
        if common is None:
            self.count("reports_without_time")
            return
        common["rec"] = rec
        if m.gap_fill:
            common["reason"] = "PossDup resend (43=Y) received after a gap"
        q = lambda tag: dec(f.get(tag), f"tag {tag}", loc)  # noqa: E731
        text = f.get(T.Text)
        s = self.st(h)
        if kind == "fill":
            qty = q(T.LastQty)
            if qty is None or qty <= 0:
                self.count("fills_without_lastqty")
                b.skip("fill report without LastQty(32) > 0")
                return
            cum = self._reconcile(h, f, rec, common, qty)
            comm = q(T.Commission)
            if comm is not None and f.get(T.CommType, "3") != "3":
                b.warn(f"{b.venue}: CommType(13)={f.get(T.CommType)} is not absolute; fee left as reported")
            fid = f.get(T.ExecID) or None
            if fid is not None:
                s.fills[fid] = qty
            b.event(
                h,
                "fill",
                qty=qty,
                px=q(T.LastPx),
                leaves=q(T.LeavesQty),
                cum=cum,
                fill_id=fid,
                liquidity=self._liquidity(f),
                fee=comm,
                fee_ccy=f.get(T.CommCurrency) or None,
                **common,
            )
        elif kind in ("bust", "correct"):
            ref = f.get(T.ExecRefID) or None
            old = s.fills.get(ref) if ref else None
            if old is None:
                self.count("bust_correct_of_unknown_fill")
                b.skip(f"trade {kind} of a fill not in the log ({why})")
                self._reconcile(h, f, rec, common, Decimal(0))
                return
            new = q(T.LastQty) if kind == "correct" else None
            delta = -old if kind == "bust" else (dsub(new, old) if new is not None else Decimal(0))
            cum = self._reconcile(h, f, rec, common, delta)
            if kind == "bust":
                s.fills.pop(str(ref), None)
            elif new is not None and ref is not None:
                s.fills[ref] = new
            b.event(
                h,
                "fill_bust" if kind == "bust" else "fill_correct",
                qty=q(T.LastQty),
                px=q(T.LastPx),
                leaves=q(T.LeavesQty),
                cum=cum,
                fill_id=f.get(T.ExecID) or None,
                ref_fill_id=ref,
                **{**common, "reason": text or common.get("reason")},
            )
        else:
            cum = self._reconcile(h, f, rec, common, Decimal(0))
            if kind == "new":
                b.event(h, "ack", qty=q(T.OrderQty), px=q(T.Price), leaves=q(T.LeavesQty), cum=cum, **common)
                s.qty = q(T.OrderQty) if q(T.OrderQty) is not None else s.qty
                s.px = q(T.Price) if q(T.Price) is not None else s.px
            elif kind == "replace":
                new_oid = self._oid(f)
                reason = f"150=5 new OrderID {new_oid}" if new_oid and prior_oid and new_oid != prior_oid else None
                nq, npx = q(T.OrderQty), q(T.Price)
                keeps = None
                if self.keep_priority and nq is not None and s.qty is not None:
                    same_px = npx is None or s.px is None or npx == s.px
                    if same_px and nq < s.qty:
                        keeps = True
                b.event(
                    h,
                    "amend_ack",
                    qty=nq,
                    px=npx,
                    leaves=q(T.LeavesQty),
                    cum=cum,
                    keeps=keeps,
                    **{**common, "reason": reason or common.get("reason")},
                )
                s.qty = nq if nq is not None else s.qty
                s.px = npx if npx is not None else s.px
            elif kind == "cancel":
                if self._solicited_cancel(h, f, m) or not b.has_flag(h, "submitted"):
                    b.event(h, "cancel_ack", leaves=q(T.LeavesQty), cum=cum, **{**common, "reason": text})
                else:
                    self.count("unsolicited_cancels")
                    reason = "150=4 unsolicited cancel (no OrderCancelRequest or mass cancel sent)"
                    reason += f": {text}" if text else ""
                    b.event(h, "expire", leaves=q(T.LeavesQty), cum=cum, **{**common, "reason": reason})
            elif kind == "reject":
                reason = " ".join(x for x in (f"103={f[T.OrdRejReason]}" if T.OrdRejReason in f else None, text) if x)
                po = b.has_flag(h, "post_only") and text is not None and "post" in text.lower()
                b.event(
                    h,
                    "post_only_reject" if po else "reject",
                    leaves=q(T.LeavesQty),
                    cum=cum,
                    **{**common, "reason": reason or None},
                )
            elif kind == "expire":
                b.event(h, "expire", leaves=q(T.LeavesQty), cum=cum, **{**common, "reason": text})
            elif kind == "restate":
                reason = " ".join(
                    x
                    for x in (
                        f"378={f[T.ExecRestatementReason]}" if T.ExecRestatementReason in f else None,
                        text,
                    )
                    if x
                )
                b.event(
                    h,
                    "restate",
                    qty=q(T.OrderQty),
                    px=q(T.Price),
                    leaves=q(T.LeavesQty),
                    cum=cum,
                    **{**common, "reason": reason or None},
                )
            elif kind == "done":
                b.event(h, "done", leaves=q(T.LeavesQty), cum=cum, **{**common, "reason": text or "150=3 done for day"})

    def cancel_reject(self, m: _Msg, rec: Record, scope: str) -> None:
        T, f = self.T, m.f
        key = (scope, "9", f.get(T.ClOrdID, ""), f.get(T.OrigClOrdID, ""), f.get(T.TransactTime, ""))
        if m.possdup and key in self.seen_req:
            self.count("possdup_reports_dropped")
            self.b.dropped_duplicate()
            return
        self.seen_req.add(key)
        common = self._times(m, "gateway_time")
        if common is None:
            self.count("reports_without_time")
            return
        h = self._order(f, rec, scope, record=False)
        resp = f.get(T.CxlRejResponseTo, "1")
        reason = " ".join(
            x
            for x in ("35=9", f"434={resp}", f"102={f[T.CxlRejReason]}" if T.CxlRejReason in f else None, f.get(T.Text))
            if x
        )
        self.b.event(h, "cancel_reject" if resp == "1" else "amend_reject", rec=rec, reason=reason, **common)

    def request_reject(self, m: _Msg, rec: Record, scope: str, ref: _Msg | None, ref_type: str | None) -> None:
        """35=j / 35=3 rejecting one of our 35=D / F / G requests."""
        T, f = self.T, m.f
        if ref is not None and ref.mt in ("D", "F", "G"):
            rtype, cid = ref.mt, ref.f.get(T.ClOrdID)
        else:
            rtype, cid = ref_type, (f.get(T.BusinessRejectRefID) if m.mt == "j" else None)
        if rtype not in ("D", "F", "G") or not cid:
            self.b.skip(f"35={m.mt} reject not linked to an order request")
            return
        h = self.b.find_order(cid=cid, cid_scope=scope)
        if h is None:
            self.b.skip(f"35={m.mt} reject of an unknown ClOrdID")
            return
        common = self._times(m, "gateway_time")
        if common is None:
            self.count("reports_without_time")
            return
        code = (
            f"380={f[T.BusinessRejectReason]}"
            if m.mt == "j" and T.BusinessRejectReason in f
            else f"373={f[T.SessionRejectReason]}"
            if m.mt == "3" and T.SessionRejectReason in f
            else None
        )
        reason = " ".join(x for x in (f"35={m.mt}", f"372={rtype}", code, f.get(T.Text)) if x)
        et = {"D": "reject", "F": "cancel_reject", "G": "amend_reject"}[rtype]
        self.count(f"request_rejects_{m.mt}")
        self.b.event(h, et, rec=rec, reason=reason, **common)


# ---- driver -------------------------------------------------------------------------------------------------------


def _session_key(m_begin: str, sender: str, target: str, hint: SessionHint | None) -> tuple[Any, ...]:
    return (m_begin, frozenset((sender, target)), hint.qualifier if hint is not None else None)


def _match_comp(part: str | None, comps: list[str]) -> str | None:
    if not part:
        return None
    for c in comps:
        if part == c:
            return c
    for c in sorted(comps, key=len, reverse=True):
        if part.startswith(c + "_"):
            return c
    return None


def _decide_local(s: _Session) -> None:
    comps = s.comps
    local = None
    if s.hint is not None:
        local = _match_comp(s.hint.sender, comps)
        if local is None:
            other = _match_comp(s.hint.target, comps)
            if other is not None and len(comps) == 2:
                local = comps[0] if comps[1] == other else comps[1]
    if local is None:
        votes: dict[str, int] = {}
        for m in [*s.msgs, *s.store_msgs]:
            if m.mt in ("D", "F", "G", "q"):
                votes[m.sender] = votes.get(m.sender, 0) + 1
            elif m.mt in ("8", "9"):
                votes[m.target] = votes.get(m.target, 0) + 1
        if votes:
            local = max(sorted(votes), key=lambda c: votes[c])
    s.local = local
    remote = next((c for c in comps if c != local), "?") if local else None
    s.sid = f"{s.begin}:{local}->{remote}" if local else f"{s.begin}:{'<->'.join(sorted(comps))}"
    for m in [*s.msgs, *s.store_msgs]:
        m.out = (m.sender == local) if local else m.mt in ("D", "F", "G", "q")


def _venue_time(m: _Msg) -> int | None:
    """When the venue sent a message originally: OrigSendingTime(122), else TransactTime(60), else SendingTime."""
    for tag in ("122", "60", "52"):
        t = utc_ts(m.f[tag]) if tag in m.f else None
        if t is not None:
            return t[0]
    return None


def _reorder(msgs: list[_Msg]) -> list[_Msg]:
    """Arrival order with (1) each direction's messages in (epoch, MsgSeqNum) order at that direction's arrival
    positions (out-of-order lines), and (2) PossDup resends that filled a gap moved back to where they belong: after
    their sequence predecessor and after the requests we sent before the venue sent them (OrigSendingTime /
    TransactTime), so a resent reply never precedes its request."""
    late = [m for m in msgs if m.gap_fill and m.seq is not None]
    base = [m for m in msgs if not (m.gap_fill and m.seq is not None)]
    out = list(base)
    for direction in (True, False):
        idx = [i for i, m in enumerate(base) if m.out is direction and m.seq is not None]
        ordered = sorted((base[i] for i in idx), key=lambda m: (m.epoch, m.seq))
        for i, m in zip(idx, ordered, strict=True):
            out[i] = m
    for m in sorted(late, key=lambda x: (x.epoch, x.seq)):
        pos = 0
        for i, x in enumerate(out):
            if x.sender == m.sender and x.epoch == m.epoch and x.seq is not None and x.seq < m.seq:  # type: ignore[operator]
                pos = max(pos, i + 1)
        t = _venue_time(m)
        if t is not None:
            for i, x in enumerate(out):
                tx = _time_key(x)
                if x.out is not m.out and tx is not None and tx <= t:
                    pos = max(pos, i + 1)
        out.insert(pos, m)
    return out


def _time_key(m: _Msg) -> int | None:
    if m.local_ns is not None:
        return m.local_ns
    t = utc_ts(m.f.get("52", "")) if "52" in m.f else None
    return t[0] if t else None


def _place_store(msgs: list[_Msg], extra: list[_Msg]) -> list[_Msg]:
    """FileStore-only messages merged in by SendingTime (before the first log message not earlier than it: a
    request precedes replies stamped in the same millisecond; untimed ones at the end)."""
    if not extra:
        return list(msgs)
    keys: list[int] = []
    run: int | None = None
    for x in msgs:
        t = _time_key(x)
        run = t if run is None else (run if t is None else max(run, t))
        keys.append(run if run is not None else -(2**62))
    slots: dict[int, list[_Msg]] = {}
    for m in sorted(extra, key=lambda x: (_time_key(x) is None, _time_key(x) or 0, x.seq or 0)):
        t = _time_key(m)
        pos = len(msgs) if t is None else bisect.bisect_left(keys, t)  # a request precedes same-time replies
        slots.setdefault(pos, []).append(m)
    out: list[_Msg] = []
    for i, x in enumerate(msgs):
        out.extend(slots.get(i, []))
        out.append(x)
    out.extend(slots.get(len(msgs), []))
    return out


def _fmt_counts(c: dict[str, int]) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(c.items()) if v)


def import_fix44(paths: list[str], opts: ImportOptions) -> ImportResult:
    """FIX logs (QuickFIX FileLog / FileStore files or plain FIX logs; outbound 35=D/F/G, inbound 35=8/9/j/3) ->
    canonical rows (see the module docstring)."""
    cfg = FixConfig.from_extra(opts.extra)
    dictionary = load_dictionary(cfg.data_dictionary)
    tags = _Tags(dictionary)
    files = [classify(p) for p in expand_inputs(paths)]
    logs = [s for s in files if s.kind in ("messages_log", "messages")]
    stores = [s for s in files if s.kind == "store_body"]
    events = [s for s in files if s.kind == "event_log"]
    seqfiles = [s for s in files if s.kind in ("store_seqnums", "store_senderseqnums", "store_targetseqnums")]

    def log_key(s: Any) -> tuple[Any, ...]:
        t = first_time(s.path)
        rot = {"backup": 0, "quickfixj": 1, "current": 1, "generic": 1}.get(s.naming, 1)
        return (t is None, t or 0, rot, s.backup_n or 0, s.path)

    logs.sort(key=log_key)
    problems: dict[str, int] = {}
    kept_bad: dict[str, int] = {}
    formats: dict[str, int] = {}
    file_notes: list[str] = []
    seen_raw: set[bytes] = set()
    dup_lines = 0
    store_in_log = 0
    sessions: dict[tuple[Any, ...], _Session] = {}
    tracker = _Tracker()
    unknown_tags: dict[str, int] = {}
    extra_skips: dict[str, int] = {}

    def regex_ts(text: str, loc: str) -> tuple[int, str]:
        return _regex_ts(text, loc, cfg.tz)

    def accept(
        text: str,
        problem: str | None,
        local: int | None,
        prec: str | None,
        loc: str,
        src: int,
        line: int,
        origin: str,
        hint: SessionHint | None,
        seq_hint: int | None,
    ) -> None:
        nonlocal dup_lines, store_in_log
        if problem in ("truncated", "malformed"):
            problems[problem] = problems.get(problem, 0) + 1
            return
        if problem is not None:
            if cfg.checksum == "exclude":
                problems[problem] = problems.get(problem, 0) + 1
                return
            kept_bad[problem] = kept_bad.get(problem, 0) + 1
        fields = parse_fields(text)
        if fields is None or not fields or fields[0][0] != "8":
            problems["malformed"] = problems.get("malformed", 0) + 1
            return
        digest = hashlib.blake2b(text.encode("latin-1", "replace"), digest_size=16).digest()
        if digest in seen_raw:
            if origin == "store":
                store_in_log += 1
            else:
                dup_lines += 1
            return
        seen_raw.add(digest)
        f = first_values(fields)
        mt = f.get("35", "")
        sender, target = f.get("49", ""), f.get("56", "")
        begin = f.get("8", "")
        seq_txt = f.get("34", "")
        seq = int(seq_txt) if seq_txt.isdigit() else seq_hint
        key = _session_key(begin, sender, target, hint)
        s = sessions.get(key)
        if s is None:
            s = _Session(key=key, begin=begin, comps=sorted({sender, target}), hint=hint, order=len(sessions))
            sessions[key] = s
        elif s.hint is None and hint is not None:
            s.hint = hint
        m = _Msg(
            f=f,
            text=text,
            local_ns=local,
            local_prec=prec,
            location=loc,
            src=src,
            line=line,
            origin=origin,
            hint=hint,
            fields=fields,
            mt=mt,
            sender=sender,
            target=target,
            begin=begin,
            seq=seq,
            possdup=f.get("43") == "Y" or f.get("97") == "Y",
        )
        if not tracker.observe(s, m):
            return
        if mt in _SESSION_TYPES:
            s.skip(f"session message 35={mt}")
            return
        if seq is not None:
            s.by_seq[(sender, m.epoch, seq)] = m
        (s.store_msgs if origin == "store" else s.msgs).append(m)
        if dictionary is not None:
            for tag, _ in fields:
                if tag not in dictionary.names:
                    unknown_tags[tag] = unknown_tags.get(tag, 0) + 1

    for src, sf in enumerate(logs):
        stats = LogStats()
        for ll in iter_log(
            sf.path, delimiter=cfg.delimiter, regex=cfg.local_ts_regex, parse_regex_ts=regex_ts, stats=stats
        ):
            accept(
                ll.text,
                ll.problem,
                ll.local_ns,
                ll.local_prec,
                f"{sf.path}:{ll.line_no}",
                src,
                ll.line_no,
                "log",
                sf.hint,
                None,
            )
        for k, v in stats.formats.items():
            formats[k] = formats.get(k, 0) + v
        if stats.non_fix_lines:
            extra_skips["log line without a FIX message"] = (
                extra_skips.get("log line without a FIX message", 0) + stats.non_fix_lines
            )
        if stats.ts_backwards:
            file_notes.append(
                f"{sf.path}: log time went backwards {stats.ts_backwards} time(s) (max {stats.max_backwards_ns} ns; "
                "clock step?): file order kept"
            )
        if stats.formats.get("unrecognised_prefix"):
            file_notes.append(
                f"{sf.path}: {stats.formats['unrecognised_prefix']} line(s) with an unrecognised prefix carry no "
                "local time (set fix.local_ts_regex)"
            )
    store_formats: dict[str, int] = {}
    for j, sf in enumerate(stores):
        hdr = sf.path[: -len(".body")] + ".header" if sf.path.endswith(".body") else None
        rd = read_store(sf.path, hdr)
        store_formats[rd.header_format] = store_formats.get(rd.header_format, 0) + 1
        if rd.index_mismatches:
            file_notes.append(f"{sf.path}: {rd.index_mismatches} index record(s) do not point at a message")
        for seq, fr, off in rd.messages:
            accept(fr.text, fr.problem, None, None, f"{sf.path}@{off}", len(logs) + j, off, "store", sf.hint, seq)
    for s in sessions.values():
        s.close()

    # sessions: local side, venue, order
    for s in sessions.values():
        _decide_local(s)
        remote = next((c for c in s.comps if c != s.local), None)
        stem = s.hint.stem if s.hint is not None else None
        s.venue = (
            (cfg.session_venues.get(stem) if stem else None)
            or (cfg.session_venues.get(remote) if remote else None)
            or cfg.venue_name
            or VENUE
        )
    unused = set(cfg.session_venues) - {
        k for s in sessions.values() for k in ((s.hint.stem if s.hint else None), *s.comps) if k
    }
    if unused:
        raise QtError("QT-CONFIG-001", f"fix.session_venues: no session matches {sorted(unused)}")
    ordered_sessions = sorted(
        sessions.values(), key=lambda s: (not any(m.out for m in s.msgs if m.mt in ("D", "F", "G")), s.order)
    )

    # pre-pass: cancel requests per venue
    cancel_cids: dict[str, set[str]] = {}
    cancel_oids: dict[str, set[str]] = {}
    mass: dict[str, list[int]] = {}
    for s in ordered_sessions:
        for m in [*s.msgs, *s.store_msgs]:
            if not m.out:
                continue
            if m.mt == "F":
                for t in (tags.OrigClOrdID, tags.ClOrdID):
                    if m.f.get(t):
                        cancel_cids.setdefault(s.venue, set()).add(m.f[t])
                if m.f.get(tags.OrderID):
                    cancel_oids.setdefault(s.venue, set()).add(m.f[tags.OrderID])
            elif m.mt in ("q", "CA"):
                t = _time_key(m)
                if t is not None:
                    mass.setdefault(s.venue, []).append(t)

    builders: dict[str, tuple[LogBuilder, _Fix]] = {}
    for s in ordered_sessions:
        if s.venue not in builders:
            b = LogBuilder(s.venue, account=opts.account, imap=opts.imap, price_exp=-9, size_exp=-8, always_chain=True)
            builders[s.venue] = (
                b,
                _Fix(
                    b,
                    cfg,
                    tags,
                    dictionary,
                    cancel_cids.get(s.venue, set()),
                    cancel_oids.get(s.venue, set()),
                    mass.get(s.venue, []),
                ),
            )
    if not builders:
        builders[cfg.venue_name or VENUE] = (
            b := LogBuilder(
                cfg.venue_name or VENUE,
                account=opts.account,
                imap=opts.imap,
                price_exp=-9,
                size_exp=-8,
                always_chain=True,
            ),
            _Fix(b, cfg, tags, dictionary, set(), set(), []),
        )

    for stream_idx, s in enumerate(ordered_sessions):
        b, fx = builders[s.venue]
        msgs = _place_store(_reorder(s.msgs), s.store_msgs)
        scope = s.sid
        skey = s.key
        for reason, n in s.skipped.items():
            b.skip(reason, n)
        engine_rejected = 0
        acceptor = 0
        for n_line, m in enumerate(msgs):
            rec = Record(m.f, display(m.text), m.location, stream_idx, n_line)
            mt = m.mt
            if mt in ("D", "F", "G"):
                if not m.out:
                    acceptor += 1
                    b.skip(f"35={mt} received (acceptor-side log: the importer reads the order sender's log)")
                    continue
                fx.outbound(m, rec, scope)
            elif mt in ("q", "CA"):
                b.skip(f"35={mt} mass cancel request (the venue's per-order cancels arrive as ExecutionReports)")
            elif mt in ("8", "9", "j"):
                if m.out:
                    acceptor += 1
                    b.skip(f"35={mt} sent (acceptor-side log: the importer reads the order sender's log)")
                    continue
                if mt == "8":
                    fx.exec_report(m, rec, scope, skey)
                elif mt == "9":
                    fx.cancel_reject(m, rec, scope)
                else:
                    ref = None
                    ref_seq = m.f.get(tags.RefSeqNum, "")
                    if ref_seq.isdigit():
                        ref = s.by_seq.get((m.target, m.epoch, int(ref_seq)))
                    fx.request_reject(m, rec, scope, ref, m.f.get(tags.RefMsgType))
            elif mt == "3":
                ref_seq = m.f.get(tags.RefSeqNum, "")
                ref = None
                if ref_seq.isdigit():
                    ref = s.by_seq.get((m.target, m.epoch, int(ref_seq)))
                if m.out:
                    if ref is not None and ref.mt == "8":
                        engine_rejected += 1
                    b.skip("35=3 session reject sent by the local engine")
                elif ref is not None and ref.mt in ("D", "F", "G"):
                    fx.request_reject(m, rec, scope, ref, ref.mt)
                else:
                    b.skip("35=3 session reject (not of an order request)")
            elif mt in ("r", "BZ"):
                b.skip(f"35={mt} mass cancel / action report (per-order cancels arrive as ExecutionReports)")
            else:
                name = dictionary.message_name(mt) if dictionary is not None else None
                b.skip(f"message type 35={mt or 'missing'}" + (f" ({name})" if name else ""))
        notes = dict(s.counters)
        notes["app_messages"] = len(s.msgs) + len(s.store_msgs)
        if acceptor:
            notes["acceptor_side_messages"] = acceptor
        if engine_rejected:
            notes["inbound_reports_rejected_by_local_engine"] = engine_rejected
        label = {}
        for k, v in notes.items():
            k2 = k
            if s.local:
                k2 = k.replace(f"from:{s.local}", "out").replace(f":{s.local}=", ":out=")
                remote = next((c for c in s.comps if c != s.local), None)
                if remote:
                    k2 = k2.replace(f"from:{remote}", "in").replace(f":{remote}=", ":in=")
            label[k2] = v
        b.warn(f"{b.venue}: FIX session {s.sid}: {_fmt_counts(label)}")
        unrec_in = label.get("unrecovered_in", 0)
        if unrec_in:
            b.warn(
                f"{b.venue}: FIX session {s.sid}: {unrec_in} inbound message(s) lost in sequence gaps and never "
                "resent: reports inside them are missing (CumQty jumps become derived fills)"
            )
        if s.appl_ver:
            b.warn(f"{b.venue}: FIX session {s.sid}: DefaultApplVerID {s.appl_ver}")

    # file-level notes go to the first builder
    first_b, _ = next(iter(builders.values()))
    for reason, n in extra_skips.items():
        first_b.skip(reason, n)
    parts = []
    if logs:
        parts.append(f"{len(logs)} message log(s) ({_fmt_counts(formats)})")
    if stores:
        parts.append(f"{len(stores)} FileStore(s) (header {_fmt_counts(store_formats)})")
    if events:
        parts.append(f"{len(events)} event log(s)")
    if parts:
        first_b.warn(f"{first_b.venue}: inputs: " + ", ".join(parts))
    if problems:
        first_b.warn(
            f"{first_b.venue}: excluded {sum(problems.values())} message(s) failing FIX validation "
            f"({_fmt_counts(problems)}); QuickFIX discards such messages"
            + ("; fix.checksum=keep imports CheckSum/BodyLength failures" if cfg.checksum == "exclude" else "")
        )
    if kept_bad:
        first_b.warn(
            f"{first_b.venue}: imported {sum(kept_bad.values())} message(s) failing CheckSum(10)/BodyLength(9) "
            f"validation (fix.checksum=keep: {_fmt_counts(kept_bad)})"
        )
    if dup_lines:
        first_b.dropped_duplicate(dup_lines)
        first_b.warn(f"{first_b.venue}: {dup_lines} byte-identical duplicate message line(s) dropped")
    if store_in_log:
        first_b.warn(f"{first_b.venue}: {store_in_log} FileStore message(s) already in the message logs")
    for note in file_notes:
        first_b.warn(f"{first_b.venue}: {note}")
    for sf in events:
        counts, n_lines = read_event_log(sf.path)
        stem = sf.hint.stem if sf.hint else sf.path
        first_b.warn(f"{first_b.venue}: QuickFIX event log {stem}: lines={n_lines} {_fmt_counts(counts)}")
    _seqnum_notes(seqfiles, sessions, first_b)
    if dictionary is not None:
        n_fields = len(dictionary.names)
        first_b.warn(f"{first_b.venue}: data dictionary {', '.join(dictionary.versions)} ({n_fields} fields)")
        if unknown_tags:
            top = sorted(unknown_tags.items(), key=lambda kv: (-kv[1], int(kv[0])))[:20]
            first_b.warn(
                f"{first_b.venue}: {len(unknown_tags)} tag(s) not in the data dictionary (kept in raw): "
                + ", ".join(f"{t}x{n}" for t, n in top)
            )
    results = []
    for b, fx in builders.values():
        n_day = b.default_field("tif", "day")
        if n_day:
            fx.count("tif_default_day", n_day)
        if fx.counters:
            b.warn(f"{b.venue}: FIX import counters: {_fmt_counts(fx.counters)}")
        results.append(b.finish())
    return _merge(results)


def _seqnum_notes(seqfiles: list[Any], sessions: dict[tuple[Any, ...], _Session], b: LogBuilder) -> None:
    by_stem: dict[str, list[_Session]] = {}
    for s in sessions.values():
        if s.hint is not None:
            by_stem.setdefault(s.hint.stem, []).append(s)
    for sf in seqfiles:
        nxt_sender, nxt_target = read_seqnums(sf.path, sf.kind)
        stem = sf.hint.stem if sf.hint else None
        ss = by_stem.get(stem or "", [])
        if not ss:
            b.warn(f"{b.venue}: FileStore sequence numbers {sf.path}: no message log of this session was given")
            continue
        s = ss[0]
        for nxt, who, label in ((nxt_sender, s.local, "outbound"), (nxt_target, None, "inbound")):
            if nxt is None:
                continue
            if who is None:
                who = next((c for c in s.comps if c != s.local), None)
            st = s.streams.get(who or "")
            last = st.max_seq if st is not None else None
            if last is not None and nxt - 1 > last:
                b.warn(
                    f"{b.venue}: FileStore {stem}: next {label} MsgSeqNum {nxt} but the inputs end at {last}: "
                    f"{nxt - 1 - last} {label} message(s) after the end of the logs"
                )


def _merge(results: list[ImportResult]) -> ImportResult:
    if len(results) == 1:
        return results[0]
    skipped: dict[str, int] = {}
    for r in results:
        for k, v in r.skipped.items():
            skipped[k] = skipped.get(k, 0) + v
    return ImportResult(
        venue=",".join(sorted(r.venue for r in results)),
        orders=[o for r in results for o in r.orders],
        events=[e for r in results for e in r.events],
        n_fills=sum(r.n_fills for r in results),
        dedupe_dropped=sum(r.dedupe_dropped for r in results),
        unmatched_fills=sum(r.unmatched_fills for r in results),
        warnings=[w for r in results for w in r.warnings],
        skipped=skipped,
        instrument_map_sha256=results[0].instrument_map_sha256,
        fills_only_orders=sum(r.fills_only_orders for r in results),
    )


__all__ = ["FIX_KEEP_PRIORITY_VENUES", "VENUE", "FixConfig", "fix_ts", "import_fix44"]
