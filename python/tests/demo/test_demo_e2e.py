"""`queuetruth demo` end to end on the synthetic days (needs the compiled core): ingest, shadows, zoo, the answer,
`demo.json` and `demo.html`; the cache; determinism of `run_id` and of the numbers across thread counts; the
interval's own checks; the IEX path with its attribution; the CLI over a local HTTP server."""

from __future__ import annotations

import functools
import gzip
import http.server
import json
import math
import re
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from queuetruth.demo import answer as A
from queuetruth.demo.run import DemoOptions, run_demo
from queuetruth.demo.summary import DemoSummary

pytestmark = pytest.mark.requires_core
SYMS = ["AAPL", "MSFT"]


def _opts(path: Path, tmp: Path, out: str, **kw: object) -> DemoOptions:
    base: dict[str, object] = {"from_file": str(path), "symbols": SYMS, "minutes": 1, "n": 200, "threads": 1}
    return DemoOptions(**{**base, "out": str(tmp / out), "cache_dir": str(tmp / "cache"), **kw})  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def first(tmp_path_factory: pytest.TempPathFactory, itch_gz: Path) -> tuple[DemoSummary, Path]:
    tmp = tmp_path_factory.mktemp("demo")
    return run_demo(_opts(itch_gz, tmp, "out1")), tmp


def test_summary_and_files(first: tuple[DemoSummary, Path]) -> None:
    s, _ = first
    assert s.source.format == "itch" and s.source.session_date == "2019-12-30" and s.source.id == "custom"
    assert (
        s.window.placement_start == "09:35:00" and s.window.placement_end == "09:36:00" and s.window.cut == "09:41:10"
    )
    assert s.n_shadows == 200 and s.n_leaderboard is not None and 0 < s.n_leaderboard <= 200
    assert s.data.data_quality == "clean" and s.data.records > 0 and not s.data.cache_hit and not s.data.remote
    assert s.data.kept_messages > 0 and s.data.symbols == SYMS
    assert A.short_id(s.headline.model_id) == "nt_prob_touch[p=1]"
    assert "nautilus_trader 1.231.0" in s.headline.verified_against and "2.0.0rc7" in s.headline.verified_against
    assert sorted(s.headline.cells, key=int) == [
        "100000000",
        "1000000000",
        "10000000000",
        "60000000000",
        "300000000000",
    ]
    assert [m.model_id for m in s.models] == [
        "nt_prob_touch[p=1]",
        "nt_queue_l2",
        "hbt_power3[n=2]",
        "hbt_risk_adverse",
        "databento_mc[k=-0.8;particles=100]",
        "front_of_queue",
        "trade_through_only",
    ]
    assert s.reference is None and s.source.attribution is None
    assert re.fullmatch(r"[0-9a-f]{16}", s.run_id)
    doc = json.loads(Path(s.outputs.json_path).read_text())
    assert DemoSummary.model_validate(doc).model_dump(mode="json") == s.model_dump(mode="json")
    assert doc["schema"] == "qt.demo_summary"
    for p in (s.outputs.dataset, s.outputs.shadows, s.outputs.zoo, s.outputs.html):
        assert Path(p).exists()
    t = s.timings
    assert min(t.fetch_s, t.ingest_s, t.shadows_s, t.zoo_s, t.answer_s) >= 0 and t.total_s >= t.zoo_s


def test_headline_cells_match_the_zoo(first: tuple[DemoSummary, Path]) -> None:
    s, _ = first
    zoo = json.loads((Path(s.outputs.zoo) / "summary.json").read_text())
    entry = next(e for e in zoo["leaderboard"] if e["model_id"] == s.headline.model_id)
    for k, c in s.headline.cells.items():
        assert c.bias == entry["fill_rate_bias"][k]["value"]
        assert c.exact_fill_rate == entry["exact_fill_rate"][k]
        assert c.model_fill_rate == entry["model_fill_rate"][k]
        assert c.bias is not None and math.isclose(c.bias, c.model_fill_rate - c.exact_fill_rate, abs_tol=1e-12)
        assert c.ci_method == "cluster_bootstrap" and c.n == s.n_leaderboard
        assert c.ci_lo is not None and c.ci_hi is not None and c.ci_lo - 1e-12 <= c.bias <= c.ci_hi + 1e-12
    assert s.interval.clusters == 2  # two instruments x one minute
    assert not any("no 95% interval" in w for w in s.warnings)


def test_html_is_self_contained(first: tuple[DemoSummary, Path]) -> None:
    s, _ = first
    page = Path(s.outputs.html).read_text()
    assert "<script" not in page and "src=" not in page and "@import" not in page
    assert not re.search(r"href=\"https?://", page)  # no IEX link on an ITCH source
    assert s.headline.sentence.replace("'", "&#x27;") in page
    assert "prefers-color-scheme: dark" in page and s.run_id in page
    assert page.count("<tr>") >= 5 + len(s.models)
    assert "nautilus_trader 1.231.0" in page
    # a local input appears by its file name only: the page can be shared without the local path
    assert s.source.location.endswith("12302019.NASDAQ_ITCH50.gz") and "/" in s.source.location
    assert "<code>12302019.NASDAQ_ITCH50.gz</code>" in page and str(Path(s.source.location).parent) not in page


def test_second_run_uses_the_cache_and_is_deterministic(first: tuple[DemoSummary, Path], itch_gz: Path) -> None:
    s, tmp = first
    s2 = run_demo(_opts(itch_gz, tmp, "out2", threads=2))
    assert s2.data.cache_hit and s2.data.bytes_downloaded == 0
    assert s2.run_id == s.run_id and s2.zoo_run_id == s.zoo_run_id
    assert s2.headline.model_dump() == s.headline.model_dump()
    assert [m.model_dump() for m in s2.models] == [m.model_dump() for m in s.models]
    # rerun into the same output directory: the demo's own entries are replaced
    s3 = run_demo(_opts(itch_gz, tmp, "out2", seed=1))
    assert s3.run_id != s.run_id and Path(s3.outputs.json_path).exists()


def test_interval_refuses_truncated_labels(first: tuple[DemoSummary, Path], tmp_path: Path) -> None:
    s, _ = first
    zoo = tmp_path / "zoo"
    shutil.copytree(s.outputs.zoo, zoo)
    t = pq.read_table(zoo / "zoo_shadows.parquet")
    live = t["ts_live"].cast(pa.int64()).to_pylist()
    admin = t["ts_admin_end"].cast(pa.int64()).to_pylist()
    admin[0] = live[0] + 2_000_000_000  # one shadow's data ends 2 s after it went live
    col = pa.array(admin, type=pa.int64()).cast(t.schema.field("ts_admin_end").type)
    t = t.set_column(t.schema.get_field_index("ts_admin_end"), t.schema.field("ts_admin_end"), col)
    pq.write_table(t, zoo / "zoo_shadows.parquet")
    board = json.loads((zoo / "summary.json").read_text())["leaderboard"]
    cells, notes = A.cluster_intervals(zoo, board, h_max_ns=300 * 10**9)
    assert notes["shadows_with_truncated_labels"] == 1
    assert {x["reason"] for x in notes["no_interval"]} == {"truncated_labels"}
    assert all(c["ci_method"] == "none" for cs in cells.values() for c in cs.values() if c["n"])


def test_interval_refuses_non_finite_rows(first: tuple[DemoSummary, Path], tmp_path: Path) -> None:
    s, _ = first
    zoo = tmp_path / "zoo"
    shutil.copytree(s.outputs.zoo, zoo)
    t = pq.read_table(zoo / "zoo_predictions.parquet")
    p = t["p_fill"].to_pylist()
    mids = t["model_id"].to_pylist()
    i = next(i for i, m in enumerate(mids) if m == s.headline.model_id and p[i] is not None)
    p[i] = math.nan
    t = t.set_column(
        t.schema.get_field_index("p_fill"), t.schema.field("p_fill"), pa.array(p, t.schema.field("p_fill").type)
    )
    pq.write_table(t, zoo / "zoo_predictions.parquet")
    board = json.loads((zoo / "summary.json").read_text())["leaderboard"]
    cells, notes = A.cluster_intervals(zoo, board, h_max_ns=300 * 10**9)
    bad = [x for x in notes["no_interval"] if x["model_id"] == s.headline.model_id]
    assert bad and {x["reason"] for x in bad} == {"non_finite"}
    assert cells[s.headline.model_id][str(bad[0]["horizon_ns"])]["ci_method"] == "none"


def test_the_demos_own_subset_gives_the_same_answer_and_run_id(first: tuple[DemoSummary, Path], tmp_path: Path) -> None:
    s, _ = first
    # the cached subset itself: format, date and symbols come from the manifest next to it
    back = run_demo(DemoOptions(from_file=s.data.subset_path, minutes=1, n=200, threads=1, out=str(tmp_path / "a"),
                                cache_dir=str(tmp_path / "c")))  # fmt: skip
    assert back.data.symbols == SYMS and back.data.content_sha256 == s.data.content_sha256
    assert back.run_id == s.run_id and back.headline.cells == s.headline.cells
    # the same content at another path, with another gzip level
    raw = gzip.decompress(Path(s.data.subset_path).read_bytes())
    other = tmp_path / "elsewhere" / "12302019.copy.itch50.gz"
    other.parent.mkdir()
    other.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    assert other.read_bytes() != Path(s.data.subset_path).read_bytes()
    again = run_demo(_opts(other, tmp_path, "b"))
    assert again.run_id == s.run_id and again.source.location != s.source.location
    assert [m.model_dump() for m in again.models] == [m.model_dump() for m in s.models]


def test_a_failed_run_can_be_rerun_into_the_same_directory(first: tuple[DemoSummary, Path], itch_gz: Path) -> None:
    _, tmp = first
    out = tmp / "failed"
    with pytest.raises(Exception):  # noqa: B017 - the zoo rejects the model id after the output directory is set up
        run_demo(_opts(itch_gz, tmp, "failed", models=["nt_prob_touch[p=1]", "no_such_model"]))
    assert out.is_dir() and not (out / "demo.json").exists()
    s = run_demo(_opts(itch_gz, tmp, "failed"))
    assert (out / "demo.json").exists() and s.data.cache_hit


def test_interval_is_deterministic_and_seeded(first: tuple[DemoSummary, Path]) -> None:
    s, _ = first
    board = json.loads((Path(s.outputs.zoo) / "summary.json").read_text())["leaderboard"]
    a, _ = A.cluster_intervals(Path(s.outputs.zoo), board, h_max_ns=300 * 10**9, seed=0)
    b, _ = A.cluster_intervals(Path(s.outputs.zoo), board, h_max_ns=300 * 10**9, seed=0)
    c, _ = A.cluster_intervals(Path(s.outputs.zoo), board, h_max_ns=300 * 10**9, seed=5, b=500)
    assert a == b
    k = "1000000000"
    mid = s.headline.model_id
    assert a[mid][k]["value"] == c[mid][k]["value"]


def test_excluded_hbt_shadows_leave_every_model(first: tuple[DemoSummary, Path]) -> None:
    s, _ = first
    preds = pq.read_table(Path(s.outputs.zoo) / "zoo_predictions.parquet", columns=["shadow_id", "status"])
    aborted = set(preds.filter(pc.is_valid(preds["status"]))["shadow_id"].to_pylist())
    assert all(c.n == 200 - len(aborted) for m in s.models for c in m.cells.values())


def test_iex_end_to_end_carries_the_attribution(tmp_path: Path, iex_gz: Path) -> None:
    s = run_demo(_opts(iex_gz, tmp_path, "out"))
    assert s.source.format == "iex" and s.source.session_date == "2026-10-09"
    assert s.source.attribution and s.source.attribution.startswith("Data provided for free by IEX.")
    page = Path(s.outputs.html).read_text()
    assert "Data provided for free by IEX. By accessing or using IEX Historical Data" in page
    assert 'href="https://iextrading.com/iex-historical-data-terms/"' in page
    assert s.data.data_quality == "clean"
    from queuetruth.demo.cli import render

    assert "Data provided for free by IEX." in render(s)


@pytest.fixture
def served(itch_gz: Path, tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "www"
    root.mkdir()
    shutil.copy(itch_gz, root / itch_gz.name)

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/{itch_gz.name}"
    srv.shutdown()
    srv.server_close()


def test_cli_over_http_with_confirmation(
    served: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from queuetruth import cli
    from queuetruth.demo import cli as dcli

    args = ["demo", "--url", served, "--symbols", "AAPL,MSFT", "--minutes", "1", "--n", "100",
            "--cache-dir", str(tmp_path / "c"), "--out", str(tmp_path / "o")]  # fmt: skip
    monkeypatch.setattr(dcli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(dcli.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(dcli, "_ask", lambda _prompt: "n\n")
    assert cli.main(args) == 2
    assert "download not confirmed" in capsys.readouterr().err
    monkeypatch.setattr(dcli, "_ask", lambda _prompt: "y\n")
    assert cli.main(args) == 0
    out = capsys.readouterr()
    assert "Within 1 s, NautilusTrader's default fill model fills" in out.out
    assert "MB downloaded" in out.out and "Download now?" not in out.out
    # the cached subset needs no confirmation and no network, also without a terminal
    monkeypatch.setattr(dcli.sys.stdin, "isatty", lambda: False)
    assert cli.main([*args, "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.demo_summary" and doc["data"]["cache_hit"] is True and doc["data"]["remote"] is True


def test_the_demos_own_subset_over_http(first: tuple[DemoSummary, Path], tmp_path: Path) -> None:
    s, _ = first
    root = tmp_path / "www"
    root.mkdir()
    shutil.copy(s.data.subset_path, root / "12302019.subset.AAPL-MSFT.itch50.gz")

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/12302019.subset.AAPL-MSFT.itch50.gz"
        got = run_demo(DemoOptions(url=url, symbols=SYMS, minutes=1, n=200, threads=1, yes=True,
                                   out=str(tmp_path / "o"), cache_dir=str(tmp_path / "c")))  # fmt: skip
    finally:
        srv.shutdown()
        srv.server_close()
    assert got.data.remote and not got.data.cache_hit and got.data.bytes_downloaded == s.data.subset_bytes
    assert got.run_id == s.run_id and got.headline.cells == s.headline.cells


def test_html_escapes_input_text(first: tuple[DemoSummary, Path]) -> None:
    from queuetruth.demo.html import render_html

    s, _ = first
    evil = s.model_copy(deep=True)
    evil.source.location = 'https://x/"><script>alert(1)</script>'
    evil.data.symbols = ["<b>AAPL</b>"]
    page = render_html(evil)
    assert "<script" not in page and "<b>AAPL" not in page and "&lt;script&gt;" in page
