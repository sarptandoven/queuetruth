"""The package's objects inside a real NautilusTrader BacktestEngine (skipped without nautilus_trader)."""

from __future__ import annotations

import math

import pytest

pytest.importorskip("nautilus_trader")

from conftest import exportable, variant
from ntkit import run_episodes

import queuetruth_nautilus as qtn

N = 400
TOUCH = [(0, 100)]  # one print at the order's price, larger than the order
THROUGH = [(-1, 100)]  # one print below a buy order's price


def opts(doc, seed=0, **kw):
    return qtn.venue_options(qtn.from_document(doc), random_seed=seed, **kw)


def test_p_one_fills_every_touch(touch_doc):
    assert run_episodes(opts(exportable(touch_doc, estimate=1.0)), N, TOUCH) == set(range(N))


def test_p_zero_never_fills_a_touch_but_fills_a_trade_through(touch_doc):
    o = exportable(touch_doc, estimate=0.0)
    assert run_episodes(opts(o), N, TOUCH) == set()
    assert run_episodes(opts(o), N, THROUGH) == set(range(N))


def test_calibrated_p_fills_about_p_of_single_touches(touch_doc):
    """With one touch per order the fill fraction is a Binomial(N, p) draw: within 4 sd of the fitted p."""
    cal = qtn.from_document(touch_doc)
    p = cal.prob_fill_on_limit
    filled = run_episodes(qtn.venue_options(cal, random_seed=12345), N, TOUCH)
    sd = math.sqrt(p * (1 - p) / N)
    assert abs(len(filled) / N - p) < 4 * sd, (len(filled), N, p)


def test_two_touches_raise_the_fill_rate_as_independent_draws(touch_doc):
    """Every at-price print is an independent Bernoulli(p) draw: two touches fill 1 - (1 - p)^2 of the orders."""
    cal = qtn.from_document(touch_doc)
    p = cal.prob_fill_on_limit
    filled = run_episodes(qtn.venue_options(cal, random_seed=7), N, [(0, 100), (0, 100)])
    q = 1 - (1 - p) ** 2
    assert abs(len(filled) / N - q) < 4 * math.sqrt(q * (1 - q) / N), (len(filled), q)


def test_same_seed_same_fills_different_seed_different_fills(touch_doc):
    a = run_episodes(opts(touch_doc, seed=99), N, TOUCH)
    b = run_episodes(opts(touch_doc, seed=99), N, TOUCH)
    c = run_episodes(opts(touch_doc, seed=100), N, TOUCH)
    assert a == b
    assert a != c


def test_default_fills_every_touched_order_calibrated_about_p(touch_doc):
    """The NautilusTrader default (p = 1) fills every touched order; the calibrated model fills about p of them."""
    default = run_episodes(
        {"fill_model": _nt_default_fill_model(), "book_type": _l2(), "trade_execution": True}, N, TOUCH
    )
    calibrated = run_episodes(opts(touch_doc, seed=1), N, TOUCH)
    assert len(default) == N
    assert len(calibrated) < 0.4 * N


@pytest.mark.filterwarnings(
    "ignore:nt_queue_l2_prob_touch was fitted under:queuetruth_nautilus.errors.CalibrationWarning"
)
def test_queue_position_from_queue_gated_calibration(queue_doc):
    """`nt_queue_l2_prob_touch` sets queue_position=True: prints at the price first consume the 100 shares ahead."""
    gated = exportable(queue_doc, estimate=1.0)
    o = opts(gated)
    assert o["queue_position"] is True
    assert run_episodes(o, 20, [(0, 50)], level_qty=100) == set()  # 50 of 100 ahead traded: still queued
    assert run_episodes(opts(gated), 20, [(0, 50), (0, 60)], level_qty=100) == set(range(20))  # 10 shares reach us
    # the same prints with the touch family (no queue tracking) fill on the first print
    touch = exportable(variant(queue_doc, family="nt_prob_touch"), estimate=1.0)
    assert opts(touch)["queue_position"] is False
    assert run_episodes(opts(touch), 20, [(0, 50)], level_qty=100) == set(range(20))


def test_refused_calibration_needs_force(queue_doc):
    cal = qtn.from_document(queue_doc)
    with pytest.raises(qtn.CalibrationRefused):
        qtn.venue_options(cal)
    with pytest.warns(qtn.CalibrationWarning):
        o = qtn.venue_options(cal, force=True, random_seed=3)
    assert o["queue_position"] is True


def _nt_default_fill_model():
    from queuetruth_nautilus import compat

    return compat.fill_model_cls()()


def _l2():
    from queuetruth_nautilus import compat

    return compat.book_type_l2()
