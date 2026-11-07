"""Finding the newest published IEX DEEP+ day (`scripts/scale/iex_latest.py`) with a fake HEAD probe (no network)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import iex_latest as L

FRI, SAT, MON = dt.date(2026, 10, 30), dt.date(2026, 10, 31), dt.date(2026, 11, 2)


def _probe(published: dict[dt.date, int]):
    asked: list[str] = []

    def probe(url: str) -> int | None:
        asked.append(url)
        return next((n for d, n in published.items() if L.url_of(d) == url), None)

    return probe, asked


def test_url_is_the_public_address_of_the_day() -> None:
    assert L.url_of(FRI) == "https://storage.googleapis.com/iex/data/feeds/20261030/20261030_IEXTP1_DPLS1.0.pcap.gz"


def test_newest_weekday_first_and_weekends_skipped() -> None:
    probe, asked = _probe({FRI: 13_866_153_756})
    # on Monday the newest session is Friday: the weekend is never asked for
    assert L.find(MON, 10, probe=probe) == (FRI, L.url_of(FRI), 13_866_153_756)
    assert asked == [L.url_of(FRI)]
    # a holiday (no file) falls back to the day before
    probe, asked = _probe({FRI - dt.timedelta(days=1): 1})
    assert L.find(SAT, 10, probe=probe)[0] == FRI - dt.timedelta(days=1)
    assert asked[:2] == [L.url_of(FRI), L.url_of(FRI - dt.timedelta(days=1))]
    assert L.candidates(SAT, 7) == [FRI - dt.timedelta(days=i) for i in range(5)]


def test_a_given_date_only_and_nothing_found(capsys: pytest.CaptureFixture[str]) -> None:
    probe, asked = _probe({FRI: 5})
    assert L.find(MON, 10, date=FRI - dt.timedelta(days=1), probe=probe) is None
    assert asked == [L.url_of(FRI - dt.timedelta(days=1))]
    assert L.find(MON, 2, probe=_probe({})[0]) is None


def test_main_prints_github_output_lines(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(L, "head", _probe({FRI: 42})[0])
    assert L.main(["--today", "2026-10-31"]) == 0
    assert capsys.readouterr().out.splitlines() == ["date=2026-10-30", f"url={L.url_of(FRI)}", "bytes=42"]
    assert L.main(["--date", "2026-10-31"]) == 1
    assert "no IEX DEEP+ file for 2026-10-31" in capsys.readouterr().err
