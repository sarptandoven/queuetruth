"""`compare`'s honest intervals and `compare --watch` on synthetic data: the instrument-by-day clustered share
interval and its independent fallback, McNemar on the discordant fills (plain and Obuchowski), the drift of a gap
between two windows of orders, and the watch loop (baseline, drift, ALERT, exit codes, state file) end to end on
the synthetic live log of tests/fixtures/synth_small split into two windows (no market data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from queuetruth import cli
from queuetruth.compare import stats as cs
from queuetruth.compare import watch as cw
from queuetruth.errors import QtError
from queuetruth.stats import clustered_mcnemar, mcnemar, wilson

from . import synth_sims as ss

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "tests" / "fixtures" / "synth_small"
LIVE = DATA / "own_orders" / "live"


# ---- clustered intervals ----------------------------------------------------------------------------------------


def _keys(n_inst: int, n_days: int, per: int) -> list[tuple[str, str]]:
    return [(f"I{i}", f"2026-01-{d + 1:02d}") for i in range(n_inst) for d in range(n_days) for _ in range(per)]


def test_share_is_clustered_with_five_clusters_and_independent_below() -> None:
    keys = _keys(2, 3, 10)  # 6 instrument-day clusters
    rng = np.random.default_rng(1)
    ok = (rng.random(len(keys)) < 0.7).tolist()
    cl = cs.clusters(keys)
    assert cl.n == 6 and cl.clustered and cl.label() == "clustered by instrument and day, 6 clusters"
    s = cs.share(ok, cl, "t")
    assert s["interval"]["method"] == "instrument_day" and s["interval"]["n_clusters"] == 6
    assert s["ci_lo"] < s["share"] < s["ci_hi"]
    assert s == cs.share(ok, cl, "t")  # seeded: the same interval every run
    few = cs.clusters(_keys(2, 2, 10))  # 4 clusters: Wilson, labelled
    s4 = cs.share(ok[:40], few, "t")
    assert s4["interval"]["method"] == "independent"
    assert few.label() == "orders counted as independent: 4 instrument-day clusters, fewer than 5"
    assert (s4["ci_lo"], s4["ci_hi"]) == wilson(sum(ok[:40]), 40, edges="exact")
    assert cs.clusters([("I", "d")]).label().startswith("orders counted as independent: 1 instrument-day cluster,")


def test_correlated_clusters_widen_the_interval() -> None:
    # whole clusters fill or not: the clustered interval is much wider than Wilson's
    keys = _keys(4, 5, 20)
    cl = cs.clusters(keys)
    ok = [cl.labels[i] % 2 == 0 for i in range(len(keys))]
    s = cs.share(ok, cl, "t")
    w = wilson(sum(ok), len(ok), edges="exact")
    assert w is not None
    assert s["ci_hi"] - s["ci_lo"] > 2 * (w[1] - w[0])


def test_discordance_plain_and_clustered() -> None:
    few = cs.clusters(_keys(1, 2, 20))
    bt = [True] * 31 + [False] * 9
    lv = [False] * 31 + [True] * 9
    d = cs.discordance(bt, lv, few, "t")
    assert (d["backtest_only"], d["live_only"], d["test"]) == (31, 9, "mcnemar_exact")
    assert d["p"] == mcnemar(9, 31).p_exact
    line = cs.discordance_line(d)
    assert line == (
        "discordant fills: the backtest fills more orders than live: 31 backtest-only against 9 live-only, "
        f"{cs.pvalue(d['p'])}, orders counted as independent: 2 instrument-day clusters, fewer than 5."
    )
    many = cs.clusters(_keys(2, 4, 5))  # 8 clusters of 5 orders
    rng = np.random.default_rng(3)
    bt2 = (rng.random(40) < 0.6).tolist()
    lv2 = (rng.random(40) < 0.4).tolist()
    d2 = cs.discordance(bt2, lv2, many, "t")
    ref = clustered_mcnemar(np.array(lv2), np.array(bt2), many.labels, b=cs.RESAMPLES, seed=d2_seed())
    assert d2["test"] == "obuchowski" and d2["p"] == ref.p_primary and d2["ci_lo"] == ref.ci_lo
    assert d2["estimate"] == pytest.approx((sum(bt2) - sum(lv2)) / 40)
    assert cs.discordance_line(d2).endswith(", clustered by instrument and day, 8 clusters.")
    same = cs.discordance([True, False], [True, False], cs.clusters([1, 1]), "t")
    assert cs.discordance_line(same).startswith("discordant fills: the backtest and live fill the same orders")


def d2_seed() -> int:
    from queuetruth.stats import bootstrap_seed

    return bootstrap_seed(0, "t")


def test_pvalue_words() -> None:
    assert cs.pvalue(0.00059) == "p = 0.00059"
    assert cs.pvalue(0.00001) == "p < 0.0001"
    assert cs.pvalue(0.14) == "p = 0.14" and cs.pvalue(1.0) == "p = 1" and cs.pvalue(None) == "p n/a"


def test_drift_clustered_and_independent() -> None:
    kb = [f"b{i % 6}" for i in range(60)]
    kn = [f"n{i % 6}" for i in range(60)]
    base = cs.Agg.of(kb, [0.0] * 60)
    new = cs.Agg.of(kn, [1.0 if i % 2 else 0.0 for i in range(60)])
    d = cs.drift(base, new, "t")
    assert d["drift"] == pytest.approx(0.5) and d["interval"]["method"] == "instrument_day"
    assert d["interval"]["label"] == "clustered by instrument and day, 6 and 6 clusters"
    assert d["ci_lo"] <= 0.5 <= d["ci_hi"]
    small = cs.Agg.of(["x"] * 30, [1.0 if i % 2 else 0.0 for i in range(30)])
    d2 = cs.drift(base, small, "t")
    assert d2["interval"]["method"] == "independent" and d2["ci_lo"] < 0.5 < d2["ci_hi"]
    assert d2["interval"]["label"].endswith("1 instrument-day cluster in the smaller window, fewer than 5")
    assert cs.Agg.from_doc(json.loads(json.dumps(new.to_doc()))).to_doc() == new.to_doc()
    m = cs.mean_ci(new, "t")
    assert m["estimate"] == pytest.approx(0.5) and m["ci_lo"] < 0.5 < m["ci_hi"]
    assert cs.drift(cs.Agg(), new, "t")["drift"] is None


# ---- the watch logic on synthetic rows ---------------------------------------------------------------------------


def _rows(
    n: int, start_ns: int, backtest_rate: float, seed: int, days: int = 6, pnl_shift: float = 0.0
) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        bt, lv = rng.random() < backtest_rate, rng.random() < 0.5
        outcome = "both" if bt and lv else "backtest_only" if bt else "live_only" if lv else "neither"
        out.append(
            {
                "order": f"o{seed}-{i}",
                "outcome": outcome,
                "pnl_gap": float(rng.normal()) + pnl_shift,
                "markout_gap": None,
                "_entry_ns": start_ns + i,
                "_cluster": ("I", f"d{seed}-{i % days}"),
            }
        )
    return out


class _Fake:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def __call__(self, backtest: Any, live: Any, *, out: Path, **kw: Any) -> Any:
        from queuetruth.compare.run import CompareResult

        Path(out).mkdir(parents=True, exist_ok=True)
        doc = {"pnl": {"currency": "USD"}, "tz": "UTC", "answer": "x"}
        return CompareResult(doc, [], Path(out)), [dict(r) for r in self.rows]


def test_watch_baseline_drift_alert_and_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from queuetruth.compare import run as crun

    fake = _Fake()
    monkeypatch.setattr(crun, "_compare", fake)
    live = tmp_path / "live.jsonl"
    live.write_text("a\n")
    out = tmp_path / "w"
    t0 = 1_767_225_600_000_000_000  # 2026-01-01 00:00:00 UTC
    fake.rows = _rows(300, 1_000, 0.5, 1)
    code, lines, state = cw.check("bt", live, out=out, now_ns=t0)
    assert code == 0 and len(lines) == 1
    assert lines[0].startswith("2026-01-01 00:00:00 UTC  baseline: 300 paired orders entered until ")
    assert "fill-share gap " in lines[0] and " pp [" in lines[0] and "P&L gap per order " in lines[0]
    assert "markout gap 1s n/a (needs --data)" in lines[0]
    assert "(clustered by instrument and day, 6 clusters)" in lines[0]
    assert json.loads((out / "watch.json").read_text())["baseline"]["n_orders"] == 300
    # the live records did not change: nothing recomputed, the last result kept
    code, lines, _ = cw.check("bt", live, out=out, now_ns=t0 + 60_000_000_000)
    assert code == 0 and lines == ["2026-01-01 00:01:00 UTC  no new live records since 2026-01-01 00:00:00 UTC"]
    # new orders where the backtest fills far more: the drift interval excludes 0 and passes --alert-pp
    live.write_text("a\nb\n")
    fake.rows = fake.rows + _rows(300, 10_000, 0.95, 2)
    code, lines, state = cw.check("bt", live, out=out, now_ns=t0 + 120_000_000_000)
    assert code == 1
    assert lines[0].startswith(
        "2026-01-01 00:02:00 UTC  drift since the baseline: 300 new paired orders (baseline 300): fill-share gap +"
    )
    assert lines[0].endswith("(clustered by instrument and day, 6 and 6 clusters)")
    alert = lines[1]
    assert alert.startswith("ALERT: fill-share gap moved +") and alert.endswith(", at least --alert-pp 2")
    d = state["last"]["drift"]["fill"]
    assert d["ci_lo"] > 0 and d["drift"] > 0.02
    # unchanged again: the alert stands (exit 1) until new records arrive
    code, lines, _ = cw.check("bt", live, out=out, now_ns=t0 + 180_000_000_000)
    assert code == 1 and "no new live records" in lines[0]
    # a threshold above the drift: no alert
    code, lines, _ = cw.check("bt", live, out=out, now_ns=t0 + 240_000_000_000, force=True, alert_pp=90)
    assert code == 0 and len(lines) == 1
    # the P&L gap alerts only with --alert-pnl: here its interval includes 0, so even a 0 threshold is quiet
    code, lines, state = cw.check("bt", live, out=out, force=True, alert_pp=90, alert_pnl=0.0)
    p = state["last"]["drift"]["pnl"]
    assert code == 0 and len(lines) == 1 and p["ci_lo"] < 0 < p["ci_hi"]
    assert "; P&L gap per order +" in lines[0] and " USD [" in lines[0]
    live.write_text("a\nb\nc\n")
    fake.rows = fake.rows + _rows(300, 20_000, 0.5, 3, pnl_shift=5.0)
    code, lines, _ = cw.check("bt", live, out=out, alert_pp=90)
    assert code == 0 and len(lines) == 1  # no --alert-pnl: printed, never an alert
    code, lines, _ = cw.check("bt", live, out=out, force=True, alert_pp=90, alert_pnl=1.0)
    assert code == 1 and lines[1].startswith("ALERT: P&L gap per order moved +")
    assert lines[1].endswith(", at least --alert-pnl 1")


def test_watch_loop_exit_codes_and_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from queuetruth.compare import run as crun

    fake = _Fake()
    fake.rows = _rows(50, 1, 0.5, 1)
    monkeypatch.setattr(crun, "_compare", fake)
    live = tmp_path / "live.jsonl"
    live.write_text("a\n")
    got: list[str] = []
    naps: list[float] = []
    code = cw.watch("bt", live, out=tmp_path / "w", interval="5min", max_checks=2, echo=got.append, sleep=naps.append)
    assert code == 0 and naps == [300.0] and "baseline: 50 paired orders" in got[0]
    assert "no new live records" in got[1]
    assert cw.watch("bt", live, out=tmp_path / "w", once=True, echo=got.append) == 0

    def boom(*a: Any, **k: Any) -> Any:
        raise QtError("QT-INPUT-001", "compare: half-written file")

    monkeypatch.setattr(crun, "_compare", boom)
    live.write_text("a\nb\n")
    err: list[str] = []
    assert cw.watch("bt", live, out=tmp_path / "w", once=True, echo=err.append) == 2
    assert err[0].endswith(" UTC  error: QT-INPUT-001: compare: half-written file")
    assert cw.watch("bt", live, out=tmp_path / "w", max_checks=1, echo=err.append, sleep=naps.append) == 0
    with pytest.raises(QtError, match="--interval"):
        cw.watch("bt", live, out=tmp_path / "w", interval="soon")
    (tmp_path / "w" / "watch.json").write_text("{}")
    with pytest.raises(QtError, match="not a watch state"):
        cw.check("bt", live, out=tmp_path / "w")


def test_signature_follows_a_directory_and_a_growing_file(tmp_path: Path) -> None:
    d = tmp_path / "live"
    d.mkdir()
    (d / "a.parquet").write_bytes(b"1")
    s1 = cw.signature([d])
    (d / "b.parquet").write_bytes(b"2")
    s2 = cw.signature([d])
    f = tmp_path / "x.jsonl"
    f.write_text("1\n")
    s3 = cw.signature([f])
    with f.open("a") as h:
        h.write("2\n")
    assert len({s1, s2, s3, cw.signature([f])}) == 4 and cw.signature([d]) == s2


# ---- end to end on the synthetic live log -----------------------------------------------------------------------


@pytest.mark.requires_core
@pytest.mark.filterwarnings("ignore::queuetruth.live.LiveImportWarning")
def test_watch_cli_end_to_end_on_a_growing_live_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hbt = ss.write_hftbacktest(tmp_path / "hbt.jsonl", ss.sim_orders(LIVE))
    live = ss.part_log(LIVE, tmp_path / "live", 100)
    out = tmp_path / "w"
    argv = ["compare", "--backtest", str(hbt), "--live", str(live), "--out", str(out), "--once"]
    assert cli.main(argv) == 0
    first = capsys.readouterr().out.strip().splitlines()
    assert len(first) == 1 and " UTC  baseline: " in first[0] and "paired orders entered until " in first[0]
    assert (out / "compare.json").is_file() and (out / "watch.json").is_file()
    ss.part_log(LIVE, live, 200)  # the live log grows
    code = cli.main([*argv, "--alert-pp", "0", "--alert-ticks", "0"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert code in (0, 1) and (code == 1) == any(x.startswith("ALERT: ") for x in lines)
    assert " UTC  drift since the baseline: " in lines[0] and "new paired orders (baseline " in lines[0]
    state = json.loads((out / "watch.json").read_text())
    assert state["schema"] == "qt.compare_watch" and state["runs"] == 2 and state["last"]["n_new"] > 0
    assert cli.main([*argv, "--json"]) == 2  # one line per check: no JSON form
    assert "QT-USAGE-001" in capsys.readouterr().out
