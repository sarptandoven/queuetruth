"""Chronological train / calib / test splits with purge and embargo (SPEC 5.5.6).

Assignment is per order (key `(order_ref, session_date)`; all landmarks follow the split of the order's start
`t_0 = ts_landmark - landmark_ns`). Schemes: `days` (first `floor(0.6 D)` dates train, next `max(1, floor(0.2 D))`
calib, rest test; needs `D >= 3`), `blocks` (per date, `n_blocks` equal-duration blocks of the session: blocks
`0..n-3` train, `n-2` calib, `n-1` test), and `auto` (`D >= 5` days; `2 <= D <= 4` the last date is test, the last
block of every earlier date is calib, the other blocks train; `D = 1` blocks). The session of a date is
`sessions[date]` when given, else `[min t_0, max t_0 + 1)` over that date's orders.

The time axis is cut into segments (a date or a block) carrying a split label; consecutive segments with the same
label form a run. Purge drops a row whose `[t_l, t_l + h_max]` reaches the start of the next run; embargo drops calib
and test rows with `t_l < run start + embargo_ns`. `check_split` re-verifies both on the kept rows.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa

from queuetruth.errors import QtError
from queuetruth.model._common import config_error, int64_column, require_columns
from queuetruth.schemas.specs import SplitSpec

SPLIT_NAMES = ("train", "calib", "test")
TRAIN, CALIB, TEST = 0, 1, 2
DEFAULT_H_MAX_NS = 60_000_000_000
SPLIT_TYPE = pa.dictionary(pa.int8(), pa.utf8())
_EPOCH = dt.date(1970, 1, 1)


def date_str(days: int) -> str:
    """Days since the Unix epoch -> `YYYY-MM-DD`."""
    return (_EPOCH + dt.timedelta(days=int(days))).isoformat()


@dataclass
class SplitResult:
    """Row-level split assignment before dropping: label per row (0 train, 1 calib, 2 test), kept mask, echo."""

    scheme: str
    label: np.ndarray
    keep: np.ndarray
    order_index: np.ndarray
    order_t0: np.ndarray
    run_start: np.ndarray
    next_run_start: np.ndarray
    h_max_ns: int
    embargo_ns: int
    echo: dict[str, Any] = field(default_factory=dict)

    def n_orders(self) -> dict[str, int]:
        """Distinct orders per split among kept rows."""
        out = {}
        for k, name in enumerate(SPLIT_NAMES):
            m = self.keep & (self.label == k)
            out[name] = int(np.unique(self.order_index[m]).size)
        return out


def _order_index(table: pa.Table, days: np.ndarray) -> np.ndarray:
    refs = table.column("order_ref").combine_chunks()
    if refs.null_count:
        raise QtError("QT-INPUT-002", "features: order_ref has null values")
    _, ref_inv = np.unique(np.asarray(refs.to_numpy(zero_copy_only=False)).astype(str), return_inverse=True)
    _, day_inv = np.unique(days, return_inverse=True)
    combo = ref_inv.reshape(-1).astype(np.int64) * (int(day_inv.max(initial=0)) + 1) + day_inv.reshape(-1)
    _, inv = np.unique(combo, return_inverse=True)
    return inv.reshape(-1).astype(np.int64)


def split_assignment(
    features: pa.Table,
    split: SplitSpec | dict[str, Any] | None = None,
    *,
    h_max_ns: int | None = None,
    sessions: dict[str, tuple[int, int]] | None = None,
) -> SplitResult:
    """Computes the split of every row (see the module docstring) without dropping anything."""
    sp = split if isinstance(split, SplitSpec) else SplitSpec.model_validate(split or {})
    require_columns(features, ("order_ref", "session_date", "ts_landmark", "landmark_ns"), "make_split")
    n_rows = features.num_rows
    days = int64_column(features, "session_date")
    t_l = int64_column(features, "ts_landmark")
    t0_row = t_l - int64_column(features, "landmark_ns")
    if h_max_ns is None:
        e_max = int(int64_column(features, "event_ns").max(initial=0)) if "event_ns" in features.column_names else 0
        h_max_ns = max(DEFAULT_H_MAX_NS, e_max)
    if n_rows == 0:
        empty = np.zeros(0, dtype=np.int64)
        return SplitResult(
            sp.scheme,
            empty.astype(np.int8),
            empty.astype(bool),
            empty,
            empty,
            empty,
            empty,
            int(h_max_ns),
            sp.embargo_ns,
            {"scheme": sp.scheme, "train": [], "calib": [], "test": []},
        )
    order = _order_index(features, days)
    n_orders = int(order.max()) + 1
    o_t0 = np.full(n_orders, np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(o_t0, order, t0_row)
    o_day = np.zeros(n_orders, dtype=np.int64)
    o_day[order] = days
    uniq_days = np.unique(o_day)
    n_days = len(uniq_days)

    scheme = sp.scheme
    if scheme == "auto":
        scheme = "days" if n_days >= 5 else ("days+blocks" if n_days >= 2 else "blocks")
    elif scheme == "days" and n_days < 3:
        raise config_error(f"split.scheme 'days' needs at least 3 session dates, got {n_days}", field="split.scheme")
    n_blocks = sp.n_blocks

    bounds: dict[int, tuple[int, int]] = {}
    for d in uniq_days:
        key = date_str(int(d))
        if sessions is not None and key in sessions:
            s, e = (int(x) for x in sessions[key])
        else:
            on = o_t0[o_day == d]
            s, e = int(on.min()), int(on.max()) + 1
        if e <= s:
            raise config_error(f"session bounds for {key} are empty", field="sessions")
        bounds[int(d)] = (s, e)

    # Segments: (start, label, date, block); orders map to a segment.
    seg_start: list[int] = []
    seg_label: list[int] = []
    o_seg = np.zeros(n_orders, dtype=np.int64)
    echo: dict[str, Any]
    if scheme == "days":
        n_train = (6 * n_days) // 10
        n_calib = max(1, (2 * n_days) // 10)
        for i, d in enumerate(uniq_days):
            lab = TRAIN if i < n_train else (CALIB if i < n_train + n_calib else TEST)
            o_seg[o_day == d] = len(seg_start)
            seg_start.append(bounds[int(d)][0])
            seg_label.append(lab)
        ds = [date_str(int(d)) for d in uniq_days]
        echo = {
            "scheme": "days",
            "train": ds[:n_train],
            "calib": ds[n_train : n_train + n_calib],
            "test": ds[n_train + n_calib :],
        }
    else:
        test_day = int(uniq_days[-1]) if scheme == "days+blocks" else None
        for d in uniq_days:
            d = int(d)
            on = o_day == d
            s, e = bounds[d]
            if d == test_day:
                o_seg[on] = len(seg_start)
                seg_start.append(s)
                seg_label.append(TEST)
                continue
            blk = np.clip(((o_t0[on] - s) * n_blocks) // (e - s), 0, n_blocks - 1)
            o_seg[on] = len(seg_start) + blk
            for b in range(n_blocks):
                seg_start.append(s + ((e - s) * b) // n_blocks)
                if scheme == "blocks":
                    seg_label.append(TRAIN if b <= n_blocks - 3 else (CALIB if b == n_blocks - 2 else TEST))
                else:
                    seg_label.append(CALIB if b == n_blocks - 1 else TRAIN)
        if scheme == "blocks":
            ds = [date_str(int(d)) for d in uniq_days]
            echo = {
                "scheme": "blocks",
                "train": {"dates": ds, "blocks": list(range(n_blocks - 2)), "n_blocks": n_blocks},
                "calib": {"dates": ds, "blocks": [n_blocks - 2], "n_blocks": n_blocks},
                "test": {"dates": ds, "blocks": [n_blocks - 1], "n_blocks": n_blocks},
            }
        else:
            ds = [date_str(int(d)) for d in uniq_days[:-1]]
            echo = {
                "scheme": "days+blocks",
                "train": {"dates": ds, "blocks": list(range(n_blocks - 1)), "n_blocks": n_blocks},
                "calib": {"dates": ds, "blocks": [n_blocks - 1], "n_blocks": n_blocks},
                "test": {"dates": [date_str(int(uniq_days[-1]))]},
            }

    starts = np.asarray(seg_start, dtype=np.int64)
    labels = np.asarray(seg_label, dtype=np.int8)
    by_time = np.argsort(starts, kind="stable")
    seg_run_start = np.zeros(len(starts), dtype=np.int64)
    seg_next = np.full(len(starts), np.iinfo(np.int64).max, dtype=np.int64)
    run_first = 0
    for pos in range(1, len(by_time) + 1):
        if pos == len(by_time) or labels[by_time[pos]] != labels[by_time[run_first]]:
            nxt = int(starts[by_time[pos]]) if pos < len(by_time) else np.iinfo(np.int64).max
            members = by_time[run_first:pos]
            seg_run_start[members] = starts[by_time[run_first]]
            seg_next[members] = nxt
            run_first = pos

    row_seg = o_seg[order]
    label = labels[row_seg]
    run_start = seg_run_start[row_seg]
    next_start = seg_next[row_seg]
    keep = _keep_mask(t_l, label, run_start, next_start, int(h_max_ns), sp.embargo_ns)
    return SplitResult(scheme, label, keep, order, o_t0, run_start, next_start, int(h_max_ns), sp.embargo_ns, echo)


def _keep_mask(t_l, label, run_start, next_start, h_max_ns: int, embargo_ns: int) -> np.ndarray:
    big = np.iinfo(np.int64).max
    purge = (next_start != big) & (t_l >= next_start - h_max_ns)
    embargo = (label != TRAIN) & (t_l < run_start + embargo_ns)
    return ~(purge | embargo)


def check_split(features: pa.Table, result: SplitResult) -> None:
    """Re-verifies on the kept rows: one split per order, no label window reaching the next run, embargo respected.
    Raises `QT-ENGINE-001` on a violation."""
    t_l = int64_column(features, "ts_landmark")
    k = result.keep
    lab, order = result.label[k], result.order_index[k]
    if lab.size:
        per_order_min = np.full(int(order.max()) + 1, 9, dtype=np.int64)
        per_order_max = np.full(int(order.max()) + 1, -1, dtype=np.int64)
        np.minimum.at(per_order_min, order, lab.astype(np.int64))
        np.maximum.at(per_order_max, order, lab.astype(np.int64))
        seen = per_order_max >= 0
        if np.any(per_order_min[seen] != per_order_max[seen]):
            raise QtError("QT-ENGINE-001", "make_split: an order straddles splits")
    big = np.iinfo(np.int64).max
    nxt = result.next_run_start[k]
    reach = (nxt != big) & (t_l[k] + result.h_max_ns >= nxt)
    if np.any(reach):
        raise QtError("QT-ENGINE-001", "make_split: a kept row's label window reaches the next split")
    emb = (lab != TRAIN) & (t_l[k] < result.run_start[k] + result.embargo_ns)
    if np.any(emb):
        raise QtError("QT-ENGINE-001", "make_split: a kept calib/test row lies inside the embargo")


def split_column(label: np.ndarray) -> pa.DictionaryArray:
    """`split` as `dictionary<int8, utf8>` with values train, calib, test."""
    return pa.DictionaryArray.from_arrays(pa.array(label.astype(np.int8), type=pa.int8()), pa.array(SPLIT_NAMES))


def make_split(
    features: pa.Table,
    split: dict[str, Any] | SplitSpec | None = None,
    *,
    h_max_ns: int | None = None,
    sessions: dict[str, tuple[int, int]] | None = None,
) -> pa.Table:
    """Adds the `split` column and drops purged / embargoed rows (SPEC 5.5.6). `h_max_ns` defaults to
    `max(60 s, max event_ns)`; `sessions` maps `YYYY-MM-DD` to `(start_ns, end_ns)` session bounds."""
    res = split_assignment(features, split, h_max_ns=h_max_ns, sessions=sessions)
    check_split(features, res)
    tbl = features.drop_columns(["split"]) if "split" in features.column_names else features
    tbl = tbl.append_column("split", split_column(res.label))
    return tbl.filter(pa.array(res.keep))


__all__ = ["SPLIT_NAMES", "SplitResult", "check_split", "make_split", "split_assignment", "split_column"]
