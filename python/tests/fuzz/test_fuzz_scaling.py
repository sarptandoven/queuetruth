"""Input-size property: every parser takes time roughly linear in its input. Each example builds a repetitive input
(a fixture prefix cut at a random point, then one token repeated: digit runs, separators, tag starts, brackets,
quotes) at two sizes, 8x apart, and times both. A quadratic parser (a regex that rescans a digit run from every
position, a string rebuilt per token) takes about 64x longer on the larger input; the property allows 16x plus a
fixed 0.25 s for timer noise on a loaded laptop. The other properties use inputs of a few KB, where super-linear
behaviour is invisible."""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fuzz._harness import guarded, scaled, workdir
from fuzz._mutate import LIVE_FIXTURES
from hypothesis import given
from hypothesis import strategies as st

from queuetruth.live import import_sim, import_venue
from queuetruth.live._common import read_json_records
from queuetruth.live.imap import load_instrument_map
from queuetruth.live.importers import ImportOptions, fixwire, quickfix
from queuetruth.live.importers.fixdict import load_dictionary
from queuetruth.live.importers.ibkr_flex import import_ibkr_flex

SMALL = 16 << 10
FACTOR = 8
MAX_RATIO = 16
NOISE_S = 0.25

TOKENS = [
    b"7",
    b"7,",
    b"7:",
    b"7 ",
    b" ",
    b"\x01",
    b"1=1\x01",
    b"8=FIX.4.4\x019=",
    b"10=",
    b"{",
    b"[",
    b'"',
    b'\\"',
    b"a",
    b"<a>",
    b"<a b='1'",
    b",",
    b"\n",
    b"\r",
    b"\r\n7,",
    b"2026-",
]


def _quiet(fn: Callable[[], Any]) -> Callable[[], Any]:
    def run() -> Any:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return fn()

    return run


def _venue(venue: str, name: str = "x.jsonl") -> Callable[[Path, bytes], Any]:
    def run(d: Path, data: bytes) -> Any:
        (d / name).write_bytes(data)
        return import_venue({"venue": venue, "inputs": [{"path": str(d / name)}], "out": str(d / "o")})

    return run


def _store_header(d: Path, data: bytes) -> Any:
    (d / "x.body").write_bytes(b"8=FIX.4.4\x019=5\x0135=0\x0110=000\x01")
    (d / "x.header").write_bytes(data)
    return quickfix.read_store(str(d / "x.body"), str(d / "x.header"))


def _store_body(d: Path, data: bytes) -> Any:
    (d / "x.body").write_bytes(data)
    return quickfix.read_store(str(d / "x.body"), None)


def _file(fn: Callable[[Path], Any], name: str) -> Callable[[Path, bytes], Any]:
    def run(d: Path, data: bytes) -> Any:
        (d / name).write_bytes(data)
        return fn(d / name)

    return run


def _seed(rel: str) -> bytes:
    return (LIVE_FIXTURES / rel).read_bytes()


#: target -> (runner, seed whose prefix starts the input)
TARGETS: dict[str, tuple[Callable[[Path, bytes], Any], bytes]] = {
    "quickfix.header": (_store_header, b"1,0,23 "),
    "quickfix.seqnums": (_file(lambda p: quickfix.read_seqnums(str(p), "store_seqnums"), "x.seqnums"), b"1 : "),
    "quickfix.body": (_store_body, b"8=FIX.4.4\x019=5\x01"),
    "quickfix.event_log": (_file(lambda p: quickfix.read_event_log(str(p)), "x.event.log"), b"20261015-13:30:00 : "),
    "fix44.messages_log": (
        _venue("fix44", "FIX.4.4-QTCLIENT-VENUE.messages.log"),
        _seed("fix44/01_soh_order_entry_log/session_soh.log"),
    ),
    "fixwire.frame": (lambda d, b: fixwire.frame(b.decode("latin-1"), 0), b"8=FIX.4.4\x019=12\x01"),
    "fixwire.parse_fields": (lambda d, b: fixwire.parse_fields(b.decode("latin-1")), b"35=D\x0195=3\x0196="),
    "fixdict": (_file(lambda p: load_dictionary(str(p)), "FIX44.xml"), b'<fix type="FIX" major="4" minor="4"><fields>'),
    "json_records": (_file(lambda p: read_json_records(p, 0), "x.jsonl"), b'{"recv_ns": 1792071000000000000, "msg": '),
    "binance": (_venue("binance"), _seed("binance/01_ws_execution_report_lifecycle/user_data_stream.jsonl")),
    "coinbase": (_venue("coinbase"), b'{"channel":"user","events":[{"type":"update","orders":['),
    "flex.xml": (
        _file(lambda p: import_ibkr_flex([str(p)], ImportOptions(account="a")), "flex.xml"),
        _seed("ibkr_flex/01_flex_xml_executions/flex_trades.xml"),
    ),
    "flex.csv": (
        _file(lambda p: import_ibkr_flex([str(p)], ImportOptions(account="a")), "flex.csv"),
        b"accountId,symbol,buySell,quantity,tradePrice,dateTime\nU1,ACME,BUY,1,10,20261015;093001\n",
    ),
    "sim.csv": (
        _file(lambda p: import_sim("nautilus", [str(p)], p.parent / "o", account="a"), "x.csv"),
        b"client_order_id,type,ts_event\nO-1,OrderAccepted,1792071000000000000\n",
    ),
    "imap": (
        _file(load_instrument_map, "instrument_map.csv"),
        b"venue,instrument,instrument_id,session_date,price_exp,size_exp,tick_size,lot_size\nx,ACME,1,,-2,0,0.01,1\n",
    ),
}


def _timed(run: Callable[[Path, bytes], Any], data: bytes, what: str) -> float:
    """Best of two runs (seconds), each in a fresh directory under the fuzz contract."""
    best = float("inf")
    for _ in range(2):
        with workdir() as d:
            t0 = time.perf_counter()
            guarded(what, _quiet(lambda d=d: run(d, data)), input_bytes=len(data))
            best = min(best, time.perf_counter() - t0)
    return best


@scaled(1.5)
@given(st.sampled_from(sorted(TARGETS)), st.sampled_from(TOKENS), st.floats(0, 1))
def test_time_is_linear_in_input_size(target: str, token: bytes, cut: float) -> None:
    run, seed = TARGETS[target]
    prefix = seed[: int(len(seed) * cut)]

    def build(size: int) -> bytes:
        return prefix + token * max(1, (size - len(prefix)) // len(token))

    small = _timed(run, build(SMALL), f"scaling:{target}")
    large = _timed(run, build(SMALL * FACTOR), f"scaling:{target}")
    assert large <= MAX_RATIO * small + NOISE_S, (
        f"{target}: {SMALL * FACTOR} bytes of {token!r} took {large:.3f} s, {large / max(small, 1e-9):.0f}x the "
        f"{small:.3f} s of {SMALL} bytes (linear is about {FACTOR}x)"
    )
