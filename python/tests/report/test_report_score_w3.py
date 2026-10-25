"""Scorecard fields the report reads (schema 1.1): markouts in bps and net of fees
(`qt.score_tests` rows `markout_mean_bps` / `markout_mean_net_ccy`), the evaluation window in the headline, the
`own_orders_in_l2_depth` blocking gate (trader review E2), NOT SCORED summaries (review F1) and the bootstrap block
wording (review E10)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.report import render, why
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table
from report import synth as rc


def _render(tmp_path: Path, inputs: list[Any], **kw: Any) -> str:
    out = tmp_path / "out" / "report.html"
    render({"inputs": [str(i) for i in inputs], "out": str(out), **kw})
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


def test_markouts_in_bps_and_net_of_fees(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(why, "WHY_PROVIDER", None)
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200)
    p = root / "tests.parquet"
    t = pq.read_table(p)
    rows = t.to_pylist()
    base = next(r for r in rows if r["test_id"] == "markout_mean_taker")
    extra = []
    for tid, v in (("markout_mean_bps", 1.25), ("markout_mean_net_ccy", -0.0042)):
        for arm in ("live", "sim"):
            extra.append({**base, "test_id": tid, "stratum_kind": arm, "stratum": "mid:1000000000", "value": v})
    write_table(pa.Table.from_pylist(rows + extra, schema=t.schema), p, SCHEMAS["qt.score_tests"])
    s = _section(_render(tmp_path, [root]), "markouts")
    assert "Maker markouts in bps and net of fees" in s
    assert '<td>live</td><td>mid</td><td>1 s</td><td class="n">1.25</td>' in s
    assert "-0.0042" in s


def test_window_in_headline_and_ci_blocks(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200, with_why=False)

    def old_headline(doc: dict) -> None:
        doc["verdict"]["headline"] = "sim overfills live by 2.0 pp fill rate; 200 orders"
        doc["spec"]["window_ns"] = 5_000_000_000
        doc["warnings"] = [*doc["warnings"], "ci_block=30min"]

    _edit_summary(root, old_headline)
    html = _render(tmp_path, [root], max_why=0)
    v = _section(html, "verdict")
    assert "sim overfills live by 2.0 pp fill rate; 200 orders (fills within 5 s of entry)" in v
    assert "95% block-bootstrap CI (30-minute blocks)" in _section(html, "two_by_two")


def test_own_orders_in_l2_depth_blocks_the_scorecard(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200, with_why=False)

    def block(doc: dict) -> None:
        doc["spec"]["gates"] = {"max_abs_fill_rate_bias": None, "require_equivalence": False}
        doc["verdict"]["gates_passed"] = False
        doc["verdict"]["failed_gates"] = ["own_orders_in_l2_depth"]
        doc["verdict"]["headline"] = "BLOCKED: own resting orders are in the L2 depth; " + doc["verdict"]["headline"]

    _edit_summary(root, block)
    v = _section(_render(tmp_path, [root], max_why=0), "verdict")
    assert "BLOCKED: your own resting orders are still in the L2 depth" in v
    assert "Gates failed" in v and "No gates configured" not in v  # the scorer's own gate failed


def test_not_scored_summary(tmp_path: Path) -> None:
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200, with_why=False)

    def empty(doc: dict) -> None:
        doc["n_matched"] = 0

    _edit_summary(root, empty)
    v = _section(_render(tmp_path, [root], max_why=0), "verdict")
    assert f"NOT SCORED: 0 of {200:,} live orders scored" in v
    assert v.count("not computable: no order scored") >= 3


def test_schema_1_1_summary_fields_win_over_note_parsing(tmp_path: Path) -> None:
    """1.1 fields (SPEC 3.11 #9) are read directly; the v0.1 note / warning parsers are only a fallback."""
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200, with_why=False)

    def native(doc: dict) -> None:
        doc["discordance_clustered"] = {
            "method": "obuchowski",
            "estimate": 0.0321,
            "ci_lo": 0.01,
            "ci_hi": 0.05,
            "p_value": 0.0123,
            "n_clusters": 9,
            "n_clusters_discordant": 8,
            "design_effect": 1.7,
            "n_effective": 117.6,
        }
        doc["n_twin_unmatched"] = 40
        doc["twin_match_rate"] = 0.8
        doc["n_taker_fills"] = {"live": 7, "sim": 11}
        doc["fills_only"] = False
        doc["warnings"] = [*doc["warnings"], "fills_only: stale v0.1 warning that the 1.1 field overrides"]

    _edit_summary(root, native)
    html = _render(tmp_path, [root], max_why=0)
    v = _section(html, "verdict")
    assert "0.012" in v and "9 clusters" in v
    assert "Self-exclusion matched only 80.0% of live orders" in v and "40 live order(s)" in v
    assert "Fills-only live source" not in v
    t = _section(html, "two_by_two")
    assert "9 (8)" in t and "+3.2 pp" in t
    assert re.search(r"live 7 of [\d,]+ fills, sim 11 of [\d,]+ fills", t)


def test_markouts_bps_fallback_from_score_markouts_columns(tmp_path: Path, monkeypatch: Any) -> None:
    """Without `markout_mean_bps` test rows, the 1.1 per-fill columns `mo_bps` / `mo_net_ccy` give plain means of
    the maker fills (taker fills of `qt.score_fills` excluded)."""
    from queuetruth.schemas.arrow import FULL_SCHEMAS

    monkeypatch.setattr(why, "WHY_PROVIDER", None)
    root = tmp_path / "score"
    rc.make_score_dir(root, n=200)
    p = root / "markouts.parquet"
    t = pq.read_table(p)
    n = t.num_rows
    t = t.append_column("mo_bps", pa.array([2.0] * n, pa.float64()))
    t = t.append_column("mo_net_ccy", pa.array([None] * n, pa.float64()))
    full = FULL_SCHEMAS["qt.score_markouts"]
    write_table(t.cast(full), p, full)
    s = _section(_render(tmp_path, [root], max_why=0), "markouts")
    assert "Maker markouts in bps and net of fees" in s
    assert "Source: per-fill qt.score_markouts columns mo_bps / mo_net_ccy (plain means, no CI)" in s
    assert '<td class="n">2</td>' in s
