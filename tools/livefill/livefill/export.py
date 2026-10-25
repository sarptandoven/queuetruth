"""Exports a Parquet capture as one `queuetruth ingest --format bitstamp` input directory per segment, and ingests
them.

Each segment directory `seg-NNNN/` holds `live_orders.jsonl` and `live_trades.jsonl` (the segment's frames in
arrival order, each line the received text plus `recv_ns`), `detail_order_book.jsonl` with exactly one seed
snapshot per seeded market, and `capture_meta.json` whose `markets` list is the capture's (so instrument ids are
the same in every segment: 1, 2, ... in capture order). A market with no seed snapshot in the segment is left out
of it (`unseeded`): replaying it from an empty book would invent a book. A segment with no seeded market is skipped.

Seed choice (`book_seed`): `auto` (default) = the segment's first REST `order_book?group=2` body when it has one (the
whole book), else `ws`; `ws` = its first `detail_order_book` frame (the best 100 orders per side, the form RD-12
validated); `rest` = REST only. The ingest applies, after the seed, every message stamped at or after the seed's
`microtimestamp`; `check.py` verifies that rule against the venue's snapshots for either seed.

`out` must not exist or be empty (`QT-CONFIG-001` otherwise): the export never deletes anything. Each segment is
written by streaming its rows from the capture, so memory does not grow with the capture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from livefill import feed
from livefill.check import SEED_CHOICES, choose_seed
from livefill.store import iter_capture, read_meta
from livefill.stream import Snapshot, iter_streams, parse_book
from queuetruth.errors import QtError


def segment_dir(out: str | Path, segment: int) -> Path:
    return Path(out) / f"seg-{segment:04d}"


def require_empty_dir(out: str | Path, what: str = "out") -> Path:
    """`out` as a Path, created when missing; `QT-CONFIG-001` when it is a file or a non-empty directory (the kit
    never deletes or overwrites earlier results)."""
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise QtError("QT-CONFIG-001", f"{what}: {out} exists and is not empty; choose a new directory")
    out.mkdir(parents=True, exist_ok=True)
    return out


def export_segments(
    capture_dir: str | Path, out: str | Path, book_seed: str = "auto", clean_seed: bool = True
) -> dict[str, Any]:
    """Writes the per-segment ingest inputs under `out` (absent or empty); returns the `livefill.export` document.
    `clean_seed=False` seeds with the first snapshot even when it is ambiguous (`check.choose_seed`)."""
    if book_seed not in SEED_CHOICES:
        raise QtError("QT-USAGE-001", f"book seed: expected one of {list(SEED_CHOICES)}, got {book_seed!r}")
    meta = read_meta(capture_dir)
    out = require_empty_dir(out)
    markets = list(meta["markets"])
    # per segment and market: the seed, its raw text, and whether order events follow it
    seeds: dict[int, dict[str, tuple[Snapshot | None, str | None, bool]]] = {}
    for st in iter_streams(capture_dir):
        s = choose_seed(st, book_seed, clean=clean_seed)
        has_orders = any(e.seq > s.seq for e in st.orders) if s else False
        raw = st.raw(s) if s is not None and has_orders else None
        seeds.setdefault(st.segment, {})[st.market] = (s, raw, has_orders)
    segments = []
    for seg in sorted(seeds):
        seeded = {m: v for m, v in seeds[seg].items() if v[0] is not None and v[2]}
        unseeded = sorted(m for m, v in seeds[seg].items() if v[0] is None)
        doc: dict[str, Any] = {"segment": seg, "dir": None, "markets": {}, "unseeded": unseeded, "skipped": None}
        if not seeded:
            doc["skipped"] = "no seeded market with order events after its seed"
            segments.append(doc)
            continue
        d = segment_dir(out, seg)
        d.mkdir()
        doc["lines"] = _write_segment_lines(capture_dir, seg, set(seeded), d)
        seed_lines = []
        for m in markets:
            if m not in seeded:
                continue
            s, raw, _ = seeded[m]
            assert s is not None and raw is not None
            if s.source == "rest":
                seed_lines.append(feed.rest_seed_line(m, raw, s.recv_ns))
            else:
                seed_lines.append(feed.jsonl_line(raw, s.recv_ns))
            book = parse_book(raw, s.seq, s.source)
            doc["markets"][m] = {
                "instrument_id": markets.index(m) + 1,
                "seed_source": s.source,
                "seed_seq": s.seq,
                "seed_us": s.us,
                "seed_bids": book.n_bids,
                "seed_asks": book.n_asks,
            }
        (d / "detail_order_book.jsonl").write_text("".join(x + "\n" for x in seed_lines))
        (d / "capture_meta.json").write_text(
            json.dumps({"markets": markets, "livefill_segment": seg, "capture": str(Path(capture_dir))}, indent=1)
            + "\n"
        )
        doc["dir"] = str(d)
        segments.append(doc)
    return {
        "schema": "livefill.export",
        "schema_version": 1,
        "capture": str(Path(capture_dir)),
        "book_seed": book_seed,
        "markets": markets,
        "gaps": len(meta.get("gaps", [])),
        "segments": segments,
    }


def _write_segment_lines(capture_dir: str | Path, seg: int, markets: set[str], d: Path) -> dict[str, int]:
    """Streams the segment's `live_orders` and `live_trades` frames of `markets` to their JSONL files in arrival
    order; returns the line counts."""
    counts = {"live_orders": 0, "live_trades": 0}
    filters = [("segment", "=", seg), ("kind", "=", "ws")]
    cols = ["recv_ns", "market", "channel", "event", "raw"]
    with (d / "live_orders.jsonl").open("w") as fo, (d / "live_trades.jsonl").open("w") as ft:
        for b in iter_capture(capture_dir, cols, filters, segment=seg):
            c = {k: b.column(k).to_pylist() for k in ("recv_ns", "market", "channel", "event")}
            raws = b.column("raw")
            for i in range(b.num_rows):
                if c["market"][i] not in markets:
                    continue
                prefix = feed.split_channel(c["channel"][i] or "")[0]
                if prefix == "live_orders" and c["event"][i] in feed.ORDER_EVENTS:
                    f, name = fo, "live_orders"
                elif prefix == "live_trades" and c["event"][i] == "trade":
                    f, name = ft, "live_trades"
                else:
                    continue
                f.write(feed.jsonl_line(raws[i].as_py(), c["recv_ns"][i]) + "\n")
                counts[name] += 1
    return counts


def ingest_segment(seg_dir: str | Path, out: str | Path, taker_window_us: int | None = 100) -> dict[str, Any]:
    """`queuetruth ingest --format bitstamp --venue generic` of one exported segment; returns the ingest summary."""
    from queuetruth import api

    spec: dict[str, Any] = {
        "format": "bitstamp",
        "inputs": [{"path": str(seg_dir)}],
        "venue": "generic",
        "source_venue": "bitstamp",
        "out": str(out),
        "options": {"taker_window_us": taker_window_us},
    }
    summary = api.ingest(spec)
    return summary.model_dump(mode="json", by_alias=True)
