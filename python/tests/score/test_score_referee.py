"""Referee comparison on constructed exact/sim tables (SPEC 5.8) and canonical simulator logs (sim:<name>)."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.score import compare_referee, run_referee
from queuetruth.score._io import read_parquet
from score.qt_score_helpers import T0, FakeRunner, make_dataset, make_table

S = 1_000_000_000


def exact():
    res = []
    fills = []
    spec = {"a": (10, T0 + S, "queue_depleted"), "b": (0, None, "never"), "c": (5, T0 + 2 * S, "trade_through")}
    for sid, (q, t, reason) in spec.items():
        res.append(
            {
                "shadow_id": sid,
                "instrument_id": 1,
                "side": 66,
                "price": 1,
                "qty": 10,
                "ts_submit": T0,
                "ts_live": T0,
                "filled_qty": q,
                "n_fills": 1 if q else 0,
                "ts_first_fill": t,
                "first_fill_reason": reason,
                "end_state": "filled" if q == 10 else "open_at_end",
                "latency_applied_ns": 0,
                "ambiguous_race": False,
                "out_of_session": False,
                "dq_flags": 0,
            }
        )
        if q:
            fills.append(
                {
                    "shadow_id": sid,
                    "fill_seq": 1,
                    "ts_event": t,
                    "sequence": 1,
                    "fill_qty": q,
                    "cum_qty": q,
                    "leaves_qty": 10 - q,
                    "fill_px": 1,
                    "reason": reason,
                    "queue_ahead_before": 0,
                    "traded_at_px_event": 0,
                    "trade_through_event": 0,
                    "behind_volume_executed": 0,
                }
            )
    return make_table("qt.shadow_results", res), make_table("qt.shadow_fills", fills)


def sim_fills(rows_):
    return pa.table(
        {"shadow_id": [r[0] for r in rows_], "fill_qty": [r[1] for r in rows_], "ts_event": [r[2] for r in rows_]}
    )


def test_identical_sim_passes():
    res, fl = exact()
    r = compare_referee(res, fl, fl, {})
    assert r.pass_ is True
    assert (r.n_orders, r.n_both_filled, r.qty_mismatch) == (3, 2, 0)
    assert r.discordant.model_dump() == {"sim_only": 0, "exact_only": 0}
    assert r.max_abs_ttf_err_ns == 0 and r.examples == []
    assert r.model_dump()["pass"] is True and r.simulator.name == "unknown"


def test_discordance_qty_and_ttf_errors():
    res, fl = exact()
    sim = sim_fills([("a", 3, T0 + S), ("a", 5, T0 + 3 * S), ("b", 2, T0 + 4 * S), ("c", 5, T0 + 2 * S + 7)])
    r = compare_referee(res, fl, sim, {"max_discordant": 0})
    assert r.discordant.sim_only == 1 and r.discordant.exact_only == 0
    assert r.qty_mismatch == 1  # a: 8 vs 10
    assert r.max_abs_ttf_err_ns == 7 and r.ttf_err_p50_ns == 4
    assert r.by_reason == {"never": 1, "queue_depleted": 1, "trade_through": 1}
    assert r.pass_ is False
    loose = compare_referee(res, fl, sim, {"max_discordant": 1, "max_qty_mismatch": 1, "max_abs_ttf_err_ns": 10})
    assert loose.pass_ is True


def test_unknown_sim_orders_count_as_sim_only():
    res, fl = exact()
    sim = sim_fills([("a", 10, T0 + S), ("c", 5, T0 + 2 * S), ("zz", 1, T0)])
    r = compare_referee(res, fl, sim, {})
    assert r.discordant.sim_only == 1 and r.by_reason == {"unknown_order": 1} and r.pass_ is False


def test_canonical_sim_log_with_order_key_alias_and_name(tmp_path):
    res, fl = exact()
    d = tmp_path / "simlog"
    d.mkdir()
    pq.write_table(pa.table({"order_key": ["K-a", "c"]}), d / "orders.parquet")
    pq.write_table(
        pa.table(
            {
                "order_key": ["K-a", "c"],
                "event_type": ["fill", "fill"],
                "qty": [10, 5],
                "px": [1, 1],
                "fill_id": ["1", "2"],
                "ts_exchange": [T0 + S, T0 + 2 * S],
                "source": ["sim:nautilus", "sim:nautilus"],
            }
        ),
        d / "events.parquet",
    )
    sched = pa.table({"shadow_id": ["a", "b", "c"], "order_key": ["K-a", "K-b", None]})
    r = compare_referee(res, fl, str(d), {}, schedule=sched)
    assert r.pass_ is True and r.simulator.name == "nautilus" and r.n_both_filled == 2


def test_run_referee_with_fake_engine(tmp_path):
    ds = make_dataset(tmp_path / "ds", [])
    sched = make_table(
        "qt.shadow_spec",
        [
            {
                "shadow_id": "s1",
                "instrument_id": 1,
                "side": 66,
                "price_mode": "abs",
                "price": 5,
                "offset_ticks": 0,
                "qty": 2,
                "ts_submit": T0,
                "latency_ns": 0,
                "source": "user",
            }
        ],
    )
    pq.write_table(sched, tmp_path / "schedule.parquet")
    pq.write_table(sim_fills([("s1", 2, T0 + 5)]), tmp_path / "sim.parquet")
    fr = FakeRunner(sim_fills={"s1": [(T0 + 5, 2, "queue_depleted")]})
    r = run_referee(
        {
            "dataset": {"path": str(ds)},
            "schedule": {"path": str(tmp_path / "schedule.parquet")},
            "sim_fills": {"path": str(tmp_path / "sim.parquet")},
            "simulator": {"name": "x", "version": "1"},
            "out": str(tmp_path / "out"),
        },
        runner=fr,
    )
    assert r.pass_ is True and r.simulator.version == "1"
    assert (tmp_path / "out" / "referee_report.json").exists()
    assert "out" not in r.spec
    assert read_parquet(tmp_path / "schedule.parquet").num_rows == 1
