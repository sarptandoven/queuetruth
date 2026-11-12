"""`queuetruth.data.infer`: format, venue and session date of an input, from synthetic bytes only."""

from __future__ import annotations

import gzip
import json
import shutil
import struct
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from queuetruth.data.infer import detect, head, json_format, sidecar_date, sniff, spec_format
from queuetruth.errors import QtError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))

import demo_fixtures as F

REPO = Path(__file__).resolve().parents[3]
NYSE = REPO / "tests" / "fixtures" / "nyse" / "EQY_US_NYSE_IBF_3_20260105.csv"


def _itch() -> bytes:
    return F.itch_day(["AAPL"], F.book_events(["AAPL"], F.hms(9, 30), F.hms(9, 31)))


def _dbn(dataset: str, version: int = 2) -> bytes:
    """A DBN prelude and the start of its metadata (enough for the sniffer; not a readable file)."""
    meta = dataset.encode().ljust(16, b"\0") + struct.pack("<H", 0) + b"\0" * 64
    return b"DBN" + bytes([version]) + struct.pack("<I", len(meta)) + meta


def test_itch_gzip_by_framing_and_date_from_the_name(tmp_path: Path) -> None:
    p = tmp_path / "12302019.NASDAQ_ITCH50.gz"
    p.write_bytes(gzip.compress(_itch(), mtime=0))
    d = detect(p)
    assert (d.format, d.venue, d.date) == ("itch", "nasdaq", "2019-12-30")
    assert d.how == {"format": "ITCH 5.0 framing", "venue": "the format", "date": "file name"}


def test_itch_date_from_a_manifest_next_to_it(tmp_path: Path) -> None:
    """The demo's cached subset is named `subset.itch50.gz`; its manifest holds the date (the first-hour audit)."""
    p = tmp_path / "subset.itch50.gz"
    p.write_bytes(gzip.compress(_itch(), mtime=0))
    man = {"schema": "queuetruth.demo_subset", "source": {"session_date": "2019-12-30"}, "subset": {"file": p.name}}
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    d = detect(p)
    assert (d.format, d.date, d.how["date"]) == ("itch", "2019-12-30", "manifest.json next to it")
    # a manifest about another file says nothing about this one
    man["subset"]["file"] = "other.gz"
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    assert sidecar_date(p) is None


def test_itch_without_a_date_asks_for_one_and_says_why(tmp_path: Path) -> None:
    p = tmp_path / "day.bin"
    p.write_bytes(_itch())
    with pytest.raises(QtError) as e:
        detect(p)
    assert e.value.code == "QT-USAGE-001" and "ITCH timestamps count from midnight" in e.value.message
    assert "--date" in (e.value.hint or "")
    assert detect(p, date="2019-12-30").how["date"] == "given"


def test_iex_pcapng_and_classic_pcap(tmp_path: Path) -> None:
    ev = F.book_events(["AAPL"], F.hms(9, 30), F.hms(9, 31))
    p = tmp_path / "anything.gz"
    p.write_bytes(gzip.compress(F.iex_day("2026-10-09", ["AAPL"], ev), mtime=0))
    d = detect(p)
    assert (d.format, d.venue, d.date, d.how["date"]) == ("iex_deep_plus", "iex", None, "the records")
    classic = tmp_path / "classic.pcap"
    classic.write_bytes(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1))
    assert detect(classic).format == "iex_deep_plus"


def test_dbn_venue_from_its_dataset_code_plain_and_zstd(tmp_path: Path) -> None:
    plain = tmp_path / "a.dbn"
    plain.write_bytes(_dbn("GLBX.MDP3"))
    d = detect(plain)
    assert (d.format, d.venue, d.how["venue"]) == ("dbn", "cme", "DBN dataset GLBX.MDP3")
    z = tmp_path / "b.mbo.dbn.zst"
    z.write_bytes(pa.compress(_dbn("XNAS.ITCH"), "zstd", asbytes=True))
    assert head(z)[:3] == b"DBN"
    assert (detect(z).format, detect(z).venue) == ("dbn", "nasdaq")


def test_dbn_of_another_dataset_asks_for_the_venue(tmp_path: Path) -> None:
    p = tmp_path / "c.dbn"
    p.write_bytes(_dbn("OPRA.PILLAR"))
    with pytest.raises(QtError) as e:
        detect(p)
    assert e.value.code == "QT-USAGE-001" and "OPRA.PILLAR" in e.value.message and "--venue" in (e.value.hint or "")
    assert detect(p, venue="generic").venue == "generic"


def test_nyse_by_its_product_name(tmp_path: Path) -> None:
    d = detect(NYSE)
    assert (d.format, d.venue, d.how["date"]) == ("nyse_ibf", "nyse", "the file name")
    gz = tmp_path / (NYSE.name + ".gz")
    gz.write_bytes(gzip.compress(NYSE.read_bytes(), mtime=0))
    assert detect(gz).format == "nyse_ibf"


def test_tardis_csv_with_its_exchange(tmp_path: Path) -> None:
    p = tmp_path / "book.csv"
    p.write_text(
        "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount\nexamplex,QTN,1,2,true,bid,10.0,1.0\n"
    )
    d = detect(p)
    assert (d.format, d.venue, d.source_venue) == ("tardis_l2", "generic", "examplex")


@pytest.mark.parametrize(
    ("line", "fmt"),
    [
        ({"event": "order_created", "channel": "live_orders_qtnusd", "data": {}, "recv_ns": 1}, "bitstamp"),
        ({"recv_ns": 1, "chan": 7, "symbol": "tQTNUSD", "msg": [7, [1, 2.0, 3.0]]}, "bitfinex"),
        ({"type": "open", "product_id": "QTN-USD", "order_id": "x", "recv_ns": 1}, "coinbase"),
        ({"type": "subscriptions", "channels": []}, "coinbase"),
        ({"recv_ns": 1, "msg": {"type": "level3", "schema": {"open": []}}}, "coinbase_level3"),
        ({"recv_ns": 1, "msg": ["open", "QTN-USD", 5]}, "coinbase_level3"),
        ({"recv_ns": 1, "product_id": "QTN-USD", "book": {"bids": [], "asks": []}}, "coinbase_level3"),
        ({"recv_ns": 1, "msg": {"channel": "level3", "type": "snapshot", "data": []}}, "kraken_l3"),
        ({"method": "subscribe", "result": {"channel": "level3", "symbol": "QTN/USD"}}, "kraken_l3"),
        ({"local_time": "t", "block_time": "t", "block_number": 1, "events": []}, "hyperliquid_node"),
    ],
)
def test_crypto_json_lines(line: dict, fmt: str) -> None:
    assert json_format(line) == fmt
    assert sniff((json.dumps(line) + "\n").encode(), "capture.jsonl") == (fmt, "its JSON lines")


def test_a_capture_directory_is_read_by_its_first_data_file(tmp_path: Path) -> None:
    d = tmp_path / "cap"
    d.mkdir()
    (d / "capture_meta.json").write_text('{"markets": ["qtnusd"]}')
    line = {"event": "trade", "channel": "live_trades_qtnusd", "data": {}, "recv_ns": 1}
    (d / "live_trades.jsonl").write_text(json.dumps(line) + "\n")
    got = detect(d)
    assert (got.format, got.venue) == ("bitstamp", "generic")


def test_unknown_bytes_ask_for_the_format(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("hello\n")
    with pytest.raises(QtError) as e:
        detect(p)
    assert e.value.code == "QT-USAGE-001" and "--input-format" in (e.value.hint or "")
    with pytest.raises(QtError) as e:
        detect(tmp_path / "missing.gz")
    assert e.value.code == "QT-INPUT-001"


def test_format_names_in_both_spellings() -> None:
    assert spec_format("iex-deep-plus") == spec_format("iex_deep_plus") == "iex_deep_plus"
    assert spec_format("ITCH") == "itch"
    with pytest.raises(QtError):
        spec_format("csv")


def test_a_given_format_wins(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    shutil.copy(NYSE, p)
    d = detect(p, format="nyse-ibf")
    assert (d.format, d.venue, d.how["format"]) == ("nyse_ibf", "nyse", "given")
