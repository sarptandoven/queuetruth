"""Deterministic text formatting for the report: numbers, p-values, durations, timestamps, prices and HTML escaping.

Every function is pure and locale-independent, so identical inputs give identical bytes.
"""

from __future__ import annotations

import html
import math
import time
from decimal import Decimal
from typing import Any

DASH = "–"


def esc(text: Any) -> str:
    """HTML/XML-escapes `text` (quotes included); safe inside element content and attribute values."""
    return html.escape(str(text), quote=True)


def is_num(x: Any) -> bool:
    """True for a finite int or float (bools excluded)."""
    if isinstance(x, bool) or x is None:
        return False
    if isinstance(x, int):
        return True
    return isinstance(x, float) and math.isfinite(x)


def num(x: Any, sig: int = 4) -> str:
    """Number with `sig` significant digits, no exponent for ordinary magnitudes; `None`/NaN -> dash."""
    if not is_num(x):
        return DASH
    if isinstance(x, int):
        return f"{x:,}"
    if x == 0:
        return "0"
    a = abs(x)
    if a >= 1e9 or a < 1e-4:
        return f"{x:.{max(sig - 1, 0)}e}"
    digits = max(sig - 1 - math.floor(math.log10(a)), 0)
    s = f"{x:,.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def fixed(x: Any, digits: int = 3) -> str:
    """Fixed-point number; `None`/NaN -> dash."""
    if not is_num(x):
        return DASH
    s = f"{float(x):.{digits}f}"
    return s[1:] if s.startswith("-") and float(s) == 0 else s


def signed(x: Any, digits: int = 3) -> str:
    """Fixed-point number with an explicit sign."""
    if not is_num(x):
        return DASH
    s = f"{float(x):+.{digits}f}"
    return s.replace("-", "+", 1) if float(s) == 0 else s


def pp(x: Any, digits: int = 1) -> str:
    """A fraction shown in signed percentage points (0.031 -> "+3.1 pp")."""
    if not is_num(x):
        return DASH
    return f"{signed(100.0 * float(x), digits)} pp"


def pct(x: Any, digits: int = 1) -> str:
    """A fraction shown as a percentage."""
    if not is_num(x):
        return DASH
    return f"{100.0 * float(x):.{digits}f}%"


def pval(p: Any) -> str:
    """p-value: 3 decimals, or 2 significant digits in exponent form below 0.001."""
    if not is_num(p):
        return DASH
    p = float(p)
    if p < 1e-300:
        return "<1e-300"
    if p < 1e-3:
        return f"{p:.1e}"
    return f"{p:.3f}"


def ci(lo: Any, hi: Any, f: Any = num) -> str:
    """`[lo, hi]` with the given formatter; dash when either end is undefined."""
    if not (is_num(lo) and is_num(hi)):
        return DASH
    return f"[{f(lo)}, {f(hi)}]"


def dur(ns: Any) -> str:
    """Duration in ns as a short human string (`250 µs`, `100 ms`, `1 s`, `5 min`)."""
    if not is_num(ns):
        return DASH
    v = float(ns)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a == 0:
        return "0"
    for unit, scale in (("h", 3.6e12), ("min", 6e10), ("s", 1e9), ("ms", 1e6), ("µs", 1e3)):
        if a >= scale and (unit not in ("h", "min") or a % scale == 0):
            return f"{sign}{num(a / scale, 3).replace(',', '')} {unit}"
    return f"{sign}{num(a, 3).replace(',', '')} ns"


def dur_axis(ns: float) -> str:
    """Compact duration for axis ticks."""
    return dur(ns).replace(" ", "")


def ts(ns: Any) -> str:
    """UTC timestamp in ns as `YYYY-MM-DD HH:MM:SS.nnnnnnnnn`."""
    if not is_num(ns):
        return DASH
    n = int(ns)
    sec, frac = divmod(n, 1_000_000_000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(sec)) + f".{frac:09d}"


def ts_short(ns: Any) -> str:
    """UTC time of day `HH:MM:SS.mmm` (axis and compact tables)."""
    if not is_num(ns):
        return DASH
    n = int(ns)
    sec, frac = divmod(n, 1_000_000_000)
    return time.strftime("%H:%M:%S", time.gmtime(sec)) + f".{frac // 1_000_000:03d}"


def date_of(days: Any) -> str:
    """Arrow date32 (days since 1970-01-01) as `YYYY-MM-DD`."""
    if not is_num(days):
        return DASH
    return time.strftime("%Y-%m-%d", time.gmtime(int(days) * 86400))


def price(px: Any, exp: int | None) -> str:
    """Fixed-point price as an exact decimal when `exp` is known, else the raw integer."""
    if not is_num(px):
        return DASH
    if exp is None:
        return str(int(px))
    d = Decimal(int(px)).scaleb(int(exp))
    s = format(d.normalize(), "f")
    return s


def bytes_h(n: Any) -> str:
    """Byte count with a binary unit."""
    if not is_num(n):
        return DASH
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if v < 1024 or unit == "GiB":
            return f"{int(v)} B" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GiB"


def coord(x: float) -> str:
    """SVG coordinate with one decimal and no trailing zeros."""
    s = f"{x:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    return "0" if s == "-0" else s


def horizon_key(h: int) -> str:
    """`{h: v}` map key of a horizon (decimal ns string)."""
    return str(int(h))
