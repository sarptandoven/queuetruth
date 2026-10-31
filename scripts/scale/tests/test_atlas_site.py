"""The Fill-Realism Atlas site generator (`scripts/scale/atlas_site.py`) on a small hand-built atlas directory."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atlas_site as S

S1, S10 = 1_000_000_000, 10_000_000_000


def _row(tier: str, model: str, h: int, bias: float, lo: float, hi: float, exact: float = 0.11) -> dict:
    return {
        "mode": "faithful", "shadow_pop": "standard", "tier": tier, "model_id": model, "horizon_ns": h,
        "n": 3000, "n_dates": 2, "n_symbols": 4,
        "fill_rate_bias_value": bias, "fill_rate_bias_ci_lo": lo, "fill_rate_bias_ci_hi": hi,
        "fill_rate_bias_se": 0.003,
        "exact_fill_rate_value": exact, "exact_fill_rate_ci_lo": exact - 0.01, "exact_fill_rate_ci_hi": exact + 0.01,
        "exact_fill_rate_se": 0.005, "fill_rate_bias_p_boot": 0.001, "fill_rate_bias_p_bh": 0.004,
        "n_flagged_symbol_days": 0, "flagged_n": 30, "flagged_share": 0.01,
    }  # fmt: skip


@pytest.fixture
def atlas(tmp_path: Path) -> Path:
    d = tmp_path / "atlas"
    d.mkdir()
    rows = []
    for tier in ("ALL", "T1_top100"):
        rows += [
            _row(tier, "nt_prob_touch[mode=faithful;p=1]", S1, 0.0313, 0.0253, 0.0377),
            _row(tier, "nt_prob_touch[mode=faithful;p=1]", S10, 0.033, 0.025, 0.041),
            _row(tier, "hbt_power3[mode=faithful;n=2]", S1, -0.004, -0.009, 0.001),
            _row(tier, "hbt_power3[mode=faithful;n=2]", S10, -0.006, -0.012, -0.001),
            _row(tier, "front_of_queue[mode=faithful]", S1, 0.2, 0.18, 0.22),
            _row(tier, "front_of_queue[mode=faithful]", S10, 0.25, 0.22, 0.28),
        ]
    rows.append({**_row("ALL", "x<script>", S1, 0.0, -0.001, 0.001), "mode": "corrected"})
    pq.write_table(pa.Table.from_pylist(rows), d / "leaderboard_by_tier.parquet")
    per = [
        {"mode": "faithful", "shadow_pop": "standard", "model_id": "nt_prob_touch[mode=faithful;p=1]",
         "horizon_ns": S1, "symbol": "AAPL", "tier": "T1_top100", "n": 1500, "n_days": 2, "bias": 0.03,
         "ci_lo": 0.02, "ci_hi": 0.04, "se": 0.005, "bias_shrunk": 0.031, "p_normal": 0.01, "p_bh": 0.02,
         "n_flagged_days": 0, "flags": None},
    ]  # fmt: skip
    pq.write_table(pa.Table.from_pylist(per), d / "per_symbol.parquet")
    manifest = {
        "schema": "qt.scale.atlas_manifest", "schema_version": 1, "atlas_root": "ab" * 32,
        "days": [
            {"date": "2019-12-30", "run_id": "r1", "merkle_root": "cd" * 32, "units_valid": 2, "units_planned": 2},
            {"date": "2020-01-30", "run_id": "r2", "merkle_root": "ef" * 32, "units_valid": 2, "units_planned": 2},
        ],
        "symbol_days": 8, "distinct_symbols": 4, "flag_policy": "label", "build": {"queuetruth": "0.2.0"},
    }  # fmt: skip
    (d / "atlas_manifest.json").write_text(json.dumps(manifest))
    return d


def test_nasdaq_site_reads_answer_first_and_is_deterministic(atlas: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    r = S.build(atlas, out, horizon="1s")
    assert r["pages"] == ["index.html"] and r["atlas_root"] == "ab" * 32
    page = (out / "index.html").read_text()
    # the most biased non-bound model in tier ALL leads, in plain words
    assert (
        "NautilusTrader's touch fill model (prob_fill_on_limit) (nt_prob_touch[mode=faithful;p=1]) fills 3.13 "
        "percentage points more orders within 1 s" in page
    )
    assert '+3.13<br><span class="ci">[+2.53, +3.77]' in page
    # sorted by |bias| at 1 s: the upper bound first, then NautilusTrader, then hftbacktest
    t_all = page.split("<h2>Tier ALL</h2>")[1].split("<h2>")[0]
    assert t_all.index("front_of_queue") < t_all.index("nt_prob_touch") < t_all.index("hbt_power3")
    assert page.index("<h2>Tier ALL</h2>") < page.index("<h2>Tier T1_top100</h2>")
    assert "ab" * 32 in page and "cd" * 32 in page and "Data provided for free by IEX" not in page
    assert "<script" not in page and "http://" not in page.replace("https://", "")
    # the headline is the search and link-preview text
    assert '<meta property="og:description" content="NautilusTrader&#x27;s touch fill model' in page
    # what a visitor can do next, before the verification section
    assert page.index("<h2>Check your own backtester</h2>") < page.index("<h2>Verify any number</h2>")
    assert "pipx run queuetruth demo --yes" in page and "referee-your-simulator.md" in page
    data = json.loads((out / "data" / "leaderboard.json").read_text())
    assert len(data["rows"]) == 12 and "attribution" not in data
    # byte-identical rebuild
    again = tmp_path / "again"
    S.build(atlas, again, horizon="1s")
    for f in ("index.html", "data/leaderboard.json", "data/atlas_manifest.json"):
        assert (out / f).read_bytes() == (again / f).read_bytes(), f


def test_iex_site_carries_the_attribution_and_escapes(atlas: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    r = S.build(atlas, out, source="iex", mode="corrected", horizon="1s", per_symbol=False)
    page = (out / "index.html").read_text()
    assert S.IEX_ATTRIBUTION in page and S.IEX_TERMS_URL in page
    assert json.loads((out / "data" / "leaderboard.json").read_text())["attribution"] == S.IEX_ATTRIBUTION
    assert "x&lt;script&gt;" in page and "x<script>" not in page
    assert r["rows"] == 1


def test_attribution_matches_the_package_and_options_are_checked(atlas: Path, tmp_path: Path) -> None:
    from queuetruth.demo.sources import IEX_ATTRIBUTION

    assert S.IEX_ATTRIBUTION == IEX_ATTRIBUTION
    r = S.build(atlas, tmp_path / "s", per_symbol=True)
    assert r["pages"] == ["index.html", "symbols.html"]
    assert "AAPL" in (tmp_path / "s" / "symbols.html").read_text()
    with pytest.raises(SystemExit, match="horizon"):
        S.build(atlas, tmp_path / "x", horizon="60s")
    with pytest.raises(SystemExit, match="no leaderboard rows"):
        S.build(atlas, tmp_path / "y", population="deep")
    assert (
        S.duration(100_000_000) == "100 ms" and S.duration(300 * S1) == "5 min" and S.parse_duration("1min") == 60 * S1
    )


def _pnl_row(tier: str, model: str, h: int, bias: float, lo: float, hi: float) -> dict:
    return {
        "mode": "faithful", "shadow_pop": "standard", "tier": tier, "model_id": model, "horizon_ns": h, "n": 2900,
        "n_dates": 2, "n_symbols": 4, "pnl_bias_ticks_value": bias, "pnl_bias_ticks_ci_lo": lo,
        "pnl_bias_ticks_ci_hi": hi, "pnl_bias_ticks_se": 0.01, "model_pnl_ticks_value": -1.0 + bias,
        "exact_pnl_ticks_value": -1.0, "pnl_bias_ticks_p_boot": 0.01, "pnl_bias_ticks_p_bh": 0.02,
    }  # fmt: skip


def test_site_shows_the_pnl_error_when_the_atlas_has_it(atlas: Path, tmp_path: Path) -> None:
    S60 = 60 * S1
    rows = []
    for tier in ("ALL", "T1_top100"):
        rows += [
            _pnl_row(tier, "nt_prob_touch[mode=faithful;p=1]", S60, 0.25, 0.2, 0.3),
            _pnl_row(tier, "hbt_power3[mode=faithful;n=2]", S60, -0.4, -0.5, -0.3),
            _pnl_row(tier, "front_of_queue[mode=faithful]", S60, 1.5, 1.2, 1.8),  # a bound never leads
            _pnl_row(tier, "hbt_power3[mode=faithful;n=2]", S1, -0.1, -0.2, 0.0),
        ]
    pq.write_table(pa.Table.from_pylist(rows), atlas / "pnl_by_tier.parquet")
    out = tmp_path / "site"
    r = S.build(atlas, out, horizon="1s")
    page = (out / "index.html").read_text()
    assert (
        "In P&amp;L, hftbacktest's ProbQueue Power3 (hbt_power3[mode=faithful;n=2]) is furthest from the real queue: "
        "it books 0.400 ticks per order less than the exact replay, marked to the mid 1 min after the fill "
        "(95% interval -0.500 to -0.300; the exact P&amp;L is -1.000 ticks per order)." in page
    )
    assert "<th>P&amp;L, ticks (1 min)</th>" in page and '<td class="under">-0.400<br>' in page
    assert "(over the 0 of 2 days whose units computed it)" in page  # the fixture manifest has no pnl_days
    t_all = page.split("<h2>Tier ALL</h2>")[1].split("<h2>")[0]
    assert '<td class="over">+0.250<br><span class="ci">[+0.200, +0.300]</span></td>' in t_all
    data = json.loads((out / "data" / "leaderboard.json").read_text())
    assert data["pnl_horizon_ns"] == S60 and len(data["pnl_rows"]) == r["pnl_rows"] == 6
    # another markout horizon, and one the atlas does not have
    S.build(atlas, tmp_path / "s1", horizon="1s", pnl_horizon="1s")
    assert (
        "ticks per order less than the exact replay, marked to the mid 1 s"
        in (tmp_path / "s1" / "index.html").read_text()
    )
    with pytest.raises(SystemExit, match="P&L horizon"):
        S.build(atlas, tmp_path / "x", horizon="1s", pnl_horizon="10s")
    # without pnl_by_tier the page is what it was
    (atlas / "pnl_by_tier.parquet").unlink()
    S.build(atlas, tmp_path / "plain", horizon="1s")
    assert "P&amp;L" not in (tmp_path / "plain" / "index.html").read_text()
