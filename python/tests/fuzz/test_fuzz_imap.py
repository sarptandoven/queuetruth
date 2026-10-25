"""Fuzz properties of the instrument-map CSV (`queuetruth.live.imap`, SPEC 3.9), read by every import that names
one: structure-aware rows with one hostile cell, mutated fixture maps, and a venue import that uses the map."""

from __future__ import annotations

import csv
import io
import warnings
from decimal import Decimal
from typing import Any

from fuzz._harness import FUZZ, guarded, workdir, write_files
from fuzz._mutate import LIVE_FIXTURES, mutated, seeds
from fuzz._values import EDGE_DECIMALS, one_bad_value
from hypothesis import given
from hypothesis import strategies as st

from queuetruth.live import import_venue
from queuetruth.live.imap import REQUIRED, load_instrument_map

COLUMNS = [*REQUIRED[:3], "session_date", *REQUIRED[3:]]


@st.composite
def _row(draw: st.DrawFn) -> dict[str, str]:
    """A valid map row: tick and lot sizes exact at the row's exponents."""
    pe, se = draw(st.integers(-12, 0)), draw(st.integers(-18, 18))
    return {
        "venue": draw(st.sampled_from(["deribit", "DERIBIT", "fix44", "binance"])),
        "instrument": draw(st.sampled_from(["BTC-PERPETUAL", "ACME", "BTCUSDT"])),
        "instrument_id": str(draw(st.integers(0, 2**32 - 1))),
        "session_date": draw(st.sampled_from(["", "2026-10-15", "2026-10-16"])),
        "price_exp": str(pe),
        "size_exp": str(se),
        "tick_size": str(Decimal(draw(st.integers(1, 10**6))).scaleb(pe)),
        "lot_size": str(Decimal(draw(st.integers(1, 10**6))).scaleb(se)),
    }


@st.composite
def instrument_map(draw: st.DrawFn) -> bytes:
    """A map of plausible rows with exactly one hostile cell (or a dropped column), quoted or written raw."""
    rows = draw(st.lists(_row(), min_size=1, max_size=5))
    cols = list(COLUMNS)
    edit = draw(st.integers(0, 5))
    if edit == 0:
        cols.remove(draw(st.sampled_from(cols)))
    elif edit < 5:  # edit 5: the plausible map as is
        i = draw(st.integers(0, len(rows) - 1))
        rows[i] = {
            **rows[i],
            draw(st.sampled_from(cols)): str(draw(st.one_of(one_bad_value, st.sampled_from(EDGE_DECIMALS)))),
        }
    if draw(st.booleans()):
        text = "\n".join([",".join(cols)] + [",".join(r[c] for c in cols) for r in rows])
    else:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        w.writerows([r[c] for c in cols] for r in rows)
        text = out.getvalue()
    return text.encode("utf-8", "surrogatepass")


def _load(data: bytes, what: str) -> Any:
    with workdir() as d:
        (p,), total = write_files(d, {"instrument_map.csv": data})
        return guarded(what, lambda: load_instrument_map(p), input_bytes=total)


@FUZZ
@given(instrument_map())
def test_imap_one_bad_cell(data: bytes) -> None:
    _load(data, "imap:one_field")


@FUZZ
@given(mutated(seeds("*/*/instrument_map.csv")))
def test_imap_mutated(data: bytes) -> None:
    _load(data, "imap:mutated")


_DERIBIT = LIVE_FIXTURES / "deribit" / "01_ws_user_orders_trades"


@FUZZ
@given(st.one_of(instrument_map(), *[mutated([(_DERIBIT / "instrument_map.csv").read_bytes()])] * 2))
def test_import_with_a_hostile_map(data: bytes) -> None:
    """The map as the importer uses it: lookups by (venue, instrument, session date) and the fixed-point scales."""
    with workdir() as d:
        paths, total = write_files(
            d, {"instrument_map.csv": data, "ws.jsonl": (_DERIBIT / "ws_notifications.jsonl").read_bytes()}
        )
        spec = {
            "venue": "deribit",
            "inputs": [{"path": paths[1]}],
            "instrument_map": {"path": paths[0]},
            "out": str(d / "o"),
        }
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            guarded("imap:import", lambda: import_venue(spec), input_bytes=total)
