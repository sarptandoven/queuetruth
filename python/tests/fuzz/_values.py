"""Value strategies of the fuzz suite: hostile numbers and timestamps, JSON documents with raw number tokens, and
structure-aware records (a venue's field names with plausible, boundary and hostile values).

`Raw(text)` is a JSON token written verbatim by `dumps` (e.g. `1e999999999`, `NaN`, `-0.0`), so a record can carry
numbers no Python float can hold; the importers parse JSON numbers as `Decimal`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy


@dataclass(frozen=True)
class Raw:
    """A JSON token written as is."""

    text: str


def dumps(obj: Any) -> str:
    """Compact JSON text of `obj` (dicts, lists, str, int, bool, None, `Raw`)."""
    if isinstance(obj, Raw):
        return obj.text
    if isinstance(obj, dict):
        return "{" + ",".join(f"{json.dumps(str(k))}:{dumps(v)}" for k, v in obj.items()) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(dumps(v) for v in obj) + "]"
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return json.dumps(obj)
    if isinstance(obj, float):
        return repr(obj)
    raise TypeError(type(obj))


# ---- numbers ------------------------------------------------------------------------------------------------------

#: Decimal texts at and beyond the edges of what a venue can mean: exponents past the decimal context, digit runs
#: past Python's int-string limit, signed zeros, non-finite spellings, separators, non-ASCII digits.
EDGE_DECIMALS = [
    "0",
    "-0",
    "0.0",
    "-0.00000000",
    "1",
    "-1",
    "0.00000001",
    "0.000000001",
    "1e-8",
    "1E-9",
    "1e8",
    "1e18",
    "9223372036854775807",
    "9223372036854775808",
    "-9223372036854775809",
    "18446744073709551615",
    "18446744073709551616",
    "92233720368.54775807",
    "1e999999",
    "1e1000000",
    "1e999999999",
    "-1e999999999",
    "1e-999999",
    "1e-1000000",
    "1e-999999999",
    "1e400000",
    "9" * 4301,
    "1" + "0" * 5000,
    "0." + "0" * 5000 + "1",
    "NaN",
    "nan",
    "sNaN",
    "-NaN",
    "Infinity",
    "-Infinity",
    "inf",
    "1_000",
    " 1.5 ",
    "+3",
    "1.",
    ".5",
    "0x10",
    "1e",
    "--1",
    "".join(map(chr, (0x661, 0x662, 0x663))),
    "".join(map(chr, (0xFF11, 0xFF12))),
    "",
    " ",
    "abc",
]
#: Raw JSON number tokens (the importers parse them as Decimal; `NaN` / `Infinity` are Python's JSON extensions).
EDGE_NUMBER_TOKENS = [
    "0",
    "-0",
    "-0.0",
    "1e999999999",
    "-1e999999999",
    "1e-999999999",
    "1e400000",
    "1E+1000000",
    "9" * 4301,
    "123456789012345678901234567890",
    "1.7e12",
    "1792071000000",
    "1792071000000000",
    "1792071000000000000",
    "NaN",
    "Infinity",
    "-Infinity",
]

decimal_texts: SearchStrategy[str] = st.one_of(
    st.sampled_from(EDGE_DECIMALS),
    st.integers(0, 12)
    .flatmap(lambda p: st.decimals(allow_nan=False, allow_infinity=False, places=p))
    .map(lambda d: format(d, "f")),
    st.integers(-(10**20), 10**20).map(str),
    st.builds(lambda m, e: f"{m}e{e}", st.integers(-999, 999), st.integers(-30, 30)),
)
raw_numbers: SearchStrategy[Raw] = st.one_of(
    st.sampled_from(EDGE_NUMBER_TOKENS).map(Raw),
    st.integers(-(10**25), 10**25).map(lambda i: Raw(str(i))),
    st.floats(allow_nan=False, allow_infinity=False).map(lambda f: Raw(repr(f))),
)
#: Plausible quantities and prices (so most records get deep into the importer).
amounts: SearchStrategy[str] = st.one_of(
    st.sampled_from(["0", "0.5", "1", "0.001", "100", "60000.00000000", "0.50000000", "101.25", "10", "3"]),
    st.integers(0, 9)
    .flatmap(lambda p: st.decimals(min_value=0, max_value=10**6, allow_nan=False, places=p))
    .map(lambda d: format(d, "f")),
)
numbers_any: SearchStrategy[Any] = st.one_of(amounts, amounts.map(Raw), decimal_texts, raw_numbers)

# ---- timestamps ---------------------------------------------------------------------------------------------------

#: 2026-10-15 13:30:00 UTC.
T0_S = 1_792_071_000
epoch_ms: SearchStrategy[int] = st.integers(T0_S * 1000 - 10**6, T0_S * 1000 + 10**6)
EDGE_EPOCHS = [
    0,
    -1,
    1,
    10**9 - 1,
    10**9,
    10**11,
    10**12 - 1,
    10**12,
    10**14,
    10**15,
    10**17,
    10**18,
    10**20 - 1,
    10**20,
    2**63 - 1,
    2**63,
    2**64,
    -(2**63),
    T0_S,
    T0_S * 1000,
    T0_S * 10**6,
    T0_S * 10**9,
]
epochs: SearchStrategy[Any] = st.one_of(
    epoch_ms,
    epoch_ms.map(str),
    st.sampled_from(EDGE_EPOCHS),
    st.sampled_from(EDGE_EPOCHS).map(str),
    raw_numbers,
    decimal_texts,
)

RFC3339_EDGE = [
    "2026-10-15T13:30:00Z",
    "2026-10-15T13:30:00.123456789Z",
    "2026-10-15T13:30:00.1234567891Z",
    "2026-10-15 13:30:00+05:30",
    "2026-10-15T13:30:00-0800",
    "2026-10-15T13:30:00",
    "2026-10-15T13:30:00,5Z",
    "2026-13-01T00:00:00Z",
    "2026-00-10T00:00:00Z",
    "2026-02-30T00:00:00Z",
    "2026-02-31T00:00:00Z",
    "2026-04-31T00:00:00Z",
    "2026-01-00T00:00:00Z",
    "2026-01-99T00:00:00Z",
    "2026-01-01T24:00:00Z",
    "2026-01-01T99:99:99Z",
    "2026-12-31T23:59:60Z",
    "0000-01-01T00:00:00Z",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59Z",
    "2026-10-15T13:30:00+99:99",
    "2026-10-15T13:30:00+24:00",
    "2026-10-15",
    "",
    "now",
    "".join(map(chr, (0xFF12, 0xFF10, 0xFF12, 0xFF16))) + "-10-15T13:30:00Z",
]


@st.composite
def _rfc3339(draw: st.DrawFn) -> str:
    y = draw(st.integers(0, 9999))
    mo = draw(st.integers(0, 13))
    d = draw(st.integers(0, 32))
    h, mi, s = draw(st.integers(0, 25)), draw(st.integers(0, 61)), draw(st.integers(0, 61))
    frac = draw(st.text("0123456789", max_size=12))
    tz = draw(st.sampled_from(["Z", "z", "+00:00", "-05:00", "+0530", "+99:99", ""]))
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}{'.' + frac if frac else ''}{tz}"


rfc3339_texts: SearchStrategy[str] = st.one_of(st.sampled_from(RFC3339_EDGE), _rfc3339())
#: Valid RFC 3339 times around T0 (offset or `Z`, 0-9 fractional digits).
rfc3339_valid: SearchStrategy[str] = st.builds(
    lambda t, frac, tz: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + (f".{frac}" if frac else "") + tz,
    st.integers(T0_S - 3600, T0_S + 3600),
    st.text("0123456789", max_size=9),
    st.sampled_from(["Z", "+00:00", "z"]),
)

# ---- generic values -----------------------------------------------------------------------------------------------

short_text: SearchStrategy[str] = st.text(max_size=12)
scalars: SearchStrategy[Any] = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**70), 2**70),
    raw_numbers,
    decimal_texts,
    short_text,
    st.sampled_from(
        ["", " ", "null", "true", "None", "\x00", chr(0xD7FF), chr(0xD800), chr(0xDFFF), chr(0x202E), "x" * 300]
    ),
)
nasty: SearchStrategy[Any] = st.recursive(
    scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(short_text, inner, max_size=3)),
    max_leaves=6,
)


def field(values: SearchStrategy[Any] | list[Any], nasty_weight: int = 1) -> SearchStrategy[Any]:
    """A field value: mostly `values` (a strategy, or a list to sample), sometimes anything (`nasty_weight` 0:
    never)."""
    base = st.sampled_from(values) if isinstance(values, list) else values
    return st.one_of(*([base] * 4), *([nasty] * nasty_weight))


def objects(
    fields: dict[str, SearchStrategy[Any]],
    required: dict[str, SearchStrategy[Any]] | None = None,
    *,
    hostile: bool = True,
) -> Any:
    """Dicts with the `required` keys and any subset of the optional `fields` (plus, rarely, a junk key)."""
    base = st.fixed_dictionaries(required or {}, optional=fields)
    if not hostile:
        return base
    junk = st.dictionaries(st.sampled_from(["", "x", "__proto__", "\x00", "e"]), nasty, max_size=1)
    return st.builds(lambda a, b: {**b, **a}, base, st.one_of(st.just({}), st.just({}), junk))


def ids(prefix: str, n: int = 4) -> SearchStrategy[Any]:
    """Order / client ids from a small pool (so records join), as text or JSON number."""
    pool: list[Any] = [f"{prefix}{i}" for i in range(n)] + list(range(1, n + 1))
    return st.one_of(st.sampled_from(pool), st.sampled_from(["", "NONE", "0", "a b", "x" * 200, "é/ü"]))


def json_document(records: SearchStrategy[Any], *, hostile: bool = True) -> SearchStrategy[bytes]:
    """A JSON-lines file, a JSON array or one JSON document of the records; lines optionally in capture wrappers
    (`recv_ns` / `dir` + `local_ns`), sometimes with an extra hostile line, a BOM or CRLF line ends."""

    @st.composite
    def build(draw: st.DrawFn) -> bytes:
        recs = draw(st.lists(records, min_size=0, max_size=8))
        times = epochs if hostile else st.integers(T0_S * 10**9, T0_S * 10**9 + 10**12)
        modes = ["bare", "bare", "recv", "dir", "dir_text", "recv_text"] if hostile else ["bare", "bare", "recv", "dir"]
        wrapped = []
        for r in recs:
            mode = draw(st.sampled_from(modes))
            if mode == "recv":
                r = {"recv_ns": draw(times), "msg": r}
            elif mode == "recv_text":
                r = {"recv_ns": draw(times), "msg": dumps(r)}
            elif mode == "dir":
                dirs = ["in", "out", "OUT", "sideways", None] if hostile else ["in"]
                r = {"dir": draw(st.sampled_from(dirs)), "local_ns": draw(times), "msg": r}
            elif mode == "dir_text":
                r = {"dir": draw(st.sampled_from(["in", "out"])), "local_ns": draw(times), "msg": dumps(r)[:-1]}
            wrapped.append(r)
        layout = draw(st.sampled_from(["lines", "lines", "array", "single"]))
        if layout == "array":
            text = dumps(wrapped)
        elif layout == "single" and wrapped:
            text = dumps(wrapped[0])
        else:
            lines = [dumps(r) for r in wrapped]
            if hostile and draw(st.integers(0, 9)) == 0:
                lines.insert(
                    draw(st.integers(0, len(lines))), draw(st.sampled_from(["{", "[1,", "nul", "{}", "[]", '"x"', "1"]))
                )
            text = draw(st.sampled_from(["\n", "\r\n"])).join(lines)
        data = text.encode("utf-8", "surrogatepass")
        if hostile and draw(st.integers(0, 19)) == 0:
            data = b"\xef\xbb\xbf" + data
        return data

    return build()


# ---- one hostile value at a time -----------------------------------------------------------------------------------

#: Values that break a parser late rather than early: integer look-alikes, digit runs, CSV structure characters, a
#: field past the CSV reader's 131 072-character limit.
LATE_BREAKERS: list[Any] = [
    "--1",
    "---2",
    "+-5",
    "9" * 5000,
    "-" + "9" * 19,
    "1\r2",
    "1\x002",
    '"',
    "a,b",
    "٢",
    "x" * 140_000,
]
one_bad_value: SearchStrategy[Any] = st.one_of(nasty, st.sampled_from(LATE_BREAKERS))


def _paths(obj: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Paths of every leaf and every container of a JSON-like value (a container path adds a key or an item)."""
    out = [path]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_paths(v, (*path, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_paths(v, (*path, i)))
    return out


def _with(obj: Any, path: tuple[Any, ...], value: Any) -> Any:
    """A copy of `obj` with the value at `path` replaced (or, at a container, a junk key / item added)."""
    if not path:
        if isinstance(obj, dict):
            return {**obj, "\x00junk": value}
        if isinstance(obj, list):
            return [*obj, value]
        return value
    head, rest = path[0], path[1:]
    if isinstance(obj, dict):
        return {**obj, head: _with(obj[head], rest, value)}
    return [_with(v, rest, value) if i == head else v for i, v in enumerate(obj)]


def one_field_replaced(records: SearchStrategy[Any]) -> SearchStrategy[Any]:
    """A plausible record with exactly one value (at any depth) replaced by a hostile one, so the parser reaches
    the fields after it with every other field valid."""

    @st.composite
    def build(draw: st.DrawFn) -> Any:
        rec = draw(records)
        return _with(rec, draw(st.sampled_from(_paths(rec))), draw(one_bad_value))

    return build()
