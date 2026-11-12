"""Human units at every boundary (plan section 3, principle 7): durations, horizons, prices, times and sides.

Data on disk keeps its exact integer form (nanoseconds, fixed-point prices, `B` / `A` sides). This module turns
those integers into what a person reads, and reads back what a person types:

- `parse_duration("1s")` -> `1_000_000_000`; `format_duration(1_500_000_000)` -> `"1.5s"`. The one duration
  grammar of the Python package: a non-negative number, optional spaces and a unit (`ns`, `us`, `ms`, `s`, `m` or
  `min`, `h`, `d`), or a bare integer count of nanoseconds. Every duration `format_duration` writes parses back to
  the same nanoseconds.
- `horizon_key("1s")` -> `"1000000000"`, the key of a horizon in the JSON documents; `horizon_label(...)` -> `"1s"`.
  `HorizonDict` is a `{horizon: value}` map with the JSON keys that also answers `"1s"`, `1_000_000_000` and
  `"1000000000"`.
- `price_from_ticks(1_501_200, -4)` -> `Decimal("150.12")`; `format_price` for display.
- `local_time(ts_ns, "America/New_York")` -> `"2026-03-02 09:30:00.000123 America/New_York"`.
- `side_word("B")` -> `"buy"`.

When a scale or a zone is unknown the formatters print the raw value with its unit (or UTC, named) rather than
guess. Standard library only: this module is in the contracts layer, below the engine, so it cannot call
`queuetruth._core` (the Rust CLI keeps its own stricter grammar in `crates/schema/src/duration.rs`; every string
it accepts means the same here).
"""

from __future__ import annotations

import datetime as dt
import math
import numbers
import re
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "I64_MAX",
    "UNITS",
    "DurationError",
    "HorizonDict",
    "format_duration",
    "format_price",
    "format_scaled",
    "horizon_key",
    "horizon_label",
    "horizon_words",
    "humanize_note",
    "instrument_units",
    "local_datetime",
    "local_time",
    "parse_duration",
    "price_from_ticks",
    "side_word",
    "zone",
    "zone_name",
]

I64_MAX = 2**63 - 1

#: Unit -> nanoseconds, for parsing. `m` and `min` are both minutes (SPEC 4.4 writes `1m`; people write `1min`).
UNITS: dict[str, int] = {
    "ns": 1,
    "us": 1_000,
    "µs": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "sec": 1_000_000_000,
    "m": 60_000_000_000,
    "min": 60_000_000_000,
    "h": 3_600_000_000_000,
    "d": 86_400_000_000_000,
}
#: The units `format_duration` writes, largest first.
_OUT_UNITS: tuple[tuple[str, int], ...] = (
    ("h", 3_600_000_000_000),
    ("min", 60_000_000_000),
    ("s", 1_000_000_000),
    ("ms", 1_000_000),
    ("us", 1_000),
    ("ns", 1),
)
_WORDS: tuple[tuple[int, str, str], ...] = (
    (3_600_000_000_000, "hour", "hours"),
    (60_000_000_000, "minute", "minutes"),
    (1_000_000_000, "second", "seconds"),
    (1_000_000, "millisecond", "milliseconds"),
    (1_000, "microsecond", "microseconds"),
    (1, "nanosecond", "nanoseconds"),
)
_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zµ]*)")
_HINT = "write e.g. 250ms, 1s, 1min (or an integer count of nanoseconds)"


class DurationError(ValueError):
    """A duration or horizon that does not parse. `hint` says what to write instead."""

    def __init__(self, message: str, hint: str = _HINT) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


# ---- durations --------------------------------------------------------------------------------------------------


def parse_duration(value: Any, *, what: str = "duration", bare_ns: bool = True) -> int:
    """Nanoseconds of a duration: `"1s"`, `"250ms"`, `"1min"`, `"1m"`, `"60 s"`, `"1.5s"`, `"100us"`, an `int`
    count of nanoseconds or its string. Case and surrounding spaces are ignored. Negative values, fractions of a
    nanosecond and values beyond the int64 range are `DurationError`. With `bare_ns=False` a string without a unit
    (other than `"0"`) is refused, for flags where `"5"` would be ambiguous."""
    if isinstance(value, bool):
        raise DurationError(f"{what}: expected a duration, got {value!r}")
    if isinstance(value, numbers.Integral):
        v = int(value)
        if v < 0:
            raise DurationError(f"{what}: must be >= 0 (got {v})")
        if v > I64_MAX:
            raise DurationError(f"{what}: {v} overflows int64 nanoseconds")
        return v
    if not isinstance(value, str):
        raise DurationError(f"{what}: expected a duration such as '1s', got {type(value).__name__}")
    t = value.strip().lower()
    m = _DUR.fullmatch(t)
    if m is None or (m.group(2) and m.group(2) not in UNITS):
        raise DurationError(f"{what}: {value!r} is not a duration")
    num, unit = m.group(1), m.group(2)
    if not unit:
        if "." in num:
            raise DurationError(f"{what}: {value!r} has no unit")
        if not bare_ns and int(num) != 0:
            raise DurationError(f"{what}: {value!r} has no unit", f"write {num}s, {num}ms, ... ({_HINT})")
        unit = "ns"
    if len(num) > 40:
        raise DurationError(f"{what}: {value!r} overflows int64 nanoseconds")
    with localcontext(prec=60):
        ns = Decimal(num) * UNITS[unit]
    if ns != ns.to_integral_value():
        raise DurationError(f"{what}: {value!r} is not a whole number of nanoseconds")
    if ns > I64_MAX:
        raise DurationError(f"{what}: {value!r} overflows int64 nanoseconds")
    return int(ns)


def _exact(ns: int, scale: int) -> str:
    with localcontext(prec=60):
        return format((Decimal(ns) / scale).normalize(), "f")


def _sig(x: float, digits: int) -> str:
    s = f"{x:.{digits}g}"
    if "e" in s:
        s = format(Decimal(s).normalize(), "f")
    return s


def format_duration(ns: Any, *, sig: int | None = None, signed: bool = False) -> str:
    """A duration for people: `1_000_000_000` -> `"1s"`, `1_500_000_000` -> `"1.5s"`, `60_000_000_000` -> `"1min"`,
    `250_000_000` -> `"250ms"`, `0` -> `"0s"`.

    Without `sig` the form is exact and has the fewest digits (a unit is used only when the value is at least 1 of
    it; a tie goes to the smaller unit, so `90s` rather than `1.5min`), so `parse_duration(format_duration(x)) ==
    x`. With `sig=n` the value is rounded to
    `n` significant digits in the largest unit it reaches (`52_234_000_000` -> `"52.2s"`), for measured spans and
    latencies. `signed` writes a `+` before positive values. `None` is `"-"`."""
    if ns is None:
        return "-"
    if isinstance(ns, float):
        if not math.isfinite(ns):
            return "n/a"
        ns = round(ns)
    n = int(ns)
    sign = "-" if n < 0 else ("+" if signed and n > 0 else "")
    a = abs(n)
    if a == 0:
        return "0s"
    if sig is not None:
        for unit, scale in _OUT_UNITS:
            if a >= scale:
                return f"{sign}{_sig(a / scale, sig)}{unit}"
    best: tuple[int, str] | None = None
    for unit, scale in _OUT_UNITS:  # largest first
        if a < scale:
            continue
        num = _exact(a, scale)
        digits = len(num.replace(".", ""))
        if best is None or digits <= best[0]:  # fewer digits wins; a tie goes to the smaller unit (a whole number)
            best = (digits, num + unit)
    return sign + (best[1] if best else f"{a}ns")


def horizon_key(h: Any) -> str:
    """The key of a horizon in the JSON documents (decimal nanoseconds): `"1s"`, `1_000_000_000` and
    `"1000000000"` -> `"1000000000"`. `DurationError` when `h` is not a duration."""
    return str(parse_duration(h, what="horizon"))


def horizon_label(h: Any) -> str:
    """The human key of a horizon: `"1000000000"` -> `"1s"`, `60_000_000_000` -> `"1min"`."""
    return format_duration(parse_duration(h, what="horizon"))


def horizon_words(h: Any) -> str:
    """A horizon in words, in the largest unit that holds it whole: `"1min"` -> `"1 minute"`, `10_000_000_000` ->
    `"10 seconds"`, `"1.5s"` -> `"1500 milliseconds"`."""
    ns = parse_duration(h, what="horizon")
    for scale, one, many in _WORDS:
        if ns >= scale and ns % scale == 0:
            k = ns // scale
            return f"{k} {one if k == 1 else many}"
    return format_duration(ns)


class HorizonDict(dict):  # type: ignore[type-arg]
    """A `{horizon: value}` map whose keys stay the JSON form (decimal nanoseconds, so it serialises and compares
    exactly as before) and that also answers human keys: `m["1s"]`, `m[1_000_000_000]` and `m["1000000000"]` are
    the same entry. `labels()` is a copy keyed `"1s"`."""

    __slots__ = ()

    def _key(self, key: Any) -> Any:
        if dict.__contains__(self, key):
            return key
        try:
            k = horizon_key(key)
        except (DurationError, TypeError):
            return key
        return k

    def __getitem__(self, key: Any) -> Any:
        return dict.__getitem__(self, self._key(key))

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, self._key(key))

    def get(self, key: Any, default: Any = None) -> Any:
        return dict.get(self, self._key(key), default)

    def labels(self) -> dict[str, Any]:
        """A plain dict keyed by human horizons (`{"1s": ...}`), in this map's order."""
        out: dict[str, Any] = {}
        for k, v in self.items():
            try:
                out[horizon_label(k)] = v
            except DurationError:
                out[str(k)] = v
        return out

    def __repr__(self) -> str:
        return f"HorizonDict({dict.__repr__(self)})"


def horizons(m: Mapping[str, Any] | None) -> Iterator[tuple[str, str]]:
    """`(json_key, label)` of each horizon key of `m`, ascending by nanoseconds."""
    keys = sorted((k for k in (m or {})), key=lambda k: int(k))
    for k in keys:
        yield k, horizon_label(k)


# ---- prices -----------------------------------------------------------------------------------------------------


def price_from_ticks(ticks: Any, price_exp: int) -> Decimal:
    """The exact decimal price of a fixed-point integer price: `ticks * 10**price_exp` (`1_501_200, -4` ->
    `Decimal("150.12")`)."""
    return Decimal(int(ticks)).scaleb(int(price_exp)).normalize()


def _places(tick_size: Any, price_exp: int) -> int:
    """Decimal places of the tick size `tick_size * 10**price_exp` (0 for whole ticks)."""
    try:
        d = Decimal(int(tick_size)).scaleb(int(price_exp)).normalize()
    except (InvalidOperation, TypeError, ValueError):
        return 0
    exp = d.as_tuple().exponent
    return max(0, -int(exp)) if isinstance(exp, int) else 0


def format_price(ticks: Any, price_exp: int | None, tick_size: Any = None) -> str:
    """A price for people: `150.12` (at least as many decimals as the tick size has, so `150.10` shows as
    `150.10` with a 0.01 tick; never rounded). An unknown scale prints the raw integer with its unit
    (`1501200 (price units, scale unknown)`); `None` is `"-"`."""
    if ticks is None:
        return "-"
    if price_exp is None:
        return f"{int(ticks)} (price units, scale unknown)"
    d = price_from_ticks(ticks, price_exp)
    text = format(d, "f")
    have = len(text.partition(".")[2])
    want = _places(tick_size, price_exp) if tick_size is not None else 0
    if want > have:
        text = format(d.quantize(Decimal(1).scaleb(-want)), "f")
    return text


def format_scaled(v: Any, exp: int | None) -> str:
    """A fixed-point integer `v * 10**exp` (a size, a quantity) as a decimal string without an exponent, trailing
    zeros trimmed (`1500, -3` -> `1.5`); with `exp` None or 0 the value as it is; `None` is `"-"`."""
    if v is None:
        return "-"
    if exp is None or exp == 0:
        return f"{v:g}" if isinstance(v, float) else str(v)
    text = format(Decimal(str(v)).scaleb(int(exp)).normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def instrument_units(dataset: Any, instrument_id: int, ts_ns: int | None = None) -> dict[str, Any]:
    """`symbol`, `price_exp`, `size_exp`, `tick_size` and `session_tz` of an instrument, from a dataset directory's
    `instruments.parquet`, on the session that holds `ts_ns` (else the instrument's first row); `{}` when the table or
    the instrument is missing. The values that turn its integers into human units."""
    from pathlib import Path

    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        t = pq.read_table(Path(dataset) / "instruments.parquet")
    except (OSError, pa.ArrowInvalid):
        return {}
    rows = [r for r in t.to_pylist() if int(r.get("instrument_id", -1)) == int(instrument_id)]
    if not rows:
        return {}
    pick = rows[0]
    if ts_ns is not None:
        for r in rows:
            mid = r.get("midnight_utc_ns")
            if mid is not None and int(mid) <= ts_ns < int(mid) + 86_400_000_000_000:
                pick = r
                break
    return {k: pick.get(k) for k in ("symbol", "price_exp", "size_exp", "tick_size", "session_tz")}


# ---- times ------------------------------------------------------------------------------------------------------


def zone(tz: str | None) -> dt.tzinfo | None:
    """The `tzinfo` of an IANA zone name (`UTC` for `"UTC"`), or None when the name is unknown or empty."""
    if not tz:
        return None
    if tz.upper() == "UTC":
        return dt.UTC
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def local_datetime(ts_ns: Any, tz: str | None) -> dt.datetime:
    """An epoch-nanosecond timestamp as an aware `datetime` in zone `tz` (UTC when `tz` is unknown), to the
    microsecond."""
    n = int(ts_ns)
    sec, frac = divmod(n, 1_000_000_000)
    z = zone(tz) or dt.UTC
    return dt.datetime.fromtimestamp(sec, tz=z).replace(microsecond=frac // 1000)


def zone_name(tz: str | None) -> str:
    """The name `local_time` writes for zone `tz`: the IANA name, or `UTC` when it is UTC, unknown or missing."""
    return tz if zone(tz) is not None and tz and tz.upper() != "UTC" else "UTC"


def local_time(ts_ns: Any, tz: str | None, *, digits: int = 6, date: bool = True, named: bool = True) -> str:
    """An epoch-nanosecond timestamp as venue-local wall time with the zone named: `2026-03-02 09:30:00.000123
    America/New_York`. `digits` fraction digits (0 to 9, truncated); `date=False` leaves out the date; `named=False`
    leaves out the zone (for a table whose header names it, `zone_name(tz)`). An unknown or missing zone is written
    in UTC and says so; `None` is `"-"`."""
    if ts_ns is None:
        return "-"
    n = int(ts_ns)
    sec, frac = divmod(n, 1_000_000_000)
    t = dt.datetime.fromtimestamp(sec, tz=zone(tz) or dt.UTC)
    base = t.strftime("%Y-%m-%d %H:%M:%S" if date else "%H:%M:%S")
    if digits > 0:
        base += f".{frac:09d}"[: min(digits, 9) + 1]
    return f"{base} {zone_name(tz)}" if named else base


# ---- notes ------------------------------------------------------------------------------------------------------

_NOTE_TS = re.compile(r"\b(ts|at|from|until|to)( |=)([12]\d{18})\b")
_NOTE_HKEY = re.compile(r"\b(\d{8,})=(better|tie|worse|mixed|n/a)\b")


def humanize_note(text: str, tz: str | None = None) -> str:
    """A warning or note written with raw integers (as stored in a summary's `warnings`) in human units, for display
    only: an epoch-nanosecond time after `ts`, `at`, `from`, `until` or `to` becomes local time with the zone named;
    a `<horizon_ns>=<better|tie|worse|mixed|n/a>` pair gets its horizon as `1s`. Anything else is unchanged."""
    out = _NOTE_TS.sub(lambda m: f"{m.group(1)}{m.group(2)}{local_time(int(m.group(3)), tz)}", text)
    return _NOTE_HKEY.sub(lambda m: f"{format_duration(int(m.group(1)))}={m.group(2)}", out)


# ---- sides ------------------------------------------------------------------------------------------------------

_SIDES = {
    "b": "buy",
    "bid": "buy",
    "buy": "buy",
    "long": "buy",
    "1": "buy",
    "+1": "buy",
    "a": "sell",
    "s": "sell",
    "ask": "sell",
    "offer": "sell",
    "sell": "sell",
    "short": "sell",
    "-1": "sell",
}


def side_word(side: Any) -> str:
    """`buy` or `sell` for a side code (`B` / `A`, `bid` / `ask`, `1` / `-1`, `BUY` / `SELL`); any other value is
    returned as text, unchanged, and `None` is `"-"`."""
    if side is None:
        return "-"
    if isinstance(side, bool):
        return str(side)
    return _SIDES.get(str(side).strip().lower(), str(side))
