"""Leaderboard order and wording (trader review C1, top-10 item 4): sorted by |fill-rate bias| at 60 s (configurable),
Brier only as a tie-break, one plain-English sentence per model."""

from __future__ import annotations

from typing import Any

from queuetruth.eval import DEFAULT_SORT_HORIZON_NS, bias_sentence, rank_leaderboard, sort_horizon
from queuetruth.eval.leaderboard import duration_text, horizons_of
from queuetruth.schemas.models import LeaderboardEntry

S = 1_000_000_000


def _entry(m: str, bias: dict[int, tuple[float | None, float | None, float | None]], brier: dict[int, float]) -> dict:
    return {
        "model_id": m,
        "brier": {str(h): v for h, v in brier.items()},
        "logloss": {},
        "fill_rate_bias": {str(h): {"value": v, "ci_lo": lo, "ci_hi": hi} for h, (v, lo, hi) in bias.items()},
        "overfill_bias": {"value": None, "ci_lo": None, "ci_hi": None},
        "qty_bias": None,
        "ttf_iad": None,
        "mcnemar": {"b": 0, "c": 0, "p_exact": None},
    }


def _board() -> list[dict[str, Any]]:
    # at 1 s every model has Brier 0 (nobody fills): the old sort by Brier at 1 s was arbitrary
    return [
        _entry("databento_mc[k=-0.8]", {S: (0.0, 0.0, 0.0), 60 * S: (-0.011, -0.02, -0.002)}, {S: 0.0, 60 * S: 0.2}),
        _entry("hbt_power3[n=2]", {S: (0.0, 0.0, 0.0), 60 * S: (-0.007, -0.013, -0.003)}, {S: 0.0, 60 * S: 0.21}),
        _entry("front_of_queue", {S: (0.0, 0.0, 0.0), 60 * S: (0.002, -0.004, 0.009)}, {S: 0.0, 60 * S: 0.22}),
        _entry("no_estimate", {S: (None, None, None)}, {S: 0.0}),
        _entry("tie_a", {60 * S: (0.002, 0.001, 0.003)}, {60 * S: 0.1}),
    ]


def test_sort_horizon_default_and_fallbacks() -> None:
    assert DEFAULT_SORT_HORIZON_NS == 60 * S
    assert sort_horizon([S, 10 * S, 60 * S, 300 * S]) == 60 * S
    assert sort_horizon([S, 10 * S, 30 * S]) == 30 * S  # the longest horizon below the target
    assert sort_horizon([120 * S, 300 * S]) == 120 * S  # none below: the shortest
    assert sort_horizon([S, 10 * S], 10 * S) == 10 * S  # configurable
    assert sort_horizon([]) is None


def test_rank_by_absolute_bias_then_brier_then_id() -> None:
    board = _board()
    ranked = [e["model_id"] for e in rank_leaderboard(board, 60 * S)]
    assert ranked == ["tie_a", "front_of_queue", "hbt_power3[n=2]", "databento_mc[k=-0.8]", "no_estimate"]
    assert horizons_of(board) == [S, 60 * S]
    # pydantic entries rank the same way
    models = [LeaderboardEntry.model_validate(e) for e in board]
    assert [e.model_id for e in rank_leaderboard(models, 60 * S)] == ranked


def test_bias_sentences() -> None:
    by = {e["model_id"]: e for e in _board()}
    assert (
        bias_sentence(by["hbt_power3[n=2]"], 60 * S)
        == "At 60 s, hbt_power3[n=2] fills 0.7 pp less often than the exact replay (95% CI -1.3 to -0.3 pp)."
    )
    assert (
        bias_sentence(by["front_of_queue"], 60 * S)
        == "At 60 s, front_of_queue fills 0.2 pp more often than the exact replay, within noise (95% CI -0.4 to "
        "+0.9 pp)."
    )
    assert bias_sentence(by["databento_mc[k=-0.8]"], S).startswith(
        "At 1 s, databento_mc[k=-0.8] fills as often as the exact replay (bias 0.0 pp)"
    )
    assert "has no fill-rate bias" in bias_sentence(by["no_estimate"], S)
    assert duration_text(300 * S) == "5 min" and duration_text(S // 10) == "100 ms"
