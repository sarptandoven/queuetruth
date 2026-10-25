"""`forecast score` end to end: the leakage guard, policies, baselines without look-ahead, known-edge recovery,
categorical and continuous questions, outputs and determinism."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from forecast.helpers import HOUR, SEC, T0, Clock, commit, fc, resolve
from queuetruth.errors import QtError
from queuetruth.forecast import ledger as lg
from queuetruth.forecast.engine import score
from queuetruth.forecast.power import brier_edge_moments
from queuetruth.forecast.schemas import ForecastScorecard
from queuetruth.forecast.simulate import binary_world
from queuetruth.forecast.tables import BETS, ELIGIBILITY, SCORES
from queuetruth.schemas import parse_document


def _status(card: ForecastScorecard) -> dict[str, int]:
    return card.counts.by_status


def _fs(card: ForecastScorecard, who: str):  # type: ignore[no-untyped-def]
    return next(f for f in card.forecasters if f.forecaster == who)


def _spec(led: Path, **kw: object) -> dict:
    return {"ledger": {"path": str(led)}, "bootstrap_b": 200, **kw}


# ---- the leakage guard ----------------------------------------------------------------------------------------------


def test_guard_rejects_late_and_untrusted_forecasts(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    cutoff = T0 + 10 * HOUR
    resolved = T0 + 12 * HOUR
    commit(led, [fc("q1", "early", 1, p=0.8), fc("q1", "lead", 2, p=0.8, as_of=T0 + HOUR)], clock)
    clock.now = cutoff - SEC
    commit(led, [fc("q1", "last_second", 3, p=0.8)], clock)
    clock.now = cutoff
    commit(led, [fc("q1", "at_cutoff", 4, p=0.99)], clock)
    clock.now = cutoff + HOUR
    commit(led, [fc("q1", "after_cutoff", 5, p=0.99)], clock)
    commit(led, [fc("q1", "backdated", 6, p=0.99)], clock, declared_at_ns=T0)
    clock.now = resolved + HOUR
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": resolved, "info_cutoff": cutoff}], clock)
    commit(led, [fc("q1", "after_resolution", 7, p=1.0)], clock)
    card = score(_spec(led))
    st = {r.forecaster: r.status for r in card.rejections}
    assert st == {
        "lead": "as_of_after_commit",
        "at_cutoff": "after_cutoff",
        "after_cutoff": "after_cutoff",
        "backdated": "declared_clock",
        "after_resolution": "after_resolution_entry",
    }
    assert {f.forecaster for f in card.forecasters} == {"early", "last_second"}
    assert _status(card) == {
        "scored": 2,
        "as_of_after_commit": 1,
        "after_cutoff": 2,
        "declared_clock": 1,
        "after_resolution_entry": 1,
    }
    assert any("rejected" in w for w in card.warnings)
    # the declared entry counts once allowed (its time is before the cutoff); the others never do
    allowed = score(_spec(led, allow_declared_clock=True))
    assert {f.forecaster for f in allowed.forecasters} == {"early", "last_second", "backdated"}
    # a minimum lead time removes the last-second forecast
    lead = score(_spec(led, min_lead_ns=HOUR))
    assert {r.forecaster: r.status for r in lead.rejections}["last_second"] == "short_lead"


def test_guard_uses_ledger_order_even_with_future_timestamps(tmp_path: Path) -> None:
    """A resolution recorded early (its resolved_at in the future) still closes the question in the chain."""
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.6)], clock)
    resolve(led, [{"question_id": "q1", "outcome": 0, "resolved_at": T0 + 100 * HOUR}], clock)
    commit(led, [fc("q1", "b", 2, p=0.1)], clock)
    card = score(_spec(led))
    assert [(r.forecaster, r.status) for r in card.rejections] == [("b", "after_resolution_entry")]


def test_corrected_resolution_changes_the_outcome_not_the_cutoff(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.9)], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": "q1", "outcome": 0, "resolved_at": T0 + 2 * HOUR}], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": T0 + 50 * HOUR}], clock, allow_correction=True)
    card = score(_spec(led))
    s = _fs(card, "a").scores
    assert next(x for x in s if x.metric == "brier").mean.value == pytest.approx(0.01)  # outcome 1 scored
    sel = _fs(card, "a").selection
    assert (sel.outcome_corrected, sel.cutoff_tightened, sel.voided_after_outcome) == (1, 0, 0)
    assert any("whose outcome a correction changed" in w for w in card.warnings)


def test_a_correction_never_loosens_the_cutoff(tmp_path: Path) -> None:
    """A forecast recorded after the first resolution's cutoff (but before its ledger entry) stays late when a
    correction moves resolved_at later."""
    led = tmp_path / "l.jsonl"
    clock = Clock(T0 + 5 * HOUR)
    commit(led, [fc("q1", "late", 1, p=0.9), fc("q2", "ok", 2, p=0.9)], clock)
    clock.now = T0 + 20 * HOUR
    resolve(
        led,
        [
            {"question_id": "q1", "outcome": 1, "resolved_at": T0 + 3 * HOUR},
            {"question_id": "q2", "outcome": 1, "resolved_at": T0 + 10 * HOUR},
        ],
        clock,
    )
    assert {r.forecaster: r.status for r in score(_spec(led)).rejections} == {"late": "after_cutoff"}
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": T0 + 50 * HOUR}], clock, allow_correction=True)
    assert {r.forecaster: r.status for r in score(_spec(led)).rejections} == {"late": "after_cutoff"}


def test_a_correction_can_tighten_the_cutoff(tmp_path: Path) -> None:
    """A correction that says the outcome was knowable earlier moves the cutoff earlier: a forecast between the new
    and the old cutoff becomes late, and the scorecard says a correction did it."""
    led = tmp_path / "l.jsonl"
    clock = Clock(T0 + 5 * HOUR)
    commit(led, [fc("q1", "a", 1, p=0.9), fc("q2", "a", 2, p=0.9)], clock)
    clock.now = T0 + 11 * HOUR
    rows = [{"question_id": q, "outcome": 1, "resolved_at": T0 + 10 * HOUR} for q in ("q1", "q2")]
    resolve(led, rows, clock)
    assert score(_spec(led)).counts.by_status == {"scored": 2}
    resolve(led, [{**rows[0], "info_cutoff": T0 + 2 * HOUR}], clock, allow_correction=True)
    card = score(_spec(led))
    assert card.counts.by_status == {"after_cutoff": 1, "scored": 1}
    assert _fs(card, "a").selection.cutoff_tightened == 1


def test_validity_statuses_and_not_scored(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(
        led,
        [
            fc("bin", "a", 1, probs={"x": 0.5, "y": 0.5}),
            fc("cat", "a", 2, probs={"x": 0.5, "z": 0.5}),
            fc("void", "a", 3, p=0.5),
            fc("open", "a", 4, p=0.5),
        ],
        clock,
    )
    clock.now += HOUR
    resolve(
        led,
        [
            {"question_id": "bin", "outcome": 1, "resolved_at": T0 + 5 * HOUR},
            {"question_id": "cat", "outcome": "x", "outcomes": ["x", "y"], "resolved_at": T0 + 5 * HOUR},
            {"question_id": "void", "void": True, "kind": "binary", "resolved_at": T0 + 5 * HOUR},
        ],
        clock,
    )
    with pytest.raises(QtError) as ei:
        score(_spec(led))
    e = ei.value
    assert e.code == "QT-INPUT-002" and e.message.startswith("NOT SCORED: 0 of 4 forecasts scored")
    assert e.details["by_status"] == {"kind_mismatch": 1, "outcome_mismatch": 1, "void": 1, "unresolved": 1}
    with pytest.raises(QtError, match="1 forecast\\(s\\) on 1 unresolved question"):
        score(_spec(led, unresolved="fail"))


def test_broken_ledger_is_never_scored(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.6)], clock)
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": T0 + HOUR}], clock)
    text = led.read_text().replace('"p":0.6', '"p":0.99')
    led.write_text(text)
    with pytest.raises(QtError) as ei:
        score(_spec(led))
    assert ei.value.code == "QT-INPUT-002" and "integrity" in ei.value.message
    empty = tmp_path / "e.jsonl"
    empty.write_text("")
    with pytest.raises(QtError, match="empty"):
        score(_spec(empty))


def test_spec_errors(tmp_path: Path) -> None:
    with pytest.raises(QtError) as ei:
        score({"ledger": {"path": "x"}, "n_bins": 1})
    assert ei.value.code == "QT-CONFIG-001" and "n_bins" in ei.value.message
    with pytest.raises(QtError) as ei:
        score({"ledger": {"path": str(tmp_path / "missing.jsonl")}})
    assert ei.value.code == "QT-INPUT-001"
    with pytest.raises(QtError, match="baselines") as ei:
        score({"ledger": {"path": "x"}, "baselines": ["market", "market"]})
    assert ei.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError, match="kelly_fraction"):
        score({"ledger": {"path": "x"}, "pnl": {"kelly_fraction": 1.5}})


# ---- policies and baselines -------------------------------------------------------------------------------------


def _two_forecasts(tmp_path: Path) -> Path:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.2)], clock)
    clock.now += HOUR
    commit(led, [fc("q1", "a", 2, p=0.6)], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": T0 + 3 * HOUR}], clock)
    return led


@pytest.mark.parametrize(("policy", "brier"), [("last", 0.16), ("first", 0.64), ("mean", 0.40)])
def test_policies(tmp_path: Path, policy: str, brier: float) -> None:
    card = score(_spec(_two_forecasts(tmp_path), policy=policy))
    s = next(x for x in _fs(card, "a").scores if x.metric == "brier")
    assert s.mean.value == pytest.approx(brier) and s.n == 1
    want = {"scored": 2} if policy == "mean" else {"scored": 1, "superseded": 1}
    assert _status(card) == want


def test_market_baseline_reads_only_quotes_known_at_commit(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.5)], clock)
    clock.now += 2 * HOUR
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": T0 + 3 * HOUR}], clock)
    quotes = tmp_path / "quotes.csv"
    quotes.write_text(
        "question_id,ts,best_bid,best_ask,bid_depth,ask_depth\n"
        f"q1,{T0 - HOUR},0.29,0.31,100,100\n"
        f"q1,{T0 + 1},0.89,0.91,100,100\n"  # one ns after the commit: must not be the baseline
    )
    card = score(_spec(led, market={"path": str(quotes)}, out=str(tmp_path / "o")))
    rows = pq.read_table(tmp_path / "o" / "forecast_scores.parquet").to_pylist()
    assert rows[0]["market_p"] == pytest.approx(0.30)
    assert rows[0]["market_brier"] == pytest.approx(0.49)
    assert rows[0]["uniform_brier"] == pytest.approx(0.25)
    assert rows[0]["climatology_brier"] is None  # no earlier resolved question
    sk = {(c.reference, c.metric): c for c in _fs(card, "a").skill}
    assert sk[("baseline:market", "brier")].mean_diff.value == pytest.approx(0.49 - 0.25)


def test_climatology_uses_only_questions_resolved_before_the_commit(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    # five earlier questions resolve YES before the forecast on q-new is recorded
    for i in range(5):
        commit(led, [fc(f"old{i}", "a", i, p=0.5)], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": f"old{i}", "outcome": 1, "resolved_at": T0 + HOUR} for i in range(5)], clock)
    clock.now += HOUR
    commit(led, [fc("new", "a", 10, p=0.5)], clock)
    # later questions resolving NO after the forecast must not enter its climatology
    commit(led, [fc(f"later{i}", "a", 20 + i, p=0.5) for i in range(5)], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": "new", "outcome": 0, "resolved_at": T0 + 3 * HOUR}], clock)
    resolve(led, [{"question_id": f"later{i}", "outcome": 0, "resolved_at": T0 + 3 * HOUR} for i in range(5)], clock)
    score(_spec(led, out=str(tmp_path / "o")))
    rows = {r["question_id"]: r for r in pq.read_table(tmp_path / "o" / "forecast_scores.parquet").to_pylist()}
    assert rows["new"]["climatology_p"] == pytest.approx(6 / 7)  # Laplace (5 + 1) / (5 + 2)
    assert rows["old0"]["climatology_p"] is None  # nothing resolved before it
    assert rows["later0"]["climatology_p"] == pytest.approx(6 / 7)  # 'new' resolves after later0's commit too
    card = score(_spec(led, climatology_min_prior=6))
    assert all(c.reference != "baseline:climatology" for c in _fs(card, "a").skill)


# ---- known-edge recovery ----------------------------------------------------------------------------------------


def _world_ledger(tmp_path: Path, n: int, edge_sd: float, seed: int, half_spread: float = 0.01) -> tuple[Path, Path]:
    w = binary_world(n, edge_sd, seed)
    led = tmp_path / "w.jsonl"
    clock = Clock()
    rows = [fc(f"q{i}", "sharp", i, p=float(w.p[i])) for i in range(n)]
    rows += [fc(f"q{i}", "market_copy", 10**6 + i, p=float(w.q[i])) for i in range(n)]
    commit(led, rows, clock)
    clock.now += HOUR
    resolve(
        led,
        [{"question_id": f"q{i}", "outcome": int(w.y[i]), "resolved_at": T0 + (2 + i) * HOUR} for i in range(n)],
        clock,
    )
    quotes = tmp_path / "w_quotes.csv"
    lines = ["question_id,ts,best_bid,best_ask,bid_depth,ask_depth"]
    lines += [
        f"q{i},{T0 - HOUR},{float(w.q[i]) - half_spread!r},{float(w.q[i]) + half_spread!r},1000,1000" for i in range(n)
    ]
    quotes.write_text("\n".join(lines) + "\n")
    return led, quotes


def test_known_edge_is_recovered(tmp_path: Path) -> None:
    """Sharp forecaster = the truth, market = q: the Brier mean difference over the market estimates the known
    edge mean((q - p)^2) and its CI covers it; the market's own copy shows zero skill exactly."""
    n = 3000
    led, quotes = _world_ledger(tmp_path, n, 0.6, seed=77)
    card = score(_spec(led, market={"path": str(quotes)}, bootstrap_b=500, pnl={"lot": 0}))
    w = binary_world(n, 0.6, 77)
    expected = float(np.mean((w.q - w.p) ** 2))
    sk = {(c.reference, c.metric): c for c in _fs(card, "sharp").skill}
    mk = sk[("baseline:market", "brier")]
    assert mk.mean_diff.ci_lo <= expected <= mk.mean_diff.ci_hi
    assert mk.mean_diff.value == pytest.approx(expected, rel=0.35)
    assert mk.dm.p_value < 1e-3 and mk.p_holm < 1e-3
    copy = {(c.reference, c.metric): c for c in _fs(card, "market_copy").skill}
    assert copy[("baseline:market", "brier")].mean_diff.value == pytest.approx(0.0, abs=1e-15)
    # the analysis' question count matches the theoretical one for this world
    pw = next(p for p in _fs(card, "sharp").power if p.reference == "baseline:market")
    delta, sd = brier_edge_moments(w.q, w.p)
    assert pw.observed_edge == pytest.approx(delta, rel=0.35)
    assert pw.sd_diff == pytest.approx(sd, rel=0.1)
    assert pw.achieved_power > 0.95
    # the pairwise comparison agrees in sign and the sharp forecaster makes money after the spread
    pair = next(c for c in card.comparisons if c.metric == "brier")
    assert (pair.subject, pair.reference) == ("market_copy", "sharp")
    assert pair.mean_diff.ci_hi < 0
    pnl = _fs(card, "sharp").pnl
    assert pnl is not None and pnl.isolated_growth.ci_lo > 0
    assert pnl.frictionless_growth > pnl.isolated_growth.value
    assert _fs(card, "market_copy").pnl.n_bets == 0  # never an edge inside its own spread


def _null_ledger(tmp_path: Path, seed: int, g: int, k: int) -> Path:
    """Two exchangeable forecasters (each the truth plus independent log-odds noise) on g groups of k questions
    that share one outcome, and a calibrated forecaster `c` who reports the truth."""
    from scipy.special import expit, logit

    rng = np.random.default_rng(seed)
    led = tmp_path / f"null{seed}.jsonl"
    rows, res, i = [], [], 0
    for gi in range(g):
        pg = float(rng.uniform(0.1, 0.9))
        y = int(rng.random() < pg)
        for ki in range(k):
            qid = f"g{gi}-{ki}"
            a, b = (float(expit(logit(pg) + 0.5 * rng.standard_normal())) for _ in range(2))
            rows += [fc(qid, "a", i, p=round(a, 6)), fc(qid, "b", i + 1, p=round(b, 6)), fc(qid, "c", i + 2, p=pg)]
            i += 3
            res.append(
                {"question_id": qid, "outcome": y, "group": f"g{gi}", "resolved_at": T0 + (10 + ki * g + gi) * HOUR}
            )
    clock = Clock()
    commit(led, rows, clock)
    clock.now += 5 * HOUR
    resolve(led, res, clock)
    return led


@pytest.mark.parametrize(("g", "k"), [(12, 8), (96, 1)])
def test_scorecard_tests_keep_their_size_under_the_null(tmp_path: Path, g: int, k: int) -> None:
    """No information separates a from b, and c is calibrated: over 30 worlds the scorecard's DM, Holm and
    bootstrap tests of a vs b, and the calibration test of c, reject at about their level, also when 8 questions
    share one outcome (`--cluster group`), where row-level tests rejected in 17% to 50% of runs."""
    sims = 30
    rej = np.zeros(4)
    for seed in range(sims):
        led = _null_ledger(tmp_path, seed, g, k)
        card = score(_spec(led, bootstrap_b=100, cluster="group", pnl=None, baselines=["uniform"], seed=seed))
        pair = next(c for c in card.comparisons if c.metric == "brier")
        cal = _fs(card, "c").calibration[0]
        if k > 1:
            assert pair.dm.units == g and pair.n_clusters == g
        rej += np.array([pair.dm.p_value, pair.p_holm, pair.p_bootstrap, cal.ece_p_value]) < 0.05
    assert rej[0] <= 3 and rej[1] <= 3 and rej[3] <= 3, rej
    assert rej[2] <= 5, rej  # the percentile bootstrap is somewhat liberal with 12 clusters (the warning says so)


def test_no_edge_is_not_flagged(tmp_path: Path) -> None:
    """edge_sd = 0: the market is the truth; a forecaster reporting the truth has no detectable edge."""
    led, quotes = _world_ledger(tmp_path, 800, 0.0, seed=5)
    card = score(_spec(led, market={"path": str(quotes)}, bootstrap_b=300))
    mk = next(c for c in _fs(card, "sharp").skill if c.reference == "baseline:market" and c.metric == "brier")
    assert mk.mean_diff.value == pytest.approx(0.0, abs=1e-15)


# ---- other kinds, outputs, determinism --------------------------------------------------------------------------


def test_categorical_and_continuous(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(
        led,
        [
            fc("cat", "a", 1, probs={"lo": 0.2, "mid": 0.5, "hi": 0.3}),
            fc("num", "a", 2, mean=10.0, sd=2.0),
            fc("num2", "a", 3, quantiles={"0.5": 4.0}),
            fc("num3", "a", 4, samples=[0.0, 1.0]),
        ],
        clock,
    )
    clock.now += HOUR
    resolve(
        led,
        [
            {
                "question_id": "cat",
                "outcome": "mid",
                "outcomes": ["lo", "mid", "hi"],
                "ordered": True,
                "resolved_at": T0 + 2 * HOUR,
            },
            {"question_id": "num", "outcome": 10.0, "kind": "continuous", "resolved_at": T0 + 2 * HOUR},
            {"question_id": "num2", "outcome": 7.0, "kind": "continuous", "resolved_at": T0 + 2 * HOUR},
            {"question_id": "num3", "outcome": 0.0, "kind": "continuous", "resolved_at": T0 + 2 * HOUR},
        ],
        clock,
    )
    card = score(_spec(led))
    f = _fs(card, "a")
    assert f.kinds == {"categorical": 1, "continuous": 3}
    s = {(x.kind, x.metric): x.mean.value for x in f.scores}
    assert s[("categorical", "brier")] == pytest.approx(0.04 + 0.25 + 0.09)
    assert s[("categorical", "rps")] == pytest.approx((0.04 + 0.09) / 2)
    crps_n = 2.0 * (2 * 0.3989422804014327 - 1 / np.sqrt(np.pi))
    assert s[("continuous", "crps")] == pytest.approx((crps_n + 3.0 + 0.25) / 3)
    assert [c.target for c in f.calibration] == ["top_label"]
    assert f.calibration[0].n == 1
    unif = {c.metric: c for c in f.skill if c.reference == "baseline:uniform"}
    assert unif["brier"].mean_diff.value == pytest.approx(2 * (1 / 3) ** 2 + (2 / 3) ** 2 - 0.38)


def test_outputs_validate_and_are_deterministic(tmp_path: Path) -> None:
    led, quotes = _world_ledger(tmp_path, 300, 0.6, seed=9)
    a = score(_spec(led, market={"path": str(quotes)}, out=str(tmp_path / "a")))
    b = score(_spec(led, market={"path": str(quotes)}, out=str(tmp_path / "b")))
    assert a.run_id == b.run_id and a.spec == b.spec and "out" not in a.spec
    ja = (tmp_path / "a" / "forecast_scorecard.json").read_text()
    jb = (tmp_path / "b" / "forecast_scorecard.json").read_text()
    assert ja == jb
    doc = json.loads(ja)
    assert type(parse_document(doc)) is ForecastScorecard
    names = sorted(f.name for f in a.files)
    assert names == ["forecast_bets.parquet", "forecast_eligibility.parquet", "forecast_scores.parquet"]
    for fe in a.files:
        data = (tmp_path / "a" / fe.name).read_bytes()
        assert (
            hashlib.sha256(data).hexdigest()
            == fe.sha256
            == hashlib.sha256((tmp_path / "b" / fe.name).read_bytes()).hexdigest()
        )
    for name, schema in (
        ("forecast_eligibility.parquet", ELIGIBILITY),
        ("forecast_scores.parquet", SCORES),
        ("forecast_bets.parquet", BETS),
    ):
        t = pq.read_table(tmp_path / "a" / name)
        assert t.schema.equals(schema, check_metadata=True), name
    # another seed changes the CIs, not the run identity rule
    c = score(_spec(led, market={"path": str(quotes)}, seed=1))
    assert c.run_id != a.run_id
    # a ledger that grows gets a new run_id
    commit(led, [fc("extra", "sharp", 10**7, p=0.5)], Clock(T0 + 10 * HOUR))
    assert score(_spec(led, market={"path": str(quotes)})).run_id != a.run_id


def test_cluster_by_group(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    rng = np.random.default_rng(0)
    rows, res = [], []
    for g in range(6):
        y = int(rng.random() < 0.5)
        for k in range(5):  # five copies of the same event per group: perfectly correlated
            qid = f"g{g}-{k}"
            rows.append(fc(qid, "a", 10 * g + k, p=0.7))
            res.append({"question_id": qid, "outcome": y, "group": f"g{g}", "resolved_at": T0 + HOUR})
    commit(led, rows, clock)
    clock.now += 2 * HOUR
    resolve(led, res, clock)
    by_q = score(_spec(led, bootstrap_b=500))
    by_g = score(_spec(led, bootstrap_b=500, cluster="group"))
    cq = next(c for c in _fs(by_q, "a").skill if c.reference == "baseline:uniform" and c.metric == "brier")
    cg = next(c for c in _fs(by_g, "a").skill if c.reference == "baseline:uniform" and c.metric == "brier")
    assert (cq.n_clusters, cg.n_clusters) == (30, 6)
    assert cg.mean_diff.value == pytest.approx(cq.mean_diff.value, abs=1e-15)
    assert (cg.mean_diff.ci_hi - cg.mean_diff.ci_lo) > 1.5 * (cq.mean_diff.ci_hi - cq.mean_diff.ci_lo)
    pw = next(p for p in _fs(by_g, "a").power if p.reference == "baseline:uniform")
    assert pw.design_effect > 3


def test_ledger_summary_counts(tmp_path: Path) -> None:
    led = _two_forecasts(tmp_path)
    card = score(_spec(led))
    assert card.ledger.verified and card.ledger.n_entries == 3
    assert card.ledger.ledger_id == lg.verify_ledger(led).entries[0].entry_sha256
    assert (card.counts.questions, card.counts.questions_resolved, card.counts.forecasters) == (1, 1, 1)
    assert _fs(card, "a").median_lead_ns == T0 + 3 * HOUR - (T0 + HOUR)


# ---- anchors, selection through resolutions, market quote age, pairwise -----------------------------------------


def _anchor_world(tmp_path: Path) -> tuple[Path, Path]:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc(f"q{i}", "a", i, p=0.7) for i in range(3)], clock)
    c = lg.commitment(lg.verify_ledger(led).entries)
    cpath = tmp_path / "c.json"
    cpath.write_text(c.model_dump_json())
    clock.now = T0 + HOUR
    commit(led, [fc("q3", "a", 3, p=0.7)], clock)  # after the anchored head
    clock.now = T0 + 11 * HOUR
    resolve(led, [{"question_id": f"q{i}", "outcome": 1, "resolved_at": T0 + 10 * HOUR} for i in range(4)], clock)
    return led, cpath


def test_anchored_commitments_bound_timing_truth(tmp_path: Path) -> None:
    led, cpath = _anchor_world(tmp_path)
    anchor = {"commitment": {"path": str(cpath)}, "anchored_at_ns": T0 + 2 * HOUR}
    card = score(_spec(led, anchors=[anchor], require_anchor=True))
    assert card.counts.by_status == {"scored": 3, "unanchored": 1}
    assert [r.status for r in card.rejections] == ["unanchored"]
    (echo,) = card.ledger.anchors
    c = lg.commitment(lg.verify_ledger(led).entries[:3])
    assert (echo.head_seq, echo.head_sha256, echo.commitment_sha256) == (2, c.head_sha256, lg.commitment_digest(c))
    assert _fs(card, "a").n_anchored == 3
    # attested only at the cutoff: it proves nothing about timing, so nothing counts
    late = {**anchor, "anchored_at_ns": T0 + 10 * HOUR}
    with pytest.raises(QtError, match="NOT SCORED") as ei:
        score(_spec(led, anchors=[late], require_anchor=True))
    assert ei.value.details["by_status"] == {"anchored_after_cutoff": 3, "unanchored": 1}
    # without require_anchor the anchors are checked and echoed, and the writer's clock decides as before
    free = score(_spec(led, anchors=[anchor]))
    assert free.counts.by_status == {"scored": 4} and _fs(free, "a").n_anchored == 3
    with pytest.raises(QtError) as ei:
        score(_spec(led, require_anchor=True))
    assert ei.value.code == "QT-CONFIG-001"


def test_a_rewritten_ledger_fails_its_anchor(tmp_path: Path) -> None:
    """Re-chaining the whole file passes the local check, but scoring against the anchored commitment refuses it."""
    led, cpath = _anchor_world(tmp_path)
    docs = [json.loads(x) for x in led.read_text().splitlines()]
    docs[0]["payload"]["forecast"]["p"] = 0.99
    prev = lg.GENESIS
    for d in docs:
        d["prev_sha256"] = prev
        d["payload_sha256"] = lg.payload_digest(d["payload"])
        d["entry_sha256"] = lg.entry_digest(d)
        prev = d["entry_sha256"]
    led.write_text("".join(lg.canonical_json(d) + "\n" for d in docs))
    assert lg.verify_ledger(led).ok
    anchor = {"commitment": {"path": str(cpath)}, "anchored_at_ns": T0 + 2 * HOUR}
    with pytest.raises(QtError, match="does not contain this anchored commitment") as ei:
        score(_spec(led, anchors=[anchor]))
    assert ei.value.code == "QT-INPUT-002"


def test_selection_through_resolutions_is_surfaced(tmp_path: Path) -> None:
    """A forecaster who resolves her own questions: 6 right, 1 wrong then corrected to void, 3 left open. The
    scorecard counts the void and the open questions per forecaster, keeps them out of the leakage-guard count,
    warns, and gives no DM p-value for constant loss differences."""
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc(f"q{i}", "alice", i, p=0.9) for i in range(10)], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": f"q{i}", "outcome": 1, "resolved_at": T0 + HOUR} for i in range(6)], clock)
    resolve(led, [{"question_id": "q6", "outcome": 0, "resolved_at": T0 + HOUR}], clock)
    resolve(
        led,
        [{"question_id": "q6", "void": True, "kind": "binary", "resolved_at": T0 + HOUR}],
        clock,
        allow_correction=True,
    )
    card = score(_spec(led))
    f = _fs(card, "alice")
    assert (f.n_scored, f.n_rejected) == (6, 0)
    sel = f.selection
    assert (sel.unresolved, sel.void, sel.voided_after_outcome, sel.outcome_corrected) == (3, 1, 1, 0)
    assert card.rejections == [] and not any("leakage" in w for w in card.warnings)
    (w,) = [w for w in card.warnings if w.startswith("alice: 3 forecast")]
    assert "voided by a correction after an outcome" in w and "trusted input" in w
    uni = next(c for c in f.skill if c.reference == "baseline:uniform" and c.metric == "brier")
    assert uni.dm.p_value is None and uni.p_holm is None
    assert any("no DM p-value" in w for w in card.warnings)
    assert any("below 30 the tests and CIs are unreliable" in w for w in card.warnings)
    with pytest.raises(QtError, match="unresolved question"):
        score(_spec(led, unresolved="fail"))


def test_market_baseline_quote_age(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc("q1", "a", 1, p=0.6)], clock)
    clock.now += 2 * HOUR
    resolve(led, [{"question_id": "q1", "outcome": 1, "resolved_at": T0 + 3 * HOUR}], clock)
    quotes = tmp_path / "quotes.csv"
    quotes.write_text(f"question_id,ts,best_bid,best_ask,bid_depth,ask_depth\nq1,{T0 - 10 * HOUR},0.29,0.31,9,9\n")
    card = score(_spec(led, market={"path": str(quotes)}, out=str(tmp_path / "o"), pnl=None))
    (row,) = pq.read_table(tmp_path / "o" / "forecast_scores.parquet").to_pylist()
    assert row["market_p"] == pytest.approx(0.30) and row["market_quote_age_ns"] == 10 * HOUR
    assert row["market_quote_ts"].timestamp() == pytest.approx((T0 - 10 * HOUR) / 1e9)
    assert any("market baseline's quotes are old (median age 10.0 h" in w for w in card.warnings)
    capped = score(_spec(led, market={"path": str(quotes)}, out=str(tmp_path / "o2"), market_max_age_ns=HOUR))
    (row,) = pq.read_table(tmp_path / "o2" / "forecast_scores.parquet").to_pylist()
    assert row["market_p"] is None and row["market_quote_ts"] is None
    assert all(c.reference != "baseline:market" for c in _fs(capped, "a").skill)
    # the PnL's quote-age limit applies to the baseline too unless the baseline has its own
    via_pnl = score(_spec(led, market={"path": str(quotes)}, pnl={"max_quote_age_ns": HOUR}))
    assert all(c.reference != "baseline:market" for c in _fs(via_pnl, "a").skill)


def test_pairwise_options_and_lead_difference(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    clock = Clock()
    commit(led, [fc(f"q{i}", "early", i, p=0.6) for i in range(6)], clock)
    commit(led, [fc(f"q{i}", "few", 100 + i, p=0.6) for i in range(2)], clock)
    clock.now += HOUR
    commit(led, [fc(f"q{i}", "late", 200 + i, p=0.7) for i in range(6)], clock)
    clock.now += HOUR
    resolve(led, [{"question_id": f"q{i}", "outcome": 1, "resolved_at": T0 + 5 * HOUR} for i in range(6)], clock)
    every = score(_spec(led))
    assert {(c.subject, c.reference) for c in every.comparisons} == {
        ("early", "few"),
        ("early", "late"),
        ("few", "late"),
    }
    el = next(c for c in every.comparisons if (c.subject, c.reference) == ("early", "late") and c.metric == "brier")
    assert el.lead_diff_ns == HOUR  # early committed an hour sooner, with less information
    assert all(c.lead_diff_ns is None for f in every.forecasters for c in f.skill)
    assert score(_spec(led, pairwise="none")).comparisons == []
    top = score(_spec(led, pairwise=2))
    assert {(c.subject, c.reference) for c in top.comparisons} == {("early", "late")}


# ---- the worked example of docs/how-to/score-forecasts.md -----------------------------------------------------------


def test_worked_example_numbers(tmp_path: Path) -> None:
    """Rebuilds the how-to's example (400 questions, seed 0, fees 0.07 / 0.01, 1,000 resamples) at a fixed start
    and pins every number the page quotes."""
    from queuetruth.forecast.io import read_records
    from queuetruth.forecast.simulate import write_example

    paths = write_example(tmp_path / "ex", start_ns=T0, seed=0)
    led = tmp_path / "ledger.jsonl"

    def rows(name: str) -> list[dict]:
        return read_records(paths[name])

    clock = Clock(T0 + SEC)
    commit(led, rows("forecasts"), clock)
    clock.now = T0 + 25 * HOUR
    resolve(led, rows("resolutions"), clock)
    commit(led, rows("late"), clock)
    card = score(
        {
            "ledger": {"path": str(led)},
            "market": {"path": str(paths["quotes"])},
            "pnl": {"fees": {"quadratic": 0.07, "round_up_to": 0.01}},
        }
    )
    assert card.counts.by_status == {"after_resolution_entry": 400, "scored": 800}

    def r(x: float | None, nd: int) -> float | None:
        return None if x is None else round(x, nd)

    def ci(v, nd: int) -> tuple:  # type: ignore[no-untyped-def]
        return (r(v.value, nd), r(v.ci_lo, nd), r(v.ci_hi, nd))

    got = {}
    for who in ("crowd", "sharp"):
        f = _fs(card, who)
        brier = next(s for s in f.scores if s.metric == "brier")
        sk = {c.reference: c for c in f.skill if c.metric == "brier"}
        cal = f.calibration[0]
        pw = next(p for p in f.power if p.reference == "baseline:market")
        p = f.pnl
        got[who] = {
            "brier": ci(brier.mean, 4),
            "market": (
                ci(sk["baseline:market"].skill, 3),
                r(sk["baseline:market"].dm.p_value, 4),
                r(sk["baseline:market"].p_holm, 3),
            ),
            "uniform": ci(sk["baseline:uniform"].skill, 3),
            "ece": (ci(cal.ece, 4), r(cal.ece_if_calibrated, 4), r(cal.ece_p_value, 3)),
            "power": (r(pw.observed_edge, 5), pw.n_required_observed),
            "isolated": ci(p.isolated_growth, 5),
            "frictionless": r(p.frictionless_growth, 5),
            "path": (p.n_bets, round(p.total_return, 4), r(p.growth_per_bet, 5), p.max_drawdown, p.path_cash_limited),
            "costs": (round(p.fees_paid, 2), round(p.spread_paid, 2)),
            "skipped": p.n_skipped,
        }
    assert got["crowd"] == {
        "brier": (0.2003, 0.1814, 0.2168),
        "market": ((-0.028, -0.056, 0.002), 0.0978, 0.126),
        "uniform": (0.199, 0.129, 0.266),
        "ece": ((0.0769, 0.0285, 0.1005), 0.0524, 0.042),
        "power": (-0.00543, None),
        "isolated": (-0.00182, -0.00398, 0.00044),
        "frictionless": -0.01479,
        "path": (39, 0.2201, 0.0051, 0.0, True),
        "costs": (29.42, 20.12),
        "skipped": {"insufficient_cash": 233, "no_edge": 121, "size_zero": 6, "fees_exceed_edge": 1},
    }
    assert got["sharp"] == {
        "brier": (0.1849, 0.1695, 0.2035),
        "market": ((0.051, -0.004, 0.1), 0.0631, 0.126),
        "uniform": (0.26, 0.191, 0.331),
        "ece": ((0.0459, 0.0, 0.052), 0.0525, 0.663),
        "power": (0.00991, 937),
        "isolated": (0.00707, 0.00198, 0.0126),
        "frictionless": 0.0212,
        "path": (20, 0.228, 0.01027, 0.0, True),
        "costs": (27.68, 19.23),
        "skipped": {"insufficient_cash": 307, "no_edge": 70, "size_zero": 2, "fees_exceed_edge": 1},
    }
    (pair,) = [c for c in card.comparisons if c.metric == "brier"]
    assert (pair.subject, pair.reference, ci(pair.mean_diff, 4), r(pair.dm.p_value, 3), r(pair.p_holm, 4)) == (
        "crowd",
        "sharp",
        (-0.0153, -0.0279, -0.0027),
        0.023,
        0.0689,
    )
    # frictionless growth equals the log-score mean_diff against the market mid (zero latency, same questions)
    for who in ("crowd", "sharp"):
        f = _fs(card, who)
        log_mk = next(c for c in f.skill if c.reference == "baseline:market" and c.metric == "log")
        assert f.pnl.frictionless_growth == pytest.approx(log_mk.mean_diff.value, rel=1e-9)
