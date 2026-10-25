"""Model confidence set on the zoo leaderboard (`queuetruth.eval.leaderboard`: `horizon_losses`, `leaderboard_mcs`,
`annotate_mcs`, `mcs_mark`, `mcs_table`, `mcs_sentence`): the loss rows are exactly the leaderboard rows, the losses
are the per-shadow first-fill error and Brier, the set is deterministic, order-level models are left out, horizons
with fewer than `MCS_MIN_BLOCKS` bootstrap blocks get null fields and a warning, the wild cluster bootstrap-t keeps
the whole set of equal models about 90% of the time at the real day's 13 blocks, and the CLI shows the set."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from queuetruth import __version__ as qt_version
from queuetruth import cli
from queuetruth.errors import QtError
from queuetruth.eval import evaluate_zoo, planned_life_table
from queuetruth.eval.leaderboard import (
    MCS_ALPHA,
    MCS_FIELDS,
    MCS_MARK,
    MCS_MIN_BLOCKS,
    MCS_STUDENTIZE,
    ORDER_LEVEL_FAMILIES,
    annotate_mcs,
    has_mcs,
    horizon_losses,
    leaderboard_mcs,
    mcs_mark,
    mcs_sentence,
    mcs_table,
)
from queuetruth.schemas import runid, specs
from queuetruth.schemas.models import LeaderboardEntry, ZooSummary
from queuetruth.stats.selection import model_confidence_set

S = 1_000_000_000
DAY = 86_400 * S
DAY0 = 20_460
H1, H10 = S, 10 * S
HMAX = H10

Builder = Callable[[list[dict[str, Any]]], pa.Table]


def _zoo(predictions: Builder, *, n: int = 240, seed: int = 7) -> tuple[pa.Table, pa.Table, dict[str, Any]]:
    """8 session dates (day blocks); exact first fill ~ Exp(4 s); models:
    - `close[a=1]`: the exact time times a lognormal error (sd 0.2); `close[a=2]`: an independent error of the same
      size (statistically tied); `close_copy`: the same forecasts as `close[a=1]` (identical losses);
    - `instant`: fills at entry (front-of-queue-like); `never`: never fills;
    - `hbt_l3_fifo`: the exact time (order-level reference, excluded from the MCS by default).
    Special shadows: `cxl` has a planned cancel at 5 s (< h_max: never in the leaderboard), `adm` an administrative
    end at 5 s (in at 1 s, out at 10 s), `nolab` no exact arm, `abort` a faithful hbt #312 abort (left out for every
    model)."""
    rng = np.random.default_rng(seed)
    t_fill = rng.exponential(4.0, n) * S
    err1 = np.exp(rng.normal(0.0, 0.2, n))
    err2 = np.exp(rng.normal(0.0, 0.2, n))
    ids = [f"s{i:03d}" for i in range(n)]
    ids[0], ids[1], ids[2], ids[3] = "cxl", "adm", "nolab", "abort"
    live = [DAY0 * DAY + (i % 8) * DAY + 36_000 * S + (i // 8) * S for i in range(n)]
    models: dict[str, Callable[[int], float | None]] = {
        "close[a=1]": lambda i: t_fill[i] * err1[i],
        "close[a=2]": lambda i: t_fill[i] * err2[i],
        "close_copy": lambda i: t_fill[i] * err1[i],
        "instant": lambda i: 0.0,
        "never": lambda i: None,
        "hbt_l3_fifo": lambda i: t_fill[i],
    }
    rows = []
    for m, rel in models.items():
        for i, sid in enumerate(ids):
            for h in (H1, H10):
                mt = rel(i)
                y = None if sid == "nolab" else bool(t_fill[i] <= h)
                rows.append(
                    {
                        "model_id": m,
                        "shadow_id": sid,
                        "horizon_ns": h,
                        "p_fill": 1.0 if mt is not None and mt <= h else 0.0,
                        "model_ts_first_fill": None if mt is None else int(live[i] + mt),
                        "truth_filled": y,
                        "truth_ts_first_fill": None if sid == "nolab" else int(live[i] + t_fill[i]),
                        "session_date": DAY0 + (i % 8),
                        "ts_live": live[i],
                        "status": "hbt_312_abort" if (sid == "abort" and m == "close[a=2]" and h == H10) else None,
                    }
                )
    # the 1.1 `status` column (a faithful hbt pair aborted at its #312 event)
    status = pa.array([r["status"] for r in rows], pa.dictionary(pa.int8(), pa.utf8()))
    table = predictions(rows).append_column("status", status)
    life = planned_life_table(
        ids,
        [live[0] + 5 * S] + [None] * (n - 1),
        [None, live[1] + 5 * S] + [None] * (n - 2),
    )
    info = {"t_fill": t_fill, "live": live, "ids": ids, "err1": err1}
    return table, life, info


def test_losses_are_the_leaderboard_rows(predictions: Builder) -> None:
    table, life, info = _zoo(predictions)
    out = horizon_losses(table, life, h_max_ns=HMAX)
    assert [x.horizon_ns for x in out] == [H1, H10]
    h1, h10 = out
    assert h1.model_ids == ("close[a=1]", "close[a=2]", "close_copy", "instant", "never", "hbt_l3_fifo")
    s1, s10 = set(h1.shadow_ids), set(h10.shadow_ids)
    # never-cancel only, an exact arm required, the #312 shadow left out for every model, admin end cuts 10 s only
    assert not {"cxl", "nolab", "abort"} & (s1 | s10)
    assert "adm" in s1 and "adm" not in s10
    assert len(s1) == 240 - 3 and len(s10) == 240 - 4
    assert h1.n_dropped == 0 and h10.n_dropped == 0
    assert list(h1.shadow_ids) == sorted(h1.shadow_ids)
    # hand check of the first-fill error on one shadow: |min(T_model, h) - min(T_exact, h)| / h
    i = info["ids"].index("s010")
    r = list(h10.shadow_ids).index("s010")
    t, e = info["t_fill"][i], info["err1"][i]
    assert h10.first_fill[r, 0] == pytest.approx(abs(min(t * e, H10) - min(t, H10)) / H10)
    assert h10.first_fill[r, 3] == pytest.approx(min(t, H10) / H10)  # instant: fills at entry
    assert h10.first_fill[r, 4] == pytest.approx(1.0 - min(t, H10) / H10)  # never: the horizon
    assert (h10.first_fill[:, 5] == 0).all() and (h10.first_fill >= 0).all() and (h10.first_fill <= 1).all()
    # day blocks (8 session dates)
    assert np.unique(h10.blocks).size == 8


def test_brier_loss_mean_equals_the_leaderboard_brier(predictions: Builder) -> None:
    table, life, _ = _zoo(predictions)
    _, board = evaluate_zoo(table, h_max_ns=HMAX, bootstrap_b=20, seed=0, planned_life=life)
    by = {e["model_id"]: e for e in board}
    for hl in horizon_losses(table, life, h_max_ns=HMAX):
        for j, m in enumerate(hl.model_ids):
            assert float(hl.brier[:, j].mean()) == pytest.approx(by[m]["brier"][str(hl.horizon_ns)], abs=1e-15)


def test_mcs_fields_match_the_stats_function(predictions: Builder) -> None:
    table, life, _ = _zoo(predictions)
    res, notes = leaderboard_mcs(table, life, h_max_ns=HMAX, bootstrap_b=300, seed=5)
    assert notes == [] and MCS_STUDENTIZE == "wild"
    assert set(res) == {"close[a=1]", "close[a=2]", "close_copy", "instant", "never"}  # order-level left out
    assert ORDER_LEVEL_FAMILIES == ("hbt_l3_fifo",)
    hl = horizon_losses(table, life, h_max_ns=HMAX, exclude_families=ORDER_LEVEL_FAMILIES)[1]
    for loss, p_f, m_f in ((hl.first_fill, "mcs_p", "mcs_member"), (hl.brier, "mcs_brier_p", "mcs_brier_member")):
        ref = model_confidence_set(
            loss, alpha=MCS_ALPHA, method="R", b=300, seed=5, test_id="zoo_mcs", blocks=hl.blocks, studentize="wild"
        )
        assert ref.studentize == "wild"
        for j, m in enumerate(hl.model_ids):
            assert res[m][p_f][str(H10)] == ref.p_values[j]
            assert res[m][m_f][str(H10)] is bool(ref.p_values[j] > MCS_ALPHA)
        assert res[hl.model_ids[0]]["first_fill_error"][str(H10)] == pytest.approx(float(hl.first_fill[:, 0].mean()))
    k = str(H10)
    # the two independent close models are tied with the best, the duplicate gets the same p, the bad ones are out
    assert res["close[a=1]"]["mcs_member"][k] and res["close[a=2]"]["mcs_member"][k]
    assert res["close_copy"]["mcs_p"][k] == res["close[a=1]"]["mcs_p"][k]
    assert not res["instant"]["mcs_member"][k] and not res["never"]["mcs_member"][k]
    assert max(res[m]["mcs_p"][k] for m in res) == 1.0
    # deterministic in (seed, bootstrap_b); another seed draws other blocks
    assert leaderboard_mcs(table, life, h_max_ns=HMAX, bootstrap_b=300, seed=5) == (res, [])
    other, _ = leaderboard_mcs(table, life, h_max_ns=HMAX, bootstrap_b=300, seed=6)
    assert any(other[m]["mcs_p"] != res[m]["mcs_p"] for m in res)


def test_including_the_order_level_reference_makes_the_set_trivial(predictions: Builder) -> None:
    table, life, _ = _zoo(predictions)
    res, _ = leaderboard_mcs(table, life, h_max_ns=HMAX, bootstrap_b=200, seed=0, exclude_families=())
    k = str(H10)
    assert res["hbt_l3_fifo"]["mcs_p"][k] == 1.0 and res["hbt_l3_fifo"]["first_fill_error"][k] == 0.0
    assert [m for m in res if res[m]["mcs_member"][k]] == ["hbt_l3_fifo"]


def test_annotate_mcs_adds_additive_fields_without_touching_the_input(predictions: Builder) -> None:
    table, life, _ = _zoo(predictions)
    _, board = evaluate_zoo(table, h_max_ns=HMAX, bootstrap_b=20, seed=0, planned_life=life)
    before = copy.deepcopy(board)
    out, notes = annotate_mcs(board, table, life, h_max_ns=HMAX, bootstrap_b=100, seed=0)
    assert board == before and notes == []
    keys = {str(H1), str(H10)}
    for e in out:
        for f in MCS_FIELDS:
            assert set(e[f]) == keys
        LeaderboardEntry.model_validate(e)  # schema-valid
    by = {e["model_id"]: e for e in out}
    assert all(v is None for f in MCS_FIELDS for v in by["hbt_l3_fifo"][f].values())
    assert by["close[a=1]"]["mcs_member"][str(H10)] is True
    # the unchanged fields stay as evaluate_zoo wrote them
    assert {k: v for k, v in by["instant"].items() if k not in MCS_FIELDS} == next(
        e for e in before if e["model_id"] == "instant"
    )
    # pydantic entries come back as updated copies
    models = [LeaderboardEntry.model_validate(e) for e in board]
    got, _ = annotate_mcs(models, table, life, h_max_ns=HMAX, bootstrap_b=100, seed=0)
    assert [g.model_dump(mode="json") for g in got] == [
        LeaderboardEntry.model_validate(e).model_dump(mode="json") for e in out
    ]
    assert models[0].mcs_member == {}


def test_old_summaries_still_validate_and_new_fields_default_empty() -> None:
    e = LeaderboardEntry.model_validate(
        {
            "model_id": "m",
            "brier": {},
            "logloss": {},
            "fill_rate_bias": {},
            "overfill_bias": {"value": None, "ci_lo": None, "ci_hi": None},
            "qty_bias": None,
            "ttf_iad": None,
            "mcnemar": {"b": 0, "c": 0, "p_exact": None},
        }
    )
    assert (e.first_fill_error, e.mcs_member, e.mcs_p, e.mcs_brier_member, e.mcs_brier_p) == ({}, {}, {}, {}, {})
    assert {"first_fill_error", "mcs_member", "mcs_p", "mcs_brier_member", "mcs_brier_p"} <= set(
        LeaderboardEntry.model_json_schema()["properties"]
    )


def test_unbalanced_panels_and_missing_models(predictions: Builder) -> None:
    table, life, _ = _zoo(predictions, n=80)
    # drop one shadow's 10 s row of one model: the shadow leaves the 10 s panel (counted), 1 s is untouched
    sid = table.column("shadow_id").to_pylist()
    mid = table.column("model_id").to_pylist()
    hz = table.column("horizon_ns").to_pylist()
    keep = [not (s == "s050" and m == "instant" and h == H10) for s, m, h in zip(sid, mid, hz, strict=True)]
    cut = table.filter(pa.array(keep))
    h1, h10 = horizon_losses(cut, life, h_max_ns=HMAX)
    assert h10.n_dropped == 1 and "s050" not in set(h10.shadow_ids) and "s050" in set(h1.shadow_ids)
    # the dropped shadow stays in evaluate_zoo's per-model Brier but leaves the MCS panel: the means then differ
    # for a model whose loss on it is not the panel mean, and a warning says so
    _, board = evaluate_zoo(cut, h_max_ns=HMAX, bootstrap_b=5, seed=0, planned_life=life)
    by = {e["model_id"]: e for e in board}
    j = h10.model_ids.index("never")
    assert float(h10.brier[:, j].mean()) != pytest.approx(by["never"]["brier"][str(H10)], abs=1e-12)
    j1 = h1.model_ids.index("never")
    assert float(h1.brier[:, j1].mean()) == pytest.approx(by["never"]["brier"][str(H1)], abs=1e-15)
    _, notes = leaderboard_mcs(cut, life, h_max_ns=HMAX, bootstrap_b=50)
    assert notes == [
        "mcs: at 10 s, 1 shadow(s) lack a row for some model and are left out of the model confidence set "
        "(its Brier means then differ from brier)"
    ]
    # a model without any 10 s row is not in the 10 s set: its fields are null there
    no10 = table.filter(pa.array([not (m == "never" and h == H10) for m, h in zip(mid, hz, strict=True)]))
    res, _ = leaderboard_mcs(no10, life, h_max_ns=HMAX, bootstrap_b=50)
    assert str(H10) not in res["never"]["mcs_p"] and str(H1) in res["never"]["mcs_p"]
    _, board = evaluate_zoo(no10, h_max_ns=HMAX, bootstrap_b=5, seed=0, planned_life=life)
    got, _ = annotate_mcs(board, no10, life, h_max_ns=HMAX, bootstrap_b=50)
    out = {e["model_id"]: e for e in got}
    assert out["never"]["mcs_member"][str(H10)] is None and out["never"]["mcs_member"][str(H1)] is not None


def test_too_few_bootstrap_blocks_give_null_fields_and_a_warning(predictions: Builder) -> None:
    # keep the first 1, 2, 4 and 5 of the 8 session dates: day blocks with >= 5 dates, else 30-minute buckets of
    # ts_live (here every date's shadows share one half-hour: 1, 2 and 4 blocks); one block has no between-block
    # variance and would read every non-identical model as separated with p = 0
    table, life, _ = _zoo(predictions, n=240)
    dates = np.asarray(table.column("session_date").cast(pa.int32()).to_pylist())
    for n_dates in (1, 2, 4, 5):
        sub = table.filter(pa.array(dates < DAY0 + n_dates))
        hl = horizon_losses(sub, life, h_max_ns=HMAX)
        assert {x.n_blocks for x in hl} == {n_dates}
        assert {x.block_kind for x in hl} == {"day" if n_dates >= 5 else "30min"}
        res, notes = leaderboard_mcs(sub, life, h_max_ns=HMAX, bootstrap_b=100)
        if n_dates < MCS_MIN_BLOCKS:
            assert res == {}
            assert notes == [
                f"mcs: at {w}, {n_dates} bootstrap block(s) (30min) < 5: no model confidence set (MCS fields null)"
                for w in ("1 s", "10 s")
            ]
            _, board = evaluate_zoo(sub, h_max_ns=HMAX, bootstrap_b=5, seed=0, planned_life=life)
            got, _ = annotate_mcs(board, sub, life, h_max_ns=HMAX, bootstrap_b=100)
            assert all(v is None for e in got for f in MCS_FIELDS for v in e[f].values())
            assert not has_mcs(got)
        else:
            assert notes == [] and res["close[a=1]"]["mcs_member"][str(H10)] is True
    assert MCS_MIN_BLOCKS == 5


def test_empty_and_tiny_inputs(predictions: Builder) -> None:
    table, life, _ = _zoo(predictions, n=40)
    assert horizon_losses(table.slice(0, 0), life, h_max_ns=HMAX) == []
    assert leaderboard_mcs(table.slice(0, 0), life, h_max_ns=HMAX) == ({}, [])
    # a horizon above h_max is never scored
    assert [x.horizon_ns for x in horizon_losses(table, life, h_max_ns=H1)] == [H1]
    # one shadow: one block, no MCS
    one = table.filter(pa.array([s == "s020" for s in table.column("shadow_id").to_pylist()]))
    res, notes = leaderboard_mcs(one, life, h_max_ns=HMAX)
    assert res == {} and len(notes) == 2 and all("1 bootstrap block(s)" in n for n in notes)


def test_input_errors(predictions: Builder) -> None:
    table, life, _ = _zoo(predictions, n=20)
    with pytest.raises(QtError) as e:
        horizon_losses(table, None, h_max_ns=HMAX)
    assert e.value.code == "QT-INPUT-001"
    with pytest.raises(QtError) as e:
        horizon_losses(table.drop_columns(["model_ts_first_fill"]), life, h_max_ns=HMAX)
    assert e.value.code == "QT-INPUT-002" and "model_ts_first_fill" in e.value.message
    with pytest.raises(QtError) as e:
        horizon_losses(table, life.slice(1), h_max_ns=HMAX)
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(QtError) as e:
        horizon_losses(pa.concat_tables([table, table.slice(10, 1)]), life, h_max_ns=HMAX)
    assert e.value.code == "QT-INPUT-003" and "duplicate prediction row" in e.value.message


#: Shadows per 30-minute block on the real day one (Nasdaq ITCH 2019-12-30, d1-sampled-corrected, every horizon).
REAL_DAY_BLOCKS = [216, 271, 239, 250, 227, 252, 222, 232, 234, 222, 252, 235, 148]


def test_coverage_at_the_leaderboard_setup() -> None:
    """16 models with equal expected loss on the real day's 13 blocks: the leaderboard MCS (T_R, wild cluster
    bootstrap-t) keeps the whole set about 90% of the time; the fixed bootstrap variance keeps it less than half the
    time (docs/concepts/selection-bias.md records the 1,000-simulation measurement)."""
    from queuetruth.eval.leaderboard import MCS_METHOD, MCS_TEST_ID

    blocks = np.repeat(np.arange(13), REAL_DAY_BLOCKS)
    kept = {"wild": 0, "fixed": 0}
    sims = 100
    for s in range(sims):
        x = np.random.default_rng(70_000 + s).standard_normal((blocks.size, 16))
        for studentize in kept:
            r = model_confidence_set(
                x,
                alpha=MCS_ALPHA,
                method=MCS_METHOD,  # type: ignore[arg-type]
                b=199,
                seed=s,
                test_id=MCS_TEST_ID,
                blocks=blocks,
                studentize=studentize,  # type: ignore[arg-type]
            )
            kept[studentize] += len(r.included) == 16
    assert kept["wild"] / sims >= 0.82
    assert kept["fixed"] / sims <= 0.6


def test_sampled_path_families_are_scored_on_their_sampled_path(predictions: Builder) -> None:
    """`nt_prob_touch[p<1]` has an analytic p_fill but one RNG-sampled fill path: the first-fill error scores that
    path (the fill time a backtest using the model would see), the Brier its analytic p_fill."""
    table, life, info = _zoo(predictions, n=80)
    mid = np.asarray(table.column("model_id").to_pylist())
    touch = table.filter(pa.array(mid == "close[a=1]"))
    for name, value in (("model_id", "nt_prob_touch[p=0.5]"), ("p_fill", 0.5)):
        f = table.schema.field(name)
        i = table.schema.get_field_index(name)
        touch = touch.set_column(i, f, pa.array([value] * touch.num_rows, f.type))
    both = pa.concat_tables([table, touch])
    hl = next(x for x in horizon_losses(both, life, h_max_ns=HMAX) if x.horizon_ns == H10)
    j, k = hl.model_ids.index("nt_prob_touch[p=0.5]"), hl.model_ids.index("close[a=1]")
    np.testing.assert_array_equal(hl.first_fill[:, j], hl.first_fill[:, k])  # the same sampled path
    y = np.array([info["t_fill"][info["ids"].index(str(s_))] <= H10 for s_ in hl.shadow_ids])
    np.testing.assert_allclose(hl.brier[:, j], (0.5 - y) ** 2)
    res, _ = leaderboard_mcs(both, life, h_max_ns=HMAX, bootstrap_b=100)
    assert res["nt_prob_touch[p=0.5]"]["first_fill_error"][str(H10)] == res["close[a=1]"]["first_fill_error"][str(H10)]


# ---- CLI marker -----------------------------------------------------------------------------------------------------


def _entry(m: str, bias: float, member: bool | None, ffe: float | None = None) -> dict[str, Any]:
    k = str(60 * S)
    mcs = {} if member is None else {"mcs_member": {k: member}, "mcs_p": {k: 0.5 if member else 0.01}}
    if ffe is not None or member is not None:
        mcs["first_fill_error"] = {k: ffe}
    return {
        "model_id": m,
        "brier": {k: 0.1},
        "logloss": {k: 0.3},
        "fill_rate_bias": {k: {"value": bias, "ci_lo": bias - 0.01, "ci_hi": bias + 0.01}},
        "overfill_bias": {"value": None, "ci_lo": None, "ci_hi": None},
        "qty_bias": None,
        "ttf_iad": None,
        "mcnemar": {"b": 0, "c": 0, "p_exact": None},
        **mcs,
    }


def _doc(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "qt.zoo_summary",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "spec": {"h_max_ns": 60 * S},
        "n_shadows": 10,
        "models": [e["model_id"] for e in entries],
        "leaderboard": entries,
        "files": [],
        "warnings": [],
    }


def test_mcs_mark_and_the_cli_table() -> None:
    a, b = _entry("alpha", 0.002, True, 0.012), _entry("beta", 0.05, False, 0.034)
    c = _entry("gamma", 0.001, True, 0.011)
    ref = _entry("hbt_l3_fifo[mode=corrected]", 0.0, None)
    ref.update({"mcs_member": {str(60 * S): None}, "first_fill_error": {str(60 * S): None}})
    assert mcs_mark(a, 60 * S) == MCS_MARK == "#" and mcs_mark(b, 60 * S) == " "
    assert mcs_mark(a, S) == " " and mcs_mark(a, None) == " " and mcs_mark(_entry("c", 0.0, None), 60 * S) == " "
    assert has_mcs([a, b]) and not has_mcs([_entry("c", 0.0, None)]) and not has_mcs([ref])
    out = cli.render(["zoo"], _doc([a, b, c, ref]))
    lines = out.splitlines()
    # the bias table keeps its legend and cells: the set is not about fill-rate bias
    assert "(* = 95% CI excludes 0):" in lines
    bias_a = next(line for line in lines if line.startswith("alpha"))
    assert bias_a.rstrip().endswith("+0.2") and "#" not in bias_a
    # a second table of first-fill error with the members marked, the order-level reference named as not ranked
    i = lines.index("first-fill error in % of the horizon (# = tied with the best, 90% MCS):")
    assert lines[i - 1] == "not ranked on first-fill error: hbt_l3_fifo[mode=corrected] (order-level reference)"
    ffe_rows = [line for line in lines[i:] if line.startswith(("alpha", "beta", "gamma", "hbt_l3"))][:3]
    assert [r.split()[0] for r in ffe_rows] == ["gamma", "alpha", "beta"]  # the leaderboard's order
    assert [r.rstrip()[-4:] for r in ffe_rows] == ["1.1#", "1.2#", " 3.4"]
    # and one plain-words line at the sort horizon, members by ascending error
    assert lines[-1] == (
        "  At 60 s, 2 of 3 models are tied for the lowest first-fill error (90% MCS): gamma (1.1%), alpha (1.2%)."
    )
    assert mcs_sentence([a, b], 60 * S) == (
        "At 60 s, alpha (1.2%) alone has the lowest first-fill error of 2 models (90% MCS)."
    )
    assert mcs_sentence([ref], 60 * S) is None and mcs_sentence([a], None) is None
    none_here = _entry("delta", 0.0, True, 0.01)
    none_here["mcs_member"][str(S)] = None
    assert mcs_sentence([none_here], S) == "At 1 s there is no model confidence set (see the mcs warnings)."
    legend, rows = mcs_table([a, ref], [60 * S])
    assert rows == [["alpha", "1.2#"]] and len(legend) == 2
    # a summary without MCS fields renders exactly as before (no second table, no marker, no extra sentence)
    plain = cli.render(["zoo"], _doc([_entry("alpha", 0.002, None), _entry("beta", 0.05, None)]))
    assert "(* = 95% CI excludes 0):" in plain and "#" not in plain and "first-fill" not in plain


# ---- end to end and real data ---------------------------------------------------------------------------------------


#: ZooSpec's fields before the MCS existed (queuetruth 0.1.1). The MCS adds no spec field, so it cannot change any
#: run_id; freezing the field set guards that without a version-specific golden run_id (run_id hashes the engine
#: version, and the fixture files carry it in their Parquet metadata, so a pinned hash breaks at every release).
PRE_MCS_ZOO_SPEC_FIELDS = [
    "amends",
    "bootstrap_b",
    "dataset",
    "exchange",
    "exclude_order_ids",
    "h_max_ns",
    "horizons_ns",
    "l2",
    "latency",
    "mode",
    "models",
    "out",
    "particles",
    "seed",
    "shadows",
    "threads",
    "write_fills",
    "write_trajectory",
]


def _resolve_ref(ref: dict[str, Any]) -> str:
    """An input reference as the core resolves it for run_id: a dataset directory's `dataset_id`, else the file's
    sha256."""
    p = Path(ref["path"])
    if (p / "manifest.json").is_file():
        return str(json.loads((p / "manifest.json").read_text())["dataset_id"])
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.requires_core
def test_run_zoo_writes_the_mcs_fields(repo_root: Path, tmp_path: Path) -> None:
    from queuetruth import api

    ds = str(repo_root / "tests" / "fixtures" / "synth_small")
    sh = repo_root / "tests" / "fixtures" / "synth_small" / "truth" / "shadows.parquet"
    spec = {
        "dataset": {"path": ds},
        "shadows": {"path": str(sh)},
        "models": ["front_of_queue", "hbt_power3[n=2]", "trade_through_only"],
        "bootstrap_b": 50,
    }
    a = api.run_zoo({**spec, "out": str(tmp_path / "a")})
    b = api.run_zoo({**spec, "out": str(tmp_path / "b")})
    # The MCS adds no spec field, so the run_id is the one the spec alone determines: the Python and Rust run_id
    # agree on the echoed spec, and the spec's fields are those of 0.1.1.
    assert sorted(specs.ZooSpec.model_fields) == PRE_MCS_ZOO_SPEC_FIELDS
    assert a.run_id == b.run_id
    echo = a.model_dump(mode="json")["spec"]
    assert runid.run_id(echo, qt_version, _resolve_ref) == a.run_id == api.run_id(echo)
    assert [e.model_dump(mode="json") for e in a.leaderboard] == [e.model_dump(mode="json") for e in b.leaderboard]
    keys = {k for e in a.leaderboard for k in e.fill_rate_bias}
    for e in a.leaderboard:
        assert set(e.mcs_member) == set(e.mcs_p) == set(e.first_fill_error) == keys
        assert any(v is not None for v in e.mcs_p.values())
    for k in keys:
        ps = [e.mcs_p[k] for e in a.leaderboard if e.mcs_p[k] is not None]
        assert ps and max(ps) == 1.0  # someone is always in the set
    doc = json.loads((tmp_path / "a" / "summary.json").read_text())
    assert ZooSummary.model_validate(doc).leaderboard[0].mcs_member == a.leaderboard[0].mcs_member


def _realdata_dir() -> Path | None:
    root = os.environ.get("QT_REALDATA_DIR")
    p = Path(root) if root else Path(__file__).resolve().parents[3] / ".realdata"
    return p if (p / "zoo" / "d1-sampled-corrected" / "zoo_predictions.parquet").exists() else None


def _real_mcs(run: str) -> dict[str, dict[str, dict[str, Any]]]:
    import pyarrow.parquet as pq

    from queuetruth.api import _planned_life

    d = _realdata_dir() / "zoo" / run  # type: ignore[operator]
    spec = json.loads((d / "summary.json").read_text())["spec"]
    planned, _ = _planned_life(d)
    preds = pq.read_table(d / "zoo_predictions.parquet")
    res, notes = leaderboard_mcs(
        preds, planned, h_max_ns=spec["h_max_ns"], bootstrap_b=spec["bootstrap_b"], seed=spec["seed"]
    )
    assert notes == []
    return res


def _members(res: dict[str, dict[str, dict[str, Any]]], h: int, field: str = "mcs_member") -> set[str]:
    """Members with the mode / exchange / particle tags dropped: `databento_mc[k=-0.8]`, `front_of_queue`."""
    out = set()
    for m in res:
        if res[m][field][str(h)]:
            base, _, rest = m.partition("[")
            tags = [t for t in rest.rstrip("]").split(";") if t and not t.startswith(("mode=", "exch=", "particles="))]
            out.add(base + (f"[{';'.join(tags)}]" if tags else ""))
    return out


@pytest.mark.realdata
@pytest.mark.skipif(_realdata_dir() is None, reason="needs .realdata/zoo/d1-sampled-corrected (set QT_REALDATA_DIR)")
def test_real_day_one_leaderboard_mcs() -> None:
    """The figures quoted in docs/concepts/selection-bias.md (Nasdaq ITCH 2019-12-30; 13 thirty-minute blocks)."""
    ms = 1_000_000
    hs = (100 * ms, S, 10 * S, 60 * S, 300 * S)
    res = _real_mcs("d1-sampled-corrected")
    assert len(res) == 16 and not any(m.startswith("hbt_l3_fifo") for m in res)
    every = _members(res, 100 * ms) | {"front_of_queue", "nt_prob_touch[p=1]"}
    touch = {"front_of_queue", "nt_prob_touch[p=1]", "trade_through_only"}
    four = {"databento_mc[k=-0.8]", "databento_mc[k=-0.95]", "nt_queue_l2", "hbt_risk_adverse"}
    later = {"hbt_power3[n=3]", "hbt_log2", "hbt_power3[n=2]", "hbt_power[n=3]"}
    assert len(every) == 16
    assert (
        _members(res, 100 * ms) == _members(res, 100 * ms, "mcs_brier_member") == every - touch | {"trade_through_only"}
    )
    assert _members(res, S) == _members(res, S, "mcs_brier_member") == every - touch
    assert _members(res, 10 * S) == four and len(_members(res, 10 * S, "mcs_brier_member")) == 5
    assert _members(res, 60 * S) == _members(res, 300 * S) == four | later
    assert [len(_members(res, h, "mcs_brier_member")) for h in (60 * S, 300 * S)] == [13, 11]
    # lowest mean first-fill error: 13 models tie exactly at 0.1 s; databento_mc[k=-0.8] is lowest from 1 s on
    ffe = {h: {m: res[m]["first_fill_error"][str(h)] for m in res} for h in hs}
    low = {h: min(ffe[h].values()) for h in hs}
    assert sum(v == low[100 * ms] for v in ffe[100 * ms].values()) == 13
    for h in hs[1:]:
        best = [m for m, v in ffe[h].items() if v == low[h]]
        assert len(best) == 1 and best[0].startswith("databento_mc[k=-0.8;") and res[best[0]]["mcs_p"][str(h)] == 1.0
    assert [round(100 * low[h], 2) for h in hs] == [0.07, 0.23, 0.82, 1.0, 0.54]
    near = sorted(ffe[S].values())  # 1 s: three models within 0.001 pp of the lowest
    assert round(100 * near[0], 3) == 0.225 and [round(100 * v, 3) for v in near[1:4]] == [0.226] * 3
    faithful = _real_mcs("d1-sampled-faithful")
    assert len(_members(faithful, 100 * ms)) == 14
    assert {round(faithful[m]["mcs_p"][str(100 * ms)], 3) for m in faithful if not m.startswith("nt_queue_l2")} == {
        0.101,
        0.0,
    }
    for h in hs[1:]:
        assert _members(faithful, h) == {"nt_queue_l2"}
        assert max(faithful[m]["mcs_p"][str(h)] for m in faithful if not m.startswith("nt_queue_l2")) <= 0.025
    assert [len(_members(faithful, h, "mcs_brier_member")) for h in hs] == [14, 1, 1, 1, 10]
    mirror = _real_mcs("d1-et_mirror-corrected")
    assert [len(_members(mirror, h)) for h in hs] == [13, 10, 15, 16, 16]
    assert [len(_members(mirror, h, "mcs_brier_member")) for h in hs] == [16, 13, 16, 16, 16]
