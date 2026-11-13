"""Pinned order schedules of every caller that used to build them on its own, now routed to
`queuetruth.schemas.schedule`.

`schedule_pins.json` holds the outputs computed before the callers moved: the live-log schedule (`build_schedule`,
`to_schedule`, `schedule_report`) on a constructed log that exercises every rule (acks and send fallbacks, keep and
lose priority amends, the CME unchanged modify, an amend to zero, expiry, rejects and exclusions), the scorer's unit
conversion of those rows, the replay export (`read_schedule`, `schedule_actions`, both replay programs) and the
sandbox's episode schedule. Tables are pinned with their Arrow schema and every value. Never regenerate it.
`python tests/live/test_schedule_pins.py --record` exists only to make the first record.
"""

from __future__ import annotations

import json
import random
import sys
import warnings
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

PINS_PATH = Path(__file__).parent / "schedule_pins.json"
T0 = 1_800_000_000_000_000_000


def h(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        return [h(v) for v in x]
    if isinstance(x, dict):
        return {str(k): h(v) for k, v in x.items()}
    if isinstance(x, bool):
        return x
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return float(x).hex()
    if hasattr(x, "value") and hasattr(x, "tzinfo"):  # pandas-free Arrow timestamps come back as datetime
        return int(x.value)
    if hasattr(x, "timestamp") and hasattr(x, "tzinfo"):
        return str(x)
    return x


def _table(t: pa.Table) -> dict[str, Any]:
    ints = t.cast(
        pa.schema(
            [pa.field(f.name, pa.int64(), f.nullable) if pa.types.is_timestamp(f.type) else f for f in t.schema],
            metadata=t.schema.metadata,
        )
    )
    return {"schema": str(t.schema), "columns": h(ints.to_pydict())}


def _order(key: str, venue: str = "v", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_key": key,
        "venue": venue,
        "account": "a",
        "instrument": "X",
        "instrument_id": 7,
        "side": "buy",
        "order_type": "limit",
        "tif": "gtc",
        "orig_qty": 10,
        "qty_str": "10",
        "limit_px": 100,
        "px_str": "100",
        "price_exp": 0,
        "size_exp": 0,
    }
    row.update(kw)
    return row


def _ev(key: str, seq: int, event_type: str, ts: int | None = None, kind: str = "gateway_time", **kw: Any) -> dict:
    row: dict[str, Any] = {
        "order_key": key,
        "event_seq": seq,
        "event_type": event_type,
        "ts_exchange": None if ts is None else T0 + ts,
        "ts_exchange_kind": kind,
        "ts_precision": "ns",
        "source": "synthetic",
    }
    for k in ("ts_local_send", "ts_local_recv"):
        if k in kw and kw[k] is not None:
            kw[k] = T0 + kw[k]
    row.update(kw)
    return row


def _log(root: Path) -> str:
    """A constructed live log (synthetic prices and times) that exercises every schedule rule."""
    from queuetruth.live import write_canonical_log

    orders, events = [], []

    def add(o: dict, evs: list[tuple]) -> None:
        orders.append(o)
        for i, (et, ts, *rest) in enumerate(evs, 1):
            kw = rest[0] if rest else {}
            events.append(_ev(o["order_key"], i, et, ts, **kw))

    add(
        _order("v:a:1"),
        [
            ("submit_sent", None, {"kind": "unknown", "ts_local_send": 0}),
            ("ack", 100, {"qty": 10, "cum_qty": 0, "leaves_qty": 10}),
            ("fill", 200, {"kind": "match_time", "qty": 2, "cum_qty": 2, "leaves_qty": 8, "fill_id": "f1"}),
            ("amend_ack", 300, {"qty": 6, "cum_qty": 2, "leaves_qty": 4, "amend_keeps_priority": True}),
            ("cancel_sent", None, {"kind": "unknown", "ts_local_send": 850}),
            ("cancel_ack", 900, {"cum_qty": 2, "leaves_qty": 0}),
        ],
    )
    add(
        _order("v:a:2", side="sell", limit_px=105),
        [
            ("ack", 110, {"qty": 10, "cum_qty": 0, "leaves_qty": 10}),
            ("amend_ack", 300, {"qty": 12, "px": 106, "cum_qty": 0, "leaves_qty": 12, "amend_keeps_priority": False}),
            ("amend_ack", 500, {"qty": 9, "px": 106, "cum_qty": 0, "leaves_qty": 9, "amend_keeps_priority": True}),
            ("amend_ack", 700, {"qty": 0, "px": 106, "cum_qty": 0, "leaves_qty": 0, "amend_keeps_priority": False}),
        ],
    )
    add(
        _order("v:a:3", venue="kraken"),
        [
            ("ack", 120, {"qty": 10, "cum_qty": 0, "leaves_qty": 10}),
            ("amend_ack", 400, {"qty": 7, "cum_qty": 0, "leaves_qty": 7}),
            ("expire", 990, {"cum_qty": 0, "leaves_qty": 0}),
        ],
    )
    add(
        _order("v:a:4", venue="CME", tif="day"),
        [
            ("ack", 130, {"qty": 10, "cum_qty": 0, "leaves_qty": 10}),
            ("amend_ack", 250, {"qty": 10, "cum_qty": 0, "leaves_qty": 10}),
            ("fill", 260, {"kind": "match_time", "qty": 10, "cum_qty": 10, "leaves_qty": 0, "fill_id": "f9"}),
        ],
    )
    add(
        _order("v:a:5"),
        [
            ("submit_sent", None, {"kind": "unknown", "ts_local_send": 140}),
            ("ack", None, {"kind": "unknown", "ts_local_recv": 160, "qty": 10, "cum_qty": 0, "leaves_qty": 10}),
            ("cancel_sent", None, {"kind": "unknown", "ts_local_send": 600}),
            ("cancel_ack", None, {"kind": "unknown", "ts_local_recv": 640, "cum_qty": 0, "leaves_qty": 0}),
        ],
    )
    add(_order("v:a:6", order_type="market", limit_px=None, px_str=None), [("ack", 150, {"qty": 10})])
    add(_order("v:a:7", tif="ioc"), [("ack", 150, {"qty": 10})])
    add(_order("v:a:8"), [("reject", 170, {})])
    add(_order("v:a:9", instrument_id=None), [("ack", 180, {"qty": 10})])
    write_canonical_log(root, orders, events)
    return str(root)


def live_outputs(tmp: Path) -> dict[str, Any]:
    from queuetruth.live import build_schedule, schedule_report, to_schedule
    from queuetruth.score._io import rows
    from queuetruth.score.units import UnitMap

    d = _log(tmp / "live")
    out: dict[str, Any] = {}
    for entry, lat in (("exchange_ack", None), ("send_plus_latency", None), ("send_plus_latency", 25)):
        s = build_schedule(d, entry=entry, latency_ns=lat)
        key = f"{entry}:{lat}"
        out[key] = {
            "spec": _table(s.spec),
            "amends": _table(s.amends),
            "legs": s.legs,
            "report": h(schedule_report(s)),
        }
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            spec, am = to_schedule(d, entry=entry, latency_ns=lat)
        out[key]["to_schedule_same"] = spec.equals(s.spec, check_metadata=True) and am.equals(
            s.amends, check_metadata=True
        )
        out[key]["to_schedule_warnings"] = [str(x.message) for x in w]
    s = build_schedule(d, entry="exchange_ack")
    um = UnitMap({7: (-2, -1)})
    um.add_orders([{"order_key": k, "instrument_id": 7, "price_exp": 0, "size_exp": 0} for k in s.legs])
    srows = rows(s.spec)
    out["units"] = {"schedule": h(um.schedule(srows)), "amends": h(um.amends(rows(s.amends), srows))}
    return out


def replay_outputs(tmp: Path) -> dict[str, Any]:
    from queuetruth.export.replay import read_schedule, replay_schedule, schedule_actions
    from queuetruth.live import to_schedule
    from queuetruth.schemas.io import write_table

    d = _log(tmp / "live2")
    out: dict[str, Any] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        spec, am = to_schedule(d, entry="exchange_ack", latency_ns=None)
    write_table(spec, tmp / "sched" / "schedule.parquet")
    write_table(am, tmp / "sched" / "schedule_amends.parquet")
    legs, amends = read_schedule(tmp / "sched" / "schedule.parquet")
    keep = ("shadow_id", "instrument_id", "side", "price_mode", "price", "qty", "ts_submit", "ts_cancel", "order_key")
    out["read_schedule"] = h([[r.get(k) for k in keep] for r in legs])
    out["read_amends"] = h([[r["shadow_id"], r["ts_send"], r["new_qty"]] for r in amends])
    out["actions"] = h([list(a.__dict__.values()) for a in schedule_actions(legs, amends, post_only=True)])
    units = {7: {"price_exp": -2, "size_exp": 0, "tick_size": "0.01", "lot_size": "1", "symbol": "X"}}
    from queuetruth._version import __version__

    for target in ("hftbacktest", "nautilus"):
        for src in ("schedule", "live"):
            p = tmp / f"{target}_{src}.py"
            kw = {"schedule": str(tmp / "sched" / "schedule.parquet")} if src == "schedule" else {"live": d}
            r = replay_schedule(target, p, units=units, **kw)  # type: ignore[arg-type]
            text = p.read_text(encoding="utf-8").replace(__version__, "<version>").replace(str(tmp), "<tmp>")
            out[f"{target}:{src}"] = {"text": text, "n_orders": r.n_orders, "n_actions": r.n_actions}
    return out


def sandbox_outputs(tmp: Path) -> dict[str, Any]:
    tests = Path(__file__).resolve().parents[1]
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    from sandbox.sbx_helpers import SYNTH, random_agent

    from queuetruth.sandbox import Sandbox
    from queuetruth.sandbox.orders import schedule

    with Sandbox.open(
        {
            "dataset": {"path": str(SYNTH)},
            "instrument_id": 2,
            "session_date": "2026-06-15",
            "start": "15:00",
            "end": "15:15",
            "latency": "300us",
        }
    ) as sb:
        random_agent(sb, random.Random(5), steps=60)
        spec, am = schedule(sb.orders, 2)
        sb.finish()
    return {"spec": _table(spec), "amends": _table(am)}


def scorer_outputs(tmp: Path) -> dict[str, Any]:
    """The scorer's schedule file and the schedules it sends to the engine, on the scorer tests' synthetic orders,
    with the helpers' minimal schedule and with the live-log schedule."""
    tests = Path(__file__).resolve().parents[1]
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    from score.qt_score_helpers import T0 as ST0
    from score.qt_score_helpers import FakeRunner, S, log_from, make_dataset, order_events

    from queuetruth.live import to_schedule
    from queuetruth.score import build_scorecard
    from queuetruth.score._io import read_parquet

    px = 100_000_000_000
    oe = [
        order_events("o1", "buy", px, 10, ST0, fills=[(ST0 + 2 * S, 10)]),
        order_events("o2", "buy", px, 10, ST0 + S, cancel_at=ST0 + 30 * S),
        order_events("o3", "sell", px + 10_000_000, 5, ST0 + 2 * S, fills=[(ST0 + 5 * S, 5)]),
        order_events("o4", "buy", px, 3, ST0 + 3 * S),
    ]
    live = log_from(oe, tmp / "slive")
    ds = make_dataset(
        tmp / "sds", [{"ts_event": ST0 + 3600 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}]
    )
    out: dict[str, Any] = {}
    for name, fn in (("helper", None), ("live", to_schedule)):
        kw = {} if fn is None else {"schedule_fn": fn}
        fr = FakeRunner(
            sim_fills={"o1": [(ST0 + 3 * S, 10, "queue_depleted")], "o4": [(ST0 + 9 * S, 3, "queue_depleted")]},
            bbo=[{"ts_event": ST0 - S, "bid_px": px, "bid_sz": 5, "ask_px": px + 10_000_000, "ask_sz": 5}],
            **kw,
        )
        spec = {
            "live": {"path": str(live)},
            "sim": {"kind": "exact"},
            "bootstrap": {"b": 20},
            "perm_b": 20,
            "dataset": {"path": str(ds)},
            "out": str(tmp / f"sout_{name}"),
        }
        build_scorecard(spec, runner=fr)
        out[name] = {
            "schedule": _table(read_parquet(tmp / f"sout_{name}" / "schedule.parquet")),
            "sent": h(fr.schedules),
        }
    return out


def outputs(tmp: Path, *, core: bool) -> dict[str, Any]:
    out = {"live": live_outputs(tmp), "replay": replay_outputs(tmp), "scorer": scorer_outputs(tmp)}
    if core:
        out["sandbox"] = sandbox_outputs(tmp)
    return out


def _pins() -> dict[str, Any]:
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def test_live_schedule_is_pinned(tmp_path: Path) -> None:
    assert live_outputs(tmp_path) == _pins()["live"]


def test_scorer_schedule_is_pinned(tmp_path: Path) -> None:
    assert scorer_outputs(tmp_path) == _pins()["scorer"]


def test_replay_schedule_is_pinned(tmp_path: Path) -> None:
    assert replay_outputs(tmp_path) == _pins()["replay"]


@pytest.mark.requires_core
def test_sandbox_schedule_is_pinned(tmp_path: Path) -> None:
    assert sandbox_outputs(tmp_path) == _pins()["sandbox"]


if __name__ == "__main__":  # first record only (module docstring)
    import tempfile

    if "--record" not in sys.argv:
        raise SystemExit("usage: python tests/live/test_schedule_pins.py --record")
    with tempfile.TemporaryDirectory() as d:
        PINS_PATH.write_text(json.dumps(outputs(Path(d), core=True), indent=1, sort_keys=True) + "\n")
