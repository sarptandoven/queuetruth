"""`queuetruth demo` command line without a download: dispatch ahead of the clap grammar, help, the plan
(`--dry-run`), consent, usage errors and the output-directory guard. No test here opens a network connection:
`Stream` is replaced by a sentinel that fails the test if anything tries."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from queuetruth import cli
from queuetruth.demo import _dispatch as D
from queuetruth.demo import cli as dcli
from queuetruth.demo import run as R
from queuetruth.demo import subset as SB
from queuetruth.demo.summary import DemoPlan
from queuetruth.errors import QtError


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the demo tried to open a stream")

    monkeypatch.setattr(SB, "Stream", boom)


@pytest.mark.parametrize(
    ("argv", "index"),
    [
        (["demo"], 0),
        (["demo", "--dry-run"], 0),
        (["--format", "json", "demo", "--n", "5"], 2),
        (["--format=json", "--quiet", "demo"], 2),
        (["--threads", "2", "--seed", "3", "--verbose", "demo"], 5),
        (["--log-level", "info", "demo"], 2),
        (["zoo", "demo"], None),
        (["--threads", "2", "zoo"], None),
        ([], None),
        (["--quiet"], None),
    ],
)
def test_command_index(argv: list[str], index: int | None) -> None:
    assert D._command_index(argv) == index


def test_other_commands_pass_through() -> None:
    assert D.maybe_run(["zoo", "--help"]) is None
    assert D.maybe_run(["help", "zoo"]) is None


def test_other_commands_never_import_the_demo() -> None:
    """The hook in `queuetruth.cli.main` must not load the demo (or numpy, ssl, zoneinfo data) for other commands,
    so a broken demo import or a missing tz database cannot break them."""
    code = (
        "import sys\n"
        "for m in ('queuetruth.demo.run', 'queuetruth.demo.sources', 'queuetruth.demo.cli'):\n"
        "    sys.modules[m] = None  # any import of them fails\n"
        "from queuetruth.demo._dispatch import maybe_run\n"
        "assert maybe_run(['--format', 'json', 'zoo', '--help']) is None\n"
        "assert maybe_run(['help', 'ingest']) is None\n"
        "print(sorted(m for m, v in sys.modules.items() if m.startswith('queuetruth.demo') and v is not None))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "['queuetruth.demo', 'queuetruth.demo._dispatch']"


def test_help_demo(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["help", "demo"]) == 0
    assert capsys.readouterr().out.startswith("usage: queuetruth demo")


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["demo", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: queuetruth demo") and "--from-file" in out and "--dry-run" in out


def test_list_sources(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["demo", "--list-sources"]) == 0
    assert "nasdaq-2019-12-30" in capsys.readouterr().out
    assert cli.main(["--format", "json", "demo", "--list-sources"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.demo_sources" and {s["id"] for s in doc["sources"]} == {
        "nasdaq-2019-12-30",
        "iex-2026-10-09",
    }
    from queuetruth.demo.summary import DemoSources

    sources = DemoSources.model_validate(doc)
    assert sources.default == "nasdaq-2019-12-30" and sources.aliases["iex"] == "iex-2026-10-09"


def test_dry_run_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["demo", "--dry-run", "--cache-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "download: about 310 MB of a 3.52 GB file" in text
    assert "orders placed 09:35:00 to 09:40:00 ET; data cut 09:45:10 ET" in text
    assert cli.main(["--format", "json", "demo", "--dry-run", "--source", "iex", "--cache-dir", str(tmp_path)]) == 0
    plan = DemoPlan.model_validate(json.loads(capsys.readouterr().out))
    assert plan.download_needed and not plan.cache_hit and plan.source.id == "iex-2026-10-09"
    assert plan.symbols == ["SPY", "AAPL", "NVDA", "TSLA"] and plan.file_bytes == 9_383_598_955
    assert plan.source.attribution and plan.estimated_download_bytes and plan.estimated_download_bytes > 800e6
    assert plan.cache_dir == str(tmp_path / "demo" / "iex-2026-10-09_0935-5m_SPY-AAPL-NVDA-TSLA")


def test_dry_run_prints_iex_attribution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["demo", "--dry-run", "--source", "iex", "--cache-dir", str(tmp_path)]) == 0
    assert (
        "Data provided for free by IEX. By accessing or using IEX Historical Data, you agree to the IEX Historical "
        "Data Terms of Use. https://iextrading.com/iex-historical-data-terms/"
    ) in capsys.readouterr().out


def test_remote_download_needs_consent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["--format", "json", "demo", "--cache-dir", str(tmp_path / "c"), "--out", str(tmp_path / "o")])
    assert rc == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.error" and doc["code"] == "QT-USAGE-001"
    assert "--yes" in doc["hint"] and "emi.nasdaq.com" in doc["message"]
    assert not (tmp_path / "o").exists()  # nothing written when the download is refused


def test_python_api_never_downloads_without_consent(tmp_path: Path) -> None:
    opts = R.DemoOptions(cache_dir=str(tmp_path / "c"), out=str(tmp_path / "o"))
    with pytest.raises(QtError) as e:
        R.run_demo(opts)
    assert e.value.code == "QT-USAGE-001" and "not confirmed" in e.value.message
    with pytest.raises(QtError):
        R.run_demo(opts, confirm=lambda _pl: False)
    assert not (tmp_path / "o").exists()


def test_download_past_the_cap_is_refused_before_it_starts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["demo", "--source", "iex", "--minutes", "360", "--cache-dir", str(tmp_path), "--out", str(tmp_path / "o")]
    assert cli.main([*args, "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "(extrapolated past the last measured time)" in text and "refused: more than --max-download-mb" in text
    assert cli.main(["--format", "json", *args, "--yes"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["code"] == "QT-USAGE-001" and "--max-download-mb" in doc["message"]


def test_prompt_goes_to_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import io

    monkeypatch.setattr(dcli.sys, "stdin", io.StringIO("y\n"))
    assert dcli._ask("Download now? [y/N] ") == "y\n"
    out = capsys.readouterr()
    assert out.out == "" and out.err == "Download now? [y/N] "


@pytest.mark.parametrize(
    "args",
    [
        ["--url", "https://x/a.gz", "--from-file", "b.gz"],
        ["--minutes", "0"],
        ["--n", "0"],
        ["--symbols", "AAPL,AAPL"],
        ["--symbols", "AAPL,BAD SYMBOL"],
        ["--symbols", ","],
        ["--input-format", "iex"],
        ["--date", "2019-12-30"],
        ["--url", "ftp://x/12302019.NASDAQ_ITCH50.gz"],
        ["--source", "cme"],
        ["--models", "hbt_power3[n=2],front_of_queue"],
        ["--threads", "0"],
        ["--minutes", "abc"],
        ["--no-such-flag"],
    ],
)
def test_usage_errors(args: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["--format", "json", "demo", "--cache-dir", str(tmp_path), "--out", str(tmp_path / "o"), *args])
    assert rc == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["code"] == "QT-USAGE-001", doc


def test_missing_from_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["demo", "--from-file", str(tmp_path / "nope.gz"), "--date", "2019-12-30", "--input-format", "itch"])
    assert rc == 2
    assert "QT-INPUT-001" in capsys.readouterr().err


def test_out_directory_guard(tmp_path: Path) -> None:
    out = tmp_path / "busy"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    with pytest.raises(QtError) as e:
        R.prepare_out(out)
    assert e.value.code == "QT-USAGE-001"
    assert (out / "notes.txt").read_text() == "mine"
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(QtError):
        R.prepare_out(f)
    # a foreign demo.json next to other files does not make a directory a demo output
    proj = tmp_path / "proj"
    (proj / "dataset").mkdir(parents=True)
    (proj / "dataset" / "mine.parquet").write_text("x")
    (proj / "demo.json").write_text("{}")
    (proj / "notes.txt").write_text("n")
    with pytest.raises(QtError):
        R.prepare_out(proj)
    assert (proj / "dataset" / "mine.parquet").exists()
    # a previous demo output (its qt.demo_summary): only the demo's entries go, the marker is written first
    prev = tmp_path / "prev"
    (prev / "zoo").mkdir(parents=True)
    (prev / "demo.json").write_text('{"schema": "qt.demo_summary"}')
    (prev / "keep.txt").write_text("k")
    R.prepare_out(prev, check_only=True)
    assert sorted(p.name for p in prev.iterdir()) == ["demo.json", "keep.txt", "zoo"]
    with pytest.raises(QtError):
        R.prepare_out(out, check_only=True)
    R.prepare_out(tmp_path / "new", check_only=True)
    assert not (tmp_path / "new").exists()
    R.prepare_out(prev)
    assert sorted(p.name for p in prev.iterdir()) == [R.MARKER, "keep.txt"]
    # a run that stopped after the marker (no demo.json yet) can be rerun into the same directory
    (prev / "dataset").mkdir()
    R.prepare_out(prev, check_only=True)
    R.prepare_out(prev)
    assert sorted(p.name for p in prev.iterdir()) == [R.MARKER, "keep.txt"]
    # only the demo's own entries, no marker (an older demo output)
    old = tmp_path / "old"
    (old / "zoo").mkdir(parents=True)
    (old / "shadows.parquet").write_text("x")
    R.prepare_out(old)
    assert sorted(p.name for p in old.iterdir()) == [R.MARKER]


def test_resolve_from_file_infers_name_and_symbols(tmp_path: Path) -> None:
    p = tmp_path / "01302020.NASDAQ_ITCH50.gz"
    p.write_bytes(b"")
    r = R.resolve(R.DemoOptions(from_file=str(p), cache_dir=str(tmp_path)))
    assert r.source.format == "itch" and r.source.session_date == "2020-01-30" and not r.remote
    assert r.symbols == ["AAPL", "MSFT", "SPY", "TSLA"]
    q = tmp_path / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    q.write_bytes(b"")
    r2 = R.resolve(R.DemoOptions(from_file=str(q), symbols=["spy"], cache_dir=str(tmp_path)))
    assert r2.source.format == "iex" and r2.source.session_date == "2026-10-09" and r2.symbols == ["SPY"]
    assert r2.source.attribution
    # only the file name counts, not the directories; the URL path is percent-decoded
    d = tmp_path / "mitchell"
    d.mkdir()
    m = d / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    m.write_bytes(b"")
    assert R.resolve(R.DemoOptions(from_file=str(m), symbols=["SPY"], cache_dir=str(tmp_path))).source.format == "iex"
    url = "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20261008%2F20261008_IEXTP1_DPLS1.0.pcap.gz?generation=1&alt=media"
    r3 = R.resolve(R.DemoOptions(url=url, source="iex", cache_dir=str(tmp_path)))
    assert r3.source.session_date == "2026-10-08" and r3.symbols == ["SPY", "AAPL", "NVDA", "TSLA"]
    # the file's size and modification time are part of the cache key
    q.write_bytes(b"changed")
    assert (
        R.resolve(R.DemoOptions(from_file=str(q), symbols=["spy"], cache_dir=str(tmp_path))).cache_entry
        != r2.cache_entry
    )


@pytest.mark.parametrize(
    ("kw", "word"),
    [
        ({"url": "https://x.example/capture.gz", "input_format": "iex"}, "--date"),
        ({"url": "https://x.example/20261009.gz"}, "--input-format"),
        ({"url": "https://x.example/12302019.gz", "source": "iex"}, "--input-format"),
    ],
)
def test_custom_inputs_never_borrow_a_builtin_date_or_format(kw: dict[str, str], word: str) -> None:
    with pytest.raises(QtError) as e:
        R.resolve(R.DemoOptions(**kw))  # type: ignore[arg-type]
    assert e.value.code == "QT-USAGE-001" and word in (e.value.hint or "")


def test_from_file_reads_the_format_from_the_bytes_and_the_rest_from_a_sidecar(tmp_path: Path) -> None:
    p = tmp_path / "capture.gz"
    p.write_bytes(gzip.compress(b"\x00\x0cS" + bytes(12)))
    r = R.resolve(R.DemoOptions(from_file=str(p), date="2019-12-30", cache_dir=str(tmp_path)))
    assert r.source.format == "itch" and r.symbols == ["AAPL", "MSFT", "SPY", "TSLA"]
    sub = tmp_path / "entry" / "subset.pcap.gz"
    sub.parent.mkdir()
    sub.write_bytes(b"")
    man = {"schema": "queuetruth.demo_subset", "subset": {"file": sub.name}, "symbols": ["SPY", "NVDA"],
           "source": {"format": "iex", "session_date": "2026-10-09"}}  # fmt: skip
    (sub.parent / "manifest.json").write_text(json.dumps(man))
    r2 = R.resolve(R.DemoOptions(from_file=str(sub), cache_dir=str(tmp_path)))
    assert (r2.source.format, r2.source.session_date, r2.symbols) == ("iex", "2026-10-09", ["SPY", "NVDA"])


def test_models_option_accepts_all_and_requires_the_headline_model() -> None:
    R.resolve(R.DemoOptions(models=["all"]))
    R.resolve(R.DemoOptions(models=["nt_prob_touch[p=1]"]))
    R.resolve(R.DemoOptions(models=["nt_prob_touch[mode=faithful;p=1]", "front_of_queue"]))
    with pytest.raises(QtError):
        R.resolve(R.DemoOptions(models=["nt_prob_touch[p=0.5]"]))


def test_custom_url_needs_consent_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = "https://example.invalid/12302019.NASDAQ_ITCH50.gz"
    args = ["demo", "--url", url, "--cache-dir", str(tmp_path), "--out", str(tmp_path / "o")]
    assert cli.main(["--format", "json", *args]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["code"] == "QT-USAGE-001" and "size not known in advance" in doc["message"]
    assert cli.main([*args, "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "TotalView-ITCH 5.0 (your input), 2019-12-30" in text and "size not known in advance" in text
