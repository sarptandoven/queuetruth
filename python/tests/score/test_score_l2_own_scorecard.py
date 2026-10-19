"""`score` on an L2-only dataset removes the user's own resting orders from the visible depth before the queue
models run (trader review E2, `queuetruth.score.l2_own` wired into `build_scorecard`), and blocks the scorecard
when the removal is not applied (`self_exclude = false`): failed gate `own_orders_in_l2_depth`, a `BLOCKED:`
headline and a warning. The tape is the hand-built one of `test_score_l2_own.py`: live order A (5 lots at 100)
filled 3 lots at t + 5 s while 13 other lots were ahead of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from queuetruth.score import build_scorecard
from queuetruth.score.scorecard import OWN_ORDERS_GATE
from score.qt_score_helpers import T0, S, log_from, order_events
from score.test_score_l2_own import P98, P99, P100, batches_with_own, make_l2_dataset

pytestmark = [pytest.mark.integration, pytest.mark.requires_core]


def _score(tmp_path: Path, name: str, **kw: object):
    tape = (
        make_l2_dataset(tmp_path / "tape", batches_with_own())
        if not (tmp_path / "tape").exists()
        else tmp_path / "tape"
    )
    live = tmp_path / "live"
    if not live.exists():
        log_from(
            [
                order_events("A", "buy", P100, 5, T0 + 2 * S, fills=[(T0 + 5 * S, 3)], cancel_at=T0 + 6 * S),
                order_events("C", "buy", P98, 4, T0 + 3 * S // 2),
                order_events("D", "buy", P99, 2, T0 + 5 * S // 2, cancel_at=T0 + 8 * S),
            ],
            live,
        )
    spec = {
        "live": {"path": str(live)},
        "sim": {"kind": "zoo", "model": "nt_queue_l2"},
        "dataset": {"path": str(tape)},
        "out": str(tmp_path / name),
        "bootstrap": {"b": 20, "block": "auto", "seed": 0},
        "perm_b": 20,
        "match_window_ns": 10_000_000,
    }
    return build_scorecard(spec, **kw)  # type: ignore[arg-type]


def test_l2_scorecard_removes_own_orders_from_the_depth(tmp_path: Path) -> None:
    s = _score(tmp_path, "free")
    assert any(
        w.startswith("self_exclusion: own resting orders removed from the L2 depth (3 orders") for w in s.warnings
    ), s.warnings
    assert OWN_ORDERS_GATE not in s.verdict.failed_gates
    assert not s.verdict.headline.startswith("BLOCKED")
    # A's twin no longer queues behind A's own 5 lots: it fills like the live order did
    assert s.two_by_two.live_only == 0, s.two_by_two


def test_l2_scorecard_without_removal_is_blocked(tmp_path: Path) -> None:
    s = _score(tmp_path, "raw", self_exclude=False)
    assert OWN_ORDERS_GATE in s.verdict.failed_gates
    assert s.verdict.gates_passed is False
    assert s.verdict.headline.startswith("BLOCKED: own resting orders are in the L2 depth")
    assert any(w.startswith(f"{OWN_ORDERS_GATE}: BLOCKING") and "self_exclude is false" in w for w in s.warnings)
    # the live order's own size stays ahead of its twin: the arm misses A's fill
    assert s.two_by_two.live_only >= 1, s.two_by_two
