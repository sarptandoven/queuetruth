"""O-MOD-1: person-period expansion counts and targets (SPEC 5.5.3)."""

from __future__ import annotations

import math

import numpy as np
import pyarrow as pa
import pytest

from model.hazard_synth import NS, tiny_features
from queuetruth.errors import QtError
from queuetruth.model import expand_person_period

GRID = [1 * NS, 2 * NS, 5 * NS]
DAY0 = 20_514 * 86_400 * NS  # 2026-03-02 00:00 UTC


def _rows(spec: list[tuple[int, str]]) -> pa.Table:
    rows = [(f"o{i}", "2026-03-02", DAY0 + i * NS, 0, e, c) for i, (e, c) in enumerate(spec)]
    return tiny_features(rows, feature=[float(i) for i in range(len(spec))])


def _pairs(pp: pa.Table) -> list[tuple[int, int, int]]:
    return list(
        zip(
            pp.column("row_id").to_pylist(),
            pp.column("interval_idx").to_pylist(),
            pp.column("target").to_pylist(),
            strict=True,
        )
    )


def test_o_mod_1_five_row_example() -> None:
    """Five rows: fill at 0, cancel inside interval 2, censor inside 3, fill beyond the grid, censor at tau_2."""
    t = _rows([(0, "fill"), (1_500_000_000, "cancel"), (3 * NS, "censor"), (10 * NS, "fill"), (2 * NS, "censor")])
    pp = expand_person_period(t, GRID)
    assert _pairs(pp) == [
        (0, 1, 1),
        (1, 1, 0),
        (1, 2, 2),
        (2, 1, 0),
        (2, 2, 0),
        (3, 1, 0),
        (3, 2, 0),
        (3, 3, 0),
        (4, 1, 0),
        (4, 2, 0),
    ]
    assert pp.num_rows == 10
    counts = np.bincount(pp.column("target").to_numpy(), minlength=3)
    assert counts.tolist() == [8, 1, 1]
    lte = pp.column("log_tau_end").to_pylist()
    assert lte == [math.log(t_ns / 1e9) for t_ns in [1e9, 1e9, 2e9, 1e9, 2e9, 1e9, 2e9, 5e9, 1e9, 2e9]]
    # Features and fixed columns are carried from the source row.
    assert pp.column("x").to_pylist() == [0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 3.0, 4.0, 4.0]
    assert pp.column("order_ref").to_pylist()[2] == "o1"
    assert pp.column_names[-4:] == ["row_id", "interval_idx", "log_tau_end", "target"]


def test_o_mod_1_boundaries() -> None:
    """Upper interval bounds are inclusive for events; a censoring before tau_1 contributes nothing; a censoring
    exactly at tau_J (the h_max cap) survives every interval."""
    t = _rows([(1 * NS, "fill"), (5 * NS, "cancel"), (500_000_000, "censor"), (5 * NS, "censor"), (1, "cancel")])
    assert _pairs(expand_person_period(t, GRID)) == [
        (0, 1, 1),
        (1, 1, 0),
        (1, 2, 0),
        (1, 3, 2),
        (3, 1, 0),
        (3, 2, 0),
        (3, 3, 0),
        (4, 1, 2),
    ]


def test_o_mod_1_rejects_bad_grid_and_negative_times() -> None:
    t = _rows([(NS, "fill")])
    with pytest.raises(QtError) as err:
        expand_person_period(t, [2 * NS, NS])
    assert err.value.code == "QT-CONFIG-001"
    with pytest.raises(QtError) as err:
        expand_person_period(_rows([(-1, "fill")]), GRID)
    assert err.value.code == "QT-INPUT-002"
