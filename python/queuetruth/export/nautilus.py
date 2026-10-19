"""NautilusTrader `FillModel` / `LatencyModel` config values (SPEC 5.10). Generated from NT's documented API at
develop 5247ad5 (LGPL code is not copied; only constructor names and keyword names are emitted).

Only FITTED values are emitted as code (trader review D4): pasting a snippet must never overwrite a setting the
calibration did not estimate.

- `fill_model = FillModel(prob_fill_on_limit=p)` only for an NT touch family (`nt_prob_touch`,
  `nt_queue_l2_prob_touch`) with a finite `p` (the C3 estimate of SPEC 5.4, trade-through/cross first fills
  censored, E28). Other families, or no estimate, give a commented line with the reason (NT's default
  `prob_fill_on_limit=1` is shown commented, never emitted). `prob_slippage` and `random_seed` are never fitted.
- `latency_model = LatencyModel(base_latency_nanos=0, ...)` only with a latency calibration, and only the
  components that have samples: `insert` = p50 entry, `update` = p50 amend, `delete` = p50 cancel (integer ns).
  `base_latency_nanos=0` is emitted with them because the fitted values are the whole one-way latencies (NT adds the
  base to each). Components without samples, and a calibration with no entry samples at all (acks without an
  exchange match/gateway timestamp, e.g. `record_time` acks from bybit, okx, coinbase, deribit, hyperliquid), are
  commented with the reason (review E3).
- `gated` = `yes` for `nt_queue_l2_prob_touch` (fit counted only touches with NT queue excess), else `no`.

The header states the fitted window, events and the held-out result (`queuetruth.export.trust`); a calibration the
export refuses carries a `# !! NOT VALIDATED` block.
"""

from __future__ import annotations

import math

from queuetruth.export import trust
from queuetruth.export._text import VERSION, data_sources_text, family_key, g6, window_text
from queuetruth.schemas.models import CalibrationResult, Quantiles

NT_FAMILIES = ("nt_prob_touch", "nt_queue_l2_prob_touch")
NO_EXCH_TS = (
    "no ack carries an exchange match/gateway timestamp (acks stamped record_time, as from bybit, okx, coinbase, "
    "deribit or hyperliquid, give no one-way latency)"
)


def _p50(q: Quantiles | None) -> int | None:
    """Rounded p50 in ns, None without samples."""
    if q is None or not q.n or q.p50 is None or not math.isfinite(q.p50):
        return None
    return max(0, round(q.p50))


def fill_probability(cal: CalibrationResult) -> float | None:
    """Fitted `prob_fill_on_limit` (clipped to [0, 1]); None when the calibration did not fit one."""
    if family_key(cal.family) in NT_FAMILIES and cal.estimate is not None and math.isfinite(cal.estimate):
        return min(1.0, max(0.0, float(cal.estimate)))
    return None


def latencies(cal: CalibrationResult) -> tuple[int | None, int | None, int | None]:
    """(insert, update, delete) p50 ns from the result's latency fit; None for a component without samples."""
    lat = cal.latency
    if lat is None:
        return None, None, None
    return _p50(lat.entry_ns), _p50(lat.amend_ns), _p50(lat.cancel_ns)


def _fill_reason(cal: CalibrationResult) -> str:
    fam = family_key(cal.family)
    if fam in NT_FAMILIES:
        return f"{cal.family} has no finite estimate"
    if fam == "latency":
        return "a latency calibration fits no fill probability"
    return f"{cal.family} has no NautilusTrader fill-probability equivalent"


def _latency_reason(cal: CalibrationResult) -> str:
    if cal.latency is None:
        return "no latency calibration in this result"
    return NO_EXCH_TS


def nautilus_notes(cal: CalibrationResult) -> list[str]:
    """Notes accompanying `nautilus_snippet`."""
    notes: list[str] = []
    if fill_probability(cal) is not None:
        notes.append(
            "prob_fill_on_limit is a per-touch Bernoulli probability: it is not portable across feeds with "
            "different trade-print or quote-update rates; refit per data source."
        )
    else:
        notes.append(
            f"prob_fill_on_limit not fitted ({_fill_reason(cal)}): no FillModel line is emitted, keep your own; "
            "use queue_position=True for an L2 queue tracker."
        )
    ins, upd, dele = latencies(cal)
    if ins is None and upd is None and dele is None:
        notes.append(
            f"latency not fitted ({_latency_reason(cal)}): no LatencyModel line is emitted; set it from your own "
            "measurements."
        )
    else:
        missing = [name for name, v in (("insert", ins), ("update", upd), ("delete", dele)) if v is None]
        notes.append(
            "latencies are p50 one-way values (entry, amend, cancel) and assume synchronized clocks; "
            "NautilusTrader's LatencyModel is static (no time-varying latency)."
        )
        if missing:
            notes.append(
                f"{', '.join(m + '_latency_nanos' for m in missing)} not fitted (no samples): set them yourself; with "
                "base_latency_nanos=0 an omitted component is 0 ns."
            )
    t = trust.assess(cal)
    if not t.ok:
        notes.append(
            "NOT VALIDATED: `queuetruth export` refuses this calibration ("
            + ", ".join(k for k, _ in t.reasons)
            + "); see the snippet header."
        )
    notes.append(
        "High-level BacktestVenueConfig accepts built-in fill/latency models only; pass these objects to "
        "BacktestEngine.add_venue(...) when your version requires it."
    )
    return notes


def _param_text(cal: CalibrationResult) -> str:
    if cal.kind == "latency":
        return "latency"
    p = cal.estimate
    if p is None or not math.isfinite(p):
        return f"{cal.param_name or 'parameter'} = no estimate"
    return f"{cal.param_name or 'parameter'} = {g6(p)}"


def nautilus_snippet(cal: CalibrationResult) -> str:
    """NautilusTrader `FillModel` / `LatencyModel` lines for the fitted values only (SPEC 5.10 layout, D4)."""
    p = fill_probability(cal)
    ins, upd, dele = latencies(cal)
    gated = "yes" if family_key(cal.family) == "nt_queue_l2_prob_touch" else "no"
    has_lat = any(v is not None for v in (ins, upd, dele))
    names = [n for n, used in (("FillModel", p is not None), ("LatencyModel", has_lat)) if used]
    lines = [
        f"# queuetruth {VERSION} | calibration {cal.run_id} | NautilusTrader API as documented at develop 5247ad5 "
        "(verify names",
        "# for your version; newer builds expose FillModel under nautilus_trader.execution)",
        f"# {cal.family} fitted on {window_text(cal.train)}; held-out {window_text(cal.test)}; "
        f"data: {data_sources_text(cal)}",
        *trust.header_lines(cal, _param_text(cal)),
    ]
    if names:
        lines.append(f"from nautilus_trader.backtest.models import {', '.join(names)}")
    else:
        lines.append("# nothing fitted for NautilusTrader in this calibration: no import or constructor is emitted")
    if p is not None:
        lines.append(f"fill_model = FillModel(prob_fill_on_limit={g6(p)})")
        lines.append("# not fitted: prob_slippage, random_seed (set them as in your current FillModel)")
    else:
        lines.append(f"# fill_model: prob_fill_on_limit not fitted ({_fill_reason(cal)}); keep your own FillModel")
        lines.append("# fill_model = FillModel(prob_fill_on_limit=1)  # NautilusTrader default, NOT fitted")
    if has_lat:
        args = ["base_latency_nanos=0"] + [
            f"{k}={v}"
            for k, v in (("insert_latency_nanos", ins), ("update_latency_nanos", upd), ("delete_latency_nanos", dele))
            if v is not None
        ]
        lines.append(f"latency_model = LatencyModel({', '.join(args)})")
        missing = [k for k, v in (("insert", ins), ("update", upd), ("delete", dele)) if v is None]
        if missing:
            lines.append(
                f"# not fitted (no samples): {', '.join(m + '_latency_nanos' for m in missing)}; set them yourself"
            )
    else:
        lines.append(f"# latency_model: not fitted ({_latency_reason(cal)}); keep your own LatencyModel")
    lines.append(
        f"# queue_position=True and trade_execution=True on the venue config when the fit was queue-gated ({gated})"
    )
    return "\n".join(lines) + "\n"
