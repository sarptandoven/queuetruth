"""Deterministic seed derivation: bit-identical port of `qt_types::splitmix64` and `qt_types::stable_seed` (SPEC 3.1).

Moved verbatim from the original `queuetruth.stats` module; behaviour is frozen.
"""

from __future__ import annotations

from collections.abc import Sequence

_MASK = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3


def splitmix64(x: int) -> int:
    """One splitmix64 step (adds the golden gamma, then mixes), as `qt_types::splitmix64`."""
    z = (x + _GOLDEN) & _MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
    return z ^ (z >> 31)


def stable_seed(run_seed: int, parts: Sequence[bytes | str]) -> int:
    """FNV-1a 64 over `run_seed` (8 LE bytes), then per part its length (4 LE bytes) and bytes; then splitmix64.

    Bit-identical to `qt_types::stable_seed`; `str` parts are UTF-8 encoded. Use with
    `numpy.random.default_rng(stable_seed(seed, [b"bootstrap", test_id]))`.
    """
    if not 0 <= run_seed <= _MASK:
        raise ValueError(f"run_seed must be a u64, got {run_seed}")
    h = _FNV_OFFSET

    def feed(data: bytes) -> None:
        nonlocal h
        for b in data:
            h = ((h ^ b) * _FNV_PRIME) & _MASK

    feed(run_seed.to_bytes(8, "little"))
    for p in parts:
        raw = p.encode("utf-8") if isinstance(p, str) else bytes(p)
        feed(min(len(raw), 0xFFFFFFFF).to_bytes(4, "little"))
        feed(raw)
    return splitmix64(h)
