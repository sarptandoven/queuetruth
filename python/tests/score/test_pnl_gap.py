"""The P&L gap of a scorecard (`queuetruth.score.pnl_gap`, `qt.scorecard_summary.pnl_gap`, schema 1.2): the per-order
mean of `qt.score_shortfall.total_ccy` with its CI, a finding only when the 95% CI excludes 0, the granted / denied /
partial / timing / fees parts adding up to the gap, the horizon choice, currencies, the plain sentence, and the
scorer writing it into `summary.json`. Synthetic quotes and orders only."""

from __future__ import annotations

import json
import math

import pytest

from queuetruth.schemas.models import PnlGap, ScorecardSummary
from queuetruth.score import build_scorecard
from queuetruth.score._io import make_table, read_parquet, rows
from queuetruth.score.pnl_gap import PART_WORDS, headline, main_part, money, pick_horizon, pnl_gap
from queuetruth.score.shortfall import shortfall_rows
from score.qt_score_helpers import MS, T0, TICK, FakeRunner, S, log_from, make_dataset, order_events
from score.test_shortfall import PX, _bbo, _end_mbo, _instruments, _order, _sf

HALF_HOUR = 1_800_000_000_000


def _total(h: int, stat: str, total: float | None, cur: str | None = "USD", n: int = 3) -> dict:
    return {"scope": "total", "statistic": stat, "horizon_ns": h, "currency": cur, "valued": n > 0, "n": n,
            "total_ccy": total}  # fmt: skip


def _orow(h: int, key: str, lq: int, sq: int, cur: str | None = "USD", valued: bool = True, **ccy: float) -> dict:
    r = {"scope": "order", "statistic": "value", "horizon_ns": h, "order_key": key, "currency": cur,
         "live_qty": lq, "sim_qty": sq, "valued": valued, "n": int(valued)}  # fmt: skip
    for c in ("delay", "spread", "missed_fill", "adverse_selection", "partial_fill", "fees"):
        r[f"{c}_ccy"] = ccy.get(c, 0.0) if valued else None
    r["total_ccy"] = math.fsum(r[f"{c}_ccy"] for c in ("delay", "spread", "missed_fill", "adverse_selection",
                                                      "partial_fill", "fees")) if valued else None  # fmt: skip
    return r


def _table(h: int, orders: list[dict], lo: float | None, hi: float | None, cur: str | None = "USD") -> list[dict]:
    valued = [o for o in orders if o["valued"]]
    mean = math.fsum(o["total_ccy"] for o in valued) / len(valued) if valued else None
    tot = [_total(h, "mean", mean, cur, len(valued)), _total(h, "ci_lo", lo, cur, len(valued)),
           _total(h, "ci_hi", hi, cur, len(valued))]  # fmt: skip
    return tot + orders


def test_finding_names_the_granted_fills_and_parts_add_up():
    orders = [
        _orow(S, "a", 0, 10, missed_fill=0.9),  # granted: the sim filled, live did not
        _orow(S, "b", 10, 0, missed_fill=-0.1),  # denied: live filled, the sim did not
        _orow(S, "c", 10, 10, delay=0.05, spread=-0.05, adverse_selection=0.2, fees=0.01),
        _orow(S, "d", 10, 4, partial_fill=-0.03),
    ]
    (g,) = pnl_gap(_table(S, orders, 0.05, 0.6), S)
    assert g["direction"] == "sim_too_high" and g["horizon_ns"] == S and g["currency"] == "USD"
    assert g["n_valued"] == 4 and g["n_unvalued"] == 0
    p = g["parts_ccy"]
    assert p == pytest.approx({"granted": 0.9 / 4, "denied": -0.1 / 4, "partial": -0.03 / 4, "timing": 0.2 / 4,
                               "fees": 0.01 / 4})  # fmt: skip
    assert math.fsum(p.values()) == pytest.approx(g["mean_ccy"], rel=1e-12)
    assert (g["main_part"], g["main_part_majority"]) == ("granted", True)
    assert g["headline"] == (
        "the simulator's P&L is 0.245 USD per order too high (95% CI +0.050 to +0.600) at the 1 s markout, mostly "
        "from fills it grants that live did not get (+0.225 USD per order); 4 orders"
    )
    PnlGap.model_validate(g)


def test_too_low_from_missed_fills():
    orders = [_orow(S, k, 10, 0, missed_fill=-2.0) for k in "abc"] + [_orow(S, "d", 0, 0)]
    (g,) = pnl_gap(_table(S, orders, -2.5, -0.5), S)
    assert g["direction"] == "sim_too_low" and g["main_part"] == "denied" and g["main_part_majority"]
    assert "P&L is 1.50 USD per order too low (95% CI -2.50 to -0.50)" in g["headline"]
    assert "mostly from fills live got that it misses (-1.50 USD per order)" in g["headline"]


def test_ci_with_zero_is_no_difference_found():
    orders = [_orow(S, "a", 0, 10, missed_fill=0.9), _orow(S, "b", 10, 0, missed_fill=-0.6)]
    (g,) = pnl_gap(_table(S, orders, -0.4, 0.7), S)
    assert g["direction"] == "no_significant_difference"
    assert g["main_part"] is None and g["main_part_majority"] is None
    h = g["headline"]
    assert h.startswith("no P&L difference found at the 1 s markout: the simulator's P&L is +0.150 USD per order")
    assert "95% CI -0.400 to +0.700 includes 0" in h  # the CI with the estimate's decimals
    assert "too high" not in h and "mostly" not in h
    # a CI touching 0 does not exclude it
    (g,) = pnl_gap(_table(S, orders, 0.0, 0.7), S)
    assert g["direction"] == "no_significant_difference"


def test_no_ci_is_never_a_finding():
    (g,) = pnl_gap(_table(S, [_orow(S, "a", 0, 10, missed_fill=5.0)], None, None), S)
    assert g["direction"] == "no_significant_difference"
    assert "no CI so no difference can be shown" in g["headline"]


def test_not_computable_without_valued_orders():
    orders = [_orow(S, "a", 10, 0, cur=None, valued=False), _orow(S, "b", 0, 0, cur=None, valued=False)]
    (g,) = pnl_gap(_table(S, orders, None, None, cur=None), S)
    assert g["direction"] == "not_computable" and g["mean_ccy"] is None and g["currency"] is None
    assert (g["n_valued"], g["n_unvalued"]) == (0, 2)
    assert g["parts_ccy"] == dict.fromkeys(PART_WORDS)
    assert g["headline"].startswith("P&L not computable at the 1 s markout: no scored order could be valued")
    assert "2 unvalued order(s) left out" in g["headline"]
    PnlGap.model_validate(g)
    assert pnl_gap([], S) == []


def test_largest_part_when_parts_offset():
    # timing +0.4 and granted +0.35 push up, denied -0.45 pulls down: the gap is +0.3 and no part is "mostly"
    parts = {"granted": 0.35, "denied": -0.45, "partial": 0.0, "timing": 0.4, "fees": 0.0}
    assert main_part(0.3, parts) == ("timing", True)
    parts = {"granted": 0.12, "denied": 0.0, "partial": 0.1, "timing": 0.1, "fees": 0.0}
    assert main_part(0.32, parts) == ("granted", False)
    assert main_part(0.0, parts) is None
    assert main_part(-0.1, parts) is None
    g = {"horizon_ns": S, "currency": "USD", "n_valued": 9, "n_unvalued": 1, "mean_ccy": 0.32, "ci_lo": 0.1,
         "ci_hi": 0.5, "direction": "sim_too_high", "parts_ccy": parts, "main_part": "granted",
         "main_part_majority": False}  # fmt: skip
    assert ", the largest part from fills it grants that live did not get (+0.120 USD per order); 9 orders; 1 " in (
        headline(g)
    )


def test_horizon_choice():
    assert pick_horizon([50 * MS, S, 10 * S], S) == S
    assert pick_horizon([50 * MS, 10 * S], S) == 50 * MS
    assert pick_horizon([500 * MS, 1500 * MS], S) == 500 * MS  # a tie goes to the shorter horizon
    assert pick_horizon([], S) is None
    orders = [_orow(h, "a", 0, 1, missed_fill=1.0) for h in (50 * MS, 10 * S)]
    t = _table(50 * MS, orders[:1], 0.5, 1.5) + _table(10 * S, orders[1:], 0.5, 1.5)
    assert [g["horizon_ns"] for g in pnl_gap(t, S)] == [50 * MS]
    assert [g["horizon_ns"] for g in pnl_gap(t, 60 * S)] == [10 * S]


def test_one_entry_per_currency_sorted():
    t = (
        _table(S, [_orow(S, "u", 0, 1, cur="USD", missed_fill=1.0)], 0.5, 1.5, cur="USD")
        + _table(S, [_orow(S, "n", 1, 0, cur=None, valued=False)], None, None, cur=None)
        + _table(S, [_orow(S, "e", 1, 0, cur="EUR", missed_fill=-1.0)], -1.5, -0.5, cur="EUR")
    )
    gaps = pnl_gap(t, S)
    assert [(g["currency"], g["direction"]) for g in gaps] == [
        ("EUR", "sim_too_low"),
        ("USD", "sim_too_high"),
        (None, "not_computable"),
    ]
    assert " EUR per order too low" in gaps[0]["headline"]


def test_money_format():
    assert [money(x) for x in (1234.5, -1.5, 0.0, 0.004567, -0.25, None)] == [
        "+1,234.50", "-1.50", "0.00", "+0.00457", "-0.250", "n/a"]  # fmt: skip
    assert money(-0.25, signed=False) == "0.250"
    assert money(0.6, dec=4) == "+0.6000" and money(12.0, dec=3) == "+12.000"


# ---- on real `qt.score_shortfall` rows ---------------------------------------------------------------------------


def _rising():
    """Quotes that rise one tick every 200 ms around 100.00 (a buy filled at the bid makes money a second later)."""
    q = [(T0 - S + i * 200 * MS, PX + i * TICK, PX + (i + 1) * TICK) for i in range(1, 2000)]
    return _bbo(q), make_table("qt.instruments", [_instruments()])


def test_granted_profitable_fills_on_shortfall_rows():
    bbo, inst = _rising()
    orders, fills = [], []
    for i in range(30):
        t0 = T0 + i * 2 * S
        k = f"o{i:02d}"
        o = _order(k, t0=t0, cls="sim_only", sq=10, limit=PX + (i + 6) * TICK)
        o["local_ns"] += (i % 3) * HALF_HOUR  # three 30-minute blocks for the bootstrap
        orders.append(o)
        fills.append(_sf(k, "sim", t0 + 500 * MS, 10, px=PX + (i + 6) * TICK))
    r = shortfall_rows(orders, fills, bbo, inst, [S, 10 * S], bootstrap={"b": 200, "seed": 1})
    (g,) = pnl_gap(r, S)
    assert g["direction"] == "sim_too_high" and g["ci_lo"] > 0
    assert (g["main_part"], g["main_part_majority"]) == ("granted", True)
    assert g["parts_ccy"]["granted"] == pytest.approx(g["mean_ccy"], rel=1e-12)
    mean = next(x for x in r if x["scope"] == "total" and x["statistic"] == "mean" and x["horizon_ns"] == S)
    assert g["mean_ccy"] == mean["total_ccy"]


def test_parts_add_up_on_mixed_shortfall_rows():
    bbo, inst = _rising()
    orders = [
        _order("a", lq=10, sq=10),
        _order("b", cls="sim_only", sq=5, t0=T0 + S),
        _order("c", cls="live_only", lq=4, t0=T0 + 2 * S),
        _order("d", cls="both", lq=4, sq=10, t0=T0 + 3 * S),
        _order("e", cls="neither", t0=T0 + 4 * S),
    ]
    fills = [
        _sf("a", "live", T0 + 2 * S, 10, fee=0.01, ccy="USD"),
        _sf("a", "sim", T0 + S, 10, fee=0.002),
        _sf("b", "sim", T0 + 4 * S, 5),
        _sf("c", "live", T0 + 5 * S, 4),
        _sf("d", "live", T0 + 6 * S, 4),
        _sf("d", "sim", T0 + 5 * S, 10),
    ]
    r = shortfall_rows(orders, fills, bbo, inst, [S], bootstrap={"b": 50})
    (g,) = pnl_gap(r, S)
    by = {x["order_key"]: x for x in r if x["scope"] == "order"}
    p = g["parts_ccy"]
    assert p["granted"] == pytest.approx(by["b"]["missed_fill_ccy"] / 5)
    assert p["denied"] == pytest.approx(by["c"]["missed_fill_ccy"] / 5)
    assert p["partial"] == pytest.approx(by["d"]["partial_fill_ccy"] / 5)
    assert p["fees"] == pytest.approx(by["a"]["fees_ccy"] / 5)
    assert math.fsum(p.values()) == pytest.approx(g["mean_ccy"], rel=1e-12, abs=1e-15)


# ---- the scorer ------------------------------------------------------------------------------------------------


def _spec(tmp_path, live, ds, out="out", **kw):
    return {"live": {"path": str(live)}, "sim": {"kind": "exact"}, "dataset": {"path": str(ds)},
            "out": str(tmp_path / out), "bootstrap": {"b": 50}, "perm_b": 50,
            "markout_horizons_ns": [100 * MS, S, 10 * S], **kw}  # fmt: skip


def test_scorer_writes_pnl_gap_into_the_summary(tmp_path):
    oe = [
        order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)]),
        order_events("o2", "buy", PX, 10, T0 + S, cancel_at=T0 + 30 * S),
        order_events("o3", "sell", PX + TICK, 5, T0 + 2 * S, fills=[(T0 + 5 * S, 5)]),
    ]
    live = log_from(oe, tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    quotes = [{"ts_event": T0 - S + i * 500 * MS, "bid_px": PX + (i % 3 - 1) * TICK, "bid_sz": 5,
               "ask_px": PX + (i % 3) * TICK, "ask_sz": 5} for i in range(200)]  # fmt: skip
    fr = FakeRunner(sim_fills={"o1": [(T0 + S, 10, "queue_depleted")], "o2": [(T0 + 3 * S, 4, "queue_depleted")]},
                    bbo=quotes)  # fmt: skip
    s = build_scorecard(_spec(tmp_path, live, ds), runner=fr)
    assert s.pnl_gap is not None and len(s.pnl_gap) == 1
    (g,) = s.pnl_gap
    assert g.horizon_ns == S and g.currency == "USD" and g.n_valued == 3
    sf = rows(read_parquet(tmp_path / "out" / "score_shortfall.parquet"))
    assert [PnlGap.model_validate(x) for x in pnl_gap(sf, S)] == s.pnl_gap
    doc = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert doc["pnl_gap"][0]["headline"] == g.headline
    assert ScorecardSummary.model_validate(doc).pnl_gap == s.pnl_gap
    # the markout equivalence horizon picks the horizon
    s10 = build_scorecard(_spec(tmp_path, live, ds, out="o10", equivalence={"markout_horizon_ns": 10 * S}), runner=fr)
    assert s10.pnl_gap is not None and s10.pnl_gap[0].horizon_ns == 10 * S
    # without an output directory there is no shortfall table and no P&L gap
    spec = _spec(tmp_path, live, ds)
    spec.pop("out")
    assert build_scorecard(spec, runner=fr).pnl_gap is None


def test_scorer_without_dataset_says_not_computable(tmp_path):
    live = log_from([order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)])], tmp_path / "live")
    sim = log_from([order_events("o1", "buy", PX, 10, T0, fills=[(T0 + S, 10)])], tmp_path / "sim")
    spec = {"live": {"path": str(live)}, "sim": {"kind": "external", "log": {"path": str(sim)}},
            "out": str(tmp_path / "out"), "bootstrap": {"b": 20}, "perm_b": 20}  # fmt: skip
    s = build_scorecard(spec, runner=FakeRunner())
    assert s.pnl_gap is not None and [g.direction for g in s.pnl_gap] == ["not_computable"]


def test_summaries_without_pnl_gap_stay_valid(tmp_path):
    live = log_from([order_events("o1", "buy", PX, 10, T0, fills=[(T0 + 2 * S, 10)])], tmp_path / "live")
    sim = log_from([order_events("o1", "buy", PX, 10, T0, fills=[(T0 + S, 10)])], tmp_path / "sim")
    spec = {"live": {"path": str(live)}, "sim": {"kind": "external", "log": {"path": str(sim)}},
            "out": str(tmp_path / "out"), "bootstrap": {"b": 20}, "perm_b": 20}  # fmt: skip
    build_scorecard(spec, runner=FakeRunner())
    doc = json.loads((tmp_path / "out" / "summary.json").read_text())
    del doc["pnl_gap"]
    assert ScorecardSummary.model_validate(doc).pnl_gap is None
