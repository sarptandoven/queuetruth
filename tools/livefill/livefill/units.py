"""Exact decimal text -> fixed-point integer conversion (the rule of `queuetruth ingest`: `value * 10^-exp` must be an
integer, nothing is rounded; anything else is `QT-INPUT-004`)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from queuetruth.errors import QtError

#: Bitstamp ingest defaults (`queuetruth ingest --format bitstamp`): prices at 1e-9, sizes at 1e-8.
PRICE_EXP = -9
SIZE_EXP = -8


def to_fixed(text: str | int | float, exp: int, what: str = "value") -> int:
    """`text * 10^-exp` as an exact integer; a JSON float is converted through its shortest repr text."""
    if isinstance(text, bool):
        raise QtError("QT-INPUT-002", f"{what}: expected a decimal, got {text!r}")
    s = repr(text) if isinstance(text, float) else str(text)
    try:
        d = Decimal(s.strip())
    except InvalidOperation as e:
        raise QtError("QT-INPUT-002", f"{what}: not a decimal: {s!r}") from e
    if not d.is_finite():
        raise QtError("QT-INPUT-002", f"{what}: not a finite decimal: {s!r}")
    scaled = d.scaleb(-exp)
    if scaled != scaled.to_integral_value():
        raise QtError("QT-INPUT-004", f"{what}: {s} is not exact at 10^{exp}")
    return int(scaled)


def from_fixed(value: int, exp: int) -> str:
    """Decimal text of a fixed-point integer (no exponent notation, no trailing zeros beyond the integer part)."""
    d = Decimal(value).scaleb(exp)
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
