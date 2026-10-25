"""Fixed-point units and exact decimal parsing (`queuetruth.pm.units`)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from queuetruth.errors import QtError
from queuetruth.pm.units import (
    ONE,
    complement,
    dec,
    fixed,
    ms_to_ns,
    price_from_fraction,
    price_units,
    ratio_price,
    req_dec,
    s_to_ns,
    size_units,
    text,
    to_decimal,
)


def code(fn, *a, **k) -> str:
    with pytest.raises(QtError) as e:
        fn(*a, **k)
    return e.value.code


def test_dec_accepts_exact_values_and_refuses_floats_bools_and_garbage() -> None:
    assert dec("0.5600", "p") == Decimal("0.5600")
    assert dec(" 12 ", "p") == Decimal(12)
    assert dec(7, "p") == Decimal(7)
    assert dec(Decimal("1.25"), "p") == Decimal("1.25")
    assert dec(None, "p") is None
    assert dec("", "p") is None
    assert code(dec, 0.56, "p") == "QT-INPUT-002"  # a binary float cannot carry the venue's decimal text
    assert code(dec, True, "p") == "QT-INPUT-002"
    assert code(dec, "abc", "p") == "QT-INPUT-002"
    assert code(dec, "NaN", "p") == "QT-INPUT-002"
    assert code(dec, "Infinity", "p") == "QT-INPUT-002"
    assert code(req_dec, None, "p") == "QT-INPUT-002"


def test_fixed_point_conversions_are_exact_or_fail() -> None:
    assert fixed(Decimal("0.5600"), -4, "p") == 5600
    assert fixed(Decimal("-1.50"), -2, "q") == -150
    assert code(fixed, Decimal("0.12345"), -4, "p") == "QT-INPUT-004"
    assert code(fixed, Decimal("-1"), -2, "q", unsigned=True) == "QT-INPUT-002"
    assert price_units("0.56", "p") == 5600
    assert price_units("1", "p") == ONE
    assert price_units("0", "p") == 0
    assert code(price_units, "1.01", "p") == "QT-INPUT-002"
    assert code(price_units, "0.00001", "p") == "QT-INPUT-004"
    assert code(price_units, None, "p") == "QT-INPUT-002"
    assert size_units("12.50", -2, "c") == 1250
    assert size_units("-3.00", -2, "d", signed=True) == -300
    assert code(size_units, "-3.00", -2, "d") == "QT-INPUT-002"
    assert size_units("0.000001", -6, "s") == 1
    assert code(size_units, "0.0000001", -6, "s") == "QT-INPUT-004"


def test_text_complement_and_decimal_roundtrip() -> None:
    assert text(5600, -4) == "0.5600"
    assert text(1250, -2) == "12.50"
    assert to_decimal(15, -6) == Decimal("0.000015")
    assert complement(4400) == 5600
    assert complement(complement(1234)) == 1234


def test_chain_prices_round_half_up_and_flag_inexact() -> None:
    assert ratio_price(1_840_000, 4_000_000) == Fraction(46, 100)
    assert price_from_fraction(Fraction(46, 100), "p") == (4600, True)
    # 17.35 collateral for 40 tokens: 0.43375 -> 0.4338 (half up), inexact
    assert price_from_fraction(ratio_price(17_350_000, 40_000_000), "p") == (4338, False)
    assert price_from_fraction(Fraction(1, 3), "p") == (3333, False)
    assert price_from_fraction(Fraction(2, 3), "p") == (6667, False)
    assert price_from_fraction(Fraction(1, 20000), "p") == (1, False)  # 0.00005 rounds up
    assert code(ratio_price, 5, 0) == "QT-INPUT-002"
    assert code(price_from_fraction, Fraction(3, 2), "p") == "QT-INPUT-002"


def test_epoch_timestamps() -> None:
    assert ms_to_ns("1792764000000", "t") == 1_792_764_000_000_000_000
    assert ms_to_ns(1, "t") == 1_000_000
    assert s_to_ns("1792764000", "t") == 1_792_764_000_000_000_000
    assert code(ms_to_ns, "1.5", "t") == "QT-INPUT-002"
    assert code(ms_to_ns, "-1", "t") == "QT-INPUT-002"
    assert code(s_to_ns, None, "t") == "QT-INPUT-002"
