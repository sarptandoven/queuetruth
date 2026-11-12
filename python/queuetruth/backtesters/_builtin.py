"""The built-in backtesters: thin entries over the importers in `queuetruth.live.importers`, the replay adapters in
`queuetruth.export.replay` and the snippet writers in `queuetruth.export`.

The `detect` functions are the rules `check` has always used, tried in `detect_priority` order (NautilusTrader's
columns are the most generic, so it comes last)."""

from __future__ import annotations

from typing import Any

from queuetruth.backtesters import BacktesterEntry, Probe, _zip_names, lazy

_NT_EVENTS = ("orderinitialized", "ordersubmitted", "orderaccepted", "orderrejected", "ordercanceled")
_NT_EVENTS += ("orderexpired", "orderupdated", "orderfilled", "orderdenied")
_IMP = "queuetruth.live.importers"


def _is_hftbacktest(p: Probe) -> bool:
    return {"price_tick", "tick_size", "leaves_qty"} <= p.columns


def _is_vectorbt(p: Probe) -> bool:
    cols = p.columns
    return {"Order Id", "Column"} <= cols or (
        "Column" in cols and any("Trade Id" in c or c == "Position Id" for c in cols)
    )


def _is_backtesting_py(p: Probe) -> bool:
    return {"EntryBar", "ExitBar", "EntryPrice", "ExitPrice"} <= p.columns


def _is_backtrader(p: Probe) -> bool:
    if "event" in p.columns and "ref" in p.columns:
        return True
    doc = p.doc
    if isinstance(doc, dict) and doc and all(isinstance(v, list) for k, v in doc.items() if k != "date"):
        return all(isinstance(r, list) for v in doc.values() if isinstance(v, list) for r in v)
    return False


def _is_freqtrade(p: Probe) -> bool:
    if p.magic.startswith(b"PK\x03\x04"):
        names = _zip_names(p.path)
        return names is not None and any(n.endswith(".json") for n in names)
    if p.magic.startswith(b"SQLite format 3\x00"):
        return True
    return isinstance(p.doc, dict) and isinstance(p.doc.get("strategy"), dict)


def _is_lean(p: Probe) -> bool:
    if isinstance(p.doc, dict) and "orders" in p.doc:
        return True
    return bool(p.columns & {"orderEventId", "algorithmId", "order-event-id", "OrderId", "orderId"})


def _is_nautilus(p: Probe) -> bool:
    if p.binary:
        return False
    if p.columns & {"client_order_id", "ts_init", "last_qty"}:
        return True
    return p.path.stem.replace("_", "").lower().startswith(_NT_EVENTS)


def _replay(target: str) -> Any:
    def replay(out: Any, **kwargs: Any) -> Any:
        from queuetruth.export.replay import replay_schedule

        return replay_schedule(target, out, **kwargs)  # type: ignore[arg-type]

    replay.__doc__ = f"`export replay-schedule --target {target}` (`queuetruth.export.replay.replay_schedule`)."
    return replay


def _hbt_snippet(cal: Any) -> tuple[str, list[str]]:
    """hftbacktest code and notes for a calibration result (`queuetruth.export.hftbacktest`)."""
    from queuetruth.export.hftbacktest import hftbacktest_notes, hftbacktest_snippet

    return hftbacktest_snippet(cal), hftbacktest_notes(cal)


def _nt_snippet(cal: Any) -> tuple[str, list[str]]:
    """NautilusTrader code and notes for a calibration result (`queuetruth.export.nautilus`)."""
    from queuetruth.export.nautilus import nautilus_notes, nautilus_snippet

    return nautilus_snippet(cal), nautilus_notes(cal)


#: In the order `check --backtester` lists them (the trading bots then); `check` tries `detect` in `detect_priority`
#: order.
BUILTINS: tuple[BacktesterEntry, ...] = (
    BacktesterEntry(
        name="hftbacktest",
        label="hftbacktest",
        import_sim=lazy(f"{_IMP}.sim:import_hftbacktest"),
        replay=_replay("hftbacktest"),
        snippet=_hbt_snippet,
        zoo_models=("hbt_risk_adverse", "hbt_power", "hbt_power2", "hbt_power3", "hbt_log", "hbt_log2", "hbt_l3_fifo"),
        detect=_is_hftbacktest,
        detect_priority=10,
    ),
    BacktesterEntry(
        name="nautilus",
        label="NautilusTrader",
        import_sim=lazy(f"{_IMP}.sim:import_nautilus"),
        replay=_replay("nautilus"),
        snippet=_nt_snippet,
        zoo_models=("nt_queue_l2", "nt_prob_touch", "nt_queue_l2_prob_touch"),
        detect=_is_nautilus,
        detect_priority=60,
    ),
    BacktesterEntry(
        name="backtrader",
        label="Backtrader",
        import_sim=lazy(f"{_IMP}.backtrader:import_backtrader_sim"),
        reconcile=lazy(f"{_IMP}.backtrader:reconcile_backtrader"),
        detect=_is_backtrader,
        detect_priority=30,
    ),
    BacktesterEntry(
        name="vectorbt",
        label="VectorBT",
        import_sim=lazy(f"{_IMP}.vectorbt:import_vectorbt_sim"),
        reconcile=lazy(f"{_IMP}.vectorbt:reconcile_vectorbt"),
        detect=_is_vectorbt,
        detect_priority=20,
    ),
    BacktesterEntry(
        name="lean",
        label="QuantConnect LEAN",
        import_sim=lazy(f"{_IMP}.lean:import_lean_sim"),
        reconcile=lazy(f"{_IMP}.lean:reconcile_lean"),
        detect=_is_lean,
        detect_priority=50,
    ),
    BacktesterEntry(
        name="freqtrade",
        label="Freqtrade",
        import_sim=lazy(f"{_IMP}.freqtrade:import_freqtrade_sim"),
        import_live=lazy(f"{_IMP}.freqtrade:import_freqtrade_live"),
        reconcile=lazy(f"{_IMP}.freqtrade:reconcile_freqtrade"),
        detect=_is_freqtrade,
        detect_priority=40,
    ),
    BacktesterEntry(
        name="hummingbot",
        label="Hummingbot",
        import_sim=lazy(f"{_IMP}.hummingbot:import_hummingbot_sim"),
        import_live=lazy(f"{_IMP}.hummingbot:import_hummingbot_live"),
        reconcile=lazy(f"{_IMP}.hummingbot:reconcile_hummingbot"),
    ),
    BacktesterEntry(
        name="backtesting_py",
        label="backtesting.py",
        import_sim=lazy(f"{_IMP}.backtesting_py:import_backtesting_py_sim"),
        detect=_is_backtesting_py,
        detect_priority=25,
    ),
)
