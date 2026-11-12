"""Short captures (audit 2026-11-12, first hour): intervals and significance on fewer than 5 bootstrap blocks.

On a capture that fits in one 30-minute bucket, the zoo's block bootstrap had one block: every replicate equalled the
estimate, so every 95% CI had zero width and every model looked significant (`+0.4 pp (95% CI +0.4 to +0.4 pp)`,
all cells starred, `BIASED`). `queuetruth.stats.ci_blocks` now falls back to instrument x minute clusters and, with
fewer than 5 blocks of any kind, reports no interval at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from queuetruth import cli
from queuetruth.eval import evaluate_zoo, planned_life_table, zoo_ci_blocks
from queuetruth.eval.leaderboard import (
    DEGENERATE_CI_NOTE,
    annotate_mcs,
    leaderboard_mcs,
    without_degenerate_cis,
)
from queuetruth.schemas.models import ZooSummary
from queuetruth.stats import MIN_CI_BLOCKS, block_labels, ci_blocks

S = 1_000_000_000
MIN = 60 * S
DAY0 = 20_460
OPEN = DAY0 * 86_400 * S + 34_200 * S  # 09:30 local as UTC (synthetic)
H1, H10 = S, 10 * S

Builder = Callable[[list[dict[str, Any]]], pa.Table]


def _short_zoo(predictions: Builder, *, minutes: int, instruments: int, n: int = 600, seed: int = 3) -> tuple:
    """`n` shadows placed over `minutes` minutes after 09:35 on `instruments` instruments (all in one 30-minute
    bucket); exact first fill ~ Exp(4s); `optimist` fills at entry, `close` near the exact time, `pessimist` never."""
    rng = np.random.default_rng(seed)
    t_fill = rng.exponential(4.0, n) * S
    err = np.exp(rng.normal(0.0, 0.3, n))
    live = [int(OPEN + 5 * MIN + rng.integers(0, minutes * MIN)) for _ in range(n)]
    inst = [1 + i % instruments for i in range(n)]
    ids = [f"s{i:04d}" for i in range(n)]
    models: dict[str, Callable[[int], float | None]] = {
        "optimist": lambda i: 0.0,
        "close": lambda i: t_fill[i] * err[i],
        "pessimist": lambda i: None,
    }
    rows = []
    for m, rel in models.items():
        for i, sid in enumerate(ids):
            for h in (H1, H10):
                mt = rel(i)
                rows.append(
                    {
                        "model_id": m,
                        "shadow_id": sid,
                        "horizon_ns": h,
                        "p_fill": 1.0 if mt is not None and mt <= h else 0.0,
                        "model_ts_first_fill": None if mt is None else int(live[i] + mt),
                        "truth_filled": bool(t_fill[i] <= h),
                        "truth_ts_first_fill": int(live[i] + t_fill[i]),
                        "instrument_id": inst[i],
                        "ts_live": live[i],
                    }
                )
    table = predictions(rows)
    life = planned_life_table(ids, [None] * n, [None] * n)
    return table, life


def _cis(board: list[dict[str, Any]]) -> list[tuple[float | None, float | None]]:
    return [(cv["ci_lo"], cv["ci_hi"]) for e in board for cv in [*e["fill_rate_bias"].values(), e["overfill_bias"]]]


# ---- the block chooser ---------------------------------------------------------------------------------------------


def test_ci_blocks_auto_keeps_long_runs_and_falls_back_on_short_ones() -> None:
    # a long day: 13 half-hours, the labels block_labels gave before (dense, in the same order)
    ts = OPEN + np.arange(0, 390 * MIN, 7 * S)
    dates = np.full(ts.size, DAY0)
    inst = np.arange(ts.size) % 3
    cb = ci_blocks(dates, ts, inst)
    raw, kind = block_labels(dates, ts)
    assert (cb.kind, cb.n, cb.available) == (kind, 13, True) == ("30min", 13, True)
    assert np.array_equal(cb.labels, np.unique(raw, return_inverse=True)[1])
    # 15 minutes on 4 instruments: one half-hour, 60 instrument x minute clusters
    ts = OPEN + 5 * MIN + np.arange(0, 15 * MIN, S)
    inst = np.arange(ts.size) % 4
    cb = ci_blocks(np.full(ts.size, DAY0), ts, inst)
    assert (cb.kind, cb.n, cb.available) == ("instrument_minute", 60, True)
    # 3 minutes on 1 instrument: 3 clusters, no interval
    ts = OPEN + 5 * MIN + np.arange(0, 3 * MIN, S)
    cb = ci_blocks(np.full(ts.size, DAY0), ts, np.zeros(ts.size))
    assert (cb.n, cb.available) == (3, False) and "no interval" in cb.describe()
    assert MIN_CI_BLOCKS == 5


def test_ci_blocks_forced_kinds() -> None:
    ts = OPEN + 5 * MIN + np.arange(0, 15 * MIN, S)
    inst = np.arange(ts.size) % 2
    dates = np.full(ts.size, DAY0)
    assert ci_blocks(dates, ts, inst, "30min").n == 1
    assert ci_blocks(dates, ts, inst, "1min").n == 15
    assert ci_blocks(dates, ts, inst, "instrument_minute").n == 30
    assert ci_blocks(dates, ts, inst, "day").n == 1
    with pytest.raises(ValueError, match="block must be one of"):
        ci_blocks(dates, ts, inst, "hour")


# ---- the zoo, its MCS and the CLI -----------------------------------------------------------------------------------


def test_short_capture_gets_instrument_minute_intervals_not_zero_width_ones(predictions: Builder) -> None:
    table, life = _short_zoo(predictions, minutes=5, instruments=4)
    # before the fix (blocks = 30-minute buckets): one block, every interval of zero width, everything "significant"
    _, old = evaluate_zoo(table, h_max_ns=H10, bootstrap_b=200, planned_life=life, block="30min")
    assert all(lo is None and hi is None for lo, hi in _cis(old))
    # now: instrument x minute clusters, intervals of positive width
    errors, board = evaluate_zoo(table, h_max_ns=H10, bootstrap_b=200, planned_life=life)
    cb = zoo_ci_blocks(table)
    assert (cb.kind, cb.n) == ("instrument_minute", 20)
    cis = _cis(board)
    assert cis and all(lo is not None and hi is not None and hi > lo for lo, hi in cis)
    by = {e["model_id"]: e["fill_rate_bias"][str(H1)] for e in board}
    assert by["optimist"]["ci_lo"] > 0 and by["pessimist"]["ci_hi"] < 0  # real biases stay significant
    assert by["close"]["ci_lo"] < by["close"]["value"] < by["close"]["ci_hi"]
    metrics = set(errors.column("metric").to_pylist())
    assert {"discordance_p_primary", "discordance_p_bootstrap"} <= metrics
    # and the model confidence set runs on the same clusters (one note at most, none here)
    res, notes = leaderboard_mcs(table, life, h_max_ns=H10, bootstrap_b=100)
    assert notes == [] and res["close"]["mcs_member"][str(H10)] is True


def test_too_few_clusters_give_no_interval_and_no_significance(predictions: Builder) -> None:
    table, life = _short_zoo(predictions, minutes=2, instruments=1, n=200)
    assert zoo_ci_blocks(table).n < MIN_CI_BLOCKS
    errors, board = evaluate_zoo(table, h_max_ns=H10, bootstrap_b=200, planned_life=life)
    assert all(lo is None and hi is None for lo, hi in _cis(board))
    rows = errors.to_pylist()
    assert all(r["ci_lo"] is None and r["ci_hi"] is None for r in rows)
    metrics = {r["metric"] for r in rows}
    assert not metrics & {"discordance_p_primary", "discordance_p_bootstrap", "discordance_p_obuchowski"}
    assert {"mcnemar_p_exact", "n_clusters", "fill_rate_bias_aj"} <= metrics
    # the MCS: one note for every horizon, not one per horizon
    got, notes = annotate_mcs(board, table, life, h_max_ns=H10, bootstrap_b=50)
    assert len(notes) == 1 and notes[0].startswith("mcs: at 1s, 10s, ")
    # the CLI: no star, and the legend says why
    doc = {
        "n_shadows": 200,
        "models": ["optimist", "close", "pessimist"],
        "run_id": "x",
        "leaderboard": got,
        "ci_block": "instrument_minute",
        "n_ci_blocks": zoo_ci_blocks(table).n,
        "spec": {"h_max_ns": H10},
    }
    text = cli.render(["zoo"], doc, args={}, extra={"sort_horizon_ns": H10})
    lines = text.split("\n")
    start = next(i for i, x in enumerate(lines) if x.startswith("(no 95% CIs"))
    table_lines = lines[start + 1 : lines.index("", start)]
    assert len(table_lines) >= 4 and not any("*" in x for x in table_lines), text
    assert "no 95% CIs and no * marks" in text and "95% CI" not in text.split("in plain words:")[1]


def test_old_zero_width_summaries_lose_their_intervals_in_the_report() -> None:
    def cv(v: float, lo: float | None, hi: float | None) -> dict[str, Any]:
        return {"value": v, "ci_lo": lo, "ci_hi": hi}

    def entry(mid: str, v: float, lo: float | None, hi: float | None) -> dict[str, Any]:
        return {
            "model_id": mid,
            "brier": {},
            "logloss": {},
            "fill_rate_bias": {str(H1): cv(v, lo, hi)},
            "overfill_bias": cv(v, lo, hi),
            "qty_bias": None,
            "ttf_iad": None,
            "mcnemar": {"b": 0, "c": 0, "p_exact": None},
        }

    base = {
        "schema": "qt.zoo_summary",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "spec": {},
        "n_shadows": 10,
        "models": ["a", "b"],
        "files": [],
        "warnings": [],
    }
    old = ZooSummary.model_validate(
        {**base, "leaderboard": [entry("a", 0.004, 0.004, 0.004), entry("b", -0.1, -0.1, -0.1)]}
    )
    fixed = without_degenerate_cis(old)
    assert all(cv.ci_lo is None and cv.ci_hi is None for e in fixed.leaderboard for cv in e.fill_rate_bias.values())
    assert fixed.warnings == [DEGENERATE_CI_NOTE]
    # a run with real intervals keeps them, even when one of them is [0, 0]
    ok = ZooSummary.model_validate({**base, "leaderboard": [entry("a", 0.0, 0.0, 0.0), entry("b", -0.1, -0.2, -0.05)]})
    assert without_degenerate_cis(ok) == ok
    # a new run says how many blocks it had
    few = ZooSummary.model_validate(
        {**base, "leaderboard": [entry("a", 0.1, 0.05, 0.2)], "ci_block": "instrument_minute", "n_ci_blocks": 3}
    )
    assert without_degenerate_cis(few).leaderboard[0].fill_rate_bias[str(H1)].ci_lo is None


# ---- stderr: diagnostics behind --verbose ----------------------------------------------------------------------------


def test_diagnostics_collapse_into_one_note() -> None:
    ws = [
        "mcs: at 1s, 10s, 1 bootstrap block(s) (30min) < 5: no model confidence set (MCS fields null)",
        "impact: 924 of 2909 shadows (31.8%) may not be independent of the book",
        "clusters: 26 (30min blocks), with discordance 15, design effect 0.8841",
        "race: 0 of 200 order(s) race-flagged",
        "self_exclusion: 199 leg(s) activated in their twin's queue place",
        "data end: 12 of 300 never-cancel shadows end with the data before 5min",
    ]
    lines = cli.stderr_lines({"warnings": ws}, verbose=False)
    assert lines[0] == "warning: data end: 12 of 300 never-cancel shadows end with the data before 5min"
    assert len(lines) == 2 and lines[1].startswith("note: 5 diagnostic(s) (model confidence set, shadow impact")
    assert "--verbose" in lines[1]
    assert cli.stderr_lines({"warnings": ws}, verbose=True) == [f"warning: {w}" for w in ws]
    assert cli.stderr_lines({"warnings": []}, verbose=False) == []
