"""Latency first, then the queue (hftbacktest discussion #332): on a synthetic live log with a known entry latency
and a known queue parameter, `calibrate_live(..., latency_from_log=True)` recovers both; fitting the queue on send
times without the latency absorbs the latency into the queue parameter.

The fake zoo is the simulator family: a shadow with power parameter `n` is live at `ts_submit + latency` and fills
`10 s / n` later. The live orders are acked `LAT` after they are sent and fill `10 s / N_TRUE` after the ack."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from queuetruth.errors import QtError
from queuetruth.schemas.io import write_table
from queuetruth.score import calibrate_live
from queuetruth.score._io import make_table, read_live_log, read_parquet, rows
from score.qt_score_helpers import DAY, MS, T0, FakeRunner, log_from, order_events
from score.test_score_calibrate_live import PX, _dataset

S = 1_000_000_000
LAT = 2 * S  # true entry latency (send to exchange ack)
THETA_TRUE = 0.5  # ln n on the grid below
N_TRUE = math.exp(THETA_TRUE)
GRID = {"lo": -1.0, "hi": 2.0, "n": 13}  # theta step 0.25
HORIZONS = [1 * S, 2 * S, 4 * S, 6 * S, 7 * S, 8 * S, 9 * S, 12 * S, 16 * S]


class LatencyAwareRunner(FakeRunner):
    """`to_schedule` honours the entry rule; the zoo applies the spec latency and fills at `ts_live + 10 s / n`."""

    def to_schedule(self, live_dir, *, entry, latency_ns):
        orders, events = read_live_log(live_dir)
        by: dict[str, list[dict]] = {}
        for e in events:
            by.setdefault(e["order_key"], []).append(e)
        out = []
        for o in orders:
            evs = by[o["order_key"]]
            ack = next(e for e in evs if e["event_type"] == "ack")
            sent = next(e for e in evs if e["event_type"] == "submit_sent")
            cxl = next((e for e in evs if e["event_type"] == "cancel_ack"), None)
            send_mode = entry == "send_plus_latency"
            out.append(
                {
                    "shadow_id": o["order_key"],
                    "instrument_id": o["instrument_id"],
                    "side": ord("B"),
                    "price_mode": "abs",
                    "price": o["limit_px"],
                    "offset_ticks": 0,
                    "qty": o["orig_qty"],
                    "ts_submit": sent["ts_local_send"] if send_mode else ack["ts_exchange"],
                    "latency_ns": 0 if latency_ns is None else latency_ns,
                    "ts_cancel": None if cxl is None else cxl["ts_exchange"],
                    "source": "live_log",
                    "order_key": o["order_key"],
                }
            )
        return make_table("qt.shadow_spec", out), make_table("qt.shadow_amend", [])

    def run_zoo(self, spec):
        self.zoo_specs.append(spec)
        out = Path(spec.out)
        out.mkdir(parents=True, exist_ok=True)
        lat = spec.latency
        extra = int(lat.feed_ns + lat.decision_ns + lat.gateway_ns) if lat is not None else 0
        preds = []
        for s in rows(read_parquet(spec.shadows.path)):
            ts_live = int(s["ts_submit"]) + int(s["latency_ns"] or 0) + extra
            for mid in spec.models:
                v = float(re.search(r"n=([^;\]]+)", mid).group(1))
                t_fill = 10 * S / v  # after ts_live
                for h in [*spec.horizons_ns, spec.h_max_ns]:
                    p = 1.0 if t_fill <= h else 0.0
                    preds.append(
                        {
                            "model_id": mid.replace("]", ";mode=faithful]"),
                            "shadow_id": s["shadow_id"],
                            "qty": s["qty"],
                            "horizon_ns": h,
                            "p_fill": p,
                            "e_fill_qty": p * s["qty"],
                            "censored": False,
                            "visible_level_qty_at_live": 0,
                            "out_of_view_at_live": False,
                            "instrument_id": 1,
                            "session_date": 0,
                            "ts_live": ts_live,
                            "trade_vol_60s": 0,
                        }
                    )
        write_table(make_table("qt.zoo_predictions", preds), out / "zoo_predictions.parquet")


def _live(tmp_path):
    """Two days of 12 orders: sent at `t - LAT`, acked at `t`, filled at `t + 10 s / N_TRUE` (every third order is
    cancelled before its fill instead, so the fit sees censoring)."""
    oe = []
    t_fill = round(10 * S / N_TRUE)
    for day in (0, 1):
        for i in range(12):
            t = T0 + day * DAY + i * 100 * S
            if i % 3 == 2:
                o, ev = order_events(f"d{day}o{i}", "buy", PX, 5, t, cancel_at=t + 3 * S, cancel_sent=t + 3 * S - MS)
            else:
                o, ev = order_events(f"d{day}o{i}", "buy", PX, 5, t, fills=[(t + t_fill, 5)])
            for e in ev:
                if e["event_type"] == "submit_sent":
                    e["ts_local_send"] = t - LAT
            oe.append((o, ev))
    return log_from(oe, tmp_path / "live")


def _spec(tmp_path, live, ds, out, **kw):
    return {
        "dataset": {"path": str(ds)},
        "live": {"path": str(live)},
        "family": "power3",
        "grid": GRID,
        "horizons_ns": HORIZONS,
        "bootstrap": {"b": 50},
        "out": str(tmp_path / out),
        **kw,
    }


def test_latency_first_recovers_latency_and_queue(tmp_path):
    live, ds = _live(tmp_path), _dataset(tmp_path)
    cal = calibrate_live(_spec(tmp_path, live, ds, "first"), runner=LatencyAwareRunner(), latency_from_log=True)
    # step 1: the latency, measured on the live log (send to ack), is the true one
    assert cal.latency is not None
    assert cal.latency.entry_ns.p50 == LAT and cal.latency.entry_ns.n == 24
    assert cal.spec["entry"] == "send_plus_latency"
    assert cal.spec["latency"]["gateway_ns"] == LAT
    assert any(w.startswith("latency_first: entry latency fitted on the live log first: median 2e+03 ms") for w in
               cal.warnings)  # fmt: skip
    # step 2: the queue parameter, fitted with that latency, is the true one, inside its profile interval
    assert cal.estimate == pytest.approx(N_TRUE, rel=1e-9)
    lo, hi = cal.ci_profile
    assert (lo is None or lo <= N_TRUE) and (hi is None or N_TRUE <= hi)
    # the hftbacktest snippet carries both: the latency file and the queue model
    assert cal.exports.latency_npz == "latency.npz" and (tmp_path / "first" / "latency.npz").is_file()
    assert ".intp_order_latency(" in cal.exports.hftbacktest.python


def test_queue_without_latency_absorbs_it(tmp_path):
    """Send times with no latency: the model's clock starts LAT early, so the fitted n is too small (the simulator
    looks slower than it is); the latency-first fit is not."""
    live, ds = _live(tmp_path), _dataset(tmp_path)
    naive = calibrate_live(
        _spec(tmp_path, live, ds, "naive", entry="send_plus_latency", latency={"gateway_ns": 0}),
        runner=LatencyAwareRunner(),
    )
    assert naive.latency is None
    assert naive.estimate <= math.exp(0.25)  # at least one grid step below the true value


def test_exchange_ack_entry_needs_no_latency(tmp_path):
    """With exchange ack times the entry already includes the latency: the fit recovers n without the latency step."""
    live, ds = _live(tmp_path), _dataset(tmp_path)
    cal = calibrate_live(_spec(tmp_path, live, ds, "ack"), runner=LatencyAwareRunner())
    assert cal.estimate == pytest.approx(N_TRUE, rel=1e-9)


def test_latency_from_log_needs_send_and_ack_times(tmp_path):
    oe = []
    for i in range(4):
        o, ev = order_events(f"o{i}", "buy", PX, 5, T0 + i * S, fills=[(T0 + i * S + S, 5)])
        oe.append((o, [e for e in ev if e["event_type"] != "submit_sent"]))
    live = log_from(oe, tmp_path / "live")
    with pytest.raises(QtError, match="latency-from-log"):
        calibrate_live(_spec(tmp_path, live, _dataset(tmp_path), "x"), runner=LatencyAwareRunner(),
                       latency_from_log=True)  # fmt: skip
