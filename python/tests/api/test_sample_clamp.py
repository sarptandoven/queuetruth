"""`shadows sample` keeps every life inside the data (audit 2026-11-12: on a 15-minute capture 1,494 of 3,000
five-minute lives ran past the end of the data, so the zoo showed four different populations), and `report` refuses
inputs it cannot render instead of writing a near-empty page."""

from __future__ import annotations

from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from queuetruth import api
from queuetruth.errors import QtError

REPO = Path(__file__).resolve().parents[3]
SYNTH = REPO / "tests" / "fixtures" / "synth_small"
S = 1_000_000_000


def _data_end(ds: Path) -> int:
    return max(
        pc.max(pc.cast(pq.read_table(p, columns=["ts_event"]).column("ts_event"), "int64")).as_py()
        for p in (ds / "mbo").rglob("*.parquet")
    )


@pytest.mark.requires_core
def test_lives_end_inside_the_data_unless_asked(tmp_path: Path) -> None:
    # 2 instruments x 2 days; each synthetic day ends about 15 minutes after the close, so a 30-minute life placed
    # up to the close would run past it
    life = 1_800 * S
    spec = {"dataset": {"path": str(SYNTH)}, "n": 400, "seed": 4, "window_close_s": 0, "lifetime_ns": life}
    end = _data_end(SYNTH)
    new = api.sample_shadows({**spec, "out": str(tmp_path / "new.parquet")})
    t = pq.read_table(tmp_path / "new.parquet")
    assert t.num_rows == new.n == 400
    assert pc.max(pc.cast(t.column("ts_cancel"), "int64")).as_py() <= end
    assert any(w.startswith("placement window shortened on 4 instrument-day(s)") for w in new.warnings)
    # the old behaviour stays reachable: lives past the end, no warning
    old = api.sample_shadows({**spec, "clamp_to_data_end": False, "out": str(tmp_path / "old.parquet")})
    t_old = pq.read_table(tmp_path / "old.parquet")
    assert pc.max(pc.cast(t_old.column("ts_cancel"), "int64")).as_py() > end
    assert old.warnings == []
    # the default does not enter the run identity: an old spec echo is unchanged apart from the new field
    assert new.spec["clamp_to_data_end"] is True and old.spec["clamp_to_data_end"] is False


@pytest.mark.requires_core
def test_default_lives_already_inside_a_full_day_are_untouched(tmp_path: Path) -> None:
    # placements end 600 s before the close and the data runs past the close: nothing to clamp, same bytes as before
    a = api.sample_shadows({"dataset": {"path": str(SYNTH)}, "n": 200, "seed": 1, "out": str(tmp_path / "a.parquet")})
    b = api.sample_shadows(
        {"dataset": {"path": str(SYNTH)}, "n": 200, "seed": 1, "clamp_to_data_end": False, "out": str(tmp_path / "b")}
    )
    assert a.warnings == [] and b.warnings == []
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b").read_bytes()


@pytest.mark.requires_core
def test_report_refuses_inputs_it_cannot_render(tmp_path: Path) -> None:
    from queuetruth.report import render

    # a recognised document the report cannot draw is a usage error that names what it renders
    dq = tmp_path / "dq"
    rep = api.dq({"dataset": {"path": str(SYNTH)}, "out": str(dq), "checks": ["vintage"]})
    assert rep.status == "pass"
    with pytest.raises(QtError) as e:
        render({"inputs": [str(dq)], "out": str(tmp_path / "r.html")})
    assert e.value.code == "QT-USAGE-001"
    assert "found qt.dq_report" in e.value.message and "zoo (leaderboard and drill-down)" in e.value.message
    assert e.value.hint is not None and "dq_report.html" in e.value.hint
    assert not (tmp_path / "r.html").exists()
