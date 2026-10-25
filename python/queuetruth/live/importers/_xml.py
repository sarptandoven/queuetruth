"""Hardened XML parsing for untrusted importer inputs (QuickFIX data dictionaries, IBKR Flex statements).

Neither format uses a document type declaration, so any `<!DOCTYPE` (and with it every entity declaration: the
"billion laughs" expansion, external entities) is refused. The check runs on expat's own view of the document, so
it cannot be bypassed by an encoding (a UTF-16 or UTF-32 file hides `<!DOCTYPE` from a byte search). Every parser
failure (malformed XML, an unknown declared encoding, nesting too deep to serialise) is `QT-INPUT-002`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from xml.parsers import expat

from queuetruth.errors import QtError


class _Refused(Exception):
    pass


def _refuse(*_: Any) -> None:
    raise _Refused


def parse_xml(data: bytes, where: str, what: str, *, encoding: str | None = None) -> ET.Element:
    """Root element of the XML document `data` (`what` names the expected format in error messages). `encoding`
    overrides the document's own declaration (pass "utf-8" for bytes re-encoded from text already decoded, so a
    stale `encoding="ISO-8859-1"` declaration cannot turn `É` into `Ã‰`)."""
    scan = expat.ParserCreate(encoding)
    scan.StartDoctypeDeclHandler = _refuse
    scan.EntityDeclHandler = _refuse
    scan.ExternalEntityRefHandler = _refuse
    try:
        scan.Parse(data, True)
    except _Refused:
        raise QtError("QT-INPUT-002", f"{where}: DTD / entity declarations are not accepted in {what}") from None
    except (expat.ExpatError, LookupError, ValueError) as e:
        raise QtError("QT-INPUT-002", f"{where}: not {what}: {e}") from None
    try:
        parser = ET.XMLParser(encoding=encoding)
        parser.feed(data)
        return parser.close()
    except (ET.ParseError, LookupError, ValueError) as e:
        raise QtError("QT-INPUT-002", f"{where}: not {what}: {e}") from None


def to_text(el: ET.Element, where: str) -> str:
    """Serialised form of an element (its raw record text); `QT-INPUT-002` when it nests too deeply to write."""
    try:
        return ET.tostring(el, encoding="unicode").strip()
    except RecursionError:
        raise QtError("QT-INPUT-002", f"{where}: element nested too deeply") from None


__all__ = ["parse_xml", "to_text"]
