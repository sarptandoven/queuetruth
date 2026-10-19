"""API contract details: `export` passes `force` straight through to `run_export` (no signature probing, no "never
refuses" warning), and bare venue order ids in a spec dict's `exclude_order_ids` (replay, zoo, why) expand to every
instrument of the dataset with `resolve_order_ids`, exactly like the CLI's `--exclude-order-ids`."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest

from queuetruth import api
from queuetruth.errors import QtError
from queuetruth.schemas import models as m
from queuetruth.schemas import specs as s


@pytest.mark.parametrize("force", [False, True])
def test_export_passes_force_straight_through(monkeypatch: pytest.MonkeyPatch, force: bool) -> None:
    import queuetruth.export as qexp

    seen: list[bool] = []

    def run_export(spec: Any, *, force: bool) -> m.ExportSummary:  # keyword without a default: always passed
        seen.append(force)
        return m.ExportSummary(target="hftbacktest", files=[], text="x")

    monkeypatch.setattr(qexp, "run_export", run_export)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        api.export({"target": "hftbacktest", "calibration": {"path": "c"}}, force=force)
    assert seen == [force]


def test_bare_ids_need_a_local_dataset() -> None:
    with pytest.raises(QtError) as e:
        api._expand_order_ids({"dataset": {"dataset_id": "ds_1"}, "exclude_order_ids": [7]})
    assert e.value.code == "QT-CONFIG-001"
    keep = {"dataset": {"path": "d"}, "exclude_order_ids": [{"instrument_id": 1, "order_id": 7}]}
    assert api._expand_order_ids(keep) is keep  # nothing instrument-less: untouched
    assert api._expand_order_ids("not a dict") == "not a dict"


@pytest.mark.requires_core
def test_bare_ids_expand_to_every_instrument(repo_root: Path) -> None:
    ds = repo_root / "tests" / "fixtures" / "synth_small"
    want = api.resolve_order_ids([123, "456"], ds)
    instruments = sorted({it["instrument_id"] for it in want})
    assert len(want) == 2 * len(instruments) >= 2
    base = {"dataset": {"path": str(ds)}, "shadows": {"path": "sh"}}
    for model, extra in ((s.ReplaySpec, {}), (s.ZooSpec, {}), (s.WhySpec, {"shadow_id": "x"})):
        doc = {**base, **extra, "exclude_order_ids": [123, "456"]}
        got = api._expand_order_ids(doc)
        assert got["exclude_order_ids"] == want, model.__name__
        spec = model.model_validate(got)  # the expanded list is a valid spec field
        assert [x.order_id for x in spec.exclude_order_ids] == [it["order_id"] for it in want]
    # Mixed lists and objects without an instrument expand the same way; session dates are kept.
    mixed = api._expand_order_ids(
        {
            **base,
            "exclude_order_ids": [{"order_id": 123, "session_date": "2019-12-30"}, {"instrument_id": 1, "order_id": 9}],
        }
    )["exclude_order_ids"]
    assert {"instrument_id": 1, "order_id": 9} in mixed
    assert sum(1 for it in mixed if it.get("session_date") == "2019-12-30") == len(instruments)
