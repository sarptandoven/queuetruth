"""`queuetruth.backtrader.QueueFiller`, `queuetruth.lean.QueueFillModel` and their shared `TouchFill`, on a
hand-built synthetic touch table (no market data). Backtrader is driven through stand-in orders here; a real
`cerebro` run is in `test_backtrader_cerebro.py` and runs where Backtrader is installed. LEAN is not installable
from PyPI, so its fill model runs against `lean_standin.py`, a minimal stand-in of LEAN's documented Python
`FillModel` API."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import queuetruth as qt
from queuetruth.backtesters.touchfill import TouchFill, bar_name, rule_ticks
from queuetruth.errors import QtError

from . import synth_touch

HERE = Path(__file__).resolve().parent


@pytest.fixture
def table_path(tmp_path: Path) -> Path:
    p = tmp_path / "touch.json"
    p.write_text(synth_touch.tables().model_dump_json(by_alias=True))
    return p


# ---- TouchFill -----------------------------------------------------------------------------------------------------


def test_lookup_from_file_tables_and_dict(table_path: Path) -> None:
    t = synth_touch.tables()
    for src in (table_path, str(table_path), t, t.model_dump(mode="json", by_alias=True)):
        tf = TouchFill(src, tier="T1")
        assert tf.probability("1m", "buy") == 0.25
        assert tf.probability("5m", "sell") == 0.60
        assert tf.bars == ["1m", "5m"]
    assert TouchFill(t, tier="T1", rule="penetrate_1", tick=0.01).probability("1m", "sell") == 0.95


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tier": "T9"}, "no tier 'T9' \\(tiers: T1, T2\\)"),
        ({"tier": "T1", "rule": "penetrate_2", "tick": 0.01}, "no rule 'penetrate_2' for tier T1"),
        ({"tier": "T1", "rule": "near"}, "rule must be touch or penetrate_<k>"),
        ({"tier": "T1", "rule": "penetrate_1"}, "needs the tick size"),
    ],
)
def test_refuses_what_the_table_lacks_at_construction(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(QtError, match=message) as e:
        TouchFill(synth_touch.tables(), **kwargs)
    assert e.value.code == "QT-CONFIG-001"


def test_refuses_missing_bars_sides_and_values() -> None:
    tf = TouchFill(synth_touch.tables(), tier="T1")
    with pytest.raises(QtError, match="no 1h bars for tier T1, rule touch \\(bars: 1m, 5m\\)"):
        tf.probability("1h", "buy")
    t2 = TouchFill(synth_touch.tables(), tier="T2")
    with pytest.raises(QtError, match=r"no P\(real \| touch\) for tier T2, buy, 1m bars .*no probability"):
        t2.probability("1m", "buy")
    one_side = TouchFill(synth_touch.tables({("T1", "buy", "1m", "touch"): 0.3}), tier="T1")
    with pytest.raises(QtError, match="no sell row for tier T1, 1m bars"):
        one_side.probability("1m", "sell")
    assert t2.probability("1m", "sell") == 0.10
    with pytest.raises(QtError, match="1m, 5m and 1h bars, not 120 s"):
        bar_name(120)
    with pytest.raises(QtError, match="not unknown"):
        bar_name(None)
    assert [bar_name(s) for s in (60, 300, 3600)] == ["1m", "5m", "1h"]
    assert rule_ticks("touch") == 0 and rule_ticks("penetrate_2") == 2


def test_refuses_no_table_and_bad_files(tmp_path: Path) -> None:
    with pytest.raises(QtError, match="need the atlas touch tables") as e:
        TouchFill(None, tier="T1")
    assert e.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as e2:
        TouchFill(tmp_path / "missing.json", tier="T1")
    assert e2.value.code == "QT-INPUT-001"
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema": "qt.atlas.touch"}')
    with pytest.raises(QtError, match=r"not a qt\.atlas\.touch document"):
        TouchFill(bad, tier="T1")


def test_draws_are_seeded_and_independent_of_call_order() -> None:
    def run(seed: int, keys: list[str]) -> dict[str, bool]:
        tf = TouchFill(synth_touch.tables(), tier="T1", seed=seed)
        return {k: tf.decide(order=k, bar_time="t", side="buy", limit=1.0, bar="1m").filled for k in keys}

    keys = [f"o{i}" for i in range(2000)]
    a, b = run(1, keys), run(1, list(reversed(keys)))
    assert a == b
    assert a != run(2, keys)
    share = sum(a.values()) / len(a)
    assert abs(share - 0.25) < 0.04  # P(real | touch) of T1 buy 1m bars
    tf = TouchFill(synth_touch.tables(), tier="T1", seed=1)
    tf.decide(order="x", bar_time="t", side="sell", limit=2.0, bar="5m")
    s = tf.summary()
    assert (s["draws"], s["tier"], s["rule"], s["seed"]) == (1, "T1", "touch", 1)
    assert tf.decisions[0].to_dict()["p"] == 0.60


def test_penetrate_rule_needs_the_bar_to_go_through() -> None:
    tf = TouchFill(synth_touch.tables(), tier="T1", rule="penetrate_1", tick=0.5)
    assert not tf.penetrated("buy", 100.0, low=99.75, high=101.0)
    assert tf.penetrated("buy", 100.0, low=99.5, high=101.0)
    assert not tf.penetrated("sell", 100.0, low=99.0, high=100.25)
    assert tf.penetrated("sell", 100.0, low=99.0, high=100.5)


# ---- Backtrader filler with stand-in orders ---------------------------------------------------------------------


def _num(t: dt.datetime) -> float:
    """Backtrader's date2num."""
    return t.toordinal() + (t - dt.datetime(t.year, t.month, t.day)).total_seconds() / 86400


class _Line(list[float]):
    def __getitem__(self, ago: Any) -> Any:
        return super().__getitem__(-1 + ago) if isinstance(ago, int) else super().__getitem__(ago)


def _order(ref: int, *, buy: bool = True, kind: str = "Limit", price: float = 100.0, low: float = 99.0,
           high: float = 101.0, when: dt.datetime = dt.datetime(2000, 1, 3, 9, 31), size: float = 2.0,
           timeframe: int = 4, compression: int = 1) -> Any:  # fmt: skip
    data = SimpleNamespace(
        _name="QTAU6", _timeframe=timeframe, _compression=compression,
        low=_Line([low]), high=_Line([high]), datetime=_Line([_num(when)]),
    )  # fmt: skip
    return SimpleNamespace(
        ref=ref,
        data=data,
        created=SimpleNamespace(dt=_num(when) - 1 / 1440, price=price, pricelimit=price, size=size),
        executed=SimpleNamespace(remsize=size if buy else -size),
        isbuy=lambda: buy,
        getordername=lambda: kind,
    )


def test_filler_fills_limits_with_the_table_probability(table_path: Path) -> None:
    f = qt.backtrader.QueueFiller(table=table_path, tier="T1", seed=5)
    got = [f(_order(i, buy=i % 2 == 0, price=100 + i * 1e-3), 100.0, 0) for i in range(400)]
    assert set(got) == {0.0, 2.0}
    assert len(f.decisions) == 400
    buys = [d for d in f.decisions if d.side == "buy"]
    sells = [d for d in f.decisions if d.side == "sell"]
    assert {d.p for d in buys} == {0.25} and {d.p for d in sells} == {0.40}
    assert abs(sum(d.filled for d in buys) / len(buys) - 0.25) < 0.08
    assert f.decisions[0].bar_time == "2000-01-03T09:31:00"
    assert [g == 2.0 for g in got] == [d.filled for d in f.decisions]
    again = qt.backtrader.QueueFiller(table=table_path, tier="T1", seed=5)
    assert [again(_order(1000 + i, buy=i % 2 == 0, price=100 + i * 1e-3), 100.0, 0) for i in range(400)] == got


def test_filler_leaves_market_and_stop_orders_alone(table_path: Path) -> None:
    f = qt.backtrader.QueueFiller(table=table_path, tier="T1")
    for kind in ("Market", "Close", "Stop", "StopTrail"):
        assert f(_order(1, kind=kind), 100.0, 0) == 2.0
    assert f.decisions == []
    f(_order(2, kind="StopLimit"), 100.0, 0)
    assert len(f.decisions) == 1


def test_filler_reads_the_bar_length_and_refuses_others(table_path: Path) -> None:
    f = qt.backtrader.QueueFiller(table=table_path, tier="T1")
    f(_order(1, compression=5), 100.0, 0)
    assert f.decisions[-1].p == 0.50  # 5-minute bars
    f(_order(2, timeframe=3, compression=60), 100.0, 0)
    assert f.decisions[-1].p == 0.25  # 60-second bars are 1-minute bars
    with pytest.raises(QtError, match="not 900 s"):
        f(_order(3, compression=15), 100.0, 0)
    with pytest.raises(QtError, match="not unknown"):
        f(_order(4, timeframe=5), 100.0, 0)  # daily bars
    with pytest.raises(QtError, match="no 1h bars"):
        f(_order(5, compression=60), 100.0, 0)
    with pytest.raises(QtError, match="no 1h bars"):
        qt.backtrader.QueueFiller(table=table_path, tier="T1", bar="1h")
    fixed = qt.backtrader.QueueFiller(table=table_path, tier="T1", bar="5m")
    fixed(_order(6, compression=15), 100.0, 0)
    assert fixed.decisions[-1].p == 0.50


def test_filler_penetrate_rule(table_path: Path) -> None:
    f = qt.backtrader.QueueFiller(table=table_path, tier="T1", rule="penetrate_1", tick=0.5, seed=3)
    assert f(_order(1, price=100.0, low=99.75), 100.0, 0) == 0.0  # touched, not one tick through
    assert f.decisions == []
    f(_order(2, price=100.0, low=99.5), 100.0, 0)
    assert len(f.decisions) == 1 and f.decisions[0].p == 0.90


def test_order_log_needs_backtrader() -> None:
    if _has("backtrader"):
        pytest.skip("Backtrader is installed here; test_backtrader_cerebro.py covers OrderLog")
    with pytest.raises(QtError, match="needs Backtrader") as e:
        _ = qt.backtrader.OrderLog
    assert e.value.hint == "pip install backtrader"


def _has(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


# ---- LEAN fill model against the stand-in ------------------------------------------------------------------------


@pytest.fixture
def lean(monkeypatch: pytest.MonkeyPatch) -> Any:
    from . import lean_standin

    monkeypatch.setitem(sys.modules, "AlgorithmImports", lean_standin)
    monkeypatch.setattr("queuetruth.lean._CLASS", [])
    return lean_standin


def test_lean_model_accepts_limit_fills_with_the_table_probability(lean: Any, table_path: Path) -> None:
    m = qt.lean.QueueFillModel(table=table_path, tier="T1", seed=9)
    assert isinstance(m, lean.FillModel)
    asset = lean.Security(low=99.0, high=101.0, resolution=lean.Resolution.MINUTE)
    events = []
    for i in range(300):
        asset.local_time = dt.datetime(2000, 1, 3, 9, 31) + dt.timedelta(minutes=i)
        order = lean.LimitOrder(id=i + 1, quantity=3 if i % 2 == 0 else -3, limit_price=100.0)
        events.append(m.limit_fill(asset, order))
    assert len(m.decisions) == 300
    for ev, d in zip(events, m.decisions, strict=True):
        if d.filled:
            assert (ev.status, ev.fill_price) == (lean.OrderStatus.FILLED, 100.0) and abs(ev.fill_quantity) == 3
        else:
            assert (ev.status, ev.fill_quantity, ev.fill_price) == (lean.OrderStatus.NONE, 0, 0)
    buys = [d for d in m.decisions if d.side == "buy"]
    assert {d.p for d in buys} == {0.25}
    assert abs(sum(d.filled for d in buys) / len(buys) - 0.25) < 0.1
    again = qt.lean.QueueFillModel(table=table_path, tier="T1", seed=9)
    for i in range(300):
        asset.local_time = dt.datetime(2000, 1, 3, 9, 31) + dt.timedelta(minutes=i)
        again.limit_fill(asset, lean.LimitOrder(id=i + 1, quantity=3 if i % 2 == 0 else -3, limit_price=100.0))
    assert [d.filled for d in again.decisions] == [d.filled for d in m.decisions]
    assert m.summary()["draws"] == 300


def test_lean_model_leaves_unreached_orders_and_reads_the_resolution(lean: Any, table_path: Path) -> None:
    m = qt.lean.QueueFillModel(table=table_path, tier="T1")
    far = m.limit_fill(lean.Security(low=101.0, high=102.0), lean.LimitOrder(id=1, quantity=1, limit_price=100.0))
    assert far.status == lean.OrderStatus.NONE and m.decisions == []
    with pytest.raises(QtError, match="no 1h bars"):
        m.limit_fill(lean.Security(low=99.0, high=101.0, resolution=lean.Resolution.HOUR),
                     lean.LimitOrder(id=2, quantity=1, limit_price=100.0))  # fmt: skip
    with pytest.raises(QtError, match="not unknown"):
        m.limit_fill(lean.Security(low=99.0, high=101.0, resolution=lean.Resolution.DAILY),
                     lean.LimitOrder(id=3, quantity=1, limit_price=100.0))  # fmt: skip
    fixed = qt.lean.QueueFillModel(table=table_path, tier="T1", bar="5m")
    fixed.limit_fill(lean.Security(low=99.0, high=101.0, resolution=lean.Resolution.DAILY),
                     lean.LimitOrder(id=4, quantity=1, limit_price=100.0))  # fmt: skip
    assert fixed.decisions[-1].p == 0.50
    assert qt.lean.QueueFillModel(table=table_path, tier="T1").market_fill(None, None) == "market"


def test_lean_refusals_come_before_lean_is_needed(table_path: Path) -> None:
    with pytest.raises(QtError, match="no tier 'T3'"):
        qt.lean.QueueFillModel(table=table_path, tier="T3")
    with pytest.raises(QtError, match="no 1h bars"):
        qt.lean.QueueFillModel(table=table_path, tier="T1", bar="1h")
    if not _has("AlgorithmImports"):
        with pytest.raises(QtError, match="inside a QuantConnect LEAN Python algorithm"):
            qt.lean.QueueFillModel(table=table_path, tier="T1")
