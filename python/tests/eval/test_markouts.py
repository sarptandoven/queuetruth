"""`queuetruth.eval.markouts`: the one markout implementation, by hand on small paths (the callers' own outputs are
pinned in `test_markout_pins.py`)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from queuetruth.eval.markouts import Markouts, PricePath, bps, ccy, markouts, ticks

S = 1_000_000_000


def _path(**kw: object) -> PricePath:
    # quotes: t=10 bid 100 ask 102; t=20 bid 101 ask 103 (and a one-sided row at 20); t=30 bid 104 ask 105
    return PricePath(
        np.array([10, 20, 20, 30], dtype=np.int64),
        np.array([100, 101, 0, 104], dtype=np.int64),
        np.array([102, 103, 103, 105], dtype=np.int64),
        ok=np.array([True, True, False, True]),
        bid_sz=np.array([1.0, 3.0, 0.0, 0.0]),
        ask_sz=np.array([1.0, 1.0, 5.0, 0.0]),
        **kw,  # type: ignore[arg-type]
    )


def _nan(x: float) -> bool:
    return isinstance(x, float) and math.isnan(x)


def test_mid_markouts_by_hand() -> None:
    m = markouts(_path(end=30), [15, 15, 20], [1, -1, 1], [101, 101, 100], [0, 5, 15, 20])
    assert isinstance(m, Markouts) and m.mo.shape == (3, 4)
    # buy at 101, t=15: M(15) = 101, M(20) = row 2 (equal timestamps: the last, one-sided) = undefined,
    # M(30) = 104.5, t + 20 = 35 > end = 30: censored
    assert m.mo[0, 0] == 0.0 and _nan(m.mo[0, 1]) and m.mo[0, 2] == 3.5 and _nan(m.mo[0, 3])
    assert m.mo[1, 2] == -3.5  # the sell side
    # M(t-) of t=15 is the row at 10 (101); of t=20 the row at 10 too (strictly before)
    assert m.pre.tolist() == [101.0, 101.0, 101.0]
    assert m.mm[0, 2] == 3.5 and m.mm[1, 2] == -3.5
    # t=20: M(20) and M(25) are the one-sided row, M(35) is past the end: no markout; `mm` neither
    assert np.isnan(m.mo[2, :3]).all() and np.isnan(m.mm[2, :3]).all()
    assert markouts(_path(end=40), [20], [1], [100], [10]).mo[0, 0] == 4.5  # M(30) = 104.5 against P = 100


def test_censoring_last_quote_and_no_end() -> None:
    hs = [0, 20]
    assert _nan(markouts(_path(end=30), [15], [1], [101], hs).mo[0, 1])
    assert markouts(_path(end=30), [15], [1], [101], hs, past_end="last_quote").mo[0, 1] == 3.5
    assert markouts(_path(end=12), [11], [1], [101], hs, past_end="last_quote").mo[0, 1] == 0.0  # M(end) = 101
    assert _nan(markouts(_path(end=25), [15], [1], [101], hs, past_end="last_quote").mo[0, 1])  # one-sided at end
    assert markouts(_path(), [15], [1], [101], hs).mo[0, 1] == 3.5  # no end: never censored
    assert _nan(markouts(_path(end=30), [5], [1], [101], [0]).mo[0, 0])  # before the first row


def test_microprice_and_print_paths() -> None:
    m = markouts(_path(end=30), [15], [1], [101], [0], ref="microprice")
    assert m.mo[0, 0] == (100 - 101) + 2 * 0.5  # sizes 1 and 1
    m = markouts(_path(end=30), [25], [1], [101], [0], ref="microprice")
    assert _nan(m.mo[0, 0])  # the prevailing row (t=20, one-sided) is undefined
    m = markouts(_path(end=35), [31], [1], [101], [0], ref="microprice")
    assert _nan(m.mo[0, 0])  # both sizes 0
    pr = PricePath.prints(np.array([1, 5], dtype=np.int64), np.array([50, 53], dtype=np.int64), end=9)
    m = markouts(pr, [3, 3], [1, -1], [51, 51], [0, 2, 7])
    assert m.mo[:, :2].tolist() == [[-1.0, 2.0], [1.0, -2.0]] and np.isnan(m.mo[:, 2]).all()
    with pytest.raises(ValueError, match="sizes"):
        markouts(pr, [3], [1], [51], [0], ref="microprice")


def test_adverse_sign_and_invalid_prices() -> None:
    fill = markouts(_path(end=30), [15, 15], [1, -1], [101, 101], [15])
    adv = markouts(_path(end=30), [15, 15], [1, -1], [101, 101], [15], sign="adverse")
    assert (adv.mo == -fill.mo).all() and (adv.mm == -fill.mm).all()
    m = markouts(_path(end=30), [15], [1], [0], [15], px_valid=[False])
    assert _nan(m.mo[0, 0]) and m.mm[0, 0] == 3.5  # `mm` does not need the fill price


def test_list_paths_equal_array_paths() -> None:
    """The sandbox reads its growing history as lists (None = empty side): the same markouts."""
    arr = _path(end=None)
    lst = PricePath([10, 20, 20, 30], [100, 101, None, 104], [102, 103, 103, 105])
    ts, side, px, hs = [9, 10, 15, 20, 25, 31], [1, -1, 1, -1, 1, 1], [101] * 6, [0, 1, 5, 10, 30]
    a, b = markouts(arr, ts, side, px, hs), markouts(lst, ts, side, px, hs)
    assert np.array_equal(a.mo, b.mo, equal_nan=True) and np.array_equal(a.mm, b.mm, equal_nan=True)
    assert np.array_equal(a.pre, b.pre, equal_nan=True)


def test_keyed_paths_and_missing_keys() -> None:
    paths = {("x", 1): _path(end=30)}
    m = markouts(paths, [15, 15], [1, 1], [101, 101], [0], keys=[("x", 1), ("y", 1)])
    assert m.mo[0, 0] == 0.0 and _nan(m.mo[1, 0])
    with pytest.raises(ValueError, match="keys"):
        markouts(paths, [15], [1], [101], [0])
    empty = markouts(None, [], [], [], [0, 1])
    assert empty.mo.shape == (0, 2)


def test_bad_settings() -> None:
    for kw in ({"ref": "last"}, {"past_end": "end"}, {"sign": "taker"}):
        with pytest.raises(ValueError):
            markouts(_path(), [1], [1], [1], [0], **kw)  # type: ignore[arg-type]


def test_units() -> None:
    assert ticks(3.5, 7) == 0.5 and bps(1.0, -200) == 50.0
    assert ccy(2.0, -2, 3, 1, 10.0) == 2.0 * 10.0**-2 * 3 * 10.0**1 * 10.0
