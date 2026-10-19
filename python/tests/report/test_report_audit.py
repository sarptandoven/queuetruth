"""The report on the real scorer, zoo evaluation, referee and calibrate-live outputs, and the audit rulings: A2
(never-cancel leaderboard primary, cancel-bearing reported separately, Aalen-Johansen truth), A5 (taker fills
separate), A10 (cluster-robust primary discordance test, plain McNemar secondary), fills-only sources, the referee
section over the 16 bug classes, calibrate-live held-out rows, and the zoo output file names.

Real outputs come from `queuetruth.score` / `queuetruth.eval` with the engine replaced by the scorer's own test
double (`score.qt_score_helpers.FakeRunner`), so no Rust build is needed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from score.qt_score_helpers import DAY, T0, FakeRunner, log_from, make_dataset, make_table, order_events

from queuetruth.report import fmt as F
from queuetruth.report import render
from queuetruth.schemas.arrow import TAKER_REASONS
from queuetruth.score import build_scorecard, calibrate_live
from queuetruth.score._io import read_parquet, rows
from queuetruth.score.referee import compare_referee
from report import synth as rc

S = 1_000_000_000
PX = 100_000_000_000
BBO = [{"ts_event": T0 - S, "bid_px": PX, "bid_sz": 5, "ask_px": PX + 10_000_000, "ask_sz": 5}]
NC = "not computable: fills-only source"


def _render(out_dir: Path, inputs: list[Any], **kw: Any) -> tuple[Any, str]:
    out = out_dir / "report.html"
    summary = render({"inputs": [str(i) for i in inputs], "out": str(out), **kw})
    return summary, out.read_text(encoding="utf-8")


def _section(html: str, sid: str) -> str:
    m = re.search(rf'<section id="{sid}">(.*?)</section>', html, re.S)
    assert m, f"section {sid} missing"
    return m.group(1)


def _end_mbo() -> list[dict[str, Any]]:
    return [{"ts_event": T0 + 3600 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}]


def _scorecard(tmp_path: Path, *, fills_only: bool = False, n_days: int = 6) -> Path:
    """A real scorecard over n_days days of orders; every 4th sim fill is a taker fill (reason crossed_at_live; the
    binding A5 refinement makes a later `crossed` fill maker)."""
    oe, sim = [], {}
    for d in range(n_days):
        for i in range(8):
            t = T0 + d * DAY + i * 60 * S
            key = f"d{d}o{i}"
            live_fill = i % 2 == 0
            oe.append(
                order_events(
                    key,
                    "buy",
                    PX,
                    10,
                    t,
                    fills=[(t + 2 * S, 10)] if live_fill else None,
                    cancel_at=None if live_fill else t + 30 * S,
                )
            )
            if live_fill or i % 3 == 1:
                sim[key] = [(t + 3 * S, 10, "crossed_at_live" if i % 4 == 1 else "queue_depleted")]
    extra = {"coverage": "fills_only"} if fills_only else None
    if fills_only:
        oe = [x for x in oe if any(e["event_type"] == "fill" for e in x[1])]
    live = log_from(oe, tmp_path / "live", manifest_extra=extra)
    mbo = [{**_end_mbo()[0], "ts_event": T0 + d * DAY + 3600 * S} for d in range(n_days)]
    ds = make_dataset(tmp_path / "ds", mbo, n_days=n_days)
    out = tmp_path / "score"
    build_scorecard(
        {
            "live": {"path": str(live)},
            "sim": {"kind": "exact"},
            "dataset": {"path": str(ds)},
            "bootstrap": {"b": 60},
            "perm_b": 60,
            "out": str(out),
        },
        runner=FakeRunner(sim_fills=sim, bbo=[{**BBO[0], "ts_event": T0 + d * DAY - S} for d in range(n_days)]),
    )
    return out


def test_a10_primary_cluster_robust_test_real_scorer(tmp_path: Path) -> None:
    out = _scorecard(tmp_path)
    t = [r for r in rows(read_parquet(out / "score_tests.parquet")) if r["test_id"] == "discordance_clustered"]
    assert len(t) == 1 and t[0]["family"] == "primary"
    _, html = _render(tmp_path, [out])
    v = _section(html, "verdict")
    assert "Discordance test, cluster-robust (primary)" in v
    assert "McNemar exact p (secondary)" in v and "assumes independent orders" in v
    assert rc.esc(F.pval(t[0]["p_value"])) in v
    m = re.search(r"clusters=(\d+)", t[0]["note"])
    assert m and f"{int(m.group(1))} clusters" in v
    s = _section(html, "two_by_two")
    assert "Primary: cluster-robust discordance test" in s and "Secondary: plain McNemar" in s
    assert "design effect" in s and "effective n" in s


def test_a5_taker_fills_separate_real_scorer(tmp_path: Path) -> None:
    out = _scorecard(tmp_path)
    fills = rows(read_parquet(out / "score_fills.parquet"))
    n_taker = sum(1 for f in fills if f["arm"] == "sim" and f["liquidity"] == "taker")
    n_sim = sum(1 for f in fills if f["arm"] == "sim")
    assert n_taker > 0
    _, html = _render(tmp_path, [out])
    assert f"sim {n_taker:,} of {n_sim:,}" in _section(html, "two_by_two")
    mk = _section(html, "markouts")
    assert "Mean maker markout by horizon" in mk
    assert "Taker fills (book crossed the order), reported separately" in mk
    taker_rows = [r for r in rows(read_parquet(out / "score_tests.parquet")) if r["test_id"] == "markout_mean_taker"]
    assert taker_rows and "Mean taker markout by horizon (mid)" in mk


def test_fills_only_source_real_scorer(tmp_path: Path) -> None:
    out = _scorecard(tmp_path, fills_only=True)
    doc = json.loads((out / "summary.json").read_text())
    assert NC in doc["verdict"]["headline"]
    summary, html = _render(tmp_path, [out])
    v = _section(html, "verdict")
    assert v.count(NC) >= 4  # bias, primary test, McNemar, equivalence tiles (+ headline)
    assert NC in _section(html, "two_by_two")
    assert NC in _section(html, "discordance")
    t = _section(html, "time_to_fill")
    assert f"Cumulative incidence of fill: {NC}" in t
    assert "Cumulative incidence of fill, live vs sim" not in t
    assert "markouts" in summary.sections


def test_a2_never_cancel_leaderboard_and_cancel_bearing(tmp_path: Path) -> None:
    rc.make_zoo_dir(tmp_path / "zoo", n=150)
    errs = rows(read_parquet(tmp_path / "zoo" / "zoo_errors.parquet"))
    assert any(r["metric"].startswith("cancel_bearing/") for r in errs)
    _, html = _render(tmp_path, [tmp_path / "zoo"])
    s = _section(html, "zoo")
    assert "Leaderboard: never-cancel shadows" in s
    assert "Cancel-bearing shadows (mirror orders with real cancel times), reported separately" in s
    assert "planned life of qt.zoo_shadows (audit ruling A2)" in s
    lb = s.split("Cancel-bearing shadows")[0]
    aj = next(
        r
        for r in errs
        if r["metric"] == "fill_rate_bias_aj" and r["horizon_ns"] == 10**9 and r["stratum_kind"] == "all"
    )
    assert rc.esc(F.pp(aj["value"])) in lb
    assert "bias vs Aalen-Johansen CIF" in s
    pop = re.search(r"population \(zoo_shadows\)</dt><dd>([^<]+)</dd>", s).group(1)
    assert pop == "120 never-cancel / 30 cancel-bearing"
    dp = next(
        r
        for r in errs
        if r["metric"] == "discordance_p_primary" and r["stratum_kind"] == "all" and r["model_id"] == rc.MODELS[0]
    )
    assert rc.esc(F.pval(dp["value"])) in lb


def test_a5_zoo_taker_fills_by_model(tmp_path: Path) -> None:
    rc.make_zoo_dir(tmp_path / "zoo", n=80)
    fl = rows(read_parquet(tmp_path / "zoo" / "zoo_fills.parquet"))
    m = rc.MODELS[0]
    n_t = sum(1 for f in fl if f["model_id"] == m and f["reason"] in TAKER_REASONS)
    assert n_t > 0
    _, html = _render(tmp_path, [tmp_path / "zoo"])
    s = _section(html, "zoo")
    assert "Taker fills by model (book crossed the order)" in s
    row = re.search(rf"<td>{re.escape(rc.esc(m))}</td><td class=\"n\">([\d,]+)</td><td class=\"n\">([\d,]+)</td>", s)
    assert row and int(row.group(2).replace(",", "")) == n_t


def test_zoo_output_names(tmp_path: Path) -> None:
    rc.make_zoo_dir(tmp_path / "zoo", n=40)
    names = sorted(p.name for p in (tmp_path / "zoo").iterdir())
    assert names == sorted(
        [
            "zoo_predictions.parquet",
            "zoo_fills.parquet",
            "zoo_trajectory.parquet",
            "zoo_shadows.parquet",
            "zoo_errors.parquet",
            "summary.json",
        ]
    )
    _, html = _render(tmp_path, [tmp_path / "zoo"])
    head = re.search(r'<header id="header">(.*?)</header>', html, re.S).group(1)
    for n in names:
        assert f"zoo/{n}" in head
    assert "qt.zoo_shadows" in head


def _referee_dir(tmp_path: Path, repo_root: Path, cls: str, break_it: bool) -> Path:
    fx = repo_root / "tests" / "fixtures" / "referee" / cls
    res = pq.read_table(fx / "expected_results.parquet")
    fills = pq.read_table(fx / "expected_fills.parquet")
    sim = fills.slice(0, 0) if break_it else fills
    rep = compare_referee(
        res,
        fills,
        sim,
        {},
        spec={"dataset": {"path": str(fx / "dataset")}},
        simulator={"name": "toysim", "version": "1.0"},
    )
    out = tmp_path / f"ref_{cls}"
    out.mkdir()
    (out / "referee_report.json").write_text(rep.model_dump_json(indent=2), encoding="utf-8")
    return out


def test_referee_section_bug_class_matrix(tmp_path: Path, repo_root: Path) -> None:
    if not (repo_root / "tests" / "fixtures" / "referee" / "16_crossed_at_live_takes_opposite").is_dir():
        pytest.skip("referee fixtures missing (make fixtures)")
    a = _referee_dir(tmp_path, repo_root, "16_crossed_at_live_takes_opposite", break_it=False)
    b = _referee_dir(tmp_path, repo_root, "02_level_shrink_caps_queue", break_it=True)
    summary, html = _render(tmp_path, [a, b])
    assert "referee" in summary.sections
    s = _section(html, "referee")
    assert "2 of 16 bug classes run, 1 failed" in s
    assert s.count("not run") == 14
    assert "16. crossed at live takes opposite" in s and "02. level shrink caps queue" in s
    readme = (repo_root / "tests/fixtures/referee/16_crossed_at_live_takes_opposite/README.txt").read_text().strip()
    assert rc.esc(readme[:60]) in s
    assert "toysim 1.0" in s
    assert "exact-only fills" in s


class _ZooByParam(FakeRunner):
    """Model with parameter n fills each shadow at 10 s / n (the calibrate-live fake zoo runner)."""

    def run_zoo(self, spec: Any) -> None:
        from queuetruth.schemas.io import write_table

        out = Path(spec.out)
        out.mkdir(parents=True, exist_ok=True)
        preds = []
        for s in rows(read_parquet(spec.shadows.path)):
            for mid in spec.models:
                v = float(re.search(r"n=([^;\]]+)", mid).group(1))
                for h in [*spec.horizons_ns, spec.h_max_ns]:
                    p = 1.0 if 10 * S / v <= h else 0.0
                    preds.append(
                        {
                            "model_id": mid.replace("]", ";mode=faithful]"),
                            "shadow_id": s["shadow_id"],
                            "qty": s["qty"],
                            "horizon_ns": h,
                            "p_fill": p,
                            "e_fill_qty": p * s["qty"],
                            "censored": False,
                            "visible_level_qty_at_live": 0,
                            "out_of_view_at_live": False,
                            "instrument_id": 1,
                            "session_date": 0,
                            "ts_live": s["ts_submit"],
                            "trade_vol_60s": 0,
                        }
                    )
        write_table(make_table("qt.zoo_predictions", preds), out / "zoo_predictions.parquet")


def test_calibrate_live_section(tmp_path: Path) -> None:
    oe = []
    for day in (0, 1):
        for i in range(12):
            t = T0 + day * DAY + i * 100 * S
            if i % 3 == 2:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, cancel_at=t + 30 * S, cancel_sent=t + 30 * S))
            else:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, fills=[(t + 3 * S, 5)]))
    live = log_from(oe, tmp_path / "live")
    ds = make_dataset(
        tmp_path / "ds",
        [
            {"ts_event": T0 + d * DAY + 7200 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}
            for d in (0, 1)
        ],
        n_days=2,
    )
    man = json.loads((ds / "manifest.json").read_text())
    man["kind"] = "l2"
    (ds / "manifest.json").write_text(json.dumps(man))
    out = tmp_path / "cal"
    cal = calibrate_live(
        {
            "dataset": {"path": str(ds)},
            "live": {"path": str(live)},
            "family": "power3",
            "horizons_ns": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S],
            "bootstrap": {"b": 30},
            "out": str(out),
        },
        runner=_ZooByParam(),
    )
    test_doc = json.loads((out / "live_calibration_test.json").read_text())
    summary, html = _render(tmp_path, [out])
    assert "calibration" in summary.sections
    s = _section(html, "calibration")
    assert "Fitted against your own live fills (calibrate-live)" in s
    assert "Live-fill log-likelihood profile (power3)" in s
    assert "Held-out test orders: fitted model vs your live fills" in s
    assert f"{test_doc['b']} / {test_doc['c']}" in s
    hd = cal.held_out_discordance  # 1.1 field (v0.1: stratified rows `held_out_test`)
    if hd is not None:
        assert rc.esc(F.pp(hd.fill_rate_bias)) in s
    else:
        held = next(x for x in cal.stratified if x.stratum_kind == "held_out_test" and x.stratum == "fill_rate_bias")
        assert rc.esc(F.pp(held.estimate)) in s
    assert "held_out_test" not in s  # echoed rows are rendered as the held-out block, not as strata
    head = re.search(r'<header id="header">(.*?)</header>', html, re.S).group(1)
    assert "qt.live_calibration_test" in head
    assert "power_prob_queue_model3(" in s
