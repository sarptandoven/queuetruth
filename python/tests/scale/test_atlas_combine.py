"""`atlas.py combine` over several days, on synthetic day tables only.

A day's cells take about 0.65 GB in memory and its P&L cells about 0.6 GB on a real IEX day, so a window of 20 days
does not fit a 16 GB runner as one table. `combine` reads the days one (mode, shadow population) slice at a time;
these tests check that no concatenation holds more than one slice, and that the numbers are those of the whole
window: pooled per-symbol and per-tier ratios, a symbol's home tier, and flagged symbol-days under both policies.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

S1 = 1_000_000_000
MODES = ("corrected", "faithful")
POPS = ("large", "standard")
MODELS = ("model_a", "model_b")
HORIZONS = (S1, 10 * S1)
SYMS = ("AAA", "BBB", "CCC", "DDD")
DATES = ("10052026", "10062026", "10072026")  # MMDDYYYY, as reduced days name them
SESSION = (20731, 20732, 20733)  # session_date: days since the epoch
FLAGGED = ("DDD", 1)  # DDD's symbol-day on the second date did not pass the replay checks
SUMS = ("n", "sum_p", "n_y", "sum_d", "sum_sq", "sum_ll", "qty_n", "qty_num", "qty_den", "b", "c")


def _tier(sym: str, k: int) -> str:
    """CCC is in the top tier on two days of three, so that is its home tier."""
    if sym == "CCC":
        return "T2_101_500" if k == 2 else "T1_top100"
    return "T1_top100" if sym in ("AAA", "BBB") else "T2_101_500"


def _sums(rng: np.random.Generator) -> dict[str, float | int]:
    n = int(rng.integers(20, 80))
    return {
        "n": n,
        "sum_p": float(rng.uniform(0, n)),
        "n_y": int(rng.integers(0, n)),
        "sum_d": float(rng.normal(0, 3)),
        "sum_sq": float(rng.uniform(0, n)),
        "sum_ll": float(rng.uniform(0, 2 * n)),
        "qty_n": n,
        "qty_num": float(rng.normal(0, 5)),
        "qty_den": float(n * 100),
        "b": int(rng.integers(0, 5)),
        "c": int(rng.integers(0, 5)),
    }


def _write_day(root: Path, k: int, rng: np.random.Generator) -> Path:
    d = root / f"day-{DATES[k]}"
    d.mkdir()
    cells, pnl, strata = [], [], []
    for mode in MODES:
        for pop in POPS:
            for model in MODELS:
                for hz in HORIZONS:
                    for sym in SYMS:
                        ok = (sym, k) != FLAGGED
                        key = {"mode": mode, "shadow_pop": pop, "model_id": model}
                        at = {"horizon_ns": hz, "symbol": sym, "session_date": SESSION[k], "tier": _tier(sym, k)}
                        flags = {"replay_ok": ok, "trading_ok": True, "exact_ok": ok}
                        # a population row besides the whole-population one ("" is what the atlas reads)
                        for population in ("", "near_touch"):
                            cells.append({**key, "population": population, **at, **flags, **_sums(rng)})
                        pn = int(rng.integers(10, 40))
                        pnl.append(
                            {**key, **at, **flags, "pnl_n": pn, "pnl_n_bps": pn}
                            | {f: float(rng.normal(0, 2)) for f in ("pnl_grant", "pnl_deny", "pnl_timing")}
                            | {"pnl_model": float(rng.normal(1, 1)), "pnl_exact": float(rng.normal(1, 1))}
                            | {"pnl_bias": float(rng.normal(0, 2)), "pnl_bias_bps": float(rng.normal(0, 2))}
                        )
                    for tier in ("T1_top100", "T2_101_500"):
                        for ok in (True, False):
                            strata.append(
                                {**key, "population": "", "stratum_kind": "queue_decile", "stratum": "0"}
                                | {"horizon_ns": hz, "tier": tier, "session_date": SESSION[k], "exact_ok": ok}
                                | _sums(rng)
                            )
    pq.write_table(pa.Table.from_pylist(cells), d / "cells_all.parquet")
    pq.write_table(pa.Table.from_pylist(pnl), d / "pnl_cells.parquet")
    pq.write_table(pa.Table.from_pylist(strata), d / "strata_by_tier.parquet")
    flagged = {FLAGGED[0]: {"replay_ok": False, "trading_ok": True, "flags": {"crossed_books": 1}}}
    manifest = {
        "date": DATES[k],
        "run_id": f"run{k}",
        "merkle_root": f"{k + 1:064x}",
        "units_valid": 1,
        "units_planned": 1,
        "source": {"format": "iex_deep_plus"},
        "problems": [],
        "flagged_units": [],
        "symbol_days": len(SYMS),
        "flagged_symbols": flagged if k == FLAGGED[1] else {},
        "trading_state": {"known": True},
    }
    (d / "day_manifest.json").write_text(json.dumps(manifest))
    return d


@pytest.fixture
def days(tmp_path: Path) -> list[Path]:
    rng = np.random.default_rng(7)
    # given out of date order: combine sorts them
    return [_write_day(tmp_path, k, rng) for k in (2, 0, 1)]


def _whole(days: list[Path], name: str) -> pa.Table:
    t = pa.concat_tables([pq.read_table(d / name) for d in days])
    return t.filter(pc.equal(t["population"], "")) if "population" in t.column_names else t


@pytest.mark.parametrize("policy", ["exclude", "label"])
def test_combine_reads_a_slice_at_a_time_and_pools_the_whole_window(days, tmp_path, monkeypatch, policy) -> None:
    import atlas

    concat = pa.concat_tables
    held: list[int] = []

    def watch(tables, *a, **kw):
        t = concat(tables, *a, **kw)
        if "symbol" in t.column_names:  # a cells or P&L cells table (the strata have no symbol)
            held.append(t.num_rows)
        return t

    monkeypatch.setattr(pa, "concat_tables", watch)
    out = tmp_path / f"atlas-{policy}"
    res = atlas.combine(days, out, bootstrap_b=50, flag_policy=policy)
    monkeypatch.setattr(pa, "concat_tables", concat)

    # one slice: the population-"" rows of one (mode, shadow population) over the days
    one_slice = len(DATES) * len(MODELS) * len(HORIZONS) * len(SYMS)
    assert held and max(held) <= one_slice
    assert res["days"] == 3 and res["distinct_symbols"] == len(SYMS)

    cells = _whole(days, "cells_all.parquet")
    if policy == "exclude":
        cells = cells.filter(cells["exact_ok"])
    per = pq.read_table(out / "per_symbol.parquet").to_pylist()
    keys = [(r["mode"], r["shadow_pop"], r["model_id"], r["horizon_ns"], r["symbol"]) for r in per]
    assert keys == sorted(keys) and len(keys) == len(MODES) * len(POPS) * len(MODELS) * len(HORIZONS) * len(SYMS)
    for r in per:
        sel = cells.filter(
            pc.and_(
                pc.and_(pc.equal(cells["mode"], r["mode"]), pc.equal(cells["shadow_pop"], r["shadow_pop"])),
                pc.and_(
                    pc.and_(pc.equal(cells["model_id"], r["model_id"]), pc.equal(cells["horizon_ns"], r["horizon_ns"])),
                    pc.equal(cells["symbol"], r["symbol"]),
                ),
            )
        )
        n = pc.sum(sel["n"]).as_py()
        assert r["n"] == n and r["n_days"] == sel.num_rows
        assert r["bias"] == pytest.approx(pc.sum(sel["sum_d"]).as_py() / n, abs=1e-12)
        assert r["tier"] == ("T1_top100" if r["symbol"] in ("AAA", "BBB", "CCC") else "T2_101_500")
        flagged_here = policy == "label" and r["symbol"] == FLAGGED[0]
        assert r["n_flagged_days"] == (1 if flagged_here else 0)
        assert (r["flags"] is not None) == flagged_here

    # the leaderboard pools every day of a slice and tier
    board = pq.read_table(out / "leaderboard_by_tier.parquet").to_pylist()
    tiers = {"T1_top100", "T2_101_500", "ALL"}
    assert {(r["mode"], r["shadow_pop"], r["tier"]) for r in board} == {
        (m, p, t) for m in MODES for p in POPS for t in tiers
    }
    for r in board:
        sel = cells.filter(
            pc.and_(
                pc.and_(pc.equal(cells["mode"], r["mode"]), pc.equal(cells["shadow_pop"], r["shadow_pop"])),
                pc.and_(pc.equal(cells["model_id"], r["model_id"]), pc.equal(cells["horizon_ns"], r["horizon_ns"])),
            )
        )
        if r["tier"] != "ALL":
            sel = sel.filter(pc.equal(sel["tier"], r["tier"]))
        assert r["n"] == pc.sum(sel["n"]).as_py() and r["n_dates"] == len(pc.unique(sel["session_date"]))
        assert r["fill_rate_bias_value"] == pytest.approx(pc.sum(sel["sum_d"]).as_py() / r["n"], abs=1e-12)

    pnl = _whole(days, "pnl_cells.parquet")
    if policy == "exclude":
        pnl = pnl.filter(pnl["exact_ok"])
    for r in (x for x in pq.read_table(out / "pnl_by_tier.parquet").to_pylist() if x["tier"] == "ALL"):
        sel = pnl.filter(
            pc.and_(
                pc.and_(pc.equal(pnl["mode"], r["mode"]), pc.equal(pnl["shadow_pop"], r["shadow_pop"])),
                pc.and_(pc.equal(pnl["model_id"], r["model_id"]), pc.equal(pnl["horizon_ns"], r["horizon_ns"])),
            )
        )
        n = pc.sum(sel["pnl_n"]).as_py()
        assert r["n"] == n and r["pnl_bias_ticks_value"] == pytest.approx(
            pc.sum(sel["pnl_bias"]).as_py() / n, abs=1e-12
        )

    man = json.loads((out / "atlas_manifest.json").read_text())
    assert [d["date"] for d in man["days"]] == list(DATES)
    flagged_rows = len(POPS) * len(MODES) * len(MODELS) * len(HORIZONS)
    assert man["flagged_cell_rows"] == flagged_rows and man["flag_policy"] == policy
    assert pq.read_table(out / "maps_by_tier.parquet").num_rows > 0
