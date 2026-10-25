"""Fixed-point units of the prediction-market kit and exact decimal parsing.

Every price is a YES-leg price in dollars at `PRICE_EXP = -4` (Kalshi prices have at most 4 decimals, Polymarket
ticks go down to 0.0001), so `$1.00 = ONE = 10_000` units and a NO-leg price `q` is the YES price `ONE - q`.
Sizes are contracts (Kalshi `*_fp` counts have 2 decimals: `KALSHI_SIZE_EXP = -2`) or outcome shares (Polymarket
amounts have 6 decimals on chain and in `last_trade_price`: `POLY_SIZE_EXP = -6`). Venue values are decimal strings
and convert exactly or fail with `QT-INPUT-004`; JSON numbers are accepted only when they are exact (integers, or
values parsed as `Decimal`).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from queuetruth.errors import QtError

#: Price exponent of every prediction-market dataset and live log (YES-leg dollars, 4 decimals).
PRICE_EXP = -4
#: $1.00 in price units.
ONE = 10**4
#: Kalshi contract counts (`count_fp`, 2 decimals).
KALSHI_SIZE_EXP = -2
#: Polymarket share amounts (6 decimals, as on chain).
POLY_SIZE_EXP = -6
#: Exponent of on-chain collateral and outcome-token amounts (pUSD / USDC and CTF positions: 6 decimals).
CHAIN_EXP = -6


def dec(value: Any, what: str, where: str = "") -> Decimal | None:
    """`Decimal` of a venue value (string, int or `Decimal`); None for null / empty. Binary floats are refused
    (`QT-INPUT-002`): they cannot carry a venue's exact decimal text."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise QtError("QT-INPUT-002", f"{where}: {what}: expected a number, got {value!r}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise QtError("QT-INPUT-002", f"{where}: {what}: {value!r} is not a decimal number") from None
    else:
        raise QtError("QT-INPUT-002", f"{where}: {what}: expected a decimal string, got {type(value).__name__}")
    if not d.is_finite():
        raise QtError("QT-INPUT-002", f"{where}: {what}: {value!r} is not finite")
    return d


def req_dec(value: Any, what: str, where: str = "") -> Decimal:
    """`dec` that refuses a missing value (`QT-INPUT-002`)."""
    d = dec(value, what, where)
    if d is None:
        raise QtError("QT-INPUT-002", f"{where}: {what} is missing")
    return d


def fixed(d: Decimal, exp: int, what: str, where: str = "", *, unsigned: bool = False) -> int:
    """`d * 10**-exp` as an exact integer, else `QT-INPUT-004`; negative values fail when `unsigned`."""
    scaled = d.scaleb(-exp)
    if scaled != scaled.to_integral_value():
        raise QtError("QT-INPUT-004", f"{where}: {what} {d} is not representable at exponent {exp}")
    v = int(scaled)
    if unsigned and v < 0:
        raise QtError("QT-INPUT-002", f"{where}: {what} {d} is negative")
    return v


def price_units(value: Any, what: str, where: str = "") -> int:
    """A dollar price string in `[0, 1]` -> price units (`PRICE_EXP`)."""
    v = fixed(req_dec(value, what, where), PRICE_EXP, what, where)
    if not 0 <= v <= ONE:
        raise QtError("QT-INPUT-002", f"{where}: {what} {value!r} outside [0, 1]")
    return v


def size_units(value: Any, exp: int, what: str, where: str = "", *, signed: bool = False) -> int:
    """A size string -> size units at `exp` (non-negative unless `signed`)."""
    return fixed(req_dec(value, what, where), exp, what, where, unsigned=not signed)


def to_decimal(units: int, exp: int) -> Decimal:
    """Fixed-point units -> `Decimal` (exact)."""
    return Decimal(int(units)).scaleb(exp)


def text(units: int, exp: int) -> str:
    """Fixed-point units -> plain decimal text (`5600, -4` -> `0.5600`)."""
    return format(to_decimal(units, exp), "f")


def complement(price: int) -> int:
    """YES price of a NO-leg price (and back): `ONE - price`."""
    return ONE - int(price)


def ratio_price(collateral: int, tokens: int) -> Fraction:
    """Exact price `collateral / tokens` of an on-chain fill (both amounts at `CHAIN_EXP`)."""
    if tokens <= 0:
        raise QtError("QT-INPUT-002", f"fill with {tokens} outcome tokens has no price")
    return Fraction(int(collateral), int(tokens))


def price_from_fraction(p: Fraction, what: str, where: str = "") -> tuple[int, bool]:
    """(price units, exact) of a rational price: units rounded half-up to `PRICE_EXP`; `exact` is false when the
    rational price is not on the 0.0001 grid (rounding happened)."""
    scaled = p * ONE
    n, d = scaled.numerator, scaled.denominator
    if d == 1:
        units = n
        exact = True
    else:
        units = (2 * n + d) // (2 * d)
        exact = False
    if not 0 <= units <= ONE:
        raise QtError("QT-INPUT-002", f"{where}: {what} {float(p)} outside [0, 1]")
    return int(units), exact


def ms_to_ns(value: Any, what: str, where: str = "") -> int:
    """Integer milliseconds since the epoch (int or digit string) -> ns."""
    d = req_dec(value, what, where)
    if d != d.to_integral_value() or d < 0:
        raise QtError("QT-INPUT-002", f"{where}: {what} {value!r} is not a non-negative integer ms timestamp")
    return int(d) * 1_000_000


def s_to_ns(value: Any, what: str, where: str = "") -> int:
    """Integer seconds since the epoch -> ns."""
    d = req_dec(value, what, where)
    if d != d.to_integral_value() or d < 0:
        raise QtError("QT-INPUT-002", f"{where}: {what} {value!r} is not a non-negative integer s timestamp")
    return int(d) * 1_000_000_000


__all__ = [
    "CHAIN_EXP",
    "KALSHI_SIZE_EXP",
    "ONE",
    "POLY_SIZE_EXP",
    "PRICE_EXP",
    "complement",
    "dec",
    "fixed",
    "ms_to_ns",
    "price_from_fraction",
    "price_units",
    "ratio_price",
    "req_dec",
    "s_to_ns",
    "size_units",
    "text",
    "to_decimal",
]
