"""calibrate_live: grid profile likelihood against live outcomes, chronological split, held-out discordance
(`kind = live`, `held_out_discordance`), outcome-independent held-out eligibility (A2, Python review F1), taker fills
count as fills (review F9), fail-safe refusals (trader review D1) and `trust:` lines, and the result feeds the exports
(fake zoo runner whose models fill at 10 s / n)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from queuetruth.errors import QtError
from queuetruth.export import hftbacktest_snippet, nautilus_snippet
from queuetruth.schemas.io import write_table
from queuetruth.schemas.models import CalibrationResult
from queuetruth.score import calibrate_live
from queuetruth.score._io import read_parquet, rows
from queuetruth.score.calibrate_live import interval_loglik
from score.qt_score_helpers import DAY, T0, FakeRunner, log_from, make_dataset, make_table, order_events

S = 1_000_000_000
PX = 100_000_000_000


class ZooByParam(FakeRunner):
    """Model with parameter v fills each shadow at `10 s / v` (power families) or never when v <= 0."""

    def run_zoo(self, spec):
        self.zoo_specs.append(spec)
        out = Path(spec.out)
        out.mkdir(parents=True, exist_ok=True)
        preds = []
        for s in rows(read_parquet(spec.shadows.path)):
            for mid in spec.models:
                v = float(re.search(r"n=([^;\]]+)", mid).group(1))
                t_fill = 10 * S / v
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
                            "ts_live": s["ts_submit"],
                            "trade_vol_60s": 0,
                        }
                    )
        from queuetruth.schemas.io import write_table

        write_table(make_table("qt.zoo_predictions", preds), out / "zoo_predictions.parquet")


def _log(tmp_path):
    oe = []
    for day in (0, 1):
        for i in range(12):
            t = T0 + day * DAY + i * 100 * S
            if i % 3 == 2:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, cancel_at=t + 30 * S, cancel_sent=t + 30 * S))
            else:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, fills=[(t + 3 * S, 5)]))
    return log_from(oe, tmp_path / "live")


def _l2_rows(day_ts: int, end_ts: int, tick: int) -> list[dict]:
    """One instrument-day of `qt.l2`: a clear + snapshot batch (bid PX x 10, ask PX + 1 tick x 10) at the session
    start and a quiet depth row at `end_ts` (the end of data)."""
    base = {"instrument_id": 1, "prev_ct": 0, "new_ct": 1, "trade_qty": 0, "aggressor": ord("N"), "level": 0}
    rows_ = [
        {**base, "kind": "clear", "side": ord("N"), "price": None, "prev_qty": 0, "new_qty": 0, "prev_ct": None,
         "new_ct": None, "flags": 8},
        {**base, "kind": "snapshot", "side": ord("B"), "price": PX, "prev_qty": 0, "new_qty": 10, "flags": 8},
        {**base, "kind": "snapshot", "side": ord("A"), "price": PX + tick, "prev_qty": 0, "new_qty": 10, "flags": 9},
    ]  # fmt: skip
    out = [{**r, "ts_event": day_ts, "ts_recv": day_ts, "event_id": 1, "sequence": 1} for r in rows_]
    out.append(
        {**base, "kind": "depth", "side": ord("A"), "price": PX + tick, "prev_qty": 10, "new_qty": 11, "prev_ct": 1,
         "new_ct": 2, "flags": 1, "ts_event": end_ts, "ts_recv": end_ts, "event_id": 2, "sequence": 2}
    )  # fmt: skip
    return out


def _dataset(tmp_path, n_days=2):
    """L2-only dataset (`qt.l2` partitions) with one instrument-day per date; data ends 7200 s after T0 each day."""
    from score.qt_score_helpers import TICK

    ds = make_dataset(tmp_path / "ds", [], n_days=n_days)
    man = json.loads((ds / "manifest.json").read_text())
    parts = []
    for d, part in enumerate(sorted(man["partitions"], key=lambda p: p["session_date"])):
        day_ts = T0 + d * DAY - 3600 * S
        rel = f"l2/day{d}/instrument_id=1/part-00000.parquet"
        fe = write_table(make_table("qt.l2", _l2_rows(day_ts, T0 + d * DAY + 7200 * S, TICK)), ds / rel)
        parts.append(
            {**part, "files": [{"path": rel, "rows": fe["rows"], "sha256": fe["sha256"]}], "rows": fe["rows"],
             "ts_min": day_ts, "ts_max": T0 + d * DAY + 7200 * S}
        )  # fmt: skip
    man.update(kind="l2", partitions=parts)
    (ds / "manifest.json").write_text(json.dumps(man))
    return ds


def test_interval_loglik_matches_hand_computation():
    p = np.array([0.0, 0.5, 1.0])
    s = (1 - p + 0.5) / 2
    h = 1 - s / np.concatenate([[1.0], s[:-1]])
    assert interval_loglik(p, 2, 1, 1) == pytest.approx(np.log(1 - h[0]) + np.log(h[1]))
    assert interval_loglik(p, 3, 0, 1) == pytest.approx(np.log(1 - h[0]) + np.log(1 - h[1]) + np.log(1 - h[2]))


def test_calibrate_live_power3_fit_split_and_exports(tmp_path):
    live = _log(tmp_path)
    ds = _dataset(tmp_path)

    fr = ZooByParam()
    cal = calibrate_live(
        {
            "dataset": {"path": str(ds)},
            "live": {"path": str(live)},
            "family": "power3",
            "horizons_ns": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S],
            "bootstrap": {"b": 30},
            "out": str(tmp_path / "out"),
        },
        runner=fr,
    )
    assert isinstance(cal, CalibrationResult)
    assert cal.kind == "live" and cal.family == "power3" and cal.param_name == "n"
    assert cal.spec["calibration_source"] == "live"
    assert 2.5 <= cal.estimate < 5.0  # model fills at 10/n s inside (2, 4] like the live fills at 3 s
    assert cal.train.dates == ["2026-01-01"] and cal.test.dates == ["2026-01-02"]
    assert cal.n_shadows == 12 and cal.n_events == 8
    fitted = next(iter(cal.held_out))
    assert cal.held_out[fitted].fill_rate_bias["4000000000"] == pytest.approx(4 / 12)
    assert cal.held_out[fitted].fill_rate_bias["2000000000"] == pytest.approx(0.0)
    d = cal.held_out_discordance
    assert d is not None and (d.n, d.b, d.c) == (12, 0, 4)
    assert d.fill_rate_bias == pytest.approx(4 / 12) and d.clustered_method
    assert cal.stratified == []  # the v0.1 `held_out_test` echo rows are gone now that the field exists
    zs = fr.zoo_specs[0]
    assert zs.h_max_ns == 60 * S and len(zs.models) == 30
    assert "power_prob_queue_model3(" in cal.exports.hftbacktest.python
    assert hftbacktest_snippet(cal) == cal.exports.hftbacktest.python
    nt = nautilus_snippet(cal)
    assert "# fill_model = FillModel(prob_fill_on_limit=1)  # NautilusTrader default, NOT fitted" in nt
    assert not any(ln.startswith(("fill_model", "latency_model")) for ln in nt.splitlines())
    doc = json.loads((tmp_path / "out" / "calibration.json").read_text())
    assert CalibrationResult.model_validate(doc).estimate == cal.estimate
    assert json.loads((tmp_path / "out" / "live_calibration_test.json").read_text())["c"] == 4
    # Trader review E2: the own resting orders were removed from the L2 depth before the zoo run.
    assert any(w.startswith("self_exclusion: own resting orders removed from the L2 depth") for w in cal.warnings)
    assert not any(w.startswith("trust: own_orders_in_depth") for w in cal.warnings)
    assert cal.trust is not None and cal.trust.verdict == "refused" and cal.trust.reasons == ["few_events"]
    assert cal.trust.beats_default["4000000000"] == "better" and "plateau" in cal.trust.flags
    # The fitted n is one point of a 3-point plateau and the bootstrap picks one grid value every time.
    assert any(w.startswith("trust: plateau: the likelihood ties over 3 grid points") for w in cal.warnings)
    assert any(w.startswith("trust: ci_grid_resolution:") for w in cal.warnings) and cal.ci_bootstrap == (None, None)
    beats = next(w for w in cal.warnings if w.startswith("trust: beats_default: "))
    assert "4000000000=better" in beats and "60000000000=tie" in beats
    assert cal.warnings[-1] == "trust: verdict: refused (few_events)"
    assert "# !! NOT VALIDATED" in cal.exports.hftbacktest.python


def test_calibrate_live_honours_block_windows(tmp_path):
    """`train`/`test` `blocks` select equal-duration blocks of each date's session (intersected with `dates`):
    10-minute blocks of the 24 h test session put day one's orders 0-5 in block 60 and 6-11 in block 61."""
    live = _log(tmp_path)
    ds = _dataset(tmp_path)
    cal = calibrate_live(
        {
            "dataset": {"path": str(ds)},
            "live": {"path": str(live)},
            "family": "power3",
            "horizons_ns": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S],
            "bootstrap": {"b": 10},
            "train": {"dates": ["2026-01-01"], "blocks": {"index": [60], "n_blocks": 144}},
            "test": {"dates": ["2026-01-01"], "blocks": {"index": [61], "n_blocks": 144}},
            "out": str(tmp_path / "out"),
        },
        runner=ZooByParam(),
    )
    assert cal.n_shadows == 6
    assert cal.train.blocks is not None and cal.train.blocks.index == [60] and cal.train.dates == ["2026-01-01"]
    assert cal.test.blocks is not None and cal.test.blocks.index == [61]
    doc = json.loads((tmp_path / "out" / "live_calibration_test.json").read_text())
    assert doc["n"] == 6


def _cal(tmp_path, live, ds, **kw):
    spec = {
        "dataset": {"path": str(ds)},
        "live": {"path": str(live)},
        "family": "power3",
        "horizons_ns": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S],
        "bootstrap": {"b": 10},
    }
    spec.update(kw)
    return calibrate_live(spec, runner=ZooByParam())


def test_held_out_keeps_cancelled_unfilled_orders_a2(tmp_path):
    """Python review F1 (ported from the review's `exp_calive_heldout.py`): orders cancelled unfilled at 30 s used to be
    dropped from the held-out metrics at every horizon past 30 s (outcome-dependent censoring), so the fill-rate bias
    at H = 60 s read 0.000 while the 2x2 said c = 4 (+1/3). They stay in with label 0; only an end-of-data cut makes
    a horizon undefined. Here an extra test order 10 s before the end of data is in at 8 s and out at 16 s / 60 s."""
    oe = []
    for day in (0, 1):
        for i in range(12):
            t = T0 + day * DAY + i * 100 * S
            if i % 3 == 2:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, cancel_at=t + 30 * S, cancel_sent=t + 30 * S))
            else:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, fills=[(t + 3 * S, 5)]))
    oe.append(order_events("d1late", "buy", PX, 5, T0 + DAY + 7190 * S))
    live = log_from(oe, tmp_path / "live")
    cal = _cal(tmp_path, live, _dataset(tmp_path))
    fitted = next(iter(cal.held_out))
    bias, brier = cal.held_out[fitted].fill_rate_bias, cal.held_out[fitted].brier
    assert bias["60000000000"] == pytest.approx(4 / 12)  # was 0.000: cancelled orders were dropped
    assert brier["60000000000"] == pytest.approx(4 / 12)
    assert bias["16000000000"] == pytest.approx(4 / 12)  # the late order is cut by the end of data at 10 s: out
    assert bias["8000000000"] == pytest.approx(5 / 13)  # ... but observed (label 0) at 8 s: in
    d = cal.held_out_discordance
    assert d is not None and (d.n, d.b, d.c) == (13, 0, 5)


def test_taker_filled_orders_count_as_filled(tmp_path):
    """Python review F9 (ported from the review's `exp_calive_taker.py`): 24 orders fully filled 1 s after entry as
    taker used to count as resting unfilled through H (n_events = 0, estimate at the slow grid end). They are fills
    now; filling at 1 s needs n >= 10, so the maximum sits on the upper grid plateau and is flagged."""
    oe = []
    for day in (0, 1):
        for i in range(12):
            t = T0 + day * DAY + i * 100 * S
            oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, fills=[(t + 1 * S, 5)], liquidity="taker"))
    cal = _cal(tmp_path, log_from(oe, tmp_path / "live"), _dataset(tmp_path))
    assert cal.n_events == 12 and cal.n_shadows == 12
    assert cal.estimate >= 10.0
    assert any(w.startswith("taker_fills: 24 order(s) first filled as taker") for w in cal.warnings)
    assert any(w.startswith("trust: at_grid_edge: the maximum is at the upper end") for w in cal.warnings)
    assert "at_grid_edge" in cal.warnings[-1]


def test_refuses_empty_or_missing_windows_and_unfilled_train(tmp_path):
    """Trader review D1: never a fit on nothing."""
    live = _log(tmp_path)
    ds = _dataset(tmp_path)
    with pytest.raises(QtError) as e:
        _cal(tmp_path, live, ds, train={"dates": ["2026-01-05"]})
    assert e.value.code == "QT-DATA-001" and "2026-01-05" in e.value.message
    assert "available: 2026-01-01, 2026-01-02" in e.value.message
    with pytest.raises(QtError) as e:
        _cal(tmp_path, live, ds, test={"dates": ["2026-01-07"]})
    assert e.value.code == "QT-DATA-001"
    # A date with data but no live order: empty test window.
    ds3 = _dataset(tmp_path / "three", n_days=3)
    with pytest.raises(QtError) as e:
        _cal(tmp_path, live, ds3, train={"dates": ["2026-01-01"]}, test={"dates": ["2026-01-03"]})
    assert e.value.code == "QT-DATA-001" and "test window" in e.value.message
    with pytest.raises(QtError) as e:
        _cal(tmp_path, live, ds3, train={"dates": ["2026-01-03"]}, test={"dates": ["2026-01-01"]})
    assert e.value.code == "QT-DATA-001" and "train window" in e.value.message
    # No train order filled: every grid value that never fills would win.
    oe = []
    for day in (0, 1):
        for i in range(4):
            t = T0 + day * DAY + i * 100 * S
            oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, cancel_at=t + 30 * S, cancel_sent=t + 30 * S))
    with pytest.raises(QtError) as e:
        _cal(tmp_path, log_from(oe, tmp_path / "unfilled"), ds)
    assert e.value.code == "QT-DATA-001" and "filled" in e.value.message


def test_l2_own_orders_kept_in_depth_refuse_the_fit(tmp_path):
    """Trader review E2: without own-order removal (self_exclude=False) the twins queue behind their own orders; the
    fit is still written but refused for export (`own_orders_in_depth`)."""
    live = _log(tmp_path)
    ds = _dataset(tmp_path)
    cal = calibrate_live(
        {"dataset": {"path": str(ds)}, "live": {"path": str(live)}, "family": "power3",
         "horizons_ns": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S], "bootstrap": {"b": 10}},
        runner=ZooByParam(),
        self_exclude=False,
    )  # fmt: skip
    assert any(w.startswith("trust: own_orders_in_depth: ") and "self_exclude is false" in w for w in cal.warnings)
    assert cal.trust is not None and "own_orders_in_depth" in cal.trust.reasons


def test_units_are_converted_to_the_dataset(tmp_path):
    """Review F4 for calibrate-live: a live log at size_exp -2 on a dataset at size_exp 0 is scheduled in dataset
    units (the zoo sees qty 5, not 500), with a `units:` note."""
    oe = []
    for day in (0, 1):
        for i in range(12):
            t = T0 + day * DAY + i * 100 * S
            o, ev = order_events(f"d{day}o{i}", "buy", PX, 500, t, fills=[(t + 3 * S, 500)])
            o["size_exp"] = -2
            for e in ev:
                if e.get("qty") is not None:
                    e["qty"] = 500
            oe.append((o, ev))

    seen: set[int] = set()

    class Recording(ZooByParam):
        def run_zoo(self, spec):
            seen.update(int(r["qty"]) for r in rows(read_parquet(spec.shadows.path)))
            return super().run_zoo(spec)

    fr = Recording()
    cal = calibrate_live(
        {"dataset": {"path": str(_dataset(tmp_path))}, "live": {"path": str(log_from(oe, tmp_path / "live"))},
         "family": "power3", "horizons_ns": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S], "bootstrap": {"b": 10}},
        runner=fr,
    )  # fmt: skip
    assert any(w.startswith("units: live log instrument 1") and "size_exp -2 -> 0" in w for w in cal.warnings)
    assert seen == {5}
