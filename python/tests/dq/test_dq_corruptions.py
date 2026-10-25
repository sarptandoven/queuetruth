"""`queuetruth dq` on injected corruptions: every check flags its defect, with the expected counts and evidence
rows, and leaves the other checks alone. Corruptions are applied Python-side to copies of the committed synthetic
fixtures (`dq.helpers.edit_partition` rewrites one partition and refreshes the manifest) or to tiny hand-made
datasets (`dq.helpers.make_dataset`)."""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from dq.helpers import (
    Cols,
    check,
    edit_instruments,
    edit_partition,
    evidence,
    insert_rows,
    make_dataset,
    part_path,
    read_cols,
    rec,
    refresh_manifest,
    take,
    write_cols,
)
from queuetruth.dq import run

D1, D2 = "2026-06-15", "2026-06-16"
B = 20_000  # an event boundary of (D1, instrument 1): rows 20000 and 20001 are single-record events
LAST = 128
NS = 10**9


def dq(ds: Path, *checks: str, replay: bool = False, **kw: object):
    spec = {"dataset": {"path": str(ds)}, "replay": replay, **kw}
    if checks:
        spec["checks"] = list(checks)
    return run(spec)


def boundary(c: Cols, at: int) -> int:
    return next(i for i in range(at, len(c["flags"])) if c["flags"][i - 1] & LAST)


# ---- sequence ---------------------------------------------------------------------------------------------------


def test_sequence_gap(nasdaq: Path) -> None:
    def shift(c: Cols) -> Cols:
        c["sequence"][B:] += 5
        return c

    edit_partition(nasdaq, D1, 1, shift)
    c = check(dq(nasdaq, "sequence"), "sequence")
    assert c.status == "warn"
    assert (c.metrics["gaps"], c.metrics["missing_messages"], c.metrics["regressions"]) == (1, 5, 0)
    row = dict(zip(evidence(c, "gaps").columns, evidence(c, "gaps").rows[0], strict=True))
    assert (row["date"], row["sequence_after"] - row["sequence_before"], row["instruments"]) == (D1, 6, "1")


def test_sequence_gap_fails_above_ratio(nasdaq: Path) -> None:
    edit_partition(
        nasdaq,
        D1,
        1,
        lambda c: {
            **c,
            "sequence": c["sequence"]
            + np.where(np.arange(len(c["sequence"])) >= B, 1000, 0).astype(c["sequence"].dtype),
        },
    )
    c = check(dq(nasdaq, "sequence"), "sequence")
    assert c.metrics["missing_messages"] == 1000
    assert c.status == "fail"  # 1000 missing of ~198k records > 1e-3


def test_sequence_duplicate_regression_and_collision(nasdaq: Path) -> None:
    def dup_and_swap(c: Cols) -> Cols:
        s = c["sequence"]
        s[B], s[B + 1] = s[B + 1], s[B]  # two single-record events out of order: one regression, no gap
        return insert_rows(c, 30_000, [{}])  # exact copy of record 29999

    edit_partition(nasdaq, D1, 1, dup_and_swap)

    def collide(c: Cols) -> Cols:
        # a record moved onto instrument 1's channel with a number of instrument 1's range
        c["sequence"][100], c["channel_id"][100] = 142_700, 0
        return c

    edit_partition(nasdaq, D1, 2, collide)
    c = check(dq(nasdaq, "sequence"), "sequence")
    assert c.status == "warn"
    assert c.metrics["duplicate_records"] == 1
    dup = dict(zip(evidence(c, "duplicate_records").columns, evidence(c, "duplicate_records").rows[0], strict=True))
    assert (dup["row"], dup["first_row"], dup["instrument_id"]) == (30_000, 29_999, 1)
    assert c.metrics["regressions"] >= 1
    reg = evidence(c, "sequence_regressions").rows
    assert any(r[1] == 1 and r[3] == B + 1 for r in reg)
    assert c.metrics["cross_instrument_collisions"] == 1
    assert evidence(c, "cross_instrument_collisions").rows[0][2:] == [142_700, "1,2"]


def test_sequence_per_instrument_and_filtered_numbering(nasdaq: Path) -> None:
    # instrument 2 numbered from the same start as instrument 1: per-instrument numbering, not collisions
    c1, _ = read_cols(part_path(nasdaq, D1, 1))
    start = int(c1["sequence"][0])
    edit_partition(
        nasdaq,
        D1,
        2,
        lambda c: {**c, "sequence": c["sequence"] - c["sequence"][0] + start, "channel_id": c["channel_id"] * 0},
    )
    c = check(dq(nasdaq, "sequence"), "sequence")
    assert c.status == "pass", c.summary
    assert c.metrics["cross_instrument_collisions"] == 0
    assert any("per-instrument sequence numbering" in n for n in c.notes)
    # a symbol-filtered feed: instrument 1 of day 2 holds every 10th number -> holes are not gaps
    edit_partition(nasdaq, D2, 1, lambda c: {**c, "sequence": c["sequence"] * 10})
    c = check(dq(nasdaq, "sequence"), "sequence")
    assert c.metrics["gaps"] == 0
    assert any("sparse share" in n for n in c.notes)


def test_rule_sets_mirror_the_venue_table() -> None:
    from queuetruth.dq.data import RULES

    assert set(RULES) == {"nasdaq", "cme", "coinbase", "generic", "crypto_l3", "iex", "nyse"}
    assert RULES["iex"] == RULES["nasdaq"]  # IEX DEEP+ runs on Nasdaq record semantics (schema 1.2)
    # NYSE TAQ: Nasdaq record semantics; the channel sequence has holes for omitted control messages (schema 1.2)
    assert RULES["nyse"] == {**RULES["nasdaq"], "sequence_meaningful": False}
    assert [RULES[v]["sequence_meaningful"] for v in ("nasdaq", "cme", "coinbase", "generic")] == [1, 1, 1, 0]
    assert RULES["cme"]["snapshot_begins_with_clear"] and not RULES["crypto_l3"]["supports_hidden_orders"]


def test_sequence_not_checked_for_itch_but_duplicates_are(tmp_path: Path) -> None:
    t0 = 1_781_530_000 * NS
    recs = [rec(t0 + i * NS, "A", "B", 150_000_000_000, 100, i + 1, 10 * i) for i in range(5)]
    recs.append(dict(recs[-1]))
    ds = make_dataset(tmp_path / "ds", {(D1, 7): recs}, {7: "QTS"}, source_format="itch")
    c = check(dq(ds, "sequence"), "sequence")
    assert c.metrics["sequence_checked"] is False
    assert (c.metrics["gaps"], c.metrics["duplicate_records"]) == (0, 1)
    assert any("assigned by the reader" in n for n in c.notes)


# ---- book: crossed books, orphans, stale book, trade parity -----------------------------------------------------

CROSS_PX = 1_500_000_000_000  # far above every ask of the fixture, on the tick grid


def _cross(c: Cols, *, cancel: bool) -> Cols:
    rows = [
        {"action": ord("A"), "side": ord("B"), "price": CROSS_PX, "size": 100, "order_id": 9_000_000_001, "flags": LAST}
    ]
    if cancel:
        rows.append(
            {
                "action": ord("C"),
                "side": ord("B"),
                "price": CROSS_PX,
                "size": 100,
                "order_id": 9_000_000_001,
                "flags": LAST,
            }
        )
    return insert_rows(c, B, rows)


@pytest.mark.requires_core
def test_crossed_book_warns_then_fails(nasdaq: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    shutil.copytree(nasdaq, other)
    edit_partition(nasdaq, D1, 1, lambda c: _cross(c, cancel=True))
    c = check(dq(nasdaq, "crossed_book", replay=True), "crossed_book")
    assert c.status == "warn"
    assert (c.metrics["crossed"], c.metrics["locked"]) == (1, 0)
    ev = evidence(c, "book_states")
    row = dict(zip(ev.columns, ev.rows[0], strict=True))
    assert (row["state"], row["instrument_id"], row["bid_px"]) == ("crossed", 1, CROSS_PX)
    # left resting, the bid crosses every later event end of the partition: unusable share
    edit_partition(other, D1, 1, lambda c: _cross(c, cancel=False))
    c = check(dq(other, "crossed_book", replay=True), "crossed_book")
    assert c.status == "fail" and c.metrics["crossed"] > 1000


@pytest.mark.requires_core
def test_orphan_references(nasdaq: Path) -> None:
    unknown = [
        {"action": ord("C"), "side": ord("A"), "size": 10, "order_id": 8_000_000_000 + i, "flags": LAST}
        for i in range(3)
    ]
    edit_partition(nasdaq, D1, 1, lambda c: insert_rows(c, B, unknown))
    c = check(dq(nasdaq, "orphan_refs", replay=True), "orphan_refs")
    assert c.status == "warn"
    assert (c.metrics["orphans_scan"], c.metrics["orphans_C"], c.metrics["replay_unknown_order_refs"]) == (3, 3, 3)
    ev = evidence(c, "records")
    assert [r[-1] for r in ev.rows] == [8_000_000_000, 8_000_000_001, 8_000_000_002]
    assert all(r[2] in (B, B + 1, B + 2) for r in ev.rows)


def test_orphan_references_fail_above_ratio(nasdaq: Path) -> None:
    many = [
        {"action": ord("F"), "side": ord("A"), "size": 1, "order_id": 7_000_000_000 + i, "flags": LAST}
        for i in range(300)
    ]
    edit_partition(nasdaq, D1, 1, lambda c: insert_rows(c, B, many))
    c = check(dq(nasdaq, "orphan_refs"), "orphan_refs")
    assert c.status == "fail" and c.metrics["orphans_F"] == 300
    assert evidence(c, "records").truncated and len(evidence(c, "records").rows) == 20


def test_orphan_scan_respects_clears(tmp_path: Path) -> None:
    t0 = 1_781_530_000 * NS
    px = 150_000_000_000
    recs = [
        rec(t0, "A", "B", px, 100, 1, 1),
        rec(t0 + 1, "C", "B", px, 50, 1, 2),  # known
        rec(t0 + 2, "R", "N", 2**63 - 1, 0, 0, 3),
        rec(t0 + 3, "C", "B", px, 50, 1, 4),  # the clear removed order 1: orphan
        rec(t0 + 4, "M", "A", px + 10_000_000, 10, 2, 5),  # unknown modify: orphan, then acts as an add
        rec(t0 + 5, "F", "A", px + 10_000_000, 10, 2, 6),  # known through the modify
    ]
    ds = make_dataset(tmp_path / "ds", {(D1, 3): recs}, {3: "QTS"})
    c = check(dq(ds, "orphan_refs"), "orphan_refs")
    assert (c.metrics["orphans_scan"], c.metrics["orphans_C"], c.metrics["orphans_M"]) == (2, 1, 1)
    assert [r[2] for r in evidence(c, "records").rows] == [3, 4]


def test_stale_book_quiet_interval(nasdaq: Path) -> None:
    open_ns = 1_781_530_200 * NS  # 2026-06-15 09:30 New York

    def quiet(c: Cols) -> Cols:
        ts, act = c["ts_event"], c["action"]
        keep = ~((ts >= open_ns + 3600 * NS) & (ts < open_ns + 3780 * NS) & (act != ord("T")))
        return take(c, np.nonzero(keep)[0])

    edit_partition(nasdaq, D1, 1, quiet)
    c = check(dq(nasdaq, "stale_book"), "stale_book")
    assert c.status == "warn"
    assert c.metrics["quiet_intervals"] >= 1 and c.metrics["stale_prints"] >= 1
    row = dict(zip(evidence(c, "quiet_intervals").columns, evidence(c, "quiet_intervals").rows[0], strict=True))
    assert row["instrument_id"] == 1 and row["quiet_ns"] > 60 * NS
    assert open_ns + 3600 * NS - 60 * NS <= row["quiet_from"] <= open_ns + 3600 * NS + 60 * NS


@pytest.mark.requires_core
def test_stale_book_print_through_the_book(nasdaq: Path) -> None:
    c0, _ = read_cols(part_path(nasdaq, D1, 1))
    t = next(i for i in range(B, B + 500) if c0["action"][i] == ord("T") and c0["side"][i] == ord("B"))

    def through(c: Cols) -> Cols:
        c["price"][t] += 50 * 10_000_000  # a buy 50 ticks above where it really printed
        return c

    edit_partition(nasdaq, D1, 1, through)
    c = check(dq(nasdaq, "stale_book", replay=True), "stale_book")
    assert c.status == "warn"
    assert c.metrics["prints_through_book"] == 1
    row = dict(zip(evidence(c, "prints_through_book").columns, evidence(c, "prints_through_book").rows[0], strict=True))
    assert row["aggressor"] == "B" and row["price"] > row["ask_px"]
    assert row["ts_event"] == int(c0["ts_event"][t])


@pytest.mark.requires_core
def test_trade_parity_mismatch(nasdaq: Path) -> None:
    def bump(c: Cols) -> Cols:
        c["price"][10] += 10_000_000
        return c

    edit_partition(nasdaq, D1, 1, bump, table="trades_source")
    c = check(dq(nasdaq, "trade_parity", replay=True), "trade_parity")
    assert c.status == "warn"
    assert c.metrics["trades_mismatches"] == 1 and c.metrics["pass"] is False
    assert evidence(c, "first_mismatches").rows


def test_trade_parity_skips_without_source_prints(nasdaq: Path) -> None:
    shutil.rmtree(nasdaq / "trades_source")
    c = check(dq(nasdaq, "trade_parity", replay=True), "trade_parity")
    assert c.status == "skip" and "trades_source" in c.summary


# ---- records: sizes, prices, timestamps -------------------------------------------------------------------------


def test_size_and_price_grid(nasdaq: Path) -> None:
    def bad(c: Cols) -> Cols:
        adds = np.nonzero(c["action"] == ord("A"))[0]
        c["size"][adds[100]] = 0
        c["price"][adds[200]] += 1  # one price unit off the 0.01 grid
        c["price"][adds[300]] = 400_000_000  # $0.40: sub-dollar quote, legal at $0.0001 increments
        return c

    edit_partition(nasdaq, D1, 2, bad)
    c = check(dq(nasdaq, "size_price"), "size_price")
    assert c.status == "warn"
    assert (c.metrics["zero_size"], c.metrics["off_tick_resting"], c.metrics["size_overflow"]) == (1, 1, 0)
    issues = sorted(r[0] for r in evidence(c, "records").rows)
    assert issues == ["off_tick_resting", "zero_size"]

    def overflow(c: Cols) -> Cols:
        c["size"][5] = np.uint64(2**63 + 1)
        return c

    edit_partition(nasdaq, D2, 1, overflow)
    assert check(dq(nasdaq, "size_price"), "size_price").status == "fail"


def test_timestamps_regression_negative_latency_and_outliers(nasdaq: Path) -> None:
    def warp(c: Cols) -> Cols:
        te, tr = c["ts_event"], c["ts_recv"]
        te[B] = te[B - 1] - NS  # 1 s back in time; its latency becomes about 1 s (an outlier)
        tr[B + 5] = te[B + 5] - 5_000  # received before it happened
        return c

    edit_partition(nasdaq, D1, 1, warp)
    c = check(dq(nasdaq, "timestamps"), "timestamps")
    assert c.status == "warn"
    m = c.metrics
    assert (m["ts_event_regressions"], m["negative_latency"], m["outliers"]) == (1, 1, 1)
    reg = dict(
        zip(evidence(c, "ts_event_regressions").columns, evidence(c, "ts_event_regressions").rows[0], strict=True)
    )
    assert (reg["row"], reg["back_ns"]) == (B, NS)
    out = dict(zip(evidence(c, "latency_outliers").columns, evidence(c, "latency_outliers").rows[0], strict=True))
    assert out["row"] == B and out["latency_ns"] >= NS
    hist = {r[0]: r[1] for r in evidence(c, "latency_histogram").rows}
    assert hist["negative"] == 1 and hist["0.1-1 s"] + hist["1-10 s"] >= 1
    # an explicit threshold
    c = check(dq(nasdaq, "timestamps", latency_outlier_ns=20_000), "timestamps")
    assert c.metrics["outlier_threshold_ns"] == 20_000 and c.metrics["outliers"] > 1000


# ---- session coverage -------------------------------------------------------------------------------------------


def _ny(date: str, hh: int, mm: int, ss: float = 0.0) -> int:
    d = datetime.date.fromisoformat(date)
    t = datetime.datetime(d.year, d.month, d.day, hh, mm, int(ss), tzinfo=ZoneInfo("America/New_York"))
    return int(t.timestamp()) * NS + round((ss % 1) * NS)


def test_session_late_start_without_snapshot(nasdaq: Path) -> None:
    open_ns = _ny(D2, 9, 30)
    edit_partition(nasdaq, D2, 2, lambda c: take(c, np.nonzero(c["ts_event"] >= open_ns + 3600 * NS)[0]))
    c = check(dq(nasdaq, "session_coverage"), "session_coverage")
    assert c.status == "warn"
    assert c.metrics["starts_late_without_snapshot"] == 1 and c.metrics["ends_early"] == 0
    row = evidence(c, "partitions").rows[0]
    assert row[:3] == ["starts_late_without_snapshot", D2, 2]


def test_session_truncated_at_utc_midnight(tmp_path: Path) -> None:
    # winter: 19:00 New York is 00:00 UTC, before the 20:00 extended close -> a truncated tail
    w = "2026-01-15"
    recs = [rec(_ny(w, 9, 0) + i * 60 * NS, "A", "B", 150_000_000_000, 1, i + 1, i + 1) for i in range(600)]
    recs.append(rec(_ny(w, 18, 59, 59.5), "A", "B", 150_000_000_000, 1, 999, 1000))
    # summer: 20:00 New York is 00:00 UTC, the extended close itself -> no truncation
    s = "2026-06-15"
    srecs = [rec(_ny(s, 9, 0) + i * 60 * NS, "A", "B", 150_000_000_000, 1, i + 1, i + 1) for i in range(600)]
    srecs.append(rec(_ny(s, 19, 59, 59.5), "A", "B", 150_000_000_000, 1, 999, 1000))
    ds = make_dataset(tmp_path / "ds", {(w, 1): recs, (s, 1): srecs}, {1: "QTS"})
    c = check(dq(ds, "session_coverage"), "session_coverage")
    assert c.metrics["truncated_at_utc_midnight"] == 1
    assert [r[:2] for r in evidence(c, "partitions").rows] == [["truncated_at_utc_midnight", w]]


# ---- symbology, snapshots, vintage ------------------------------------------------------------------------------


def test_symbology(nasdaq: Path) -> None:
    def rename(rows: list[dict]) -> list[dict]:
        for r in rows:
            if r["instrument_id"] == 2 and str(r["session_date"]) == D2:
                r["symbol"] = "QTSC"
        return rows

    edit_instruments(nasdaq, rename)
    c = check(dq(nasdaq, "symbology"), "symbology")
    assert c.status == "warn" and c.metrics["id_symbol_changes"] == 1
    assert evidence(c, "mappings").rows[0][0] == "symbol_changed_for_id"

    def exp(rows: list[dict]) -> list[dict]:
        for r in rows:
            if r["instrument_id"] == 1 and str(r["session_date"]) == D2:
                r["price_exp"] = -8
        return [*rows, dict(rows[0])]

    edit_instruments(nasdaq, exp)
    c = check(dq(nasdaq, "symbology"), "symbology")
    assert c.status == "fail"
    assert (c.metrics["price_exp_conflicts"], c.metrics["duplicate_instrument_rows"]) == (1, 1)


def test_symbology_per_day_ids_are_informational(tmp_path: Path) -> None:
    t = _ny(D1, 10, 0)
    parts = {
        (D1, 13): [rec(t, "A", "B", 150_000_000_000, 1, 1, 1)],
        (D2, 15): [rec(t + 86_400 * NS, "A", "B", 150_000_000_000, 1, 1, 1)],
    }
    syms = {13: "AAPL", 15: "AAPL"}
    c = check(dq(make_dataset(tmp_path / "itch", parts, syms, source_format="etcsv"), "symbology"), "symbology")
    assert c.status == "pass" and c.metrics["symbol_id_changes"] == 1
    c = check(dq(make_dataset(tmp_path / "dbn", parts, syms, source_format="dbn"), "symbology"), "symbology")
    assert c.status == "warn"
    row = evidence(c, "mappings").rows[0]
    assert row[:2] == ["instrument_id_changed", "AAPL"] and row[-1] == f"{D1}:13 -> {D2}:15"


def test_snapshots_maybe_bad_book(nasdaq: Path) -> None:
    def flag(c: Cols) -> Cols:
        c["flags"][B] |= 4
        return c

    edit_partition(nasdaq, D1, 1, flag)
    c = check(dq(nasdaq, "snapshots"), "snapshots")
    assert c.status == "fail" and c.metrics["maybe_bad_book"] == 1
    assert evidence(c, "records").rows[0][:4] == ["maybe_bad_book", D1, 1, B]


def test_snapshots_without_clear_on_cme(cme: Path) -> None:
    c0, _ = read_cols(part_path(cme, D1, 1))
    snap = (c0["flags"] & 32) != 0
    clears = [i for i in np.nonzero(c0["action"] == ord("R"))[0] if i + 1 < len(snap) and snap[i + 1] and i > 0]
    drop = int(clears[0])
    edit_partition(cme, D1, 1, lambda c: take(c, [i for i in range(len(c["action"])) if i != drop]))
    c = check(dq(cme, "snapshots"), "snapshots")
    assert c.status == "warn" and c.metrics["snapshot_without_clear"] == 1
    assert evidence(c, "records").rows[0][:4] == ["snapshot_without_clear", D1, 1, drop]


def test_vintage_detects_tampering(nasdaq: Path, tmp_path: Path) -> None:
    p = part_path(nasdaq, D1, 1)
    c, schema = read_cols(p)
    c["size"][0] += 1
    write_cols(c, schema, p)  # manifest not refreshed: the file no longer matches its recorded hash
    shutil.copy(p, p.parent / "part-00001.parquet")  # a file the manifest does not list
    src = tmp_path / "raw.bin"
    src.write_bytes(b"x" * 10)
    m = json.loads((nasdaq / "manifest.json").read_text())
    m["inputs"] = [
        {"path": str(src), "bytes": 10, "sha256": "0" * 64},
        {"path": str(tmp_path / "gone"), "bytes": 1, "sha256": "1" * 64},
    ]
    (nasdaq / "manifest.json").write_text(json.dumps(m))
    c = check(dq(nasdaq, "vintage"), "vintage")
    assert c.status == "fail"
    issues = {r[0] for r in evidence(c, "partition_files").rows}
    assert issues == {"hash_mismatch", "not_in_manifest"}
    assert c.metrics["dataset_id_ok"] is True  # the id is a function of the manifest, which was not touched
    states = [r[-1] for r in evidence(c, "inputs").rows]
    assert states == ["size_ok", "not_found"]
    c = check(dq(nasdaq, "vintage", verify_sources=True), "vintage")
    assert [r[-1] for r in evidence(c, "inputs").rows] == ["sha256_mismatch", "not_found"]


def test_vintage_dbn_provenance(nasdaq: Path) -> None:
    m = json.loads((nasdaq / "manifest.json").read_text())
    m["source_format"] = "dbn"
    (nasdaq / "manifest.json").write_text(json.dumps(m))
    refresh_manifest(nasdaq)
    c = check(dq(nasdaq, "vintage"), "vintage")
    assert c.status == "warn" and any("without source_vintage" in n for n in c.notes)
    m = json.loads((nasdaq / "manifest.json").read_text())
    m["source_vintage"] = [
        {"sha256": "a" * 64, "dbn_version": 2, "dataset": "XNAS.ITCH"},
        {"sha256": "b" * 64, "dbn_version": 3, "dataset": "XNAS.ITCH"},
    ]
    (nasdaq / "manifest.json").write_text(json.dumps(m))
    c = check(dq(nasdaq, "vintage"), "vintage")
    assert c.status == "warn" and any("mixed vintages" in n for n in c.notes)
    assert c.metrics["dbn_versions"] == "2,3" and len(evidence(c, "source_vintage").rows) == 2


def test_dq_counters_follow_the_verdict(nasdaq: Path) -> None:
    d = json.loads((nasdaq / "data_quality.json").read_text())
    d.update(verdict="unusable", maybe_bad_book_records=3, reasons=["maybe_bad_book_records = 3"])
    (nasdaq / "data_quality.json").write_text(json.dumps(d))
    c = check(dq(nasdaq, "dq_counters"), "dq_counters")
    assert c.status == "fail" and c.metrics["origin"] == "ingest"
    assert evidence(c, "reasons").rows == [["maybe_bad_book_records = 3"]]
    (nasdaq / "data_quality.json").unlink()
    assert check(dq(nasdaq, "dq_counters"), "dq_counters").status == "warn"


def test_l2_dataset_skips_record_checks(nasdaq: Path) -> None:
    m = json.loads((nasdaq / "manifest.json").read_text())
    m["kind"] = "l2"
    (nasdaq / "manifest.json").write_text(json.dumps(m))
    rep = dq(nasdaq)
    st = {c.id: c.status for c in rep.checks}
    for cid in ("sequence", "orphan_refs", "size_price", "timestamps", "snapshots", "crossed_book", "cross_source"):
        assert st[cid] == "skip", cid
    assert "needs an MBO dataset" in check(rep, "sequence").summary
    assert st["vintage"] == st["symbology"] == "pass"


# ---- cross-source parity, rate drift ----------------------------------------------------------------------------


def test_cross_source_parity(nasdaq: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    shutil.copytree(nasdaq, other)
    rep = dq(nasdaq, "cross_source", compare={"path": str(other)})
    c = check(rep, "cross_source")
    assert c.status == "pass" and c.metrics["only_a"] == c.metrics["only_b"] == 0
    assert c.metrics["matched"] == c.metrics["records_a"] == c.metrics["records_b"]
    assert rep.compare is not None and rep.compare.path == str(other)

    def edit(c: Cols) -> Cols:
        c["size"][100] += 1
        c["ts_event"][200] += 1_000
        return c

    edit_partition(other, D1, 2, edit)
    c = check(dq(nasdaq, "cross_source", compare={"path": str(other)}), "cross_source")
    assert c.status == "warn"
    m = c.metrics
    assert (m["only_a"], m["only_b"], m["timestamp_only_differences"]) == (2, 2, 2)
    assert len(evidence(c, "only_in_a").rows) == 2 and len(evidence(c, "only_in_b").rows) == 2
    per = {(r[0], r[1]): r for r in evidence(c, "partitions").rows}
    assert per[(D1, "QTSB")][5:8] == [2, 2, 2]


def test_cross_source_matches_by_symbol_and_rescales_prices(tmp_path: Path) -> None:
    t = _ny(D1, 10, 0)
    a_recs = [rec(t + i, "A", "B", 150_010_000_000, 100, i + 1, i + 1) for i in range(10)]
    b_recs = [dict(r, price=r["price"] // 10, sequence=r["sequence"] + 500) for r in a_recs]
    a = make_dataset(tmp_path / "a", {(D1, 13): a_recs}, {13: "AAPL"}, source_format="itch")
    b = make_dataset(tmp_path / "b", {(D1, 4242): b_recs}, {4242: "AAPL"}, source_format="dbn")
    edit_instruments(b, lambda rows: [dict(r, price_exp=-8) for r in rows])
    c = check(dq(a, "cross_source", compare={"path": str(b)}), "cross_source")
    assert c.status == "pass" and c.metrics["matched"] == 10, c.summary
    c = check(dq(a, "cross_source", compare={"path": str(tmp_path / "a")}, checks=["cross_source"]), "cross_source")
    assert c.status == "pass"
    lone = make_dataset(tmp_path / "c", {(D2, 1): a_recs}, {1: "MSFT"})
    c = check(dq(a, "cross_source", compare={"path": str(lone)}), "cross_source")
    assert c.status == "fail" and "no (session_date, symbol) in common" in c.summary


def test_rate_drift(tmp_path: Path) -> None:
    days = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"]
    parts = {}
    for k, d in enumerate(days):
        n = 5 if k == 3 else 100
        t = _ny(d, 10, 0)
        parts[(d, 1)] = [rec(t + i * NS, "A", "B", 150_000_000_000, 1, i + 1, i + 1) for i in range(n)]
    c = check(dq(make_dataset(tmp_path / "ds", parts, {1: "QTS"}), "rate_drift"), "rate_drift")
    assert c.status == "warn" and c.metrics["days_flagged"] == 1
    assert evidence(c, "days").rows[0][:3] == ["QTS", days[3], 5]
    two = {k: v for k, v in parts.items() if k[0] in days[:2]}
    c = check(dq(make_dataset(tmp_path / "two", two, {1: "QTS"}), "rate_drift"), "rate_drift")
    assert c.status == "skip"
