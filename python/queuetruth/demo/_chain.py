"""Exact frame-chain walk by pointer doubling (shared by the ITCH and IEX fast paths).

Variable-length framing is sequential: frame k+1 starts where frame k ends. Given every position that could start
a unit (`cand`, sorted, a superset of the true starts) and where each candidate's unit would end (`end`), the true
chain from position 0 is expanded in O(log n) vectorized steps: `jump` maps a candidate to the candidate its unit
ends on (or to a sentinel), and squaring `jump` doubles the number of steps covered. The walk is exact: a chain
that reaches a position that is not a candidate (`BROKEN`) returns None so the caller falls back to its scalar
loop, and a unit that ends past the buffer, or whose successor's header is not complete in it, ends the chain.
"""

from __future__ import annotations

import numpy as np


def chain_from_zero(cand: np.ndarray, end: np.ndarray, n: int, header: int) -> np.ndarray | None:
    """Indices into `cand` of the complete units chained from `cand[0] == 0`, in order; None when the chain leaves
    the candidate set. `n` is the buffer length and `header` the units a successor needs to be recognised (both in
    the units of `cand`)."""
    m = int(cand.size)
    if m == 0 or int(cand[0]) != 0:
        return None
    end_s, broken = m, m + 1
    j = np.searchsorted(cand, end)
    jc = np.minimum(j, m - 1)
    hit = (j < m) & (cand[jc] == end)
    succ = np.where(hit, j, broken)
    succ = np.where(end + header > n, end_s, succ)
    jump = np.concatenate([succ, [end_s, broken]]).astype(np.int64)
    path = np.zeros(1, np.int64)
    while True:
        nxt = jump[path]
        path = np.concatenate([path, nxt])
        if nxt[-1] >= m:  # the chain ends inside the steps covered so far
            break
        jump = jump[jump]
    path = path[path < m]
    if succ[path[-1]] == broken:
        return None
    return path[end[path] <= n]


__all__ = ["chain_from_zero"]
