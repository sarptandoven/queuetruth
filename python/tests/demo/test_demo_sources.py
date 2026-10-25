"""`queuetruth.demo.sources` and `queuetruth.demo.cache`: the pinned sources, the demo window arithmetic, name
inference and the user cache directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from queuetruth.demo import sources as S
from queuetruth.demo.cache import user_cache_dir
from queuetruth.errors import QtError


def test_window_arithmetic_default() -> None:
    w = S.DemoWindow("2019-12-30", 5)
    assert w.start_s == 9 * 3600 + 35 * 60
    assert w.end_s == w.start_s + 300
    assert w.cut_s == w.end_s + 300 + 10
    assert (w.label(w.start_s), w.label(w.end_s), w.label(w.cut_s)) == ("09:35:00", "09:40:00", "09:45:10")
    assert w.cut_ns_local == w.cut_s * S.SEC_NS
    assert w.tag() == "0935-5m"


def test_window_midnight_follows_daylight_saving() -> None:
    # 2019-12-30 is EST (UTC-5), 2026-10-09 is EDT (UTC-4)
    assert S.DemoWindow("2019-12-30", 5).midnight_utc_ns == 1_577_682_000 * S.SEC_NS
    assert S.DemoWindow("2026-10-09", 5).midnight_utc_ns == 1_791_518_400 * S.SEC_NS
    w = S.DemoWindow("2026-10-09", 5)
    assert w.cut_ns_utc == w.midnight_utc_ns + w.cut_ns_local


def test_missing_time_zone_database_is_a_qt_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfoNotFoundError

    def missing(_key: str) -> None:
        raise ZoneInfoNotFoundError("no tz database")

    monkeypatch.setattr(S, "ZoneInfo", missing)
    S.eastern.cache_clear()
    try:
        with pytest.raises(QtError) as e:
            _ = S.DemoWindow("2019-12-30", 5).midnight_utc_ns
        assert e.value.code == "QT-USAGE-001" and "tzdata" in (e.value.hint or "")
    finally:
        S.eastern.cache_clear()


@pytest.mark.parametrize("minutes", [0, -1, 361])
def test_window_rejects_bad_minutes(minutes: int) -> None:
    with pytest.raises(QtError) as e:
        S.DemoWindow("2019-12-30", minutes)
    assert e.value.code == "QT-USAGE-001"


def test_bad_date() -> None:
    with pytest.raises(QtError) as e:
        S.DemoWindow("2019-13-40", 5).midnight_utc_ns  # noqa: B018
    assert e.value.code == "QT-USAGE-001"


def test_get_source_aliases_and_unknown() -> None:
    assert S.get_source("nasdaq").id == "nasdaq-2019-12-30"
    assert S.get_source("iex").id == "iex-2026-10-09"
    assert S.get_source("iex-2026-10-09").format == "iex"
    with pytest.raises(QtError) as e:
        S.get_source("cme")
    assert e.value.code == "QT-USAGE-001"
    assert "nasdaq-2019-12-30" in (e.value.hint or "")


def test_iex_sources_carry_the_exact_attribution() -> None:
    iex = S.get_source("iex")
    assert iex.attribution == (
        "Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical "
        "Data Terms of Use."
    )
    assert iex.terms_url == "https://iextrading.com/iex-historical-data-terms/"
    assert S.custom_source("iex", "2026-10-09", "/x.pcap", ("SPY",)).attribution == iex.attribution
    assert S.custom_source("itch", "2019-12-30", "/x.gz", ("SPY",)).attribution is None
    assert S.get_source("nasdaq").attribution is None and S.get_source("nasdaq").note


def test_estimate_bytes_interpolates_between_checkpoints() -> None:
    src = S.Source(
        id="t", title="t", format="itch", session_date="2019-12-30", url="u", default_symbols=("A",),
        checkpoints=((100, 1000), (200, 3000)),
    )  # fmt: skip
    assert src.estimate_bytes(50) == 1000
    assert src.estimate_bytes(100) == 1000
    assert src.estimate_bytes(150) == 2000
    assert src.estimate_bytes(200) == 3000
    assert not src.extrapolated(200) and src.extrapolated(201)
    assert src.estimate_bytes(300) == 5000  # the last segment's rate extrapolated
    capped = S.Source(
        id="t", title="t", format="itch", session_date="d", url="u", default_symbols=(),
        checkpoints=((100, 1000), (200, 3000)), listed_bytes=4000,
    )  # fmt: skip
    assert capped.estimate_bytes(300) == 4000  # never more than the whole file
    assert (
        S.Source(id="t", title="t", format="itch", session_date="d", url="u", default_symbols=()).estimate_bytes(1)
        is None
    )


def test_long_iex_window_is_estimated_past_the_cap() -> None:
    src = S.get_source("iex")
    w = S.DemoWindow(src.session_date, 360)
    est = src.estimate_bytes(w.cut_s)
    assert src.extrapolated(w.cut_s) and est == src.listed_bytes and est > 4000e6


def test_default_estimate_is_monotone_and_measured() -> None:
    src = S.get_source("nasdaq")
    w = S.DemoWindow(src.session_date, 5)
    est = src.estimate_bytes(w.cut_s)
    assert est is not None and 300e6 < est < 330e6
    pts = [b for _, b in src.checkpoints]
    assert pts == sorted(pts)


@pytest.mark.parametrize(
    ("name", "date", "fmt"),
    [
        ("12302019.NASDAQ_ITCH50.gz", "2019-12-30", "itch"),
        ("https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302020.NASDAQ_ITCH50.gz", "2020-01-30", "itch"),
        ("/data/12302019.subset.AAPL-MSFT.itch50.gz", "2019-12-30", "itch"),
        ("20261009_IEXTP1_DPLS1.0.pcap.gz", "2026-10-09", "iex"),
        (
            "https://x/o/data%2Ffeeds%2F20261008%2F20261008_IEXTP1_DPLS1.0.pcap.gz?generation=1&alt=media",
            "2026-10-08",
            "iex",
        ),
        (S.get_source("iex").url, "2026-10-09", "iex"),
        ("/data/mitchell/20261009_IEXTP1_DPLS1.0.pcap.gz", "2026-10-09", "iex"),
        ("/data/switch/capture.pcap.gz", None, "iex"),
        ("https://itch-mirror.example/files/20261009_DEEP.gz?fmt=itch", "2026-10-09", "iex"),
        ("C:\\data\\12302019.NASDAQ_ITCH50.gz", "2019-12-30", "itch"),
        ("/data/itch/day.bin", None, None),
        ("/data/20261009_DPLS.subset.SPY.pcap.gz", "2026-10-09", "iex"),
        ("/data/day.bin", None, None),
    ],
)
def test_infer_date_and_format(name: str, date: str | None, fmt: str | None) -> None:
    assert S.infer_date(name) == date
    assert S.infer_format(name) == fmt


@pytest.mark.parametrize(
    ("head", "fmt"),
    [
        (b"\x0a\x0d\x0d\x0a\x1c\x00\x00\x00", "iex"),
        (b"\x4d\x3c\xb2\xa1\x02\x00", "iex"),
        (b"\xd4\xc3\xb2\xa1\x02\x00", "iex"),
        (b"\x00\x0cS\x00\x00", "itch"),
        (b"\x00\x27R\x00\x01", "itch"),
        (b"\x00\x0cR\x00\x01", None),  # an R with the length of an S
        (b"\x00\x00\x00\x0cS", None),
        (b"\x00\x0c", None),
        (b"PK\x03\x04", None),
    ],
)
def test_sniff_format(head: bytes, fmt: str | None) -> None:
    assert S.sniff_format(head) == fmt


def test_read_sidecar(tmp_path: Path) -> None:
    sub = tmp_path / "subset.itch50.gz"
    assert S.read_sidecar(sub) is None
    (tmp_path / "manifest.json").write_text('{"schema": "queuetruth.demo_subset", "subset": {"file": "other.gz"}}')
    assert S.read_sidecar(sub) is None
    (tmp_path / "manifest.json").write_text(
        '{"schema": "queuetruth.demo_subset", "subset": {"file": "subset.itch50.gz"}}'
    )
    assert S.read_sidecar(sub) == {"schema": "queuetruth.demo_subset", "subset": {"file": "subset.itch50.gz"}}
    (tmp_path / "manifest.json").write_text("[1]")
    assert S.read_sidecar(sub) is None


def test_custom_source_rejects_unknown_format() -> None:
    with pytest.raises(QtError) as e:
        S.custom_source("dbn", "2019-12-30", "/x", ())
    assert e.value.code == "QT-USAGE-001"


def test_references_match_results_page() -> None:
    ref = S.REFERENCES["nasdaq-2019-12-30"]
    assert ref["bias"] == {"1000000000": 0.0313, "10000000000": 0.0330, "60000000000": 0.0143}
    text = (Path(__file__).resolve().parents[3] / "docs" / "RESULTS.md").read_text(encoding="utf-8")
    assert "| `nt_prob_touch[p=1]` | +3.13 [+2.63, +3.64] | +3.30 [+2.47, +4.14] | +1.43 [+1.01, +1.92] |" in text
    assert "2019-12-30 331 / 1,453 / 2,250 of 3,000 at 1 s / 10 s / 60 s (11.0%, 48.4%, 75.0%)" in text
    assert ref["exact_fill_rate"] == {
        k: round(n / 3000, 4) for k, n in (("1000000000", 331), ("10000000000", 1453), ("60000000000", 2250))
    }
    assert (
        "nautilus_trader 1.231.0" in text
        and "2.0.0rc7 (`DefaultFillModel(prob_fill_on_limit=1)`) fills exactly" in text
    )


@pytest.mark.parametrize(
    ("env", "platform", "want"),
    [
        ({"QT_CACHE_DIR": "/c", "HOME": "/h"}, "darwin", "/c"),
        ({"HOME": "/h"}, "darwin", "/h/Library/Caches/queuetruth"),
        ({"HOME": "/h"}, "linux", "/h/.cache/queuetruth"),
        ({"HOME": "/h", "XDG_CACHE_HOME": "/x"}, "linux", "/x/queuetruth"),
        ({"HOME": "/h", "XDG_CACHE_HOME": "relative"}, "linux", "/h/.cache/queuetruth"),
        ({"HOME": "/h", "LOCALAPPDATA": "/l"}, "win32", "/l/queuetruth/Cache"),
        ({"HOME": "/h"}, "win32", "/h/AppData/Local/queuetruth/Cache"),
    ],
)
def test_user_cache_dir(env: dict[str, str], platform: str, want: str) -> None:
    assert user_cache_dir(env, platform) == Path(want)
