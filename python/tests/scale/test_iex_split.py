"""`scripts/scale/iex_split.py`: an IEX DEEP+ day routed into symbol buckets, each byte-identical to the
`IexCutFilter` subset of its symbols, ingestible with the ids the manifest names, and run through the whole W1
pipeline (plan, units, reduce, combine, site)."""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import sys
from pathlib import Path

import iex_split
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
import demo_fixtures as F

from queuetruth.demo.iex import IexCutFilter

DATE = "2026-10-09"
MMDD = "10092026"
SYMS = ["AAPL", "MSFT", "NVDA", "SPY", "TSLA"]
NEVER = 2**62


def _h(ts: int, sym: str, status: str, reason: str) -> bytes:
    """DEEP+ Trading Status (`H`, 22 bytes)."""
    return b"H" + status.encode() + struct.pack("<q", ts) + sym.encode().ljust(8) + reason.encode().ljust(4)


def _d_test(ts: int, sym: str) -> bytes:
    """Security Directory (`D`) of a test security (flag 0x80)."""
    return b"D" + b"\x80" + struct.pack("<q", ts) + sym.encode().ljust(8) + struct.pack("<Iqb", 100, 0, 1)


def iex_capture(*, gap_at: int | None = 7, duplicate_at: int | None = 11, seed: int = 5) -> bytes:
    """pcapng of a small DEEP+ day: five symbols trading 09:30-10:00, a test security, a LULD pause of NVDA, one
    source gap, one duplicate segment and heartbeats."""
    import random

    mid = F.midnight_utc_ns(DATE)
    rng = random.Random(seed)
    msgs: list[tuple[int, bytes]] = [(mid + F.hms(3, 0), F.iex_msg("S", mid + F.hms(3, 0), event=ord("O")))]
    for i, s in enumerate(SYMS):
        t = mid + F.hms(3, 5) + i
        msgs.append((t, F.iex_msg("D", t, s)))
    msgs.append((mid + F.hms(3, 6), _d_test(mid + F.hms(3, 6), "ZIEXT")))
    for i, s in enumerate(SYMS):
        t = mid + F.hms(3, 7) + i
        msgs.append((t, _h(t, s, "T", "")))
    msgs.append((mid + F.hms(9, 30), F.iex_msg("S", mid + F.hms(9, 30), event=ord("R"))))
    events = F.book_events(SYMS, F.hms(9, 30), F.hms(10, 0), mean_step_ns=200_000_000, seed=seed)
    for e in events:
        t = mid + e.ts
        if e.kind == "add":
            m = F.iex_msg("a", t, e.sym, side=ord(e.side), oid=e.oid, size=e.size, price=e.price)
        elif e.kind == "delete":
            m = F.iex_msg("R", t, e.sym, oid=e.oid)
        else:
            m = F.iex_msg("L", t, e.sym, oid=e.oid, size=e.size, price=e.price, trade=e.match)
        msgs.append((t, m))
    pause, resume = mid + F.hms(9, 45), mid + F.hms(9, 50)
    msgs += [(pause, _h(pause, "NVDA", "P", "LUDP")), (resume, _h(resume, "NVDA", "T", ""))]
    msgs.sort(key=lambda x: x[0])
    frames: list[tuple[int, bytes]] = []
    seq, i, k = 1, 0, 0
    while i < len(msgs):
        chunk = msgs[i : i + rng.randint(1, 4)]
        send = chunk[-1][0]
        seg = F.tp_segment(seq, [m for _, m in chunk], send)
        if k != gap_at:
            frames.append((send, F.eth_ipv4_udp(seg)))
        if k == duplicate_at:
            frames.append((send, F.eth_ipv4_udp(seg)))
        if k % 5 == 4:
            frames.append((send, F.eth_ipv4_udp(F.tp_segment(seq + len(chunk), [], send))))
        seq += len(chunk)
        i += len(chunk)
        k += 1
    return F.pcapng(frames)


def _split(tmp: Path, raw: bytes, k: int, symbols: str | None = None) -> dict:
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    F.write_gz(src, raw, members=2)
    a = argparse.Namespace(
        input=str(src), url=None, date=None, buckets=k, out=tmp / "split", symbols=symbols, level=1, min_free_gib=0.0
    )
    assert iex_split.run_split(a) == 0
    return json.loads((tmp / "split" / MMDD / "split_manifest.json").read_text())


def test_buckets_are_the_subset_filter_of_their_symbols(tmp_path: Path) -> None:
    raw = iex_capture()
    m = _split(tmp_path, raw, 3)
    assert m["ok"], {k: v for k, v in m["checks"].items() if not v["ok"]}
    assert m["date"] == MMDD and m["format"] == "iex_deep_plus" and m["ingest"] == iex_split.INGEST
    assert m["stream"]["sequence_gaps"] == 1 and m["stream"]["duplicate_messages"] > 0
    seen: set[str] = set()
    for b in m["buckets"]:
        syms = list(b["symbols"])
        assert all(iex_split.bucket_of(s, 3) == b["bucket"] for s in syms)
        assert not seen & set(syms)
        seen |= set(syms)
        ref = IexCutFilter(syms or ["NONE"], NEVER, None)
        ref.feed(raw)
        st = ref.finish()
        assert b["content_sha256"] == st["content_sha256"], b["bucket"]
        content = gzip.decompress((tmp_path / "split" / MMDD / b["file"]).read_bytes())
        import hashlib

        assert hashlib.sha256(content).hexdigest() == b["content_sha256"]
        # system events go to every bucket
        assert b["messages"] == sum(v[1] for v in b["symbols"].values()) + m["routing"]["system_messages"]
    assert seen == {*SYMS, "ZIEXT"}
    assert m["routing"]["test_issues"] == ["ZIEXT"]
    pause = 9 * 3600 + 45 * 60
    assert m["trading_state"]["non_t_spans"] == {"NVDA": [[pause * F.NS, (pause + 300) * F.NS, "P", "LUDP"]]}
    assert m["stream"]["system_events_ns_since_midnight"]["R"] == (9 * 3600 + 30 * 60) * F.NS
    # executions are the L messages
    n_exec = sum(v[2] for b in m["buckets"] for v in b["symbols"].values())
    assert n_exec > 0
    # deterministic: a second split of the same bytes writes the same files
    m2 = _split(tmp_path / "again", raw, 3)
    assert [b["file_sha256"] for b in m2["buckets"]] == [b["file_sha256"] for b in m["buckets"]]


def test_symbol_filter_and_bad_input(tmp_path: Path) -> None:
    m = _split(tmp_path, iex_capture(gap_at=None, duplicate_at=None), 2, symbols="AAPL,TSLA")
    assert m["ok"] and {s for b in m["buckets"] for s in b["symbols"]} == {"AAPL", "TSLA"}
    assert m["routing"]["dropped_messages"] > 0
    bad = tmp_path / "20261009_bad.pcap"
    bad.write_bytes(b"not a capture at all" * 10)
    a = argparse.Namespace(
        input=str(bad), url=None, date=None, buckets=2, out=tmp_path / "x", symbols=None, level=1, min_free_gib=0.0
    )
    assert iex_split.run_split(a) == iex_split.EXIT_INPUT
    assert not list((tmp_path / "x" / MMDD).glob("*.pcap.gz*"))


@pytest.mark.requires_core
def test_bucket_ingest_equals_subset_ingest_and_ids_match(tmp_path: Path) -> None:
    import queuetruth as qt

    raw = iex_capture()
    m = _split(tmp_path, raw, 2)
    spec = {"venue": "iex", "price_exp": -9, "tick_size": "0.01", "lot_size": "1"}
    for b in m["buckets"]:
        syms = sorted(b["symbols"])
        ds_b = tmp_path / f"b{b['bucket']}"
        qt.ingest(
            {
                "format": "iex_deep_plus",
                "inputs": [{"path": str(tmp_path / "split" / MMDD / b["file"])}],
                "out": str(ds_b),
                "symbols": syms,
                **spec,
            }
        )
        sub = tmp_path / f"sub{b['bucket']}.pcap"
        with open(sub, "wb") as f:
            flt = IexCutFilter(syms, NEVER, f)
            flt.feed(raw)
        ds_s = tmp_path / f"s{b['bucket']}"
        qt.ingest(
            {"format": "iex_deep_plus", "inputs": [{"path": str(sub)}], "out": str(ds_s), "symbols": syms, **spec}
        )
        mb = json.loads((ds_b / "manifest.json").read_text())
        ms = json.loads((ds_s / "manifest.json").read_text())
        assert [p["files"] for p in mb["partitions"]] == [p["files"] for p in ms["partitions"]]
        dq_b, dq_s = (json.loads((d / "data_quality.json").read_text()) for d in (ds_b, ds_s))
        assert dq_b == dq_s and dq_b["sequence_gaps"] == 1, "the source gap, and none made by the split"
        import pyarrow.parquet as pq

        inst = {r["symbol"]: r["instrument_id"] for r in pq.read_table(ds_b / "instruments.parquet").to_pylist()}
        # the manifest names the ingest's instrument ids (a symbol with no book record, such as the directory-only
        # test security, has no instrument row)
        assert inst and inst == {s: v[0] for s, v in b["symbols"].items() if s in inst}
        if "NVDA" in syms:
            assert [h["instrument_id"] for h in dq_b["trading_halts"]] == [inst["NVDA"]]


@pytest.mark.requires_core
def test_iex_day_through_the_pipeline(tmp_path: Path) -> None:
    import atlas
    import atlas_site
    import plan as P
    from unit import run_unit

    m = _split(tmp_path, iex_capture(), 2)
    pl = tmp_path / "plan.json"
    P.main(
        ["--split", str(tmp_path / "split" / MMDD), "--out", str(pl), "--quota-rule", "fixed", "--fixed-n", "20",
         "--populations", "standard", "--modes", "faithful", "--unit-core-s", "1e9", "--exclude", "ZIEXT"]
    )  # fmt: skip
    plan = json.loads(pl.read_text())
    assert plan["sources"][0]["format"] == "iex_deep_plus"
    assert all(u["ingest"]["format"] == "iex_deep_plus" and u["ingest"]["venue"] == "iex" for u in plan["units"])
    assert {u["symbol"] for u in plan["universe"]} == set(SYMS)
    assert next(u for u in plan["universe"] if u["symbol"] == "NVDA")["non_t_spans"]
    for u in plan["units"]:
        run_unit(u, tmp_path / "split" / MMDD, tmp_path / "shards" / u["unit_id"], 1)
    r = atlas.reduce_day(plan, tmp_path / "shards", tmp_path / "day")
    assert r["units_valid"] == r["units_planned"] == len(plan["units"])
    res = atlas.combine([tmp_path / "day"], tmp_path / "atlas", bootstrap_b=50)
    assert res["days"] == 1 and res["symbol_days"] == len(SYMS)
    site = atlas_site.build(tmp_path / "atlas", tmp_path / "site", source="iex", horizon="1s")
    page = (tmp_path / "site" / "index.html").read_text()
    assert atlas_site.IEX_ATTRIBUTION in page and site["atlas_root"] == res["atlas_root"]
    assert m["buckets"][0]["file"].startswith(MMDD)


@pytest.mark.requires_core
def test_local_day_runs_an_iex_day_and_builds_the_site(tmp_path: Path) -> None:
    import local_day

    src = tmp_path / "20261009_IEXTP1_DPLS1.0.pcap.gz"
    F.write_gz(src, iex_capture())
    rc = local_day.main(
        ["--source", "iex", "--date", DATE, "--input", str(src), "--out", str(tmp_path / "run"), "--buckets", "2",
         "--workers", "1", "--threads", "1", "--min-free-gib", "0", "--flag-policy", "label", "--site"]
    )  # fmt: skip
    assert rc == 0
    rep = json.loads((tmp_path / "run" / MMDD / "local_day.json").read_text())
    assert {"split", "plan", "units", "reduce", "combine", "site"} <= set(rep["stages"])
    page = (tmp_path / "run" / MMDD / "site" / "index.html").read_text()
    assert atlas_site_attribution() in page
    assert not list((tmp_path / "run" / MMDD / "split" / MMDD).glob("*.pcap.gz")), "buckets deleted after the units"


def atlas_site_attribution() -> str:
    import atlas_site

    return atlas_site.IEX_ATTRIBUTION
