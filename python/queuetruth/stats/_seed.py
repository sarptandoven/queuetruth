"""Deterministic seed derivation (SPEC 3.1): `splitmix64` and `stable_seed` call `qt_types::splitmix64` and
`qt_types::stable_seed` through `queuetruth._core`, so the derivation has one implementation. Behaviour is frozen.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

_MASK = (1 << 64) - 1


def _core() -> Any:
    from queuetruth import _core

    return _core


def splitmix64(x: int) -> int:
    """One splitmix64 step (adds the golden gamma, then mixes): `qt_types::splitmix64`."""
    return _core().splitmix64(x & _MASK)


def stable_seed(run_seed: int, parts: Sequence[bytes | str]) -> int:
    """FNV-1a 64 over `run_seed` (8 LE bytes), then per part its length (4 LE bytes) and bytes; then splitmix64.

    `qt_types::stable_seed`; `str` parts are UTF-8 encoded. Use with
    `numpy.random.default_rng(stable_seed(seed, [b"bootstrap", test_id]))`.
    """
    if not 0 <= run_seed <= _MASK:
        raise ValueError(f"run_seed must be a u64, got {run_seed}")
    raw = [p.encode("utf-8") if isinstance(p, str) else bytes(p) for p in parts]
    return _core().stable_seed(run_seed, raw)
