"""Gate tests (gate-2, `-m integration`): O-SCORE-1, O-SCORE-2, O-SCORE-4 (audit A1 self-exclusion oracle) on
tests/fixtures/synth_small and O-REF-1 on tests/fixtures/referee, with the real engine (queuetruth.api / _core),
the real live-log package and the real statistics."""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

import queuetruth as qt
from queuetruth.score import build_scorecard, run_referee
from queuetruth.score._io import read_parquet, rows
from queuetruth.score.runner import DefaultRunner
from score.minisim import BUGS, simulate
from score.qt_score_helpers import write_live_log

pytestmark = [pytest.mark.integration, pytest.mark.requires_core]

MS = 1_000_000
TAKER = ("crossed", "crossed_at_live")


@pytest.fixture(scope="module")
def synth(repo_root) -> Path:
    p = repo_root / "tests" / "fixtures" / "synth_small"
    assert (p / "manifest.json").exists(), "tests/fixtures/synth_small missing (make fixtures)"
    return p


def _live_from_exact(results: list[dict], fills: list[dict], out: Path, venue_ids: dict[str, int] | None = None):
    """Canonical live log whose fills are the exact engine's (submit/ack at ts_submit, cancel at the effective
    cancel time, exchange times of kind match_time)."""
    by: dict[str, list[dict]] = {}
    for f in fills:
        by.setdefault(f["shadow_id"], []).append(f)
    orders, events = [], []
    for r in results:
        if r["price"] is None or r["ts_activated_event"] is None or str(r["end_state"]).startswith("rejected"):
            continue  # a rejected shadow never rested: not a live order (the engine reports its price since 1.1)
        sid = r["shadow_id"]
        orders.append(
            {
                "order_key": sid,
                "venue": "test",
                "account": "acct",
                "instrument": str(r["instrument_id"]),
                "instrument_id": r["instrument_id"],
                "venue_order_id": None if venue_ids is None else str(venue_ids[sid]),
                "side": "buy" if r["side"] == ord("B") else "sell",
                "order_type": "limit",
                "tif": "gtc",
                "post_only": False,
                "orig_qty": r["qty"],
                "qty_str": str(r["qty"]),
                "limit_px": r["price"],
                "price_exp": -9,
                "size_exp": 0,
                "validation_status": "fixture_only",
            }
        )
        ev = [
            {"event_type": "submit_sent", "ts_local_send": r["ts_submit"] - MS},
            {"event_type": "ack", "ts_exchange": r["ts_submit"], "ts_local_recv": r["ts_submit"] + MS},
        ]
        for f in sorted(by.get(sid, []), key=lambda f: f["fill_seq"]):
            ev.append(
                {
                    "event_type": "fill",
                    "ts_exchange": f["ts_event"],
                    "qty": f["fill_qty"],
                    "px": f["fill_px"],
                    "fill_id": f"{sid}-{f['fill_seq']}",
                    # the engine's own label (qt.shadow_fills 1.1 `liquidity`; A5 as refined: a resting order later
                    # crossed is a maker fill); v0.1 files: from the reason
                    "liquidity": f.get("liquidity") or ("taker" if f["reason"] in TAKER else "maker"),
                }
            )
        if r["end_state"] == "cancelled" and r["ts_cancel_effective"] is not None:
            ev.append({"event_type": "cancel_sent", "ts_local_send": r["ts_cancel_effective"]})
            ev.append({"event_type": "cancel_ack", "ts_exchange": r["ts_cancel_effective"]})
        for i, e in enumerate(ev, start=1):
            e.update(
                order_key=sid,
                event_seq=i,
                ts_exchange_kind="match_time",
                ts_precision="ns",
                source="synthetic",
                derived=False,
            )
        events += ev
    return write_live_log(out, orders, events)


@pytest.fixture(scope="module")
def exact_live(synth, tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("oscore")
    qt.sample_shadows(
        {
            "dataset": {"path": str(synth)},
            "out": str(tmp / "shadows.parquet"),
            "n": 400,
            "seed": 11,
            "lifetime_ns": 30_000_000_000,
        }
    )
    qt.replay(
        {"dataset": {"path": str(synth)}, "out": str(tmp / "exact"), "shadows": {"path": str(tmp / "shadows.parquet")}}
    )
    res = rows(read_parquet(tmp / "exact" / "shadow_results.parquet"))
    fl = rows(read_parquet(tmp / "exact" / "shadow_fills.parquet"))
    return _live_from_exact(res, fl, tmp / "live")


def test_o_score_1_exact_vs_exact(synth, exact_live, tmp_path):
    s = build_scorecard(
        {
            "live": {"path": str(exact_live)},
            "sim": {"kind": "exact"},
            "dataset": {"path": str(synth)},
            "out": str(tmp_path / "out"),
            "bootstrap": {"b": 200},
            "perm_b": 200,
        },
        self_exclude=False,
    )
    assert s.n_matched > 50
    assert s.two_by_two.live_only == 0 and s.two_by_two.sim_only == 0
    assert s.bias.fill_rate_bias == 0.0 and s.bias.ci_lo <= 0.0 <= s.bias.ci_hi
    assert s.equivalence.fill_rate_equivalent is True
    so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))
    assert all(r["live_filled_qty"] == r["sim_filled_qty"] for r in so)
    assert all(r["ttf_live_ns"] == r["ttf_sim_ns"] for r in so)
    # audit A1 oracle with the default self-exclusion: shadows are not in the tape, so nothing may be excluded and
    # exact vs exact stays at zero discordance
    d = build_scorecard(
        {
            "live": {"path": str(exact_live)},
            "sim": {"kind": "exact"},
            "dataset": {"path": str(synth)},
            "bootstrap": {"b": 50},
            "perm_b": 50,
        }
    )
    assert d.n_matched == s.n_matched and d.two_by_two == s.two_by_two


def test_o_score_2_front_of_queue_overfills(synth, exact_live):
    s = build_scorecard(
        {
            "live": {"path": str(exact_live)},
            "sim": {"kind": "zoo", "model": "front_of_queue"},
            "dataset": {"path": str(synth)},
            "bootstrap": {"b": 200},
            "perm_b": 200,
        },
        self_exclude=False,
    )
    assert s.bias.direction == "sim_overfills"
    assert s.bias.ci_lo > 0
    assert s.two_by_two.sim_only > s.two_by_two.live_only


def test_zoo_arm_writes_traced_model_trajectory(synth, exact_live, tmp_path):
    """A zoo-arm scorecard writes `zoo_trajectory.parquet` for the orders the report traces
    (sim-only, live-only, largest time-to-fill gap), with the sim model's estimate next to the exact queue."""
    import pyarrow.parquet as pq

    from queuetruth.score._io import rows
    from queuetruth.score.scorecard import TRACED_ORDERS, traced_order_keys

    out = tmp_path / "sc"
    s = build_scorecard(
        {
            "live": {"path": str(exact_live)},
            "sim": {"kind": "zoo", "model": "front_of_queue"},
            "dataset": {"path": str(synth)},
            "out": str(out),
            "bootstrap": {"b": 50},
            "perm_b": 50,
        },
        self_exclude=False,
    )
    assert any(f.name == "zoo_trajectory.parquet" for f in s.files)
    orders = rows(pq.read_table(out / "score_orders.parquet"))
    keys = traced_order_keys(orders)
    assert 0 < len(keys) <= TRACED_ORDERS
    sched = rows(pq.read_table(out / "schedule.parquet"))
    legs = {str(r["shadow_id"]) for r in sched if str(r["order_key"]) in set(keys)}
    traj = rows(pq.read_table(out / "zoo_trajectory.parquet"))
    assert traj and {str(r["shadow_id"]) for r in traj} <= legs
    assert {r["model_id"] for r in traj} == {s.sim_source.model_id}


def test_o_score_4_self_exclusion_real_orders(synth, tmp_path):
    """Audit A1 oracle: live orders that ARE in the tape (real MBO lives, venue order id known) scored against the
    exact arm with self-exclusion give zero discordance."""
    qt.replay({"dataset": {"path": str(synth)}, "out": str(tmp_path / "r"), "real_orders": True, "bbo": "none"})
    lives = [
        r
        for r in rows(read_parquet(tmp_path / "r" / "real_orders.parquet"))
        if not r["in_snapshot"] and r["n_reduces"] == 0 and r["end_reason"] in ("filled", "cancelled")
    ]
    lives = sorted(lives, key=lambda r: (r["ts_add"], r["order_id"]))[::7][:150]
    assert len(lives) >= 30
    want = {(r["instrument_id"], r["order_id"]) for r in lives}
    fills: dict[tuple[int, int], list[dict]] = {}
    for f in sorted(Path(synth, "mbo").rglob("*.parquet")):
        for rec in rows(pq.read_table(f)):
            if rec["action"] == ord("F") and (rec["instrument_id"], rec["order_id"]) in want:
                fills.setdefault((rec["instrument_id"], rec["order_id"]), []).append(rec)
    results, fl, ids = [], [], {}
    for r in lives:
        sid = f"r{r['instrument_id']}_{r['order_id']}"
        ids[sid] = r["order_id"]
        results.append(
            {
                "shadow_id": sid,
                "instrument_id": r["instrument_id"],
                "side": r["side"],
                "price": r["price"],
                "qty": r["size_at_add"],
                "ts_submit": r["ts_add"],
                "ts_activated_event": r["ts_add"],
                "end_state": "cancelled" if r["end_reason"] == "cancelled" else "filled",
                "ts_cancel_effective": r["ts_end"],
            }
        )
        for k, f in enumerate(fills.get((r["instrument_id"], r["order_id"]), []), start=1):
            fl.append(
                {
                    "shadow_id": sid,
                    "fill_seq": k,
                    "ts_event": f["ts_event"],
                    "fill_qty": f["size"],
                    "fill_px": r["price"],
                    "reason": "queue_depleted",
                }
            )
    live = _live_from_exact(results, fl, tmp_path / "live", ids)
    s = build_scorecard(
        {
            "live": {"path": str(live)},
            "sim": {"kind": "exact"},
            "dataset": {"path": str(synth)},
            "bootstrap": {"b": 100},
            "perm_b": 100,
        }
    )
    assert any("matched" in w and "(100.0%" in w for w in s.warnings), s.warnings
    assert s.two_by_two.sim_only == 0 and s.two_by_two.live_only == 0
    no_excl = build_scorecard(
        {
            "live": {"path": str(live)},
            "sim": {"kind": "exact"},
            "dataset": {"path": str(synth)},
            "bootstrap": {"b": 100},
            "perm_b": 100,
        },
        self_exclude=False,
    )
    assert no_excl.two_by_two.live_only > 0  # behind its own twin, the sim underfills
    # regression: the filtered-copy fallback loses the twins' executions (live_only = 12 before the native fix)
    fb = build_scorecard(
        {
            "live": {"path": str(live)},
            "sim": {"kind": "exact"},
            "dataset": {"path": str(synth)},
            "bootstrap": {"b": 20},
            "perm_b": 20,
        },
        runner=_FallbackRunner(),
    )
    assert fb.two_by_two.live_only > 0 and fb.two_by_two.sim_only == 0


class _FallbackRunner(DefaultRunner):
    native_replay_options = False


def test_o_score_4_own_orders_fixture(synth, tmp_path):
    """synth_small/own_orders (testkit A1 input): sim = exact with self-exclusion reproduces every live fill."""
    s = build_scorecard(
        {
            "live": {"path": str(synth / "own_orders" / "live")},
            "sim": {"kind": "exact"},
            "dataset": {"path": str(synth)},
            "out": str(tmp_path / "out"),
            "bootstrap": {"b": 50},
            "perm_b": 50,
        }
    )
    assert any("(100.0%" in w for w in s.warnings), s.warnings
    assert s.n_matched == 200 and s.two_by_two.live_only == 0 and s.two_by_two.sim_only == 0
    so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))
    assert all(r["live_filled_qty"] == r["sim_filled_qty"] and r["ttf_live_ns"] == r["ttf_sim_ns"] for r in so)


# SPEC 5.8 referee classes (fixture dir `NN_<slug>`) -> the mini-simulator bug that class must catch.
CLASS_BUG = {
    "01_unrelated_delete_keeps_position": "unrelated_delete_to_front",
    "02_level_shrink_caps_queue": "no_cap_on_shrink",
    "03_distinct_trades_each_consume": "one_trade_per_event",
    "04_post_trade_book_decides": "pre_trade_book",
    "05_partial_fills_sum_to_cum": "last_chunk_only",
    "06_fill_equal_to_leaves_completes": "exact_leaves_dropped",
    "07_size_increase_loses_priority": "upsize_keeps_priority",
    "08_price_change_loses_priority": "reprice_keeps_priority",
    "09_hidden_print_no_queue": "hidden_consumes_queue",
    "10_trade_through_fills": "no_trade_through",
    "11_fill_before_cancel_effective_stands": "cancel_at_send",
    "12_hidden_execution_at_price_after_queue": "ignore_hidden_prints",
    "13_execution_without_print": "print_driven_queue",
    "14_off_price_print_not_trade_through": "off_price_print_trade_through",
    "15_cme_same_size_modify_loses_priority": "cme_modify_keeps_priority",
    "16_crossed_at_live_takes_opposite": "crossed_fills_at_limit",
}
N_CLASSES = len(CLASS_BUG)


def _bug_for(d: Path) -> str:
    assert d.name in CLASS_BUG, f"referee fixture {d.name} is not a SPEC 5.8 class"
    return CLASS_BUG[d.name]


def _ref_dirs(repo_root: Path) -> list[Path]:
    root = repo_root / "tests" / "fixtures" / "referee"
    return sorted(p for p in root.iterdir() if (p / "schedule.parquet").exists()) if root.exists() else []


def test_o_ref_1_fixture_set_complete(repo_root):
    dirs = _ref_dirs(repo_root)
    assert [d.name for d in dirs] == sorted(CLASS_BUG)
    assert sorted(_bug_for(d) for d in dirs) == sorted(BUGS)


@pytest.mark.parametrize("idx", range(N_CLASSES))
def test_o_ref_1_referee(idx, repo_root, tmp_path):
    dirs = _ref_dirs(repo_root)
    assert len(dirs) == N_CLASSES
    d = dirs[idx]
    ds = d / "dataset"
    base = {
        "dataset": {"path": str(ds)},
        "schedule": {"path": str(d / "schedule.parquet")},
        "simulator": {"name": "exact"},
    }
    ok = run_referee({**base, "sim_fills": {"path": str(d / "expected_fills.parquet")}})
    assert ok.pass_ is True, ok.model_dump()
    sched = rows(read_parquet(d / "schedule.parquet"))
    clean = simulate(ds, sched)
    pq.write_table(clean, tmp_path / "clean.parquet")
    r_clean = run_referee({**base, "sim_fills": {"path": str(tmp_path / "clean.parquet")}})
    assert r_clean.pass_ is True, f"mini-sim without bugs disagrees on {d.name}: {r_clean.model_dump()}"
    bug = _bug_for(d)
    pq.write_table(simulate(ds, sched, {bug}), tmp_path / "buggy.parquet")
    r_bug = run_referee(
        {**base, "sim_fills": {"path": str(tmp_path / "buggy.parquet")}, "simulator": {"name": f"minisim:{bug}"}}
    )
    assert r_bug.pass_ is False, f"{d.name}: bug {bug} not detected"
