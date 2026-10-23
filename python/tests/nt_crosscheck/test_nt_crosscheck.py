"""queuetruth's NautilusTrader-style zoo models against the real `nautilus_trader` BacktestEngine.

On the synthetic Nasdaq fixture (one session, 400 sampled shadows) `scripts/nt_crosscheck/run.py all` replays the
zoo's own L2 view through NautilusTrader with the same passive GTC orders and compares fill outcomes per order and
horizon. Pinned facts (nautilus_trader 1.231.0, faithful zoo, cross-check of 2026-10-19):

- Rule-set replicas reproduce both sides exactly. Both follow NautilusTrader: a hidden NO_AGGRESSOR print at the
  order price is a touch; queue ahead only falls on trades at the price and is set to 0 when the level is deleted
  or scrolls out of the view; queue models do not fill on trade-throughs or crosses while queue ahead > 0. The one
  remaining rule difference is a CLEAR + snapshot inside the window (NautilusTrader zeroes the queue and keeps the
  order, the zoo censors the pair); no shadow window on the fixture contains a clear.
- `touch_p1` (NT default `prob_fill_on_limit = 1`) agrees with `nt_prob_touch[p=1]` on every order and horizon, and
  `queue_p1` (`queue_position=True`) agrees with `nt_queue_l2` and `nt_queue_l2_prob_touch[p=1]` on every order and
  horizon, so the zoo's bias vs the exact replay is NautilusTrader's own.
- At p = 0.29 the zoo reports exactly the fill-probability expectation under NautilusTrader's rules, and
  NautilusTrader's realised (seeded) fill rate falls within sampling noise of it.
- `d10_queue_p1`: fed OrderBookDepth10 (MBP-10 snapshots), NautilusTrader 1.x resets queue positions on every update,
  so `queue_position=True` behaves like the touch model. 2.x (checked on 2.0.0rc7, 2026-10-22) keeps the queue
  position across snapshots, capped at the new level size, so the depth feed fills like the delta feed. 2.x applies
  that cap on delta updates too. On the fixture it never moves a p = 1 fill, so the queue pairs still agree on every
  order (on the real Nasdaq days 99.6% to 99.9%, every difference from the cap), but it changes touch counts at
  p = 0.29, where the zoo matches the expectation under its own (1.x) rules and Nautilus's realised rate the
  expectation under Nautilus's rules.

Needs the compiled core (prepare/compare) and nautilus_trader (requires_nautilus; NT runs in its own interpreter).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUN = ROOT / "scripts" / "nt_crosscheck" / "run.py"


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory, nt_python: str) -> dict:
    import queuetruth as qt

    base = tmp_path_factory.mktemp("ntx")
    ds = base / "synth"
    qt.fixtures_synth(str(ds), venue="nasdaq", sessions=1)
    work = base / "work"
    subprocess.run(
        [
            sys.executable,
            str(RUN),
            "all",
            "--work",
            str(work),
            "--dataset",
            str(ds),
            "--n-shadows",
            "400",
            "--nt-python",
            nt_python,
            "--depth10",
            "--procs",
            "2",
        ],
        check=True,
        cwd=ROOT,
    )
    return json.loads((work / "report.json").read_text())


@pytest.mark.requires_core
@pytest.mark.requires_nautilus
def test_replicas_reproduce_both_sides(report: dict) -> None:
    val = report["replica_validation"]
    for name in ("zoo_touch", "nt_touch", "zoo_queue", "nt_queue"):
        assert val[name]["agree"] == val[name]["total"] > 0, (name, val[name])


@pytest.mark.requires_core
@pytest.mark.requires_nautilus
def test_touch_model_matches_nt_default(report: dict) -> None:
    assert report["discrepancy_classes"]["touch"] == {}
    for cname in ("touch_p1", "d10_touch_p1"):
        for h, hz in report["configs"][cname]["horizons"].items():
            assert hz["agree"] == 1.0, (cname, h, hz)
            assert hz["nt_bias_pp"] == pytest.approx(hz["zoo_bias_pp"]), (cname, h)


@pytest.mark.requires_core
@pytest.mark.requires_nautilus
def test_queue_position_matches_queue_models(report: dict) -> None:
    # the only rule difference left is a mid-window CLEAR, and no fixture window contains one
    assert report["discrepancy_classes"]["queue"] == {}
    pairs = [report["configs"]["queue_p1"], *report["extra_pairs"].values()]
    assert {p["zoo_model"] for p in pairs} == {
        "nt_queue_l2[mode=faithful]",
        "nt_queue_l2_prob_touch[mode=faithful;p=1]",
    }
    for p in pairs:
        for h, hz in p["horizons"].items():
            assert hz["agree"] == 1.0, (p["zoo_model"], h, hz)
            assert hz["nt_bias_pp"] == pytest.approx(hz["zoo_bias_pp"]), (p["zoo_model"], h)


@pytest.mark.requires_core
@pytest.mark.requires_nautilus
def test_depth10_input_queue_position(report: dict) -> None:
    if int(report["nautilus_trader"].split(".")[0]) >= 2:
        # 2.x keeps the queue position across depth snapshots: the depth feed fills like the delta feed
        for h, q in report["configs"]["d10_queue_p1"]["horizons"].items():
            assert q["nt_rate"] == report["configs"]["queue_p1"]["horizons"][h]["nt_rate"], h
        return
    q = report["configs"]["d10_queue_p1"]["horizons"]["60s"]
    t = report["configs"]["touch_p1"]["horizons"]["60s"]
    assert q["nt_rate"] > q["zoo_rate"]
    assert abs(q["nt_rate"] - t["nt_rate"]) < 0.03


@pytest.mark.requires_core
@pytest.mark.requires_nautilus
def test_low_p_matches_nt_rule_expectation(report: dict) -> None:
    n = report["n_shadows"]
    for cname in ("touch_p0.29", "queue_p0.29"):
        for h, hz in report["configs"][cname]["horizons"].items():
            e = hz["expect_nt_rules"]
            assert hz["zoo_rate"] == pytest.approx(hz["expect_zoo_rules"], abs=1e-9), (cname, h, hz)
            if int(report["nautilus_trader"].split(".")[0]) < 2:
                assert hz["zoo_rate"] == pytest.approx(e, abs=1e-9), (cname, h, hz)
            sd = max((e * (1 - e) / n) ** 0.5, 1 / n)
            assert abs(hz["nt_rate"] - e) <= 4 * sd + 1e-9, (cname, h, hz)
