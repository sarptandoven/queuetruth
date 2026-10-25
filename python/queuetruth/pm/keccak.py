"""Keccak-256 as used by Ethereum and Polygon (original Keccak padding `0x01 .. 0x80`, rate 136 bytes).

`hashlib.sha3_256` is the FIPS 202 SHA3-256 (padding `0x06`) and gives different digests, so event topics
(`keccak256("OrderFilled(bytes32,...)")`) need this implementation. It is a direct transcription of the
Keccak-f[1600] permutation from the Keccak reference (theta, rho, pi, chi, iota over 24 rounds); it is slow (pure
Python) and used only to hash event signatures. Known answers are tested in `python/tests/pm/test_chain.py`.
"""

from __future__ import annotations

_MASK = (1 << 64) - 1
_RC = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)
#: Rotation offsets r[x][y].
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_RATE = 136


def _rol(v: int, n: int) -> int:
    n %= 64
    return ((v << n) | (v >> (64 - n))) & _MASK if n else v


def _permute(a: list[int]) -> list[int]:
    for rc in _RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [a[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _ROT[x][y])
        a = [b[i] ^ ((b[(i + 1) % 5 + i - i % 5] ^ _MASK) & b[(i + 2) % 5 + i - i % 5]) for i in range(25)]
        a[0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    """Keccak-256 digest (32 bytes) of `data`."""
    msg = bytearray(data)
    msg.append(0x01)
    while len(msg) % _RATE:
        msg.append(0)
    msg[-1] |= 0x80
    state = [0] * 25
    for off in range(0, len(msg), _RATE):
        block = msg[off : off + _RATE]
        for i in range(_RATE // 8):
            state[i] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        state = _permute(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


def topic(signature: str) -> str:
    """Event topic 0 (`0x` + 64 lower-case hex digits) of a canonical event signature."""
    return "0x" + keccak256(signature.encode("ascii")).hex()


__all__ = ["keccak256", "topic"]
