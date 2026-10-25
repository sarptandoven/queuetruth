"""Pure helpers of the demo's answer: model id handling and the headline sentence."""

from __future__ import annotations

import pytest

from queuetruth.demo.answer import find_model, short_id
from queuetruth.demo.run import headline_sentence
from queuetruth.demo.summary import Cell


@pytest.mark.parametrize(
    ("full", "short"),
    [
        ("nt_prob_touch[mode=faithful;p=1]", "nt_prob_touch[p=1]"),
        ("front_of_queue[mode=faithful]", "front_of_queue"),
        ("hbt_power3[exch=partial;mode=faithful;n=2]", "hbt_power3[n=2]"),
        ("hbt_power3[exch=no_partial;mode=corrected;n=2]", "hbt_power3[exch=no_partial;mode=corrected;n=2]"),
        ("trade_through_only", "trade_through_only"),
    ],
)
def test_short_id(full: str, short: str) -> None:
    assert short_id(full) == short


def test_find_model() -> None:
    board = [{"model_id": "nt_queue_l2[mode=faithful]"}, {"model_id": "nt_prob_touch[mode=faithful;p=1]"}]
    assert find_model(board, "nt_prob_touch[p=1]") is board[1]
    assert find_model(board, "nt_prob_touch[p=0.5]") is None
    assert find_model([], "nt_prob_touch[p=1]") is None


def _c(exact: float | None, model: float | None, lo: float | None = None, hi: float | None = None) -> Cell:
    bias = None if exact is None or model is None else model - exact
    return Cell(exact_fill_rate=exact, model_fill_rate=model, bias=bias, ci_lo=lo, ci_hi=hi, n=10, ci_method="none")


def test_headline_sentence_variants() -> None:
    k = "1000000000"
    s = headline_sentence({k: _c(0.294, 0.331, 0.026, 0.048)})
    assert s == (
        "Within 1 s, NautilusTrader's default fill model fills 33.1% of these orders; the exact replay of the real "
        "queue fills 29.4%: 3.7 pp (1.13x) too many (95% CI +2.6 to +4.8 pp)."
    )
    assert headline_sentence({k: _c(0.30, 0.25)}).endswith(": 5.0 pp (0.83x) too few.")
    assert headline_sentence({k: _c(0.30, 0.3001)}).endswith("fills 30.0%: the same.")
    assert headline_sentence({k: _c(0.0, 0.01)}).endswith(": 1.0 pp too many.")
    assert headline_sentence({}).startswith("No fill-rate bias within 1 s")
    assert headline_sentence({k: _c(None, 0.2)}).startswith("No fill-rate bias")
    assert "Within 10 s" in headline_sentence({"10000000000": _c(0.5, 0.6)}, 10_000_000_000)
