"""Test-local builders for the scorer tests: canonical live logs, tiny MBO datasets, a minimal schedule builder (a
stand-in for `queuetruth.live.to_schedule`, one leg per order) and a fake engine runner that writes the tables the
scorer reads. Independent of the other packages' implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from queuetruth.schemas.io import sha256_file, write_table
from queuetruth.score._io import make_table, read_live_log

T0 = 1_767_261_600_000_000_000  # 2026-01-01 10:00:00 UTC (fixed constant)
MIDNIGHT = 1_767_225_600_000_000_000  # 2026-01-01 00:00:00 UTC
DAY = 86_400_000_000_000
DAY0 = MIDNIGHT // DAY
MS = 1_000_000
S = 1_000_000_000
TICK = 10_000_000


def order_events(
    key: str,
    side: str,
    px: int,
    qty: int,
    t_ack: int,
    *,
    fills: list[tuple[int, int]] | None = None,
    cancel_at: int | None = None,
    cancel_sent: int | None = None,
    instrument_id: int = 1,
    venue_order_id: str | None = None,
    liquidity: str = "maker",
    venue: str = "test",
    tif: str = "gtc",
    fill_prefix: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One limit order: submit_sent (t_ack - 1 ms local), ack (exchange t_ack), fills, optional cancel."""
    o = {
        "order_key": key,
        "venue": venue,
        "account": "acct",
        "instrument": f"SYM{instrument_id}",
        "instrument_id": instrument_id,
        "venue_order_id": venue_order_id,
        "side": side,
        "order_type": "limit",
        "tif": tif,
        "post_only": False,
        "orig_qty": qty,
        "qty_str": str(qty),
        "limit_px": px,
        "price_exp": -9,
        "size_exp": 0,
        "validation_status": "fixture_only",
    }
    ev: list[dict[str, Any]] = [
        {"event_type": "submit_sent", "ts_local_send": t_ack - MS},
        {"event_type": "ack", "ts_exchange": t_ack, "ts_local_recv": t_ack + MS},
    ]
    for i, (t, q) in enumerate(fills or []):
        ev.append(
            {
                "event_type": "fill",
                "ts_exchange": t,
                "qty": q,
                "px": px,
                "fill_id": f"{fill_prefix}{key}-f{i}",
                "liquidity": liquidity,
            }
        )
    if cancel_at is not None:
        ev.append(
            {"event_type": "cancel_sent", "ts_local_send": cancel_sent if cancel_sent is not None else cancel_at - MS}
        )
        ev.append({"event_type": "cancel_ack", "ts_exchange": cancel_at, "ts_local_recv": cancel_at + MS})
    for i, e in enumerate(ev, start=1):
        e.setdefault("order_key", key)
        e["event_seq"] = i
        e.setdefault("ts_exchange_kind", "match_time")
        e.setdefault("ts_precision", "ns")
        e.setdefault("source", "synthetic")
        e.setdefault("derived", False)
    return o, ev


def write_live_log(root: Path, orders: list[dict], events: list[dict], manifest_extra: dict | None = None) -> Path:
    """Canonical live-log directory (orders.parquet, events.parquet, manifest.json)."""
    root.mkdir(parents=True, exist_ok=True)
    write_table(make_table("qt.live_orders", orders), root / "orders.parquet")
    write_table(make_table("qt.live_events", events), root / "events.parquet")
    man = {
        "schema": "qt.live_manifest",
        "schema_version": 1,
        "venues": sorted({o["venue"] for o in orders}),
        "accounts": ["acct"],
        "n_orders": len(orders),
        "n_events": len(events),
        "created_by": "test",
    }
    man.update(manifest_extra or {})
    (root / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return root


def log_from(orders_events: list[tuple[dict, list[dict]]], root: Path, **kw: Any) -> Path:
    """Writes a live log from `order_events` results."""
    return write_live_log(root, [o for o, _ in orders_events], [e for _, evs in orders_events for e in evs], **kw)


def simple_schedule(live_dir: str, *, entry: str, latency_ns: int | None) -> tuple[pa.Table, pa.Table]:
    """Minimal `to_schedule` stand-in: one abs leg per acked limit order, `ts_submit = ack.ts_exchange`, cancel at
    `cancel_ack.ts_exchange`."""
    orders, events = read_live_log(live_dir)
    by: dict[str, list[dict]] = {}
    for e in events:
        by.setdefault(e["order_key"], []).append(e)
    rows = []
    for o in orders:
        evs = by.get(o["order_key"], [])
        ack = next((e for e in evs if e["event_type"] == "ack"), None)
        if ack is None or o.get("tif") in ("ioc", "fok") or o.get("instrument_id") is None:
            continue
        cxl = next((e for e in evs if e["event_type"] == "cancel_ack"), None)
        rows.append(
            {
                "shadow_id": o["order_key"],
                "instrument_id": o["instrument_id"],
                "side": ord("B") if o["side"] == "buy" else ord("A"),
                "price_mode": "abs",
                "price": o["limit_px"],
                "offset_ticks": 0,
                "qty": o["orig_qty"],
                "ts_submit": ack["ts_exchange"],
                "latency_ns": 0 if latency_ns is None else latency_ns,
                "ts_cancel": None if cxl is None else cxl["ts_exchange"],
                "source": "live_log",
                "order_key": o["order_key"],
            }
        )
    return make_table("qt.shadow_spec", rows), make_table("qt.shadow_amend", [])


def make_dataset(root: Path, mbo: list[dict] | None = None, n_days: int = 1, instrument_id: int = 1) -> Path:
    """Tiny MBO dataset: instruments.parquet (one instrument, `n_days` session dates), one mbo partition per day."""
    root.mkdir(parents=True, exist_ok=True)
    inst = [
        {
            "instrument_id": instrument_id,
            "session_date": DAY0 + d,
            "symbol": "SYM",
            "venue": "cme",
            "source_venue": "cme",
            "price_exp": -9,
            "tick_size": TICK,
            "lot_size": 1,
            "size_exp": 0,
            "contract_multiplier": 1.0,
            "currency": "USD",
            "session_tz": "UTC",
            "midnight_utc_ns": MIDNIGHT + d * DAY,
            "open_s": 0,
            "close_s": 86_400,
            "source": "test",
        }
        for d in range(n_days)
    ]
    write_table(make_table("qt.instruments", inst), root / "instruments.parquet")
    parts = []
    for d in range(n_days):
        lo, hi = MIDNIGHT + d * DAY, MIDNIGHT + (d + 1) * DAY
        recs = [r for r in (mbo or []) if lo <= r["ts_event"] < hi]
        if not recs:
            recs = [
                {"ts_event": lo + 23 * 3600 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1, "flags": 128}
            ]
        full = [
            {
                "ts_recv": r["ts_event"],
                "instrument_id": instrument_id,
                "size": 0,
                "order_id": 0,
                "flags": 128,
                "sequence": i + 1,
                "channel_id": 0,
                "publisher_id": 0,
                **r,
            }
            for i, r in enumerate(recs)
        ]
        rel = f"mbo/date=day{d}/instrument_id={instrument_id}/part-00000.parquet"
        fe = write_table(make_table("qt.mbo", full), root / rel)
        parts.append(
            {
                "instrument_id": instrument_id,
                "session_date": DAY0 + d,
                "files": [{"path": rel, "rows": fe["rows"], "sha256": fe["sha256"]}],
                "rows": fe["rows"],
                "ts_min": full[0]["ts_event"],
                "ts_max": full[-1]["ts_event"],
            }
        )
    man = {
        "schema": "qt.dataset_manifest",
        "schema_version": 1,
        "dataset_id": "0" * 64,
        "kind": "mbo",
        "source_format": "test",
        "venue": "cme",
        "source_venue": "cme",
        "inputs": [],
        "partitions": parts,
        "price_exp_by_instrument": {str(instrument_id): -9},
        "created_by": "test",
        "validation_status": "synthetic_only",
    }
    (root / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return root


@dataclass
class FakeRunner:
    """Engine stand-in: `replay` writes shadow results/fills from `sim_fills` (shadow_id -> [(ts, qty, reason)]),
    plus bbo/trades rows; `run_zoo` writes predictions/fills from the same fills."""

    sim_fills: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    bbo: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    queue_ahead: dict[str, int] = field(default_factory=dict)
    schedule_fn: Any = simple_schedule
    replay_specs: list[Any] = field(default_factory=list)
    zoo_specs: list[Any] = field(default_factory=list)
    replay_datasets: list[str] = field(default_factory=list)
    schedules: list[list[dict]] = field(default_factory=list)
    mbo_seen: list[dict[str, list]] = field(default_factory=list)
    native_replay_options: bool = False
    replay_opts: list[dict[str, Any]] = field(default_factory=list)

    def validate(self, live_dir: str) -> list[str]:
        return []

    def to_schedule(self, live_dir: str, *, entry: str, latency_ns: int | None) -> tuple[pa.Table, pa.Table]:
        return self.schedule_fn(live_dir, entry=entry, latency_ns=latency_ns)

    def _sched(self, spec: Any) -> list[dict]:
        from queuetruth.score._io import read_parquet, rows

        return rows(read_parquet(spec.shadows.path))

    def replay(self, spec: Any, **opts: Any) -> None:
        import pyarrow.parquet as pq

        assert not opts or self.native_replay_options, "replay options passed to a runner without native support"
        self.replay_opts.append(opts)
        self.replay_specs.append(spec)
        self.replay_datasets.append(spec.dataset.path)
        self.schedules.append(self._sched(spec))
        parts = sorted(Path(spec.dataset.path).rglob("mbo/**/*.parquet"))
        self.mbo_seen.append(pa.concat_tables([pq.read_table(p) for p in parts]).to_pydict() if parts else {})
        out = Path(spec.out)
        out.mkdir(parents=True, exist_ok=True)
        res, fl = [], []
        for s in self._sched(spec):
            sid = s["shadow_id"]
            cum = 0
            first = None
            for i, (t, q, reason) in enumerate(sorted(self.sim_fills.get(sid, [])), start=1):
                q = min(q, s["qty"] - cum)
                if q <= 0:
                    break
                cum += q
                first = t if first is None else first
                fl.append(
                    {
                        "shadow_id": sid,
                        "fill_seq": i,
                        "ts_event": t,
                        "sequence": i,
                        "fill_qty": q,
                        "cum_qty": cum,
                        "leaves_qty": s["qty"] - cum,
                        "fill_px": s["price"],
                        "reason": reason,
                        "queue_ahead_before": 0,
                        "traded_at_px_event": 0,
                        "trade_through_event": 0,
                        "behind_volume_executed": 0,
                    }
                )
            end = "filled" if cum >= s["qty"] else ("cancelled" if s.get("ts_cancel") is not None else "open_at_end")
            res.append(
                {
                    "shadow_id": sid,
                    "instrument_id": s["instrument_id"],
                    "side": s["side"],
                    "price": s["price"],
                    "qty": s["qty"],
                    "ts_submit": s["ts_submit"],
                    "ts_live": s["ts_submit"],
                    "ts_activated_event": s["ts_submit"] + 1,
                    "ts_cancel_effective": s.get("ts_cancel"),
                    "queue_ahead_at_live": self.queue_ahead.get(sid, 0),
                    "rank_at_live": 0,
                    "level_qty_at_live": 0,
                    "ts_first_fill": first,
                    "ts_last_fill": None,
                    "ts_full_fill": None,
                    "filled_qty": cum,
                    "n_fills": len([f for f in fl if f["shadow_id"] == sid]),
                    "queue_ahead_at_first_fill": None,
                    "first_fill_reason": "never" if first is None else "queue_depleted",
                    "end_state": end,
                    "latency_applied_ns": 0,
                    "ambiguous_race": False,
                    "out_of_session": False,
                    "dq_flags": 0,
                }
            )
        write_table(make_table("qt.shadow_results", res), out / "shadow_results.parquet")
        write_table(make_table("qt.shadow_fills", fl), out / "shadow_fills.parquet")
        bbo = [{"sequence": 0, "bid_ct": 1, "ask_ct": 1, "instrument_id": 1, **b} for b in self.bbo]
        write_table(make_table("qt.bbo", bbo), out / "bbo.parquet")
        tr = [
            {"sequence": 0, "aggressor_side": ord("A"), "kind": "displayed", "instrument_id": 1, **t}
            for t in self.trades
        ]
        write_table(make_table("qt.trades", tr), out / "trades.parquet")

    def run_zoo(self, spec: Any) -> None:
        self.zoo_specs.append(spec)
        out = Path(spec.out)
        out.mkdir(parents=True, exist_ok=True)
        mid = spec.models[0]
        preds, zf = [], []
        for s in self._sched(spec):
            sid = s["shadow_id"]
            fills = sorted(self.sim_fills.get(sid, []))
            for h in [*spec.horizons_ns, spec.h_max_ns]:
                done = sum(q for t, q, _ in fills if t <= s["ts_submit"] + h)
                preds.append(
                    {
                        "model_id": mid,
                        "shadow_id": sid,
                        "qty": s["qty"],
                        "horizon_ns": h,
                        "p_fill": 1.0 if done else 0.0,
                        "e_fill_qty": float(min(done, s["qty"])),
                        "model_ts_first_fill": fills[0][0] if fills else None,
                        "censored": False,
                        "queue_ahead_at_live": self.queue_ahead.get(sid, 0),
                        "level_qty_at_live": 0,
                        "visible_level_qty_at_live": 7,
                        "out_of_view_at_live": False,
                        "instrument_id": s["instrument_id"],
                        "session_date": DAY0,
                        "ts_live": s["ts_submit"],
                        "trade_vol_60s": 3,
                    }
                )
            cum = 0.0
            for i, (t, q, reason) in enumerate(fills, start=1):
                cum += q
                zf.append(
                    {
                        "model_id": mid,
                        "shadow_id": sid,
                        "fill_seq": i,
                        "ts_event": t,
                        "fill_qty": float(q),
                        "cum_qty": cum,
                        "queue_ahead_est_before": 0.0,
                        "reason": "queue" if reason == "queue_depleted" else reason,
                        "quirk": "none",
                    }
                )
        write_table(make_table("qt.zoo_predictions", preds), out / "zoo_predictions.parquet")
        write_table(make_table("qt.zoo_fills", zf), out / "zoo_fills.parquet")

    def bbo_from_l2(self, dataset: str, out: str) -> None:
        raise AssertionError("not used")


def sha(p: Path) -> str:
    """File sha256."""
    return sha256_file(p)
