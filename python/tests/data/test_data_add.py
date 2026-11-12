"""`queuetruth data add` on synthetic ITCH and IEX days (needs the compiled core): one dataset from several days,
adding a day to an existing dataset, refusals that leave the dataset as it was, the merge (also through the
repository script), the attribution in the manifest, and the command line."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import pytest

import queuetruth as qt
from queuetruth.data.merge import merge
from queuetruth.errors import QtError
from queuetruth.schemas.models import DatasetManifest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))

import demo_fixtures as F

pytestmark = pytest.mark.requires_core
REPO = Path(__file__).resolve().parents[3]
SYMS = ["AAPL", "MSFT"]


@pytest.fixture(scope="module")
def days(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Three ITCH days and two IEX days, five minutes of two symbols each."""
    d = tmp_path_factory.mktemp("days")
    ev = F.book_events(SYMS, F.hms(9, 30), F.hms(9, 35))
    out = {}
    for name in ("12302019.NASDAQ_ITCH50.gz", "01302020.NASDAQ_ITCH50.gz", "02032020.NASDAQ_ITCH50.gz"):
        F.write_gz(d / name, F.itch_day(SYMS, ev))
        out[name[:8]] = d / name
    for day in ("2026-10-09", "2026-10-12"):
        p = d / f"{day.replace('-', '')}_IEXTP1_DPLS1.0.pcap.gz"
        F.write_gz(p, F.iex_day(day, SYMS, ev))
        out[day] = p
    return out


def _manifest(ds: Path) -> dict:
    return json.loads((ds / "manifest.json").read_text())


def test_two_itch_days_into_one_dataset(days: dict[str, Path], tmp_path: Path) -> None:
    res = qt.data.add([days["12302019"], days["01302020"]], tmp_path / "ds")
    doc = res.doc
    assert doc["schema"] == "qt.data_add"
    assert res.summary.startswith(f"{tmp_path / 'ds'}: AAPL, MSFT on 2019-12-30 and 2020-01-30, ")
    assert "data quality clean" in res.summary
    assert [d["date"] for d in doc["dates"]] == ["2019-12-30", "2020-01-30"]
    assert all(d["timezone"] == "America/New_York" and d["first"] == "09:30:00" for d in doc["dates"])
    assert doc["symbols"] == SYMS and doc["partitions"] == 4
    assert doc["records"] == sum(d["records"] for d in doc["dates"]) > 0
    assert doc["dataset"]["format"] == "itch" and doc["dataset"]["venue"] == "nasdaq"
    assert [i["date"] for i in doc["inputs"]] == ["2019-12-30", "2020-01-30"]
    man = _manifest(tmp_path / "ds")
    DatasetManifest.model_validate(man)
    assert man["created_by"].endswith(" + queuetruth data add") and "attribution" not in man
    assert (tmp_path / "ds" / "dq_report.json").is_file() and (tmp_path / "ds" / "dq_report.html").is_file()
    assert doc["checks"]["status"] in ("pass", "warn")
    # the same records as one ingest per day
    one = qt.ingest({"format": "itch", "inputs": [{"path": str(days["12302019"])}], "venue": "nasdaq",
                     "out": str(tmp_path / "one"), "session_date": "2019-12-30"})  # fmt: skip
    assert doc["dates"][0]["records"] == one.data_quality.records
    rows = res.table()
    assert list(rows.columns[:4]) == ["date", "symbol", "instrument_id", "records"] and len(rows) == 4
    text = res.text()
    assert text.splitlines()[0] == res.summary and "2019-12-30  09:30:00 to 09:34:59" in text
    assert "next: queuetruth check BACKTEST --data" in text
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no staging left


def test_a_day_added_to_an_existing_dataset_and_a_day_added_twice(days: dict[str, Path], tmp_path: Path) -> None:
    ds = tmp_path / "ds"
    qt.data.add([days["12302019"]], ds)
    res = qt.data.add([days["02032020"]], ds)
    assert [d["date"] for d in res.doc["dates"]] == ["2019-12-30", "2020-02-03"]
    assert res.doc["added_to_existing"] and res.doc["added_dates"] == ["2020-02-03"]
    before = (ds / "manifest.json").read_bytes()
    with pytest.raises(QtError) as e:
        qt.data.add([days["02032020"]], ds)
    assert e.value.code == "QT-INPUT-002" and "2020-02-03" in e.value.message
    assert (ds / "manifest.json").read_bytes() == before  # refused: the dataset is as it was
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    man = _manifest(ds)
    assert man["created_by"].count("queuetruth data add") == 1


def test_two_iex_days_in_one_call(days: dict[str, Path], tmp_path: Path) -> None:
    res = qt.data.add([days["2026-10-09"], days["2026-10-12"]], tmp_path / "iex")
    assert [d["date"] for d in res.doc["dates"]] == ["2026-10-09", "2026-10-12"]
    assert res.doc["dataset"]["rule_set"] == "iex" and "venue iex" in res.text()
    assert res.doc["inputs"][0]["how"]["date"] == "the records"


def test_symbols_filter(days: dict[str, Path], tmp_path: Path) -> None:
    res = qt.data.add([days["12302019"]], tmp_path / "ds", symbols=["MSFT"])
    assert res.doc["symbols"] == ["MSFT"]


def test_refusals(days: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        qt.data.add([days["12302019"], days["2026-10-09"]], tmp_path / "mixed")
    assert e.value.code == "QT-USAGE-001" and "one dataset holds one format" in e.value.message
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("x")
    with pytest.raises(QtError) as e:
        qt.data.add([days["12302019"]], busy)
    assert e.value.code == "QT-USAGE-001" and "not a dataset" in e.value.message
    twice = tmp_path / "twice"
    twice.mkdir()
    copy = twice / "12302019.copy.gz"
    copy.write_bytes(days["12302019"].read_bytes())
    with pytest.raises(QtError) as e:
        qt.data.add([days["12302019"], copy], tmp_path / "t")
    assert "both itch for 2019-12-30" in e.value.message
    with pytest.raises(QtError):
        qt.data.add([days["12302019"]], tmp_path / "d", date="30.12.2019")
    assert not (tmp_path / "mixed").exists() and not (tmp_path / "t").exists()


def test_a_failed_ingest_leaves_nothing(days: dict[str, Path], tmp_path: Path) -> None:
    bad = tmp_path / "12302019.NASDAQ_ITCH50.gz"
    data = gzip.decompress(days["12302019"].read_bytes())
    bad.write_bytes(gzip.compress(data[: len(data) // 2 + 3], mtime=0))  # cut inside a message
    with pytest.raises(QtError):
        qt.data.add([bad], tmp_path / "ds")
    assert not (tmp_path / "ds").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_merge_keeps_every_days_trading_halts_and_the_script_still_works(days: dict[str, Path], tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for src, out, day in ((days["12302019"], a, "2019-12-30"), (days["01302020"], b, "2020-01-30")):
        qt.ingest({"format": "itch", "inputs": [{"path": str(src)}], "venue": "nasdaq", "out": str(out),
                   "session_date": day})  # fmt: skip
    for ds, ts in ((a, 1), (b, 2)):
        dq = json.loads((ds / "data_quality.json").read_text())
        dq["trading_halts"] = [
            {"instrument_id": 1, "ts_start": ts, "ts_end": ts + 1, "state": "itch_H:H", "reason": ""}
        ]
        (ds / "data_quality.json").write_text(json.dumps(dq))
    man = merge([a, b], tmp_path / "m")
    halts = json.loads((tmp_path / "m" / "data_quality.json").read_text())["trading_halts"]
    assert [h["ts_start"] for h in halts] == [1, 2]
    assert len(man["partitions"]) == 4
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "realdata" / "merge_datasets.py"), "--out", str(tmp_path / "s"),
         str(a), str(b)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dataset_id"] == man["dataset_id"] and out["partitions"] == 4
    assert _manifest(tmp_path / "s")["created_by"].endswith(" + merge_datasets.py")
    # `data add` of the two files builds the same dataset as the two ingests and the script (docs/how-to/notebooks.md)
    one = qt.data.add([days["12302019"], days["01302020"]], tmp_path / "one")
    assert one.doc["dataset"]["dataset_id"] == out["dataset_id"]
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "realdata" / "merge_datasets.py"), "--out", str(tmp_path / "s2"),
         str(a), str(a)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 1 and "cannot merge" in r.stderr


def test_attribution_is_recorded_and_read_by_the_engine(days: dict[str, Path], tmp_path: Path) -> None:
    att = {"source": "IEX DEEP+ (IEX HIST)", "text": "Data provided by a test.", "terms_url": "https://example.org/t",
           "url": "http://127.0.0.1/day.pcap.gz"}  # fmt: skip
    res = qt.data.add([days["2026-10-09"]], tmp_path / "iex", attribution=[att])
    man = _manifest(tmp_path / "iex")
    assert man["attribution"] == [att] and res.doc["attribution"] == [att]
    assert DatasetManifest.model_validate(man).attribution[0].terms_url == "https://example.org/t"
    # the Rust reader accepts it (sampling shadows reads the manifest)
    qt.sample_shadows({"dataset": {"path": str(tmp_path / "iex")}, "out": str(tmp_path / "s.parquet"), "n": 5})
    # and a later day keeps it
    qt.data.add([days["2026-10-12"]], tmp_path / "iex")
    assert _manifest(tmp_path / "iex")["attribution"] == [att]
    assert "Data provided by a test. https://example.org/t" in res.text()


def test_the_command_line(days: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from queuetruth import cli

    out = tmp_path / "ds"
    rc = cli.main(["data", "add", str(days["12302019"]), str(days["01302020"]), "--out", str(out), "--quiet"])
    assert rc == 0
    text = capsys.readouterr().out
    assert text.splitlines()[0].startswith(f"{out}: AAPL, MSFT on 2019-12-30 and 2020-01-30")
    rc = cli.main(["--format", "json", "data", "add", str(days["02032020"]), "--out", str(out)])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.data_add" and len(doc["dates"]) == 3
    # --format with a source format after `data` is the input format; --json after it is the output format
    rc = cli.main(["data", "add", str(days["2026-10-09"]), "--out", str(tmp_path / "i"), "--format", "iex_deep_plus",
                   "--json", "--quiet"])  # fmt: skip
    assert rc == 0 and json.loads(capsys.readouterr().out)["inputs"][0]["how"]["format"] == "given"
    plain = tmp_path / "day.bin"
    plain.write_bytes(gzip.decompress(days["12302019"].read_bytes()))
    rc = cli.main(["data", "add", str(plain), "--out", str(tmp_path / "x")])
    err = capsys.readouterr().err
    assert rc == 2 and "QT-USAGE-001" in err and "cannot tell the session date of day.bin" in err
    assert "hint: pass --date YYYY-MM-DD" in err
    rc = cli.main(["data", "add", str(plain), "--out", str(tmp_path / "x"), "--date", "2019-12-30", "--quiet"])
    assert rc == 0
    capsys.readouterr()
    assert cli.main(["help", "data"]) == 0 and "add" in capsys.readouterr().out
    assert cli.main(["data", "add", "--help"]) == 0 and "--input-format" in capsys.readouterr().out
