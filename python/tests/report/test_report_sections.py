"""One test per report section (SPEC 5.9), each rendered from synthetic inputs that conform to the frozen schemas;
a section appears only when its input exists."""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
import pytest

from queuetruth.report import fmt as F
from queuetruth.report import render, why
from queuetruth.report.compute import _pi, alloc_profile
from queuetruth.schemas.models import WhyTrace
from report import synth as rc


def _render(tmp_path: Path, inputs: list[Any], **kw: Any) -> tuple[Any, str]:
    out = tmp_path / "out" / "report.html"
    summary = render({"inputs": [str(i) for i in inputs], "out": str(out), **kw})
    return summary, out.read_text(encoding="utf-8")


def _section(html: str, sid: str) -> str:
    m = re.search(rf'<section id="{sid}">(.*?)</section>', html, re.S)
    assert m, f"section {sid} missing"
    return m.group(1)


def test_section_order_full_bundle(rendered: tuple[Any, str]) -> None:
    summary, html = rendered
    assert summary.sections == [
        "header",
        "verdict",
        "data_quality",
        "two_by_two",
        "discordance",
        "markouts",
        "time_to_fill",
        "position",
        "zoo",
        "calibration",
        "learned_model",
        "why",
        "config",
    ]
    pos = [html.index(f'id="{s}"') for s in summary.sections]
    assert pos == sorted(pos)


def test_section_header_inputs_hashes_and_banner(bundle: Any, rendered: tuple[Any, str]) -> None:
    _, html = rendered
    head = re.search(r'<header id="header">(.*?)</header>', html, re.S).group(1)
    for d in (bundle.score, bundle.zoo, bundle.cal, bundle.hazard, bundle.replay):
        for p in sorted(d.iterdir()):
            if p.suffix in (".json", ".parquet"):
                assert rc.sha256(p) in head, p
    assert "Synthetic or fixture data" in head
    assert "synthetic_only" in head
    assert "Scope and limits" in head and "never see each other" in head
    assert "Profile-likelihood intervals are quasi-likelihood" in head


def test_section_header_banner_real_and_unknown(tmp_path: Path) -> None:
    zf = rc.make_zoo_dir(tmp_path / "zoo", n=30)
    rc.make_replay_dir(tmp_path / "rep", zf["sids"], zf["ts_live"], zf["qa"], status="real_data")
    _, html = _render(tmp_path / "a", [tmp_path / "rep"])
    assert "Real data" in html and "Synthetic or fixture data" not in html
    _, html = _render(tmp_path / "b", [tmp_path / "zoo"])
    assert "Data provenance unknown" in html


def test_section_header_banner_vendor_cme(tmp_path: Path) -> None:
    # a Databento CME file: `synthetic_only` is the cme rule set's validation, not the data's provenance (0.1.1)
    zf = rc.make_zoo_dir(tmp_path / "zoo", n=30)
    rc.make_replay_dir(tmp_path / "rep", zf["sids"], zf["ts_live"], zf["qa"], source_format="dbn")
    _, html = _render(tmp_path / "a", [tmp_path / "rep"])
    assert "Vendor data; venue rules validated on synthetic data only" in html
    assert "Synthetic or fixture data" not in html


def test_section_verdict(tmp_path: Path) -> None:
    rc.make_score_dir(tmp_path / "score", n=300)
    summary, html = _render(tmp_path, [tmp_path / "score"])
    assert summary.sections[:2] == ["header", "verdict"]
    v = _section(html, "verdict")
    doc = rc.json_load(tmp_path / "score" / "summary.json")
    assert rc.esc(doc["verdict"]["headline"]) in v
    assert "Gates failed" in v and "max_abs_fill_rate_bias" in v
    assert "ci_block=30min" in v


def test_section_data_quality_replay(tmp_path: Path) -> None:
    zf = rc.make_zoo_dir(tmp_path / "zoo", n=20)
    rc.make_replay_dir(tmp_path / "rep", zf["sids"], zf["ts_live"], zf["qa"])
    summary, html = _render(tmp_path, [tmp_path / "rep"])
    assert summary.sections == ["header", "data_quality", "config"]
    dq = _section(html, "data_quality")
    assert dq.count("Data-quality verdict: warnings") == 1  # data_quality.json deduplicated against the summary
    assert "Parity against source data: pass" in dq
    assert "FIFO rank oracle" in dq and "94.40%" in dq
    assert "unknown_order_refs" in dq and "sequence gap" in dq


def test_section_two_by_two(tmp_path: Path) -> None:
    f = rc.make_score_dir(tmp_path / "score", n=300)
    _, html = _render(tmp_path, [tmp_path / "score"])
    s = _section(html, "two_by_two")
    assert f"{f['b']:,}" in s and f"{f['c']:,}" in s
    assert "live-only (b)" in s and "sim-only (c)" in s
    assert "McNemar exact p (secondary)" in s and "Primary: cluster-robust discordance test" in s
    assert "equivalence margin" in s
    assert "All hypothesis tests" in s and "mcnemar_decile_9" in s


def test_section_discordance(tmp_path: Path) -> None:
    rc.make_score_dir(tmp_path / "score", n=300)
    _, html = _render(tmp_path, [tmp_path / "score"])
    s = _section(html, "discordance")
    assert "Discordant orders by queue decile" in s
    assert "Discordant orders by liquidity bucket" in s
    assert s.count('class="bar s2"') >= 5


def test_section_markouts(tmp_path: Path) -> None:
    rc.make_score_dir(tmp_path / "score", n=300)
    _, html = _render(tmp_path, [tmp_path / "score"])
    s = _section(html, "markouts")
    assert "Mean maker markout by horizon (mid), live vs sim" in s
    assert "Paired first-fill markout difference" in s
    assert "sim-only vs live-only" in s
    assert "RS_h = 2" in s


def test_section_time_to_fill(tmp_path: Path) -> None:
    rc.make_score_dir(tmp_path / "score", n=300)
    _, html = _render(tmp_path, [tmp_path / "score"])
    s = _section(html, "time_to_fill")
    assert "Cumulative incidence of fill, live vs sim" in s
    assert "QQ plot of log time to first fill" in s
    assert "paired permutation p" in s and "assumes independence" in s


def test_section_position_downsampled(tmp_path: Path) -> None:
    root = tmp_path / "pos"
    root.mkdir()
    n = 9000
    ts = (rc.OPEN + np.arange(n) * 1_000_000).tolist()
    rc.write(
        root,
        "position.parquet",
        "qt.score_position",
        {
            "arm": ["live", "sim"] * (n // 2),
            "instrument_id": [7] * n,
            "ts": ts,
            "position": np.cumsum(np.where(np.arange(n) % 3, 1, -1)).tolist(),
        },
    )
    summary, html = _render(tmp_path, [root])
    assert summary.sections == ["header", "position", "config"]
    s = _section(html, "position")
    paths = re.findall(r'<path class="ln s[12]" d="([^"]+)"', s)
    assert len(paths) == 2
    for d in paths:
        assert d.count("H") <= 1999  # at most 2,000 points per series
    assert "instrument 7" in s


def test_section_zoo(tmp_path: Path) -> None:
    rc.make_zoo_dir(tmp_path / "zoo", n=120)
    summary, html = _render(tmp_path, [tmp_path / "zoo"])
    assert "zoo" in summary.sections
    s = _section(html, "zoo")
    doc = rc.json_load(tmp_path / "zoo" / "summary.json")
    # trader review C1: sorted by absolute fill-rate bias at 60 s (Brier ties at short horizons), bias at every
    # horizon, one plain-English sentence per model
    h60 = str(60 * 10**9)

    def key(e: dict) -> tuple:
        v = e["fill_rate_bias"][h60]["value"]
        return (v is None, abs(v) if v is not None else math.inf, e["brier"].get(h60) or math.inf, e["model_id"])

    ranked = sorted(doc["leaderboard"], key=key)
    rows = re.findall(r"<tr><td>([^<]+)</td>", s.split("<tbody>", 1)[1])
    assert rows[0] == "exact fill rate (truth)"  # the base rate of every bias in its column (0.1.1)
    assert rc.esc(ranked[0]["model_id"]) == rows[1]
    assert "sorted by absolute fill-rate bias at 60 s" in s
    for h in [*rc.HORIZONS, rc.H_MAX]:
        assert f'<th scope="col" class="n">fill-rate bias {F.dur(h)}</th>' in s
    sentences = re.findall(r"<li>(At 60 s, [^<]+)</li>", s)
    assert len(sentences) == len(rc.MODELS)
    assert sentences[0].startswith(f"At 60 s, {rc.esc(ranked[0]['model_id'])} ")
    v0 = ranked[0]["fill_rate_bias"][h60]
    word = "more" if v0["value"] > 0 else "less"
    assert f"pp {word} often" in sentences[0] or "as often as" in sentences[0]
    assert "in the exact replay" in sentences[0] or "as often as" in sentences[0]
    nh = len(rc.HORIZONS) + 1  # the real evaluation scores every horizon up to and including h_max
    assert s.count("Signed fill-rate bias by model at") == nh + 1  # never-cancel per horizon + cancel-bearing
    assert s.count('class="chart rel"') == len(rc.MODELS) * nh
    assert s.count("Fill-rate bias by queue decile at") == nh
    assert "horizon 1 s · n = " in s
    assert "qt.zoo_errors (block-bootstrap CIs in the tooltips; blocks are session days, or 30-minute blocks" in s


def test_section_zoo_decile_fallback_and_empty_leaderboard(tmp_path: Path) -> None:
    rc.make_zoo_dir(tmp_path / "zoo", n=80, with_errors=False, with_traj=False)
    doc = rc.json_load(tmp_path / "zoo" / "summary.json")
    doc["leaderboard"] = []
    (tmp_path / "zoo" / "summary.json").write_text(rc.json.dumps(doc), encoding="utf-8")
    _, html = _render(tmp_path, [tmp_path / "zoo"])
    s = _section(html, "zoo")
    assert "Leaderboard not computed" in s
    assert "computed by the report from qt.zoo_predictions" in s
    assert s.count('class="chart rel"') >= len(rc.MODELS)


def test_section_calibration(tmp_path: Path) -> None:
    f = rc.make_cal_dir(tmp_path / "cal")
    summary, html = _render(tmp_path, [tmp_path / "cal"])
    assert summary.sections == ["header", "calibration", "config"]
    s = _section(html, "calibration")
    assert s.count("alloc · power3") == 1  # fit.json is superseded by calibration.json
    assert "Allocation quasi-log-likelihood profile (power3)" in s
    assert rc.esc(f["snippet"].splitlines()[4].strip()) in s
    assert "Held-out Brier score: fitted vs default parameters" in s
    assert "Stratified estimates" in s


def test_calibration_profile_matches_spec_formula() -> None:
    # SPEC 5.3.3 golden pi values (F = 30, back = prev - F = 70)
    f, b = np.array([30.0]), np.array([70.0])
    assert _pi("power", math.log(2), f, b)[0] == pytest.approx(0.8448275862068966, rel=1e-12)
    assert _pi("power2", math.log(2), f, b)[0] == pytest.approx(0.49, rel=1e-12)
    assert _pi("power3", math.log(2), f, b)[0] == pytest.approx(0.91, rel=1e-12)
    rng = np.random.default_rng(11)
    m = 20000
    front, back = rng.uniform(1, 400, m), rng.uniform(1, 400, m)
    tot = rng.integers(1, 10, m)
    behind = rng.binomial(tot, 1.0 - (front / (front + back)) ** 3.0)
    prof = alloc_profile("power3", front, back, tot - behind, behind, (-3.0, 4.0, 701))
    t_hat = max(prof, key=lambda tv: tv[1])[0]
    assert math.exp(t_hat) == pytest.approx(3.0, rel=0.05)


def test_section_learned_model(tmp_path: Path) -> None:
    rc.make_hazard_dir(tmp_path / "hz")
    summary, html = _render(tmp_path, [tmp_path / "hz"])
    assert summary.sections == ["header", "learned_model", "config"]
    s = _section(html, "learned_model")
    assert "Brier skill vs climatology by horizon" in s
    assert s.count('class="chart rel"') == 4
    assert "best zoo: " + rc.esc(rc.MODELS[1]) in s
    assert "improper" in s


def test_section_why_doc_trajectory_and_order(bundle: Any, rendered: tuple[Any, str]) -> None:
    _, html = rendered
    s = _section(html, "why")
    items = re.findall(r'<div class="why" id="why-\d+"><h3>\d+\. ([^<]+)</h3>', s)
    assert len(items) == 6
    assert items[0] == f"Order {bundle.facts['score']['sim_only_key']}"
    assert "qt.why_trace document in the inputs" in s
    assert "Exact queue trajectory" in s  # zoo shadows traced from the replay's qt.queue_trajectory
    first = s.index('id="why-1"')
    assert "filled in the simulator only" in s[first : first + 600]


def test_section_why_engine_provider_and_cli_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ds, lv = tmp_path / "ds", tmp_path / "live"
    ds.mkdir()
    lv.mkdir()
    rc.make_score_dir(tmp_path / "score", n=200, with_why=False, dataset=str(ds), live=str(lv))
    calls: list[dict[str, Any]] = []

    def fake(spec: dict[str, Any]) -> WhyTrace:
        calls.append(spec)
        return WhyTrace(
            subject={"order_key": spec["order_key"]},
            instrument_id=1,
            side="B",
            price=1,
            qty=1,
            outcome={"filled_qty": 0, "end_state": "cancelled"},
            events=[
                {
                    "ts_event": rc.OPEN + k,
                    "sequence": k,
                    "action": "C",
                    "order_id": k,
                    "side": "B",
                    "price": 1,
                    "size": 1,
                    "effect": "remove",
                    "is_ahead": True,
                    "queue_ahead_after": 5 - k,
                    "rank_after": 5 - k,
                    "level_qty_after": 9,
                    "fill_qty": 0,
                }
                for k in range(4)
            ],
            truncated=False,
        )

    monkeypatch.setattr(why, "WHY_PROVIDER", fake)
    _, html = _render(tmp_path / "a", [tmp_path / "score"], max_why=3)
    assert len(calls) == 3 and all(c["live"] == {"path": str(lv)} and "order_key" in c for c in calls)
    assert "exact engine (queuetruth why)" in _section(html, "why")
    monkeypatch.setattr(why, "WHY_PROVIDER", None)
    _, html = _render(tmp_path / "b", [tmp_path / "score"], max_why=2)
    s = _section(html, "why")
    assert "No event-level trace in the inputs" in s and "queuetruth why --data" in s
    summary, _ = _render(tmp_path / "c", [tmp_path / "score"], max_why=0)
    assert "why" not in summary.sections


def test_section_config_and_integrity(tmp_path: Path, bundle: Any) -> None:
    shutil.copytree(bundle.zoo, tmp_path / "zoo")
    t = pq.read_table(tmp_path / "zoo" / "zoo_errors.parquet")
    pq.write_table(t.slice(0, 100), tmp_path / "zoo" / "zoo_errors.parquet")
    _, html = _render(tmp_path, [tmp_path / "zoo", bundle.cal])
    s = _section(html, "config")
    assert "Spec echo" in s and "&quot;family&quot;: &quot;power3&quot;" in s
    assert "CHANGED" in s
    assert "Files changed after their summary was written" in html


def test_parent_directory_expands_to_runs(tmp_path: Path, bundle: Any) -> None:
    summary, _ = _render(tmp_path, [bundle.root])
    assert {"verdict", "zoo", "calibration", "learned_model"} <= set(summary.sections)
