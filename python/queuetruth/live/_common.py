"""Shared helpers of the live-log package: exact decimal conversion (SPEC 3.9, `QT-INPUT-004`), timestamp parsing
into integer UTC ns with their precision, id sanitising for `order_key` (SPEC 0.1 id regex) and input readers
(JSON, JSON lines with the capture wrappers of SPEC 5.7.1, CSV).

Capture wrappers accepted on every JSON-lines input: `{"recv_ns": int, "msg": <raw>}` (gives `ts_local_recv`) and
`{"dir": "out"|"in", "local_ns": int, "msg": <raw>}` (request/response capture: `out` gives `ts_local_send`, `in`
gives `ts_local_recv`). `msg` may be the raw message object or its JSON text.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import decimal
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from queuetruth.errors import QtError

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")
_ID_BAD = re.compile(r"[^A-Za-z0-9_.:\-]")
I64_MIN, I64_MAX = -(2**63), 2**63 - 1
U64_MAX = 2**64 - 1
#: Maximum `order_key` length; leaves room for the `.leg<k>` suffix of schedule legs (ids are <= 128 chars).
MAX_KEY_LEN = 116

#: Range of every decimal an input may carry (`dec`): |x| < 1e31 with at most 40 fractional digits (71 digit
#: positions). Venue quantities, prices, fees and ns epochs are far inside it, and so is the float noise a venue's
#: JSON can carry (`1.3877787807814457e-17` has 33 fractional digits); outside it a value is an attack
#: (`1e999999999` would make `int()` allocate gigabytes). Within it sums and differences of up to 10**9 values stay
#: exact at the 80-digit context below. Whether a value is representable at its column's exponent is `to_fixed`'s
#: check, not this one.
MAX_MAGNITUDE = 30
MAX_SCALE = 40

_CTX = decimal.Context(prec=80, rounding=decimal.ROUND_HALF_EVEN)
#: 80 digits, any exponent, and an exception instead of a rounded result: the fast path of `to_fixed` and `dmul`.
_EXACT = decimal.Context(
    prec=80,
    rounding=decimal.ROUND_HALF_EVEN,
    Emin=decimal.MIN_EMIN,
    Emax=decimal.MAX_EMAX,
    traps=[decimal.Inexact, decimal.Overflow, decimal.InvalidOperation],
)


def input_error(location: str, msg: str) -> QtError:
    """`QT-INPUT-002` (format or schema mismatch) at `location`."""
    return QtError("QT-INPUT-002", f"{location}: {msg}")


def units_error(msg: str) -> QtError:
    """`QT-INPUT-004` (decimal not exactly representable)."""
    return QtError("QT-INPUT-004", msg)


# ---- decimals ---------------------------------------------------------------------------------------------------


def dec(value: Any, what: str, location: str = "") -> Decimal | None:
    """Decimal of a JSON / CSV value (str, int or Decimal); None for null or empty text. Non-numbers fail
    `QT-INPUT-002`; values outside `MAX_MAGNITUDE` / `MAX_SCALE` fail `QT-INPUT-004`."""
    if value is None:
        return None
    short = False  # text of at most MAX_SCALE characters without an exponent: its scale needs no check
    if isinstance(value, bool):
        raise input_error(location or what, f"{what}: boolean {value!r} is not a number")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
        short = True
    elif isinstance(value, float):
        d = Decimal(repr(value))
    else:
        text = str(value).strip()
        if text == "":
            return None
        try:
            d = Decimal(text)
        except InvalidOperation as e:
            raise input_error(location or what, f"{what}: {_show(value)} is not a decimal number") from e
        short = len(text) <= MAX_SCALE and "e" not in text and "E" not in text
    if not d.is_finite():
        raise input_error(location or what, f"{what}: {_show(value)} is not finite")
    if d.is_zero():
        return Decimal(0).copy_sign(d)  # `0E+999999999` and friends: no extreme exponent leaks into arithmetic
    if d.adjusted() > MAX_MAGNITUDE or not (short or _scale_ok(d)):
        raise units_error(
            f"{location or what}: {what}: {_show(value)} is outside the supported decimal range "
            f"(|x| < 1e{MAX_MAGNITUDE + 1}, at most {MAX_SCALE} fractional digits)"
        )
    return d


def _scale_ok(d: Decimal) -> bool:
    """At most `MAX_SCALE` fractional digits once trailing zeros are dropped (non-zero finite `d`)."""
    exp = d.as_tuple().exponent
    assert isinstance(exp, int)
    return exp >= -MAX_SCALE or _eff_exp(d) >= -MAX_SCALE


def _eff_exp(d: Decimal) -> int:
    """Exponent of the last non-zero digit of a non-zero finite decimal (`1.500` -> -1, `1200` -> 2)."""
    _, digits, exp = d.as_tuple()
    assert isinstance(exp, int)
    return exp + len(digits) - len(bytes(digits).rstrip(b"\x00"))


def _show(value: Any) -> str:
    """`repr` of an input value for an error message, shortened (inputs can be megabytes)."""
    r = repr(value) if not isinstance(value, Decimal) else repr(str(value))
    return r if len(r) <= 64 else f"{r[:40]}...({len(r)} chars)"


def dec_text(value: Any) -> str | None:
    """Original decimal text: strings as given (stripped), numbers in plain positional notation."""
    if value is None:
        return None
    if isinstance(value, str):
        t = value.strip()
        return t or None
    if isinstance(value, Decimal):
        return plain_decimal(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return str(value)


def to_fixed(d: Decimal, exp: int, what: str, *, unsigned: bool = False) -> int:
    """`d * 10^-exp` as an exact integer (int64, or uint64 when `unsigned`); `QT-INPUT-004` when not exact or out
    of range. The range is checked on the decimal exponent before any integer is built, so a hostile exponent costs
    nothing, and the scaling never rounds: when the scaled value would need more than 80 digits, it has non-zero
    digits below the units place (it is below 10**21), so it is not representable."""
    if not d.is_finite():
        raise units_error(f"{what}: {d} is not a finite number")
    if d.is_zero():
        return 0
    if d.adjusted() - exp > 20:  # the scaled value has more than 21 integer digits
        raise units_error(f"{what}: {_show(d)} out of {'uint64' if unsigned else 'int64'} range at exponent {exp}")
    try:
        scaled = d.scaleb(-exp, _EXACT)
    except decimal.Inexact:
        scaled = None
    if scaled is None or scaled != scaled.to_integral_value():
        raise units_error(f"{what}: {_show(d)} is not representable at exponent {exp}")
    v = int(scaled)
    if unsigned:
        if v < 0 or v > U64_MAX:
            raise units_error(f"{what}: {format(d, 'f')} out of uint64 range at exponent {exp}")
    elif v < I64_MIN or v > I64_MAX:
        raise units_error(f"{what}: {format(d, 'f')} out of int64 range at exponent {exp}")
    return v


def dsum(values: list[Decimal]) -> Decimal:
    """Exact sum."""
    total = Decimal(0)
    for v in values:
        total = _CTX.add(total, v)
    return total


def dsub(a: Decimal, b: Decimal) -> Decimal:
    """Exact difference."""
    return _CTX.subtract(a, b)


def dmul(a: Decimal, b: Decimal) -> Decimal:
    """Exact product: at 80 digits when it fits (the common case), else in a context as wide as both factors'
    digits, so nothing is rounded."""
    try:
        return _EXACT.multiply(a, b)
    except decimal.Inexact:
        ctx = _CTX.copy()
        ctx.prec = len(a.as_tuple().digits) + len(b.as_tuple().digits)
        return ctx.multiply(a, b)


def ddiv_exact(a: Decimal, b: Decimal) -> Decimal | None:
    """`a / b` when the quotient is exact within 80 digits, else None."""
    ctx = _CTX.copy()
    ctx.clear_flags()
    q = ctx.divide(a, b)
    return None if ctx.flags[decimal.Inexact] else q


# ---- timestamps -------------------------------------------------------------------------------------------------

_UNIT_MULT = {"s": 10**9, "ms": 10**6, "us": 10**3, "ns": 1}
# Magnitude windows (2001-09 .. 5138) per epoch unit.
_UNIT_BOUNDS = {"s": (10**9, 10**11), "ms": (10**12, 10**14), "us": (10**15, 10**17), "ns": (10**18, 10**20)}
_EPOCH_MIN, _EPOCH_MAX = Decimal(10**9), Decimal(10**20)


def epoch_ns(value: Any, unit: str, what: str, location: str = "") -> tuple[int, str]:
    """Integer epoch value -> (ns, precision). `unit` is the documented unit or `auto` (detected from magnitude);
    a value whose magnitude contradicts the documented unit fails instead of being rescaled."""
    d = dec(value, what, location)
    if d is None:
        raise input_error(location or what, f"{what}: missing timestamp")
    if d != d.to_integral_value():
        raise input_error(location or what, f"{what}: {_show(value)} is not an integer epoch value")
    if not _EPOCH_MIN <= d < _EPOCH_MAX:  # before int(): `1e999999999` must not become an integer
        raise input_error(location or what, f"{what}: {_show(value)} is outside every epoch unit window")
    v = int(d)
    hits = [u for u, (lo, hi) in _UNIT_BOUNDS.items() if lo <= v < hi]
    if not hits:
        raise input_error(location or what, f"{what}: {v} is outside every epoch unit window")
    detected = hits[0]
    if unit != "auto" and detected != unit:
        raise input_error(location or what, f"{what}: {v} looks like epoch {detected} but the format documents {unit}")
    return v * _UNIT_MULT[detected], detected


def epoch_ns_opt(value: Any, unit: str, what: str, location: str = "") -> tuple[int | None, str | None]:
    """`epoch_ns` with None / empty / non-positive sentinel values mapped to (None, None)."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None, None
    d = dec(value, what, location)
    if d is None or d <= 0:
        return None, None
    return epoch_ns(value, unit, what, location)


def _precision(frac_digits: int) -> str:
    if frac_digits == 0:
        return "s"
    if frac_digits <= 3:
        return "ms"
    if frac_digits <= 6:
        return "us"
    return "ns"


_RFC3339 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?\s*(Z|z|[+-]\d{2}:?\d{2})?$",
    re.ASCII,
)


def _offset_s(tz: str) -> int:
    if tz in ("Z", "z"):
        return 0
    sign = -1 if tz[0] == "-" else 1
    body = tz[1:].replace(":", "")
    h, m = int(body[:2]), int(body[2:])
    if h > 23 or m > 59:
        raise ValueError(f"UTC offset {tz!r} out of range")
    return sign * (h * 3600 + m * 60)


def rfc3339_ns(text: str, what: str, location: str = "", *, default_tz: str | None = None) -> tuple[int, str]:
    """RFC 3339 / ISO 8601 date-time with up to 9 fractional digits -> (UTC ns, precision), exact. Without an
    offset, `default_tz` (IANA name) is required."""
    m = _RFC3339.match(str(text).strip())
    if not m:
        raise input_error(location or what, f"{what}: {_show(text)} is not an RFC 3339 timestamp")
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    tz = m.group(8)
    frac_ns = int((frac + "000000000")[:9])
    if tz is not None:
        _check_civil(y, mo, d, h, mi, s, text, what, location)
        try:
            off = _offset_s(tz)
        except ValueError as e:
            raise input_error(location or what, f"{what}: {_show(text)}: {e}") from e
        secs = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - off
        return secs * 10**9 + frac_ns, _precision(len(frac))
    if default_tz is None:
        raise input_error(location or what, f"{what}: {text!r} has no UTC offset and no time zone was given")
    secs = local_to_utc_s(y, mo, d, h, mi, s, default_tz, what, location)
    return secs * 10**9 + frac_ns, _precision(len(frac))


def local_to_utc_s(y: int, mo: int, d: int, h: int, mi: int, s: int, tz: str, what: str, location: str = "") -> int:
    """Local wall time in IANA zone `tz` -> UTC epoch seconds; impossible dates and times (month 13, February 30,
    hour 24, year 0) fail `QT-INPUT-002`, as do DST-ambiguous or nonexistent times. Second 60 (a leap second) is
    accepted and counts as the next minute's second 0 (POSIX time)."""
    _check_civil(y, mo, d, h, mi, s, f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}", what, location)
    if tz.upper() == "UTC":
        return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise QtError("QT-CONFIG-001", f"unknown time zone {tz!r}") from e
    try:
        naive = dt.datetime(y, mo, d, h, mi, s)
    except ValueError as e:
        raise input_error(location or what, f"{what}: invalid date-time: {e}") from e
    a = naive.replace(tzinfo=zone, fold=0)
    b = naive.replace(tzinfo=zone, fold=1)
    if a.utcoffset() != b.utcoffset():
        raise input_error(location or what, f"{what}: {naive.isoformat()} is ambiguous in {tz} (DST fall-back)")
    back = a.astimezone(dt.UTC).astimezone(zone).replace(tzinfo=None)
    if back != naive:
        raise input_error(location or what, f"{what}: {naive.isoformat()} does not exist in {tz} (DST gap)")
    off = a.utcoffset()
    off_s = 0 if off is None else int(off.total_seconds())
    return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - off_s


def _check_civil(y: int, mo: int, d: int, h: int, mi: int, s: int, text: str, what: str, location: str) -> None:
    """`QT-INPUT-002` unless the fields name a real UTC date-time (second 60 allowed for leap seconds)."""
    try:
        dt.datetime(y, mo, d, h, mi, min(s, 59))
    except ValueError as e:
        raise input_error(location or what, f"{what}: {_show(text)} is not a valid date-time: {e}") from e
    if s > 60:
        raise input_error(location or what, f"{what}: {_show(text)} is not a valid date-time: second {s}")


def utc_date_days(ts_ns: int) -> int:
    """Days since 1970-01-01 of a UTC ns timestamp."""
    return ts_ns // (86_400 * 10**9)


def date_days(text: str) -> int:
    """`YYYY-MM-DD` -> days since 1970-01-01."""
    try:
        d = dt.date.fromisoformat(text.strip())
    except ValueError as e:
        raise input_error(_show(text), f"not a YYYY-MM-DD date: {e}") from e
    return (d - dt.date(1970, 1, 1)).days


# ---- ids --------------------------------------------------------------------------------------------------------


def safe_id_part(text: str) -> str:
    """Id component restricted to `[A-Za-z0-9_.:-]` (other characters become `_`)."""
    return _ID_BAD.sub("_", text) or "_"


def make_order_key(venue: str, account: str, ident: str) -> str:
    """`<venue>:<account>:<venue_order_id or root client_order_id>`, sanitised to the id regex and shortened
    (with a sha256 suffix) above `MAX_KEY_LEN` characters."""
    key = f"{safe_id_part(venue)}:{safe_id_part(account)}:{safe_id_part(ident)}"
    if len(key) > MAX_KEY_LEN:
        digest = hashlib.sha256(f"{venue}\x00{account}\x00{ident}".encode()).hexdigest()[:16]
        key = key[: MAX_KEY_LEN - 17] + "-" + digest
    return key


def valid_id(text: str) -> bool:
    """True when `text` matches the SPEC 0.1 id rule (1..=128 chars of `[A-Za-z0-9_.:-]`)."""
    return 1 <= len(text) <= 128 and ID_RE.match(text) is not None


# ---- inputs -----------------------------------------------------------------------------------------------------


@dataclass
class Record:
    """One input record: parsed object, its raw text, location, and capture-wrapper local times."""

    obj: Any
    raw: str
    location: str
    file_idx: int
    line_idx: int
    recv_ns: int | None = None
    send_ns: int | None = None
    direction: str | None = None
    wrapped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _loads(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _dump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=plain_decimal)


def plain_decimal(d: Decimal) -> str:
    """Positional text of a decimal (`1E+3` -> `1000`), or its scientific form when the positional one would run past
    `_PLAIN_MAX` characters: `format(Decimal("1e999999999"), "f")` builds a billion-character string."""
    t = d.as_tuple()
    if d.is_finite() and isinstance(t.exponent, int) and t.exponent >= -_PLAIN_MAX and d.adjusted() <= _PLAIN_MAX:
        return format(d, "f")
    return str(d)


_PLAIN_MAX = 100


def read_text(path: Path) -> str:
    """UTF-8 text of an input file (BOM stripped); `QT-INPUT-001` when unreadable."""
    try:
        data = path.read_bytes()
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from e
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise input_error(str(path), f"not UTF-8 text: {e}") from e


def _unwrap(obj: Any, raw: str, location: str, file_idx: int, line_idx: int) -> Record:
    if isinstance(obj, dict) and "msg" in obj and ("recv_ns" in obj or "local_ns" in obj):
        msg = obj["msg"]
        if isinstance(msg, str):
            try:
                msg = _loads(msg)
            except (ValueError, RecursionError) as e:
                raise input_error(location, f"wrapped msg is not JSON: {type(e).__name__}: {e!s:.200}") from e
        rec = Record(msg, raw, location, file_idx, line_idx, wrapped=True)
        if "recv_ns" in obj:
            rec.recv_ns = _int_field(obj["recv_ns"], "recv_ns", location)
            rec.direction = "in"
        else:
            direction = str(obj.get("dir", "in")).lower()
            if direction not in ("in", "out"):
                raise input_error(location, f"capture dir must be 'in' or 'out', got {direction!r}")
            local = _int_field(obj["local_ns"], "local_ns", location)
            rec.direction = direction
            if direction == "out":
                rec.send_ns = local
            else:
                rec.recv_ns = local
        return rec
    return Record(obj, raw, location, file_idx, line_idx)


def _int_field(v: Any, what: str, location: str) -> int:
    d = dec(v, what, location)
    if d is None or d != d.to_integral_value() or not I64_MIN <= d <= I64_MAX:
        raise input_error(location, f"{what} must be an integer ns timestamp (int64), got {_show(v)}")
    return int(d)


def read_json_records(path: Path, file_idx: int) -> list[Record]:
    """Records of a JSON document (array -> elements; object -> one record) or of JSON lines (one value per
    non-empty line). Capture wrappers are unwrapped. Unparseable lines fail with their line number."""
    text = read_text(path)
    stripped = text.strip()
    if not stripped:
        return []
    try:
        doc = _loads(stripped)
    except (ValueError, RecursionError):  # JSONDecodeError, a number past the int digit limit, deep nesting
        doc = None
        whole = False
    else:
        whole = True
    out: list[Record] = []
    if whole:
        items = doc if isinstance(doc, list) else [doc]
        for i, item in enumerate(items):
            raw = stripped if not isinstance(doc, list) else _dump(item)
            out.append(_unwrap(item, raw, f"{path}[{i}]", file_idx, i))
        return out
    for n, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        try:
            obj = _loads(s)
        except json.JSONDecodeError as e:
            raise input_error(f"{path}:{n}", f"invalid JSON line: {e.msg}") from e
        except ValueError as e:  # an integer past Python's int-string digit limit
            raise input_error(f"{path}:{n}", f"invalid JSON line: {e}") from e
        except RecursionError as e:
            raise input_error(f"{path}:{n}", "invalid JSON line: nested too deeply") from e
        out.append(_unwrap(obj, s, f"{path}:{n}", file_idx, n))
    return out


def dump_raw(obj: Any) -> str:
    """Compact JSON text of a parsed object (numbers kept exact)."""
    return _dump(obj)


def read_csv_rows(text: str, where: str = "CSV input") -> tuple[list[str], list[list[str]]]:
    """Header and rows of a CSV text (header names stripped); `QT-INPUT-002` naming `where` and the line when the
    text is not CSV the reader accepts (a bare CR inside an unquoted field, a field past the reader's 128 KiB field
    limit)."""
    reader = csv.reader(io.StringIO(text))
    try:
        rows = list(reader)
    except csv.Error as e:
        raise input_error(f"{where}:{reader.line_num}", f"not readable as CSV: {e}") from e
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], []
    return [c.strip() for c in rows[0]], rows[1:]


def jget(obj: Any, *keys: str) -> Any:
    """First present (non-None) key of a dict."""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        v = obj.get(k)
        if v is not None:
            return v
    return None


def text_or_none(v: Any) -> str | None:
    """String form of an id-like value; None for null / empty."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        s = plain_decimal(v)
    else:
        s = str(v).strip()
    return s or None


def as_bool(v: Any) -> bool | None:
    """JSON / text boolean; None for null or empty."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y", "t"):
        return True
    if s in ("false", "0", "no", "n", "f"):
        return False
    return None
