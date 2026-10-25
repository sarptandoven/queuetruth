"""Fuzz properties of the JSON venue importers (Binance, Bybit, OKX, Coinbase, Kraken, Deribit, Hyperliquid) through
the full `live import` path the service runs (`import_venue`: importer, canonical build, validation, Parquet
write): structure-aware records, mutated fixtures and raw bytes either import or fail with a `QT-*` code."""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from fuzz._harness import FUZZ, guarded, scaled, workdir, write_files
from fuzz._mutate import mutated, seeds
from fuzz._values import json_document, one_field_replaced
from fuzz._venues import VENUE_RECORDS, VENUE_RECORDS_CLEAN
from hypothesis import given
from hypothesis import strategies as st

from queuetruth.errors import QtError
from queuetruth.live import import_venue
from queuetruth.live._common import read_json_records

VENUES = sorted(VENUE_RECORDS)


def import_files(venue: str, files: list[bytes], *, ext: str = ".jsonl", what: str = "import") -> Any:
    """`import_venue` over the files (written to a fresh directory) under the fuzz contract."""
    with workdir() as d:
        paths, total = write_files(d, {f"in{i}{ext}": b for i, b in enumerate(files)})
        spec = {"venue": venue, "inputs": [{"path": p} for p in paths], "out": str(d / "out")}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return guarded(f"{what}:{venue}", lambda: import_venue(spec), input_bytes=total)


@pytest.mark.parametrize("venue", VENUES)
def test_hostile_records(venue: str) -> None:
    @FUZZ
    @given(st.lists(json_document(VENUE_RECORDS[venue]), min_size=1, max_size=2))
    def prop(files: list[bytes]) -> None:
        import_files(venue, files, what="hostile")

    prop()


@pytest.mark.parametrize("venue", VENUES)
def test_plausible_records(venue: str) -> None:
    """Plausible values only: most imports succeed, so the order-building and validation logic runs on random
    message sequences (orderings, joins, duplicates, missing fields)."""

    @FUZZ
    @given(st.lists(json_document(VENUE_RECORDS_CLEAN[venue], hostile=False), min_size=1, max_size=2))
    def prop(files: list[bytes]) -> None:
        import_files(venue, files, what="plausible")

    prop()


@pytest.mark.parametrize("venue", VENUES)
def test_one_bad_field(venue: str) -> None:
    """Plausible records with one value each replaced by a hostile one: unlike `test_hostile_records`, where most
    examples stop at the first of several bad fields, every field is reached with the others valid."""
    clean = VENUE_RECORDS_CLEAN[venue]

    @FUZZ
    @given(st.lists(json_document(st.one_of(clean, one_field_replaced(clean)), hostile=False), min_size=1, max_size=2))
    def prop(files: list[bytes]) -> None:
        import_files(venue, files, what="one_field")

    prop()


@pytest.mark.parametrize("venue", VENUES)
def test_mutated_fixtures(venue: str) -> None:
    corpus = seeds(f"{venue}/*/*.json*")

    @FUZZ
    @given(st.lists(mutated(corpus), min_size=1, max_size=2))
    def prop(files: list[bytes]) -> None:
        import_files(venue, files, what="mutated")

    prop()


@pytest.mark.parametrize("venue", VENUES)
def test_raw_bytes(venue: str) -> None:
    @scaled(0.5)
    @given(st.binary(max_size=512))
    def prop(data: bytes) -> None:
        import_files(venue, [data], what="bytes")

    prop()


@scaled(2)
@given(st.one_of(st.binary(max_size=256), json_document(VENUE_RECORDS["binance"])))
def test_json_reader(data: bytes) -> None:
    """The shared JSON / JSON-lines reader (capture wrappers, BOM, CRLF) on its own."""
    with workdir() as d:
        p = d / "x.jsonl"
        p.write_bytes(data)
        out = guarded("read_json_records", lambda: read_json_records(p, 0), input_bytes=len(data))
        assert isinstance(out, (list, QtError))
