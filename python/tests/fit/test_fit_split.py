"""`queuetruth.fit.split`: `--split 70%` and `--test-after HH:MM` turned into the train and test windows the
calibrations take, cut where the data are (a short capture has orders on both sides), and the expert windows."""

from __future__ import annotations

from pathlib import Path

import pytest
from score.qt_score_helpers import DAY, MIDNIGHT, S, make_dataset

from queuetruth.errors import QtError
from queuetruth.fit import split as sp
from queuetruth.score._dataset import DatasetView

MIN = 60 * S


@pytest.fixture
def ds(tmp_path: Path) -> DatasetView:
    """Three days of one instrument whose session is the whole UTC day (open 00:00, close 24:00)."""
    return DatasetView(make_dataset(tmp_path / "ds", [], n_days=3))


def _capture(day: int, start_min: int, minutes: int, every_s: int = 10) -> list[sp.Order]:
    t0 = MIDNIGHT + day * DAY + start_min * MIN
    return [sp.Order(1, t0 + i * every_s * S) for i in range(minutes * 60 // every_s)]


def test_share_and_clock_parsing() -> None:
    assert sp.parse_share("70%") == pytest.approx(0.7)
    assert sp.parse_share("70") == pytest.approx(0.7)
    assert sp.parse_share("0.6") == pytest.approx(0.6)
    for bad in ("0%", "100%", "abc", "-5"):
        with pytest.raises(QtError, match="--split"):
            sp.parse_share(bad)
    assert sp.parse_clock("09:42") == 9 * 3600 + 42 * 60
    assert sp.parse_clock("9:42:30") == 9 * 3600 + 42 * 60 + 30
    assert sp.clock(34920) == "09:42:00"
    for bad in ("24:00", "09:60", "0942", "9h42"):
        with pytest.raises(QtError, match="--test-after"):
            sp.parse_clock(bad)


def test_window_arg_dates_and_blocks() -> None:
    assert sp.window_arg("2026-06-15,2026-06-16", "--train").dates == ["2026-06-15", "2026-06-16"]
    w = sp.window_arg("blocks:0-3/5", "--train")
    assert w.blocks is not None and w.blocks.index == [0, 1, 2, 3] and w.blocks.n_blocks == 5
    assert sp.window_arg("blocks:1,3/4", "--test").blocks.index == [1, 3]  # type: ignore[union-attr]
    for bad in ("blocks:5/5", "blocks:0-3", "June 15", "blocks:a/5"):
        with pytest.raises(QtError, match="expected"):
            sp.window_arg(bad, "--train")


def test_split_by_days_with_two_or_more_dates(ds: DatasetView) -> None:
    orders = _capture(0, 600, 30) + _capture(1, 600, 30) + _capture(2, 600, 30)
    s = sp.plan(ds, orders, split="70%")
    assert s.train.dates == ["2026-01-01", "2026-01-02"] and s.test.dates == ["2026-01-03"]
    assert (s.n_train, s.n_test) == (360, 180)
    assert s.how == "the first 70% of the 3 days"
    # one day is never all of train or all of test
    assert sp.plan(ds, orders, split="99%").test.dates == ["2026-01-03"]
    assert sp.plan(ds, orders, split="1%").train.dates == ["2026-01-01"]


def test_short_capture_is_cut_where_the_data_are(ds: DatasetView) -> None:
    """15 minutes of orders on one day: blocks:4/5 (the last fifth of the session) holds none of them, the 70%
    split puts the last 30% of the capture in the test window."""
    orders = _capture(0, 600, 15)
    fifths = sp.given(ds, orders, sp.window_arg("blocks:0-3/5", "--train"), sp.window_arg("blocks:4/5", "--test"))
    assert fifths.n_test == 0
    s = sp.plan(ds, orders, split="70%")
    assert s.train.blocks is not None and s.test.blocks is not None
    assert s.n_train + s.n_test == len(orders) == 90
    assert 60 <= s.n_train <= 66 and s.n_test >= 24
    assert s.train_text == "before 10:10:30" and s.test_text == "from 10:10:30"
    # the windows select the same orders as the calibrations will (block of the continuous session)
    assert sum(sp.inside(ds, o, s.train) for o in orders) == s.n_train
    assert sum(sp.inside(ds, o, s.test) for o in orders) == s.n_test
    assert not any(sp.inside(ds, o, s.train) and sp.inside(ds, o, s.test) for o in orders)


def test_test_after_is_a_venue_local_time_on_every_day(ds: DatasetView) -> None:
    orders = _capture(0, 600, 60) + _capture(1, 600, 60)  # 10:00 to 11:00 on two days
    s = sp.plan(ds, orders, test_after="10:42")
    assert s.test_text == "from 10:42:00" and s.how == "held out after 10:42:00 venue time"
    assert (s.n_train, s.n_test) == (2 * 42 * 6, 2 * 18 * 6)
    assert s.train.dates is None and s.test.dates is None
    with pytest.raises(QtError, match="outside the trading session"):
        sp.plan(ds, orders, test_after="00:00")
    with pytest.raises(QtError, match="not both"):
        sp.plan(ds, orders, split="70%", test_after="10:42")


def test_time_split_needs_one_session(tmp_path: Path) -> None:
    from queuetruth.schemas.io import write_table
    from queuetruth.score._io import make_table, read_parquet, rows

    root = make_dataset(tmp_path / "ds", [], n_days=1)
    inst = rows(read_parquet(root / "instruments.parquet"))
    inst.append({**inst[0], "instrument_id": 2, "open_s": 3600})
    write_table(make_table("qt.instruments", inst), root / "instruments.parquet")
    orders = [sp.Order(1, MIDNIGHT + 7200 * S), sp.Order(2, MIDNIGHT + 9000 * S)]
    with pytest.raises(QtError, match="one trading session"):
        sp.plan(DatasetView(root), orders, split="50%")
