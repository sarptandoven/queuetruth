"""The Fill-Realism Atlas site generator (`scripts/scale/atlas_site.py`) on a small hand-built atlas directory."""

from __future__ import annotations

import json
import shutil
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
            {"date": "2019-12-30", "run_id": "r1", "merkle_root": "cd" * 32, "units_valid": 2, "units_planned": 2,
             "git_heads": ["1a" * 20]},
            {"date": "2020-01-30", "run_id": "r2", "merkle_root": "ef" * 32, "units_valid": 2, "units_planned": 2},
        ],
        "symbol_days": 8, "distinct_symbols": 4, "flag_policy": "label",
        "build": {"queuetruth_version": "0.2.0", "git_head": "2b" * 20},
    }  # fmt: skip
    (d / "atlas_manifest.json").write_text(json.dumps(manifest))
    return d


def test_nasdaq_site_reads_answer_first_and_is_deterministic(atlas: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    r = S.build(atlas, out, horizon="1s")
    # one page per library the atlas has (this one has no Databento model)
    assert r["pages"] == ["index.html", "hftbacktest.html", "nautilustrader.html"] and r["atlas_root"] == "ab" * 32
    page = (out / "index.html").read_text()
    # in plain words first: the exact rate, the most optimistic clear model with its relative size, and the closest;
    # hftbacktest's -0.40 pp is not a finding at 1 s (its interval holds 0), so no "most pessimistic" is named
    assert (
        "Within 1 s, the exact replay of the real queue fills 11.00% of these orders; the most optimistic fill model, "
        "NautilusTrader's touch fill model (prob_fill_on_limit) (nt_prob_touch[mode=faithful;p=1]), fills 3.13 "
        "percentage points more (28% more orders; 95% interval +2.53 to +3.77 pp); the closest is hftbacktest's "
        "ProbQueue Power3 (hbt_power3[mode=faithful;n=2]), -0.40 pp." in page
    )
    assert "pessimistic" not in page
    assert '+3.13<br><span class="ci">[+2.53, +3.77]' in page
    # sorted by |bias| at 1 s: the upper bound first, then NautilusTrader, then hftbacktest
    t_all = page.split("<h2>Tier ALL</h2>")[1].split("<h2>")[0]
    assert t_all.index("front_of_queue") < t_all.index("nt_prob_touch") < t_all.index("hbt_power3")
    assert page.index("<h2>Tier ALL</h2>") < page.index("<h2>Tier T1_top100</h2>")
    assert "ab" * 32 in page and "cd" * 32 in page and "Data provided for free by IEX" not in page
    # each day root with the commit its units ran at (a day from before builds were recorded says so), the site's
    # own build, and a verify command that works on the published day
    commit = "https://github.com/sarptandoven/queuetruth/commit/"
    assert f'<a href="{commit}{"1a" * 20}"><code>{"1a" * 6}</code></a>' in page
    assert page.count("not recorded") == 1 and f'queuetruth</a> 0.2.0 (<a href="{commit}{"2b" * 20}">' in page
    assert "verify_unit.py --day DAY_DIR --atlas-manifest atlas_manifest.json" in page
    assert "/releases/tag/atlas-days" in page and "fill-realism-atlas.md#verify-a-number" in page
    assert "<script" not in page and "http://" not in page.replace("https://", "")
    # the headline is the search and link-preview text
    assert '<meta property="og:description" content="Within 1 s, the exact replay' in page
    # what a visitor can do next, before the verification section
    assert page.index("<h2>Check your own backtester</h2>") < page.index("<h2>Verify any number</h2>")
    assert "pipx run queuetruth demo --yes" in page and "referee-your-simulator.md" in page
    assert "concepts/fill-realism-atlas.md" in page
    assert "2 days from 2019-12-30 to 2020-01-30" in page and S.iso_date("10092026") == "2026-10-09"
    data = json.loads((out / "data" / "leaderboard.json").read_text())
    assert len(data["rows"]) == 12 and "attribution" not in data
    # byte-identical rebuild
    again = tmp_path / "again"
    S.build(atlas, again, horizon="1s")
    for f in ("index.html", "hftbacktest.html", "nautilustrader.html", "data/leaderboard.json",
              "data/atlas_manifest.json"):  # fmt: skip
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
    r = S.build(atlas, tmp_path / "s", horizon="1s", per_symbol=True)
    assert r["pages"] == ["index.html", "hftbacktest.html", "nautilustrader.html", "symbols.html"]
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
    # the P&L sentence is about the 100 most traded symbols (ticks pool there), and names the largest clear error
    assert (
        "Among the 100 most traded symbols, marked to the mid 1 min after each fill, the exact replay makes -1.000 "
        "ticks per order; hftbacktest's ProbQueue Power3 (hbt_power3[mode=faithful;n=2]) books 0.400 ticks per order "
        "less (an error of 40% of the exact P&amp;L; 95% interval -0.500 to -0.300), the largest clear P&amp;L error "
        "of a fill model." in page
    )
    assert "<th>P&amp;L, ticks (1 min)</th>" in page and '<td class="under">-0.400<br>' in page
    assert "(over the 0 of 2 days whose units computed it)" in page  # the fixture manifest has no pnl_days
    t_all = page.split("<h2>Tier ALL</h2>")[1].split("<h2>")[0]
    assert '<td class="over">+0.250<br><span class="ci">[+0.200, +0.300]</span></td>' in t_all
    data = json.loads((out / "data" / "leaderboard.json").read_text())
    assert data["pnl_horizon_ns"] == S60 and len(data["pnl_rows"]) == r["pnl_rows"] == 6
    # another markout horizon, and one the atlas does not have
    S.build(atlas, tmp_path / "s1", horizon="1s", pnl_horizon="1s")
    # hftbacktest's -0.1 tick at 1 s has an interval that reaches 0: no P&L finding is stated
    assert (
        "marked to the mid 1 s after each fill, the exact replay makes -1.000 ticks per order, and no fill model's "
        "P&amp;L differs from it beyond its 95% interval." in (tmp_path / "s1" / "index.html").read_text()
    )
    with pytest.raises(SystemExit, match="P&L horizon"):
        S.build(atlas, tmp_path / "x", horizon="1s", pnl_horizon="10s")
    # without pnl_by_tier the page is what it was
    (atlas / "pnl_by_tier.parquet").unlink()
    S.build(atlas, tmp_path / "plain", horizon="1s")
    assert "P&amp;L" not in (tmp_path / "plain" / "index.html").read_text()


def test_compat_link_only_when_asked(atlas: Path, tmp_path: Path) -> None:
    S.build(atlas, tmp_path / "plain", horizon="1s")
    assert 'href="compat.html"' not in (tmp_path / "plain" / "index.html").read_text()
    S.build(atlas, tmp_path / "linked", horizon="1s", compat_link=True)
    page = (tmp_path / "linked" / "index.html").read_text()
    assert 'Upstream compatibility: <a href="compat.html">' in page
    assert S.main(["--atlas", str(atlas), "--out", str(tmp_path / "cli"), "--horizon", "1s", "--compat-link"]) == 0
    assert (tmp_path / "cli" / "index.html").read_text() == page


def test_two_venues_get_their_own_pages_and_a_switch(atlas: Path, tmp_path: Path) -> None:
    # a second venue whose newest day ran on a symbol sample
    nasdaq = tmp_path / "nasdaq_atlas"
    shutil.copytree(atlas, nasdaq)
    man = json.loads((nasdaq / "atlas_manifest.json").read_text())
    man["atlas_root"] = "99" * 32
    man["days"][-1] |= {
        "date": "01302020", "source_url": "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302020.NASDAQ_ITCH50.gz",
        "buckets": 32, "symbol_sample": {"per_tier": 40, "tiers": {"T1_top100": {"kept": 40, "of": 100},
                                                                    "T5_rest": {"kept": 40, "of": 4900}}},
    }  # fmt: skip
    (nasdaq / "atlas_manifest.json").write_text(json.dumps(man))
    r = S.build_site([("iex", atlas), ("nasdaq", nasdaq)], tmp_path / "site", horizon="1s", compat_link=True)
    assert r["venues"]["iex"]["dir"] == "." and r["venues"]["nasdaq"]["dir"] == "nasdaq/"
    index = (tmp_path / "site" / "index.html").read_text()
    nindex = (tmp_path / "site" / "nasdaq" / "index.html").read_text()
    assert S.IEX_ATTRIBUTION in index and "ab" * 32 in index and "99" * 32 not in index
    assert 'href="nasdaq/index.html">Nasdaq TotalView-ITCH</a>' in index and 'href="compat.html"' in index
    assert "Data provided for free by IEX" not in nindex and "99" * 32 in nindex and "compat.html" not in nindex
    assert "2020-01-30: 40 symbols per tier (T1_top100 40 of 100, T5_rest 40 of 4,900)" in nindex
    assert (
        "scripts/scale/itch_split.py --url https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302020.NASDAQ_ITCH50.gz "
        "--date 01302020 --buckets 32 --out split" in nindex
    )
    assert "Symbol sample." not in index
    # a venue given twice, or an unknown one, is refused
    with pytest.raises(SystemExit):
        S.build_site([("iex", atlas), ("iex", nasdaq)], tmp_path / "x")
    with pytest.raises(SystemExit):
        S.build_site([("cme", atlas)], tmp_path / "x")


def test_a_finding_needs_its_estimate_and_interval_on_one_side() -> None:
    # a percentile interval wholly above 0 around a negative estimate states nothing either way
    skewed = {"fill_rate_bias_value": -0.001, "fill_rate_bias_ci_lo": 0.0005, "fill_rate_bias_ci_hi": 0.003}
    assert not S._clear(skewed, "fill_rate_bias")
    assert S._clear({**skewed, "fill_rate_bias_value": 0.001}, "fill_rate_bias")
    assert not S._clear(
        {"fill_rate_bias_value": 0.01, "fill_rate_bias_ci_lo": None, "fill_rate_bias_ci_hi": 0.02}, "fill_rate_bias"
    )
    rows = [
        {**_row("ALL", "nt_prob_touch[mode=faithful;p=1]", S1, -0.001, 0.0005, 0.003)},
        {**_row("ALL", "hbt_power3[mode=faithful;n=2]", S1, -0.004, -0.009, 0.001)},
    ]
    line = S.headline(rows, S1)
    assert "optimistic" not in line and "pessimistic" not in line and "no fill model differs" in line


S60 = 60 * S1
#: An invented atlas with every library and several settings per library (no real numbers).
PB2 = "hbt_power2[exch=partial;mode=faithful;n=2]"
PW3 = "hbt_power3[exch=partial;mode=faithful;n=1]"
LOG = "hbt_log[exch=partial;mode=faithful]"
L3 = "hbt_l3_fifo[mode=faithful]"
TOUCH = "nt_prob_touch[mode=faithful;p=1]"
QPOS = "nt_queue_l2[mode=faithful]"
DBN = "databento_mc[k=-0.8;mode=faithful;particles=100]"
FRONT = "front_of_queue[mode=faithful]"
ZOO = {  # tier -> model -> (bias, lo, hi) at 60 s; at 1 s a quarter of it
    "ALL": {
        TOUCH: (0.019, 0.015, 0.023), QPOS: (0.0004, -0.002, 0.0028), PB2: (-0.0088, -0.012, -0.0056),
        PW3: (-0.012, -0.015, -0.009), LOG: (-0.010, -0.013, -0.007), L3: (0.0001, -0.001, 0.0012),
        DBN: (-0.015, -0.019, -0.011), FRONT: (0.15, 0.13, 0.17),
    },
    "T1_top100": {
        TOUCH: (0.021, 0.016, 0.026), QPOS: (-0.004, -0.007, -0.001), PB2: (-0.006, -0.010, -0.002),
        PW3: (-0.009, -0.013, -0.005), LOG: (-0.003, -0.007, 0.001), L3: (0.0, -0.001, 0.001),
        DBN: (-0.011, -0.016, -0.006), FRONT: (0.12, 0.10, 0.14),
    },
}  # fmt: skip


@pytest.fixture
def zoo_atlas(atlas: Path) -> Path:
    rows = []
    for tier, models in ZOO.items():
        for m, (b, lo, hi) in models.items():
            rows.append(_row(tier, m, S60, b, lo, hi, exact=0.13))
            rows.append(_row(tier, m, S1, b / 4, lo / 4, hi / 4, exact=0.03))
    rows.append({**_row("ALL", PB2.replace("faithful", "corrected"), S60, 0.0, -0.001, 0.001), "mode": "corrected"})
    pq.write_table(pa.Table.from_pylist(rows), atlas / "leaderboard_by_tier.parquet")
    pnl = [
        _pnl_row(t, m, S60, 10 * b, 10 * lo, 10 * hi) for t, models in ZOO.items() for m, (b, lo, hi) in models.items()
    ]
    pq.write_table(pa.Table.from_pylist(pnl), atlas / "pnl_by_tier.parquet")
    return atlas


def test_snippets_use_the_libraries_own_names_and_refuse_what_they_cannot_reproduce() -> None:
    assert S.parse_id(PB2) == ("hbt_power2", {"exch": "partial", "mode": "faithful", "n": "2"})
    assert S.snippet(PB2) == ("hftbacktest BacktestAsset chain", ".power_prob_queue_model2(2).partial_fill_exchange()")
    # the canonical exchange spelling (crates/zoo model_id.rs) and the CLI's alias give the no-partial exchange
    for exch in ("no_partial", "no-partial"):
        assert S.snippet(f"hbt_power[exch={exch};mode=faithful;n=3]")[1] == (
            ".power_prob_queue_model(3).no_partial_fill_exchange()"
        )
    assert S.snippet(LOG)[1] == ".log_prob_queue_model().partial_fill_exchange()"
    assert S.snippet("hbt_log2[mode=faithful]")[1] == ".log_prob_queue_model2().partial_fill_exchange()"
    assert S.snippet("hbt_risk_adverse[exch=partial;mode=faithful]")[1] == (
        ".risk_adverse_queue_model().partial_fill_exchange()"
    )
    assert S.snippet(TOUCH) == (
        "NautilusTrader BacktestEngine.add_venue keywords",
        "fill_model=FillModel(prob_fill_on_limit=1), book_type=BookType.L2_MBP, trade_execution=True, "
        "queue_position=False",
    )
    assert S.snippet(QPOS)[1].endswith("prob_fill_on_limit=1), book_type=BookType.L2_MBP, trade_execution=True, "
                                       "queue_position=True")  # fmt: skip
    assert "prob_fill_on_limit=0.3)" in S.snippet("nt_queue_l2_prob_touch[mode=faithful;p=0.3]")[1]
    # Databento's estimator has no library of its own: hftbacktest's expected-value equivalent, n = 1 / (1 + k)
    # with the partial-fill exchange the zoo runs it with, as `queuetruth export hftbacktest` writes it
    assert S.snippet(DBN)[1] == ".power_prob_queue_model3(5).partial_fill_exchange()"
    assert S.snippet("databento_mc[k=-0.95;mode=faithful;particles=100]")[1] == (
        ".power_prob_queue_model3(20).partial_fill_exchange()"
    )
    # the same text as the export's own builder for the same family and parameter
    from queuetruth.export.hftbacktest import EXCHANGES, POWER_BUILDERS

    assert S.snippet(PB2)[1] == f".{POWER_BUILDERS['power2']}(2).{EXCHANGES['partial']}()"
    assert "prob_fill_on_limit=0.286626)" in S.snippet("nt_queue_l2_prob_touch[mode=faithful;p=0.28662597]")[1]
    # nothing invented: corrected mode, L3 input, k > 0, unknown families and parameters have no snippet
    for m in (PB2.replace("faithful", "corrected"), L3, "databento_mc[k=0.5;mode=faithful]", FRONT, "x[n=2]",
              "hbt_power3[mode=faithful;n=abc]", "hbt_power3[mode=faithful]", "hbt_log[exch=both;mode=faithful]",
              "nt_prob_touch[mode=faithful;p=2]", "databento_mc[k=-1;mode=faithful]"):  # fmt: skip
        assert S.snippet(m) is None, m
    assert S.short_label(PB2) == "hftbacktest Power2 n=2" and S.short_label(DBN) == "Databento k=-0.8"
    assert S.short_label("x<y>") == "x<y>"


def test_best_setting_by_tier_says_whether_it_is_still_clearly_off(zoo_atlas: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    S.build(zoo_atlas, out)  # 60 s, the default
    best = {(b["tier"], b["library"]): b for b in S.best_settings(S._rows(zoo_atlas, "faithful", None)[0], S60)}
    # closest to 0 per tier and library; hftbacktest's L3 FIFO model (+0.01 pp) is not a setting of an L2 backtest
    assert best[("ALL", "hftbacktest")]["model_id"] == PB2 and best[("ALL", "hftbacktest")]["clearly_off"]
    assert best[("T1_top100", "hftbacktest")]["model_id"] == LOG
    assert not best[("T1_top100", "hftbacktest")]["clearly_off"]
    assert best[("ALL", "NautilusTrader")]["model_id"] == QPOS and not best[("ALL", "NautilusTrader")]["clearly_off"]
    assert best[("T1_top100", "NautilusTrader")]["clearly_off"]
    assert best[("ALL", "Databento")]["model_id"] == DBN and best[("ALL", "Databento")]["n_settings"] == 1
    page = (out / "index.html").read_text()
    sec = page.split("<h2>Best setting by tier</h2>")[1].split("<h2>")[0]
    assert page.index("<h2>Best setting by tier</h2>") < page.index("<h2>Tier ALL</h2>")
    assert "closest to 0" in sec and "L3 FIFO model is left out" in sec
    row = sec.split("<tr><td>ALL</td>")[1].split("</tr>")[0]
    assert (
        '<td><a href="hftbacktest.html">hftbacktest</a></td><td>hftbacktest Power2 n=2<br><code>'
        f"{PB2}</code></td>" + '<td class="under">-0.88<br><span class="ci">[-1.20, -0.56]</span></td>'
        '<td>still clearly off: too pessimistic<br><span class="ci">interval overlaps: hftbacktest Log, '
        "hftbacktest Power3 n=1</span></td>" in row
    )
    assert "<code>.power_prob_queue_model2(2).partial_fill_exchange()</code>" in row
    assert '<td class="under">-0.088<br>' in row  # its P&L error, with its interval
    nt = sec.split("<tr><td>ALL</td>")[2].split("</tr>")[0]
    assert '<td class="">+0.04<br>' in nt and "<td>no clear difference (95% interval includes 0)</td>" in nt
    assert "queue_position=True</code>" in nt and "DefaultFillModel" in sec
    assert "<code>.power_prob_queue_model3(5).partial_fill_exchange()</code>" in sec
    assert "<th>closest setting (point estimate)</th>" in sec and "random_seed" in sec and "from 2.0" in sec
    assert best[("ALL", "hftbacktest")]["overlapping"] == [LOG, PW3] and best[("ALL", "Databento")]["overlapping"] == []
    data = json.loads((out / "data" / "leaderboard.json").read_text())
    assert [(b["tier"], b["library"]) for b in data["best_settings"]] == [
        (t, lib) for t in ("ALL", "T1_top100") for lib in ("hftbacktest", "NautilusTrader", "Databento")
    ]
    assert data["best_settings"][0]["snippet"] == ".power_prob_queue_model2(2).partial_fill_exchange()"
    # corrected mode: the closest setting is named, but no library config is offered for it
    S.build(zoo_atlas, tmp_path / "c", mode="corrected")
    c = (tmp_path / "c" / "index.html").read_text().split("<h2>Best setting by tier</h2>")[1].split("<h2>")[0]
    assert "no library setting reproduces it" in c and "power_prob_queue_model2" not in c


def test_one_page_per_library_answers_the_search_question(zoo_atlas: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    r = S.build(zoo_atlas, out, source="iex")
    assert r["pages"] == ["index.html", "hftbacktest.html", "nautilustrader.html", "databento.html"]
    index = (out / "index.html").read_text()
    assert (
        '<nav class="libs">By library: <a href="hftbacktest.html">hftbacktest</a>, '
        '<a href="nautilustrader.html">NautilusTrader</a>, <a href="databento.html">Databento</a></nav>' in index
    )
    hbt = (out / "hftbacktest.html").read_text()
    assert "<title>How accurate is hftbacktest&#x27;s queue model? Fill-Realism Atlas</title>" in hbt
    lead = (
        "Within 1 min, the exact replay of the real queue fills 13.00% of these orders. Of hftbacktest's 3 settings "
        "in the atlas, 3 fill clearly fewer (95% interval excludes 0). The closest point estimate is hftbacktest's "
        "ProbQueue Power2 "
        f"({PB2}), -0.88 pp (95% interval -1.20 to -0.56 pp), and it still fills clearly fewer orders than the real "
        "queue. Its interval overlaps those of 2 other settings (hftbacktest Log, hftbacktest Power3 n=1), so the "
        "atlas cannot rank them by closeness."
    )
    assert f'<p class="lead">{lead}</p>' in hbt
    assert '<meta name="description" content="Within 1 min, the exact replay' in hbt
    # only hftbacktest's models (the L3 model is in its table, not in its best settings), across tiers and horizons
    assert all(m in hbt for m in (PB2, PW3, LOG, L3)) and TOUCH not in hbt and DBN not in hbt and FRONT not in hbt
    assert "<h2>Best hftbacktest setting by tier</h2>" in hbt and "<th>1 s</th><th>1 min</th>" in hbt
    assert "<h2>Tier ALL</h2>" in hbt and "<h2>Tier T1_top100</h2>" in hbt
    assert (
        "of hftbacktest&#x27;s settings." not in hbt
        and "the largest clear P&amp;L error of hftbacktest's models." in hbt
    )
    assert "calibrate-hftbacktest.md" in hbt and '<a href="index.html">Fill-Realism Atlas</a>' in hbt
    assert S.IEX_ATTRIBUTION in hbt and "<script" not in hbt
    assert "2 days from 2019-12-30 to 2020-01-30" in hbt  # the same ISO dates as the index
    nt = (out / "nautilustrader.html").read_text()
    assert "<title>How accurate is NautilusTrader&#x27;s fill model? Fill-Realism Atlas</title>" in nt
    # a setting whose interval holds 0 is never stated as a difference
    assert (
        "Of NautilusTrader's 2 settings in the atlas, 1 fills clearly more orders (95% interval excludes 0). The "
        f"closest point estimate is NautilusTrader's queue_position model ({QPOS}), +0.04 pp (95% interval -0.20 to "
        "+0.28 pp), "
        "which the atlas cannot tell from the exact replay at this sample size." in nt
    )
    assert "integrations/nautilus/README.md" in nt
    db = (out / "databento.html").read_text()
    assert "How accurate is Databento&#x27;s queue position estimate?" in db and DBN in db and PB2 not in db
    # no setting clear at all
    wide = [
        {**r, "fill_rate_bias_ci_lo": -1.0, "fill_rate_bias_ci_hi": 1.0}
        for r in S._rows(zoo_atlas, "faithful", None)[0]
    ]
    said = S.library_headline(wide, "hftbacktest", S60)
    assert "none fills more or fewer orders than it beyond its 95% interval" in said and "clearly" not in said
    again = tmp_path / "again"
    S.build(zoo_atlas, again, source="iex")
    for f in r["pages"]:
        assert (out / f).read_bytes() == (again / f).read_bytes(), f


def test_charts_are_accessible_static_svg_with_the_table_values(zoo_atlas: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    S.build(zoo_atlas, out)
    page = (out / "index.html").read_text()
    assert page.count("<svg ") == 2 and "<script" not in page  # one chart per tier
    t_all = page.split("<h2>Tier ALL</h2>")[1].split("<h2>")[0]
    svg = t_all.split("<svg ")[1].split("</svg>")[0]
    assert t_all.index("<svg ") < t_all.index("<table>")
    assert 'role="img" aria-labelledby="bias-ALL-t" aria-describedby="bias-ALL-d"' in svg
    assert '<title id="bias-ALL-t">Fill-rate bias by model within 1 min, tier ALL</title>' in svg
    assert '<desc id="bias-ALL-d">Fill-rate bias of 7 models at 1 min' in svg  # the bound is not drawn
    assert FRONT in t_all and "front of the queue" not in svg
    # each value drawn is the value in the table, coloured only when its interval excludes 0
    assert '<g class="over"><title>NautilusTrader touch p=1: +1.90 pp [+1.50, +2.30]</title>' in svg
    assert '<g class="plain"><title>NautilusTrader queue_position: +0.04 pp [-0.20, +0.28]</title>' in svg
    assert '<g class="under"><title>hftbacktest Power2 n=2: -0.88 pp [-1.20, -0.56]</title>' in svg
    assert '<td class="over">+1.90<br><span class="ci">[+1.50, +2.30]</span></td>' in t_all
    assert '<line class="zero"' in svg and ">0</text>" in svg and "-2</text>" in svg and "+2</text>" in svg
    # themed only through the page's colour variables
    assert "svg.bias .over .dot { fill: var(--over); }" in page and 'fill="#' not in svg and 'stroke="#' not in svg
    hbt = (out / "hftbacktest.html").read_text()
    assert hbt.count("<svg ") == 2 and "NautilusTrader touch" not in hbt.split("<svg ")[1]
    assert S.chart([], "x", "t", S60) == ""
    flat = S.chart([_row("ALL", PB2, S60, 0.0, 0.0, 0.0)], "f", "t", S60)  # a zero-width domain still draws
    assert 'cx="475.0"' in flat


def test_words_follow_the_interval_and_labels_fit(zoo_atlas: Path, tmp_path: Path) -> None:
    # a point estimate below 0 whose interval lies above it (a skewed bootstrap) is no finding: grey everywhere, and
    # no "more" or "fewer" in the verdict, the summary, the table or the chart (the same rule as `_clear`)
    odd = _row("ALL", PB2, S60, -0.001, 0.0005, 0.003)
    assert S.side(odd) == 0 and S.verdict(odd) == "no clear difference (95% interval includes 0)"
    assert '<g class="plain">' in S.chart([odd], "o", "t", S60) and S._cls(odd, "fill_rate_bias") == ""
    said = S.library_headline([odd], "hftbacktest", S60)
    assert "none fills more or fewer" in said and "clearly" not in said
    assert not S.best_settings([odd], S60)[0]["clearly_off"]
    # estimate and interval on one side: red, and the words say optimistic
    up = _row("ALL", PB2, S60, 0.001, 0.0005, 0.003)
    assert S.side(up) == 1 and S.verdict(up) == "still clearly off: too optimistic"
    assert '<g class="over">' in S.chart([up], "u", "t", S60) and S._cls(up, "fill_rate_bias") == "over"
    # a long label is cut with an ellipsis; the row's tooltip keeps all of it
    long = "nt_queue_l2_prob_touch[mode=faithful;p=0.28662597]"
    svg = S.chart([_row("ALL", long, S60, 0.01, 0.0, 0.02)], "l", "t", S60)
    assert "<title>NautilusTrader queue_position + touch p=0.28662597: +1.00 pp" in svg
    assert ">NautilusTrader queue_position + touch p\u2026</text>" in svg
    # links are not the colour of "too pessimistic", and the nav marks the current page
    S.build(zoo_atlas, tmp_path / "s")
    hbt = (tmp_path / "s" / "hftbacktest.html").read_text()
    assert "a { color: var(--fg);" in hbt and "a { color: var(--under)" not in hbt
    assert '<span aria-current="page">hftbacktest</span>' in hbt
