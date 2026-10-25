"""The documented real-data demonstration (docs/how-to/agent-sandbox.md): a touch-quoting agent on TSLA, Nasdaq
2019-12-30, 10:00 to 10:30 ET, 5 s steps. Needs the day-one dataset (`QT_REALDATA_DS`, default
`.realdata/ds/nasdaq-2019-12-30` of the repository); never in CI. The scorecard is deterministic, so every scorecard
number the page documents is asserted exactly as printed (the wall times there are measurements)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from queuetruth.errors import QtError
from queuetruth.sandbox import Sandbox, verify
from sandbox.sbx_helpers import realdata_dataset

pytestmark = [pytest.mark.realdata, pytest.mark.integration, pytest.mark.requires_core]


def quote_at_touch(
    sb: Sandbox, *, size: int = 100, max_pos: int = 300, step: str = "5s", max_away_ticks: int = 2
) -> int:
    """Joins the best bid and the best ask with pegged day orders, cancels a quote that is more than two ticks off
    the touch, and stops quoting the side that would grow a position beyond `max_pos`. Returns the rejections."""
    tick = sb.instrument["tick_size"]
    obs = sb.observe()
    rejected = 0
    while not obs.done:
        bid, ask, pos = obs.book.bid, obs.book.ask, obs.position.qty
        working = {o.side: o for o in obs.orders if o.status in ("pending", "working") and o.cancel_sent_ns is None}
        for side, best, allowed in (("buy", bid, pos < max_pos), ("sell", ask, pos > -max_pos)):
            o = working.get(side)
            stale = (
                o is not None
                and o.price is not None
                and best is not None
                and abs(o.price - best[0]) > max_away_ticks * tick
            )
            if o is not None and (not allowed or stale):
                sb.cancel(o.order_id)
                o = None
            if o is None and allowed and best is not None:
                try:
                    sb.submit(side, size, peg_offset_ticks=0)
                except QtError:
                    rejected += 1
        obs = sb.advance(by=step)
    return rejected


SPEC: dict[str, Any] = {
    "symbol": "TSLA",
    "start": "10:00",
    "end": "10:30",
    "latency": "1ms",
    "jitter": "500us",
    "seed": 1,
    "fees": {"maker_per_unit": -0.0020, "taker_per_unit": 0.0030},
    "markout_horizons": ["1s", "10s", "60s"],
}


def test_touch_quoter_on_tsla_day_one(tmp_path: Path) -> None:
    ds = realdata_dataset()
    if ds is None:
        pytest.skip("the day-one dataset is not available (set QT_REALDATA_DS)")
    with Sandbox.open({"dataset": {"path": str(ds)}, **SPEC}, workdir=tmp_path) as sb:
        assert quote_at_touch(sb) == 0
        card = sb.finish(out=tmp_path / "out")
    c = card.consistency
    assert (c.steps, c.window_replays, c.fills_reported, c.fills_matched) == (360, 360, 537, 537)
    assert c.equal and c.timestamps_monotone and c.fills_unreported == c.fills_misattributed == 0
    assert (c.statuses_checked, c.statuses_mismatched) == (511, 0)
    rates = {r.order_class: r for r in card.fill_rates}
    assert (rates["passive_limit"].n_orders, rates["passive_limit"].n_any_fill) == (511, 260)
    assert round(100 * rates["passive_limit"].rate_any, 1) == 50.9
    assert rates["passive_limit"].median_ttf_ns == 2_456_146_282
    assert {f.liquidity for f in card.fills} == {"maker"} and len(card.fills) == 537
    p = card.pnl
    assert (p.position_end, p.bought_qty, p.sold_qty) == (-304, 11_903, 12_207)
    assert (p.pnl_gross, p.fees, p.pnl_net, p.pnl_net_at_touch) == (-965.67, -48.22, -917.45, -943.29)
    adv = {a.horizon_ns // 1_000_000_000: a for a in card.adverse_selection}
    assert (round(adv[1].cost_ccy, 2), round(adv[10].cost_ccy, 3), round(adv[60].cost_ccy, 2)) == (
        1084.98,
        1459.055,
        1058.40,
    )
    assert round(100 * adv[1].adverse_fraction) == 64
    cost = card.execution_cost
    assert round(cost.edge_ccy, 3) == 2087.045 and round(cost.mean_edge_ticks, 1) == 8.9
    # the mid moved against the fills by more than their edge: gross PnL = edge + the move after the decision
    assert round(cost.edge_ccy - p.pnl_gross, 3) == 3052.715
    assert card.data_quality == "clean"
    v = verify(tmp_path / "out" / "order_log.json", scorecard=tmp_path / "out" / "scorecard.json", workdir=tmp_path)
    assert v.ok, v.problems
