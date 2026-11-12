"""`queuetruth data fetch iex` against a local HTTP server serving a small synthetic DEEP+ day at IEX's path layout
(needs the compiled core): consent before any request, the symbol filter, the attribution, the day checks, a day
the server does not have, and the command line. No real data and no outside network."""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import json
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import queuetruth as qt
from queuetruth.data.iex import IEX_BASE, iex_url, plan_fetch
from queuetruth.errors import QtError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))

import demo_fixtures as F

pytestmark = pytest.mark.requires_core
DAY = dt.date(2026, 10, 9)
SYMS = ["AAPL", "MSFT"]


class Server:
    def __init__(self, root: Path) -> None:
        self.requests: list[str] = []
        outer = self

        class H(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.requests.append(self.path)
                super().do_GET()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(scope="module")
def day_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic day (AAPL, MSFT and an extra symbol ZZZT) at `<root>/20261009/20261009_IEXTP1_DPLS1.0.pcap.gz`."""
    root = tmp_path_factory.mktemp("bucket")
    ev = F.book_events([*SYMS, "ZZZT"], F.hms(9, 30), F.hms(9, 34))
    p = root / "20261009" / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    p.parent.mkdir()
    F.write_gz(p, F.iex_day(DAY.isoformat(), [*SYMS, "ZZZT"], ev, extra_symbols=()), members=2)
    return p


@pytest.fixture
def server(day_file: Path) -> Iterator[Server]:
    s = Server(day_file.parents[1])
    yield s
    s.close()


def test_the_public_url() -> None:
    assert iex_url(DAY) == f"{IEX_BASE}/20261009/20261009_IEXTP1_DPLS1.0.pcap.gz"
    assert IEX_BASE == "https://storage.googleapis.com/iex/data/feeds"
    pl = plan_fetch("2026-10-09", "aapl, msft")
    assert pl.symbols == ("AAPL", "MSFT") and pl.out == "iex-2026-10-09" and pl.url == iex_url(DAY)
    with pytest.raises(QtError):
        plan_fetch(dt.date.today() + dt.timedelta(days=2), "AAPL")
    with pytest.raises(QtError):
        plan_fetch("2026-10-09", "")


def test_fetch_keeps_the_symbols_and_records_the_attribution(server: Server, day_file: Path, tmp_path: Path) -> None:
    seen: list[dict] = []
    out = tmp_path / "ds"
    res = qt.data.fetch_iex("2026-10-09", ["msft", "AAPL"], out, yes=True, url=iex_url(DAY, server.base),
                            progress=seen.append)  # fmt: skip
    assert res.doc["symbols"] == SYMS and [d["date"] for d in res.doc["dates"]] == ["2026-10-09"]
    man = json.loads((out / "manifest.json").read_text())
    (att,) = man["attribution"]
    assert att["text"].startswith("Data provided for free by IEX.")
    assert att["terms_url"] == "https://iextrading.com/iex-historical-data-terms/"
    assert att["url"] == iex_url(DAY, server.base) and att["source"] == "IEX DEEP+ (IEX HIST)"
    f = res.doc["fetch"]
    assert f["bytes_read"] == day_file.stat().st_size == f["total_bytes"] and f["kept_messages"] < f["messages"]
    assert seen and seen[-1]["bytes"] == f["bytes_read"] and seen[-1]["market_s"] is not None
    # the same records as `data add` of the whole file with the same symbols; the day file is not kept
    full = qt.data.add([day_file], tmp_path / "full", symbols=SYMS)
    assert [d["records"] for d in res.doc["dates"]] == [d["records"] for d in full.doc["dates"]]
    assert not [p for p in tmp_path.rglob("*.pcap*")]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    text = res.text()
    assert "Data provided for free by IEX." in text and "the day's file was not stored" in text


def test_nothing_is_fetched_without_consent(server: Server, tmp_path: Path) -> None:
    asked = []
    with pytest.raises(QtError) as e:
        qt.data.fetch_iex("2026-10-09", "AAPL", tmp_path / "ds", url=iex_url(DAY, server.base),
                          confirm=lambda pl: asked.append(pl) is not None)  # fmt: skip
    assert e.value.code == "QT-USAGE-001" and "not confirmed" in e.value.message
    assert len(asked) == 1 and asked[0].symbols == ("AAPL",)
    with pytest.raises(QtError):
        qt.data.fetch_iex("2026-10-09", "AAPL", tmp_path / "ds", url=iex_url(DAY, server.base))
    assert server.requests == [] and not (tmp_path / "ds").exists()


def test_a_day_the_bucket_does_not_have(server: Server, tmp_path: Path) -> None:
    day = dt.date(2026, 10, 10)
    with pytest.raises(QtError) as e:
        qt.data.fetch_iex(day, "AAPL", tmp_path / "ds", yes=True, url=iex_url(day, server.base))
    assert e.value.code == "QT-INPUT-001" and "IEX has no DEEP+ file for 2026-10-10" in e.value.message
    assert "weekends and holidays" in (e.value.hint or "")
    assert not (tmp_path / "ds").exists() and not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_the_file_of_another_day_or_a_missing_symbol_is_refused(server: Server, tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        qt.data.fetch_iex("2026-10-08", "AAPL", tmp_path / "a", yes=True, url=iex_url(DAY, server.base))
    assert e.value.code == "QT-INPUT-002" and "starts on 2026-10-09" in e.value.message
    with pytest.raises(QtError) as e:
        qt.data.fetch_iex("2026-10-09", "AAPL,QQQQ", tmp_path / "b", yes=True, url=iex_url(DAY, server.base))
    assert e.value.code == "QT-INPUT-002" and "no messages for QQQQ" in e.value.message
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_a_second_day_joins_the_dataset(server: Server, day_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "ds"
    qt.data.add([day_file], out, symbols=["AAPL"], date="2026-10-08")  # the same bytes filed as another day
    res = qt.data.fetch_iex("2026-10-09", "AAPL", out, yes=True, url=iex_url(DAY, server.base))
    assert [d["date"] for d in res.doc["dates"]] == ["2026-10-08", "2026-10-09"]
    assert res.doc["added_to_existing"]


def test_the_command_line(
    server: Server, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from queuetruth import cli

    url = iex_url(DAY, server.base)
    out = tmp_path / "ds"
    base = ["data", "fetch", "iex", "--date", "2026-10-09", "--symbols", "AAPL", "--url", url, "--out", str(out)]
    assert cli.main([*base, "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema"] == "qt.data_fetch_plan" and plan["symbols"] == ["AAPL"] and plan["url"] == url
    assert cli.main([*base, "--dry-run"]) == 0
    assert "never stored" in capsys.readouterr().out
    assert server.requests == []
    # not a terminal and no --yes: refused with the way to agree
    assert cli.main(base) == 2
    err = capsys.readouterr().err
    assert "QT-USAGE-001" in err and "--yes" in err and server.requests == []
    # a terminal that says no
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.readline", lambda: "n\n")
    assert cli.main(base) == 2
    err = capsys.readouterr().err
    assert "Download now? [y/N]" in err and "Data provided for free by IEX." in err and server.requests == []
    monkeypatch.setattr("sys.stdin.readline", lambda: "y\n")
    assert cli.main([*base, "--quiet"]) == 0
    assert capsys.readouterr().out.startswith(f"{out}: AAPL on 2026-10-09")
    assert len(server.requests) == 1
