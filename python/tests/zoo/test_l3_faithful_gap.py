"""Faithful `hbt_l3_fifo` against the exact replay, through the zoo and the atlas's block statistics.

The Rust tests `crates/zoo/tests/l3_faithful_gap.rs` pin the rules; this checks the comparison the Fill-Realism Atlas
makes on the synthetic Nasdaq fixture with the `standard` population's placements and sizes:

- both arms are scored on the same rows with the same label: the exact fill rate is identical for every model, the
  corrected mode equals it, and `p_fill` / `truth_filled` both mean "first fill by the deadline", so faithful's whole
  order fills do not count for more than a partial exact fill;
- every shadow faithful misses within 60 s has a corrected (= exact) first fill of `trade_through` (rule 1, or a
  hidden print through the price), `queue` (a usable hidden print at the price, rule 2) or `crossed_at_live`
  (rule 3) (docs/concepts/l2-model-zoo.md (c)). The counts are pinned. The reason alone cannot tell rule 1 from a
  hidden print through the price; `crates/zoo/tests/l3_faithful_gap.rs` makes that split from the records.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

REPO = Path(__file__).resolve().parents[3]
FIXTURE = REPO / "tests" / "fixtures" / "synth_small_nasdaq"
S = 1_000_000_000
H = 60 * S
H_MAX = 300 * S
FAITHFUL = "hbt_l3_fifo[mode=faithful]"
CORRECTED = "hbt_l3_fifo[mode=corrected]"
# scripts/repro/breadth.py STD_PLACEMENTS / STD_QTY (the atlas's `standard` population)
PLACEMENTS = [
    {"offset_ticks": 0, "weight": 0.6},
    {"offset_ticks": -1, "weight": 0.2},
    {"offset_ticks": -2, "weight": 0.1},
    {"offset_ticks": 1, "weight": 0.1},
]
# shadows only one arm fills within 60 s, by the first fill's reason (corrected arm for missed, faithful for extra)
MISSED = {"trade_through": 26, "queue": 1}
EXTRA = {"trade_through": 1}
QTY = [{"kind": "lots", "lots": 1, "weight": 0.5}, {"kind": "median_order_size", "weight": 0.5}]


@pytest.fixture(scope="module")
def zoo_out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("queuetruth._core")
    from queuetruth import api

    if not (FIXTURE / "manifest.json").exists():
        pytest.fail("tests/fixtures/synth_small_nasdaq is missing (run `make fixtures`)")
    root = tmp_path_factory.mktemp("l3gap")
    sh = root / "shadows.parquet"
    api.sample_shadows(
        {"dataset": {"path": str(FIXTURE)}, "out": str(sh), "n": 2000, "seed": 0, "placements": PLACEMENTS, "qty": QTY}
    )
    api.run_zoo(
        {
            "dataset": {"path": str(FIXTURE)},
            "shadows": {"path": str(sh)},
            "out": str(root / "zoo"),
            "models": [FAITHFUL, CORRECTED, "hbt_power3[n=2]"],
            "horizons_ns": [S, 10 * S, H],
            "h_max_ns": H_MAX,
            "bootstrap_b": 1,
        }
    )
    return root / "zoo"


@pytest.mark.requires_core
def test_both_arms_are_scored_on_the_same_rows(zoo_out: Path) -> None:
    from queuetruth import api
    from queuetruth.eval import blocks as B

    pred = pq.read_table(zoo_out / "zoo_predictions.parquet")
    planned, _ = api._planned_life(zoo_out)
    st = B.block_stats(pred, planned, h_max_ns=H_MAX, block_ns=None)["stats"]
    st = st.filter(pc.and_(pc.equal(st["stratum_kind"], "all"), pc.equal(st["horizon_ns"], H)))
    by: dict[str, Counter] = {}
    for r in st.to_pylist():
        c = by.setdefault(r["model_id"], Counter())
        for k in ("n", "sum_p", "n_y"):
            c[k] += r[k]
    f, c = by[FAITHFUL], by[CORRECTED]
    # same rows and labels for every model; corrected is the exact replay (O-ZOO-4)
    assert all(m["n"] == f["n"] and m["n_y"] == f["n_y"] for m in by.values()), by
    assert c["sum_p"] == c["n_y"]
    # the gap the atlas reports, pinned: faithful 1007 of 1970 within 60 s, exact 1033
    assert (f["n"], f["sum_p"], f["n_y"]) == (1970, 1007.0, 1033), f


@pytest.mark.requires_core
def test_every_missed_fill_is_a_trade_through_or_hidden_print(zoo_out: Path) -> None:
    pred = pq.read_table(zoo_out / "zoo_predictions.parquet")
    pred = pred.filter(pc.and_(pc.equal(pred["horizon_ns"], H), pc.invert(pred["censored"])))
    p = {(r["model_id"], r["shadow_id"]): r["p_fill"] for r in pred.to_pylist() if r["truth_filled"] is not None}
    first: dict[tuple[str, str], str] = {}
    for r in pq.read_table(zoo_out / "zoo_fills.parquet").to_pylist():
        first.setdefault((r["model_id"], r["shadow_id"]), r["reason"])
    missed, extra = Counter(), Counter()
    for (m, sid), pf in p.items():
        if m != FAITHFUL:
            continue
        pc_ = p[(CORRECTED, sid)]
        if pf == 0.0 and pc_ == 1.0:
            missed[first[(CORRECTED, sid)]] += 1
        elif pf == 1.0 and pc_ == 0.0:
            extra[first[(FAITHFUL, sid)]] += 1
    assert set(missed) <= {"trade_through", "queue", "crossed_at_live"}, missed
    assert (dict(missed), dict(extra)) == (MISSED, EXTRA)
