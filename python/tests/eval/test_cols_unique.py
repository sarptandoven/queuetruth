"""`queuetruth.eval._cols.unique_strings` / `unique_ints` and `blocks._unique_rows` give exactly what `np.unique`
gives (values, first rows, inverse), so the summed statistics of a shard do not depend on which one ran."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest

from queuetruth.eval import _cols as C
from queuetruth.eval import blocks as B


def _ids(rng: np.random.Generator, n: int, k: int) -> list[str]:
    pool = [f"s{rng.integers(0, 10**9)}-{i}" for i in range(k)] + ["", "é", "Z", "a", "aa"]
    return [pool[i] for i in rng.integers(0, len(pool), n)]


@pytest.mark.parametrize("seed", range(6))
def test_unique_strings_is_np_unique(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 3000))
    vals = _ids(rng, n, int(rng.integers(1, 200)))
    # chunked, plain and dictionary columns
    chunks = [vals[: n // 3], vals[n // 3 :]]
    for col in (pa.chunked_array(chunks, pa.utf8()), pa.array(vals), pa.array(vals).dictionary_encode()):
        t = pa.table({"x": col})
        want = np.unique(np.array(vals, dtype=object), return_index=True, return_inverse=True)
        got = C.unique_strings(t, "x")
        assert got[0].dtype == object and list(got[0]) == list(want[0])
        assert np.array_equal(got[1], want[1]) and np.array_equal(got[2], np.asarray(want[2]).reshape(-1))


def test_unique_strings_with_nulls_and_empty() -> None:
    t = pa.table({"x": pa.array(["b", None, "a", "b"])})
    with pytest.raises(TypeError):  # None and str do not compare: the object path is kept as it was
        np.unique(C.strings(t, "x"))
    with pytest.raises(TypeError):
        C.unique_strings(t, "x")
    u, first, inv = C.unique_strings(pa.table({"x": pa.array([], pa.utf8())}), "x")
    assert u.size == first.size == inv.size == 0


@pytest.mark.parametrize("seed", range(6))
def test_unique_ints_is_np_unique(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 5000))
    for x in (
        rng.integers(0, 7, n),  # codes
        rng.integers(-50, 50, n),  # negative values
        rng.choice(np.array([10**9, 10 * 10**9, 60 * 10**9, 300 * 10**9]), n),  # sparse: the hashing path
        rng.integers(1_700_000_000 * 10**9, 1_700_000_100 * 10**9, n),  # timestamps
        np.full(n, 20_000),
    ):
        x = x.astype(np.int64)
        u, inv = C.unique_ints(x)
        wu, winv = np.unique(x, return_inverse=True)
        assert u.dtype == x.dtype and np.array_equal(u, wu) and np.array_equal(inv, np.asarray(winv).reshape(-1))
    u, inv = C.unique_ints(np.zeros(0, dtype=np.int64))
    assert u.size == inv.size == 0


@pytest.mark.parametrize("seed", range(4))
def test_unique_rows_is_row_wise_np_unique(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 4000))
    cols = [rng.integers(0, 3, n), rng.choice([10**9, 60 * 10**9], n), rng.integers(0, 40, n), rng.integers(-2, 2, n)]
    rows, inv = B._unique_rows(n, [lambda c=c: c for c in cols])
    wrows, winv = np.unique(np.stack(cols, axis=1).astype(np.int64), axis=0, return_inverse=True)
    assert rows.dtype == np.int64 and np.array_equal(rows, wrows)
    assert np.array_equal(inv, np.asarray(winv).reshape(-1))
    rows, inv = B._unique_rows(0, [lambda: np.zeros(0)] * 3)
    assert rows.shape == (0, 3) and inv.size == 0
