"""The strategy written by `queuetruth export replay-schedule --target nautilus`, run in a real `BacktestEngine` of
the installed nautilus_trader (1.x or 2.x) and read back with `queuetruth.live.import_sim("nautilus")`. Needs
queuetruth and nautilus_trader in the same venv; skips without either."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")
replay = pytest.importorskip("queuetruth.export.replay")
qlive = pytest.importorskip("queuetruth.live")

from ntkit import T0, run_strategy  # noqa: E402

from queuetruth_nautilus import compat  # noqa: E402

S = 1_000_000_000
PX = 100_000_000_000  # 100.00 at price_exp -9
TICK = 10_000_000  # 0.01
UNITS = {1: replay.Units(-9, 0, "0.01", "1", "QTX")}


def leg(sid: str, side: str, px: int, qty: int, t: int, cancel: int | None = None) -> dict[str, Any]:
    return {
        "shadow_id": sid,
        "instrument_id": 1,
        "side": ord(side),
        "price_mode": "abs",
        "price": px,
        "offset_ticks": 0,
        "qty": qty,
        "ts_submit": t,
        "latency_ns": 0,
        "ts_cancel": cancel,
        "group": None,
        "source": "live_log",
        "order_key": sid.split(".leg")[0],
    }


#: o1 buy 5 @ 100.00 from +1 s, cancel at +20 s; o2 sell 3 @ 100.03 from +2 s, amended to 2 at +4 s, never filled;
#: o3 buy 2 @ 99.98 from +3 s, re-priced to 100.00 at +6 s; o4 buy 1 @ 99.97 from +3 s, cancelled at +7 s.
LEGS = [
    leg("t:a:o1", "B", PX, 5, T0 + 1 * S, cancel=T0 + 20 * S),
    leg("t:a:o2", "A", PX + 3 * TICK, 3, T0 + 2 * S),
    leg("t:a:o3", "B", PX - 2 * TICK, 2, T0 + 3 * S, cancel=T0 + 6 * S),
    leg("t:a:o3.leg2", "B", PX, 2, T0 + 6 * S),
    leg("t:a:o4", "B", PX - 3 * TICK, 1, T0 + 3 * S, cancel=T0 + 7 * S),
]
AMENDS = [{"shadow_id": "t:a:o2", "ts_send": T0 + 4 * S, "new_qty": 2}]
#: Trade-throughs (seller-aggressed prints below the order's price): 99.99 x 50 at +5 s fills o1 (5 @ 100.00), the
#: cancel of o1 at +20 s is then skipped; 99.99 x 50 at +8 s fills o3 after its re-price to 100.00. The last print
#: (100.00 x 1 at +21 s, no open buy left) runs the engine's clock past the last action.
PRINTS = [(T0 + 5 * S, 9_999, 50), (T0 + 8 * S, 9_999, 50), (T0 + 21 * S, 10_000, 1)]


def load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("qt_replay_strategy_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_replay_strategy_runs_in_backtest_engine_and_imports(tmp_path):
    out = tmp_path / "qt_replay_strategy.py"
    res = replay.replay_schedule(
        "nautilus", out, schedule=LEGS, amends=AMENDS, units=UNITS, instrument_ids={1: "QTX.XNAS"}
    )
    assert res.n_orders == 4
    mod = load(out)
    events = tmp_path / "nt_events.jsonl"
    strat = mod.ScheduleReplayStrategy(mod.ScheduleReplayConfig(events_path=str(events), order_id_tag="001"))
    run_strategy(strat, {"book_type": compat.book_type_l2()}, PRINTS)

    summary = json.loads(Path(str(events) + ".summary.json").read_text())
    assert summary == {"actions_done": 8, "actions": 8, "skipped": {"cancel_order_not_open": 1}}
    rows = [json.loads(x) for x in events.read_text().splitlines()]
    kinds = {(r["client_order_id"], r["event_type"]) for r in rows}
    for key in ("t:a:o1", "t:a:o2", "t:a:o3", "t:a:o4"):
        assert (key, "OrderInitialized") in kinds and (key, "OrderAccepted") in kinds
    assert ("t:a:o1", "OrderFilled") in kinds and ("t:a:o3", "OrderFilled") in kinds
    assert ("t:a:o2", "OrderUpdated") in kinds and ("t:a:o3", "OrderUpdated") in kinds
    assert ("t:a:o4", "OrderCanceled") in kinds
    assert ("t:a:o2", "OrderFilled") not in kinds and ("t:a:o1", "OrderCanceled") not in kinds
    fills = {r["client_order_id"]: r for r in rows if r["event_type"] == "OrderFilled"}
    assert fills["t:a:o1"]["order_side"] == "BUY" and fills["t:a:o1"]["liquidity_side"] == "MAKER"
    assert float(fills["t:a:o1"]["last_qty"]) == 5 and float(fills["t:a:o1"]["last_px"]) == 100.0
    assert float(fills["t:a:o3"]["last_qty"]) == 2 and float(fills["t:a:o3"]["last_px"]) == 100.0
    updated = [r for r in rows if r["event_type"] == "OrderUpdated" and r["client_order_id"] == "t:a:o3"]
    assert float(updated[0]["price"]) == 100.0

    s = qlive.import_sim("nautilus", [str(events)], str(tmp_path / "nt-sim"), account="a")
    assert s.n_orders == 4 and s.n_fills == 2
    orders, evs = qlive.read_log(str(tmp_path / "nt-sim"))
    assert sorted(orders.column("order_key").to_pylist()) == ["t:a:o1", "t:a:o2", "t:a:o3", "t:a:o4"]
    seen = {(r["order_key"], r["event_type"]) for r in evs.to_pylist()}
    assert ("t:a:o1", "fill") in seen and ("t:a:o3", "fill") in seen
    assert ("t:a:o2", "amend_ack") in seen and ("t:a:o3", "amend_ack") in seen
    assert ("t:a:o4", "cancel_ack") in seen
