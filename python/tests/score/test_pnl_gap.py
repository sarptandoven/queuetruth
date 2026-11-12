"""The P&L gap of a scorecard (`queuetruth.score.pnl_gap`, `qt.scorecard_summary.pnl_gap`, schema 1.2): the per-order
mean of `qt.score_shortfall.total_ccy` with its CI, a finding only when the 95% CI excludes 0, the granted / denied /
timing / fees parts adding up to the gap with CIs on the total's draws, when a part is named, fills-only logs,
reasons, the horizon choice, currencies, the plain sentence, and the scorer writing it into `summary.json`.
Synthetic quotes and orders only."""

from __future__ import annotations

import json
import math

import pytest

from queuetruth.schemas.models import PnlGap, ScorecardSummary
from queuetruth.score import build_scorecard
from queuetruth.score._io import make_table, read_parquet, rows
from queuetruth.score.pnl_gap import (
    FILLS_ONLY_REASON,
    PART_WORDS,
    headline,
    main_part,
    money,
    part_values,
    pick_horizon,
    pnl_gap,
    pnl_gap_table,
)
from queuetruth.score.shortfall import block_mean_cis, shortfall_rows, shortfall_table, stream_id
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


def _scorer(keys: list[str]) -> list[dict]:
    """Scorer order rows for `keys`, spread over three 30-minute blocks of one day (the bootstrap's clusters)."""
    out = []
    for i, k in enumerate(keys):
        o = _order(k, t0=T0 + i * S)
        o["local_ns"] += (i % 3) * HALF_HOUR
        out.append(o)
    return out


def _gap(orders: list[dict], lo: float | None, hi: float | None, scored: bool = True, **kw) -> dict:
    keys = [o["order_key"] for o in orders]
    (g,) = pnl_gap(_table(S, orders, lo, hi), S, order_rows=_scorer(keys) if scored else None,
                   bootstrap={"b": 400, "seed": 5}, **kw)  # fmt: skip
    PnlGap.model_validate(g)
    return g


def test_part_values_split_partial_fills_by_direction():
    over = _orow(S, "o", 4, 10, partial_fill=0.3, delay=0.1, spread=-0.1, adverse_selection=0.05, fees=-0.01)
    under = _orow(S, "u", 10, 4, partial_fill=-0.2)
    assert part_values(over) == pytest.approx({"granted": 0.3, "denied": 0.0, "timing": 0.05, "fees": -0.01})
    assert part_values(under) == pytest.approx({"granted": 0.0, "denied": -0.2, "timing": 0.0, "fees": 0.0})
    assert part_values(_orow(S, "g", 0, 10, missed_fill=0.9))["granted"] == 0.9
    assert part_values(_orow(S, "d", 10, 0, missed_fill=-0.6))["denied"] == -0.6
    for r in (over, under):
        assert math.fsum(part_values(r).values()) == pytest.approx(r["total_ccy"])


def test_finding_names_the_granted_fills_and_parts_add_up():
    orders = [_orow(S, f"g{i}", 0, 10, missed_fill=0.9) for i in range(9)]
    orders += [_orow(S, "b", 10, 0, missed_fill=-0.1), _orow(S, "c", 10, 10, adverse_selection=0.2, fees=0.01),
               _orow(S, "d", 10, 4, partial_fill=-0.03)]  # fmt: skip
    g = _gap(orders, 0.4, 0.9)
    assert g["direction"] == "sim_too_high" and g["horizon_ns"] == S and g["currency"] == "USD"
    assert g["n_valued"] == 12 and g["n_unvalued"] == 0 and g["reason"] is None
    p = g["parts_ccy"]
    assert p == pytest.approx({"granted": 8.1 / 12, "denied": -0.13 / 12, "timing": 0.2 / 12, "fees": 0.01 / 12})
    assert math.fsum(p.values()) == pytest.approx(g["mean_ccy"], rel=1e-12)
    assert g["parts_ci_lo"]["granted"] > 0 and g["main_part"] == "granted"
    assert g["headline"] == (
        "the simulator's P&L is 0.682 USD per order too high (95% CI +0.400 to +0.900) at the 1s markout, mostly "
        "from fills it grants that live did not get (+0.675 USD per order); 12 orders"
    )


def test_too_low_mostly_from_fills_it_misses_in_whole_or_in_part():
    # the shape of the how-to's hbt_power3 run: timing is the largest single bucket, but whole missed fills and
    # short fills together are the larger part
    orders = [_orow(S, f"t{i}", 10, 10, adverse_selection=-0.34 * 3) for i in range(3)]
    orders += [_orow(S, f"m{i}", 10, 0, missed_fill=-0.6) for i in range(3)]
    orders += [_orow(S, f"p{i}", 10, 4, partial_fill=-0.9) for i in range(3)]
    g = _gap(orders, -1.2, -0.4)
    assert g["direction"] == "sim_too_low" and g["main_part"] == "denied"
    assert g["parts_ccy"]["denied"] == pytest.approx(-0.5) and g["parts_ccy"]["timing"] == pytest.approx(-0.34)
    assert "P&L is 0.840 USD per order too low (95% CI -1.200 to -0.400)" in g["headline"]
    assert "mostly from fills live got that it misses (-0.500 USD per order); 9 orders" in g["headline"]


def test_a_part_from_one_order_is_not_named():
    # one order carries the whole granted part: its own CI reaches 0, so the finding names no part
    orders = [_orow(S, "g", 0, 10, missed_fill=9.0)] + [_orow(S, f"n{i}", 0, 0) for i in range(11)]
    g = _gap(orders, 0.1, 1.5)
    assert g["direction"] == "sim_too_high" and g["parts_ci_lo"]["granted"] == 0.0
    assert g["main_part"] is None and "mostly" not in g["headline"]


def test_no_part_named_without_order_rows_or_when_parts_offset():
    orders = [_orow(S, f"g{i}", 0, 10, missed_fill=0.9) for i in range(9)]
    g = _gap(orders, 0.5, 1.2, scored=False)  # the report's fallback for older summaries
    assert g["parts_ci_lo"] == dict.fromkeys(PART_WORDS) and g["main_part"] is None
    assert g["headline"].endswith("at the 1s markout; 9 orders")
    # timing +0.4 and granted +0.35 push up, denied -0.45 pulls down: the gap is +0.3 and no part is most of it
    parts = {"granted": 0.35, "denied": -0.45, "timing": 0.4, "fees": 0.0}
    sure = dict.fromkeys(PART_WORDS, (0.01, 1.0))
    assert main_part(0.3, parts, sure) is None
    assert main_part(0.3, {**parts, "timing": 1.5}, sure) == "timing"  # 1.5 of 2.3
    assert main_part(0.3, {**parts, "timing": 1.5}, {**sure, "timing": (-0.1, 2.0)}) is None
    assert main_part(-0.3, {**parts, "denied": -1.5}, {**sure, "denied": (-2.0, -0.1)}) == "denied"
    assert main_part(0.0, parts, sure) is None


def test_ci_with_zero_is_no_difference_found():
    orders = [_orow(S, "a", 0, 10, missed_fill=0.9), _orow(S, "b", 10, 0, missed_fill=-0.6)]
    g = _gap(orders, -0.4, 0.7)
    assert g["direction"] == "no_significant_difference" and g["main_part"] is None
    h = g["headline"]
    assert h.startswith("no P&L difference found at the 1s markout: the simulator's P&L is +0.150 USD per order")
    assert "95% CI -0.400 to +0.700 includes 0; 2 orders)" in h
    assert "too high" not in h and "mostly" not in h
    # a CI touching 0 does not exclude it
    assert _gap(orders, 0.0, 0.7)["direction"] == "no_significant_difference"


def test_no_ci_is_never_a_finding():
    g = _gap([_orow(S, "a", 0, 10, missed_fill=5.0)], None, None)
    assert g["direction"] == "no_significant_difference"
    assert "no CI so no difference can be shown; 1 order)" in g["headline"]


def test_ci_bounds_never_print_as_zero():
    g = _gap([_orow(S, f"a{i}", 0, 10, missed_fill=1.5) for i in range(3)], 0.004, 2.9)
    assert g["direction"] == "sim_too_high"
    assert "too high (95% CI +0.00400 to +2.90000)" in g["headline"] and "+0.00 " not in g["headline"]


def test_fills_only_live_log_is_not_answerable():
    orders = [_orow(S, f"d{i}", 10, 0, missed_fill=-2.0) for i in range(6)]
    g = _gap(orders, -2.5, -1.5, fills_only=True)
    assert g["direction"] == "not_answerable" and g["main_part"] is None and g["mean_ccy"] == -2.0
    assert g["reason"] == FILLS_ONLY_REASON
    assert g["headline"].startswith("P&L gap not answerable at the 1s markout: the live log holds fills but not")
    assert "too low" not in g["headline"]


def test_not_computable_gives_the_reason():
    orders = [_orow(S, "a", 10, 0, cur=None, valued=False), _orow(S, "b", 0, 0, cur=None, valued=False),
              _orow(S, "c", 0, 0, cur=None, valued=False)]  # fmt: skip
    orders[0]["note"] = orders[1]["note"] = "no dataset quotes"
    orders[2]["note"] = "no quote at t + h (fill at 123)"
    (g,) = pnl_gap(_table(S, orders, None, None, cur=None), S)
    assert g["direction"] == "not_computable" and g["mean_ccy"] is None and g["currency"] is None
    assert (g["n_valued"], g["n_unvalued"]) == (0, 3)
    assert g["parts_ccy"] == dict.fromkeys(PART_WORDS)
    assert g["reason"] == "no dataset quotes: 2; no quote at t + h: 1"
    assert g["headline"] == (
        "P&L not computable at the 1s markout: no scored order could be valued (no dataset quotes: 2; no quote at "
        "t + h: 1)"
    )
    PnlGap.model_validate(g)
    assert pnl_gap([], S) == []


def test_horizon_choice():
    assert pick_horizon([50 * MS, S, 10 * S], S) == S
    assert pick_horizon([50 * MS, 10 * S], S) == 50 * MS
    assert pick_horizon([500 * MS, 1500 * MS], S) == 500 * MS  # a tie goes to the shorter horizon
    assert pick_horizon([], S) is None
    orders = [_orow(h, "a", 0, 1, missed_fill=1.0) for h in (50 * MS, 10 * S)]
    t = _table(50 * MS, orders[:1], 0.5, 1.5) + _table(10 * S, orders[1:], 0.5, 1.5)
    assert [g["horizon_ns"] for g in pnl_gap(t, S)] == [50 * MS]
    assert [g["horizon_ns"] for g in pnl_gap(t, 60 * S)] == [10 * S]


def test_one_entry_per_currency_and_no_currency_group_dropped():
    unk = _orow(S, "n", 1, 0, cur=None, valued=False)
    unk["note"] = "instrument not in the dataset"
    t = (
        _table(S, [_orow(S, "u", 0, 1, cur="USD", missed_fill=1.0)], 0.5, 1.5, cur="USD")
        + _table(S, [unk], None, None, cur=None)
        + _table(S, [_orow(S, "e", 1, 0, cur="EUR", missed_fill=-1.0)], -1.5, -0.5, cur="EUR")
    )
    gaps = pnl_gap(t, S)
    # orders of an instrument missing from the dataset have no currency: no misleading "not computable" line
    assert [(g["currency"], g["direction"]) for g in gaps] == [("EUR", "sim_too_low"), ("USD", "sim_too_high")]
    assert " EUR per order too low" in gaps[0]["headline"]
    (only,) = pnl_gap(_table(S, [unk], None, None, cur=None), S)  # alone, it is the answer
    assert only["direction"] == "not_computable" and only["reason"] == "instrument not in the dataset: 1"


def test_money_format():
    assert [money(x) for x in (1234.5, -1.5, 0.0, 0.004567, -0.25, None)] == [
        "+1,234.50", "-1.50", "0.00", "+0.00457", "-0.250", "n/a"]  # fmt: skip
    assert money(-0.25, signed=False) == "0.250"
    assert money(0.6, dec=4) == "+0.6000" and money(12.0, dec=3) == "+12.000"
    g = {"horizon_ns": S, "currency": None, "n_valued": 1, "n_unvalued": 1, "mean_ccy": 0.5, "ci_lo": 0.1,
         "ci_hi": 0.9, "direction": "sim_too_high", "main_part": None}  # fmt: skip
    assert headline(g).endswith("; 1 order; 1 unvalued order(s) left out")


# ---- on real `qt.score_shortfall` rows ---------------------------------------------------------------------------


def _rising():
    """Quotes that rise one tick every 200ms around 100.00 (a buy filled at the bid makes money a second later)."""
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
    boot = {"b": 200, "seed": 1}
    r = shortfall_rows(orders, fills, bbo, inst, [S, 10 * S], bootstrap=boot)
    (g,) = pnl_gap(r, S, order_rows=orders, bootstrap=boot)
    assert g["direction"] == "sim_too_high" and g["ci_lo"] > 0
    assert g["main_part"] == "granted" and g["parts_ci_lo"]["granted"] > 0
    assert g["parts_ccy"]["granted"] == pytest.approx(g["mean_ccy"], rel=1e-12)
    tot = {x["statistic"]: x for x in r if x["scope"] == "total" and x["horizon_ns"] == S}
    assert g["mean_ccy"] == tot["mean"]["total_ccy"]
    # the parts' CIs are on the same draws as the table's: the total's CI recomputed that way is the table's
    vals = [x["total_ccy"] for x in r if x["scope"] == "order" and x["horizon_ns"] == S]
    got, _, _ = block_mean_cis(orders, {"total": vals}, 200, "30min", 1, stream_id("USD", S))
    assert got["total"] == (tot["ci_lo"]["total_ccy"], tot["ci_hi"]["total_ccy"])
    # converting only the chosen horizon gives the same entries
    t = shortfall_table(orders, fills, bbo, inst, [S, 10 * S], bootstrap=boot)
    assert pnl_gap_table(t, S, order_rows=orders, bootstrap=boot) == [g]


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
    # d: the simulator filled 10 where live filled 4, so its extra units are granted fills
    assert by["d"]["partial_fill_ccy"] != 0
    assert p["granted"] == pytest.approx((by["b"]["missed_fill_ccy"] + by["d"]["partial_fill_ccy"]) / 5)
    assert p["denied"] == pytest.approx(by["c"]["missed_fill_ccy"] / 5)
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
    (x,) = pnl_gap(sf, S)  # the report's fallback: the same gap, no part CIs
    assert (x["mean_ccy"], x["ci_lo"], x["ci_hi"], x["direction"]) == (g.mean_ccy, g.ci_lo, g.ci_hi, g.direction)
    assert x["parts_ccy"] == g.parts_ccy.model_dump()
    assert g.parts_ci_lo.granted is not None and g.parts_ci_hi.timing is not None
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
