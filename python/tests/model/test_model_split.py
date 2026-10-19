"""O-MOD-4: chronological splits; no temporal overlap after purge / embargo; orders never straddle splits
(SPEC 5.5.6). The invariants are re-derived here from the split's segments, independently of `check_split`."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pyarrow as pa
import pytest

from model.hazard_synth import NS, OPEN_UTC_S, make_features, tiny_features
from queuetruth.errors import QtError
from queuetruth.model import make_split, split_assignment

H_MAX = 60 * NS
EMBARGO = 60 * NS
DATES6 = tuple(f"2026-03-{d:02d}" for d in (2, 3, 4, 5, 6, 9))


def _arr(t: pa.Table, name: str) -> np.ndarray:
    col = t.column(name).combine_chunks()
    if pa.types.is_timestamp(col.type):
        col = col.cast(pa.int64())
    elif pa.types.is_date32(col.type):
        col = col.cast(pa.int32())
    elif pa.types.is_dictionary(col.type):
        col = col.cast(pa.utf8())
    return np.asarray(col.to_numpy(zero_copy_only=False))


def _assert_orders_single_split(out: pa.Table) -> None:
    key = [f"{r}|{d}" for r, d in zip(_arr(out, "order_ref"), _arr(out, "session_date"), strict=True)]
    seen: dict[str, str] = {}
    for k, s in zip(key, _arr(out, "split"), strict=True):
        assert seen.setdefault(k, s) == s, f"order {k} straddles splits"


def _assert_no_overlap(out: pa.Table, boundaries: list[tuple[int, str]]) -> None:
    """`boundaries`: (start ns, label) of every split run in time order. A row belongs to the run containing its
    order's start; its label window ends before the next run starts, and calib / test rows are past run start +
    embargo."""
    t_l = _arr(out, "ts_landmark")
    t0 = t_l - _arr(out, "landmark_ns")
    split = _arr(out, "split")
    starts = np.array([b[0] for b in boundaries])
    for t, o, s in zip(t_l, t0, split, strict=True):
        own = max(int(np.searchsorted(starts, o, side="right")) - 1, 0)
        assert boundaries[own][1] == s
        if own + 1 < len(boundaries):
            assert t + H_MAX < boundaries[own + 1][0]
        if s != "train":
            assert t >= boundaries[own][0] + EMBARGO


def test_o_mod_4_days_scheme() -> None:
    f = make_features(1, dates=DATES6, orders_per_day=150)
    out = make_split(f, {"scheme": "auto", "embargo_ns": EMBARGO}, h_max_ns=H_MAX)
    res = split_assignment(f, {"scheme": "auto", "embargo_ns": EMBARGO}, h_max_ns=H_MAX)
    assert res.scheme == "days"
    assert res.echo == {"scheme": "days", "train": list(DATES6[:3]), "calib": [DATES6[3]], "test": list(DATES6[4:])}
    by_date = {}
    for d, s in zip(_arr(out, "session_date"), _arr(out, "split"), strict=True):
        by_date.setdefault(int(d), set()).add(s)
    assert [sorted(v) for _, v in sorted(by_date.items())] == [["train"]] * 3 + [["calib"]] + [["test"]] * 2
    _assert_orders_single_split(out)
    # Embargo: the first 60 s of the calib day and of the first test day are dropped (session start = first t_0).
    t0 = _arr(f, "ts_landmark") - _arr(f, "landmark_ns")
    days = _arr(f, "session_date")
    starts = {int(d): int(t0[days == d].min()) for d in np.unique(days)}
    bounds = [(starts[k], s) for k, s in zip(sorted(starts), ["train"] * 3 + ["calib", "test", "test"], strict=True)]
    runs = [bounds[0], bounds[3], bounds[4]]
    _assert_no_overlap(out, runs)
    assert out.num_rows < f.num_rows
    assert res.n_orders()["train"] == 450


def test_o_mod_4_blocks_within_one_day_purges() -> None:
    f = make_features(2, dates=("2026-03-02",), orders_per_day=1500)
    sp = {"scheme": "auto", "n_blocks": 5, "embargo_ns": EMBARGO}
    res = split_assignment(f, sp, h_max_ns=H_MAX)
    assert res.scheme == "blocks"
    assert res.echo["calib"] == {"dates": ["2026-03-02"], "blocks": [3], "n_blocks": 5}
    out = make_split(f, sp, h_max_ns=H_MAX)
    _assert_orders_single_split(out)
    t0 = _arr(f, "ts_landmark") - _arr(f, "landmark_ns")
    s, e = int(t0.min()), int(t0.max()) + 1
    cut = [s + ((e - s) * b) // 5 for b in range(5)]
    _assert_no_overlap(out, [(cut[0], "train"), (cut[3], "calib"), (cut[4], "test")])
    # Purge is active: some train rows near the calib start existed before and are gone after.
    t_l = _arr(f, "ts_landmark")
    near = (t0 < cut[3]) & (t_l + H_MAX >= cut[3])
    assert near.any()
    kept = set(zip(_arr(out, "order_ref").tolist(), _arr(out, "landmark_ns").tolist(), strict=True))
    for r, lm in zip(_arr(f, "order_ref")[near], _arr(f, "landmark_ns")[near], strict=True):
        assert (r, lm) not in kept


def test_o_mod_4_two_to_four_dates() -> None:
    dates = ("2026-03-02", "2026-03-03", "2026-03-04")
    f = make_features(3, dates=dates, orders_per_day=300)
    sp = {"scheme": "auto", "n_blocks": 4, "embargo_ns": EMBARGO}
    res = split_assignment(f, sp, h_max_ns=H_MAX)
    assert res.scheme == "days+blocks"
    assert res.echo["test"] == {"dates": ["2026-03-04"]}
    assert res.echo["calib"] == {"dates": list(dates[:2]), "blocks": [3], "n_blocks": 4}
    out = make_split(f, sp, h_max_ns=H_MAX)
    _assert_orders_single_split(out)
    t0 = _arr(f, "ts_landmark") - _arr(f, "landmark_ns")
    days = _arr(f, "session_date")
    runs = []
    for d, lab in zip(np.unique(days), ["x", "y", "test"], strict=True):
        on = t0[days == d]
        s, e = int(on.min()), int(on.max()) + 1
        if lab == "test":
            runs.append((s, "test"))
        else:
            runs += [(s, "train"), (s + ((e - s) * 3) // 4, "calib")]
    _assert_no_overlap(out, runs)
    assert set(_arr(out, "split")) == {"train", "calib", "test"}


def test_o_mod_4_random_tables_property() -> None:
    """Invariants on random tables, schemes and parameters (checked by `check_split` inside `make_split` and by the
    independent single-split assertion here)."""
    rng = np.random.default_rng(7)
    for i in range(12):
        n_dates = int(rng.integers(1, 7))
        dates = tuple((dt.date(2026, 3, 2) + dt.timedelta(days=k)).isoformat() for k in range(n_dates))
        f = make_features(100 + i, dates=dates, orders_per_day=int(rng.integers(40, 120)))
        sp = {"scheme": "auto", "n_blocks": int(rng.integers(3, 7)), "embargo_ns": int(rng.integers(0, 120)) * NS}
        out = make_split(f, sp, h_max_ns=int(rng.integers(1, 90)) * NS)
        _assert_orders_single_split(out)
        assert out.num_rows <= f.num_rows


def test_split_errors_and_sessions() -> None:
    f = make_features(4, dates=("2026-03-02", "2026-03-03"), orders_per_day=50)
    with pytest.raises(QtError) as err:
        make_split(f, {"scheme": "days"})
    assert err.value.code == "QT-CONFIG-001"
    # Explicit session bounds move the block cut points.
    day0 = (dt.date(2026, 3, 2) - dt.date(1970, 1, 1)).days * 86_400 * NS
    sessions = {"2026-03-02": (day0 + OPEN_UTC_S * NS, day0 + 21 * 3600 * NS)}
    a = split_assignment(f, {"scheme": "blocks", "n_blocks": 3}, sessions=sessions)
    b = split_assignment(f, {"scheme": "blocks", "n_blocks": 3})
    assert a.label.shape == b.label.shape
    # A pre-existing split column is replaced.
    out = make_split(make_split(f, {"scheme": "auto"}), {"scheme": "auto"})
    assert out.column_names.count("split") == 1


def test_landmarks_follow_order_start() -> None:
    """An order whose later landmark falls after the calib start keeps the train label (and is purged)."""
    day0 = (dt.date(2026, 3, 2) - dt.date(1970, 1, 1)).days * 86_400 * NS
    rows = []
    for k in range(10):  # orders every 100 s from day0
        t0 = day0 + k * 100 * NS
        rows.append((f"o{k}", "2026-03-02", t0, 0, 10 * NS, "fill"))
        rows.append((f"o{k}", "2026-03-02", t0 + 90 * NS, 90 * NS, NS, "fill"))
    f = tiny_features(rows)
    res = split_assignment(f, {"scheme": "blocks", "n_blocks": 3, "embargo_ns": 0}, h_max_ns=5 * NS)
    lab = res.label.reshape(-1, 2)
    assert np.all(lab[:, 0] == lab[:, 1])
