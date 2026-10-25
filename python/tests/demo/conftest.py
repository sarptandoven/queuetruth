"""Fixtures of the `queuetruth demo` tests: tiny synthetic ITCH and IEX days (`demo_fixtures`), written once per
session."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import demo_fixtures as F

SYMS = ["AAPL", "MSFT"]
ITCH_DATE = "2019-12-30"
IEX_DATE = "2026-10-09"


@pytest.fixture(scope="session")
def events() -> list[F.Ev]:
    """Two symbols from 09:30 to 09:42 ET (about 36,000 events)."""
    return F.book_events(SYMS, F.hms(9, 30), F.hms(9, 42))


@pytest.fixture(scope="session")
def itch_bytes(events: list[F.Ev]) -> bytes:
    return F.itch_day(SYMS, events)


@pytest.fixture(scope="session")
def iex_bytes(events: list[F.Ev]) -> bytes:
    return F.iex_day(IEX_DATE, SYMS, events, heartbeat_every=50)


@pytest.fixture(scope="session")
def itch_gz(tmp_path_factory: pytest.TempPathFactory, itch_bytes: bytes) -> Path:
    """The ITCH day as a three-member gzip file named like a public day file."""
    p = tmp_path_factory.mktemp("itch") / "12302019.NASDAQ_ITCH50.gz"
    F.write_gz(p, itch_bytes, members=3)
    return p


@pytest.fixture(scope="session")
def iex_gz(tmp_path_factory: pytest.TempPathFactory, iex_bytes: bytes) -> Path:
    p = tmp_path_factory.mktemp("iex") / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    F.write_gz(p, iex_bytes)
    return p
