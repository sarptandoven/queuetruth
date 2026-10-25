"""The agent's market view equals the engine's: `BookView`'s best-quote history, displayed depth and prints against
the exact replay's `bbo.parquet` (change mode), `depth.parquet` and `trades.parquet` on the same records, released
through the tape at random cuts; plus the record rules the view restates (modifies, clears, venues without modify
records, cross prints)."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import queuetruth as qt
from queuetruth.sandbox.book import BookView
from queuetruth.sandbox.tape import Tape as ScratchTape
from queuetruth.sandbox.tape import open_source
from sandbox.sbx_helpers import SYNTH, SYNTH_NASDAQ, Tape, at, px, realdata_dataset, write_dataset

pytestmark = pytest.mark.requires_core


def _engine_depth(out: Path) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    state: dict[int, dict[int, tuple]] = {ord("B"): {}, ord("A"): {}}
    for r in pq.read_table(out / "depth.parquet").to_pylist():
        state[r["side"]][r["level"]] = (r["price"], r["size"], r["count"])
    out_ = []
    for side in (ord("B"), ord("A")):
        out_.append([state[side][k] for k in sorted(state[side]) if state[side][k][0] is not None])
    return out_[0], out_[1]


def _check_against_engine(
    src_ds: Path, iid: int, date: str | None, tmp: Path, cuts: list[int], depth_at: set[int]
) -> int:
    src = open_source(src_ds, instrument_id=iid, session_date=date)
    tape = ScratchTape(src, tmp / "tape", compact_every=3)
    view = BookView(
        instrument_id=iid,
        modify_records_present=src.modify_records_present,
        supports_hidden_orders=src.supports_hidden_orders,
    )
    prints = []
    for k, t in enumerate(cuts):
        mbo, pr = tape.extend_to(t)
        view.add_cross_prints(pr)
        prints += view.apply(mbo)
        if tape.cursor.eof:
            view.end_partial_event()
        if k in depth_at and not tape.empty:
            out = tmp / f"d{k}"
            qt.replay(
                {
                    "dataset": {"path": str(tape.root)},
                    "out": str(out),
                    "bbo": "none",
                    "depth_levels": 10,
                    "trajectory": "none",
                }
            )
            assert _engine_depth(out) == view.depth(10), k
    out = tmp / "full"
    qt.replay({"dataset": {"path": str(tape.root)}, "out": str(out), "bbo": "change", "trajectory": "none"})
    bbo = pq.read_table(out / "bbo.parquet")
    assert bbo.column("ts_event").cast(pa.int64()).to_pylist() == view.h_ts
    assert bbo.column("sequence").to_pylist() == view.h_seq
    for col, mine in (
        ("bid_px", view.h_bid),
        ("bid_sz", view.h_bsz),
        ("bid_ct", view.h_bct),
        ("ask_px", view.h_ask),
        ("ask_sz", view.h_asz),
        ("ask_ct", view.h_act),
    ):
        assert bbo.column(col).to_pylist() == mine, col
    assert view.bbo_table().equals(bbo.select(view.bbo_table().column_names).cast(view.bbo_table().schema))
    tr = pq.read_table(out / "trades.parquet")
    eng = list(
        zip(
            tr.column("ts_event").cast(pa.int64()).to_pylist(),
            tr.column("price").to_pylist(),
            tr.column("size").to_pylist(),
            [chr(x) for x in tr.column("aggressor_side").to_pylist()],
            tr.column("kind").cast(pa.utf8()).to_pylist(),
            strict=True,
        )
    )
    assert eng == prints
    return len(view.h_ts)


@pytest.mark.parametrize(
    ("ds", "iid", "date"),
    [
        (SYNTH, 1, "2026-06-15"),
        (SYNTH, 2, "2026-06-16"),
        (SYNTH_NASDAQ, 1, "2026-06-15"),
        (SYNTH_NASDAQ, 2, "2026-06-16"),
    ],
)
def test_book_view_equals_the_engine_on_the_fixtures(tmp_path: Path, ds: Path, iid: int, date: str) -> None:
    src = open_source(ds, instrument_id=iid, session_date=date)
    rng = random.Random(iid * 7 + len(date))
    lo, hi = src.partition.ts_min - 1_000_000_000, src.partition.ts_max + 1_000_000_000
    cuts = [*sorted(rng.randrange(lo, hi) for _ in range(25)), hi]
    n = _check_against_engine(ds, iid, date, tmp_path, cuts, depth_at={3, 11, 19, 25})
    assert n > 5_000


@pytest.mark.realdata
@pytest.mark.integration
def test_book_view_equals_the_engine_on_tsla_day_one(tmp_path: Path) -> None:
    """The TSLA partition of the day-one Nasdaq data (678,438 records), released through the tape at 16 cuts: the
    best-quote history and the prints equal the engine's over the whole day, the top-10 depth at five of the cuts."""
    ds = realdata_dataset()
    if ds is None:
        pytest.skip("the day-one dataset is not available (set QT_REALDATA_DS)")
    src = open_source(ds, symbol="TSLA")
    p = src.partition
    cuts = [p.ts_min + (p.ts_max - p.ts_min) * k // 15 for k in range(15)] + [p.ts_max + 1]
    n = _check_against_engine(ds, p.instrument_id, p.date, tmp_path, cuts, depth_at={2, 5, 8, 11, 14})
    assert (p.rows, n) == (678_438, 126_458)


def test_book_view_record_rules(tmp_path: Path) -> None:
    """Modify to a new price and size, modify of an unknown id (an add), modify to zero, duplicate add, partial and
    oversized cancels, side-N adds ignored, a clear; on CME rules (modifies present) and Nasdaq rules (ignored)."""
    for venue in ("cme", "nasdaq", "generic"):
        t = Tape()
        a = t.add(at(1), "B", px(0), 5)
        b = t.add(at(1), "B", px(-1), 3)
        t.add(at(1), "A", px(5), 4)
        t.modify(at(2), a, px(1), 7)
        t.event([t._rec(at(3), "M", "A", px(6), 2, 777)])  # unknown id
        t.resting[777] = ("A", px(6), 2)
        t.modify(at(4), b, px(-1), 1)  # priority-keeping reduction
        t.event([t._rec(at(5), "A", "B", px(0), 9, a)])  # duplicate add of a live id
        t.resting[a] = ("B", px(0), 9)
        t.cancel(at(6), a, 4)
        t.event([t._rec(at(7), "C", "B", px(-1), 99, b)])  # cancel larger than resting
        t.event([t._rec(at(8), "A", "N", px(0), 5, 888)])  # side N add: ignored
        t.modify(at(9), 777, px(6), 0)
        t.event([t._rec(at(9.5), "C", "B", px(0), 1, 4242)])  # unknown id
        t.noop(at(10))
        t.clear(at(11))
        t.add(at(12), "A", px(9), 1)
        t.noop(at(13))
        ds = write_dataset(tmp_path / venue, t, venue=venue)
        cuts = [at(x) for x in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.7, 10.5, 11.5, 12.5, 20)]
        _check_against_engine(ds, 1, None, tmp_path / f"run-{venue}", cuts, depth_at=set(range(len(cuts))))


def test_cross_prints_and_hidden_kinds(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "A", px(5), 5)
    t.hidden_print(at(2), px(2), 3)
    t.hidden_print(at(3), px(2), 4, cross=True)
    t.noop(at(4))
    for venue, kinds in (("nasdaq", ["hidden", "cross"]), ("cme", ["displayed", "cross"])):
        ds = write_dataset(tmp_path / venue, t, venue=venue)
        src = open_source(ds, instrument_id=1)
        tape = ScratchTape(src, tmp_path / f"tape-{venue}")
        view = BookView(
            instrument_id=1,
            modify_records_present=src.modify_records_present,
            supports_hidden_orders=src.supports_hidden_orders,
        )
        mbo, pr = tape.extend_to(at(5))
        view.add_cross_prints(pr)
        assert [p[4] for p in view.apply(mbo)] == kinds


def test_queries(tmp_path: Path) -> None:
    t = Tape()
    t.add(at(1), "B", px(0), 5)
    t.add(at(1), "B", px(0), 2)
    t.add(at(1), "B", px(-3), 1)
    t.add(at(2), "A", px(4), 6)
    t.noop(at(3))
    ds = write_dataset(tmp_path / "ds", t)
    src = open_source(ds, instrument_id=1)
    tape = ScratchTape(src, tmp_path / "tape")
    view = BookView(instrument_id=1, modify_records_present=True, supports_hidden_orders=False)
    view.apply(tape.extend_to(at(1.5))[0])
    assert view.best() == ((px(0), 7, 2), None)
    assert view.mid_x2(view.quote_at(at(1.5))) is None  # one-sided
    view.apply(tape.extend_to(at(5))[0])
    assert view.best() == ((px(0), 7, 2), (px(4), 6, 1))
    assert view.depth(5) == ([(px(0), 7, 2), (px(-3), 1, 1)], [(px(4), 6, 1)])
    assert view.level_qty("buy", px(0)) == 7 and view.level_qty("sell", px(0)) == 0
    i = view.quote_at(at(2))
    assert view.h_ts[i] == at(2) and view.mid_x2(i) == px(0) + px(4)
    assert view.quote_before(at(2)) == i - 1 and view.quote_at(at(0)) == -1
    assert view.records == 5 and view.events == 5


def test_heap_rebuild_keeps_best_prices(tmp_path: Path) -> None:
    """Thousands of levels created and emptied: the lazy heaps are rebuilt and the best quotes stay right."""
    t = Tape()
    t.add(at(1), "A", px(500), 1)
    rng = np.random.default_rng(0)
    ids = []
    for k in range(3000):
        ts = at(2 + k * 0.001)
        if ids and rng.random() < 0.45:
            t.cancel(ts, ids.pop(int(rng.integers(len(ids)))))
        else:
            ids.append(t.add(ts, "B", px(int(rng.integers(-400, 0))), 1))
    t.noop(at(10))
    ds = write_dataset(tmp_path / "ds", t)
    _check_against_engine(ds, 1, None, tmp_path / "run", [at(3), at(4), at(20)], depth_at={0, 1, 2})
