"""Shared helpers for the reference-value generators in `scripts/reference/`.

- An independent Python mirror of the SPEC 3.1 RNG (`splitmix64`, `stable_seed`, `Xoshiro256StarStar`), used only to
  build small deterministic synthetic inputs (never market data). Inputs are always echoed into the fixture, so a test
  never has to regenerate them.
- A deterministic JSON writer: floats via `repr` (shortest round-trip), no NaN/Infinity (`null` instead), fixed key
  order, two-space indent, trailing newline.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

M64 = 0xFFFFFFFFFFFFFFFF
GAMMA = 0x9E3779B97F4A7C15

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "reference"


def splitmix64(x: int) -> int:
    """SPEC 3.1: `z = x + gamma; z = (z ^ z>>30) * C1; z = (z ^ z>>27) * C2; z ^ z>>31` (wrapping)."""
    z = (x + GAMMA) & M64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
    return z ^ (z >> 31)


def stable_seed(run_seed: int, parts: Sequence[bytes]) -> int:
    """SPEC 3.1: FNV-1a 64 over run_seed (8 LE) then each part as (len 4 LE, bytes); result through splitmix64."""
    h = 0xCBF29CE484222325
    data = bytearray(run_seed.to_bytes(8, "little"))
    for p in parts:
        data += len(p).to_bytes(4, "little") + bytes(p)
    for b in data:
        h = ((h ^ b) * 0x100000001B3) & M64
    return splitmix64(h)


class Xoshiro256StarStar:
    """SPEC 3.1 xoshiro256**: state = splitmix64(seed + k*gamma) for k = 0..3 (each output of the sequence)."""

    def __init__(self, seed: int) -> None:
        self.s = [splitmix64((seed + k * GAMMA) & M64) for k in range(4)]

    def next_u64(self) -> int:
        s = self.s
        x = (s[1] * 5) & M64
        result = ((((x << 7) | (x >> 57)) & M64) * 9) & M64
        t = (s[1] << 17) & M64
        s[2] ^= s[0]
        s[3] ^= s[1]
        s[1] ^= s[2]
        s[0] ^= s[3]
        s[2] ^= t
        s[3] = ((s[3] << 45) | (s[3] >> 19)) & M64
        return result

    def next_f64(self) -> float:
        return (self.next_u64() >> 11) * 2.0**-53

    def range_inclusive(self, lo: int, hi: int) -> int:
        span = (hi - lo + 1) & M64
        x = self.next_u64()
        return x if span == 0 else lo + x % span


def rng_for(name: str, seed: int = 0) -> Xoshiro256StarStar:
    """Input-generation stream for one fixture case: `Xoshiro256StarStar(stable_seed(seed, [b"ref", name]))`."""
    return Xoshiro256StarStar(stable_seed(seed, [b"ref", name.encode()]))


def clean(obj: Any) -> Any:
    """Convert numpy scalars/arrays to plain Python; NaN/inf -> None; tuples -> lists."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        np = None  # type: ignore[assignment]
    if np is not None:
        if isinstance(obj, np.ndarray):
            return [clean(v) for v in obj.tolist()]
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            obj = float(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def write_fixture(name: str, doc: dict[str, Any]) -> Path:
    """Write `tests/fixtures/reference/<name>` deterministically and return its path."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / name
    text = json.dumps(clean(doc), indent=2, allow_nan=False, ensure_ascii=True) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def header(oracle: list[str], generator: str, description: str, packages: Sequence[str] = ()) -> dict[str, Any]:
    """Common fixture header. Versions are recorded (no wall clock, host or absolute path)."""
    import importlib.metadata as md
    import platform

    return {
        "reference": name_of(generator),
        "oracle": oracle,
        "generator": f"scripts/reference/{generator}",
        "description": description,
        "software": {"python": platform.python_version(), **{p: md.version(p) for p in packages}},
    }


def name_of(generator: str) -> str:
    return generator.rsplit(".", 1)[0].removeprefix("gen_")


def write_manifest() -> Path:
    """Rewrite `MANIFEST.sha256` (shasum -a 256 format, sorted by name) over every fixture file except itself."""
    lines = []
    for p in sorted(FIXTURES.iterdir()):
        if p.is_file() and p.name != "MANIFEST.sha256":
            lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n")
    out = FIXTURES / "MANIFEST.sha256"
    out.write_text("".join(lines), encoding="utf-8")
    return out
