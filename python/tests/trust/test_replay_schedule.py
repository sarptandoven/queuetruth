"""Schedule-replay adapters (trader review F11): schedule -> actions, units, the generated hftbacktest script (run end
to end in a separate hftbacktest 2.4.4 venv when one exists) and the generated NautilusTrader strategy (driven by a stub
of the documented NT API, since nautilus_trader is not installed and its code is LGPL); both outputs are imported
with `queuetruth.live.import_sim`."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from score.qt_score_helpers import T0, log_from, order_events

from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.export import hftbacktest_replay_script, nautilus_replay_strategy, replay_schedule
from queuetruth.export.hftbacktest import hftbacktest_snippet
from queuetruth.export.replay import Units, read_schedule, resolve_units, scaled, schedule_actions
from queuetruth.schemas.io import write_table
from queuetruth.schemas.models import CalibrationResult
from queuetruth.score._io import make_table
from queuetruth.score._io import rows as table_rows

S = 1_000_000_000
MS = 1_000_000
PX = 100_000_000_000  # 100.00 at price_exp -9
TICK = 10_000_000  # 0.01
UNITS = {1: Units(-9, 0, "0.01", "1", "SYM1")}


def leg(sid, side, px, qty, t, cancel=None, key=None):
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
        "order_key": key or sid.split(".leg")[0],
    }


#: o1 buy 5 @ 100.00 cancelled at +20 s; o2 sell 3 @ 100.03 with a priority-keeping amend to 2 at +4 s;
#: o3 buy 2 @ 99.99 re-priced to 100.00 at +6 s (leg 2), never cancelled.
LEGS = [
    leg("test:acct:o1", "B", PX, 5, T0 + 1 * S, cancel=T0 + 20 * S),
    leg("test:acct:o2", "A", PX + 3 * TICK, 3, T0 + 2 * S),
    leg("test:acct:o3", "B", PX - TICK, 2, T0 + 3 * S, cancel=T0 + 6 * S),
    leg("test:acct:o3.leg2", "B", PX, 2, T0 + 6 * S),
]
AMENDS = [{"shadow_id": "test:acct:o2", "ts_send": T0 + 4 * S, "new_qty": 2}]


def test_schedule_actions_one_order_per_live_order():
    acts = schedule_actions(LEGS, AMENDS, post_only={"test:acct:o2": True})
    got = [(a.ts - T0, a.kind, a.order_key, a.price, a.qty, a.post_only) for a in acts]
    assert got == [
        (1 * S, "submit", "test:acct:o1", PX, 5, False),
        (2 * S, "submit", "test:acct:o2", PX + 3 * TICK, 3, True),
        (3 * S, "submit", "test:acct:o3", PX - TICK, 2, False),
        (4 * S, "modify", "test:acct:o2", PX + 3 * TICK, 2, True),
        (6 * S, "modify", "test:acct:o3", PX, 2, False),
        (20 * S, "cancel", "test:acct:o1", PX, 5, False),
    ]
    assert acts[4].leg == "test:acct:o3.leg2"


def test_schedule_actions_refusals():
    gap = [leg("k:a:1", "B", PX, 1, T0, cancel=T0 + S), leg("k:a:1.leg2", "B", PX, 1, T0 + 2 * S)]
    with pytest.raises(QtError) as e:
        schedule_actions(gap)
    assert e.value.code == "QT-INPUT-002" and "contiguous legs" in e.value.message
    touch = [{**leg("k:a:2", "B", PX, 1, T0), "price_mode": "touch", "price": None}]
    with pytest.raises(QtError) as e:
        schedule_actions(touch)
    assert "absolute prices" in e.value.message
    with pytest.raises(QtError):
        schedule_actions(LEGS, [{"shadow_id": "nope", "ts_send": T0, "new_qty": 1}])


def test_units_and_scaling(tmp_path):
    assert scaled(PX, -9) == "100" and scaled(PX + 3 * TICK, -9) == "100.03" and scaled(5, -8) == "0.00000005"
    with pytest.raises(QtError) as e:
        resolve_units([1])
    assert e.value.code == "QT-CONFIG-001"
    o, ev = order_events("test:acct:o1", "buy", PX, 5, T0)
    live = log_from([(o, ev)], tmp_path / "live")
    u = resolve_units([1], live=str(live))
    assert u[1].price_exp == -9 and u[1].size_exp == 0 and u[1].symbol == "SYM1" and u[1].tick_size is None
    u = resolve_units([1], live=str(live), units={1: {"tick_size": "0.01", "lot_size": "1"}})
    assert (u[1].tick_size, u[1].lot_size) == ("0.01", "1")


def _literal(text: str, name: str):
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def test_hftbacktest_script_structure(tmp_path):
    sched = tmp_path / "schedule.parquet"
    write_table(make_table("qt.shadow_spec", LEGS), sched)
    write_table(make_table("qt.shadow_amend", AMENDS), tmp_path / "schedule_amends.parquet")
    legs, amends = read_schedule(sched)
    assert len(legs) == 4 and len(amends) == 1
    text = hftbacktest_replay_script(sched, units=UNITS)
    compile(text, "hbt_replay.py", "exec")
    acts = _literal(text, "ACTIONS")
    assert acts[0] == (T0 + S, "submit", "test:acct:o1", 1, "B", "100", "5", False)
    assert [a[1] for a in acts] == ["submit", "submit", "submit", "modify", "modify", "cancel"]
    assert _literal(text, "INSTRUMENTS")[1]["tick_size"] == "0.01"
    assert "def configure_asset(asset):" in text and "return None" in text
    r = replay_schedule("hftbacktest", tmp_path / "out" / "hbt_replay.py", schedule=sched, units=UNITS)
    assert (r.n_orders, r.n_actions) == (3, 6) and Path(r.path).read_text() == text.replace(
        "python hbt_replay.py", "python hbt_replay.py"
    )
    with pytest.raises(QtError):
        replay_schedule("zipline", tmp_path / "x.py", schedule=sched, units=UNITS)  # type: ignore[arg-type]


# ---- NautilusTrader: the generated strategy driven by a stub of the documented API --------------------------------


class _Ev:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _event_class(name):
    return type(name, (_Ev,), {})


class _Enum:
    def __init__(self, name):
        self.name = name


def _stub_nautilus(monkeypatch, fills):
    """Minimal stand-ins for the NT names the generated module uses; the 'venue' accepts and fills orders."""
    mods = {
        n: types.ModuleType(n)
        for n in (
            "nautilus_trader",
            "nautilus_trader.config",
            "nautilus_trader.model",
            "nautilus_trader.model.enums",
            "nautilus_trader.model.identifiers",
            "nautilus_trader.trading",
            "nautilus_trader.trading.strategy",
        )
    }

    class StrategyConfig:
        def __init_subclass__(cls, frozen=False, **kw):
            super().__init_subclass__(**kw)

        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class InstrumentId:
        def __init__(self, value):
            self.value = value

        @staticmethod
        def from_str(s):
            return InstrumentId(s)

        def __str__(self):
            return self.value

    class Instrument:
        def make_qty(self, v):
            return v

        def make_price(self, v):
            return v

    names = ["OrderSubmitted", "OrderAccepted", "OrderFilled", "OrderUpdated", "OrderCanceled", "OrderInitialized"]
    ev = {n: _event_class(n) for n in names}

    class Clock:
        def __init__(self):
            self.now = 0
            self.alerts = []

        def timestamp_ns(self):
            return self.now

        def set_time_alert_ns(self, name, alert_time_ns, callback):
            self.alerts.append((alert_time_ns, name, callback))

    class Strategy:
        def __init__(self, config):
            self.config = config
            self.clock = Clock()
            self.orders = {}
            self.n = 0
            strat = self

            class Cache:
                def instrument(self, iid):
                    return Instrument()

                def order(self, cid):
                    return strat.orders.get(cid.value)

            class Factory:
                def limit(self, instrument_id, order_side, quantity, price, time_in_force, post_only):
                    strat.n += 1
                    cid = _Ev(value=f"O-{strat.n}")
                    return _Ev(
                        client_order_id=cid, side=order_side, quantity=quantity, price=price, is_open=False, filled=0
                    )

            self.cache = Cache()
            self.order_factory = Factory()

        def _emit(self, name, order, **kw):
            self.on_order_event(ev[name](client_order_id=order.client_order_id, ts_event=self.clock.now, **kw))

        def submit_order(self, order):
            self.orders[order.client_order_id.value] = order
            self.on_order_event(ev["OrderInitialized"](client_order_id=order.client_order_id, ts_event=self.clock.now))
            self._emit("OrderSubmitted", order)
            order.is_open = True
            self._emit("OrderAccepted", order, venue_order_id=f"V{order.client_order_id.value}")

        def modify_order(self, order, quantity=None, price=None):
            order.quantity, order.price = quantity, price
            self._emit("OrderUpdated", order, quantity=quantity, price=price, venue_order_id=None)

        def cancel_order(self, order):
            order.is_open = False
            self._emit("OrderCanceled", order)

        def fill(self, cid, qty, px):
            order = self.orders[cid]
            order.filled += int(qty)
            if order.filled >= int(order.quantity):
                order.is_open = False
            self._emit(
                "OrderFilled",
                order,
                trade_id=f"T{self.clock.now}",
                order_side=order.side,
                last_qty=qty,
                last_px=px,
                liquidity_side=_Enum("MAKER"),
                commission="0.01 USD",
            )

    mods["nautilus_trader.config"].StrategyConfig = StrategyConfig
    mods["nautilus_trader.model.enums"].OrderSide = types.SimpleNamespace(BUY=_Enum("BUY"), SELL=_Enum("SELL"))
    mods["nautilus_trader.model.enums"].TimeInForce = types.SimpleNamespace(GTC=_Enum("GTC"))
    mods["nautilus_trader.model.identifiers"].InstrumentId = InstrumentId
    mods["nautilus_trader.trading.strategy"].Strategy = Strategy
    for n, m in mods.items():
        monkeypatch.setitem(sys.modules, n, m)
    return Strategy


def test_nautilus_strategy_replays_and_imports(tmp_path, monkeypatch):
    text = nautilus_replay_strategy(LEGS, amends=AMENDS, units=UNITS, instrument_ids={1: "SYM1.SIM"})
    assert "from nautilus_trader.trading.strategy import Strategy" in text
    assert "class ScheduleReplayStrategy(Strategy):" in text and "def on_order_event(self, event)" in text
    assert _literal(text, "INSTRUMENT_IDS") == {"1": "SYM1.SIM"}
    _stub_nautilus(monkeypatch, None)
    mod = types.ModuleType("qt_replay_strategy")
    exec(compile(text, "qt_replay_strategy.py", "exec"), mod.__dict__)
    events_path = tmp_path / "nt_events.jsonl"
    strat = mod.ScheduleReplayStrategy(mod.ScheduleReplayConfig(events_path=str(events_path)))
    strat.clock.now = T0
    strat.on_start()
    # The venue fills o1 (2 of 5) at +5 s and o3 (2, after its re-price) at +8 s.
    venue_fills = {T0 + 5 * S: ("test:acct:o1", "2", "100"), T0 + 8 * S: ("test:acct:o3", "2", "100")}
    while strat.clock.alerts or venue_fills:
        nxt = min([a[0] for a in strat.clock.alerts] + list(venue_fills))
        strat.clock.now = nxt
        if nxt in venue_fills:
            key, q, px = venue_fills.pop(nxt)
            strat.fill(strat._cid[key].value, q, px)
        due = [a for a in strat.clock.alerts if a[0] <= nxt]
        strat.clock.alerts = [a for a in strat.clock.alerts if a[0] > nxt]
        for _, _, cb in due:
            cb(None)
    strat.on_stop()
    rows = [json.loads(x) for x in events_path.read_text().splitlines()]
    assert {r["client_order_id"] for r in rows} == {"test:acct:o1", "test:acct:o2", "test:acct:o3"}
    assert json.loads(Path(str(events_path) + ".summary.json").read_text()) == {
        "actions_done": 6,
        "actions": 6,
        "skipped": {},
    }
    s = qlive.import_sim("nautilus", [str(events_path)], tmp_path / "nt-sim", account="acct")
    assert s.n_orders == 3 and s.n_fills == 2
    orders, events = qlive.read_log(str(tmp_path / "nt-sim"))
    keys = sorted(orders.column("order_key").to_pylist())
    assert keys == ["test:acct:o1", "test:acct:o2", "test:acct:o3"]
    evs = [(r["order_key"], r["event_type"]) for r in events.to_pylist()]
    assert ("test:acct:o2", "amend_ack") in evs and ("test:acct:o3", "amend_ack") in evs
    assert ("test:acct:o1", "cancel_ack") in evs and ("test:acct:o1", "fill") in evs


# ---- hftbacktest 2.4.4 end to end (separate venv) --------------------------------------------------------------------

EVENT_DTYPE = np.dtype(
    [
        ("ev", "<u8"),
        ("exch_ts", "<i8"),
        ("local_ts", "<i8"),
        ("px", "<f8"),
        ("qty", "<f8"),
        ("order_id", "<u8"),
        ("ival", "<i8"),
        ("fval", "<f8"),
    ],
    align=True,
)
EXCH, LOCAL, BUY, SELL = 1 << 31, 1 << 30, 1 << 29, 1 << 28


def _hbt_python(repo_root: Path) -> Path | None:
    env = os.environ.get("QT_HBT_PYTHON")
    if env:
        return Path(env)
    for name in ("hbt-venv", "w3trust-hbt"):
        p = repo_root / ".scratch" / name / "bin" / "python"
        if p.exists():
            return p
    return None


def _hbt_data(path: Path) -> None:
    """Bid 100.00 x 10 and ask 100.01 x 10 every 10 ms for 40 s from T0; from +5 s a 4-lot sell trade at 100.00
    every 500 ms (so resting bids at 100.00 work through the queue and fill)."""
    rows = []
    for i in range(4000):
        ts = T0 + i * 10 * MS
        rows.append((1 | BUY | EXCH | LOCAL, ts, ts + 1000, 100.0, 10.0, 0, 0, 0.0))
        rows.append((1 | SELL | EXCH | LOCAL, ts, ts + 1000, 100.01, 10.0, 0, 0, 0.0))
        if ts >= T0 + 5 * S and i % 50 == 0:
            rows.append((2 | SELL | EXCH | LOCAL, ts + 1, ts + 1001, 100.0, 4.0, 0, 0, 0.0))
    np.savez_compressed(path, data=np.array(rows, dtype=EVENT_DTYPE))


def test_hftbacktest_replay_end_to_end(tmp_path, repo_root):
    """Generated script -> hftbacktest 2.4.4 (power3 n = 2 via a `queuetruth export hftbacktest` snippet) ->
    order snapshots -> `import_sim("hftbacktest")` -> one canonical order per live order with its fills, amends and
    cancel."""
    py = _hbt_python(repo_root)
    if py is None:
        pytest.skip("hftbacktest venv missing (run `make hbt-crosscheck` or set QT_HBT_PYTHON)")
    sched = tmp_path / "schedule.parquet"
    write_table(make_table("qt.shadow_spec", LEGS), sched)
    write_table(make_table("qt.shadow_amend", AMENDS), tmp_path / "schedule_amends.parquet")
    script = tmp_path / "hbt_replay.py"
    replay_schedule("hftbacktest", script, schedule=sched, units=UNITS, step_ns=10 * MS, window_ns=30 * S)
    _hbt_data(tmp_path / "data.npz")
    cal = CalibrationResult.model_validate(
        {
            "run_id": "0" * 16,
            "spec": {"exchange": "partial"},
            "kind": "alloc",
            "family": "power3",
            "mode": "faithful",
            "param_name": "n",
            "estimate": 2.0,
            "n_events": 0,
            "n_shadows": 0,
            "train": {},
            "test": {},
            "held_out": {},
            "baseline_held_out": {},
            "stratified": [],
            "exports": {},
            "data_sources": [],
            "warnings": [],
        }
    )
    # no_partial_fill_exchange: hftbacktest 2.4.4 raises InvalidOrderStatus on some partial fills (#312/#316),
    # which the script reports as a fatal error (test_hftbacktest_replay_reports_fatal_error).
    (tmp_path / "snippet.py").write_text(
        hftbacktest_snippet(cal).replace(".partial_fill_exchange()", ".no_partial_fill_exchange()")
    )
    out = tmp_path / "sim_orders.jsonl"
    r = subprocess.run(
        [
            str(py),
            "-I",
            str(script),
            "--data",
            f"1={tmp_path / 'data.npz'}",
            "--snippet",
            str(tmp_path / "snippet.py"),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr[-3000:]
    summary = json.loads(Path(str(out) + ".summary.json").read_text())
    # o1 fills completely at +6 s, before its cancel at +20 s (skipped: a venue would reject it); o2 (an ask nobody
    # buys) is cancelled by the replay 30 s after entry; o3 fills after its re-price.
    assert (summary["submitted"], summary["modified"], summary["cancelled"], summary["window_cancels"]) == (3, 2, 0, 1)
    assert summary["skipped"] == {"cancel_order_not_open": 1} and summary["errors"] == {} and "fatal" not in summary
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert {x["order_key"] for x in rows} == {"test:acct:o1", "test:acct:o2", "test:acct:o3"}
    s = qlive.import_sim("hftbacktest", [str(out)], tmp_path / "hbt-sim", account="acct")
    assert s.n_orders == 3 and s.n_fills == 2
    orders, events = qlive.read_log(str(tmp_path / "hbt-sim"))
    assert sorted(orders.column("order_key").to_pylist()) == ["test:acct:o1", "test:acct:o2", "test:acct:o3"]
    ev = table_rows(events)
    kinds = {(e["order_key"], e["event_type"]) for e in ev}
    assert {("test:acct:o2", "amend_ack"), ("test:acct:o3", "amend_ack"), ("test:acct:o2", "cancel_ack")} <= kinds
    assert ("test:acct:o1", "cancel_ack") not in kinds
    acks = {e["order_key"]: e["ts_exchange"] for e in ev if e["event_type"] == "ack"}
    assert acks == {"test:acct:o1": T0 + S, "test:acct:o2": T0 + 2 * S, "test:acct:o3": T0 + 3 * S}
    amends = {e["order_key"]: e["ts_exchange"] for e in ev if e["event_type"] == "amend_ack"}
    assert amends == {"test:acct:o2": T0 + 4 * S, "test:acct:o3": T0 + 6 * S}
    fills = {e["order_key"]: (e["ts_exchange"], e["qty"], e["liquidity"]) for e in ev if e["event_type"] == "fill"}
    assert set(fills) == {"test:acct:o1", "test:acct:o3"}
    assert fills["test:acct:o1"][0] == T0 + 6 * S + 1 and fills["test:acct:o3"][0] == T0 + 7 * S + 1
    assert all(f[2] == "maker" for f in fills.values())
    cancel = next(e for e in ev if e["order_key"] == "test:acct:o2" and e["event_type"] == "cancel_ack")
    assert T0 + 32 * S <= cancel["ts_exchange"] <= T0 + 33 * S


def test_hftbacktest_replay_reports_fatal_error(tmp_path, repo_root):
    """hftbacktest 2.4.4 with the partial-fill exchange stops with InvalidOrderStatus on these prints (#312/#316): the
    script keeps the rows so far, names the error code in the summary and exits with status 3 (never a silent
    truncated run)."""
    py = _hbt_python(repo_root)
    if py is None:
        pytest.skip("hftbacktest venv missing (run `make hbt-crosscheck` or set QT_HBT_PYTHON)")
    sched = tmp_path / "schedule.parquet"
    write_table(make_table("qt.shadow_spec", LEGS), sched)
    script = tmp_path / "hbt_replay.py"
    replay_schedule("hftbacktest", script, schedule=sched, units=UNITS, step_ns=10 * MS, window_ns=30 * S)
    _hbt_data(tmp_path / "data.npz")
    (tmp_path / "snippet.py").write_text(
        "from hftbacktest import BacktestAsset\nasset = BacktestAsset().power_prob_queue_model3(2.0)"
        ".partial_fill_exchange()\n"
    )
    out = tmp_path / "sim_orders.jsonl"
    r = subprocess.run(
        [
            str(py),
            "-I",
            str(script),
            "--data",
            f"1={tmp_path / 'data.npz'}",
            "--snippet",
            str(tmp_path / "snippet.py"),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=tmp_path,
    )
    summary = json.loads(Path(str(out) + ".summary.json").read_text())
    assert r.returncode == 3 and "hftbacktest error" in r.stderr
    assert summary["fatal"]["rc"] != 0 and "#312/#316" in summary["fatal"]["hint"]
    assert out.read_text().count("\n") >= 3


def test_hftbacktest_script_requires_a_model(tmp_path, repo_root):
    py = _hbt_python(repo_root)
    if py is None:
        pytest.skip("hftbacktest venv missing (run `make hbt-crosscheck` or set QT_HBT_PYTHON)")
    script = tmp_path / "hbt_replay.py"
    replay_schedule("hftbacktest", script, schedule=LEGS, units=UNITS)
    _hbt_data(tmp_path / "data.npz")
    r = subprocess.run(
        [str(py), "-I", str(script), "--data", f"1={tmp_path / 'data.npz'}", "--out", str(tmp_path / "x.jsonl")],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=tmp_path,
    )
    assert r.returncode != 0 and "choose the fill model" in r.stderr
