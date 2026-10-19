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

_CTX = decimal.Context(prec=80, rounding=decimal.ROUND_HALF_EVEN)


def input_error(location: str, msg: str) -> QtError:
    """`QT-INPUT-002` (format or schema mismatch) at `location`."""
    return QtError("QT-INPUT-002", f"{location}: {msg}")


def units_error(msg: str) -> QtError:
    """`QT-INPUT-004` (decimal not exactly representable)."""
    return QtError("QT-INPUT-004", msg)


# ---- decimals ---------------------------------------------------------------------------------------------------


def dec(value: Any, what: str, location: str = "") -> Decimal | None:
    """Decimal of a JSON / CSV value (str, int or Decimal); None for null or empty text. Non-numbers fail."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise input_error(location or what, f"{what}: boolean {value!r} is not a number")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(repr(value))
    else:
        text = str(value).strip()
        if text == "":
            return None
        try:
            d = Decimal(text)
        except InvalidOperation as e:
            raise input_error(location or what, f"{what}: {value!r} is not a decimal number") from e
    if not d.is_finite():
        raise input_error(location or what, f"{what}: {value!r} is not finite")
    return d


def dec_text(value: Any) -> str | None:
    """Original decimal text: strings as given (stripped), numbers in plain positional notation."""
    if value is None:
        return None
    if isinstance(value, str):
        t = value.strip()
        return t or None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return str(value)


def to_fixed(d: Decimal, exp: int, what: str, *, unsigned: bool = False) -> int:
    """`d * 10^-exp` as an exact integer (int64, or uint64 when `unsigned`); `QT-INPUT-004` when not exact."""
    scaled = d.scaleb(-exp, _CTX)
    if scaled != scaled.to_integral_value(context=_CTX):
        raise units_error(f"{what}: {format(d, 'f')} is not representable at exponent {exp}")
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
    """Exact product."""
    return _CTX.multiply(a, b)


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


def epoch_ns(value: Any, unit: str, what: str, location: str = "") -> tuple[int, str]:
    """Integer epoch value -> (ns, precision). `unit` is the documented unit or `auto` (detected from magnitude);
    a value whose magnitude contradicts the documented unit fails instead of being rescaled."""
    d = dec(value, what, location)
    if d is None:
        raise input_error(location or what, f"{what}: missing timestamp")
    if d != d.to_integral_value():
        raise input_error(location or what, f"{what}: {value!r} is not an integer epoch value")
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
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?\s*(Z|z|[+-]\d{2}:?\d{2})?$"
)


def _offset_s(tz: str) -> int:
    if tz in ("Z", "z"):
        return 0
    sign = -1 if tz[0] == "-" else 1
    body = tz[1:].replace(":", "")
    return sign * (int(body[:2]) * 3600 + int(body[2:]) * 60)


def rfc3339_ns(text: str, what: str, location: str = "", *, default_tz: str | None = None) -> tuple[int, str]:
    """RFC 3339 / ISO 8601 date-time with up to 9 fractional digits -> (UTC ns, precision), exact. Without an
    offset, `default_tz` (IANA name) is required."""
    m = _RFC3339.match(str(text).strip())
    if not m:
        raise input_error(location or what, f"{what}: {text!r} is not an RFC 3339 timestamp")
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    tz = m.group(8)
    frac_ns = int((frac + "000000000")[:9])
    if tz is not None:
        secs = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - _offset_s(tz)
        return secs * 10**9 + frac_ns, _precision(len(frac))
    if default_tz is None:
        raise input_error(location or what, f"{what}: {text!r} has no UTC offset and no time zone was given")
    secs = local_to_utc_s(y, mo, d, h, mi, s, default_tz, what, location)
    return secs * 10**9 + frac_ns, _precision(len(frac))


def local_to_utc_s(y: int, mo: int, d: int, h: int, mi: int, s: int, tz: str, what: str, location: str = "") -> int:
    """Local wall time in IANA zone `tz` -> UTC epoch seconds; DST-ambiguous or nonexistent times fail."""
    if tz.upper() == "UTC":
        return calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
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


def utc_date_days(ts_ns: int) -> int:
    """Days since 1970-01-01 of a UTC ns timestamp."""
    return ts_ns // (86_400 * 10**9)


def date_days(text: str) -> int:
    """`YYYY-MM-DD` -> days since 1970-01-01."""
    d = dt.date.fromisoformat(text.strip())
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
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=lambda o: format(o, "f"))


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
            except json.JSONDecodeError as e:
                raise input_error(location, f"wrapped msg is not JSON: {e}") from e
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
    if d is None or d != d.to_integral_value():
        raise input_error(location, f"{what} must be an integer ns timestamp, got {v!r}")
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
    except json.JSONDecodeError:
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
        out.append(_unwrap(obj, s, f"{path}:{n}", file_idx, n))
    return out


def dump_raw(obj: Any) -> str:
    """Compact JSON text of a parsed object (numbers kept exact)."""
    return _dump(obj)


def read_csv_rows(text: str) -> tuple[list[str], list[list[str]]]:
    """Header and rows of a CSV text (header names stripped)."""
    rows = list(csv.reader(io.StringIO(text)))
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
        s = format(v, "f")
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
