"""Instrument-map CSV (`queuetruth.live.imap`, SPEC 3.9): integers and dates are ASCII only, and every error names
the file and line (python-fuzz follow-up)."""

from __future__ import annotations

from pathlib import Path

import pytest

from queuetruth.errors import QtError
from queuetruth.live.imap import load_instrument_map

HEADER = "venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\n"
GOOD = {
    "instrument_id": "7",
    "session_date": "2026-10-15",
    "price_exp": "-2",
    "size_exp": "-5",
}


def _map(tmp_path: Path, **cells: str) -> Path:
    row = {**GOOD, **cells}
    p = tmp_path / "instrument_map.csv"
    p.write_text(
        HEADER + f"binance,BTCUSDT,{row['instrument_id']},{row['session_date']},{row['price_exp']},"
        f"{row['size_exp']},0.01,0.00001\n",
        encoding="utf-8",
    )
    return p


def _refused(p: Path) -> str:
    with pytest.raises(QtError) as ei:
        load_instrument_map(p)
    assert ei.value.code == "QT-INPUT-002"
    msg = str(ei.value)
    assert f"{p}:2" in msg, msg
    return msg


def test_ascii_row_loads(tmp_path: Path) -> None:
    m = load_instrument_map(_map(tmp_path, price_exp=" -2 ", size_exp="-05", instrument_id="+0007"))
    (e,) = m.entries
    assert (e.instrument_id, e.price_exp, e.size_exp, e.session_date) == (7, -2, -5, 20741)
    assert m.lookup("BINANCE", "BTCUSDT", 20741) is e


@pytest.mark.parametrize("column", ["price_exp", "size_exp", "instrument_id"])
@pytest.mark.parametrize(
    "value",
    [
        "\u0662",  # ARABIC-INDIC DIGIT TWO: int() reads 2
        "-\uff15",  # FULLWIDTH DIGIT FIVE: int() reads -5
        "1_0",  # int() reads 10
        "\u00b2",  # SUPERSCRIPT TWO: int() refuses it too
        "0x1",
        "1.0",
        "",
    ],
)
def test_integer_columns_refuse_what_is_not_an_ascii_integer(tmp_path: Path, column: str, value: str) -> None:
    msg = _refused(_map(tmp_path, **{column: value}))
    assert f"{column}: {value!r} is not an integer" in msg


def test_integer_columns_keep_their_ranges(tmp_path: Path) -> None:
    assert "price_exp: 1 outside [-12, 0]" in _refused(_map(tmp_path, price_exp="1"))
    assert "instrument_id: 4294967296 outside" in _refused(_map(tmp_path, instrument_id=str(2**32)))
    assert "is not an integer" in _refused(_map(tmp_path, instrument_id="9" * 5000))


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("2026-02-30", "is not a valid date (day is out of range for month)"),
        ("2026-13-01", "is not a valid date"),
        ("20261015", "is not a YYYY-MM-DD date"),
        ("2026-10-15T00:00", "is not a YYYY-MM-DD date"),
        ("\u0662026-10-15", "is not a YYYY-MM-DD date"),
        ("2026-W42-4", "is not a YYYY-MM-DD date"),
    ],
)
def test_session_date_errors_name_the_file(tmp_path: Path, value: str, why: str) -> None:
    msg = _refused(_map(tmp_path, session_date=value))
    assert f"session_date: {value!r} {why}" in msg
