"""Touch tables for bar backtests (`scripts/scale/atlas_touch.py`, docs/concepts/touch-tables.md), on synthetic data.

- `first_reach` finds the first print at or through a price from a start index, as a scan would;
- `outcomes` applies the definitions to a hand-built day: prints before placement never fill, bars are aligned to
  the clock, "by the end of the same bar" in both directions, markouts one bar later, orders that never rested left
  out;
- `combine` pools the days that have touch cells (and names them), with the atlas's bootstrap, under both flag
  policies, and leaves every other output byte-identical;
- the site section and `atlas/v1/touch.json` (validated by `queuetruth.atlas.touch.TouchTables`), and nothing at all
  for an atlas without touch tables.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import atlas_touch as AT
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from test_atlas_combine import DATES, SESSION, SYMS, _tier, _write_day

S1 = 1_000_000_000
M = 60 * S1
OPEN = 34_200 * S1  # 09:30 since midnight (midnight_utc_ns = 0 here)
TICK = 10


@pytest.mark.parametrize("seed", range(4))
def test_first_reach_is_a_scan(seed: int) -> None:
    rng = np.random.default_rng(seed)
    s = rng.integers(0, 1000, size=int(rng.integers(1, 3000))).astype(np.int64)
    start = rng.integers(0, len(s) + 3, size=200)
    thr = rng.integers(-5, 60, size=200)
    got = AT.first_reach(s, start, thr)
    for q in range(200):
        hit = [j for j in range(int(start[q]), len(s)) if s[j] <= thr[q]]
        assert got[q] == (hit[0] if hit else -1)
    assert AT.first_reach(np.zeros(0, np.int64), np.array([0]), np.array([1])).tolist() == [-1]


def _ts(ns: list[int | None]) -> pa.Array:
    return pa.array(ns, pa.int64()).cast(pa.timestamp("ns", tz="UTC"))


def _day() -> tuple[pa.Table, pa.Table, pa.Table]:
    """Prints (time since midnight, price) and four orders on one instrument; tick 10."""
    prints = [
        (9 * 3600 * S1, 500),  # before the open: never in a bar
        (OPEN + 20 * S1, 900),  # before A's placement: never fills A
        (OPEN + 40 * S1, 1000),  # 09:30:40 touches A's 1000
        (OPEN + 70 * S1, 990),  # 09:31:10 one tick through A
        (OPEN + 210 * S1, 980),  # 09:33:30 two ticks through A
        (OPEN + 15 * M, 1050),  # 09:45:00 touches B's 1050
        (OPEN + 15 * M + 30 * S1, 1040),  # 09:45:30
    ]
    trades = pa.table(
        {
            "ts_event": _ts([t for t, _ in prints]),
            "instrument_id": pa.array([1] * len(prints), pa.uint32()),
            "sequence": pa.array(range(len(prints)), pa.uint64()),
            "price": pa.array([p for _, p in prints], pa.int64()),
        }
    )
    results = pa.table(
        {
            "shadow_id": ["standard:a", "standard:b", "standard:c", "standard:d"],
            "shadow_pop": ["standard"] * 4,
            "instrument_id": pa.array([1] * 4, pa.uint32()),
            "side": pa.array([66, 65, 66, 66], pa.uint8()),  # A buy, B sell, C buy (rejected), D buy (never live)
            "price": pa.array([1000, 1050, 1200, None], pa.int64()),
            "ts_live": _ts([OPEN + 30 * S1, OPEN + 60 * S1, OPEN + 30 * S1, None]),
            "ts_first_fill": _ts([OPEN + 70 * S1, None, None, None]),
            "end_state": ["filled", "open_at_end", "rejected_would_cross", "open_at_end"],
            "out_of_session": [False] * 4,
        }
    )
    instruments = pa.table(
        {
            "instrument_id": pa.array([1], pa.uint32()),
            "session_date": pa.array([dt.date(2026, 10, 5)], pa.date32()),
            "symbol": ["AAA"],
            "tick_size": pa.array([TICK], pa.int64()),
            "midnight_utc_ns": pa.array([0], pa.int64()),
            "open_s": pa.array([34_200], pa.uint32()),
            "close_s": pa.array([57_600], pa.uint32()),
        }
    )
    return results, trades, instruments


def test_outcomes_follow_the_definitions() -> None:
    results, trades, instruments = _day()
    per = AT.outcomes(results, trades, instruments, AT.DEFAULT_SPEC).to_pylist()
    got = {(r["side"], r["bar_s"], r["rule"]): r for r in per}
    assert len(per) == 2 * 3 * 3  # A and B; C was rejected and D never went live
    want = {
        # A: buy 1000 placed 09:30:30, real fill 09:31:10; touch 09:30:40, one tick 09:31:10, two ticks 09:33:30
        ("buy", 60, "touch"): (1, 1, 0, 1),  # rule in the 09:30 bar, real in 09:31: not real by the rule's bar
        ("buy", 300, "touch"): (1, 1, 1, 1),  # both in the 09:30-09:35 bar
        ("buy", 60, "penetrate_1"): (1, 1, 1, 1),
        ("buy", 60, "penetrate_2"): (1, 1, 1, 0),  # real first: the rule misses it by the real fill's bar
        ("buy", 3600, "penetrate_2"): (1, 1, 1, 1),
        # B: sell 1050 placed 09:31:00, never filled; touched at 09:45:00, never one tick through
        ("sell", 60, "touch"): (0, 1, 0, 0),
        ("sell", 60, "penetrate_1"): (0, 0, 0, 0),
    }
    for key, (real, rule, rule_real, real_rule) in want.items():
        r = got[key]
        assert (r["real"], r["rule_fill"], r["rule_real"], r["real_rule"]) == (real, rule, rule_real, real_rule), key
    # markouts one bar later, in ticks, signed for the order: A's real fill 09:31:10 -> last print by 09:32:10 is 990
    assert got[("buy", 60, "touch")]["mk_real"] == -1.0
    assert got[("buy", 60, "touch")]["mk_rule"] == -1.0  # 09:30:40 -> by 09:31:40: 990
    assert got[("buy", 300, "penetrate_2")]["mk_rule"] == -2.0  # 09:33:30 -> by 09:38:30: 980
    assert got[("sell", 60, "touch")]["mk_rule"] == 1.0  # sold 1050, 1040 a bar later: one tick good
    assert got[("sell", 60, "penetrate_1")]["mk_rule"] == 0.0 and got[("sell", 60, "touch")]["mk_real"] == 0.0

    st = AT.stats(AT.outcomes(results, trades, instruments, AT.DEFAULT_SPEC), instruments)
    assert st.column_names == [*AT.KEYS, *AT.SUMS]
    assert st.num_rows == 18 and set(st.column("session_date").to_pylist()) == {20731}
    assert pc.sum(st["n"]).as_py() == 18


def _touch_cells(d: Path, k: int, rng: np.random.Generator) -> None:
    rows = []
    for pop in ("standard", "large"):
        for sym in SYMS:
            ok = not (sym == "DDD" and k == 1)
            for side in ("buy", "sell"):
                for bar_s in (60, 3600):
                    for rule in ("touch", "penetrate_1"):
                        n = int(rng.integers(20, 60))
                        n_real, n_rule = int(rng.integers(0, n)), int(rng.integers(0, n))
                        rows.append(
                            {"shadow_pop": pop, "symbol": sym, "session_date": SESSION[k], "side": side}
                            | {"bar_s": bar_s, "rule": rule, "n": n, "n_real": n_real, "n_rule": n_rule}
                            | {"n_rule_real": int(rng.integers(0, n_rule + 1))}
                            | {"n_real_rule": int(rng.integers(0, n_real + 1))}
                            | {"mk_real": float(rng.normal(-1, 1)), "mk_rule": float(rng.normal(1, 1))}
                            | {"tier": _tier(sym, k), "replay_ok": ok, "trading_ok": True, "exact_ok": ok}
                        )
    pq.write_table(pa.Table.from_pylist(rows), d / AT.CELLS)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.parametrize("policy", ["exclude", "label"])
def test_combine_pools_the_days_that_have_touch_cells(tmp_path: Path, policy: str) -> None:
    import atlas

    rng = np.random.default_rng(11)
    days = [_write_day(tmp_path, k, rng) for k in range(3)]
    before = atlas.combine(days, tmp_path / "no-touch", bootstrap_b=50, flag_policy=policy)
    assert "touch_days" not in before
    assert "touch_days" not in json.loads((tmp_path / "no-touch" / "atlas_manifest.json").read_text())
    assert not (tmp_path / "no-touch" / AT.TABLE).exists()
    for k in (1, 2):  # the first day was reduced before the touch tables existed
        _touch_cells(days[k], k, rng)
    res = atlas.combine(days, tmp_path / "touch", bootstrap_b=50, flag_policy=policy)
    man = json.loads((tmp_path / "touch" / "atlas_manifest.json").read_text())
    assert man["touch_days"] == [DATES[1], DATES[2]] and res["touch_days"] == 2
    # every other table is byte-identical
    for name in ("leaderboard_by_tier", "pnl_by_tier", "per_symbol", "maps_by_tier"):
        assert _sha(tmp_path / "touch" / f"{name}.parquet") == _sha(tmp_path / "no-touch" / f"{name}.parquet")

    cells = pa.concat_tables([pq.read_table(d / AT.CELLS) for d in days[1:]])
    if policy == "exclude":
        cells = cells.filter(cells["exact_ok"])
    rows = pq.read_table(tmp_path / "touch" / AT.TABLE).to_pylist()
    assert {r["tier"] for r in rows} == {"T1_top100", "T2_101_500", "ALL"}
    assert {r["side"] for r in rows} == {"buy", "sell", "all"}
    for r in rows:
        sel = cells.filter(pc.equal(cells["shadow_pop"], r["shadow_pop"]))
        if r["tier"] != "ALL":
            sel = sel.filter(pc.equal(sel["tier"], r["tier"]))
        if r["side"] != "all":
            sel = sel.filter(pc.equal(sel["side"], r["side"]))
        sel = sel.filter(pc.and_(pc.equal(sel["bar_s"], r["bar_s"]), pc.equal(sel["rule"], r["rule"])))
        s = {f: pc.sum(sel[f]).as_py() for f in AT.SUMS}
        assert r["n"] == s["n"] and r["n_dates"] == len(pc.unique(sel["session_date"]))
        assert r["real_fill_rate_value"] == pytest.approx(s["n_real"] / s["n"], abs=1e-12)
        assert r["p_real_given_rule_value"] == pytest.approx(s["n_rule_real"] / s["n_rule"], abs=1e-12)
        assert r["p_rule_given_real_value"] == pytest.approx(s["n_real_rule"] / s["n_real"], abs=1e-12)
        assert r["rule_markout_ticks_value"] == pytest.approx(s["mk_rule"] / s["n_rule"], abs=1e-12)
        assert r["pnl_overstated_ticks_value"] == pytest.approx((s["mk_rule"] - s["mk_real"]) / s["n"], abs=1e-12)
        assert r["real_fill_rate_ci_lo"] <= r["real_fill_rate_value"] <= r["real_fill_rate_ci_hi"]
        flagged = (r["tier"] in ("T2_101_500", "ALL")) and policy == "label"
        assert (r["flagged_n"] > 0) == flagged

    # a window without any touch day writes nothing and names none
    out = tmp_path / "old-only"
    res = atlas.combine(days[:1], out, bootstrap_b=50, flag_policy=policy)
    assert "touch_days" not in res and not (out / AT.TABLE).exists()


def test_site_section_and_touch_json(tmp_path: Path) -> None:
    import atlas
    import atlas_site

    from queuetruth.atlas.touch import TouchTables

    rng = np.random.default_rng(5)
    days = [_write_day(tmp_path, k, rng) for k in range(3)]
    atlas.combine(days, tmp_path / "plain", bootstrap_b=50, flag_policy="exclude")
    atlas_site.build(
        tmp_path / "plain", tmp_path / "site-plain", source="iex", population="standard", pnl_horizon="10s"
    )
    # the data API is always written; without touch tables it has no touch.json and its manifest lists none
    plain_api = tmp_path / "site-plain" / "atlas" / "v1"
    assert not (plain_api / "touch.json").exists()
    assert "touch.json" not in [f["path"] for f in json.loads((plain_api / "manifest.json").read_text())["files"]]
    assert "bar-backtests" not in (tmp_path / "site-plain" / "index.html").read_text()

    for k in range(3):
        _touch_cells(days[k], k, rng)
    atlas.combine(days, tmp_path / "touch", bootstrap_b=50, flag_policy="exclude")
    atlas_site.build(tmp_path / "touch", tmp_path / "site", source="iex", population="standard", pnl_horizon="10s")
    page = (tmp_path / "site" / "index.html").read_text()
    assert 'id="bar-backtests"' in page and 'href="atlas/v1/touch.json"' in page
    doc = TouchTables.model_validate_json((tmp_path / "site" / "atlas" / "v1" / "touch.json").read_text())
    # touch.json is listed in the API manifest with its size and sha256, like every other file
    api = tmp_path / "site" / "atlas" / "v1"
    entry = {f["path"]: f for f in json.loads((api / "manifest.json").read_text())["files"]}["touch.json"]
    data = (api / "touch.json").read_bytes()
    assert entry["bytes"] == len(data) and entry["sha256"] == hashlib.sha256(data).hexdigest()
    # on the front page after the libraries' answers (none in this synthetic atlas), before the symbol lookup
    assert page.index("<h1>") < page.index('id="bar-backtests"') < page.index("<h2>Your symbol</h2>")
    assert doc.population == "standard" and doc.source == "iex" and doc.attribution
    assert doc.days == ["2026-10-05", "2026-10-06", "2026-10-07"]
    row = doc.row(tier="ALL", side="all", bar="1m", rule="touch")
    assert row is not None and row.bar_s == 60 and row.dates == 3 and row.flagged_share == 0
    assert {r.bar for r in doc.rows} == {"1m", "1h"}
    # the lead sentence quotes the 1-minute touch row
    assert f"{100 * row.p_real_given_rule.value:.1f}%" in page
    # the P&L is called too high or too low only when its interval excludes 0
    clear = AT.pnl_words({"value": 0.5, "ci_lo": 0.1, "ci_hi": 0.9})
    assert clear == "Its P&L is 0.50 ticks per order too high (+0.10 to +0.90)."
    assert "too low" in AT.pnl_words({"value": -0.5, "ci_lo": -0.9, "ci_hi": -0.1})
    assert "no clear difference" in AT.pnl_words({"value": 0.16, "ci_lo": -0.11, "ci_hi": 0.39})
    assert "no clear difference" in AT.pnl_words({"value": 0.2, "ci_lo": None, "ci_hi": None})
    # the rest of the site is what it was: only index.html changes
    for name in ("hftbacktest.html", "nautilustrader.html", "databento.html"):
        if (tmp_path / "site-plain" / name).exists():
            assert (tmp_path / "site" / name).read_text() == (tmp_path / "site-plain" / name).read_text()
