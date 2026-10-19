"""Schema 1.1 `HazardFitSpec.zoo_shadows`: the planned-life table of the zoo comparison (review F6) named in the spec
wins over `zoo_shadows.parquet` next to the zoo predictions; without the field the sibling is still discovered; the
field without `zoo_predictions` is `QT-CONFIG-001` (never silently ignored); the default (null) keeps `run_id`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from model import reference_stats
from model.hazard_synth import NS, make_features
from queuetruth.errors import QtError
from queuetruth.model import fit_hazard
from queuetruth.schemas import specs as s
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import write_table
from queuetruth.schemas.runid import run_id

FAST = {"max_iter": 30, "min_samples_leaf": 40, "max_leaf_nodes": 7}


@pytest.fixture(scope="module")
def features_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    p = tmp_path_factory.mktemp("feat") / "features.parquet"
    write_table(make_features(5, orders_per_day=200), p)
    return p


def _lm0(features_path: Path) -> tuple[list[str], list[int], list[str]]:
    f = pq.read_table(features_path)
    lm0 = f.filter(pa.compute.equal(f.column("landmark_ns"), 0))
    return (
        lm0.column("order_ref").to_pylist(),
        lm0.column("event_ns").to_pylist(),
        lm0.column("cause").cast(pa.utf8()).to_pylist(),
    )


def _zoo(features_path: Path) -> pa.Table:
    sids, ev, cause = _lm0(features_path)
    schema = SCHEMAS["qt.zoo_predictions"]
    rows: dict[str, list] = {k: [] for k in schema.names}
    for sid, e, c in zip(sids, ev, cause, strict=True):
        y = c == "fill" and e <= 10 * NS
        vals = {
            "model_id": "front_of_queue",
            "shadow_id": sid,
            "qty": 1,
            "horizon_ns": 10 * NS,
            "p_fill": 0.9,
            "e_fill_qty": 0.9,
            "truth_filled": y,
            "truth_filled_qty": int(y),
            "censored": False,
            "visible_level_qty_at_live": 0,
            "out_of_view_at_live": False,
            "instrument_id": 1,
            "session_date": dt.date(1970, 1, 1),
            "ts_live": 0,
            "trade_vol_60s": 0,
        }
        for k in rows:
            rows[k].append(vals.get(k))
    return pa.Table.from_arrays([pa.array(rows[f.name]).cast(f.type) for f in schema], schema=schema)


def _planned_life(features_path: Path, never_cancel: bool) -> pa.Table:
    sids = _lm0(features_path)[0]
    n = len(sids)
    ts = pa.timestamp("ns", tz="UTC")
    cols = {
        "shadow_id": pa.array(sids, pa.utf8()),
        "ts_cancel_planned": pa.array([None if never_cancel else NS] * n, pa.int64()).cast(ts),
        "ts_admin_end": pa.array([None] * n, pa.int64()).cast(ts),
        "instrument_id": pa.array([1] * n, pa.uint32()),
        "session_date": pa.array([dt.date(1970, 1, 1)] * n, pa.date32()),
        "ts_live": pa.array([0] * n, pa.int64()).cast(ts),
        "session_close": pa.array([10**13] * n, pa.int64()).cast(ts),
        "exact_end_state": pa.array([None] * n, pa.utf8()),
        "never_cancel": pa.array([never_cancel] * n, pa.bool_()),
    }
    schema = SCHEMAS["qt.zoo_shadows"]
    return pa.Table.from_arrays([cols[f.name].cast(f.type) for f in schema], schema=schema)


def _spec(features_path: Path, **kw: object) -> dict:
    return {"features": {"path": str(features_path)}, "gbdt": FAST, "bootstrap_b": 20, "grid_ns": [NS, 10 * NS], **kw}


def _rule(card: object) -> str:
    return next(w for w in card.warnings if w.startswith("zoo_comparison: "))  # type: ignore[attr-defined]


def test_zoo_shadows_field_wins_over_the_sibling(
    monkeypatch: pytest.MonkeyPatch, features_path: Path, tmp_path: Path
) -> None:
    reference_stats.install(monkeypatch)
    zdir = tmp_path / "zoo"
    zdir.mkdir()
    write_table(_zoo(features_path), zdir / "zoo_predictions.parquet")
    pred = {"path": str(zdir / "zoo_predictions.parquet")}
    # Sibling: every shadow has a planned cancel (nothing comparable). Spec table: every shadow never-cancel.
    write_table(_planned_life(features_path, never_cancel=False), zdir / "zoo_shadows.parquet")
    other = tmp_path / "elsewhere" / "planned.parquet"
    other.parent.mkdir()
    write_table(_planned_life(features_path, never_cancel=True), other)

    discovered = fit_hazard(_spec(features_path, zoo_predictions=pred))
    assert "no test shadow is comparable" in _rule(discovered)
    assert "planned life of qt.zoo_shadows" in _rule(discovered)

    named = fit_hazard(_spec(features_path, zoo_predictions=pred, zoo_shadows={"path": str(other)}))
    assert named.zoo_comparison is not None and "front_of_queue" in named.zoo_comparison
    assert "planned life of qt.zoo_shadows" in _rule(named)
    assert named.spec["zoo_shadows"] == {"path": str(other)}

    # Without a sibling and without the field: an error, never the censored flag (open conflict 2, E38 OC-7).
    (zdir / "zoo_shadows.parquet").unlink()
    with pytest.raises(QtError) as ei:
        fit_hazard(_spec(features_path, zoo_predictions=pred))
    assert ei.value.code == "QT-INPUT-001" and "zoo_shadows.parquet" in ei.value.message


def test_zoo_shadows_needs_zoo_predictions(features_path: Path, tmp_path: Path) -> None:
    with pytest.raises(QtError) as err:
        fit_hazard(_spec(features_path, zoo_shadows={"path": str(tmp_path / "z.parquet")}))
    assert err.value.code == "QT-CONFIG-001" and "zoo_shadows" in err.value.message


def test_zoo_shadows_default_keeps_the_run_id() -> None:
    def res(_: dict) -> str:
        return "0" * 64

    base = s.HazardFitSpec.model_validate({"features": {"path": "f"}}).model_dump(mode="json")
    assert base["zoo_shadows"] is None
    v0_1 = {k: v for k, v in base.items() if k != "zoo_shadows"}
    assert run_id(base, "0.1.0", res) == run_id(v0_1, "0.1.0", res) == "f457068ad04c7bf9"
    assert run_id({**base, "zoo_shadows": {"path": "z"}}, "0.1.0", res) != run_id(base, "0.1.0", res)
