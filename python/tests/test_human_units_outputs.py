"""Human units in what people read (plan section 3, principle 7): the CLI text of the everyday commands and the HTML
reports, run on the synthetic fixture `tests/fixtures/synth_small`, show horizons as `1s`, times as venue-local wall
time with the zone named and prices as decimals. The scan fails on a nanosecond horizon next to a horizon word, a
raw epoch-nanosecond timestamp, or an integer price of the fixture's book printed unscaled."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from queuetruth import cli

pytestmark = pytest.mark.requires_core

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "fixtures" / "synth_small"
LIVE = DATA / "own_orders" / "live"

#: A horizon word followed (within a few words) by a 9+ digit integer (not an order id), the integer followed by
#: `ns` or `horizon`, or a `<ns>=<verdict>` pair.
_HORIZON_WORDS = r"(?:horizon|markout|within|window|latency|at|after|h)"
_NS_HORIZON = re.compile(
    rf"\b{_HORIZON_WORDS}\b[^\n\d]{{0,24}}(?<!order )(?<!id )\b\d{{9,}}\b(?!\.)"
    rf"|\b\d{{9,}}\b(?!\.)[^\n\d]{{0,12}}\b(?:ns|horizon)\b|\b\d{{9,}}=(?:better|tie|worse|mixed)",
    re.IGNORECASE,
)
#: A `*_ns` field of a dump printed as a raw integer of 6 or more digits.
_NS_FIELD = re.compile(r"\b\w*_ns(?:\.\w+)?: -?\d{6,}\b")
#: An epoch timestamp in nanoseconds (2001 to 2286): 19 digits starting with 1 or 2.
_EPOCH_NS = re.compile(r"(?<![\w.])[12]\d{18}(?![\w.])")


def _book_prices() -> set[str]:
    """Every integer price of the fixture's book (fixed point at 10^-9), as text: printing one of these is an
    unscaled price."""
    out: set[str] = set()
    for p in sorted((DATA / "mbo").rglob("*.parquet")):
        col = pq.read_table(p, columns=["price"]).column("price")
        for v in pc.unique(col).to_pylist():
            if v is not None and abs(int(v)) >= 10**6:
                out.add(str(int(v)))
    return out


def offenders(text: str, prices: set[str]) -> list[str]:
    """The raw integers in `text` that a person should not have to decode, with their context."""
    bad: list[str] = []
    for line in text.splitlines():
        if "/" in line and re.search(r"(?:/[\w.-]+){2,}", line):
            line = re.sub(r"(?:[A-Za-z]:)?(?:/[\w.@+-]+)+/?", "<path>", line)  # file paths may hold digits
        for rx, what in ((_NS_HORIZON, "ns horizon"), (_EPOCH_NS, "epoch ns"), (_NS_FIELD, "ns field")):
            for m in rx.finditer(line):
                bad.append(f"{what} {m.group()!r}: {line.strip()[:160]}")
        for tok in re.findall(r"(?<![\w.])-?\d{7,}(?![\w.])", line):
            if tok.lstrip("-") in prices:
                bad.append(f"unscaled price {tok}: {line.strip()[:160]}")
    return bad


def _html_text(path: Path) -> str:
    """The visible text of an HTML page (tags, scripts and styles dropped; attributes ignored)."""
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", "\n", raw)
    return html.unescape(raw)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """name -> human text of each command (stdout and stderr) and each HTML report."""
    from check import synth_backtests as sb

    capsys_out: dict[str, str] = {}
    tmp = tmp_path_factory.mktemp("human")
    import contextlib
    import io

    def run(name: str, *argv: str, ok: Callable[[int], bool] = lambda c: c == 0) -> None:
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            code = cli.main(list(argv))
        assert ok(code), (name, code, e.getvalue()[-2000:])
        capsys_out[name] = o.getvalue() + "\n" + e.getvalue()

    ds = str(DATA)
    sh = str(tmp / "sh.parquet")
    run("shadows sample", "shadows", "sample", "--data", ds, "--n", "120", "--out", sh, "--seed", "3")
    zoo = str(tmp / "zoo")
    models = "front_of_queue,hbt_power3[n=2],nt_prob_touch[p=1]"
    run("zoo", "zoo", "--data", ds, "--shadows", sh, "--out", zoo, "--models", models, "--bootstrap-b", "20")
    run("zoo --verbose", "--verbose", "zoo", "--data", ds, "--shadows", sh, "--out", str(tmp / "zoo2"),
        "--models", "front_of_queue", "--bootstrap-b", "5")  # fmt: skip
    run("zoo-toxicity", "zoo-toxicity", "--zoo", zoo, "--data", ds, "--out", str(tmp / "tox"), "--bootstrap-b", "10")
    sid = pq.read_table(sh).column("shadow_id")[0].as_py()
    run("why shadow", "why", "--data", ds, "--shadows", sh, "--shadow-id", sid, "--zoo", zoo)
    key = pq.read_table(DATA / "own_orders" / "schedule.parquet").column("order_key")[0].as_py()
    run("why live", "why", "--data", ds, "--live", str(LIVE), "--order-key", key, "--model", "hbt_power3[n=2]")
    sc = str(tmp / "score")
    run("score", "score", "--live", str(LIVE), "--sim-model", "hbt_power3[n=2]", "--data", ds, "--out", sc,
        "--bootstrap-b", "30", "--perm-b", "30")  # fmt: skip
    run("score --verbose", "--verbose", "score", "--live", str(LIVE), "--sim-model", "front_of_queue", "--data", ds,
        "--out", str(tmp / "score2"), "--bootstrap-b", "30", "--perm-b", "30")  # fmt: skip
    cal = str(tmp / "cal")
    run("calibrate", "calibrate", "alloc", "--family", "power3", "--data", ds, "--shadows", sh, "--bootstrap-b", "10",
        "--out", cal, "--train", "2026-06-15", "--test", "2026-06-16", ok=lambda c: c in (0, 1, 2))  # fmt: skip
    rep = tmp / "report.html"
    run("report", "report", "--in", zoo, "--in", sc, "--in", str(tmp / "tox"), "--in", cal, "--out", str(rep))
    capsys_out["report.html"] = _html_text(rep)

    bt = sb.write_nautilus(tmp / "orders_report.csv", sb.orders(DATA))
    chk = tmp / "check"
    run("check", "check", str(bt), "--data", ds, "--out", str(chk), "--window", "10s", "--html")
    capsys_out["check.html"] = _html_text(chk / "check.html")
    run("fit", "fit", "--backtester", "nautilus", "--data", ds, "--out", str(tmp / "fit"))
    dq = tmp / "dq"
    run("dq", "dq", "--data", ds, "--out", str(dq), ok=lambda c: c in (0, 1))
    for page in sorted(dq.glob("*.html")):
        capsys_out[f"dq {page.name}"] = _html_text(page)

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_atlas_site", Path(__file__).parent / "atlas" / "test_atlas_lookup.py"
    )
    assert spec is not None and spec.loader is not None
    atlas_tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(atlas_tests)
    _write_api = atlas_tests._write_api

    site = str(_write_api(tmp / "site"))
    run("atlas", "atlas", "--library", "hftbacktest", "--from", site)
    run("atlas --symbol", "atlas", "--symbol", "ZZZA", "--from", site)
    run("atlas --horizon", "atlas", "--library", "nautilus", "--horizon", "1000000000", "--from", site,
        ok=lambda c: c in (0, 2))  # fmt: skip
    from check import synth_atlas as sa

    est = tmp / "check_est"
    touch_site = str(sa.write_atlas(tmp / "touch_site", symbols={"QTAU6": "T1_top100"}))
    run("check without data", "check", str(bt), "--atlas", touch_site, "--bar", "5min", "--tier", "ALL", "--out",
        str(est), "--html")  # fmt: skip
    capsys_out["check without data html"] = _html_text(est / "check.html")

    import sys

    sys.path.insert(0, str(Path(__file__).parent / "demo"))
    import demo_fixtures as df

    raw = tmp / "12302019.NASDAQ_ITCH50.gz"
    df.write_gz(raw, df.itch_day(["AAPL", "MSFT"], df.book_events(["AAPL", "MSFT"], df.hms(9, 30), df.hms(9, 35))))
    run("data add", "data", "add", str(raw), "--out", str(tmp / "ds_added"), "--quiet")
    return capsys_out


def test_the_scan_catches_raw_units() -> None:
    """The scan itself: it flags the raw forms and passes the human ones."""
    prices = {"5000250000000"}
    assert offenders("fill-rate bias at 60000000000: +1.2 pp", prices)
    assert offenders("markouts: horizon 1000000000 ns", prices)
    assert offenders("trust: beats_default: 100000000=tie,1000000000=better", prices)
    assert offenders("equivalence.markout_horizon_ns: 1000000000", prices)
    assert not offenders("filled after M of order 1002000006984", prices)
    assert offenders("live 1781533800123456789", prices)
    assert offenders("price 5000250000000", prices)
    assert not offenders("fill-rate bias at 1min: +1.2 pp; price 5000.25; 1,234,567,890 rows", prices)
    assert not offenders("live 2026-06-15 13:30:00.123456 UTC, run 0123456789abcdef", prices)


def test_cli_text_and_html_show_human_units(corpus: dict[str, Any]) -> None:
    prices = _book_prices()
    assert prices, "the fixture has no prices"
    found = {name: offenders(text, prices) for name, text in corpus.items()}
    found = {k: v for k, v in found.items() if v}
    assert not found, "\n".join(f"{k}:\n  " + "\n  ".join(v[:8]) for k, v in found.items())
