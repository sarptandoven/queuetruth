"""Small exact helpers of the sandbox: duration and time-of-day parsing, decimal <-> fixed-point prices, local time
rendering, dates, and canonical hashing (SPEC 7.3 canonical JSON)."""

from __future__ import annotations

import datetime as _dt
import math
import numbers
import re
import warnings
from decimal import Decimal, InvalidOperation
from typing import Any

from queuetruth import units
from queuetruth.errors import QtError
from queuetruth.integrity import canonical_sha256, sha256_text

NS = 1
US = 1_000
MS = 1_000_000
S = 1_000_000_000
MIN = 60 * S
H = 3600 * S
DAY_NS = 86_400 * S
I64_MAX = 2**63 - 1
UNDEF_PRICE = I64_MAX
_SANDBOX_DUR = re.compile(r"^\s*[0-9]+\s*(ns|us|ms|s|m|h)\s*$")
_TOD = re.compile(r"^\s*([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?\s*$")


def config_error(msg: str, hint: str | None = None, **details: Any) -> QtError:
    """`QT-CONFIG-001` with optional structured details."""
    return QtError("QT-CONFIG-001", msg, hint, details or None)


def usage_error(msg: str, hint: str | None = None, **details: Any) -> QtError:
    """`QT-USAGE-001` (the call does not fit the session's state)."""
    return QtError("QT-USAGE-001", msg, hint, details or None)


def wire(value: Any) -> Any:
    """An instruction argument as the JSON value the sandbox validates and logs: None, bools, integers, strings and
    finite floats as they are (other integer and real types, e.g. numpy's, as `int` and `float`), a `Decimal` as its
    string, a non-finite float as `'nan'`, `'inf'` or `'-inf'`, anything else as its repr. Validating this form rather
    than the original value is what lets `verify` re-execute a rejected instruction from the log and meet the same
    rejection, with the same message."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        f = float(value)
        return f if math.isfinite(f) else repr(f)
    return repr(value)


def duration(value: Any, what: str) -> int:
    """A duration in ns: an integer (ns) or a string `<integer><unit>` with unit ns, us, ms, s, m or h (`250us`,
    `100ms`, `1s`, `5m`), read by `queuetruth.units.parse_duration`. The sandbox takes this whole-number subset of the
    grammar, so an instruction logged by any version replays the same: a string without a unit (`"5"` is ambiguous:
    SPEC 4.4's CLI footgun), a fraction (`1.5s`) or another unit (`1d`) is refused. Errors are `QT-CONFIG-001`."""
    if isinstance(value, str) and not _SANDBOX_DUR.match(value):
        raise config_error(
            f"{what}: duration {value!r} needs a whole number and a unit",
            "write e.g. 250us, 100ms, 1s, 5m (or an integer in ns)",
        )
    try:
        return units.parse_duration(value, what=what, bare_ns=False)
    except units.DurationError as e:
        raise config_error(e.message, e.hint) from None


def parse_duration(value: Any, what: str) -> int:
    """Deprecated: use `queuetruth.units.parse_duration` (this name stays, with the sandbox's error code)."""
    warnings.warn(
        "queuetruth.sandbox._util.parse_duration is deprecated; use queuetruth.units.parse_duration",
        DeprecationWarning,
        stacklevel=2,
    )
    return duration(value, what)


def parse_time(value: Any, midnight_utc_ns: int, what: str) -> int:
    """An absolute time in ns since the epoch: an integer, or a local time of day `HH:MM[:SS[.fraction]]` of the
    session day (`midnight_utc_ns` = its local midnight in UTC ns)."""
    if isinstance(value, bool):
        raise config_error(f"{what}: expected a time, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = _TOD.match(value)
        if not m:
            raise config_error(
                f"{what}: time {value!r} is not HH:MM[:SS[.fraction]]", "or give absolute ns since the epoch"
            )
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        frac = (m.group(4) or "").ljust(9, "0")
        if hh > 47 or mm > 59 or ss > 59:
            raise config_error(f"{what}: time {value!r} out of range")
        return midnight_utc_ns + hh * H + mm * MIN + ss * S + int(frac or 0)
    raise config_error(f"{what}: expected a time (ns or 'HH:MM'), got {type(value).__name__}")


def fmt_duration(ns: int) -> str:
    """A duration in its shortest exact form (`1ms`, `250us`, `5s`, `1min`, `0s`): `units.format_duration`."""
    return units.format_duration(int(ns))


def local_time(ts: int, midnight_utc_ns: int) -> str:
    """`HH:MM:SS.ffffff` local time of day of `ts` (hours beyond 24 for the next day; `-` before midnight)."""
    d = ts - midnight_utc_ns
    sign = "-" if d < 0 else ""
    d = abs(d)
    hh, rem = divmod(d, H)
    mm, rem = divmod(rem, MIN)
    ss, ns = divmod(rem, S)
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}.{ns // 1000:06d}"


def rel_time(ns: int) -> str:
    """`+HH:MM:SS.ffffff` of a time in ns since the session open (`-` before it): the clock of a blind episode."""
    text = local_time(int(ns), 0)
    return text if text.startswith("-") else "+" + text


def date_of_days(days: int) -> str:
    """`YYYY-MM-DD` of a date32 day number."""
    return (_dt.date(1970, 1, 1) + _dt.timedelta(days=int(days))).isoformat()


def days_of_date(text: str) -> int:
    """date32 day number of `YYYY-MM-DD`."""
    try:
        return (_dt.date.fromisoformat(text) - _dt.date(1970, 1, 1)).days
    except ValueError:
        raise config_error(f"session_date {text!r} is not a valid YYYY-MM-DD date") from None


def to_fixed(value: Any, exp: int, what: str) -> int:
    """A decimal price or size (`str`, `Decimal`, or `int` already in fixed-point units) as an integer at `10^exp`,
    computed with integer arithmetic (no decimal context rounding); a decimal not representable exactly is
    `QT-INPUT-004`, one beyond 64 bits `QT-CONFIG-001`."""
    if isinstance(value, bool):
        raise config_error(f"{what}: expected a number, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise config_error(f"{what}: give a decimal string (e.g. '291.65') or an integer in fixed-point units")
    text = str(value).strip()
    if len(text) > 64:
        raise config_error(f"{what}: {text[:20]}... is too long for a price or size")
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise config_error(f"{what}: {value!r} is not a decimal number") from None
    if not d.is_finite():
        raise config_error(f"{what}: {value!r} is not finite")
    sign, digits, e = d.as_tuple()
    coeff = int("".join(map(str, digits)) or "0")
    if coeff == 0:
        return 0
    k = int(e) - exp
    if k >= 0:
        if len(digits) + k > 19:
            raise config_error(f"{what}: {value} overflows 64-bit fixed point")
        out = coeff * 10**k
    else:
        if -k > len(digits):
            q, r = 0, coeff
        else:
            q, r = divmod(coeff, 10 ** (-k))
        if r:
            raise QtError(
                "QT-INPUT-004",
                f"{what}: {value} is not representable at 10^{exp} (finer than the instrument's price grid)",
                "round it to the tick size",
            )
        out = q
    if out >= I64_MAX:
        raise config_error(f"{what}: {value} overflows 64-bit fixed point")
    return -out if sign else out


def tick_decimals(tick: int, exp: int) -> int:
    """Decimals needed to print every multiple of `tick` at `10^exp` exactly."""
    if tick <= 0:
        return max(0, -exp)
    t = Decimal(tick).scaleb(exp).normalize()
    return max(0, -int(t.as_tuple().exponent))


def fmt_fixed(v: int | None, exp: int, decimals: int | None = None) -> str:
    """Fixed-point integer as a decimal string (`decimals` digits; default: no trailing zeros)."""
    if v is None:
        return "-"
    d = Decimal(int(v)).scaleb(exp)
    if decimals is not None:
        return f"{d:.{decimals}f}"
    s = format(d.normalize(), "f")
    return s


def real(v: int, exp: int) -> float:
    """Fixed-point integer as a float in real units."""
    return float(Decimal(int(v)).scaleb(exp))


def money(raw: int | Decimal, exp: int, mult: float) -> Decimal:
    """Exact currency amount of `raw` price x size units at `10^exp` (price_exp + size_exp) times the contract
    multiplier; converted to float once, by the caller."""
    return Decimal(raw).scaleb(exp) * Decimal(repr(float(mult)))


def dec(x: float) -> Decimal:
    """A float as the decimal of its shortest repr (`0.1` -> `Decimal('0.1')`)."""
    return Decimal(repr(float(x)))


def digest(value: Any) -> str:
    """sha256 of the canonical JSON (RFC 8785) of a JSON value."""
    return canonical_sha256(value)


__all__ = [
    "DAY_NS",
    "MIN",
    "MS",
    "NS",
    "UNDEF_PRICE",
    "US",
    "H",
    "S",
    "config_error",
    "date_of_days",
    "days_of_date",
    "dec",
    "digest",
    "duration",
    "fmt_duration",
    "fmt_fixed",
    "local_time",
    "money",
    "parse_duration",
    "parse_time",
    "real",
    "rel_time",
    "sha256_text",
    "tick_decimals",
    "to_fixed",
    "usage_error",
    "wire",
]
