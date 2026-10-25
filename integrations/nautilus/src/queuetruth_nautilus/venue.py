"""NautilusTrader objects from a `NautilusCalibration`: the fill model, the latency model and the venue settings.

Only fitted values are set (as `queuetruth export nautilus`): `prob_fill_on_limit` from the fit, `prob_slippage`
and `random_seed` from the caller (never fitted), and a latency model only for components with samples.

The venue settings the calibration was fitted and cross-checked under are part of the model: `book_type=L2_MBP`
(the fit's touches are L2 book updates and trade prints), `trade_execution=True` (trade prints fill resting orders)
and `queue_position` = True only for the queue-gated family. `venue_options` returns all of them for
`BacktestEngine.add_venue(**...)`; `venue_config` builds the high-level `BacktestVenueConfig`.

A calibration whose trust verdict is `refused` raises `CalibrationRefused` (`QT-CALIB-001`) unless `force=True`,
which emits a `CalibrationWarning` instead. Each public function checks trust and version once and emits each
`CalibrationWarning` once, attributed to the caller's line.
"""

from __future__ import annotations

import math
import warnings
from typing import Any

from queuetruth_nautilus import compat
from queuetruth_nautilus.calibration import MODE_UNKNOWN, NautilusCalibration
from queuetruth_nautilus.errors import CalibrationRefused, CalibrationWarning, QtNautilusError

U64_MAX = 2**64 - 1


def _trust_note(cal: NautilusCalibration, force: bool) -> str | None:
    """None for an exportable calibration; raises `CalibrationRefused` for a refused one, or with `force` returns the
    warning text."""
    if cal.trust.ok:
        return None
    reasons = list(cal.trust.reasons) or ["refused"]
    texts = "; ".join(f"{r}: {cal.trust.texts[r]}" if r in cal.trust.texts else r for r in reasons)
    msg = f"calibration {cal.run_id} ({cal.family}) is refused by its trust verdict ({texts})"
    if not force:
        raise CalibrationRefused(
            msg,
            reasons,
            "fit on more data or a wider grid; force=True (CLI --force) uses it anyway, for inspection only",
        )
    return f"force: using a NOT VALIDATED calibration: {msg}"


def check_trust(cal: NautilusCalibration, *, force: bool = False) -> None:
    """Raises `CalibrationRefused` for a refused calibration; with `force`, warns instead."""
    note = _trust_note(cal, force)
    if note is not None:
        warnings.warn(CalibrationWarning(note), stacklevel=2)


def caveats(
    cal: NautilusCalibration,
    nt_major: int,
    *,
    fill: bool,
    latency: bool,
    random_seed: int | None = None,
) -> list[str]:
    """The `CalibrationWarning` texts for using `cal` on release line `nt_major` (no trust or version check):
    `fill` when a fill model is built, `latency` when a latency model is."""
    out: list[str] = []
    if fill:
        if cal.mode == MODE_UNKNOWN:
            out.append(
                f"calibration {cal.run_id}: the export text does not name the fit's mode; only a faithful-mode fit "
                "describes NautilusTrader's touch rules, so check the calibration JSON (its `mode` field)"
            )
        elif cal.mode != "faithful":
            out.append(
                f"calibration {cal.run_id} was fitted in {cal.mode!r} mode, whose touch rules are not "
                "NautilusTrader's; only faithful-mode fits describe the engine"
            )
        if cal.queue_position and nt_major >= 2:
            out.append(
                f"{cal.family} was fitted under nautilus_trader 1.231.0 queue rules; from 2.0 a book update that "
                "shrinks the order's level also caps queue ahead, so gated touches differ on a few orders (at most "
                "0.4% of orders at 0.1 to 60 s on the cross-checked Nasdaq days); refit if that matters"
            )
        p = cal.prob_fill_on_limit
        if random_seed is None and p is not None and 0.0 < p < 1.0:
            out.append(
                f"random_seed not set: with prob_fill_on_limit={p:.6g} every at-price print is a random draw, so "
                "fills change from run to run; fix random_seed to compare runs, and average several seeds for "
                "per-order or markout analysis"
            )
    if latency and cal.latency is not None:
        lat = cal.latency
        missing = [
            n for n, v in (("insert", lat.insert_ns), ("update", lat.update_ns), ("cancel", lat.cancel_ns)) if v is None
        ]
        if missing:
            out.append(
                f"{', '.join(m + '_latency_nanos' for m in missing)} not fitted (no samples): set to 0 ns; pass "
                "your own value if you have one"
            )
    return out


def _emit(messages: list[str]) -> None:
    """Warns once per message, attributed to the caller of the public function that called `_emit`."""
    for m in messages:
        warnings.warn(CalibrationWarning(m), stacklevel=3)


def _prepare(
    cal: NautilusCalibration, force: bool, *, fill: bool, latency: bool, random_seed: int | None = None
) -> tuple[compat.NtVersion, list[str]]:
    """Trust check, version check and caveats, once per public call: the installed version and the warnings to emit
    after the objects are built."""
    note = _trust_note(cal, force)
    v = compat.installed_version()
    msgs = [] if note is None else [note]
    unverified = compat.unverified_message(v)
    if unverified is not None:
        msgs.append(unverified)
    msgs += caveats(cal, v.major, fill=fill, latency=latency, random_seed=random_seed)
    return v, msgs


def _nothing(cal: NautilusCalibration) -> QtNautilusError:
    return QtNautilusError(
        "QT-CONFIG-001",
        f"calibration {cal.run_id} ({cal.family}) has nothing NautilusTrader can use (no fill probability, no latency)",
    )


def _seed(random_seed: int | None) -> int | None:
    if random_seed is None:
        return None
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise QtNautilusError("QT-CONFIG-001", f"random_seed: expected an int or None, got {random_seed!r}")
    if not 0 <= random_seed <= U64_MAX:
        raise QtNautilusError("QT-CONFIG-001", f"random_seed: {random_seed} is outside [0, 2**64 - 1]")
    return random_seed


def _slippage(prob_slippage: float) -> float:
    if isinstance(prob_slippage, bool) or not isinstance(prob_slippage, int | float):
        raise QtNautilusError("QT-CONFIG-001", f"prob_slippage: expected a number, got {prob_slippage!r}")
    if not (math.isfinite(prob_slippage) and 0.0 <= prob_slippage <= 1.0):
        raise QtNautilusError("QT-CONFIG-001", f"prob_slippage: {prob_slippage!r} is not a probability in [0, 1]")
    return float(prob_slippage)


def _require_p(cal: NautilusCalibration) -> float:
    if cal.prob_fill_on_limit is None:
        raise QtNautilusError(
            "QT-CONFIG-001",
            f"calibration {cal.run_id} ({cal.family}) fits no NautilusTrader prob_fill_on_limit",
            "fit `queuetruth calibrate nt-touch` (optionally --queue-gated); keep your own FillModel meanwhile",
        )
    return cal.prob_fill_on_limit


def fill_model_kwargs(
    cal: NautilusCalibration, *, random_seed: int | None = None, prob_slippage: float = 0.0
) -> dict[str, Any]:
    """Constructor keywords of the probabilistic fill model (the same names in 1.x and 2.x)."""
    return {
        "prob_fill_on_limit": _require_p(cal),
        "prob_slippage": _slippage(prob_slippage),
        "random_seed": _seed(random_seed),
    }


def fill_model(
    cal: NautilusCalibration, *, random_seed: int | None = None, prob_slippage: float = 0.0, force: bool = False
) -> Any:
    """NautilusTrader's probabilistic fill model with the fitted `prob_fill_on_limit` (`FillModel` in 1.x,
    `DefaultFillModel` in 2.x). `QT-CONFIG-001` when the calibration fits no fill probability."""
    kw = fill_model_kwargs(cal, random_seed=random_seed, prob_slippage=prob_slippage)
    v, msgs = _prepare(cal, force, fill=True, latency=False, random_seed=random_seed)
    obj = compat.fill_model_cls(v)(**kw)
    _emit(msgs)
    return obj


def latency_model_kwargs(cal: NautilusCalibration) -> dict[str, int] | None:
    """Constructor keywords of the static latency model, None when no component was fitted.

    `base_latency_nanos=0` because the fitted values are whole one-way latencies (NautilusTrader adds the base to
    each); a component without samples is 0 (the class default on both lines) and named in a `CalibrationWarning`."""
    lat = cal.latency
    if lat is None or not lat.fitted:
        return None
    return {
        "base_latency_nanos": 0,
        "insert_latency_nanos": lat.insert_ns or 0,
        "update_latency_nanos": lat.update_ns or 0,
        "cancel_latency_nanos": lat.cancel_ns or 0,
    }


def latency_model(cal: NautilusCalibration, *, force: bool = False) -> Any | None:
    """NautilusTrader's static latency model with the fitted p50 one-way latencies, or None when none was fitted."""
    kw = latency_model_kwargs(cal)
    if kw is None:
        return None
    v, msgs = _prepare(cal, force, fill=False, latency=True)
    obj = compat.latency_model_cls(v)(**kw)
    _emit(msgs)
    return obj


def venue_options(
    cal: NautilusCalibration, *, random_seed: int | None = None, prob_slippage: float = 0.0, force: bool = False
) -> dict[str, Any]:
    """Keyword arguments for `BacktestEngine.add_venue(...)`: `fill_model` (when fitted), `latency_model` (when
    fitted), `book_type=L2_MBP`, `trade_execution=True` and `queue_position` (True only for the queue-gated family;
    absent for a latency-only calibration). `QT-CONFIG-001` when the calibration carries neither a fill probability
    nor a latency. Not included: `fee_model`, which nautilus_trader requires on every backtest venue from 2.0.0rc7
    (pass `fee_model=FixedFeeModel(Money(0, USD))` for zero fees)."""
    fill = cal.prob_fill_on_limit is not None
    fkw = fill_model_kwargs(cal, random_seed=random_seed, prob_slippage=prob_slippage) if fill else None
    lkw = latency_model_kwargs(cal)
    if fkw is None and lkw is None:
        raise _nothing(cal)
    v, msgs = _prepare(cal, force, fill=fill, latency=lkw is not None, random_seed=random_seed)
    out: dict[str, Any] = {}
    if fkw is not None:
        out["fill_model"] = compat.fill_model_cls(v)(**fkw)
        out["book_type"] = compat.book_type_l2(v)
        out["trade_execution"] = True
        out["queue_position"] = cal.queue_position
    if lkw is not None:
        out["latency_model"] = compat.latency_model_cls(v)(**lkw)
    _emit(msgs)
    return out


def venue_config(
    cal: NautilusCalibration,
    name: str,
    oms_type: Any,
    account_type: Any,
    starting_balances: list[str],
    *,
    random_seed: int | None = None,
    prob_slippage: float = 0.0,
    force: bool = False,
    **kwargs: Any,
) -> Any:
    """`BacktestVenueConfig` for the high-level API (`BacktestNode`) with the calibrated settings. `kwargs` are
    passed through (they may not set the calibrated fields). In 1.x the models are importable configs
    (`FillModelConfig` / `LatencyModelConfig`); in 2.x the config takes the model objects, and `fee_model` is
    required (`QT-CONFIG-001` without it): from 2.0.0rc7 a backtest venue refuses to start without one, and requiring
    it on every 2.x release keeps code written for one release candidate working on the next."""
    clash = sorted({"fill_model", "latency_model", "queue_position", "trade_execution", "book_type"} & set(kwargs))
    if clash:
        raise QtNautilusError("QT-CONFIG-001", f"venue_config: {', '.join(clash)} come from the calibration")
    fill = cal.prob_fill_on_limit is not None
    fkw = fill_model_kwargs(cal, random_seed=random_seed, prob_slippage=prob_slippage) if fill else None
    lkw = latency_model_kwargs(cal)
    if fkw is None and lkw is None:
        raise _nothing(cal)
    v, msgs = _prepare(cal, force, fill=fill, latency=lkw is not None, random_seed=random_seed)
    if v.major >= 2 and kwargs.get("fee_model") is None:
        raise QtNautilusError(
            "QT-CONFIG-001",
            f"venue_config: nautilus_trader 2.x (from 2.0.0rc7; installed {v.text}) requires an explicit fee_model "
            "on every backtest venue, a zero-fee model included",
            "pass fee_model=FixedFeeModel(Money(0, USD)) for zero fees (from nautilus_trader.execution and "
            "nautilus_trader.model), or your own",
        )
    opts: dict[str, Any] = {}
    if fkw is not None:
        if v.major >= 2:
            opts["fill_model"] = compat.fill_model_cls(v)(**fkw)
        else:
            ifm = compat.resolve("nautilus_trader.backtest.config:ImportableFillModelConfig")
            opts["fill_model"] = ifm(
                fill_model_path="nautilus_trader.backtest.models:FillModel",
                config_path="nautilus_trader.backtest.config:FillModelConfig",
                config=fkw,
            )
        opts["book_type"] = "L2_MBP"
        opts["trade_execution"] = True
        opts["queue_position"] = cal.queue_position
    if lkw is not None:
        if v.major >= 2:
            opts["latency_model"] = compat.latency_model_cls(v)(**lkw)
        else:
            ilm = compat.resolve("nautilus_trader.backtest.config:ImportableLatencyModelConfig")
            opts["latency_model"] = ilm(
                latency_model_path="nautilus_trader.backtest.models:LatencyModel",
                config_path="nautilus_trader.backtest.config:LatencyModelConfig",
                config=lkw,
            )
    bvc = compat.resolve(
        "nautilus_trader.backtest:BacktestVenueConfig"
        if v.major >= 2
        else "nautilus_trader.backtest.config:BacktestVenueConfig"
    )
    cfg = bvc(
        name=name, oms_type=oms_type, account_type=account_type, starting_balances=starting_balances, **opts, **kwargs
    )
    _emit(msgs)
    return cfg
