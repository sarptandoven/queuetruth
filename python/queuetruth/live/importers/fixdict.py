"""QuickFIX data dictionaries (the `DataDictionary` / `TransportDataDictionary` / `AppDataDictionary` XML files of
QuickFIX C++, QuickFIX/J, QuickFIX/n and QuickFIX/Go, e.g. `FIX44.xml`, `FIXT11.xml` + `FIX50SP2.xml`, or a
venue's customised copy).

Format (shared by every QuickFIX engine): a `<fix type="FIX|FIXT" major minor servicepack>` root with
`<fields><field number name type><value enum description/></field></fields>` and
`<messages><message name msgtype msgcat>`. The importer uses a dictionary to:

- resolve the tag numbers of the concepts it reads by their standard names (a venue dictionary that declares
  `LastLiquidityInd` under a custom number is honoured; the standard number stays the fallback);
- map venue-specific enum values by their descriptions: custom `ExecType(150)` values (`TRADE`, `CANCELED`, ...)
  and custom liquidity fields (a field whose name mentions liquidity / aggressor / maker-taker, with enum
  descriptions such as `ADDED` / `REMOVED`);
- name message types and count the tags of imported messages that the dictionary does not define (they are kept
  verbatim in the canonical `raw` columns).

Entity declarations are refused (no DTD processing of untrusted XML).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from queuetruth.errors import QtError

#: Standard tag numbers of every concept the FIX importer reads (FIX 4.2 - 5.0SP2 names).
STANDARD_TAGS: dict[str, str] = {
    "Account": "1",
    "AvgPx": "6",
    "BeginSeqNo": "7",
    "BeginString": "8",
    "BodyLength": "9",
    "CheckSum": "10",
    "ClOrdID": "11",
    "Commission": "12",
    "CommType": "13",
    "CumQty": "14",
    "Currency": "15",
    "EndSeqNo": "16",
    "ExecID": "17",
    "ExecInst": "18",
    "ExecRefID": "19",
    "ExecTransType": "20",
    "LastPx": "31",
    "LastQty": "32",
    "MsgSeqNum": "34",
    "MsgType": "35",
    "NewSeqNo": "36",
    "OrderID": "37",
    "OrderQty": "38",
    "OrdStatus": "39",
    "OrdType": "40",
    "OrigClOrdID": "41",
    "PossDupFlag": "43",
    "Price": "44",
    "RefSeqNum": "45",
    "SecurityID": "48",
    "SenderCompID": "49",
    "SendingTime": "52",
    "Side": "54",
    "Symbol": "55",
    "TargetCompID": "56",
    "Text": "58",
    "TimeInForce": "59",
    "TransactTime": "60",
    "PossResend": "97",
    "CxlRejReason": "102",
    "OrdRejReason": "103",
    "SecurityDesc": "107",
    "OrigSendingTime": "122",
    "GapFillFlag": "123",
    "ResetSeqNumFlag": "141",
    "ExecType": "150",
    "LeavesQty": "151",
    "SessionRejectReason": "373",
    "RefMsgType": "372",
    "ExecRestatementReason": "378",
    "BusinessRejectRefID": "379",
    "BusinessRejectReason": "380",
    "CxlRejResponseTo": "434",
    "CommCurrency": "479",
    "LastLiquidityInd": "851",
    "TrdMatchID": "880",
    "AggressorIndicator": "1057",
    "ApplVerID": "1128",
    "DefaultApplVerID": "1137",
}

_LIQ_NAME = re.compile(r"liquidit|aggressor|maker.?taker|add.?remove", re.IGNORECASE)
_MAKER_WORDS = ("ADDED", "ADD", "MAKER", "PASSIVE", "PROVIDE", "PROVIDED", "PROVIDER", "RESTING", "POSTED")
_TAKER_WORDS = ("REMOVED", "REMOVE", "TAKER", "AGGRESSIVE", "AGGRESSOR", "TAKE", "TAKEN", "TAKING")

#: ExecType kinds by enum description (custom venue values; first matching rule wins).
_EXEC_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("TRADE_CANCEL", "TRADE_BUST", "BUST", "BUSTED"), "H"),
    (("TRADE_CORRECT", "CORRECT", "CORRECTED", "CORRECTION"), "G"),
    (("PENDING",), "A"),
    (("PARTIAL_FILL", "PARTIALLY_FILLED", "FILL", "FILLED", "TRADE", "EXECUTION", "EXECUTED"), "F"),
    (("DONE_FOR_DAY",), "3"),
    (("CANCELED", "CANCELLED", "CANCEL"), "4"),
    (("REPLACED", "REPLACE", "MODIFIED", "AMENDED"), "5"),
    (("REJECTED", "REJECT"), "8"),
    (("EXPIRED", "EXPIRE"), "C"),
    (("RESTATED", "RESTATEMENT"), "D"),
    (("ORDER_STATUS", "STATUS"), "I"),
    (("NEW", "ACCEPTED", "ACKNOWLEDGED"), "0"),
)


def _words(desc: str) -> list[str]:
    return [w for w in re.split(r"[^A-Z0-9]+", desc.upper()) if w]


def _joined(desc: str) -> str:
    return "_".join(_words(desc))


@dataclass
class FixDictionary:
    """Merged content of one or more QuickFIX data-dictionary files."""

    paths: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)
    numbers: dict[str, str] = field(default_factory=dict)
    enums: dict[str, dict[str, str]] = field(default_factory=dict)
    msgtypes: dict[str, str] = field(default_factory=dict)

    def tag(self, name: str) -> str:
        """Tag number of a concept: the dictionary's number for that name, else the standard one."""
        return self.numbers.get(name) or STANDARD_TAGS[name]

    def exec_type(self, value: str) -> str | None:
        """Standard ExecType code for a non-standard `ExecType` enum value, by its description."""
        desc = self.enums.get(self.tag("ExecType"), {}).get(value)
        if not desc:
            return None
        joined = _joined(desc)
        words = set(_words(desc))
        for keys, code in _EXEC_RULES:
            for k in keys:
                if ("_" in k and k in joined) or k in words or joined == k:
                    return code
        return None

    def liquidity_fields(self) -> dict[str, dict[str, str]]:
        """Custom liquidity fields: tag -> {enum value: maker|taker} (standard 851 / 1057 excluded)."""
        out: dict[str, dict[str, str]] = {}
        for tag, name in self.names.items():
            if tag in ("851", "1057") or not _LIQ_NAME.search(name):
                continue
            aggressor = "aggressor" in name.lower()
            vals: dict[str, str] = {}
            for v, desc in self.enums.get(tag, {}).items():
                words = set(_words(desc))
                if aggressor and v in ("Y", "N"):
                    vals[v] = "taker" if v == "Y" else "maker"
                elif words & set(_TAKER_WORDS):
                    vals[v] = "taker"
                elif words & set(_MAKER_WORDS):
                    vals[v] = "maker"
            if vals:
                out[tag] = vals
        return out

    def message_name(self, msgtype: str) -> str | None:
        """Name of a message type (e.g. `8` -> `ExecutionReport`)."""
        return self.msgtypes.get(msgtype)


def _attr(el: ET.Element, name: str) -> str:
    return (el.get(name) or "").strip()


def load_dictionary(paths: list[str] | str | None) -> FixDictionary | None:
    """Loads and merges QuickFIX data-dictionary XML files (later files override earlier ones per tag). None
    for no paths. Unreadable files fail `QT-INPUT-001`, malformed XML or entity declarations `QT-INPUT-002`."""
    if not paths:
        return None
    plist = [paths] if isinstance(paths, str) else list(paths)
    d = FixDictionary()
    for p in plist:
        path = Path(p)
        try:
            data = path.read_bytes()
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{p}: {e}") from e
        if b"<!ENTITY" in data or b"<!DOCTYPE" in data:
            raise QtError("QT-INPUT-002", f"{p}: DTD / entity declarations are not accepted in a data dictionary")
        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise QtError("QT-INPUT-002", f"{p}: not a QuickFIX data dictionary: {e}") from e
        if root.tag != "fix":
            raise QtError("QT-INPUT-002", f"{p}: root element is <{root.tag}>, expected <fix>")
        kind = _attr(root, "type") or "FIX"
        ver = f"{kind}.{_attr(root, 'major')}.{_attr(root, 'minor')}"
        sp = _attr(root, "servicepack")
        if sp and sp != "0":
            ver += f"SP{sp}"
        d.paths.append(str(p))
        d.versions.append(ver)
        fields_el = root.find("fields")
        for f in fields_el.findall("field") if fields_el is not None else []:
            num, name = _attr(f, "number"), _attr(f, "name")
            if not num.isdigit() or not name:
                raise QtError("QT-INPUT-002", f"{p}: <field> without a numeric number and a name")
            d.names[num] = name
            d.numbers[name] = num
            values = {_attr(v, "enum"): _attr(v, "description") for v in f.findall("value") if _attr(v, "enum")}
            if values:
                d.enums[num] = values
        msgs_el = root.find("messages")
        for m in msgs_el.findall("message") if msgs_el is not None else []:
            if _attr(m, "msgtype"):
                d.msgtypes[_attr(m, "msgtype")] = _attr(m, "name")
    return d
