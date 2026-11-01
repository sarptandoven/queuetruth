"""The scorecard's P&L gap in the report (`qt.scorecard_summary.pnl_gap`, schema 1.2): an answer per currency next to
the fill-rate answer, the sentence under the verdict headline, the fallback to `qt.score_shortfall` for summaries
without the field, and no P&L answer for blocked or older outputs. Synthetic score directories only."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from queuetruth.report import render
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table
from queuetruth.score._io import make_table
from queuetruth.score.pnl_gap import pnl_gap
from report import synth as rc

S = 1_000_000_000


def _render(tmp_path: Path, root: Path) -> str:
    out = tmp_path / "out" / "report.html"
    render({"inputs": [str(root)], "out": str(out)})
    return out.read_text(encoding="utf-8")


def _section(html: str, sid: str) -> str:
    m = re.search(rf'<section id="{sid}"[^>]*>(.*?)</section>', html, re.S)
    assert m, f"section {sid} missing"
    return m.group(1)


def _edit_summary(root: Path, fn: Any) -> None:
    p = root / "summary.json"
    doc = json.loads(p.read_text())
    fn(doc)
    p.write_text(json.dumps(doc), encoding="utf-8")


def _row(scope: str, stat: str, total: float | None, cur: str = "USD", **kw: Any) -> dict[str, Any]:
    r = {"scope": scope, "statistic": stat, "horizon_ns": S, "currency": cur, "valued": True, "n": 1,
         "total_ccy": total}  # fmt: skip
    buckets = ("delay", "spread", "missed_fill", "adverse_selection", "partial_fill", "fees")
    for c in buckets:
        r[f"{c}_ccy"] = kw.get(c, 0.0)
    r.update({k: v for k, v in kw.items() if k not in buckets})
    return r


def _shortfall(lo: float, hi: float) -> list[dict[str, Any]]:
    """Two orders: one the simulator filled and live did not (+0.9), one both filled (+0.1 of adverse selection)."""
    orders = [
        _row("order", "value", 0.9, missed_fill=0.9, order_key="a", live_qty=0, sim_qty=10),
        _row("order", "value", 0.1, adverse_selection=0.1, order_key="b", live_qty=10, sim_qty=10),
    ]
    tot = [dict(_row("total", s, v), n=2) for s, v in (("sum", 1.0), ("mean", 0.5), ("ci_lo", lo), ("ci_hi", hi))]
    return tot + orders


def _gap(lo: float, hi: float) -> list[dict[str, Any]]:
    return pnl_gap(_shortfall(lo, hi), S)


def test_answer_and_verdict_carry_the_pnl_gap(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200)
    _edit_summary(root, lambda d: d.update(pnl_gap=_gap(0.2, 0.8)))
    html = _render(tmp_path, root)
    ans = _section(html, "answer")
    assert "Does the simulator fill like live trading?" in ans
    assert "Does the simulator&#x27;s P&amp;L match live?" in ans and "P&amp;L TOO HIGH" in ans
    assert ans.index("fill like live trading") < ans.index("P&amp;L match live")
    assert (
        "The simulator&#x27;s P&amp;L is 0.500 USD per order too high (95% CI +0.200 to +0.800) at the 1 s markout, "
        "mostly from fills it grants that live did not get (+0.450 USD per order); 2 orders." in ans
    )
    v = _section(html, "verdict")
    assert "P&amp;L: the simulator&#x27;s P&amp;L is 0.500 USD per order too high" in v


def test_no_difference_found_is_not_a_pass(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200)
    _edit_summary(root, lambda d: d.update(pnl_gap=_gap(-0.1, 0.9)))
    ans = _section(_render(tmp_path, root), "answer")
    assert "NO DIFFERENCE FOUND" in ans and "No P&amp;L difference found at the 1 s markout" in ans
    assert "TOO HIGH" not in ans and "mostly" not in ans


def test_one_answer_per_currency(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200)
    rows = _shortfall(0.2, 0.8) + [{**r, "currency": "EUR"} for r in _shortfall(-0.1, 0.9)]
    _edit_summary(root, lambda d: d.update(pnl_gap=pnl_gap(rows, S)))
    ans = _section(_render(tmp_path, root), "answer")
    assert "P&amp;L match live in EUR?" in ans and "P&amp;L match live in USD?" in ans
    assert ans.index("in EUR?") < ans.index("in USD?")


def test_older_summaries_fall_back_to_the_shortfall_table(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200)
    html = _render(tmp_path, root)  # no pnl_gap and no qt.score_shortfall: no P&L answer
    assert "P&amp;L match live" not in _section(html, "answer")
    t = make_table("qt.score_shortfall", _shortfall(0.2, 0.8), full=True)
    write_table(t, root / "score_shortfall.parquet", SCHEMAS["qt.score_shortfall"])
    ans = _section(_render(tmp_path, root), "answer")
    assert "P&amp;L TOO HIGH" in ans and "0.500 USD per order too high" in ans


def test_blocked_scorecard_has_no_pnl_answer(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200)

    def block(d: dict[str, Any]) -> None:
        d["pnl_gap"] = _gap(0.2, 0.8)
        d["verdict"]["failed_gates"] = ["own_orders_in_l2_depth"]
        d["verdict"]["gates_passed"] = False

    _edit_summary(root, block)
    html = _render(tmp_path, root)
    assert "P&amp;L match live" not in _section(html, "answer")
    assert "P&amp;L: " not in _section(html, "verdict")
