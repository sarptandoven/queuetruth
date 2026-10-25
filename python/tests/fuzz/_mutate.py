"""Mutation fuzzing over real seed files (the importer fixtures): byte flips, insertions, deletions, duplicated and
swapped lines, truncation, numeric tokens replaced by edge values, and dictionary tokens spliced in. Each example
applies 1-6 mutations to one seed, so most outputs stay close to a valid file and reach deep parser states."""

from __future__ import annotations

import re
from pathlib import Path

from fuzz._values import EDGE_DECIMALS, EDGE_NUMBER_TOKENS
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

LIVE_FIXTURES = Path(__file__).resolve().parents[1] / "live" / "fixtures"
_NUM = re.compile(rb"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
GENERIC_TOKENS: list[bytes] = [
    t.encode("utf-8", "surrogatepass") for t in [*EDGE_DECIMALS, *EDGE_NUMBER_TOKENS] if len(t) < 6000
] + [b"\x00", b"\xff\xfe", b"\xc3", b"\xed\xa0\x80", b"\r", b"\n", b'"', b"{", b"}", b"[", b"]", b",", b":", b"\\u0000"]


def seeds(*globs: str) -> list[bytes]:
    """Contents of the fixture files matching `globs` (relative to the live fixtures directory)."""
    out = []
    for g in globs:
        for p in sorted(LIVE_FIXTURES.glob(g)):
            if p.name != "expected.json" and p.is_file():
                out.append(p.read_bytes())
    if not out:
        raise FileNotFoundError(f"no fixture seeds for {globs}")
    return out


def _lines(b: bytes) -> list[bytes]:
    return b.split(b"\n")


@st.composite
def _mutate_once(draw: st.DrawFn, data: bytes, tokens: list[bytes]) -> bytes:
    n = len(data)
    op = draw(st.integers(0, 10))
    pos = draw(st.integers(0, n)) if n else 0
    if op == 0 and n:  # flip one bit
        i = min(pos, n - 1)
        return data[:i] + bytes([data[i] ^ (1 << draw(st.integers(0, 7)))]) + data[i + 1 :]
    if op == 1:  # insert random bytes
        return data[:pos] + draw(st.binary(min_size=1, max_size=8)) + data[pos:]
    if op == 2 and n:  # delete a range
        k = draw(st.integers(1, max(1, min(64, n - min(pos, n - 1)))))
        return data[:pos] + data[pos + k :]
    if op == 3 and n:  # duplicate a range
        k = draw(st.integers(1, max(1, min(256, n - min(pos, n - 1)))))
        return data[: pos + k] + data[pos : pos + k] + data[pos + k :]
    if op == 4:  # truncate
        return data[:pos]
    if op in (5, 6):  # replace a numeric token
        nums = list(_NUM.finditer(data))
        if nums:
            m = nums[draw(st.integers(0, len(nums) - 1))]
            return data[: m.start()] + draw(st.sampled_from(tokens)) + data[m.end() :]
        return data
    if op == 7:  # splice a token
        return data[:pos] + draw(st.sampled_from(tokens)) + data[pos:]
    lines = _lines(data)
    if len(lines) < 2:
        return data
    i, j = draw(st.integers(0, len(lines) - 1)), draw(st.integers(0, len(lines) - 1))
    if op == 8:  # swap two lines
        lines[i], lines[j] = lines[j], lines[i]
    elif op == 9:  # duplicate a line
        lines.insert(j, lines[i])
    else:  # delete a line
        del lines[i]
    return b"\n".join(lines)


def mutated(seed_files: list[bytes], tokens: list[bytes] | None = None) -> SearchStrategy[bytes]:
    """One seed with 1-6 mutations."""
    toks = list(GENERIC_TOKENS) + list(tokens or [])

    @st.composite
    def build(draw: st.DrawFn) -> bytes:
        data = draw(st.sampled_from(seed_files))
        for _ in range(draw(st.integers(1, 6))):
            data = draw(_mutate_once(data, toks))
        return data

    return build()
