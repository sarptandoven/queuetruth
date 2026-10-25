"""Version-correct NautilusTrader code for a calibration (no nautilus_trader import needed).

`queuetruth export nautilus` writes one layout for every NautilusTrader version, against the API documented at a
pinned develop commit. `render` writes the names of the release line it is asked for (`nt_major` 1 or 2), checked
on 1.231.0 and 2.0.0rc7: in 2.x the fill model is `nautilus_trader.execution.DefaultFillModel` and the latency
model `StaticLatencyModel`; in both the cancel component is `cancel_latency_nanos`.
"""

from __future__ import annotations

import warnings

from queuetruth_nautilus.calibration import NautilusCalibration
from queuetruth_nautilus.errors import CalibrationWarning, QtNautilusError
from queuetruth_nautilus.venue import _nothing, _trust_note, caveats, fill_model_kwargs, latency_model_kwargs


def _g6(x: float) -> str:
    return format(float(x), ".6g")


def _repr_p(p: float) -> str:
    """Shortest text that round-trips the fitted probability (the export's 6 digits lose precision)."""
    return repr(float(p))


def render(cal: NautilusCalibration, nt_major: int, *, random_seed: int | None = None, force: bool = False) -> str:
    """Python lines building the calibrated fill model, latency model and venue settings for `nt_major`.

    The trust rule and the caveats of `venue_options` apply for the requested release line (not the installed one):
    `CalibrationRefused` for a refused fit unless `force`, and a `CalibrationWarning` per caveat. Only fitted values
    become code: a latency component without samples is left at the class default (0) and named in a comment."""
    if nt_major not in (1, 2):
        raise QtNautilusError("QT-CONFIG-001", f"nt_major: expected 1 or 2, got {nt_major!r}")
    fkw = fill_model_kwargs(cal, random_seed=random_seed) if cal.prob_fill_on_limit is not None else None
    lkw = latency_model_kwargs(cal)
    if fkw is None and lkw is None:
        raise _nothing(cal)
    note = _trust_note(cal, force)
    msgs = ([] if note is None else [note]) + caveats(
        cal, nt_major, fill=fkw is not None, latency=lkw is not None, random_seed=random_seed
    )
    fm, lm, mod = (
        ("DefaultFillModel", "StaticLatencyModel", "nautilus_trader.execution")
        if nt_major == 2
        else ("FillModel", "LatencyModel", "nautilus_trader.backtest.models")
    )
    bt_mod = "nautilus_trader.model" if nt_major == 2 else "nautilus_trader.model.enums"
    lines = [
        f"# queuetruth-nautilus | calibration {cal.run_id} | {cal.family} fitted on {cal.train}; held-out {cal.test}",
        f"# nautilus_trader {nt_major}.x names (checked on {'2.0.0rc7' if nt_major == 2 else '1.231.0'})",
    ]
    if cal.n_events is not None:
        ci = cal.ci_bootstrap
        ci_t = "" if ci is None or None in ci else f", bootstrap CI [{_g6(ci[0])}, {_g6(ci[1])}]"  # type: ignore[arg-type]
        lines.append(f"# fit: {cal.n_events} events{ci_t}; trust verdict: {cal.trust.verdict}")
    if not cal.trust.ok:
        lines.append(f"# !! NOT VALIDATED ({', '.join(cal.trust.reasons) or 'refused'}): for inspection only")
    names = [n for n, used in ((fm, fkw is not None), (lm, lkw is not None)) if used]
    if names:
        lines.append(f"from {mod} import {', '.join(names)}")
    if fkw is not None:
        lines.append(f"from {bt_mod} import BookType")
        p = fkw["prob_fill_on_limit"]
        if fkw["random_seed"] is None:
            seed = ""
            if 0.0 < p < 1.0:
                lines.append(
                    "# random_seed not set: each at-price print is a random draw, so fills change between runs; add "
                    "random_seed=<int> and average several seeds for per-order analysis"
                )
        else:
            seed = f", random_seed={fkw['random_seed']}"
        lines.append(f"fill_model = {fm}(prob_fill_on_limit={_repr_p(p)}, prob_slippage=0.0{seed})")
    else:
        lines.append(f"# fill_model: {cal.family} fits no prob_fill_on_limit; keep your own")
    if lkw is not None:
        assert cal.latency is not None
        lat = cal.latency
        fitted = {
            "insert_latency_nanos": lat.insert_ns,
            "update_latency_nanos": lat.update_ns,
            "cancel_latency_nanos": lat.cancel_ns,
        }
        args = ", ".join(["base_latency_nanos=0", *(f"{k}={v}" for k, v in fitted.items() if v is not None)])
        lines.append(f"latency_model = {lm}({args})")
        missing = [k for k, v in fitted.items() if v is None]
        if missing:
            lines.append(f"# not fitted (no samples): {', '.join(missing)}; 0 ns by default, set your own")
    else:
        lines.append("# latency_model: not fitted; keep your own")
    fee = "fee_model=..., " if nt_major == 2 else ""
    if fkw is not None:
        lines.append("venue_options = dict(")
        lines.append("    fill_model=fill_model,")
        if lkw is not None:
            lines.append("    latency_model=latency_model,")
        lines.append("    book_type=BookType.L2_MBP,")
        lines.append("    trade_execution=True,")
        lines.append(f"    queue_position={cal.queue_position},")
        lines.append(")")
        lines.append(
            f"# engine.add_venue(venue=..., oms_type=..., account_type=..., starting_balances=[...], {fee}"
            "**venue_options)"
        )
    elif lkw is not None:
        lines.append(f"# engine.add_venue(..., {fee}latency_model=latency_model)")
    if nt_major == 2:
        lines.append(
            "# nautilus_trader 2.x (from 2.0.0rc7) requires an explicit fee_model on every backtest venue: your own, "
            "or nautilus_trader.execution.FixedFeeModel(Money(0, USD)) for zero fees (not fitted)"
        )
    for m in msgs:
        warnings.warn(CalibrationWarning(m), stacklevel=2)
    return "\n".join(lines) + "\n"
