"""`queuetruth.api.why_explain` / `why` (trader review item 5, C4 and F5): the exact trace with a one-sentence
conclusion, the zoo model's queue estimate per event (from a zoo run's trajectory or a one-shadow run), the first
divergence, the user's own order removed from live-order traces (A1), and no `why` suggestion that cannot run on
L2-only data."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from queuetruth import api
from queuetruth.errors import QtError
from queuetruth.schemas import models as m

pytestmark = pytest.mark.requires_core


@pytest.fixture(scope="module")
def synth(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures" / "synth_small"


@pytest.fixture(scope="module")
def shadows(synth: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("why") / "shadows.parquet"
    api.sample_shadows({"dataset": {"path": str(synth)}, "out": str(p), "n": 40, "seed": 5})
    return p


def _spec(synth: Path, shadows: Path, sid: str, **kw: Any) -> dict[str, Any]:
    return {"dataset": {"path": str(synth)}, "shadows": {"path": str(shadows)}, "shadow_id": sid, **kw}


@pytest.fixture(scope="module")
def filled_id(synth: Path, shadows: Path) -> str:
    for sid in pq.read_table(shadows).column("shadow_id").to_pylist():
        x = api.why_explain(_spec(synth, shadows, sid))
        if x.trace.outcome.filled_qty > 0 and x.trace.events:
            return str(sid)
    pytest.fail("no filled shadow among the sampled ones")


def test_conclusion_and_units(synth: Path, shadows: Path, filled_id: str) -> None:
    x = api.why_explain(_spec(synth, shadows, filled_id))
    assert x.symbol is not None and x.symbol.startswith("QT") and x.price_exp == -9 and x.size_exp == 0
    assert x.model_id is None and x.model_queue_ahead == [] and x.first_divergence is None
    assert x.conclusion.startswith(f"Filled {x.trace.outcome.filled_qty} of {x.trace.qty} at ")
    assert " UTC" in x.conclusion and "Queue ahead went " in x.conclusion
    # The trace is unchanged without a model: the same document as the core's.
    assert api.why(_spec(synth, shadows, filled_id)) == x.trace


def test_model_column_and_first_divergence(synth: Path, shadows: Path, filled_id: str, tmp_path: Path) -> None:
    out = tmp_path / "why.json"
    x = api.why_explain(_spec(synth, shadows, filled_id, out=str(out)), model="front_of_queue")
    assert x.model_id is not None and x.model_id.startswith("front_of_queue")
    assert len(x.model_queue_ahead) == len(x.trace.events)
    est = [v for v in x.model_queue_ahead if v is not None]
    assert est and all(v == 0.0 for v in est)  # front of queue: nothing ahead, ever
    # The exact queue had something ahead at some point, so the model diverges.
    if any(e.queue_ahead_after > 0 for e in x.trace.events):
        assert x.first_divergence is not None
        assert f"Model {x.model_id}" in x.conclusion
    # JSON carries the model column in the notes and stays a valid qt.why_trace.
    doc = json.loads(out.read_text())
    m.WhyTrace.model_validate(doc)
    notes = [e["note"] or "" for e in doc["events"]]
    assert any("model_ahead=0" in n for n in notes)
    if x.first_divergence is not None:
        assert notes[x.first_divergence].endswith(
            f"first_divergence: {x.model_id} 0 vs exact {doc['events'][x.first_divergence]['queue_ahead_after']}"
        )


def test_model_from_a_zoo_run_and_stale_trajectories(
    synth: Path, shadows: Path, filled_id: str, tmp_path: Path
) -> None:
    zoo = tmp_path / "zoo"
    api.run_zoo(
        {
            "dataset": {"path": str(synth)},
            "shadows": {"path": str(shadows)},
            "out": str(zoo),
            "models": ["front_of_queue", "hbt_power3[n=2]"],
            "write_trajectory": True,
            "bootstrap_b": 5,
        }
    )
    x = api.why_explain(_spec(synth, shadows, filled_id), zoo=str(zoo), model="hbt_power3[n=2]")
    assert x.model_id is not None and x.model_id.startswith("hbt_power3[") and "n=2" in x.model_id
    assert x.notes == [] and any(v is not None for v in x.model_queue_ahead)
    # The same model run on the order alone gives the same estimates.
    y = api.why_explain(_spec(synth, shadows, filled_id), model="hbt_power3[n=2]")
    assert y.model_queue_ahead == x.model_queue_ahead and y.first_divergence == x.first_divergence
    # Without --model the zoo's model that strays furthest from the exact queue is shown.
    z = api.why_explain(_spec(synth, shadows, filled_id), zoo=str(zoo))
    assert z.model_id is not None
    # A trajectory whose exact queue disagrees with this trace (another tape or schedule) is not used.
    t = pq.read_table(zoo / "zoo_trajectory.parquet")
    ex = pc.add(t.column("queue_ahead_exact"), pa.scalar(7, pa.uint64()))
    t = t.set_column(t.schema.get_field_index("queue_ahead_exact"), "queue_ahead_exact", ex)
    pq.write_table(t, zoo / "zoo_trajectory.parquet")
    w = api.why_explain(_spec(synth, shadows, filled_id), zoo=str(zoo))
    assert w.model_id is None and any("disagrees with this trace" in n for n in w.notes)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        api.why(_spec(synth, shadows, filled_id), zoo=str(zoo))
    assert any("disagrees with this trace" in str(c.message) for c in caught)


def test_live_order_trace_excludes_the_own_order(synth: Path) -> None:
    live = synth / "own_orders" / "live"
    orders = pq.read_table(live / "orders.parquet")
    sched = pq.read_table(synth / "own_orders" / "schedule.parquet")
    keys = [k for k in sched.column("order_key").to_pylist() if k is not None]
    vids = dict(zip(orders.column("order_key").to_pylist(), orders.column("venue_order_id").to_pylist(), strict=True))
    checked = 0
    for key in keys[:6]:
        try:
            x = api.why_explain({"dataset": {"path": str(synth)}, "live": {"path": str(live)}, "order_key": key})
        except QtError:
            continue
        assert x.trace.subject.order_key == key
        own = int(vids[key])
        assert x.excluded_order_ids == [own], x.notes
        # The user's own order never appears in its own trace (it would sit ahead of itself).
        assert all(e.order_id != own for e in x.trace.events)
        checked += 1
    assert checked >= 3


def test_l2_only_dataset_suggests_a_command_that_runs(tmp_path: Path) -> None:
    ds = tmp_path / "l2ds"
    ds.mkdir()
    (ds / "manifest.json").write_text(json.dumps({"kind": "l2"}))
    with pytest.raises(QtError) as e:
        api.why_explain({"dataset": {"path": str(ds)}, "live": {"path": "live"}, "order_key": "binance:A:1"})
    assert e.value.code == "QT-CONFIG-001" and "L2-only" in e.value.message
    hint = e.value.hint or ""
    assert "queuetruth zoo" in hint and "--write-trajectory" in hint and "queuetruth why" not in hint
    s = api.why_suggestion(str(ds), shadows="sh.parquet", shadow_id="s1", model="hbt_power3[n=2]")
    assert s is not None and s.startswith("queuetruth zoo --data") and "why " not in s.split("(")[0]
    mbo = tmp_path / "mbo"
    mbo.mkdir()
    (mbo / "manifest.json").write_text(json.dumps({"kind": "mbo"}))
    assert api.why_suggestion(str(mbo), live="live", order_key="k", model="front_of_queue") == (
        f"queuetruth why --data {mbo} --live live --order-key k --model front_of_queue"
    )
    assert api.why_suggestion(str(tmp_path / "missing"), shadows="s", shadow_id="x") is None
