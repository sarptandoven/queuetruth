"""`queuetruth.units`: the one duration grammar, horizon keys, prices, local times and sides."""

from __future__ import annotations

import json
import warnings
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from queuetruth import units as u

I64 = 2**63 - 1


@pytest.mark.parametrize(
    ("text", "ns"),
    [
        ("1s", 1_000_000_000),
        ("250ms", 250_000_000),
        ("1min", 60_000_000_000),
        ("1m", 60_000_000_000),
        ("60 s", 60_000_000_000),
        ("100us", 100_000),
        ("100µs", 100_000),
        ("7ns", 7),
        ("1.5s", 1_500_000_000),
        ("2h", 7_200_000_000_000),
        ("1d", 86_400_000_000_000),
        (" 2S ", 2_000_000_000),
        ("0", 0),
        ("1000000000", 1_000_000_000),
        (1_000_000_000, 1_000_000_000),
        (0, 0),
    ],
)
def test_parse_duration(text: object, ns: int) -> None:
    assert u.parse_duration(text) == ns


@pytest.mark.parametrize(
    "bad",
    ["", "s", "-1s", "1.5", "1e9", "1 xs", "1.0000000001ns", "99999999999999999999s", "1s,", True, 1.5, None, -1],
)
def test_parse_duration_rejects(bad: object) -> None:
    with pytest.raises(u.DurationError):
        u.parse_duration(bad)


def test_bare_ns_off_refuses_unitless() -> None:
    assert u.parse_duration("0", bare_ns=False) == 0
    with pytest.raises(u.DurationError, match="no unit"):
        u.parse_duration("5", bare_ns=False)


def test_every_rust_duration_means_the_same() -> None:
    """The Rust CLI grammar (`crates/schema/src/duration.rs`) is a subset: its unit tests' cases agree."""
    cases = {
        "0": 0,
        "1500": 1500,
        "7ns": 7,
        "250us": 250_000,
        "100ms": 100_000_000,
        "1s": 1_000_000_000,
        "10s": 10_000_000_000,
        "1m": 60_000_000_000,
        "5m": 300_000_000_000,
        " 2s ": 2_000_000_000,
    }
    for text, ns in cases.items():
        assert u.parse_duration(text) == ns, text


@pytest.mark.parametrize(
    ("ns", "text"),
    [
        (0, "0s"),
        (1, "1ns"),
        (1_500, "1.5us"),
        (100_000, "100us"),
        (250_000_000, "250ms"),
        (1_000_000_000, "1s"),
        (1_500_000_000, "1.5s"),
        (10_000_000_000, "10s"),
        (60_000_000_000, "1min"),
        (90_000_000_000, "90s"),
        (300_000_000_000, "5min"),
        (3_600_000_000_000, "1h"),
        (1_000_000_001, "1000000001ns"),
        (-1_500_000_000, "-1.5s"),
    ],
)
def test_format_duration(ns: int, text: str) -> None:
    assert u.format_duration(ns) == text


def test_format_duration_rounded() -> None:
    assert u.format_duration(52_234_000_000, sig=3) == "52.2s"
    assert u.format_duration(3_200_000.0, sig=3) == "3.2ms"
    assert u.format_duration(5_400_000_000_000, sig=2, signed=True) == "+1.5h"
    assert u.format_duration(-30_000_000_000, sig=2, signed=True) == "-30s"
    assert u.format_duration(None) == "-"
    assert u.format_duration(float("nan"), sig=3) == "n/a"


@settings(max_examples=2000)
@given(st.integers(min_value=0, max_value=I64))
def test_parse_of_format_is_identity(ns: int) -> None:
    text = u.format_duration(ns)
    assert u.parse_duration(text) == ns, text


@settings(max_examples=500)
@given(st.integers(min_value=0, max_value=10_000), st.sampled_from([1, 10**3, 10**6, 10**9, 60 * 10**9]))
def test_round_numbers_round_trip(k: int, scale: int) -> None:
    ns = k * scale
    assert u.parse_duration(u.format_duration(ns)) == ns
    assert u.parse_duration(u.horizon_label(ns)) == ns
    assert u.horizon_key(u.horizon_label(ns)) == str(ns)


def test_horizon_key_and_label() -> None:
    assert u.horizon_key("1s") == u.horizon_key(1_000_000_000) == u.horizon_key("1000000000") == "1000000000"
    assert u.horizon_label("60000000000") == "1min"
    assert u.horizon_words("1min") == "1 minute"
    assert u.horizon_words(10_000_000_000) == "10 seconds"
    assert u.horizon_words("1.5s") == "1500 milliseconds"
    assert u.horizon_words(1_500) == "1500 nanoseconds"


def test_horizon_dict() -> None:
    d = u.HorizonDict({"1000000000": 0.5, "60000000000": None})
    assert d["1s"] == d[1_000_000_000] == d["1000000000"] == 0.5
    assert "1min" in d and "1m" in d and "2s" not in d
    assert d.get("5s", "x") == "x"
    assert d.labels() == {"1s": 0.5, "1min": None}
    assert json.dumps(d) == '{"1000000000": 0.5, "60000000000": null}'
    assert d == {"1000000000": 0.5, "60000000000": None}
    with pytest.raises(KeyError):
        d["bogus"]


def test_prices() -> None:
    assert u.price_from_ticks(1_501_200, -4) == Decimal("150.12")
    assert u.format_price(1_501_000, -4, tick_size=100) == "150.10"
    assert u.format_price(1_501_200, -4) == "150.12"
    assert u.format_price(1_501_234, -4, tick_size=100) == "150.1234"  # never rounded
    assert u.format_price(42, None) == "42 (price units, scale unknown)"
    assert u.format_price(None, -4) == "-"


def test_local_time() -> None:
    ts = 1_700_000_000_123_456_789  # 2023-11-14 22:13:20 UTC
    assert u.local_time(ts, "America/New_York") == "2023-11-14 17:13:20.123456 America/New_York"
    assert u.local_time(ts, "UTC", digits=3) == "2023-11-14 22:13:20.123 UTC"
    assert u.local_time(ts, None, date=False, digits=0) == "22:13:20 UTC"
    assert u.local_time(ts, "Not/AZone").endswith(" UTC")
    assert u.local_time(None, "UTC") == "-"
    assert u.local_datetime(ts, "America/New_York").utcoffset().total_seconds() == -5 * 3600  # type: ignore[union-attr]


def test_side_word() -> None:
    assert [u.side_word(x) for x in ("B", "A", "bid", "ask", 1, -1, "SELL", "buy")] == [
        "buy",
        "sell",
        "buy",
        "sell",
        "buy",
        "sell",
        "sell",
        "buy",
    ]
    assert u.side_word("N") == "N"
    assert u.side_word(None) == "-"


def test_old_names_still_work_with_a_warning() -> None:
    from queuetruth.api import fmt_dur, fmt_scaled, fmt_utc
    from queuetruth.eval.leaderboard import duration_text
    from queuetruth.forecast.cli import parse_duration as forecast_parse
    from queuetruth.sandbox._util import parse_duration as sandbox_parse
    from queuetruth.score.pnl_gap import duration_text as gap_text
    from queuetruth.score.statistics import duration_text as stats_text

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert fmt_dur(250_000_000) == "250ms"
        assert duration_text(60_000_000_000) == "1min"
        assert gap_text(1_000_000_000) == stats_text(1_000_000_000) == "1s"
        assert forecast_parse("5m", "--min-lead") == 300_000_000_000
        assert sandbox_parse("100ms", "latency_ns") == 100_000_000
        assert fmt_scaled(1500, -3) == u.format_scaled(1500, -3) == "1.5"
        assert fmt_utc(1_700_000_000_123_456_789) == "22:13:20.123456"
    assert len(caught) == 8
    assert all(issubclass(w.category, DeprecationWarning) for w in caught)


def test_schema_maps_answer_human_keys() -> None:
    from pydantic import TypeAdapter

    from queuetruth.schemas.models import CiValue, HMap  # noqa: F401
    from queuetruth.units import HorizonDict

    m = TypeAdapter(HMap).validate_python({"1000000000": 0.25})
    assert isinstance(m, HorizonDict)
    assert m["1s"] == 0.25
