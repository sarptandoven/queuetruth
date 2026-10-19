"""Scorer follow-ups: the live package's schedule accounting (`build_schedule` / `schedule_report`)
feeds the exclusion and entry-fallback counts, `gtd` rests, fill dedupe keys on the ORDER side, live exponents come
from `queuetruth.live.instrument_scales` (a scale change within the log is refused), and a same-price size decrease
keeps priority on CME (a priority-keeping amend, not a new leg)."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from queuetruth import live as qlive
from queuetruth.errors import QtError
from queuetruth.score import build_scorecard
from queuetruth.score._io import read_parquet, rows
from queuetruth.score.outcomes import log_outcomes
from queuetruth.score.runner import DefaultRunner
from queuetruth.score.scorecard import keep_priority_venues, not_scheduled_reasons
from queuetruth.score.units import scales_by_instrument
from score.qt_score_helpers import MS, T0, TICK, FakeRunner, S, log_from, make_dataset, order_events

PX9 = 100_000_000_000
PX8 = 10_000_000_000
BBO = [{"ts_event": T0 - S, "bid_px": PX9, "bid_sz": 5, "ask_px": PX9 + TICK, "ask_sz": 5}]


def _end_mbo():
    return [{"ts_event": T0 + 3600 * S, "action": ord("N"), "side": ord("N"), "price": 2**63 - 1}]


def _spec(live, ds=None, out=None, **kw):
    d = {"live": {"path": str(live)}, "sim": {"kind": "exact"}, "bootstrap": {"b": 30}, "perm_b": 30}
    if ds is not None:
        d["dataset"] = {"path": str(ds)}
    if out is not None:
        d["out"] = str(out)
    d.update(kw)
    return d


class _SchedRunner(FakeRunner):
    """Fake engine with the optional `build_schedule` of the default runner (real `queuetruth.live` schedule) that
    records the keep-priority venues and the amends handed to the exact replay."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.keep: list[frozenset[str] | None] = []
        self.replay_amends: list[list[dict]] = []

    def build_schedule(self, live_dir, *, entry, latency_ns, keep_priority_venues=None):
        self.keep.append(keep_priority_venues)
        return qlive.build_schedule(
            live_dir, entry=entry, latency_ns=latency_ns, keep_priority_venues=keep_priority_venues
        )

    def replay(self, spec, **opts):
        self.replay_amends.append([] if spec.amends is None else rows(read_parquet(spec.amends.path)))
        return super().replay(spec, **opts)


def _renumber(ev: list[dict]) -> list[dict]:
    for i, e in enumerate(ev, start=1):
        e["event_seq"] = i
        for k, v in (("ts_exchange_kind", "match_time"), ("ts_precision", "ns"), ("source", "synthetic")):
            e.setdefault(k, v)
        e.setdefault("derived", False)
    return ev


# ---- (1) schedule accounting from build_schedule --------------------------------------------------------------------


def test_default_runner_has_build_schedule():
    assert callable(getattr(DefaultRunner(), "build_schedule", None))


def test_exclusions_and_entry_fallback_come_from_the_live_schedule(tmp_path):
    a = order_events("a", "buy", PX9, 1, T0, fills=[(T0 + S, 1)])
    b = order_events("b", "buy", PX9, 1, T0, tif="fok")
    c = order_events("c", "buy", PX9, 1, T0)
    c[0]["instrument_id"] = None
    d = order_events("d", "buy", PX9, 1, T0 + 2 * S)
    for e in d[1]:
        if e["event_type"] == "ack":
            e["ts_exchange_kind"] = "record_time"  # not an entry kind: falls back to send_plus_latency
    live = log_from([a, b, c, d], tmp_path / "live")
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = _SchedRunner(bbo=BBO)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = build_scorecard(_spec(live, ds), runner=fr)
    assert not [w for w in caught if issubclass(w.category, qlive.LiveScheduleWarning)]  # counts go to the summary
    assert s.n_matched == 2
    assert (
        "not_scheduled: 2 live order(s) not schedulable (1 missing instrument_id: pass --instrument-map; "
        "1 FOK time in force (not resting))"
    ) in s.warnings
    assert any(w.startswith("schedule: entry_fallback_share=1/2") for w in s.warnings)
    assert not any(w.startswith("schedule: excluded") for w in s.warnings)


def test_not_scored_reasons_from_the_live_schedule(tmp_path):
    b = order_events("b", "buy", PX9, 1, T0, tif="ioc")
    c = order_events("c", "buy", PX9, 1, T0)
    c[0]["order_type"] = "market"
    live = log_from([b, c], tmp_path / "live")
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, make_dataset(tmp_path / "ds", _end_mbo())), runner=_SchedRunner(bbo=BBO))
    msg = str(e.value)
    assert e.value.code == "QT-INPUT-002" and msg.startswith("QT-INPUT-002: NOT SCORED: 0 of 2 live orders scored (")
    assert "1 market orders (only resting limit orders are scheduled)" in msg and "1 IOC time in force" in msg


# ---- (2) gtd rests --------------------------------------------------------------------------------------------------


def test_gtd_is_a_resting_tif_in_the_inferred_reasons():
    orders = [
        {"order_key": "g1", "order_type": "limit", "tif": "gtd", "limit_px": 1, "instrument_id": None},
        {"order_key": "g2", "order_type": "limit", "tif": "gtd", "limit_px": 1, "instrument_id": 1},
        {"order_key": "i1", "order_type": "limit", "tif": "ioc", "limit_px": 1, "instrument_id": 1},
    ]
    assert not_scheduled_reasons(orders, [], scheduled={"g2"}) == {"no_instrument_id": 1, "tif_ioc": 1}


# ---- (3) fill dedupe on (venue, instrument, order side, fill_id) ----------------------------------------------------


def test_fill_dedupe_key_uses_the_order_side():
    o = {"venue": "binance", "instrument": "BTCUSDT", "orig_qty": 5}
    orders = [
        {**o, "order_key": "s1", "side": "sell"},
        {**o, "order_key": "b1", "side": "buy"},
        {**o, "order_key": "b2", "side": "buy"},
    ]

    def fill(k, seq):
        return {
            "order_key": k,
            "event_seq": seq,
            "event_type": "fill",
            "qty": 5,
            "px": 1,
            "fill_id": "T1",
            "ts_exchange": T0,
        }

    oc, w = log_outcomes(orders, [fill("s1", 1), fill("b1", 1), fill("b2", 1)])
    # s1 (sell) and b1 (buy) are the two sides of one trade; b2 repeats b1's (venue, instrument, buy, T1) key
    assert (oc["s1"].filled_qty, oc["b1"].filled_qty, oc["b2"].filled_qty) == (5, 5, 0)
    assert any(x.startswith("fill_dedupe: 1 fill(s) with a repeated (venue, instrument, side, fill_id)") for x in w)


# ---- (4) live exponents from instrument_scales ----------------------------------------------------------------------


def _with_scales_metadata(live: Path, entries: list[dict]) -> None:
    p = live / "orders.parquet"
    t = pq.read_table(p)
    meta = dict(t.schema.metadata or {})
    meta[b"qt.live.instrument_scales"] = json.dumps(entries).encode()
    pq.write_table(t.replace_schema_metadata(meta), p)


def test_live_exponents_come_from_instrument_scales(tmp_path):
    """The `qt.live.instrument_scales` metadata (importer's instrument map) is the source of the live scale: here it
    says price_exp -8 for prices written at that scale, and the scorer converts them to the dataset's -9."""
    o, ev = order_events("o1", "buy", PX8, 10, T0, fills=[(T0 + 2 * S, 10)])
    live = log_from([(o, ev)], tmp_path / "live")
    entry = {"venue": "test", "instrument": "SYM1", "instrument_id": 1, "price_exp": -8, "size_exp": 0}
    _with_scales_metadata(live, [{**entry, "scale_source": "instrument_map", "n_orders": 1}])
    assert qlive.instrument_scales(live)[0]["price_exp"] == -8
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    fr = FakeRunner(sim_fills={"o1": [(T0 + 3 * S, 10, "queue_depleted")]}, bbo=BBO)
    s = build_scorecard(_spec(live, ds), runner=fr)
    assert fr.schedules[0][0]["price"] == PX9
    assert any(w.startswith("units: live log instrument 1 price_exp -8 -> -9") for w in s.warnings)
    assert {r.arm: r.mean_ticks for r in s.markouts if r.horizon_ns == S and r.ref == "mid"} == {
        "live": 0.5,
        "sim": 0.5,
    }


def test_scale_change_within_the_log_is_refused(tmp_path):
    a = order_events("a", "buy", PX9, 1, T0)
    b = order_events("b", "buy", PX8, 1, T0 + S)
    b[0]["price_exp"] = -8  # a date-dependent instrument-map row: instrument 1 changes scale within the log
    live = log_from([a, b], tmp_path / "live")
    with pytest.raises(QtError) as e:
        build_scorecard(_spec(live, make_dataset(tmp_path / "ds", _end_mbo())), runner=FakeRunner(bbo=BBO))
    msg = str(e.value)
    assert e.value.code == "QT-INPUT-002"
    assert "instrument 1 changes scale within the log" in msg
    assert "price_exp -9 / size_exp 0 on 1 order(s)" in msg and "price_exp -8 / size_exp 0 on 1 order(s)" in msg
    assert e.value.hint is not None and "split the live log" in e.value.hint


def test_scales_by_instrument_merges_renames_and_refuses_changes():
    same = [
        {"instrument_id": 1, "instrument": "BTC-USD", "price_exp": -2, "size_exp": -8, "n_orders": 3},
        {"instrument_id": 1, "instrument": "BTCUSD", "price_exp": -2, "size_exp": -8, "n_orders": 2},
        {"instrument_id": None, "instrument": "X", "price_exp": -9, "size_exp": 0, "n_orders": 4},
    ]
    assert scales_by_instrument(same) == {1: (-2, -8)}
    with pytest.raises(QtError) as e:
        scales_by_instrument([*same, {"instrument_id": 1, "price_exp": -2, "size_exp": -6, "n_orders": 1}])
    assert "on 5 order(s)" in str(e.value) and "size_exp -6 on 1 order(s)" in str(e.value)


# ---- (5) CME: a same-price size decrease keeps priority -------------------------------------------------------------


def _decrease_log(tmp_path: Path, venue: str) -> Path:
    o, ev = order_events("K", "buy", PX9, 10, T0, venue=venue)
    ev.append({"order_key": "K", "event_type": "amend_sent", "ts_local_send": T0 + 5 * S - MS, "qty": 6, "px": PX9})
    ev.append({"order_key": "K", "event_type": "amend_ack", "ts_exchange": T0 + 5 * S, "qty": 6, "px": PX9})
    return log_from([(o, _renumber(ev))], tmp_path / "live")


def _dataset(tmp_path: Path, venue: str) -> Path:
    ds = make_dataset(tmp_path / "ds", _end_mbo())
    man = json.loads((ds / "manifest.json").read_text())
    man["venue"] = venue
    (ds / "manifest.json").write_text(json.dumps(man))
    return ds


@pytest.mark.parametrize(
    ("live_venue", "ds_venue", "kept"),
    [("cme", "cme", True), ("fix44", "cme", True), ("CME", "generic", True), ("coinbase", "coinbase", False)],
)
def test_same_price_decrease_keeps_priority_on_cme(tmp_path, live_venue, ds_venue, kept):
    live = _decrease_log(tmp_path, live_venue)
    fr = _SchedRunner(bbo=BBO, sim_fills={"K": [(T0 + 7 * S, 10, "queue_depleted")]})
    s = build_scorecard(_spec(live, _dataset(tmp_path, ds_venue), tmp_path / "out"), runner=fr)
    legs = [r["shadow_id"] for r in fr.schedules[0]]
    if kept:
        assert legs == ["K"]
        assert [(a["shadow_id"], a["ts_send"], a["new_qty"]) for a in fr.replay_amends[0]] == [("K", T0 + 5 * S, 6)]
        so = rows(read_parquet(tmp_path / "out" / "score_orders.parquet"))[0]
        assert so["legs"] == 1 and so["sim_filled_qty"] == 6  # the amended total caps the sim fills
    else:
        assert legs == ["K", "K.leg2"] and fr.replay_amends[0] == []
    assert s.n_matched == 1


def test_keep_priority_venues():
    base = set(qlive.KEEP_PRIORITY_VENUES)
    orders = [{"venue": "fix44"}, {"venue": "CME"}, {"venue": "binance"}]
    assert keep_priority_venues(orders, None) == frozenset(base | {"CME"})
    assert keep_priority_venues(orders, "cme") == frozenset(base | {"CME", "fix44", "binance"})
