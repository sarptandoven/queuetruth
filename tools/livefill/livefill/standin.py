"""Stand-in harness log: treat other traders' resting orders of a capture as if they were the user's, to rehearse the
whole pipeline on a public capture before any account exists (no order is placed; the orders are someone else's).

Picks up to `n` orders created after the segment's seed that rested and never took liquidity. `sample =
"filled_first"` (the default) takes orders filled at least once first, which exercises the fill path but tilts the
sample toward fills; `sample = "random"` draws uniformly, for a rehearsal whose fill rate means something. It writes
a `livefill.harness_log` with `mode = "stand_in"`: a `submit` per order (sent 50 ms before its `order_created`
arrived, answered 1 ms after, with the venue id; client order ids `standin-<market>-<seed>-<index>`) and, for
orders later deleted with a remainder, a `cancel` 50 ms before the delete arrived. The local clocks are therefore
invented; the queue positions, fills and venue stamps the matcher reads are the feed's own."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from livefill.check import DEFAULT_TAKER_WINDOW_US, choose_seed, maker_ids, taker_ids
from livefill.harness import LOG_SCHEMA, HarnessLog
from livefill.store import read_meta
from livefill.stream import load_stream, stream_index
from livefill.units import PRICE_EXP, SIZE_EXP, from_fixed
from queuetruth.errors import QtError

SEND_BEFORE_NS = 50_000_000
SAMPLES = ("filled_first", "random")


def write_stand_in_log(
    capture: str | Path,
    out: str | Path,
    n: int = 50,
    seed: int = 0,
    market: str | None = None,
    sample: str = "filled_first",
) -> dict[str, Any]:
    """Writes the stand-in log of one market (default: the capture's first) at `out`; returns `{orders, filled,
    cancelled, market}`."""
    meta = read_meta(capture)
    market = market or meta["markets"][0]
    if market not in meta["markets"]:
        raise QtError("QT-USAGE-001", f"market {market!r} is not in the capture ({meta['markets']})")
    if sample not in SAMPLES:
        raise QtError("QT-USAGE-001", f"sample: expected one of {list(SAMPLES)}, got {sample!r}")
    rng = random.Random(seed)
    picks: list[tuple[str, Any, Any]] = []
    n_filled = 0
    ix = stream_index(capture)
    for seg, m in ix.keys:
        if m != market:
            continue
        st = load_stream(capture, seg, m, ix.segment_start_ns.get(seg))
        s = choose_seed(st)
        if s is None:
            continue
        takers = taker_ids(st.orders, DEFAULT_TAKER_WINDOW_US, maker_ids(st.trades))
        # an order that ever took liquidity (it crossed on entry) is no stand-in for a passive order
        takers |= {t.buy_id if t.aggressor == "buy" else t.sell_id for t in st.trades}
        filled = maker_ids(st.trades)
        created = {e.oid: e for e in st.orders if e.event == "order_created" and e.us >= s.us and e.oid not in takers}
        deleted = {e.oid: e for e in st.orders if e.event == "order_deleted" and e.oid in created}
        ids = sorted(created)
        rng.shuffle(ids)
        if sample == "filled_first":
            ids.sort(key=lambda o: o not in filled)  # filled first, stable within each group
        picks += [(market, created[o], deleted.get(o)) for o in ids]
        n_filled += sum(1 for o in ids if o in filled)
    if sample == "random":
        rng.shuffle(picks)
    picks = picks[:n]
    picks.sort(key=lambda p: p[1].seq)
    log = HarnessLog(Path(out))
    log.write(
        {
            "type": "run_start",
            "schema": LOG_SCHEMA,
            "schema_version": 1,
            "mode": "stand_in",
            "venue": "bitstamp",
            "market": market,
            "config": {"capture": str(capture), "n": n, "seed": seed, "sample": sample},
            "run_tag": f"standin-{market}-{seed}",
            "local_ns": picks[0][1].recv_ns - SEND_BEFORE_NS if picks else 0,
        }
    )
    n_cancel = 0
    for i, (_m, c, d) in enumerate(picks):
        coid = f"standin-{market}-{seed}-{i:05d}"
        side = c.side
        log.write(
            {
                "type": "submit",
                "i": i,
                "client_order_id": coid,
                "endpoint": f"{side}/{market}",
                "params": {
                    "amount": from_fixed(c.amt, SIZE_EXP),
                    "price": from_fixed(c.px, PRICE_EXP),
                    "client_order_id": coid,
                },
                "sent_ns": c.recv_ns - SEND_BEFORE_NS,
                "recv_ns": c.recv_ns + 1_000_000,
                "outcome": "ok",
                "http_status": 200,
                "body": {"id": str(c.oid), "client_order_id": coid},
                "side": side,
            }
        )
        if d is not None and d.amt > 0:
            n_cancel += 1
            log.write(
                {
                    "type": "cancel",
                    "i": i,
                    "client_order_id": coid,
                    "endpoint": "cancel_order",
                    "params": {"id": str(c.oid)},
                    "sent_ns": d.recv_ns - SEND_BEFORE_NS,
                    "recv_ns": d.recv_ns + 1_000_000,
                    "outcome": "ok",
                    "http_status": 200,
                    "body": {"id": str(c.oid), "status": "Canceled"},
                    "order_id": str(c.oid),
                }
            )
    log.write({"type": "run_end", "summary": {"stand_in": len(picks)}, "local_ns": 0})
    log.close()
    return {
        "orders": len(picks),
        "cancelled": n_cancel,
        "filled_candidates": n_filled,
        "market": market,
        "sample": sample,
    }
