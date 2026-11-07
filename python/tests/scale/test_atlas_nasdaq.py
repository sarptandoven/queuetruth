"""A Nasdaq TotalView-ITCH day next to IEX in the Fill-Realism Atlas, on synthetic data only:

- `plan.py`'s symbol sample keeps the same symbols per tier whatever else is in the day, fits its budget with the
  largest per-tier count, ranks tiers on the whole day, and leaves a day that fits (and its run id) unchanged;
- a synthetic ITCH day runs from split to site on a sample (`local_day.py --source itch`), and its atlas and an
  IEX day's atlas build one site with a page set per venue (`atlas_site.build_site`).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import itch_split
import pytest
from scale_itch import write_itch_day

S1 = 1_000_000_000
NASDAQ_DATE = "01022020"
SYMS = [f"N{i:02d}" for i in range(10)]


def _day(n: int, prefix: str = "S") -> list[dict]:
    """n symbols with distinct execution counts (rank i has n - i executions)."""
    return [
        {"symbol": f"{prefix}{i:04d}", "locate": i + 1, "messages": 1000 + i, "executions": n - i} for i in range(n)
    ]


def test_symbol_sample_is_per_tier_deterministic_and_fits_the_budget() -> None:
    import plan as P

    day = _day(700)  # 100 in T1, 400 in T2, 200 in T3
    tier = {d["symbol"]: P.tier_of(i + 1) for i, d in enumerate(day)}
    cost = {d["symbol"]: 1.0 for d in day}
    keep, n = P.sample_symbols(day, tier, cost, seed=0, date="01302020", per_tier=30)
    assert n == 30 and len(keep) == 90
    assert {t: sum(tier[s] == t for s in keep) for t in set(tier.values())} == {
        "T1_top100": 30,
        "T2_101_500": 30,
        "T3_501_1500": 30,
    }
    # a symbol's place in the sample depends on (seed, date, symbol) only: dropping half of another tier keeps T1's
    fewer = [d for d in day if tier[d["symbol"]] != "T2_101_500" or d["locate"] % 2]
    keep2, _ = P.sample_symbols(fewer, tier, cost, seed=0, date="01302020", per_tier=30)
    assert {s for s in keep2 if tier[s] == "T1_top100"} == {s for s in keep if tier[s] == "T1_top100"}
    # another date or seed draws another sample
    assert P.sample_symbols(day, tier, cost, seed=0, date="12302019", per_tier=30)[0] != keep
    assert P.sample_symbols(day, tier, cost, seed=1, date="01302020", per_tier=30)[0] != keep
    # a budget: the largest per-tier count that fits (a tier smaller than the count is kept whole)
    keep3, n3 = P.sample_symbols(day, tier, cost, seed=0, date="01302020", budget_core_s=400)
    assert n3 == 150 and len(keep3) == 100 + 150 + 150
    keep4, n4 = P.sample_symbols(day, tier, cost, seed=0, date="01302020", budget_core_s=500)
    assert n4 == 200 and len(keep4) == 100 + 200 + 200
    # ... and a day that fits keeps everything, with no sample
    assert P.sample_symbols(day, tier, cost, seed=0, date="01302020", budget_core_s=700) == ({*cost}, None)
    # never empty: a budget below one symbol per tier still keeps one per tier
    assert P.sample_symbols(day, tier, cost, seed=0, date="01302020", budget_core_s=0.5)[1] == 1


def _split(tmp: Path, symbols: list[str], date: str = NASDAQ_DATE, **kw: int) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / f"{date}.NASDAQ_ITCH50.gz"
    write_itch_day(src, symbols, **kw)
    a = argparse.Namespace(
        date=date,
        url=f"file://{src}",
        buckets=2,
        out=tmp / "split",
        symbols=None,
        allow_missing=False,
        level=1,
        min_free_gib=0.0,
    )
    assert itch_split.run_split(a) == 0
    return tmp / "split" / date


def test_plan_records_the_sample_and_a_day_that_fits_keeps_its_run_id(tmp_path: Path) -> None:
    import plan as P

    split = _split(tmp_path, SYMS, orders_per_symbol=60)
    common = ["--split", str(split), "--quota-rule", "fixed", "--fixed-n", "10", "--populations", "standard"]

    def run(name: str, *extra: str) -> dict:
        assert P.main([*common, "--out", str(tmp_path / name), *extra]) == 0
        return json.loads((tmp_path / name).read_text())

    whole = run("whole.json")
    fits = run("fits.json", "--sample-budget-core-h", "1000")
    assert "symbol_sample" not in fits and fits["run_id"] == whole["run_id"] and fits["units"] == whole["units"]

    sampled = run("sampled.json", "--sample-per-tier", "4")
    ss = sampled["symbol_sample"]
    assert ss["per_tier"] == 4 and ss["tiers"] == {"T1_top100": {"kept": 4, "of": 10}}
    # only the rule id, the seed and N enter the run id: the budget and the cost figures stay in the plan
    assert sampled["sources"][0]["symbol_sample"] == {"rule_id": "per_tier_sha256_v1", "seed": 0, "per_tier": 4}
    assert sampled["run_id"] != whole["run_id"]
    kept = {u["symbol"] for u in sampled["universe"]}
    assert len(kept) == 4 and kept == {s for u in sampled["units"] for s in u["symbols"]}
    out = [e for e in sampled["exclusions"] if e["reason"].startswith("outside the symbol sample")]
    assert {e["symbol"] for e in out} == set(SYMS) - kept
    assert out[0]["reason"] == "outside the symbol sample (T1_top100: 4 of 10 kept)"
    # the same sample again: the same plan (deterministic); budgets that keep the same 4 per tier give the same run
    # id and units as asking for 4, however the budget was set
    assert run("again.json", "--sample-per-tier", "4") == sampled
    cost4 = sum(u["cost_core_s"] for u in whole["universe"] if u["symbol"] in kept)
    step = min(u["cost_core_s"] for u in whole["universe"])
    for i, extra in enumerate((0.1 * step, 0.5 * step)):
        by_budget = run(f"budget{i}.json", "--sample-budget-core-h", f"{(cost4 + extra) / 3600:.12f}")
        assert by_budget["symbol_sample"]["per_tier"] == 4
        assert by_budget["run_id"] == sampled["run_id"] and by_budget["units"] == sampled["units"]
    with pytest.raises(SystemExit):
        P.main([*common, "--out", str(tmp_path / "x.json"), "--sample-per-tier", "2", "--sample-budget-core-h", "1"])


@pytest.mark.requires_core
def test_nasdaq_sample_day_and_iex_day_build_one_site(tmp_path: Path) -> None:
    import atlas
    import atlas_site
    import local_day
    import plan as P
    from test_iex_split import DATE as IEX_DATE
    from test_iex_split import MMDD as IEX_MMDD
    from test_iex_split import F, iex_capture

    # a synthetic Nasdaq day of two tiers (100 + 10 symbols) from split to site on the workflow's path: a sample
    # budget through local_day.py and --flag-policy exclude. The budget is what 3 symbols per tier predict, from a dry
    # plan of the same split with the quota rule local_day.py uses
    syms = [f"N{i:03d}" for i in range(110)]
    dry = _split(tmp_path / "dry", syms, orders_per_symbol=40, seed=5, max_step_ns=S1 // 2, priority=True)
    P.main(
        ["--split", str(dry), "--out", str(tmp_path / "dry.json"), "--quota-rule", "scale", "--sample-per-tier", "3"]
    )
    budget = json.loads((tmp_path / "dry.json").read_text())["symbol_sample"]["sample_core_h"] + 1e-6
    src = tmp_path / f"{NASDAQ_DATE}.NASDAQ_ITCH50.gz"
    write_itch_day(src, syms, orders_per_symbol=40, seed=5, max_step_ns=S1 // 2, priority=True)
    common = ["--buckets", "2", "--workers", "2", "--threads", "1", "--min-free-gib", "0"]
    rc = local_day.main(
        ["--source", "itch", "--date", NASDAQ_DATE, "--url", f"file://{src}", "--out", str(tmp_path / "nasdaq"),
         "--sample-budget-core-h", f"{budget:.6f}", "--unit-core-s", "100", "--unit-mem-gib", "3", "--site",
         "--flag-policy", "exclude", *common]
    )  # fmt: skip
    assert rc == 0
    nday = tmp_path / "nasdaq" / NASDAQ_DATE
    rep = json.loads((nday / "local_day.json").read_text())
    assert {"split", "plan", "units", "reduce", "combine", "site"} <= set(rep["stages"])
    assert rep["stages"]["plan"]["symbol_sample"]["tiers"] == {
        "T1_top100": {"kept": 3, "of": 100},
        "T2_101_500": {"kept": 3, "of": 10},
    }
    assert rep["stages"]["units"]["failed"] == [] and rep["stages"]["combine"]["flag_policy"] == "exclude"
    dm = json.loads((nday / "day" / "day_manifest.json").read_text())
    assert dm["symbol_sample"]["per_tier"] == 3 and dm["symbol_days"] == 6
    assert dm["symbols_by_tier"] == {"T1_top100": 3, "T2_101_500": 3}
    one = (nday / "site" / "index.html").read_text()
    assert "Nasdaq TotalView-ITCH" in one and "Symbol sample." in one and "Data provided for free by IEX" not in one

    # an IEX day (all symbols), then both venues' atlases in one site, IEX first
    isrc = tmp_path / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    F.write_gz(isrc, iex_capture())
    rc = local_day.main(
        ["--source", "iex", "--date", IEX_DATE, "--input", str(isrc), "--out", str(tmp_path / "iex"), *common,
         "--flag-policy", "label"]
    )  # fmt: skip
    assert rc == 0
    iatlas, natlas = tmp_path / "iex" / IEX_MMDD / "atlas", nday / "atlas"
    site = tmp_path / "site"
    args = ["--venue", f"iex={iatlas}", "--venue", f"nasdaq={natlas}", "--out", str(site), "--horizon", "1s"]
    assert atlas_site.main([*args, "--compat-link"]) == 0
    index, nindex = (site / "index.html").read_text(), (site / "nasdaq" / "index.html").read_text()

    # the default address is IEX, with IEX's attribution, root, tables and verify command, and a link to Nasdaq
    imanifest = json.loads((iatlas / "atlas_manifest.json").read_text())
    nmanifest = json.loads((natlas / "atlas_manifest.json").read_text())
    assert imanifest["atlas_root"] != nmanifest["atlas_root"]
    assert '<nav class="venues">Venue: <span aria-current="page">IEX DEEP+</span> | ' in index
    assert '<a href="nasdaq/index.html">Nasdaq TotalView-ITCH</a></nav>' in index
    assert atlas_site.IEX_ATTRIBUTION in index and imanifest["atlas_root"] in index
    assert nmanifest["atlas_root"] not in index and "Symbol sample." not in index
    assert "scripts/scale/iex_split.py --url" in index and "itch_split.py" not in index
    assert "<title>Fill-Realism Atlas</title>" in index and 'href="compat.html"' in index
    assert 'releases/tag/atlas-days">atlas-days</a>' in index and "<code>day-&lt;date&gt;.tar.gz</code>" in index
    assert "fill-realism-atlas.md#verify-a-number" in index

    # the Nasdaq pages: their own root, tables, sample note, verify command and attribution, and a link back
    assert '<a href="../index.html">IEX DEEP+</a> | <span aria-current="page">Nasdaq TotalView-ITCH</span>' in nindex
    assert nmanifest["atlas_root"] in nindex and imanifest["atlas_root"] not in nindex
    assert "Data provided for free by IEX" not in nindex and "Nasdaq TotalView-ITCH 5.0 sample files" in nindex
    assert "Symbol sample." in nindex and "T1_top100 3 of 100, T2_101_500 3 of 10" in nindex
    # its days are verified from the Nasdaq release, never from the IEX one
    assert 'releases/tag/atlas-days-nasdaq">atlas-days-nasdaq</a>' in nindex
    assert "<code>nasdaq-day-&lt;date&gt;.tar.gz</code>" in nindex and 'releases/tag/atlas-days"' not in nindex
    assert "fill-realism-atlas.md#verify-a-nasdaq-day" in nindex and 'href="https://emi.nasdaq.com/' in nindex
    assert f"scripts/scale/itch_split.py --url file://{src} --date {NASDAQ_DATE} --buckets 2" in nindex
    assert "<title>Fill-Realism Atlas (Nasdaq TotalView-ITCH)</title>" in nindex
    assert "compat.html" not in nindex and "<h2>Tier ALL</h2>" in nindex
    for lib in ("hftbacktest.html", "nautilustrader.html"):
        ipage, npage = (site / lib).read_text(), (site / "nasdaq" / lib).read_text()
        assert atlas_site.IEX_ATTRIBUTION in ipage and "Data provided for free by IEX" not in npage
        assert 'href="../index.html">IEX DEEP+</a>' in npage and "Symbol sample." in npage
        assert 'href="nasdaq/index.html">Nasdaq TotalView-ITCH</a>' in ipage
    idata = json.loads((site / "data" / "leaderboard.json").read_text())
    ndata = json.loads((site / "nasdaq" / "data" / "leaderboard.json").read_text())
    assert (idata["source"], ndata["source"]) == ("iex", "nasdaq") and "attribution" not in ndata
    nday0 = json.loads((site / "nasdaq" / "data" / "atlas_manifest.json").read_text())["days"][0]
    assert nday0["symbol_sample"] and nday0["venue"] == "nasdaq" and imanifest["days"][0]["venue"] == "iex"

    # static and byte-identical for the same inputs
    again = tmp_path / "again"
    assert atlas_site.main([*args[:-4], "--out", str(again), "--horizon", "1s", "--compat-link"]) == 0
    for p in sorted(site.rglob("*")):
        if p.is_file():
            assert (again / p.relative_to(site)).read_bytes() == p.read_bytes(), p
    assert "<script" not in index + nindex
    with pytest.raises(SystemExit):
        atlas_site.main(["--venue", f"iex={iatlas}", "--venue", f"iex={natlas}", "--out", str(again)])
    with pytest.raises(SystemExit):
        atlas_site.main(["--atlas", str(iatlas), "--venue", f"iex={iatlas}", "--out", str(again)])
    # a venue label that does not match the atlas's days is refused (IEX data would lose IEX's attribution), and so
    # is one atlas over both venues' days
    with pytest.raises(SystemExit, match="attribution is per venue"):
        atlas_site.main(["--venue", f"iex={natlas}", "--venue", f"nasdaq={iatlas}", "--out", str(tmp_path / "bad")])
    with pytest.raises(SystemExit, match="more than one venue"):
        atlas.combine([tmp_path / "iex" / IEX_MMDD / "day", nday / "day"], tmp_path / "mixed")
