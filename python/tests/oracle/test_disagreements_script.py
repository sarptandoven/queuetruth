"""`scripts/oracle/disagreements.py`: the evidence passes behind docs/design/extensions/oracle-disagreements.md."""

from __future__ import annotations

import hashlib
import importlib.util

from _helpers import REPO
from qt_oracle.scenarios import build_tape, shadow
from qt_oracle.tape import InstrumentMeta

NS = 1_000_000_000


def _mod():
    spec = importlib.util.spec_from_file_location("disagreements", REPO / "scripts" / "oracle" / "disagreements.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _meta(tick: int = 1) -> InstrumentMeta:
    return InstrumentMeta(1, "2019-12-30", "nasdaq", tick, 0, 0, 57_600, "itch")


def test_priority_counts_late_adds_and_reference_order_executions() -> None:
    recs = build_tape(
        [
            (10, ["A 5 B 100 10"]),
            (20, ["A 7 B 100 10"]),
            (30, ["A 3 B 100 2"]),  # late, lands ahead of larger references 5 and 7
            (40, ["A 2 B 99 1"]),  # late, at an empty level
            (50, ["T A 100 2", "F 3 2", "C 3 2"]),  # arrival front is 5; reference front is 3
            (61 * NS, ["T A 100 10", "F 5 10", "C 5 10"]),  # the front either way
        ],
        seq_per_event=True,
    )
    ev = _mod().priority_evidence(_meta(), recs)
    assert ev["late_adds"] == {"pre_open": 0, "first_60s": 2, "after_60s": 0}
    assert ev["late_adds_at_a_level_with_a_larger_reference"]["first_60s"] == 1
    assert ev["late_adds_first_s_after_open"] == 30 / NS and ev["late_adds_last_s_after_open"] == 40 / NS
    assert ev["executions"]["first_60s"] == {
        "all": 1,
        "not_arrival_front": 1,
        "not_arrival_front_but_reference_front": 1,
    }
    assert ev["executions"]["after_60s"] == {
        "all": 1,
        "not_arrival_front": 0,
        "not_arrival_front_but_reference_front": 0,
    }


def test_classify_finds_released_orders_and_reference_order_executions() -> None:
    recs = build_tape(
        [
            (10, ["A 5 B 100 10"]),
            (20, ["A 7 B 100 10"]),  # the shadow goes live at this event: bound 5
            (30, ["A 3 B 100 2", "A 6 B 100 1"]),  # 3 < 5: received before the shadow; 6 is not
            (50, ["T A 100 2", "F 3 2", "C 3 2"]),
        ],
        seq_per_event=True,
    )
    sh = shadow("x", "B", 100, qty=1, ts_submit=15)
    ev = _mod().classify_evidence(_meta(), recs, [sh], {"x": 50})["x"]
    assert ev["bound"] == 5 and ev["live_s_after_open"] == round(20 / NS, 3)
    assert ev["d1"] == {"n": 1, "first": [(30, 3, 2)]}
    assert ev["d1_exec"]["n"] == 1 and ev["o1_at"] == {"n": 1, "first": [(50, 3, 5)]}
    assert ev["o1_better"]["n"] == 0 and ev["d2"]["n"] == 0 and ev["d3"]["n"] == 0


def test_classify_finds_hidden_prints_of_a_sweep() -> None:
    recs = build_tape(
        [
            (10, ["A 11 A 990 5", "A 12 B 1000 5", "A 13 B 970 1"]),
            # a buyer lifts the ask at 990 and then hidden asks at 990: the sweep is on the ask side (D2 for a bid)
            (100, ["T B 990 5", "F 11 5", "C 11 5"]),
            (100, ["T N 990 4"]),
            # a seller hits the bid at 1000 and then a hidden midpoint bid at 985, through a bid at 990 (D3)
            (200, ["T A 1000 5", "F 12 5", "C 12 5"]),
            (200, ["T N 985 3"]),
            # an add on the swept side closes the sweep: no evidence
            (300, ["T A 970 1", "F 13 1", "C 13 1"]),
            (300, ["A 14 B 960 1"]),
            (300, ["T N 980 2"]),
        ],
        seq_per_event=True,
    )
    sh = shadow("y", "B", 990, qty=10, ts_submit=15)
    ev = _mod().classify_evidence(_meta(10), recs, [sh], {"y": 300})["y"]
    assert ev["d2"] == {"n": 1, "first": [(100, "A", 990, 4)]}
    assert ev["d3"] == {"n": 1, "first": [(200, "B", 985, 3)]}


def test_priority_follows_modifies_and_clears() -> None:
    """An `M` that only lowers the size keeps the order's place; one that raises it (or moves it) goes to the tail;
    an `R` empties the book. Without them the arrival-order fronts go stale."""
    recs = build_tape(
        [
            (10, ["A 5 B 100 10"]),
            (20, ["A 7 B 100 10"]),
            (30, ["M 5 B 100 4"]),  # keeps its place: 5 is still the front
            (40, ["T A 100 4", "F 5 4", "C 5 4"]),
            (50, ["A 8 B 100 3"]),
            (60, ["M 7 B 100 20"]),  # loses priority: 8 is the arrival front now, 7 the reference front
            (70, ["T A 100 3", "F 8 3", "C 8 3"]),
            (80, ["R"]),
            (90, ["A 9 B 100 1"]),
            (95, ["T A 100 1", "F 9 1", "C 9 1"]),  # alone after the clear: the front
        ],
        seq_per_event=True,
    )
    ev = _mod().priority_evidence(_meta(), recs)
    assert ev["executions"]["first_60s"] == {
        "all": 3,
        "not_arrival_front": 0,
        "not_arrival_front_but_reference_front": 0,
    }


def test_classify_without_a_divergence_time_walks_to_the_end() -> None:
    recs = build_tape(
        [
            (10, ["A 5 B 100 10"]),
            (20, ["A 7 B 100 10"]),
            (5 * NS, ["A 3 B 100 2"]),
        ],
        seq_per_event=True,
    )
    sh = shadow("x", "B", 100, qty=1, ts_submit=15)
    ev = _mod().classify_evidence(_meta(), recs, [sh], {"x": None})["x"]
    assert ev["divergence_s_after_open"] is None
    assert ev["d1"] == {"n": 1, "first": [(5 * NS, 3, 2)]}


def test_outputs_carry_provenance(tmp_path) -> None:
    f = tmp_path / "shadows.csv"
    f.write_text("x\n")
    d = tmp_path / "ds"
    d.mkdir()
    (d / "manifest.json").write_text('{"dataset_id": "abc"}')
    prov = _mod().provenance({"shadows": str(f), "dataset": str(d), "gone": str(tmp_path / "nope")})
    assert prov["oracle_version"]
    assert prov["inputs"]["shadows"]["sha256"] == hashlib.sha256(b"x\n").hexdigest()
    assert prov["inputs"]["dataset"]["dataset_id"] == "abc"
    assert prov["inputs"]["gone"]["missing"] is True
    assert set(prov["queuetruth"]) >= {"version", "extension_sha256"}


def test_bound_splits_adds_by_the_instrument_and_counter_bounds() -> None:
    """Instruments 1 and 2 share a reference counter (their adds after the first minute interleave in reference
    order); 3 has its own. The shadow on 1 goes live with 1's references at 5 and 2's at 50. Added at its level
    afterwards: 3 (below 5: ahead), 20 (between 5 and 50: received before the shadow, ranked behind it by both
    engines) and 55 (received after it). Instrument 3's reference 40 says nothing about 1's counter."""
    from qt_oracle.tape import Partition

    m = 61 * NS
    one = build_tape(
        [
            (10, ["A 5 B 100 10"]),
            (30, ["A 3 B 100 1"]),
            (40, ["A 20 B 100 1"]),
            (50, ["A 60 A 101 1"]),
            (60, ["A 55 B 100 1"]),
            (70, ["A 4 B 99 1"]),  # another level
            (m + 1, ["A 100 A 102 1"]),
            (m + 3, ["A 300 A 102 1"]),
        ],
        seq_per_event=True,
    )
    two = build_tape([(15, ["A 50 B 7 1"]), (m + 2, ["A 200 B 7 1"])], instrument_id=2, seq_per_event=True)
    three = build_tape([(12, ["A 40 B 7 1"]), (m + 2, ["A 45 B 7 1"])], instrument_id=3, seq_per_event=True)

    def meta(i: int) -> InstrumentMeta:
        return InstrumentMeta(i, "2019-12-30", "nasdaq", 1, 0, 0, 57_600, "itch")

    parts = [Partition(meta(1), [], one), Partition(meta(2), [], two), Partition(meta(3), [], three)]
    res = [
        {"shadow_id": "x", "instrument_id": 1, "side": "B", "price": 100, "ts_activated_event": 20, "ts_end": None},
        {"shadow_id": "y", "instrument_id": 1, "side": "B", "price": 100, "ts_activated_event": None},
    ]
    ev = _mod().bound_evidence(parts, res)
    assert ev["counter_groups"] == [[1, 2], [3]]
    assert ev["total"] == {
        "shadows_live": 1,
        "below_instrument_bound": 1,
        "between_bounds": 1,
        "shadows_with_between": 1,
    }
    assert ev["shadows_with_between"][0]["bound_instrument"] == 5
    assert ev["shadows_with_between"][0]["bound_counter"] == 50
