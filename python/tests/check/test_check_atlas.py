"""`queuetruth check` without data: the estimate from the atlas touch tables, on synthetic backtests and a synthetic
atlas (invented numbers): the answer, its interval, the tiers and bar length, and every refusal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import queuetruth as qt
from queuetruth import cli
from queuetruth.check.estimate import bar_from_times, bar_of, tier_of
from queuetruth.errors import QtError

from . import synth_atlas as sa
from . import synth_backtests as sb

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "tests" / "fixtures" / "synth_small"
M = 60_000_000_000


@pytest.fixture(scope="module")
def orders() -> list[sb.Order]:
    return sb.orders(DATA)


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory, orders: list[sb.Order]) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("bt")
    return {
        "hftbacktest": sb.write_hftbacktest(d / "hbt_snapshots.jsonl", orders),
        "nautilus": sb.write_nautilus(d / "orders_report.csv", orders),
        "backtrader": sb.write_backtrader(d / "bt_orders.jsonl", orders),
        "vectorbt": sb.write_vectorbt(d / "vbt_orders.csv", orders),
    }


@pytest.fixture
def atlas(tmp_path: Path) -> Path:
    """QTAU6 is in the atlas (tier T1); QTBU6 is not."""
    return sa.write_atlas(tmp_path / "site", symbols={"QTAU6": "T1_top100"})


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        got = capsys.readouterr()
        return code, got.out, got.err

    return _run


def _expected(orders: list[sb.Order], tier_b: str) -> float:
    """Expected real fills by hand: each touch fill times P(real | touch) of its tier and side."""
    tot = 0.0
    for o in orders:
        if not o.filled:
            continue
        tier = "T1_top100" if o.symbol == "QTAU6" else tier_b
        side = "buy" if o.side == 1 else "sell"
        tot += sa.CELLS[(tier, side)][0][0]
    return tot


def test_bar_and_tier_words() -> None:
    assert [bar_of(x) for x in ("1m", "1min", "5m", "5min", "1h", "60m", "3600s")] == [
        "1m", "1m", "5m", "5m", "1h", "1h", "1h"
    ]  # fmt: skip
    with pytest.raises(QtError, match="1m, 5m and 1h, not 15m"):
        bar_of("15m")
    assert tier_of("t1") == "T1_top100" and tier_of("all") == "ALL" and tier_of("T5_rest") == "T5_rest"


def test_bar_from_the_log_times() -> None:
    day = 86_400 * 10**9 * 20_000
    assert bar_from_times([day + h * 60 * M for h in (9, 10, 13, 14, 15)])[0] == "1h"
    assert bar_from_times([day + k * M for k in (5, 35, 50, 55, 65)])[0] == "5m"
    assert bar_from_times([day + 7 * M, day + 9 * M])[0] == "1m"
    bar, why = bar_from_times([day + 5 * M, day + 35 * M])  # two times on a 5m grid: too few to tell
    assert bar is None and "too few" in why
    assert bar_from_times([day + 7 * M + 10**9])[0] is None  # whole seconds, not minutes
    bar, why = bar_from_times([day + 7 * M + 1_000])
    assert bar is None and "tick-driven" in why
    bar, why = bar_from_times([day + k * 15 * M for k in (1, 3, 5, 6, 9)])
    assert bar is None and "15 minute grid" in why


def test_estimate_answer_interval_and_groups(tmp_path: Path, atlas: Path, files: dict[str, Path], orders: list) -> None:
    res = qt.check(files["hftbacktest"], bar="5m", tier="ALL", atlas=atlas, out=tmp_path / "o")
    d = res.doc
    n_filled = sum(o.filled for o in orders)
    assert d["schema"] == "qt.check_estimate" and d["mode"] == "atlas" and d["backtester"] == "hftbacktest"
    assert d["n_resting"] == len(orders) and d["backtest_filled"] == n_filled and d["bar"] == "5m"
    want = _expected(orders, "ALL")
    rf = d["real_fills"]
    assert rf["expected"] == pytest.approx(want, abs=0.05)
    assert rf["ci_lo"] <= want <= rf["ci_hi"] and rf["ci_lo"] < rf["ci_hi"] <= n_filled
    assert d["touch_only"]["expected"] == pytest.approx(n_filled - want, abs=0.05)
    assert {(g["tier"], g["side"]) for g in d["groups"]} <= set(sa.CELLS)
    assert sum(g["orders"] for g in d["groups"]) == len(orders)
    p = d["pnl_overstated_ticks_per_order"]
    assert p["ci_lo"] < p["value"] < p["ci_hi"] and p["ci_lo"] > 0
    # the answer comes first, in plain words, and says what it rests on
    s = res.summary
    assert s.startswith(f"Your backtest filled {n_filled} of {len(orders)} resting limit orders when a 5m bar touched")
    assert "95% interval" in s and "touch-only" in s and "too high" in s
    assert "IEX DEEP+, one venue, 3 days" in s and "not an exact replay" in s and "--data" in s
    assert any("not in the atlas use --tier ALL: QTBU6" in n for n in d["notes"])
    assert any("QTAU6 T1_top100" in n for n in d["notes"])
    assert json.loads((tmp_path / "o" / "check.json").read_text())["answer"] == s
    assert (tmp_path / "o" / "groups.csv").read_text().startswith("tier,side,bar,orders")
    t = res.text()
    assert t.splitlines()[0] == s and "P(real | touch)" in t and "What it rests on:" in t and "IEX" in t
    assert "Skip to the answer" in res.to_html()
    assert len(res.table()) == len(d["groups"])


def test_the_interval_matches_a_direct_simulation(atlas: Path, files: dict[str, Path], orders: list) -> None:
    """The 95% interval covers both the tables' uncertainty and the binomial noise: wider than either alone."""
    d = qt.check(files["hftbacktest"], bar="5m", tier="ALL", atlas=atlas, seed=7).doc
    n = sum(g["backtest_filled"] for g in d["groups"])
    width = d["real_fills"]["ci_hi"] - d["real_fills"]["ci_lo"]
    p = d["real_fills"]["expected"] / n
    binom_only = 2 * 1.96 * np.sqrt(n * p * (1 - p))
    assert width >= binom_only - 2
    again = qt.check(files["hftbacktest"], bar="5m", tier="ALL", atlas=atlas, seed=7).doc
    assert again["real_fills"] == d["real_fills"]  # seeded


def test_formats_agree(atlas: Path, files: dict[str, Path]) -> None:
    got = {
        k: qt.check(files[k], bar="1m", tier="ALL", atlas=atlas).doc["real_fills"]["expected"]
        for k in ("hftbacktest", "nautilus", "backtrader")
    }
    assert len(set(got.values())) == 1, got
    vbt = qt.check(files["vectorbt"], bar="1m", tier="ALL", atlas=atlas).doc
    assert vbt["n_resting"] == vbt["backtest_filled"] == 20 and any("fills only" in n for n in vbt["notes"])


def test_symbol_not_in_the_atlas_asks_for_a_tier(atlas: Path, files: dict[str, Path]) -> None:
    with pytest.raises(QtError) as e:
        qt.check(files["hftbacktest"], bar="5m", atlas=atlas)
    assert e.value.code == "QT-USAGE-001"
    assert "QTBU6 is not in the atlas" in e.value.message and "tier" in e.value.message
    assert "--tier" in (e.value.hint or "")


def test_missing_cells_are_refused_not_invented(tmp_path: Path, files: dict[str, Path]) -> None:
    site = sa.write_atlas(tmp_path / "a", symbols={"QTAU6": "T1_top100"})
    with pytest.raises(QtError, match="no tier T3_501_1500"):
        qt.check(files["hftbacktest"], bar="5m", tier="T3", atlas=site)
    with pytest.raises(QtError, match="no 1h bars for tier"):
        qt.check(files["hftbacktest"], bar="1h", tier="ALL", atlas=site)
    with pytest.raises(QtError, match="no sell side for tier T5_rest"):
        qt.check(files["hftbacktest"], bar="5m", tier="T5", atlas=site)
    cells = {**sa.CELLS, ("ALL", "buy"): ((0.6, None, None), (0.2, 0.1, 0.3))}
    site2 = sa.write_atlas(tmp_path / "b", touch=sa.touch_doc(cells), symbols={"QTAU6": "T1_top100"})
    with pytest.raises(QtError, match="no p_real_given_rule with an interval"):
        qt.check(files["hftbacktest"], bar="5m", tier="ALL", atlas=site2)


def test_no_touch_tables_yet(tmp_path: Path, files: dict[str, Path]) -> None:
    site = sa.write_atlas(tmp_path / "s", touch=None)
    with pytest.raises(QtError) as e:
        qt.check(files["hftbacktest"], bar="5m", tier="ALL", atlas=site)
    m = e.value.message
    assert "no touch tables yet" in m and "backfill" in m and "3 day(s)" in m and "no number" in m
    assert "--data" in (e.value.hint or "")


def test_bar_length_is_needed_when_the_log_does_not_say(atlas: Path, files: dict[str, Path]) -> None:
    # the synthetic orders are at whole seconds, not on a bar grid
    with pytest.raises(QtError, match="cannot tell the backtest's bar length") as e:
        qt.check(files["hftbacktest"], tier="ALL", atlas=atlas)
    assert "--bar" in (e.value.hint or "")
    res = qt.check(files["backtrader"], tier="ALL", atlas=atlas, options={"timeframe": "5m"})
    assert res.doc["bar"] == "5m" and res.doc["bar_from"] == "the importer option timeframe"


def test_bar_read_from_a_bar_backtest(tmp_path: Path, atlas: Path, orders: list[sb.Order]) -> None:
    on_grid = [
        sb.Order(o.oid, o.instrument_id, o.side, o.px, o.qty, (o.ts // (5 * M)) * 5 * M, o.filled) for o in orders
    ]
    p = sb.write_nautilus(tmp_path / "orders_report.csv", on_grid)
    # fills come FILL_AFTER (5 s) after placement: off the grid, so move them onto the next bar too
    text = p.read_text().splitlines()
    fixed = [text[0]]
    for line in text[1:]:
        cols = line.split(",")
        cols[-1] = str(int(cols[-2]) + 5 * M)
        fixed.append(",".join(cols))
    p.write_text("\n".join(fixed) + "\n")
    res = qt.check(p, tier="ALL", atlas=atlas)
    assert res.doc["bar"] == "5m" and res.doc["bar_from"].startswith("read from the log")


def test_consent_before_any_download(tmp_path: Path, files: dict[str, Path], monkeypatch: Any) -> None:
    site = sa.write_atlas(tmp_path / "site", symbols={"QTAU6": "T1_top100"})
    base = site / "atlas" / "v1"
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return (base / url.removeprefix("https://example.org/atlas/v1/")).read_bytes()

    monkeypatch.setenv("QT_CACHE_DIR", str(tmp_path / "cache"))
    from queuetruth.atlas import _lookup as L

    monkeypatch.setattr(L, "_get", fetch)
    with pytest.raises(QtError, match="nothing was downloaded"):
        qt.check(files["hftbacktest"], bar="5m", tier="ALL", atlas="https://example.org/atlas/v1/")
    assert calls == []
    res = qt.check(files["hftbacktest"], bar="5m", tier="ALL", atlas="https://example.org/atlas/v1/", yes=True)
    assert res.doc["real_fills"]["expected"] > 0 and any(c.endswith("touch.json") for c in calls)


def test_cli_without_data(tmp_path: Path, run: Any, atlas: Path, files: dict[str, Path]) -> None:
    argv = ["check", str(files["nautilus"]), "--atlas", str(atlas), "--bar", "5m", "--tier", "ALL"]
    code, out, err = run(*argv, "--out", str(tmp_path / "c"), "--html")
    assert code == 0, err
    assert out.splitlines()[0].startswith("Your backtest filled 20 of 40 resting limit orders when a 5m bar")
    assert (tmp_path / "c" / "check.html").is_file()
    code, out, _ = run(*argv, "--json", "--out", str(tmp_path / "j"))
    doc = json.loads(out)
    assert code == 0 and doc["schema"] == "qt.check_estimate" and doc["bar"] == "5m"
    code, out, err = run("check", str(files["nautilus"]), "--atlas", str(atlas), "--bar", "5m")
    assert code == 2 and out == "" and err.startswith("QT-USAGE-001: check: QTBU6 is not in the atlas")
    code, out, err = run("check", str(files["nautilus"]), "--data", str(DATA), "--tier", "T1")
    assert code == 2 and "apply only without --data" in err


def test_cli_asks_before_downloading(run: Any, files: dict[str, Path], tmp_path: Path, monkeypatch: Any) -> None:
    from queuetruth.atlas import _lookup as L

    monkeypatch.setenv("QT_CACHE_DIR", str(tmp_path / "cache"))

    def fetch(url: str) -> bytes:
        raise AssertionError(f"downloaded {url}")

    monkeypatch.setattr(L, "_get", fetch)
    code, out, err = run("check", str(files["nautilus"]), "--bar", "5m", "--tier", "ALL")
    assert code == 2 and out == ""
    assert "check: needs to download the atlas data API" in err and "--yes" in err and "--atlas DIR" in err
